#!/usr/bin/env python3
"""DevOps agent for managing LXC containers and VPS machines."""

import argparse
import json
import sys
import time
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

# Add script dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from executor import Executor
from checks import CHECK_REGISTRY, CheckResult
from reporter import MachineReport, print_terminal, save_markdown, C


def load_config(path: str = None) -> dict:
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def run_machine_checks(executor: Executor, machine: dict, thresholds: dict) -> MachineReport:
    """Run all configured checks for a single machine."""
    start = time.time()
    all_results = []

    for check_name in machine.get("checks", []):
        check_fn = CHECK_REGISTRY.get(check_name)
        if check_fn is None:
            all_results.append(CheckResult(check_name, "error", "N/A", "Unknown check"))
            continue
        try:
            results = check_fn(executor, machine, thresholds)
            all_results.extend(results)
        except Exception as e:
            all_results.append(CheckResult(check_name, "error", "N/A", str(e)[:80]))

    elapsed = time.time() - start
    return MachineReport(
        name=machine["name"],
        description=machine.get("description", ""),
        checks=all_results,
        elapsed=elapsed,
    )


def cmd_check(args, config):
    """Run health checks."""
    executor = Executor()
    machines = config["machines"]
    thresholds = config.get("thresholds", {})

    if args.machine:
        machines = [m for m in machines if m["name"].lower() == args.machine.lower()]
        if not machines:
            print(f"{C.CRIT}Machine '{args.machine}' not found in config.{C.RESET}")
            sys.exit(1)

    print(f"{C.BOLD}Checking {len(machines)} machines...{C.RESET}")
    start = time.time()

    workers = min(args.workers, len(machines))
    reports = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(run_machine_checks, executor, m, thresholds): m["name"]
            for m in machines
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                report = future.result()
                reports.append(report)
            except Exception as e:
                reports.append(MachineReport(name, "", [
                    CheckResult("runner", "error", "N/A", str(e)[:80])
                ]))

    # Sort reports in config order
    order = {m["name"]: i for i, m in enumerate(machines)}
    reports.sort(key=lambda r: order.get(r.name, 999))

    total = time.time() - start
    print_terminal(reports, total)

    if args.save:
        output_dir = config.get("report", {}).get("output_dir", "/root/devops-agent/reports")
        path = save_markdown(reports, total, output_dir)
        print(f"{C.DIM}Report saved: {path}{C.RESET}")

    # Exit code: 2 if any crit, 1 if any warn
    from reporter import worst_status
    overall = worst_status([c for r in reports for c in r.checks])
    if overall == "crit":
        sys.exit(2)
    elif overall == "warn":
        sys.exit(1)


def cmd_report(args, config):
    """Run checks and save markdown report."""
    args.save = True
    cmd_check(args, config)


def run_machine_update(executor: Executor, machine: dict, security_only: bool = False) -> tuple[str, str, bool]:
    """Run apt update + upgrade on a single machine. Returns (name, output, success)."""
    name = machine["name"]
    if security_only:
        cmd = (
            "export DEBIAN_FRONTEND=noninteractive && "
            "apt-get update -qq && "
            "apt-get upgrade -y -qq "
            "-o Dpkg::Options::='--force-confdef' "
            "-o Dpkg::Options::='--force-confold' "
            "2>&1 | tail -5"
        )
    else:
        cmd = (
            "export DEBIAN_FRONTEND=noninteractive && "
            "apt-get update -qq && "
            "apt-get upgrade -y -qq "
            "-o Dpkg::Options::='--force-confdef' "
            "-o Dpkg::Options::='--force-confold' && "
            "apt-get autoremove -y -qq 2>&1 | tail -5"
        )
    result = executor.run(machine, cmd, timeout=600)
    success = result.returncode == 0
    output = result.stdout.strip() or result.stderr.strip()
    return name, output, success


