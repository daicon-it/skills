# Systemd & Docker Reference

## Systemd Service Template

Minimal template for a long-running application service:

```ini
[Unit]
Description=My Application Service
After=network.target
Wants=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/myapp
ExecStart=/usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# Environment variables
EnvironmentFile=/root/.config/myapp.env

[Install]
WantedBy=multi-user.target
```

Place in `/etc/systemd/system/myapp.service`.

### Example: skills-api.service (hiplet-66136)

```ini
[Unit]
Description=Skills DB API (uvicorn)
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/skills-db
ExecStart=/root/skills-db/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8410
Restart=always
RestartSec=10
EnvironmentFile=/root/skills-db/.env

[Install]
WantedBy=multi-user.target
```

## Systemd Lifecycle Commands

```bash
# Deploy a new service
cp myapp.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now myapp

# Day-to-day operations
systemctl status myapp
systemctl restart myapp
systemctl stop myapp
systemctl start myapp

# Disable and remove
systemctl disable --now myapp
rm /etc/systemd/system/myapp.service
systemctl daemon-reload

# Follow logs
journalctl -u myapp -f

# Last 100 lines
journalctl -u myapp -n 100

# Since a specific time
journalctl -u myapp --since "2026-01-01 00:00:00"

# List all services and their status
systemctl list-units --type=service --state=running
systemctl --failed
```

## Docker Installation (Ubuntu 22.04 / 24.04)

```bash
# Install Docker engine (system package, no snap)
apt update
apt install -y docker.io docker-compose-v2

# Enable and start
systemctl enable --now docker

# Verify
docker --version
docker compose version
```

For production workloads on this infra, prefer `docker.io` (Ubuntu package) over Docker's official repo — simpler and auto-updated with apt.

## Docker Common Operations

```bash
# Container management
docker ps                          # running containers
docker ps -a                       # all containers (including stopped)
docker stop <name>                 # graceful stop
docker rm <name>                   # remove stopped container
docker restart <name>

# Logs
docker logs <name>                 # all logs
docker logs -f <name>              # follow
docker logs --tail 100 <name>      # last 100 lines
docker logs --since 1h <name>      # last hour

# Execute command in running container
docker exec -it <name> bash
docker exec <name> env | grep DB_

# Images
docker images
docker rmi <image>
docker pull <image>:<tag>

# Inspect
docker inspect <name>
docker stats <name>
```

## Docker Compose

```bash
# Start (detached)
docker compose up -d

# Rebuild and restart
docker compose up -d --build

# Stop and remove containers (keep volumes)
docker compose down

# Stop and remove everything including volumes
docker compose down -v

# Scale a service
docker compose up -d --scale worker=3

# View logs for all services
docker compose logs -f

# View logs for specific service
docker compose logs -f api
```

## Cron vs Systemd Timer

**Use cron when:**
- Simple periodic tasks (backup, cleanup, scrape)
- You want quick setup without creating service files
- The task runs in a well-known environment

**Use systemd timer when:**
- You need dependency ordering (run after service X)
- You want persistent timers (catch up missed runs)
- You need more precise scheduling (OnBootSec, OnCalendar)
- Centralized logging via journald is important

### Cron examples

```bash
# Edit crontab
crontab -e

# Common patterns
0 3 * * *   /root/scripts/pg-backup.sh >> /var/log/pg-backup.log 2>&1
*/15 * * * * /root/enrichment-worker/embed.sh >> /var/log/embed.log 2>&1
0 4 * * 0   /root/scripts/offsite-sync.sh    # Sunday 04:00
```

### Systemd timer example

```ini
# myapp.timer
[Unit]
Description=Run myapp every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl enable --now myapp.timer
systemctl list-timers
```

## Log Rotation

Create `/etc/logrotate.d/myapp`:

```
/var/log/myapp/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 0640 root root
    postrotate
        systemctl reload myapp 2>/dev/null || true
    endscript
}
```

```bash
# Test rotation config
logrotate -d /etc/logrotate.d/myapp

# Force rotation now
logrotate -f /etc/logrotate.d/myapp
```

For systemd services, logs go to journald automatically — no logrotate needed. journald has built-in rotation via `SystemMaxUse` in `/etc/systemd/journald.conf`.
