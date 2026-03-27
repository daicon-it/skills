# Troubleshooting Reference

## Layered Diagnostic Approach

Work through layers in order. Don't skip to application logs before confirming the network and process are healthy.

```
1. Network  →  Can the machine be reached? Can it reach dependencies?
2. Process  →  Is the service running? Did it crash? What's in logs?
3. Application  →  Config correct? Permissions? Resource limits?
```

## 1. Network Diagnostics

```bash
# Basic reachability
ping -c 3 8.8.8.8                    # internet
ping -c 3 100.124.99.73              # PostgreSQL (CT 231)
ping -c 3 100.115.152.102            # skills-db (hiplet-66136)

# Check what's listening
ss -tlnp                             # listening TCP ports
ss -tlnp | grep :8080                # specific port

# Test HTTP endpoint
curl -v http://localhost:8080/health
curl -sk https://100.93.132.32:8006/api2/json/version   # Proxmox API

# Check Tailscale
tailscale status
tailscale ping 100.124.99.73

# Check exit node routing
curl -s ifconfig.me                  # should return 166.1.160.18 if exit node active
ip route show table 52               # should have routes if exit node working

# Firewall
ufw status verbose
iptables -L -n | head -30
```

## 2. Process Diagnostics

```bash
# Systemd service
systemctl status myservice           # state + recent logs
journalctl -u myservice -n 100       # last 100 log lines
journalctl -u myservice -f           # follow live
systemctl list-units --failed        # all failed services

# Process list
ps aux | grep myapp
pgrep -a python                      # find Python processes

# Resource usage
top                                  # interactive
htop                                 # better interactive (if installed)
pidstat -p <pid> 1 5                 # per-process CPU/IO

# Memory
free -h
cat /proc/meminfo | grep -E 'MemTotal|MemFree|MemAvailable|SwapTotal|SwapFree'

# Open files / sockets by process
lsof -p <pid> | tail -20
lsof -i :8080                        # what's using port 8080
```

## 3. Application Diagnostics

```bash
# Config validation (example for nginx/postgres)
nginx -t
pg_lsclusters

# Environment variables loaded?
systemctl show myservice --property=Environment
cat /proc/<pid>/environ | tr '\0' '\n'

# Permissions on files/dirs
ls -la /root/myapp/
ls -la /root/.config/myapp.env       # should be 600

# Python app errors
journalctl -u myservice -n 200 | grep -E 'Error|Exception|Traceback'

# Node.js app errors
journalctl -u myservice -n 200 | grep -iE 'error|unhandled|fatal'
```

## Common Issues on This Infrastructure

### LXC Container: No Network / DNS Fails

```bash
# Check DNS inside container
ssh root@100.93.132.32 "pct exec <vmid> -- bash -c 'cat /etc/resolv.conf'"
# Expected: nameserver 8.8.8.8, 1.1.1.1, 100.100.100.100

# Check bridge on host
ssh root@100.93.132.32 "ip link show vmbr0"
ssh root@100.93.132.32 "brctl show vmbr0"

# Restart networking inside container
ssh root@100.93.132.32 "pct exec <vmid> -- bash -c 'systemctl restart networking'"
```

### No AVX on Host CPU (Athlon X4 750K)

Symptom: a binary exits with `Illegal instruction` immediately.

```bash
# Check CPU flags
grep -o 'avx[^ ]*' /proc/cpuinfo | sort -u
# If empty, no AVX support

# Common culprits:
# - Some TensorFlow builds (use CPU-only tensorflow without AVX)
# - Certain PyTorch CUDA builds
# - Some pre-built Node.js native modules
# Solution: install from source or use packages that don't require AVX
```

### Exit Node Connectivity Loss

Symptom: `curl ifconfig.me` returns LAN IP, not `166.1.160.18`.

```bash
ip route show table 52              # empty = problem
systemctl restart tailscaled
sleep 3
tailscale up --accept-routes --exit-node=100.125.198.74 --accept-dns=false
ip route show table 52              # should now have routes
curl ifconfig.me                    # should return 166.1.160.18
```

### PostgreSQL: Connection Refused

```bash
# 1. Is PostgreSQL running?
ssh root@100.93.132.32 "pct exec 231 -- bash -c 'systemctl status postgresql'"

# 2. Does it listen on all interfaces?
ssh root@100.93.132.32 "pct exec 231 -- bash -c \"psql -U postgres -c 'SHOW listen_addresses'\""
# Expected: *

# 3. Is pg_hba allowing the source IP?
ssh root@100.93.132.32 "pct exec 231 -- bash -c 'grep -v ^# /etc/postgresql/16/main/pg_hba.conf | grep -v ^$'"
# Must include: host all all 100.64.0.0/10 md5

# 4. Can you reach the port?
nc -zv 100.124.99.73 5432
```

### Disk Full on Small Container

CT 101 has only 7.8GB, parser containers (232-234) have 10GB on HDD.

```bash
df -h                                # overview
du -sh /* 2>/dev/null | sort -rh | head -15    # top directories

# Common culprits:
apt autoremove && apt clean          # package cache
journalctl --vacuum-size=100M        # trim journald logs
docker system prune -f               # unused Docker layers
find /tmp /var/tmp -mtime +7 -delete # old temp files
```

### Claude CLI "High Memory Usage" Warning

Message: `High memory usage detected` or similar.

This is the built-in V8 heap check in Claude Code:
- **1.5GB heap** → "high" warning
- **2.5GB heap** → "critical" warning

This is not configurable. It's a guard against runaway context accumulation. If you see it consistently, use `/compact` to compress the context window. Do not attempt to patch the threshold.

### Service Fails to Start After Deploy

```bash
# Check exact error
journalctl -u myservice -n 50 --no-pager

# Common reasons:
# - Port already in use: ss -tlnp | grep :PORT
# - Missing env file: ls -la /root/.config/myapp.env
# - Wrong working directory: systemctl show myservice | grep WorkingDir
# - Python venv not activated: check ExecStart uses full venv path
# - Permission denied on file: check User= in [Service] section
```

### GitHub / gh CLI Auth Issues

```bash
gh auth status                       # check token validity
gh auth login --with-token           # re-authenticate
cat ~/.config/gh/hosts.yml           # see stored credentials
```

## Quick Health Check

Run the built-in health check script to verify the machine is correctly set up:

```bash
bash ~/.claude/skills/devops-agent/scripts/health-check.sh
```

This checks: Claude CLI, Codex CLI, Node.js, zsh, settings.json, statusline, and skills installation.