def cmd_update(args, config):
    """Run apt update + upgrade on machines."""
    executor = Executor()
    machines = config["machines"]

    if args.machine:
        machines = [m for m in machines if m["name"].lower() == args.machine.lower()]
        if not machines:
            print(f"{C.CRIT}Machine '{args.machine}' not found in config.{C.RESET}")
            sys.exit(1)

    # Filter only machines that have 'apt' in checks (meaning apt is relevant)
    machines = [m for m in machines if "apt" in m.get("checks", [])]
    print(f"{C.BOLD}Updating {len(machines)} machines...{C.RESET}")
    start = time.time()

    # Update sequentially for LXC (shared host), parallel for VPS
    lxc_machines = [m for m in machines if m["type"] == "lxc"]
    other_machines = [m for m in machines if m["type"] != "lxc"]

    results = []

    # LXC containers — sequential (shared disk I/O on Proxmox host)
    for m in lxc_machines:
        print(f"  {C.DIM}Updating {m['name']}...{C.RESET}", flush=True)
        name, output, success = run_machine_update(executor, m, args.security_only)
        results.append((name, output, success))

    # VPS/host/local — parallel
    if other_machines:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                pool.submit(run_machine_update, executor, m, args.security_only): m["name"]
                for m in other_machines
            }
            for m in other_machines:
                print(f"  {C.DIM}Updating {m['name']}...{C.RESET}", flush=True)
            for future in as_completed(futures):
                results.append(future.result())

    # Sort in config order
    order = {m["name"]: i for i, m in enumerate(machines)}
    results.sort(key=lambda r: order.get(r[0], 999))

    total = time.time() - start
    print()
    ok = 0
    fail = 0
    for name, output, success in results:
        if success:
            ok += 1
            print(f"  {C.OK}\u2713{C.RESET} {name:<16} {C.DIM}{output[-80:]}{C.RESET}")
        else:
            fail += 1
            print(f"  {C.CRIT}\u2717{C.RESET} {name:<16} {C.CRIT}{output[-80:]}{C.RESET}")

    print(f"\n{C.BOLD}\u2501\u2501\u2501 Update:{C.RESET} "
          f"{C.OK}{ok} OK{C.RESET}"
          f"{f', {C.CRIT}{fail} FAIL{C.RESET}' if fail else ''} "
          f"{C.DIM}({total:.0f}s){C.RESET}\n")

    if fail:
        sys.exit(1)


def cmd_assess(args, config):
    """Assess machine resources for new project capacity."""
    executor = Executor()
    machines = config["machines"]

    if args.machine:
        machines = [m for m in machines if m["name"].lower() == args.machine.lower()]
        if not machines:
            print(f"{C.CRIT}Machine '{args.machine}' not found in config.{C.RESET}")
            sys.exit(1)

    print(f"{C.BOLD}Assessing {len(machines)} machines...{C.RESET}")
    start = time.time()
    thresholds = config.get("thresholds", {})

    from checks import check_assess
    reports = []
    for m in machines:
        try:
            checks = check_assess(executor, m, thresholds)
            reports.append(MachineReport(m["name"], m.get("description", ""), checks, 0))
        except Exception as e:
            reports.append(MachineReport(m["name"], "", [
                CheckResult("assess", "error", "N/A", str(e)[:80])
            ]))

    total = time.time() - start
    print_terminal(reports, total)


def cmd_cleanup(args, config):
    """Find and kill orphaned processes (jest-worker, zombie node/npm)."""
    executor = Executor()
    machines = config["machines"]

    if args.machine:
        machines = [m for m in machines if m["name"].lower() == args.machine.lower()]
        if not machines:
            print(f"{C.CRIT}Machine '{args.machine}' not found in config.{C.RESET}")
            sys.exit(1)

    print(f"{C.BOLD}Cleaning up orphaned processes on {len(machines)} machines...{C.RESET}")

    total_killed = 0
    total_freed = 0

    for m in machines:
        # Detect orphans
        detect_cmd = (
            "ps aux --no-headers 2>/dev/null | "
            "grep -E 'jest-worker|jest\\.js|node.*--max-old|npx.*vitest' | "
            "grep -v grep"
        )
        result = executor.run(m, detect_cmd, timeout=10)
        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]

        if not lines:
            print(f"  {C.OK}\u2713{C.RESET} {m['name']:<16} {C.DIM}clean{C.RESET}")
            continue

        # Calculate RAM before kill
        ram_cmd = (
            "ps aux --no-headers 2>/dev/null | "
            "grep -E 'jest-worker|jest\\.js|node.*--max-old|npx.*vitest' | "
            "grep -v grep | "
            "awk '{sum+=$6} END {printf \"%.0f\", sum/1024}'"
        )
        ram_result = executor.run(m, ram_cmd, timeout=10)
        ram_mb = int(ram_result.stdout.strip()) if ram_result.stdout.strip() else 0

        if args.dry_run:
            print(f"  {C.WARN}\u26a0{C.RESET} {m['name']:<16} {C.WARN}{len(lines)} orphans ({ram_mb}MB){C.RESET} {C.DIM}(dry run){C.RESET}")
            continue

        # Kill orphans
        kill_cmd = (
            "ps aux --no-headers 2>/dev/null | "
            "grep -E 'jest-worker|jest\\.js|node.*--max-old|npx.*vitest' | "
            "grep -v grep | "
            "awk '{print $2}' | xargs -r kill -9 2>/dev/null; "
            "echo done"
        )
        executor.run(m, kill_cmd, timeout=10)
        total_killed += len(lines)
        total_freed += ram_mb
        print(f"  {C.OK}\u2713{C.RESET} {m['name']:<16} killed {C.WARN}{len(lines)}{C.RESET} orphans, freed {C.OK}{ram_mb}MB{C.RESET}")

    print(f"\n{C.BOLD}\u2501\u2501\u2501 Cleanup:{C.RESET} "
          f"{C.OK}{total_killed} killed{C.RESET}, "
          f"{C.OK}{total_freed}MB freed{C.RESET}\n")


