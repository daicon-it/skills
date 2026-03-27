# Tailscale Networking Reference

VPN mesh for the daicon-it infrastructure. All containers and VPS machines route traffic through the Tailscale exit node.

## Tailnet Details

- **Tailnet:** `tail262256.ts.net`
- **User:** `lccdaicon@`
- **Exit node:** `usa-100mbs` — hiplet-32436 at `100.125.198.74`
- **External IP through exit node:** `166.1.160.18`
- **Tailscale DNS:** `100.100.100.100` (MagicDNS)

## Known IPs

| Machine | Tailscale IP | Notes |
|---------|-------------|-------|
| Proxmox host (pmx) | 100.93.132.32 | Proxmox API, pct exec |
| CT 231 (postgresql) | 100.124.99.73 | PostgreSQL 5432 |
| hiplet-66136 | 100.115.152.102 | skills-db API :8410 |
| hiplet-32436 (exit node) | 100.125.198.74 | usa-100mbs |
| PC-001 | 100.105.50.119 | Linux desktop |
| PC-Kirill | 100.112.47.43 | Windows 11 |

## Install Tailscale

```bash
# Universal installer
curl -fsSL https://tailscale.com/install.sh | sh

# Start and enable
systemctl enable --now tailscaled
```

## Connect to Tailnet

```bash
# Interactive (opens browser URL)
tailscale up

# With auth key (non-interactive, for automation)
tailscale up --auth-key=<tskey-auth-...>

# With exit node (recommended for all containers)
tailscale up --accept-routes --exit-node=100.125.198.74 --accept-dns=false
```

Get auth keys from: https://login.tailscale.com/admin/settings/keys

## Set Exit Node

```bash
# Set exit node (when already connected)
tailscale set --exit-node=100.125.198.74

# Or reconnect with exit node
tailscale up --accept-routes --exit-node=100.125.198.74 --accept-dns=false
```

## Common Commands

```bash
tailscale status              # show all peers and connection state
tailscale ping <hostname>     # test reachability
tailscale ip                  # show this machine's Tailscale IP
tailscale netcheck            # check NAT type and relay latency
tailscale bugreport           # generate debug report
```

## Exit Node Auto-Start Service

Containers CT 232 and CT 234 use a systemd service to reconnect the exit node after boot (in case tailscaled starts before the network is ready).

**Script:** `/usr/local/bin/tailscale-exit-node.sh`

```bash
#!/bin/bash
MAX_RETRIES=3
for i in $(seq 1 $MAX_RETRIES); do
    if tailscale up --accept-routes --exit-node=100.125.198.74 --accept-dns=false 2>/dev/null; then
        exit 0
    fi
    systemctl restart tailscaled
    sleep 5
done
exit 1
```

**Systemd unit:** `/etc/systemd/system/tailscale-exit-node.service`

```ini
[Unit]
Description=Ensure Tailscale exit node is connected
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/tailscale-exit-node.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

```bash
chmod +x /usr/local/bin/tailscale-exit-node.sh
systemctl daemon-reload
systemctl enable --now tailscale-exit-node.service
```

## DNS Configuration

All containers use:
```
nameserver 8.8.8.8
nameserver 1.1.1.1
nameserver 100.100.100.100
searchdomain tail262256.ts.net
```

`100.100.100.100` is Tailscale's MagicDNS resolver. It resolves `<hostname>.tail262256.ts.net` to Tailscale IPs. The `--accept-dns=false` flag in `tailscale up` prevents Tailscale from overwriting `/etc/resolv.conf` — manage DNS manually in LXC containers.

## Troubleshooting

### Exit node stops routing traffic

Symptom: `curl ifconfig.me` returns local IP, not `166.1.160.18`.

```bash
# Check routing table
ip route show table 52

# If table 52 is empty — restart tailscaled and reconnect
systemctl restart tailscaled
sleep 3
tailscale up --accept-routes --exit-node=100.125.198.74 --accept-dns=false
```

### Peer unreachable

```bash
tailscale status           # check if peer is online
tailscale ping <peer-ip>   # test direct vs relay path
tailscale netcheck         # check DERP relay connectivity
```

### Can't connect to Tailnet after reboot

```bash
systemctl status tailscaled
journalctl -u tailscaled -n 50
# If auth expired, re-run: tailscale up --auth-key=<new-key>
```

### Proxyma.io proxy note

The Proxyma.io proxy IP (`91.188.184.112`) is blocked by Anthropic API (returns 403). Do not route Claude API calls through this proxy.
