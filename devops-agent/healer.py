"""AI healer — diagnoses and fixes service failures using Claude Code CLI."""

import json
import subprocess
import time
from dataclasses import dataclass
from executor import Executor, CmdResult


@dataclass
class ServiceIssue:
    machine_name: str
    machine: dict
    service: str
    state: str
    logs: str
    project_path: str | None
    project_desc: str | None


def detect_issues(executor: Executor, machines: list[dict]) -> list[ServiceIssue]:
    """Scan all machines for failed services and collect error context."""
    issues = []

    for machine in machines:
        services = machine.get("services", [])
        projects = {p["service"]: p for p in machine.get("projects", []) if "service" in p}

        for svc in services:
            result = executor.run(machine, f"systemctl is-active {svc} 2>/dev/null", timeout=10)
            state = result.stdout.strip()

            if state == "active":
                continue

            # Collect journal logs for failed service
            log_result = executor.run(
                machine,
                f"journalctl -u {svc} --no-pager -n 80 --since '24 hours ago' 2>/dev/null",
                timeout=15,
            )
            logs = log_result.stdout.strip()

            project = projects.get(svc)
            issues.append(ServiceIssue(
                machine_name=machine["name"],
                machine=machine,
                service=svc,
                state=state,
                logs=logs,
                project_path=project["path"] if project else None,
                project_desc=project["description"] if project else None,
            ))

    return issues


def _build_heal_prompt(issue: ServiceIssue) -> str:
    """Build a prompt for Claude CLI to diagnose and fix the issue."""
    parts = [
        f"Сервис `{issue.service}` на машине `{issue.machine_name}` в статусе `{issue.state}`.",
        "",
        "## Логи сервиса (journalctl, последние записи):",
        "```",
        issue.logs[-3000:] if issue.logs else "(логи пусты)",
        "```",
        "",
    ]

    if issue.project_path:
        parts.extend([
            f"## Проект: {issue.project_desc or issue.project_path}",
            f"Путь: `{issue.project_path}`",
            "",
        ])

    parts.extend([
        "## Задача:",
        "1. Проанализируй логи и определи корневую причину падения",
        "2. Изучи код проекта если нужно",
        "3. Исправь баг — отредактируй файлы",
        "4. Перезапусти сервис: `systemctl restart " + issue.service + "`",
        "5. Проверь что сервис работает: `systemctl is-active " + issue.service + "`",
        "6. Выведи краткий отчёт: что было сломано, что исправлено",
        "",
        "ВАЖНО: Не добавляй фичи. Исправь только причину падения. Минимальный патч.",
    ])

    return "\n".join(parts)


def _get_ssh_prefix(machine: dict) -> str | None:
    """Get SSH command prefix for running claude on the target machine."""
    mtype = machine["type"]
    if mtype == "local":
        return None  # Run locally
    elif mtype == "vps":
        return machine["ssh"]
    elif mtype == "lxc":
        host = machine["ssh_host"]
        vmid = machine["vmid"]
        return f"ssh root@{host} \"pct exec {vmid} -- bash -c"
    return None


def heal_issue(issue: ServiceIssue, dry_run: bool = False, model: str = "sonnet") -> dict:
    """Run Claude CLI to heal a single issue. Returns result dict."""
    prompt = _build_heal_prompt(issue)
    result = {
        "machine": issue.machine_name,
        "service": issue.service,
        "state_before": issue.state,
        "prompt": prompt,
        "output": "",
        "success": False,
        "dry_run": dry_run,
    }

    if dry_run:
        result["output"] = "(dry run — не запускаем Claude CLI)"
        return result

    machine = issue.machine
    mtype = machine["type"]

    # Determine where to run claude
    if mtype == "local":
        cwd = issue.project_path or "/root"
        cmd = [
            "claude", "-p", prompt,
            "--model", model,
            "--output-format", "text",
            "--max-turns", "15",
        ]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=300, cwd=cwd,
            )
            result["output"] = r.stdout.strip()
            result["success"] = r.returncode == 0
        except subprocess.TimeoutExpired:
            result["output"] = "Claude CLI timeout (5 min)"
        except Exception as e:
            result["output"] = f"Error: {e}"

    elif mtype == "vps":
        ssh = machine["ssh"]
        cwd = issue.project_path or "/root"
        # Escape prompt for shell
        escaped_prompt = prompt.replace("'", "'\\''")
        cmd = (
            f"{ssh} \"cd {cwd} && claude -p '{escaped_prompt}' "
            f"--model {model} --output-format text --max-turns 15\""
        )
        try:
            r = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=300,
            )
            result["output"] = r.stdout.strip()
            result["success"] = r.returncode == 0
        except subprocess.TimeoutExpired:
            result["output"] = "Claude CLI timeout (5 min)"
        except Exception as e:
            result["output"] = f"Error: {e}"

    elif mtype == "lxc":
        host = machine["ssh_host"]
        vmid = machine["vmid"]
        cwd = issue.project_path or "/root"
        # For LXC: write prompt to temp file, then run claude with it
        escaped_prompt = prompt.replace("'", "'\\''").replace('"', '\\"')
        # Use a temp file approach to avoid shell escaping hell
        write_prompt_cmd = (
            f"ssh root@{host} \"pct exec {vmid} -- bash -c "
            f"'cat > /tmp/heal_prompt.txt << HEALPROMPTEOF\n{prompt}\nHEALPROMPTEOF'\""
        )
        run_claude_cmd = (
            f"ssh root@{host} \"pct exec {vmid} -- bash -c "
            f"'cd {cwd} && claude -p \\\"$(cat /tmp/heal_prompt.txt)\\\" "
            f"--model {model} --output-format text --max-turns 15'\""
        )
        try:
            # Write prompt
            subprocess.run(write_prompt_cmd, shell=True, timeout=10,
                          capture_output=True, text=True)
            # Run claude
            r = subprocess.run(
                run_claude_cmd, shell=True,
                capture_output=True, text=True, timeout=300,
            )
            result["output"] = r.stdout.strip()
            result["success"] = r.returncode == 0
        except subprocess.TimeoutExpired:
            result["output"] = "Claude CLI timeout (5 min)"
        except Exception as e:
            result["output"] = f"Error: {e}"

    # Verify service status after heal
    if result["success"] and not dry_run:
        executor = Executor()
        time.sleep(3)
        check = executor.run(machine, f"systemctl is-active {issue.service} 2>/dev/null", timeout=10)
        result["state_after"] = check.stdout.strip()
        result["success"] = result["state_after"] == "active"

    return result