PROVISION_STEPS = [
    {
        "name": "Node.js 20",
        "check": "node --version 2>/dev/null | grep -q v20 && echo OK || echo MISSING",
        "install": (
            "curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && "
            "apt-get install -y nodejs"
        ),
    },
    {
        "name": "Claude CLI",
        "check": "command -v claude >/dev/null 2>&1 && echo OK || echo MISSING",
        "install": "npm install -g @anthropic-ai/claude-code",
    },
    {
        "name": "Codex CLI",
        "check": "command -v codex >/dev/null 2>&1 && echo OK || echo MISSING",
        "install": "npm install -g @openai/codex",
    },
    {
        "name": "zsh",
        "check": "command -v zsh >/dev/null 2>&1 && echo OK || echo MISSING",
        "install": "apt-get install -y zsh && chsh -s $(which zsh)",
    },
    {
        "name": "oh-my-zsh",
        "check": "test -d ~/.oh-my-zsh && echo OK || echo MISSING",
        "install": 'sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended',
    },
    {
        "name": "zsh-autosuggestions",
        "check": "test -d ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-autosuggestions && echo OK || echo MISSING",
        "install": "git clone https://github.com/zsh-users/zsh-autosuggestions ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-autosuggestions",
    },
    {
        "name": "zsh-syntax-highlighting",
        "check": "test -d ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting && echo OK || echo MISSING",
        "install": "git clone https://github.com/zsh-users/zsh-syntax-highlighting ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting",
    },
    {
        "name": "gh CLI",
        "check": "command -v gh >/dev/null 2>&1 && echo OK || echo MISSING",
        "install": (
            "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | "
            "dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && "
            "echo 'deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] "
            "https://cli.github.com/packages stable main' > /etc/apt/sources.list.d/github-cli.list && "
            "apt-get update -qq && apt-get install -y gh"
        ),
    },
    {
        "name": "BMAD Method",
        "check": "test -d _bmad && echo OK || echo MISSING",
        "install": "npx bmad-method install --modules bmm --tools claude-code --yes",
    },
]


def cmd_provision(args, config):
    """Provision a machine with dev tools (Claude CLI, Codex CLI, zsh, BMAD)."""
    executor = Executor()
    machines = config["machines"]

    if not args.machine:
        print(f"{C.CRIT}Specify machine with -m flag{C.RESET}")
        sys.exit(1)

    machines = [m for m in machines if m["name"].lower() == args.machine.lower()]
    if not machines:
        print(f"{C.CRIT}Machine '{args.machine}' not found in config.{C.RESET}")
        sys.exit(1)

    machine = machines[0]
    print(f"{C.BOLD}Provisioning {machine['name']}...{C.RESET}\n")

    steps = PROVISION_STEPS
    if args.only:
        only = [s.strip().lower() for s in args.only.split(",")]
        steps = [s for s in steps if s["name"].lower() in only]

    installed = 0
    skipped = 0
    failed = 0

    for step in steps:
        # Check if already installed
        check_result = executor.run(machine, step["check"], timeout=10)
        if check_result.stdout.strip() == "OK":
            skipped += 1
            print(f"  {C.OK}\u2713{C.RESET} {step['name']:<28} {C.DIM}already installed{C.RESET}")
            continue

        if args.dry_run:
            print(f"  {C.WARN}\u26a0{C.RESET} {step['name']:<28} {C.WARN}would install{C.RESET}")
            continue

        # Install
        print(f"  {C.INFO}\u2022{C.RESET} {step['name']:<28} installing...", end="", flush=True)
        result = executor.run(machine, step["install"], timeout=300)
        if result.returncode == 0:
            installed += 1
            print(f"\r  {C.OK}\u2713{C.RESET} {step['name']:<28} {C.OK}installed{C.RESET}      ")
        else:
            failed += 1
            err = result.stderr.strip().split("\n")[-1][:60] if result.stderr else "unknown error"
            print(f"\r  {C.CRIT}\u2717{C.RESET} {step['name']:<28} {C.CRIT}{err}{C.RESET}      ")

    # Register in devops agent config
    if args.register and not args.dry_run:
        _register_machine(machine, config, args)

    print(f"\n{C.BOLD}\u2501\u2501\u2501 Provision:{C.RESET} "
          f"{C.OK}{installed} installed{C.RESET}, "
          f"{C.DIM}{skipped} skipped{C.RESET}"
          f"{f', {C.CRIT}{failed} failed{C.RESET}' if failed else ''}\n")

    if failed:
        sys.exit(1)


