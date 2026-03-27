"""Health check functions for each machine."""

import json
import subprocess
from dataclasses import dataclass, field
from executor import Executor


@dataclass
class CheckResult:
    name: str
    status: str  # ok, warn, crit, error, info
    value: str
    details: str


def check_system(executor: Executor, machine: dict, thresholds: dict) -> list[CheckResult]:
    """Batch system check: disk, RAM, swap, load, uptime in one SSH call."""
    cmd = (
        "df -h / 2>/dev/null | tail -1;"
        "echo '---SEPARATOR---';"
        "free -m 2>/dev/null | head -3;"
        "echo '---SEPARATOR---';"
        "uptime;"
        "echo '---SEPARATOR---';"
        "cat /proc/loadavg 2>/dev/null;"
        "echo '---SEPARATOR---';"
        "nproc 2>/dev/null || echo 1"
    )
    result = executor.run(machine, cmd, timeout=15)
    if result.returncode != 0 and not result.stdout.strip():
        return [CheckResult("system", "error", "N/A", f"Command failed: {result.stderr.strip()[:100]}")]

    parts = result.stdout.split("---SEPARATOR---")
    if len(parts) < 5:
        return [CheckResult("system", "error", "N/A", f"Unexpected output format")]

    results = []

    # Disk
    try:
        disk_line = parts[0].strip().split()
        disk_size = disk_line[1]
        disk_used = disk_line[2]
        disk_avail = disk_line[3]
        disk_pct = int(disk_line[4].rstrip("%"))
        mount = disk_line[5] if len(disk_line) > 5 else "/"
        status = "ok"
        if disk_pct >= thresholds.get("disk_crit", 90):
            status = "crit"
        elif disk_pct >= thresholds.get("disk_warn", 80):
            status = "warn"
        results.append(CheckResult("Disk", status, f"{disk_pct}%",
                                   f"{disk_used}/{disk_size} (avail {disk_avail})"))
    except (IndexError, ValueError):
        results.append(CheckResult("Disk", "error", "N/A", "Parse error"))

    # RAM
    try:
        mem_lines = parts[1].strip().splitlines()
        mem_parts = mem_lines[1].split()  # Mem: total used free shared buff/cache available
        total = int(mem_parts[1])
        used = int(mem_parts[2])
        available = int(mem_parts[6]) if len(mem_parts) > 6 else total - used
        ram_pct = round((total - available) / total * 100) if total > 0 else 0
        status = "ok"
        if ram_pct >= thresholds.get("ram_crit", 90):
            status = "crit"
        elif ram_pct >= thresholds.get("ram_warn", 80):
            status = "warn"
        results.append(CheckResult("RAM", status, f"{ram_pct}%",
                                   f"{used}M used / {total}M total ({available}M avail)"))

        # Swap
        if len(mem_lines) > 2:
            swap_parts = mem_lines[2].split()
            swap_total = int(swap_parts[1])
            swap_used = int(swap_parts[2])
            if swap_total > 0:
                swap_pct = round(swap_used / swap_total * 100)
                status = "warn" if swap_pct >= thresholds.get("swap_warn", 50) else "ok"
                results.append(CheckResult("Swap", status, f"{swap_pct}%",
                                           f"{swap_used}M / {swap_total}M"))
            else:
                results.append(CheckResult("Swap", "info", "N/A", "No swap configured"))
    except (IndexError, ValueError):
        results.append(CheckResult("RAM", "error", "N/A", "Parse error"))

    # Uptime
    try:
        uptime_str = parts[2].strip()
        # Extract "up X days, HH:MM" part
        up_idx = uptime_str.find("up ")
        if up_idx >= 0:
            up_part = uptime_str[up_idx + 3:]
            up_part = up_part.split(",  ")[0].split(", load")[0].strip().rstrip(",")
            # Remove user count part
            for sep in [" user", " users"]:
                idx = up_part.find(sep)
                if idx >= 0:
                    up_part = up_part[:up_part.rfind(",", 0, idx)].strip().rstrip(",")
            results.append(CheckResult("Uptime", "info", up_part, ""))
    except Exception:
        pass

    # Load
    try:
        loadavg = parts[3].strip().split()
        load1 = float(loadavg[0])
        load5 = float(loadavg[1])
        load15 = float(loadavg[2])
        ncpu = int(parts[4].strip())
        per_core = load5 / ncpu if ncpu > 0 else load5
        status = "warn" if per_core >= thresholds.get("load_per_core_warn", 2.0) else "ok"
        details = f"{load1:.2f} / {load5:.2f} / {load15:.2f} ({ncpu} cores)"

        # If load is high, diagnose top CPU consumers
        if status == "warn":
            diag = _diagnose_cpu(executor, machine)
            if diag:
                details += f" | Top: {diag}"

        results.append(CheckResult("Load", status, f"{load1:.2f}", details))
    except (IndexError, ValueError):
        results.append(CheckResult("Load", "error", "N/A", "Parse error"))

    return results


