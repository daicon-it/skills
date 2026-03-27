"""Report formatting: terminal (color) and markdown."""

import os
from datetime import datetime
from dataclasses import dataclass


class C:
    """ANSI color codes."""
    OK = "\033[92m"
    WARN = "\033[93m"
    CRIT = "\033[91m"
    INFO = "\033[94m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


STATUS_ICONS = {
    "ok": f"{C.OK}\u2713{C.RESET}",
    "warn": f"{C.WARN}\u26a0{C.RESET}",
    "crit": f"{C.CRIT}\u2717{C.RESET}",
    "error": f"{C.CRIT}?{C.RESET}",
    "info": f"{C.DIM}\u2022{C.RESET}",
}

STATUS_COLORS = {
    "ok": C.OK,
    "warn": C.WARN,
    "crit": C.CRIT,
    "error": C.CRIT,
    "info": C.DIM,
}

MD_STATUS = {
    "ok": "\u2705",
    "warn": "\u26a0\ufe0f",
    "crit": "\u274c",
    "error": "\u2753",
    "info": "\u2139\ufe0f",
}


@dataclass
class MachineReport:
    name: str
    description: str
    checks: list  # list of CheckResult
    elapsed: float = 0.0


def worst_status(checks: list) -> str:
    """Return worst status from a list of CheckResult."""
    priority = {"crit": 4, "error": 3, "warn": 2, "info": 1, "ok": 0}
    worst = "ok"
    for c in checks:
        if priority.get(c.status, 0) > priority.get(worst, 0):
            worst = c.status
    return worst


def print_terminal(reports: list[MachineReport], total_elapsed: float):
    """Print colored terminal report."""
    counts = {"ok": 0, "warn": 0, "crit": 0, "error": 0}

    for report in reports:
        ws = worst_status(report.checks)
        color = STATUS_COLORS.get(ws, "")
        icon = STATUS_ICONS.get(ws, "")

        print(f"\n{C.BOLD}\u2501\u2501\u2501 {report.name}{C.RESET}"
              f" {C.DIM}({report.description}){C.RESET} {icon}"
              f"  {C.DIM}[{report.elapsed:.1f}s]{C.RESET}")

        for check in report.checks:
            ci = STATUS_ICONS.get(check.status, "\u2022")
            cc = STATUS_COLORS.get(check.status, "")
            detail = f" {C.DIM}{check.details}{C.RESET}" if check.details else ""
            print(f"  {ci} {check.name:<14} {cc}{check.value}{C.RESET}{detail}")

        # Count for summary
        if ws in counts:
            counts[ws] += 1
        else:
            counts["ok"] += 1

    # Summary
    total = len(reports)
    parts = []
    if counts["ok"]:
        parts.append(f"{C.OK}{counts['ok']} OK{C.RESET}")
    if counts["warn"]:
        parts.append(f"{C.WARN}{counts['warn']} WARN{C.RESET}")
    if counts["crit"]:
        parts.append(f"{C.CRIT}{counts['crit']} CRIT{C.RESET}")
    if counts["error"]:
        parts.append(f"{C.CRIT}{counts['error']} ERR{C.RESET}")

    print(f"\n{C.BOLD}\u2501\u2501\u2501 Summary:{C.RESET} "
          f"{', '.join(parts)} "
          f"{C.DIM}({total} machines, {total_elapsed:.1f}s){C.RESET}\n")


def save_markdown(reports: list[MachineReport], total_elapsed: float, output_dir: str) -> str:
    """Save markdown report and return file path."""
    os.makedirs(output_dir, exist_ok=True)
    now = datetime.now()
    filename = now.strftime("%Y-%m-%d_%H-%M") + ".md"
    filepath = os.path.join(output_dir, filename)

    lines = []
    lines.append(f"# DevOps Report {now.strftime('%Y-%m-%d %H:%M')}\n")

    # Summary table
    lines.append("| Machine | Status | Description |")
    lines.append("|---------|--------|-------------|")
    for r in reports:
        ws = worst_status(r.checks)
        icon = MD_STATUS.get(ws, "")
        lines.append(f"| {r.name} | {icon} {ws.upper()} | {r.description} |")
    lines.append("")

    # Details per machine
    for r in reports:
        lines.append(f"## {r.name}\n")
        lines.append(f"_{r.description}_ ({r.elapsed:.1f}s)\n")
        lines.append("| Check | Status | Value | Details |")
        lines.append("|-------|--------|-------|---------|")
        for c in r.checks:
            icon = MD_STATUS.get(c.status, "")
            lines.append(f"| {c.name} | {icon} {c.status} | {c.value} | {c.details} |")
        lines.append("")

    lines.append(f"---\n*{len(reports)} machines checked in {total_elapsed:.1f}s*\n")

    with open(filepath, "w") as f:
        f.write("\n".join(lines))

    return filepath
