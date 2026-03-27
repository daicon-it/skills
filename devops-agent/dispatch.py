#!/usr/bin/env python3
"""DevOps Dispatch — обработка задач из GitHub Issues.

Мониторит issues в daicon-it/devops-agent, выполняет:
- Создание LXC контейнеров через Proxmox API
- Provision (установка инструментов)
- Отчёт в dc-corp issue

Запуск: python3 dispatch.py [--dry-run] [--issue N]
Cron: каждые 5 минут
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("devops-dispatch")

GITHUB_REPO = "daicon-it/devops-agent"
DC_CORP_REPO = "daicon-it/dc-corp"
DISPATCH_MARKER = "<!-- devops-dispatch -->"
PVE_ENV = "/root/.config/proxmox-api.env"
PVE_HOST = "https://100.93.132.32:8006"
PVE_NODE = "pmx"
SSH_HOST = "root@100.93.132.32"

# Default LXC config for new project containers
DEFAULT_LXC = {
    "ostemplate": "local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst",
    "cores": 1,
    "memory": 1024,
    "swap": 2048,
    "rootfs": "HDD:10",
    "nameserver": "8.8.8.8 1.1.1.1 100.100.100.100",
    "searchdomain": "tail262256.ts.net",
    "start": 1,
    "onboot": 1,
    "unprivileged": 0,
    "features": "nesting=1",
    "net0": "name=eth0,bridge=vmbr0,ip=dhcp",
}


def run_gh(args: list[str], timeout: int = 30) -> str | None:
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            log.error("gh %s failed: %s", " ".join(args[:3]), result.stderr[:300])
            return None
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        log.error("gh %s timed out", " ".join(args[:3]))
        return None


def run_cmd(cmd: str, timeout: int = 30) -> tuple[str, int]:
    """Run shell command, return (output, returncode)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (r.stdout.strip() or r.stderr.strip()), r.returncode
    except subprocess.TimeoutExpired:
        return "timeout", 1


def load_pve_creds() -> dict:
    """Load Proxmox API credentials."""
    creds = {}
    try:
        with open(PVE_ENV) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    creds[k.strip()] = v.strip()
    except FileNotFoundError:
        log.error("PVE env not found: %s", PVE_ENV)
    return creds


def pve_api(method: str, endpoint: str, data: dict = None) -> dict | None:
    """Call Proxmox API."""
    creds = load_pve_creds()
    token_id = creds.get("PVE_TOKEN_ID", "")
    token_secret = creds.get("PVE_TOKEN_SECRET", "")
    if not token_id or not token_secret:
        log.error("PVE credentials incomplete")
        return None

    url = f"{PVE_HOST}{endpoint}"
    auth = f"PVEAPIToken={token_id}={token_secret}"

    cmd = f'curl -sk -H "Authorization: {auth}"'
    if method == "POST" and data:
        for k, v in data.items():
            cmd += f' --data-urlencode "{k}={v}"'
        cmd += f' -X POST "{url}"'
    else:
        cmd += f' "{url}"'

    output, rc = run_cmd(cmd, timeout=30)
    if rc != 0:
        log.error("PVE API failed: %s", output[:200])
        return None

    try:
        return json.loads(output)
    except json.JSONDecodeError:
        log.error("PVE API invalid JSON: %s", output[:200])
        return None


def get_pending_issues() -> list[dict]:
    """Get open issues without dispatch marker."""
    out = run_gh([
        "issue", "list", "-R", GITHUB_REPO,
        "--state", "open",
        "--json", "number,title,body,labels",
        "--limit", "50"
    ])
    if not out:
        return []

    issues = json.loads(out)
    pending = []
    for issue in issues:
        # Check if already processed
        comments = run_gh([
            "api", f"repos/{GITHUB_REPO}/issues/{issue['number']}/comments",
            "--jq", ".[].body"
        ], timeout=15)
        if comments and DISPATCH_MARKER in comments:
            continue
        pending.append(issue)

    return pending