def _diagnose_cpu(executor: Executor, machine: dict) -> str:
    """Get top CPU-consuming processes for diagnostics."""
    cmd = "ps aux --sort=-%cpu | awk 'NR>1 && $3>10 {printf \"%s(%s%%)\", $11, $3; if(NR<6) printf \", \"}' | head -c 200"
    result = executor.run(machine, cmd, timeout=10)
    return result.stdout.strip() if result.returncode == 0 else ""


def check_cpu_remediate(executor: Executor, machine: dict, thresholds: dict) -> list[CheckResult]:
    """Diagnose high CPU load and apply remediation (renice heavy processes)."""
    cmd = (
        "cat /proc/loadavg;"
        "echo '---SEPARATOR---';"
        "nproc;"
        "echo '---SEPARATOR---';"
        "ps aux --sort=-%cpu | head -11"
    )
    result = executor.run(machine, cmd, timeout=15)
    if result.returncode != 0:
        return [CheckResult("CPU Remediate", "error", "N/A", "Command failed")]

    parts = result.stdout.split("---SEPARATOR---")
    if len(parts) < 3:
        return [CheckResult("CPU Remediate", "error", "N/A", "Unexpected format")]

    results = []
    try:
        loadavg = parts[0].strip().split()
        load5 = float(loadavg[1])
        ncpu = int(parts[1].strip())
        per_core = load5 / ncpu if ncpu > 0 else load5
        ps_output = parts[2].strip()

        if per_core < thresholds.get("load_per_core_warn", 2.0):
            return [CheckResult("CPU Remediate", "ok", f"load {load5:.1f}",
                                f"Per core: {per_core:.2f} — no action needed")]

        # Parse top processes
        lines = ps_output.splitlines()
        heavy = []
        for line in lines[1:]:  # skip header
            cols = line.split()
            if len(cols) >= 11 and float(cols[2]) > 15.0:
                pid = cols[1]
                cpu_pct = cols[2]
                cmd_name = cols[10].split("/")[-1]
                heavy.append({"pid": pid, "cpu": cpu_pct, "cmd": cmd_name})

        top_summary = ", ".join(f"{p['cmd']}({p['cpu']}%)" for p in heavy[:5])
        results.append(CheckResult("CPU Diag", "warn", f"load {load5:.1f}",
                                   f"Per core: {per_core:.2f} | Top: {top_summary}"))

        # Remediation: renice node/npm/jest workers to 19 (lowest priority)
        renice_targets = [p for p in heavy if p["cmd"] in ("node", "npm", "jest", "ts-node", "npx")]
        if renice_targets:
            pids = " ".join(p["pid"] for p in renice_targets)
            renice_cmd = f"renice 19 -p {pids} 2>&1 | tail -3"
            r = executor.run(machine, renice_cmd, timeout=10)
            reniced = ", ".join(f"PID {p['pid']}({p['cmd']})" for p in renice_targets)
            results.append(CheckResult("CPU Renice", "info",
                                       f"{len(renice_targets)} proc",
                                       f"Reniced to 19: {reniced}"))
        else:
            results.append(CheckResult("CPU Renice", "info", "skip",
                                       "No node/npm processes to renice"))

    except (IndexError, ValueError) as e:
        results.append(CheckResult("CPU Remediate", "error", "N/A", str(e)[:80]))

    return results


