---
name: devops-agent
description: >-
  Server setup, infrastructure management, and troubleshooting for daicon-it infrastructure.
  USE FOR: set up new server, configure container, harden VPS, troubleshoot service, manage systemd,
  restart container, check logs, setup Docker, configure Tailscale, manage PostgreSQL, backup server,
  bootstrap machine, deploy service, fix firewall, setup monitoring, new machine setup, install Claude on server
  DO NOT USE FOR: application code development, database schema design, frontend work
install: global
---

# DevOps Agent

Infrastructure and operations skill for the daicon-it server fleet. Use this skill when working on server setup, container management, networking, services, or anything system-level.

## 1. Infrastructure Overview

| Machine | Type | Access | Tailscale IP | Services |
|---------|------|--------|-------------|----------|
| CT 101 | LXC Ubuntu 22.04 | direct or pct exec 101 | — (mgmt) | dev hub, git repos |
| CT 231 | LXC Ubuntu 24.04 | pct exec 231 | 100.124.99.73 | PostgreSQL 16 + pgvector |
| CT 232 | LXC Ubuntu 24.04 | pct exec 232 | — | Pulscen parser |
| CT 233 | LXC Ubuntu 24.04 | pct exec 233 | — | Kwork parser |
| CT 234 | LXC Ubuntu 24.04 | pct exec 234 | — | Telethon bot |
| hiplet-66136 | VPS | ssh root@193.168.199.43 | 100.115.152.102 | skills-db API :8410, enrichment-worker |
| PC-001 | Linux desktop | ssh ss@100.105.50.119 | 100.105.50.119 | workstation |
| hiplet-36312 | VPS | ssh root@138.124.125.174 | — | lightweight VPS |
| hiplet-48342 | VPS | ssh root@193.168.199.249 | — | VPS |

**Proxmox host:** 100.93.132.32 (node: `pmx`), API token: `PVEAPIToken=root@pam!claude-api2`

All LXC containers (231–234): Ubuntu 24.04, Node.js 20, Python 3.12, Claude CLI, Codex CLI, gh CLI, zsh, bmad-method repo.

## 2. Access Patterns

```bash
# LXC containers (from CT 101)
ssh root@100.93.132.32 "pct exec <vmid> -- bash -c 'command'"

# VPS / Desktop (direct SSH)
ssh root@<ip> "command"

# Proxmox API — always export vars first
export $(grep -v '^#' /root/.config/proxmox-api.env | xargs)
curl -sk -H "Authorization: PVEAPIToken=$PVE_TOKEN_ID=$PVE_TOKEN_SECRET" \
  "https://100.93.132.32:8006/api2/json/..."
```

Note: `pct exec` starts bash with a minimal PATH (`/sbin:/bin:/usr/sbin:/usr/bin`). `~/.local/bin` is not included — use full path `/root/.local/bin/claude` to reach Claude CLI.

## 3. New Server Bootstrap

Bootstrap installs: zsh, vps-zsh-config, Node.js 20, Python 3.12, Claude CLI (native), Codex CLI, gh CLI, bmad-method, claude-code skills, statusline.

```bash
# One-liner for new machines:
curl -fsSL https://raw.githubusercontent.com/daicon-it/machine-setup/master/machine-bootstrap.sh | bash

# LXC containers (from CT 101):
ssh root@100.93.132.32 "pct exec <vmid> -- bash -c 'curl -fsSL https://raw.githubusercontent.com/daicon-it/machine-setup/master/machine-bootstrap.sh | bash'"
```

**Post-bootstrap checklist:**
- `~/.local/bin/claude --version` — Claude CLI responds
- `codex --version` — Codex CLI responds
- `zsh --version` — zsh installed, default shell
- `~/.claude/skills/devops-agent/SKILL.md` exists
- `~/.claude/skills/skills-db/SKILL.md` exists
- Tailscale connected (if applicable): `tailscale status`

Run `~/.claude/skills/devops-agent/scripts/health-check.sh` to verify all at once.

## 4. Common Operations

### Systemd