def parse_issue(issue: dict) -> dict:
    """Parse issue to extract task parameters."""
    title = issue.get("title", "")
    body = issue.get("body", "") or ""

    # Extract dc-corp issue number
    dc_match = re.search(r'dc-corp\s*#(\d+)', title + body)
    dc_issue = int(dc_match.group(1)) if dc_match else None

    # Extract slug
    slug_match = re.search(r'инфраструктуру:\s*(\S+)', title)
    slug = slug_match.group(1) if slug_match else None

    # Extract repo name
    repo_match = re.search(r'daicon-it/(\S+)', body)
    repo_name = repo_match.group(1) if repo_match else slug

    return {
        "dc_issue": dc_issue,
        "slug": slug,
        "repo_name": repo_name,
        "body": body,
    }


def find_next_vmid() -> int | None:
    """Find next available VMID on Proxmox."""
    resp = pve_api("GET", f"/api2/json/cluster/resources?type=vm")
    if not resp or "data" not in resp:
        return None

    used = {r["vmid"] for r in resp["data"]}
    # Start from 235 (next after existing containers)
    for vmid in range(235, 300):
        if vmid not in used:
            return vmid
    return None


def create_lxc(vmid: int, hostname: str, config: dict = None) -> bool:
    """Create LXC container via Proxmox API."""
    cfg = {**DEFAULT_LXC, **(config or {})}
    cfg["vmid"] = str(vmid)
    cfg["hostname"] = hostname

    log.info("Creating LXC CT-%d (%s)...", vmid, hostname)

    resp = pve_api("POST", f"/api2/json/nodes/{PVE_NODE}/lxc", cfg)
    if not resp:
        return False

    # Wait for creation
    upid = resp.get("data", "")
    if upid:
        log.info("Task UPID: %s", upid)
        # Poll task status
        for _ in range(60):
            time.sleep(2)
            status = pve_api("GET", f"/api2/json/nodes/{PVE_NODE}/tasks/{upid}/status")
            if status and status.get("data", {}).get("status") == "stopped":
                exitstatus = status["data"].get("exitstatus", "")
                if exitstatus == "OK":
                    log.info("CT-%d created successfully", vmid)
                    return True
                else:
                    log.error("CT-%d creation failed: %s", vmid, exitstatus)
                    return False

    return False


def start_lxc(vmid: int) -> bool:
    """Start LXC container."""
    resp = pve_api("POST", f"/api2/json/nodes/{PVE_NODE}/lxc/{vmid}/status/start")
    if resp:
        log.info("CT-%d started", vmid)
        time.sleep(5)  # Wait for boot
        return True
    return False


def setup_tailscale(vmid: int) -> str | None:
    """Install and configure Tailscale on container."""
    commands = [
        "curl -fsSL https://tailscale.com/install.sh | sh",
        "tailscale up --authkey=$(cat /dev/stdin) --accept-routes --accept-dns=false",
    ]
    # Install tailscale
    cmd = f'ssh {SSH_HOST} "pct exec {vmid} -- bash -c \'curl -fsSL https://tailscale.com/install.sh | sh\'"'
    output, rc = run_cmd(cmd, timeout=120)
    if rc != 0:
        log.error("Tailscale install failed on CT-%d: %s", vmid, output[:200])
        return None

    # Start tailscale (will need auth key or manual login)
    cmd = f'ssh {SSH_HOST} "pct exec {vmid} -- bash -c \'tailscale up --accept-routes --accept-dns=false 2>&1 | head -3\'"'
    output, rc = run_cmd(cmd, timeout=30)
    log.info("CT-%d tailscale: %s", vmid, output[:200])

    # Get IP
    cmd = f'ssh {SSH_HOST} "pct exec {vmid} -- bash -c \'tailscale ip -4 2>/dev/null\'"'
    output, rc = run_cmd(cmd, timeout=10)
    if rc == 0 and output.strip():
        return output.strip()
    return None