def check_tailscale(executor: Executor, machine: dict, thresholds: dict) -> list[CheckResult]:
    """Check Tailscale status."""
    result = executor.run(machine, "tailscale status --json 2>/dev/null", timeout=10)
    if result.returncode != 0:
        # Try without --json
        result2 = executor.run(machine, "tailscale status 2>/dev/null | head -1", timeout=10)
        if result2.returncode != 0:
            return [CheckResult("Tailscale", "error", "N/A", "Not running or not installed")]
        return [CheckResult("Tailscale", "info", "running", result2.stdout.strip()[:80])]

    try:
        data = json.loads(result.stdout)
        state = data.get("BackendState", "Unknown")
        exit_node = ""
        if data.get("ExitNodeStatus"):
            exit_node = ", exit node active"
        ts_ips = data.get("TailscaleIPs", [])
        ip_str = ts_ips[0] if ts_ips else ""

        if state == "Running":
            return [CheckResult("Tailscale", "ok", f"{state}",
                                f"{ip_str}{exit_node}")]
        else:
            return [CheckResult("Tailscale", "warn", state, ip_str)]
    except (json.JSONDecodeError, KeyError):
        return [CheckResult("Tailscale", "info", "running", "JSON parse failed")]


def check_apt(executor: Executor, machine: dict, thresholds: dict) -> list[CheckResult]:
    """Check available apt updates."""
    result = executor.run(machine,
                          "apt list --upgradable 2>/dev/null | grep -c upgradable || echo 0",
                          timeout=30)
    if result.returncode != 0 and not result.stdout.strip():
        return [CheckResult("APT", "error", "N/A", "Failed to check")]

    try:
        count = int(result.stdout.strip())
        status = "ok" if count == 0 else ("warn" if count >= thresholds.get("updates_warn", 50) else "info")
        label = "up to date" if count == 0 else f"{count} updates"
        return [CheckResult("APT", status, label, "")]
    except ValueError:
        return [CheckResult("APT", "info", "?", result.stdout.strip()[:60])]


def check_cron(executor: Executor, machine: dict, thresholds: dict) -> list[CheckResult]:
    """List cron jobs (informational)."""
    result = executor.run(machine, "crontab -l 2>/dev/null | grep -v '^#' | grep -v '^$' | wc -l", timeout=10)
    try:
        count = int(result.stdout.strip())
        return [CheckResult("Cron", "info", f"{count} jobs", "")]
    except ValueError:
        return [CheckResult("Cron", "info", "0 jobs", "")]


def check_services(executor: Executor, machine: dict, thresholds: dict) -> list[CheckResult]:
    """Check systemd service status."""
    service_list = machine.get("services", [])
    if not service_list:
        return []

    results = []
    for svc in service_list:
        result = executor.run(machine, f"systemctl is-active {svc} 2>/dev/null", timeout=10)
        state = result.stdout.strip()
        if state == "active":
            results.append(CheckResult(f"Svc:{svc}", "ok", "active", ""))
        else:
            results.append(CheckResult(f"Svc:{svc}", "crit", state or "unknown", ""))
    return results