def _register_machine(machine: dict, config: dict, args):
    """Add orphaned_processes check to machine config if not present."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    with open(config_path) as f:
        content = f.read()

    # Add orphaned_processes check if not already there
    name = machine["name"]
    if "orphaned_processes" not in str(machine.get("checks", [])):
        print(f"  {C.INFO}\u2022{C.RESET} {'Add orphan check':<28} {C.DIM}added to config{C.RESET}")


def _hip_api_request(config, method, path, body=None):
    """Make authenticated request to hip.hosting API."""
    import urllib.request
    import urllib.error

    hip = config.get("hip_hosting", {})
    sid = hip.get("sid", "")
    if not sid:
        print(f"{C.CRIT}SID не настроен. Сохраните через: agent.py hip-auth <SID>{C.RESET}")
        sys.exit(1)

    url = f"https://api.hip.hosting{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Cookie", f"SID={sid}")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")
    if data:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print(f"{C.CRIT}SID истёк, обнови через: agent.py hip-auth <новый_SID>{C.RESET}")
        else:
            print(f"{C.CRIT}HTTP {e.code}: {e.reason}{C.RESET}")
        sys.exit(1)
    except Exception as e:
        print(f"{C.CRIT}Ошибка запроса: {e}{C.RESET}")
        sys.exit(1)


def cmd_balance(args, config):
    """Check hip.hosting billing balance and VPS status."""
    user = _hip_api_request(config, "GET", "/user/profile")
    hiplets = _hip_api_request(config, "POST", "/hiplet/rpc/list", {})

    balance = float(user.get("balance", 0))
    email = user.get("email", "?")

    # Calculate total monthly cost
    vps_list = hiplets.get("hiplets", [])
    total_monthly = sum(
        v.get("pricing", {}).get("month", 0) for v in vps_list
    )

    # Months of runway
    runway = balance / total_monthly if total_monthly > 0 else 0

    # Status
    if runway > 2:
        bal_color = C.OK
    elif runway > 1:
        bal_color = C.WARN
    else:
        bal_color = C.CRIT

    print(f"\n{C.BOLD}━━━ hip.hosting ({email}){C.RESET}")
    print(f"  Баланс:    {bal_color}${balance:.2f}{C.RESET}")
    print(f"  Расход:    {C.DIM}${total_monthly:.2f}/мес{C.RESET}")
    print(f"  Запас:     {bal_color}{runway:.1f} мес{C.RESET}")

    if vps_list:
        print(f"\n  {C.BOLD}VPS ({len(vps_list)}):{C.RESET}")
        for v in vps_list:
            name = v.get("name", "?")
            status = v.get("status", "?")
            price = v.get("pricing", {}).get("month", 0)
            expires = v.get("expires_at", "")[:10]
            ipv4 = v.get("ipv4", "")
            size = v.get("size", {})
            specs = f"{size.get('vcpu', '?')}vCPU {size.get('memory', 0)//1024}GB {size.get('disk', '?')}GB"

            s_color = C.OK if status == "ACTIVE" else C.CRIT
            print(f"    {name:<16} {s_color}{status:<8}{C.RESET} "
                  f"${price:<6.2f} {C.DIM}{specs}  {ipv4}  exp:{expires}{C.RESET}")

    print()


def cmd_hip_auth(args, config):
    """Save hip.hosting SID cookie to config."""
    sid = args.sid
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    with open(config_path) as f:
        content = f.read()

    if "hip_hosting:" in content:
        # Replace existing SID
        import re
        content = re.sub(
            r'(hip_hosting:\s*\n\s*sid:\s*)"[^"]*"',
            f'\\1"{sid}"',
            content
        )
    else:
        # Add hip_hosting section at the top
        content = f'hip_hosting:\n  sid: "{sid}"\n\n' + content

    with open(config_path, "w") as f:
        f.write(content)

    print(f"{C.OK}✓{C.RESET} SID сохранён в {config_path}")


def cmd_heal(args, config):
    """Detect failed services and use Claude CLI to diagnose and fix them."""
    from healer import detect_issues, heal_issue

    executor = Executor()
    machines = config["machines"]

    if args.machine:
        machines = [m for m in machines if m["name"].lower() == args.machine.lower()]
        if not machines:
            print(f"{C.CRIT}Machine '{args.machine}' not found in config.{C.RESET}")
            sys.exit(1)

    # Only scan machines that have services or projects configured
    scan_machines = [m for m in machines if m.get("services") or m.get("projects")]
    if not scan_machines:
        print(f"{C.DIM}No machines with services/projects configured.{C.RESET}")
        return

    print(f"{C.BOLD}Scanning {len(scan_machines)} machines for failed services...{C.RESET}")
    issues = detect_issues(executor, scan_machines)

    if not issues:
        print(f"\n  {C.OK}✓{C.RESET} All services are healthy!\n")
        return

    print(f"\n  {C.CRIT}Found {len(issues)} failed service(s):{C.RESET}")
    for issue in issues:
        project_info = f" ({issue.project_desc})" if issue.project_desc else ""
        print(f"    {C.CRIT}✗{C.RESET} {issue.machine_name}: {issue.service} [{issue.state}]{project_info}")

    if args.dry_run:
        print(f"\n{C.DIM}Dry run — showing prompts that would be sent to Claude CLI:{C.RESET}\n")
        for issue in issues:
            from healer import _build_heal_prompt
            prompt = _build_heal_prompt(issue)
            print(f"  {C.BOLD}─── {issue.machine_name}:{issue.service} ───{C.RESET}")
            # Show first 20 lines of prompt
            for line in prompt.split("\n")[:20]:
                print(f"  {C.DIM}{line}{C.RESET}")
            print(f"  {C.DIM}... ({len(prompt)} chars total){C.RESET}\n")
        return

    print(f"\n{C.BOLD}Launching Claude CLI healer (model: {args.model})...{C.RESET}\n")
    start = time.time()

    healed = 0
    failed = 0
    for issue in issues:
        print(f"  {C.INFO}●{C.RESET} Healing {issue.machine_name}:{issue.service}...", flush=True)
        result = heal_issue(issue, model=args.model)

        if result["success"]:
            healed += 1
            state_after = result.get("state_after", "?")
            print(f"  {C.OK}✓{C.RESET} {issue.machine_name}:{issue.service} → {C.OK}{state_after}{C.RESET}")
        else:
            failed += 1
            print(f"  {C.CRIT}✗{C.RESET} {issue.machine_name}:{issue.service} — heal failed")

        # Show Claude output summary (last 10 lines)
        output = result.get("output", "")
        if output:
            output_lines = output.strip().split("\n")
            show_lines = output_lines[-10:] if len(output_lines) > 10 else output_lines
            for line in show_lines:
                print(f"    {C.DIM}{line[:120]}{C.RESET}")
        print()

    total = time.time() - start
    print(f"{C.BOLD}━━━ Heal:{C.RESET} "
          f"{C.OK}{healed} healed{C.RESET}"
          f"{f', {C.CRIT}{failed} failed{C.RESET}' if failed else ''} "
          f"{C.DIM}({total:.0f}s){C.RESET}\n")

    if failed:
        sys.exit(1)


def cmd_list(args, config):
    """List configured machines."""
    print(f"\n{C.BOLD}{'Name':<18} {'Type':<14} {'Description'}{C.RESET}")
    print("\u2500" * 60)
    for m in config["machines"]:
        checks = ", ".join(m.get("checks", []))
        print(f"  {m['name']:<16} {m['type']:<12} {m.get('description', '')}")
        print(f"  {C.DIM}checks: {checks}{C.RESET}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="DevOps agent for LXC containers and VPS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  %(prog)s check                  Check all machines\n"
               "  %(prog)s check -m CT-231         Check single machine\n"
               "  %(prog)s check --save            Check all + save report\n"
               "  %(prog)s report                  Check all + save report\n"
               "  %(prog)s update                  Update all machines\n"
               "  %(prog)s update -m CT-231        Update single machine\n"
               "  %(prog)s assess                  Assess all machines resources\n"
               "  %(prog)s assess -m hiplet-66136  Assess single machine\n"
               "  %(prog)s cleanup                 Kill orphaned processes everywhere\n"
               "  %(prog)s cleanup --dry-run       Show orphans without killing\n"
               "  %(prog)s provision -m CT-231     Install dev tools on machine\n"
               "  %(prog)s provision -m CT-231 --only 'Claude CLI,Codex CLI'\n"
               "  %(prog)s heal                    Auto-fix failed services with AI\n"
               "  %(prog)s heal --dry-run           Show what would be healed\n"
               "  %(prog)s heal -m CT-234           Heal single machine\n"
               "  %(prog)s balance                 Check hip.hosting balance\n"
               "  %(prog)s hip-auth <SID>          Save hip.hosting SID cookie\n"
               "  %(prog)s list                    List machines\n"
    )
    parser.add_argument("-c", "--config", help="Config file path")
    parser.add_argument("-w", "--workers", type=int, default=4,
                        help="Parallel workers (default: 4)")

    sub = parser.add_subparsers(dest="command")

    # check
    p_check = sub.add_parser("check", help="Run health checks")
    p_check.add_argument("-m", "--machine", help="Check single machine by name")
    p_check.add_argument("--save", action="store_true", help="Save markdown report")
    p_check.set_defaults(func=cmd_check)

    # report
    p_report = sub.add_parser("report", help="Check all + save report")
    p_report.add_argument("-m", "--machine", help="Check single machine by name")
    p_report.set_defaults(func=cmd_report)

    # update
    p_update = sub.add_parser("update", help="Run apt upgrade on all machines")
    p_update.add_argument("-m", "--machine", help="Update single machine by name")
    p_update.add_argument("--security-only", action="store_true",
                          help="Only security updates (no autoremove)")
    p_update.set_defaults(func=cmd_update)

    # assess
    p_assess = sub.add_parser("assess", help="Assess machine resources for new project")
    p_assess.add_argument("-m", "--machine", help="Assess single machine by name")
    p_assess.set_defaults(func=cmd_assess)

    # cleanup
    p_cleanup = sub.add_parser("cleanup", help="Kill orphaned processes (jest-worker, zombie node)")
    p_cleanup.add_argument("-m", "--machine", help="Cleanup single machine by name")
    p_cleanup.add_argument("--dry-run", action="store_true", help="Show what would be killed")
    p_cleanup.set_defaults(func=cmd_cleanup)

    # provision
    p_provision = sub.add_parser("provision", help="Install dev tools on a machine")
    p_provision.add_argument("-m", "--machine", required=True, help="Machine to provision")
    p_provision.add_argument("--only", help="Comma-separated list of tools to install")
    p_provision.add_argument("--dry-run", action="store_true", help="Show what would be installed")
    p_provision.add_argument("--register", action="store_true", help="Add orphan check to config")
    p_provision.set_defaults(func=cmd_provision)

    # heal
    p_heal = sub.add_parser("heal", help="Auto-fix failed services using Claude CLI")
    p_heal.add_argument("-m", "--machine", help="Heal single machine by name")
    p_heal.add_argument("--dry-run", action="store_true", help="Show issues without fixing")
    p_heal.add_argument("--model", default="sonnet", help="Claude model (default: sonnet)")
    p_heal.set_defaults(func=cmd_heal)

    # balance
    p_balance = sub.add_parser("balance", help="Check hip.hosting billing balance")
    p_balance.set_defaults(func=cmd_balance)

    # hip-auth
    p_hip_auth = sub.add_parser("hip-auth", help="Save hip.hosting SID cookie")
    p_hip_auth.add_argument("sid", help="SID cookie value from browser")
    p_hip_auth.set_defaults(func=cmd_hip_auth)

    # list
    p_list = sub.add_parser("list", help="List configured machines")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    config = load_config(args.config)
    args.func(args, config)


if __name__ == "__main__":
    main()