def provision_container(vmid: int) -> dict:
    """Run provision steps on container via agent.py."""
    # Add container to config temporarily for provisioning
    # Use executor directly via pct exec
    results = {"installed": [], "failed": [], "skipped": []}

    steps = [
        ("apt update", "apt-get update -qq"),
        ("Python 3.12", "python3 --version 2>/dev/null | grep -q '3.1[2-9]' && echo SKIP || apt-get install -y python3 python3-pip"),
        ("Node.js 20", "node --version 2>/dev/null | grep -q v20 && echo SKIP || (curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs)"),
        ("git", "apt-get install -y git"),
        ("zsh", "apt-get install -y zsh && chsh -s $(which zsh)"),
        ("vps-zsh-config", "bash <(curl -fsSL https://raw.githubusercontent.com/daicon-it/vps-zsh-config/main/install.sh)"),
        ("Claude CLI", "npm install -g @anthropic-ai/claude-code 2>&1 | tail -3"),
        ("Codex CLI", "npm install -g @openai/codex 2>&1 | tail -3"),
        ("gh CLI", "command -v gh >/dev/null 2>&1 && echo SKIP || (curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main' > /etc/apt/sources.list.d/github-cli.list && apt-get update -qq && apt-get install -y gh)"),
    ]

    for name, cmd in steps:
        escaped = cmd.replace("'", "'\\''")
        full_cmd = f"ssh {SSH_HOST} \"pct exec {vmid} -- bash -c '{escaped}'\""
        output, rc = run_cmd(full_cmd, timeout=300)

        if "SKIP" in output:
            results["skipped"].append(name)
            log.info("CT-%d: %s — skipped", vmid, name)
        elif rc == 0:
            results["installed"].append(name)
            log.info("CT-%d: %s — installed", vmid, name)
        else:
            results["failed"].append(name)
            log.error("CT-%d: %s — failed: %s", vmid, name, output[:100])

    return results


def post_report(issue_number: int, vmid: int, hostname: str, provision_results: dict,
                dc_issue: int = None, tailscale_ip: str = None):
    """Post completion report to devops-agent issue and dc-corp issue."""
    installed = ", ".join(provision_results.get("installed", []))
    failed = ", ".join(provision_results.get("failed", []))
    skipped = ", ".join(provision_results.get("skipped", []))

    report = f"""{DISPATCH_MARKER}
## DevOps: инфраструктура создана

**Контейнер:** CT-{vmid} (`{hostname}`)
**Tailscale IP:** {tailscale_ip or 'не настроен — требуется auth key'}

### Provision
- Установлено: {installed or 'нет'}
- Пропущено: {skipped or 'нет'}
- Ошибки: {failed or 'нет'}

### Следующие шаги
1. Настроить Tailscale auth key (если не подключён)
2. Настроить Claude CLI statusline
3. Настроить gh auth
4. Сообщить CTO-агенту → лейбл `создание-репо`

---
_DevOps Dispatch автоматический отчёт_"""

    # Post to devops-agent issue
    run_gh(["issue", "comment", str(issue_number), "-R", GITHUB_REPO, "--body", report])

    # Close devops-agent issue
    run_gh(["issue", "close", str(issue_number), "-R", GITHUB_REPO])

    # Post to dc-corp issue and update label
    if dc_issue:
        dc_report = f"""{DISPATCH_MARKER}
## DevOps: инфраструктура готова

**Контейнер:** CT-{vmid} (`{hostname}`)
**Tailscale IP:** {tailscale_ip or 'требуется настройка'}
**Установлено:** {installed}
{f'**Ошибки:** {failed}' if failed else ''}

Готово к созданию репозитория."""

        run_gh(["issue", "comment", str(dc_issue), "-R", DC_CORP_REPO, "--body", dc_report])

        # Update dc-corp label: создание-инфра → создание-репо
        run_gh([
            "issue", "edit", str(dc_issue), "-R", DC_CORP_REPO,
            "--remove-label", "создание-инфра",
            "--add-label", "создание-репо"
        ])
        log.info("dc-corp#%d: label → создание-репо", dc_issue)


