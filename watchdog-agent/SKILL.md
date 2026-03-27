---
name: watchdog-agent
description: >-
  Monitor and auto-heal LXC containers and VPS servers on Proxmox infrastructure.
  USE FOR: monitor containers, auto-heal OOM, kill zombie processes, restart failed services,
  detect high CPU/RAM, fix stuck processes, cron conflict detection, container health monitoring,
  machine unreachable detection, SSH/Tailscale connectivity checks
  DO NOT USE FOR: initial server setup (use devops-agent), application debugging, database admin
---

# Watchdog Agent

Automated monitoring and auto-healing for LXC containers and VPS.

## What it detects and fixes

| Issue | Detection | Action |
|-------|-----------|--------|
| OOM / high RAM+Swap | Memory usage > threshold | Kill heaviest process |
| Duplicate processes | Multiple claude/node/python instances | Kill excess copies |
| Stuck processes | claude --print, analyzer running too long | Kill by age |
| Zombie / D-state | ps aux shows Z or D state | Kill parent or reap |
| Failed systemd services | systemctl is-failed | Restart service |
| High CPU load | load average > cores * threshold | Renice heavy processes |
| Cron conflicts | Multiple heavy cron jobs running | Defer or kill newer |
| Machine unreachable | SSH timeout, ping fail, Tailscale down | Diagnose → fix (restart tailscale/sshd/pct start) → email lccdaicon@gmail.com if physical action needed |
| Skills drift | Missing/outdated skills vs daicon-it/skills repo | Report diff, suggest `push-to-all.sh --skills-only` |

## Usage

```bash
# Dry run (show what would happen)
python3 watchdog.py --dry-run

# Check specific machine
python3 watchdog.py --machine CT-234

# Verbose output
python3 watchdog.py --verbose

# Full run (auto-heal)
python3 watchdog.py
```

## Configuration

Edit `config.yaml` to set thresholds per machine, monitored services, and process rules.

## Deployment

Runs via cron every 5 minutes:
```bash
*/5 * * * * /usr/bin/python3 /root/watchdog-agent/watchdog.py >> /root/watchdog-agent/logs/watchdog.log 2>&1
```

Logs in `logs/watchdog.log`, state persisted in `state/`.