| Task | Command |
|------|---------|
| Service status | `systemctl status <name>` |
| Start / stop / restart | `systemctl start/stop/restart <name>` |
| Enable on boot | `systemctl enable <name>` |
| Follow logs | `journalctl -u <name> -f` |
| Logs since time | `journalctl -u <name> --since "1 hour ago"` |
| List failed | `systemctl --failed` |

### Docker

| Task | Command |
|------|---------|
| List containers | `docker ps` / `docker ps -a` |
| Container logs | `docker logs -f <name>` |
| Exec into container | `docker exec -it <name> bash` |
| Compose up | `docker compose up -d` |
| Compose down | `docker compose down` |
| Rebuild | `docker compose up -d --build` |

### Tailscale

| Task | Command |
|------|---------|
| Status | `tailscale status` |
| Ping a node | `tailscale ping <hostname>` |
| Set exit node | `tailscale set --exit-node=100.125.198.74` |
| Connect with exit node | `tailscale up --accept-routes --exit-node=100.125.198.74 --accept-dns=false` |

### Firewall (ufw)

| Task | Command |
|------|---------|
| Status | `ufw status verbose` |
| Allow port | `ufw allow 8080/tcp` |
| Deny port | `ufw deny 22/tcp` |
| Enable | `ufw enable` |

### Disk & Memory

| Task | Command |
|------|---------|
| Disk usage summary | `df -h` |
| Top directories | `du -sh /* 2>/dev/null \| sort -rh \| head -20` |
| Interactive disk | `ncdu /` |
| Memory | `free -h` |
| Process overview | `htop` or `top` |

### Logs

```bash
# Follow a log file
tail -f /var/log/app.log

# Search logs
journalctl -u myservice --since "2026-01-01" | grep ERROR

# Rotate logs manually
logrotate -f /etc/logrotate.d/myapp
```

### Cron

```bash
crontab -l          # list current user's cron
crontab -e          # edit
cat /etc/cron.d/*   # system-wide cron jobs
```

### PostgreSQL

See [references/postgres-ops.md](references/postgres-ops.md) for full details.

```bash
# Quick connection
psql "postgresql://pulscen:pulscen_pass@100.124.99.73:5432/db_pulscen"
```

## 5. Skills-DB Integration

Before starting a DevOps task, search for relevant pre-built skills. The skills-db contains 74K+ Claude Code skills that provide ready-made solutions.

```bash
# Keyword search
curl -s "http://100.115.152.102:8410/keyword_search?q=<topic>&limit=5" \
  | jq '.results[] | {name, description, installs}'

# Semantic search
curl -s "http://100.115.152.102:8410/semantic_search?q=<topic>&limit=5" \
  | jq '.results[] | {name, description, installs}'

# Or use the helper script
bash ~/.claude/skills/devops-agent/scripts/skills-db-query.sh "<topic>"
```

Example queries: `"docker compose setup"`, `"systemd service template"`, `"nginx reverse proxy"`, `"postgresql backup"`.

## 6. Hardening Checklist

Apply to every new VPS or container exposed to the internet:

- **SSH**: disable password auth (`PasswordAuthentication no` in `/etc/ssh/sshd_config`), use keys only
- **Firewall**: `ufw enable`, allow only required ports (22, app port), deny rest
- **Updates**: `apt install unattended-upgrades` + `dpkg-reconfigure unattended-upgrades`
- **Tailscale**: use ACLs to restrict access between nodes; avoid exposing ports publicly if Tailscale access is sufficient
- **Secrets**: env files `chmod 600`, never commit to git, use `/root/.config/` directory
- **Backups**: automated pg_dumpall on CT 231 (03:00 daily), offsite rsync weekly; verify restores periodically

## 7. References

Detailed reference files for specific subsystems:

- [references/proxmox-lxc.md](references/proxmox-lxc.md) — LXC container lifecycle, API, storage
- [references/tailscale-net.md](references/tailscale-net.md) — VPN setup, exit node, troubleshooting
- [references/postgres-ops.md](references/postgres-ops.md) — database admin, backups, pgvector
- [references/systemd-docker.md](references/systemd-docker.md) — service templates, Docker, cron
- [references/troubleshooting.md](references/troubleshooting.md) — layered diagnostics, common issues