def find_existing_container(slug: str) -> tuple[int, str] | None:
    """Check if a container with matching hostname already exists.

    Matches by slug prefix (e.g. 'neuronet-one' matches 'neuronet-235')
    or by exact substring.
    """
    resp = pve_api("GET", "/api2/json/cluster/resources?type=vm")
    if not resp or "data" not in resp:
        return None
    # Extract base name: 'neuronet-one' → 'neuronet', 'my-app-name' → 'my-app-name'
    slug_base = slug.rsplit("-", 1)[0] if slug.count("-") > 0 else slug
    for r in resp["data"]:
        name = r.get("name", "")
        if slug in name or slug_base in name:
            return r["vmid"], name
    return None


def process_issue(issue: dict, dry_run: bool = False) -> bool:
    """Process a single infrastructure issue."""
    task = parse_issue(issue)
    issue_num = issue["number"]
    slug = task["slug"]
    dc_issue = task["dc_issue"]

    log.info("#%d: обработка — %s (dc-corp#%s)", issue_num, slug, dc_issue)

    if dry_run:
        existing = find_existing_container(slug)
        if existing:
            log.info("#%d: [DRY-RUN] контейнер уже существует: CT-%d (%s) — provision only", issue_num, existing[0], existing[1])
        else:
            log.info("#%d: [DRY-RUN] создал бы контейнер для %s", issue_num, slug)
        return True

    # Check if container already exists
    existing = find_existing_container(slug)
    if existing:
        vmid, hostname = existing
        log.info("#%d: контейнер CT-%d (%s) уже существует — пропускаю создание", issue_num, vmid, hostname)
    else:
        # 1. Find next VMID
        vmid = find_next_vmid()
        if not vmid:
            log.error("No available VMID")
            return False
        log.info("Next VMID: %d", vmid)

        # 2. Create LXC container
        hostname = f"{slug}-{vmid}"
        success = create_lxc(vmid, hostname)
        if not success:
            log.error("Failed to create CT-%d", vmid)
            run_gh(["issue", "comment", str(issue_num), "-R", GITHUB_REPO,
                    "--body", f"{DISPATCH_MARKER}\n## Ошибка создания CT-{vmid}\n\nНе удалось создать контейнер. Проверьте логи."])
            return False

        # 3. Start container
        start_lxc(vmid)

    # 4. Setup Tailscale (check if already connected)
    ts_check = f'ssh {SSH_HOST} "pct exec {vmid} -- bash -c \'tailscale ip -4 2>/dev/null\'"'
    ts_output, ts_rc = run_cmd(ts_check, timeout=10)
    if ts_rc == 0 and ts_output.strip() and ts_output.strip().startswith("100."):
        ts_ip = ts_output.strip()
        log.info("CT-%d: Tailscale already connected: %s", vmid, ts_ip)
    else:
        ts_ip = setup_tailscale(vmid)

    # 5. Provision
    provision_results = provision_container(vmid)

    # 6. Report
    post_report(issue_num, vmid, hostname, provision_results, dc_issue, ts_ip)

    log.info("#%d: CT-%d создан и настроен", issue_num, vmid)
    return True


def main():
    parser = argparse.ArgumentParser(description="DevOps Dispatch — process infrastructure tasks from GitHub")
    parser.add_argument("--dry-run", action="store_true", help="Don't create anything, just show what would be done")
    parser.add_argument("--issue", type=int, help="Process specific issue number")
    args = parser.parse_args()

    log.info("=== DevOps Dispatch started ===")

    if args.issue:
        out = run_gh(["issue", "view", str(args.issue), "-R", GITHUB_REPO,
                       "--json", "number,title,body,labels"])
        if not out:
            log.error("Issue #%d not found", args.issue)
            return 1
        issues = [json.loads(out)]
    else:
        issues = get_pending_issues()

    if not issues:
        log.info("No pending issues")
        return 0

    log.info("Found %d pending issues", len(issues))

    processed = 0
    for issue in issues:
        if process_issue(issue, args.dry_run):
            processed += 1

    log.info("=== DevOps Dispatch finished: %d/%d processed ===", processed, len(issues))
    return 0


if __name__ == "__main__":
    sys.exit(main())