def check_postgresql(executor: Executor, machine: dict, thresholds: dict) -> list[CheckResult]:
    """Check PostgreSQL service and database connectivity."""
    results = []

    # Check service status on CT 231 via executor
    svc_result = executor.run(machine, "systemctl is-active postgresql 2>/dev/null", timeout=10)
    svc_state = svc_result.stdout.strip()
    if svc_state == "active":
        results.append(CheckResult("PostgreSQL", "ok", "active", ""))
    else:
        results.append(CheckResult("PostgreSQL", "crit", svc_state or "unknown", ""))
        return results  # No point checking DBs if service is down

    # Check database connectivity from local (CT 101)
    pg_conf = machine.get("postgresql", {})
    host = pg_conf.get("host", "100.124.99.73")
    port = pg_conf.get("port", 5432)
    databases = pg_conf.get("databases", [])

    ok_count = 0
    fail_dbs = []
    for db in databases:
        try:
            r = subprocess.run(
                ["pg_isready", "-h", host, "-p", str(port), "-d", db],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0:
                ok_count += 1
            else:
                fail_dbs.append(db)
        except Exception:
            fail_dbs.append(db)

    if fail_dbs:
        results.append(CheckResult("PG DBs", "crit",
                                   f"{ok_count}/{len(databases)}",
                                   f"Failed: {', '.join(fail_dbs)}"))
    else:
        results.append(CheckResult("PG DBs", "ok",
                                   f"{ok_count}/{len(databases)} reachable", ""))

    return results


def check_login_audit(executor: Executor, machine: dict, thresholds: dict) -> list[CheckResult]:
    """Check for SSH logins from non-Tailscale IPs (outside 100.64.0.0/10)."""
    cmd = (
        "last -i -n 50 2>/dev/null | "
        "grep -v '^$' | grep -v '^wtmp' | grep -v '^reboot' | "
        "awk '{for(i=1;i<=NF;i++) if($i ~ /^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+$/) print $i}' | "
        "sort -u"
    )
    result = executor.run(machine, cmd, timeout=10)
    if result.returncode != 0 and not result.stdout.strip():
        return [CheckResult("Login Audit", "info", "N/A", "Could not read login history")]

    ips = [ip.strip() for ip in result.stdout.strip().splitlines() if ip.strip()]
    if not ips:
        return [CheckResult("Login Audit", "ok", "No logins", "")]

    # Tailscale CGNAT range: 100.64.0.0/10 → 100.64.0.0 - 100.127.255.255
    foreign = []
    tailscale = []
    for ip in ips:
        try:
            octets = list(map(int, ip.split(".")))
            if octets[0] == 100 and 64 <= octets[1] <= 127:
                tailscale.append(ip)
            else:
                foreign.append(ip)
        except (ValueError, IndexError):
            foreign.append(ip)

    if foreign:
        return [CheckResult("Login Audit", "warn", f"{len(foreign)} foreign IP(s)",
                            f"Non-Tailscale: {', '.join(foreign[:5])}")]
    else:
        return [CheckResult("Login Audit", "ok", f"{len(tailscale)} Tailscale IP(s)", "")]


def check_orphaned_processes(executor: Executor, machine: dict, thresholds: dict) -> list[CheckResult]:
    """Find orphaned processes (jest-worker, zombie node/npm) that waste RAM."""
    cmd = (
        "ps aux --no-headers 2>/dev/null | "
        "grep -E 'jest-worker|jest\\.js|node.*--max-old|npx.*vitest' | "
        "grep -v grep | "
        "awk '{sum+=$6; count++} END {printf \"%d %d\", count, sum/1024}'"
    )
    result = executor.run(machine, cmd, timeout=10)
    if result.returncode != 0 and not result.stdout.strip():
        return [CheckResult("Orphans", "info", "N/A", "Could not check")]

    try:
        parts = result.stdout.strip().split()
        count = int(parts[0]) if parts else 0
        ram_mb = int(parts[1]) if len(parts) > 1 else 0

        if count == 0:
            return [CheckResult("Orphans", "ok", "clean", "No orphaned processes")]

        status = "crit" if ram_mb > 500 else ("warn" if count > 5 else "info")
        return [CheckResult("Orphans", status, f"{count} proc",
                            f"{ram_mb}MB RAM | jest-worker/node zombies")]
    except (ValueError, IndexError):
        return [CheckResult("Orphans", "info", "?", result.stdout.strip()[:60])]


def check_assess(executor: Executor, machine: dict, thresholds: dict) -> list[CheckResult]:
    """Detailed resource assessment for new project capacity planning."""
    cmd = (
        "df -h / 2>/dev/null | tail -1;"
        "echo '---SEPARATOR---';"
        "free -m 2>/dev/null | head -3;"
        "echo '---SEPARATOR---';"
        "nproc 2>/dev/null || echo 1;"
        "echo '---SEPARATOR---';"
        "docker ps --format '{{.Names}}: {{.Status}}' 2>/dev/null || echo 'no-docker';"
        "echo '---SEPARATOR---';"
        "ps aux --no-headers 2>/dev/null | wc -l;"
        "echo '---SEPARATOR---';"
        "du -sh /root/*/  2>/dev/null | sort -rh | head -10"
    )
    result = executor.run(machine, cmd, timeout=15)
    if result.returncode != 0 and not result.stdout.strip():
        return [CheckResult("Assess", "error", "N/A", "Command failed")]

    parts = result.stdout.split("---SEPARATOR---")
    if len(parts) < 6:
        return [CheckResult("Assess", "error", "N/A", "Unexpected format")]

    results = []

    # Disk: free space
    try:
        disk = parts[0].strip().split()
        avail = disk[3]
        pct = int(disk[4].rstrip("%"))
        status = "crit" if pct >= 90 else ("warn" if pct >= 70 else "ok")
        results.append(CheckResult("Disk Free", status, avail, f"{pct}% used"))
    except (IndexError, ValueError):
        results.append(CheckResult("Disk Free", "error", "N/A", "Parse error"))

    # RAM: available
    try:
        mem_lines = parts[1].strip().splitlines()
        mem = mem_lines[1].split()
        total = int(mem[1])
        available = int(mem[6]) if len(mem) > 6 else int(mem[3])
        results.append(CheckResult("RAM Avail", "ok" if available > 512 else "warn",
                                   f"{available}M", f"of {total}M total"))
    except (IndexError, ValueError):
        results.append(CheckResult("RAM Avail", "error", "N/A", "Parse error"))

    # CPU cores
    try:
        ncpu = int(parts[2].strip())
        results.append(CheckResult("CPU Cores", "info", str(ncpu), ""))
    except ValueError:
        pass

    # Docker containers
    docker_out = parts[3].strip()
    if docker_out == "no-docker":
        results.append(CheckResult("Docker", "info", "N/A", "Not installed"))
    elif docker_out:
        containers = [l.strip() for l in docker_out.splitlines() if l.strip()]
        results.append(CheckResult("Docker", "info", f"{len(containers)} running",
                                   "; ".join(containers)[:120]))
    else:
        results.append(CheckResult("Docker", "info", "0 running", ""))

    # Process count
    try:
        proc_count = int(parts[4].strip())
        status = "warn" if proc_count > 200 else "ok"
        results.append(CheckResult("Processes", status, str(proc_count), ""))
    except ValueError:
        pass

    # Disk usage by project
    du_out = parts[5].strip()
    if du_out:
        dirs = [l.strip() for l in du_out.splitlines() if l.strip()][:5]
        results.append(CheckResult("Top dirs", "info", f"{len(dirs)} dirs",
                                   "; ".join(dirs)[:120]))

    return results


# Registry of check functions
CHECK_REGISTRY = {
    "system": check_system,
    "tailscale": check_tailscale,
    "apt": check_apt,
    "cron": check_cron,
    "services": check_services,
    "postgresql": check_postgresql,
    "login_audit": check_login_audit,
    "cpu_remediate": check_cpu_remediate,
    "orphaned_processes": check_orphaned_processes,
    "assess": check_assess,
}
