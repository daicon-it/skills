#!/usr/bin/env python3
"""
Watchdog Agent — мониторинг и авто-healing LXC контейнеров и VPS.

Обнаруживает и устраняет:
- OOM / высокое потребление RAM+Swap
- Накопление одинаковых процессов (claude, node, python)
- Зависшие процессы (claude --print, analyzer) по возрасту
- Zombie / D-state процессы
- Упавшие systemd-сервисы
- Высокая загрузка CPU (load average) — renice тяжёлых процессов
- Конфликты cron-задач — одновременный запуск тяжёлых процессов

Запуск: python3 watchdog.py [--dry-run] [--machine CT-234] [--verbose]
Cron:   */5 * * * * /usr/bin/python3 /root/watchdog-agent/watchdog.py >> /root/watchdog-agent/logs/watchdog.log 2>&1
"""

import argparse
import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
STATE_DIR = BASE_DIR / "state"
LOG_DIR = BASE_DIR / "logs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("watchdog")


# ── Helpers ─────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def run_on_machine(machine: dict, cmd: str, ssh_host: str, timeout: int = 15) -> str:
    """Execute command on any machine type (lxc or vps)."""
    mtype = machine["type"]
    if mtype == "lxc":
        full_cmd = (
            f"ssh -o ConnectTimeout=5 root@{ssh_host} "
            f"\"pct exec {machine['vmid']} -- bash -c '{cmd}'\""
        )
    elif mtype == "vps":
        full_cmd = f"{machine['ssh']} \"{cmd}\""
    else:
        return ""
    try:
        r = subprocess.run(
            full_cmd, shell=True, capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""
    except Exception as e:
        log.error(f"{machine['name']} exec failed: {e}")
        return ""


def pct_cmd(ssh_host: str, cmd: str, timeout: int = 10) -> str:
    """Execute command on Proxmox host."""
    full_cmd = f"ssh -o ConnectTimeout=5 root@{ssh_host} \"{cmd}\""
    try:
        r = subprocess.run(
            full_cmd, shell=True, capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout.strip()
    except Exception:
        return ""


@dataclass
class Issue:
    name: str
    severity: str   # warn, crit
    category: str   # ram, swap, duplicate_procs, stale_proc, zombie, service, stopped, unreachable
    message: str
    action_taken: str = ""


@dataclass
class MachineState:
    name: str
    mtype: str
    ram_pct: int = 0
    swap_pct: int = 0
    ram_used: int = 0
    ram_total: int = 0
    swap_used: int = 0
    swap_total: int = 0
    cpu_load: float = 0.0
    cpu_cores: int = 1
    top_procs: list = field(default_factory=list)
    issues: list = field(default_factory=list)


# ── Checks ──────────────────────────────────────────

def check_memory(machine: dict, ssh_host: str, thresholds: dict) -> MachineState:
    """Check RAM and swap usage."""
    name = machine["name"]
    state = MachineState(name=name, mtype=machine["type"])

    output = run_on_machine(machine, "free -m", ssh_host)
    if not output:
        state.issues.append(Issue(name, "crit", "unreachable",
                                  "Cannot connect to machine"))
        return state

    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "Mem:":
            state.ram_total = int(parts[1])
            state.ram_used = int(parts[2])
            state.ram_pct = round(state.ram_used / state.ram_total * 100) if state.ram_total else 0
        elif parts[0] == "Swap:":
            state.swap_total = int(parts[1])
            state.swap_used = int(parts[2])
            state.swap_pct = round(state.swap_used / state.swap_total * 100) if state.swap_total else 0

    if state.ram_pct >= thresholds["ram_crit"]:
        state.issues.append(Issue(name, "crit", "ram",
                                  f"RAM {state.ram_pct}% ({state.ram_used}/{state.ram_total}MB)"))
    elif state.ram_pct >= thresholds["ram_warn"]:
        state.issues.append(Issue(name, "warn", "ram",
                                  f"RAM {state.ram_pct}% ({state.ram_used}/{state.ram_total}MB)"))

    if state.swap_total > 0:
        if state.swap_pct >= thresholds["swap_crit"]:
            state.issues.append(Issue(name, "crit", "swap",
                                      f"Swap {state.swap_pct}% ({state.swap_used}/{state.swap_total}MB)"))
        elif state.swap_pct >= thresholds["swap_warn"]:
            state.issues.append(Issue(name, "warn", "swap",
                                      f"Swap {state.swap_pct}% ({state.swap_used}/{state.swap_total}MB)"))

    return state


def _parse_procs(output: str) -> list[dict]:
    """Parse ps aux output into list of dicts."""
    procs = []
    for line in output.splitlines()[1:]:
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        procs.append({
            "pid": parts[1],
            "cpu": parts[2],
            "mem": parts[3],
            "vsz": parts[4],
            "rss": parts[5],
            "tty": parts[6],
            "stat": parts[7],
            "start": parts[8],
            "time": parts[9],
            "cmd": parts[10],
        })
    return procs


def check_processes(machine: dict, ssh_host: str, thresholds: dict, state: MachineState):
    """Detect duplicate processes, zombies."""
    name = machine["name"]
    protected = machine.get("protected_procs", [])

    output = run_on_machine(machine, "ps aux --sort=-%mem", ssh_host, timeout=10)
    if not output:
        return

    procs = _parse_procs(output)
    state.top_procs = procs[:10]

    # Duplicate process detection
    dup_patterns = ["claude", "node /usr/local/bin/claude", "node /root/.local/bin/claude",
                    "scrapy", "celery", "python3 analyzer", "python3 supervisor"]
    cmd_counts = {}
    for p in procs:
        for pattern in dup_patterns:
            if pattern in p["cmd"]:
                cmd_counts.setdefault(pattern, []).append(p)
                break

    dup_threshold = thresholds.get("duplicate_procs", 5)
    for pattern, proc_list in cmd_counts.items():
        if len(proc_list) >= dup_threshold:
            is_protected = any(pp in pattern for pp in protected)
            severity = "warn" if is_protected else "crit"
            total_rss = sum(int(p["rss"]) for p in proc_list)
            state.issues.append(Issue(
                name, severity, "duplicate_procs",
                f"{len(proc_list)}x '{pattern}' ({total_rss // 1024}MB total RSS)",
            ))

    # Zombie / D-state
    zombies = [p for p in procs if "Z" in p["stat"]]
    d_state = [p for p in procs if p["stat"].startswith("D")]
    if zombies:
        state.issues.append(Issue(name, "warn", "zombie",
                                  f"{len(zombies)} zombie processes"))
    if len(d_state) > 3:
        state.issues.append(Issue(name, "crit", "zombie",
                                  f"{len(d_state)} D-state processes — possible swap thrashing"))


def _parse_lstart(lstart_str: str) -> int | None:
    """Parse ps lstart format 'Wed Mar 22 17:08:35 2026' into elapsed minutes."""
    try:
        start_dt = datetime.strptime(lstart_str.strip(), "%a %b %d %H:%M:%S %Y")
        elapsed = datetime.now() - start_dt
        return max(0, int(elapsed.total_seconds()) // 60)
    except (ValueError, TypeError):
        return None


def check_stale_procs(machine: dict, ssh_host: str, stale_patterns: list, state: MachineState):
    """Detect processes that have been running longer than their max_age_min.

    Uses `ps -eo pid,lstart,tty,args` with absolute start time (works in LXC).
    Skips interactive sessions (tty != '?') — those are user sessions.
    """
    name = machine["name"]
    # lstart gives: "Wed Mar 22 17:08:35 2026" — 5 fields
    output = run_on_machine(machine, "ps -eo pid,lstart,tty,args --no-headers", ssh_host, timeout=10)
    if not output:
        return

    for line in output.splitlines():
        # Format: PID DAY MON DD HH:MM:SS YEAR TTY CMD
        parts = line.split()
        if len(parts) < 8:
            continue
        pid = parts[0]
        lstart_str = " ".join(parts[1:6])  # "Sun Mar 22 17:08:35 2026"
        tty = parts[6]
        cmd = " ".join(parts[7:])

        # Skip interactive sessions
        if tty != "?":
            continue

        elapsed_min = _parse_lstart(lstart_str)
        if elapsed_min is None:
            continue

        for sp in stale_patterns:
            if sp["pattern"] in cmd:
                max_age = sp["max_age_min"]
                if elapsed_min >= max_age:
                    rss_mb = "?"
                    for tp in state.top_procs:
                        if tp["pid"] == pid:
                            rss_mb = f"{int(tp['rss']) // 1024}MB"
                            break
                    state.issues.append(Issue(
                        name, "crit", "stale_proc",
                        f"PID {pid} '{sp['pattern']}' running {elapsed_min}min (max {max_age}min), RSS {rss_mb}",
                    ))
                break


def check_services(machine: dict, ssh_host: str, state: MachineState):
    """Check systemd services."""
    name = machine["name"]
    services = machine.get("services", [])

    for svc in services:
        output = run_on_machine(machine, f"systemctl is-active {svc} 2>/dev/null", ssh_host)
        if output != "active":
            state.issues.append(Issue(name, "crit", "service",
                                      f"Service '{svc}' is {output or 'unknown'}"))


def check_cpu_load(machine: dict, ssh_host: str, thresholds: dict, state: MachineState):
    """Check CPU load average and detect cron job conflicts."""
    name = machine["name"]

    # Get load average and core count
    output = run_on_machine(machine, "cat /proc/loadavg && nproc", ssh_host, timeout=10)
    if not output:
        return

    lines = output.strip().splitlines()
    if not lines:
        return

    load_parts = lines[0].split()
    if len(load_parts) < 3:
        return

    try:
        load5 = float(load_parts[1])  # 5-min load average
        cores = int(lines[1]) if len(lines) > 1 else 1
    except (ValueError, IndexError):
        return

    state.cpu_load = load5
    state.cpu_cores = cores

    warn_threshold = thresholds.get("cpu_load_warn", 3.5)
    crit_threshold = thresholds.get("cpu_load_crit", 6.0)

    if load5 >= crit_threshold:
        state.issues.append(Issue(name, "crit", "cpu_load",
                                  f"CPU load {load5} (5min) on {cores} cores (threshold: {crit_threshold})"))
    elif load5 >= warn_threshold:
        state.issues.append(Issue(name, "warn", "cpu_load",
                                  f"CPU load {load5} (5min) on {cores} cores (threshold: {warn_threshold})"))

    # Detect cron job conflicts — multiple heavy processes running simultaneously
    if load5 >= warn_threshold:
        cpu_min = thresholds.get("cron_conflict_cpu", 15)
        output = run_on_machine(machine,
                                "ps aux --sort=-%cpu | awk 'NR>1 && $3>=" + str(cpu_min) + " {print $2,$3,$11}'",
                                ssh_host, timeout=10)
        if output:
            heavy = []
            for line in output.strip().splitlines():
                parts = line.split(None, 2)
                if len(parts) >= 3:
                    heavy.append({"pid": parts[0], "cpu": parts[1], "cmd": parts[2]})

            if len(heavy) >= 2:
                procs_str = "; ".join(f"{h['cmd'][:50]}(PID {h['pid']}, {h['cpu']}%)" for h in heavy[:5])
                state.issues.append(Issue(
                    name, "warn", "cron_conflict",
                    f"{len(heavy)} heavy processes simultaneous: {procs_str}. "
                    f"Consider spreading cron schedules to avoid overlap",
                ))


def renice_heavy_procs(machine: dict, ssh_host: str, state: MachineState, dry_run: bool) -> str:
    """Renice heavy non-protected processes to reduce CPU contention."""
    protected = machine.get("protected_procs", [])

    output = run_on_machine(machine,
                            "ps aux --sort=-%cpu | awk 'NR>1 && $3>=15 {print $2,$11}'",
                            ssh_host, timeout=10)
    if not output:
        return "No heavy processes to renice"

    reniced = []
    for line in output.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        pid, cmd = parts
        if any(pp in cmd for pp in protected):
            continue
        if any(s in cmd for s in ["/sbin/init", "systemd", "sshd", "journald"]):
            continue

        if dry_run:
            reniced.append(f"[DRY-RUN] renice PID {pid} ({cmd[:40]})")
        else:
            run_on_machine(machine, f"renice 19 -p {pid}", ssh_host, timeout=5)
            reniced.append(f"PID {pid} ({cmd[:40]})")

    if reniced:
        result = f"Reniced {len(reniced)}: {'; '.join(reniced)}"
        if not dry_run:
            log.warning(f"{machine['name']}: {result}")
        return result
    return "No non-protected heavy processes to renice"


# ── Actions ─────────────────────────────────────────

def kill_heavy_procs(machine: dict, ssh_host: str, state: MachineState, dry_run: bool) -> list[str]:
    """Kill heaviest non-protected processes."""
    protected = machine.get("protected_procs", [])
    actions = []

    for p in state.top_procs[:20]:
        cmd = p["cmd"]
        rss_mb = int(p["rss"]) // 1024
        if any(pp in cmd for pp in protected):
            continue
        if rss_mb < 50:
            continue
        if any(s in cmd for s in ["/sbin/init", "systemd", "sshd", "journald"]):
            continue

        action = f"kill PID {p['pid']} ({cmd[:60]}, {rss_mb}MB)"
        if dry_run:
            actions.append(f"[DRY-RUN] {action}")
        else:
            run_on_machine(machine, f"kill -9 {p['pid']}", ssh_host, timeout=5)
            actions.append(action)
            log.warning(f"{machine['name']}: {action}")

    return actions


def kill_duplicate_procs(machine: dict, ssh_host: str, thresholds: dict, dry_run: bool) -> list[str]:
    """Kill duplicate processes, keeping the oldest one."""
    protected = machine.get("protected_procs", [])
    actions = []

    output = run_on_machine(machine, "ps aux --sort=lstart", ssh_host, timeout=10)
    if not output:
        return actions

    killable_patterns = ["claude", "node /usr/local/bin/claude", "node /root/.local/bin/claude",
                         "python3 analyzer"]
    cmd_groups = {}
    for line in output.splitlines()[1:]:
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        cmd = parts[10]
        for pattern in killable_patterns:
            if pattern in cmd:
                cmd_groups.setdefault(pattern, []).append(parts[1])
                break

    dup_threshold = thresholds.get("duplicate_procs", 5)
    for pattern, pids in cmd_groups.items():
        if len(pids) < dup_threshold:
            continue
        if any(pp in pattern for pp in protected):
            continue
        kill_pids = pids[1:]
        action = f"kill {len(kill_pids)} duplicate '{pattern}' (keep PID {pids[0]})"
        if dry_run:
            actions.append(f"[DRY-RUN] {action}")
        else:
            pid_str = " ".join(kill_pids)
            run_on_machine(machine, f"kill -9 {pid_str}", ssh_host, timeout=10)
            actions.append(action)
            log.warning(f"{machine['name']}: {action}")

    return actions


def kill_stale_proc(machine: dict, ssh_host: str, issue: Issue, dry_run: bool) -> str:
    """Kill a specific stale process identified in an issue."""
    # Extract PID from message: "PID 12345 ..."
    match = re.search(r"PID (\d+)", issue.message)
    if not match:
        return "Cannot parse PID"
    pid = match.group(1)

    if dry_run:
        return f"[DRY-RUN] Would kill PID {pid}"

    run_on_machine(machine, f"kill {pid}", ssh_host, timeout=5)
    time.sleep(2)
    # Verify killed
    check = run_on_machine(machine, f"kill -0 {pid} 2>&1 && echo alive || echo dead", ssh_host, timeout=5)
    if "dead" in check or not check:
        log.warning(f"{machine['name']}: Killed stale PID {pid}")
        return f"Killed PID {pid}"
    else:
        # Force kill
        run_on_machine(machine, f"kill -9 {pid}", ssh_host, timeout=5)
        log.warning(f"{machine['name']}: Force-killed stale PID {pid}")
        return f"Force-killed PID {pid}"


def drop_caches(machine: dict, ssh_host: str, dry_run: bool) -> str:
    if dry_run:
        return "[DRY-RUN] Would drop kernel caches"
    run_on_machine(machine, "sync && echo 3 > /proc/sys/vm/drop_caches", ssh_host, timeout=10)
    return "Dropped kernel caches"


def restart_service(machine: dict, ssh_host: str, service: str, dry_run: bool) -> str:
    if dry_run:
        return f"[DRY-RUN] Would restart {service}"
    run_on_machine(machine, f"systemctl restart {service}", ssh_host, timeout=30)
    time.sleep(3)
    status = run_on_machine(machine, f"systemctl is-active {service}", ssh_host, timeout=5)
    return f"Restarted {service} → {status}"


def reboot_container(ssh_host: str, vmid: int, dry_run: bool) -> str:
    if dry_run:
        return f"[DRY-RUN] Would reboot CT-{vmid}"
    pct_cmd(ssh_host, f"pct reboot {vmid}", timeout=30)
    return f"Rebooted CT-{vmid}"


# ── Main Loop ───────────────────────────────────────

def process_machine(machine: dict, config: dict, dry_run: bool) -> MachineState:
    """Run all checks and actions for a single machine."""
    ssh_host = config["ssh_host"]
    thresholds = config["thresholds"]
    actions_cfg = config["actions"]
    stale_patterns = config.get("stale_proc_patterns", [])

    # For LXC: check container is running
    if machine["type"] == "lxc":
        status = pct_cmd(ssh_host, f"pct status {machine['vmid']}")
        if "running" not in status:
            state = MachineState(name=machine["name"], mtype=machine["type"])
            state.issues.append(Issue(machine["name"], "crit", "stopped",
                                      f"Container is {status}"))
            return state

    # Run checks
    state = check_memory(machine, ssh_host, thresholds)
    if any(i.category == "unreachable" for i in state.issues):
        return state  # Skip other checks if unreachable

    check_processes(machine, ssh_host, thresholds, state)
    check_stale_procs(machine, ssh_host, stale_patterns, state)
    check_services(machine, ssh_host, state)
    check_cpu_load(machine, ssh_host, thresholds, state)

    # Apply actions
    for issue in list(state.issues):
        if issue.category == "stale_proc" and issue.severity == "crit":
            if actions_cfg.get("kill_stale_procs"):
                issue.action_taken = kill_stale_proc(machine, ssh_host, issue, dry_run)

        elif issue.category == "duplicate_procs" and issue.severity == "crit":
            if actions_cfg.get("kill_duplicate_procs"):
                results = kill_duplicate_procs(machine, ssh_host, thresholds, dry_run)
                issue.action_taken = "; ".join(results) if results else "No killable duplicates"

        elif issue.category == "ram" and issue.severity == "crit":
            if actions_cfg.get("kill_on_ram_crit"):
                results = kill_heavy_procs(machine, ssh_host, state, dry_run)
                issue.action_taken = "; ".join(results) if results else "No killable processes"

        elif issue.category == "swap" and issue.severity == "crit":
            if actions_cfg.get("drop_caches_on_swap_crit"):
                issue.action_taken = drop_caches(machine, ssh_host, dry_run)
            if machine["type"] == "lxc" and state.swap_pct >= actions_cfg.get("reboot_swap_threshold", 95):
                if actions_cfg.get("reboot_on_emergency"):
                    reboot_action = reboot_container(ssh_host, machine["vmid"], dry_run)
                    issue.action_taken += f"; {reboot_action}"

        elif issue.category == "cpu_load" and issue.severity == "crit":
            if actions_cfg.get("renice_on_cpu_crit"):
                issue.action_taken = renice_heavy_procs(machine, ssh_host, state, dry_run)

        elif issue.category == "cron_conflict":
            # Log only — cron conflicts are informational for manual review
            issue.action_taken = "Logged for review — check watchdog.log"

        elif issue.category == "service":
            svc_name = issue.message.split("'")[1]
            issue.action_taken = restart_service(machine, ssh_host, svc_name, dry_run)

    return state


def save_state(states: list[MachineState]):
    """Save current state for trend analysis."""
    STATE_DIR.mkdir(exist_ok=True)
    state_file = STATE_DIR / f"{datetime.now().strftime('%Y-%m-%d_%H-%M')}.json"
    data = []
    for s in states:
        data.append({
            "name": s.name,
            "type": s.mtype,
            "ram_pct": s.ram_pct,
            "swap_pct": s.swap_pct,
            "cpu_load": s.cpu_load,
            "issues": [
                {"severity": i.severity, "category": i.category,
                 "message": i.message, "action": i.action_taken}
                for i in s.issues
            ],
        })
    with open(state_file, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # Rotate: keep last 24h (288 files at */5)
    files = sorted(STATE_DIR.glob("*.json"))
    while len(files) > 288:
        files[0].unlink()
        files.pop(0)


def print_summary(states: list[MachineState]):
    total_issues = sum(len(s.issues) for s in states)
    crits = sum(1 for s in states for i in s.issues if i.severity == "crit")
    warns = sum(1 for s in states for i in s.issues if i.severity == "warn")

    for s in states:
        if not s.issues:
            status = "OK"
        elif any(i.severity == "crit" for i in s.issues):
            status = "CRIT"
        else:
            status = "WARN"
        cpu_str = f" | CPU {s.cpu_load:.1f}/{s.cpu_cores}cores" if s.cpu_load > 0 else ""
        log.info(f"{s.name}: {status} | RAM {s.ram_pct}% | Swap {s.swap_pct}%{cpu_str}")
        for i in s.issues:
            action_str = f" → {i.action_taken}" if i.action_taken else ""
            log.info(f"  [{i.severity.upper()}] {i.category}: {i.message}{action_str}")

    log.info(f"Summary: {len(states)} machines, {total_issues} issues ({crits} crit, {warns} warn)")


def main():
    parser = argparse.ArgumentParser(description="Watchdog Agent")
    parser.add_argument("--dry-run", action="store_true", help="Check only, no actions")
    parser.add_argument("--machine", type=str, help="Check specific machine by name")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config()
    machines = config["machines"]

    if args.machine:
        machines = [m for m in machines if m["name"].lower() == args.machine.lower()]
        if not machines:
            log.error(f"Machine '{args.machine}' not found in config")
            sys.exit(1)

    states = []
    for m in machines:
        log.info(f"Checking {m['name']}...")
        state = process_machine(m, config, args.dry_run)
        states.append(state)

    save_state(states)
    print_summary(states)

    if any(i.severity == "crit" for s in states for i in s.issues):
        sys.exit(2)
    elif any(i.severity == "warn" for s in states for i in s.issues):
        sys.exit(1)


if __name__ == "__main__":
    main()
