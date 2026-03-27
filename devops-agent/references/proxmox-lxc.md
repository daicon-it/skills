# Proxmox LXC Reference

Container management for the Proxmox host at `100.93.132.32` (node: `pmx`).

## API Setup

```bash
# Load credentials (vars are NOT exported by default)
export $(grep -v '^#' /root/.config/proxmox-api.env | xargs)

# Test API access
curl -sk -H "Authorization: PVEAPIToken=$PVE_TOKEN_ID=$PVE_TOKEN_SECRET" \
  "https://100.93.132.32:8006/api2/json/nodes/pmx/lxc" | jq '.data[].name'
```

Env file: `/root/.config/proxmox-api.env` (chmod 600). Variables: `PVE_TOKEN_ID`, `PVE_TOKEN_SECRET`.

## List Containers

```bash
# Via API
curl -sk -H "Authorization: PVEAPIToken=$PVE_TOKEN_ID=$PVE_TOKEN_SECRET" \
  "https://100.93.132.32:8006/api2/json/nodes/pmx/lxc" \
  | jq -r '.data[] | "\(.vmid) \(.name) \(.status)"'

# Via SSH on Proxmox host
ssh root@100.93.132.32 "pct list"
```

## Create LXC Container

```bash
export $(grep -v '^#' /root/.config/proxmox-api.env | xargs)

curl -sk -X POST \
  -H "Authorization: PVEAPIToken=$PVE_TOKEN_ID=$PVE_TOKEN_SECRET" \
  "https://100.93.132.32:8006/api2/json/nodes/pmx/lxc" \
  -d "vmid=235" \
  -d "hostname=new-container-235" \
  -d "ostemplate=local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst" \
  -d "storage=local-lvm" \
  -d "rootfs=local-lvm:10" \
  -d "memory=4096" \
  -d "swap=2048" \
  -d "cores=2" \
  -d "onboot=1" \
  -d "unprivileged=1" \
  -d "password=changeme" \
  --data-urlencode "net0=name=eth0,bridge=vmbr0,ip=dhcp"
```

**IMPORTANT:** `net0` contains commas and equals signs — always use `--data-urlencode` for this parameter, never `-d`. Other simple parameters can use `-d`.

### Minimal Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| vmid | 231–299 | Pick next available |
| hostname | descriptive-name-vmid | Include vmid in name |
| ostemplate | local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst | Standard template |
| storage | local-lvm or HDD | SSD for DB, HDD for parsers |
| rootfs | storage:size_gb | e.g. local-lvm:10 |
| memory | 4096 | MB |
| swap | 2048 | MB |
| cores | 1–4 | Host has 4 total |
| onboot | 1 | Always set |

## Start / Stop / Restart

```bash
# Via SSH on host
ssh root@100.93.132.32 "pct start <vmid>"
ssh root@100.93.132.32 "pct stop <vmid>"
ssh root@100.93.132.32 "pct restart <vmid>"

# Via API
curl -sk -X POST \
  -H "Authorization: PVEAPIToken=$PVE_TOKEN_ID=$PVE_TOKEN_SECRET" \
  "https://100.93.132.32:8006/api2/json/nodes/pmx/lxc/<vmid>/status/start"
```

## Execute Commands in Container

```bash
# From CT 101 via Proxmox host
ssh root@100.93.132.32 "pct exec <vmid> -- bash -c 'command here'"

# Multi-line commands
ssh root@100.93.132.32 "pct exec <vmid> -- bash -c '
  apt update
  apt install -y curl
  echo done
'"

# Check a service
ssh root@100.93.132.32 "pct exec <vmid> -- bash -c 'systemctl status myservice'"
```

Note: `pct exec` provides PATH `/sbin:/bin:/usr/sbin:/usr/bin` only. Use full paths for tools in `/usr/local/bin` or `~/.local/bin`.

## Resource Adjustment

```bash
# Change memory and CPU (can be done while running)
ssh root@100.93.132.32 "pct set <vmid> -memory 4096 -swap 2048 -cores 2"

# Resize rootfs (increase only)
ssh root@100.93.132.32 "pct resize <vmid> rootfs +5G"
```

## Mount Points

Add a mount point from the HDD pool into a container:

```bash
# Create directory on host first
ssh root@100.93.132.32 "mkdir -p /HDD/ct-data/<vmid>"

# Attach as mount point
ssh root@100.93.132.32 "pct set <vmid> -mp0 /HDD/ct-data/<vmid>,mp=/mnt/data"
```

CT 231 has: `mp0 = /mnt/images` (200GB HDD, ZFS), containing pulscen/kwork parser output.

## Snapshots

```bash
# Create snapshot
ssh root@100.93.132.32 "pct snapshot <vmid> snap-before-upgrade --description 'before apt upgrade'"

# List snapshots
ssh root@100.93.132.32 "pct listsnapshot <vmid>"

# Rollback (container must be stopped)
ssh root@100.93.132.32 "pct stop <vmid>"
ssh root@100.93.132.32 "pct rollback <vmid> snap-before-upgrade"
ssh root@100.93.132.32 "pct start <vmid>"

# Delete snapshot
ssh root@100.93.132.32 "pct delsnapshot <vmid> snap-before-upgrade"
```

## Storage

| Pool | Type | Size | Use For |
|------|------|------|---------|
| local-lvm | LVM-thin on SSD | ~49GB total | OS disks, databases (CT 231) |
| HDD | ZFS on HDD | ~899GB | Parser output, large data, archives |

Choose `local-lvm` for containers needing fast I/O (CT 231 PostgreSQL). Use `HDD` for containers storing large scraped datasets (CT 232, 233, 234).

## Template Management

```bash
# List available templates
curl -sk -H "Authorization: PVEAPIToken=$PVE_TOKEN_ID=$PVE_TOKEN_SECRET" \
  "https://100.93.132.32:8006/api2/json/nodes/pmx/storage/local/content?content=vztmpl" \
  | jq -r '.data[].volid'

# Download a template (if missing)
ssh root@100.93.132.32 "pveam download local ubuntu-24.04-standard_24.04-2_amd64.tar.zst"
```

## Host Constraints

- **CPU:** AMD Athlon X4 750K, 4 cores. Total cores across all containers should not exceed 4 (or use slight overcommit with caution).
- **RAM:** 16GB total. Current allocation: CT101=4GB, CT231=4GB, CT232=4GB, CT233=4GB, CT234=4GB. Leave ~2GB for Proxmox host.
- **No AVX:** The Athlon X4 750K does not support AVX instructions. Binaries compiled with AVX will SIGILL. Avoid AVX-optimized packages (certain TensorFlow builds, some native Node.js modules). Python with numpy/scipy works fine (falls back to SSE).
- **Disk (SSD):** local-lvm is ~49GB. Monitor with `pvs` and `lvs` on the host.
