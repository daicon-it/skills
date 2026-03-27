# PostgreSQL Operations Reference

PostgreSQL 16 + pgvector 0.8.2 running on CT 231 (Ubuntu 24.04, 4 CPU, 4GB RAM + 4GB swap).

## Connection Details

- **Host (Tailscale):** `100.124.99.73`
- **Port:** `5432`
- **pg_hba allows:** `192.168.88.0/24` (LAN) and `100.64.0.0/10` (Tailscale CGNAT)

## Databases

| Database | User | Password | pgvector | Purpose |
|----------|------|----------|----------|---------|
| db_pulscen | pulscen | pulscen_pass | enabled | Pulscen parser data |
| db_kwork | kwork | kwork_pass | enabled | Kwork parser data |
| db_telethon | telethon | telethon_pass | enabled | Telethon messages, enrichment |
| skillsdb | skillsdb | jVtZERFuGUrjFTlMPwoPKPyA | enabled | Skills DB (74K+ skills) |
| idea-db | skillsdb | jVtZERFuGUrjFTlMPwoPKPyA | — | Ideas pipeline |

## Connection Strings

```bash
psql "postgresql://pulscen:pulscen_pass@100.124.99.73:5432/db_pulscen"
psql "postgresql://kwork:kwork_pass@100.124.99.73:5432/db_kwork"
psql "postgresql://telethon:telethon_pass@100.124.99.73:5432/db_telethon"
psql "postgresql://skillsdb:jVtZERFuGUrjFTlMPwoPKPyA@100.124.99.73:5432/skillsdb"
psql "postgresql://skillsdb:jVtZERFuGUrjFTlMPwoPKPyA@100.124.99.73:5432/idea-db"
```

Use the same DSN format in Python (`psycopg2`, `asyncpg`), Node.js (`pg`), etc.

## Common Admin Tasks

### Create database and user

```sql
-- Connect as postgres (on CT 231)
CREATE DATABASE mydb;
CREATE USER myuser WITH PASSWORD 'mypassword';
GRANT ALL PRIVILEGES ON DATABASE mydb TO myuser;

-- Also grant schema privileges (PostgreSQL 15+)
\c mydb
GRANT ALL ON SCHEMA public TO myuser;
```

### Inspect databases

```bash
# List databases
psql "postgresql://postgres@100.124.99.73:5432/postgres" -c "\l"

# List tables in a database
psql "postgresql://<conn_string>" -c "\dt"

# Table sizes
psql "postgresql://<conn_string>" -c "
  SELECT relname, pg_size_pretty(pg_total_relation_size(oid))
  FROM pg_class WHERE relkind='r' ORDER BY pg_total_relation_size(oid) DESC LIMIT 20;
"
```

### Alter user / permissions

```sql
ALTER USER myuser WITH PASSWORD 'newpassword';
ALTER DATABASE mydb OWNER TO myuser;
REVOKE ALL ON DATABASE mydb FROM public;
```

## pgvector

pgvector 0.8.2 is installed and enabled in all main databases.

```sql
-- Enable in a database (already done for main DBs)
CREATE EXTENSION IF NOT EXISTS vector;

-- Create a vector column
ALTER TABLE my_table ADD COLUMN embedding vector(768);

-- IVFFlat index (approximate, fast, good for >100K rows)
CREATE INDEX ON my_table USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- HNSW index (better recall, slower build)
CREATE INDEX ON my_table USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- Similarity search
SELECT id, 1 - (embedding <=> '[0.1, 0.2, ...]'::vector) AS similarity
FROM my_table
ORDER BY embedding <=> '[0.1, 0.2, ...]'::vector
LIMIT 10;
```

Operators: `<=>` cosine distance, `<->` L2 distance, `<#>` negative inner product.

## Storage

- **rootfs:** 20GB SSD (local-lvm) — PostgreSQL data directory `/var/lib/postgresql/`
- **mp0:** 200GB HDD (ZFS pool) mounted at `/mnt/images`
  - `/mnt/images/pulscen/{products,suppliers,services,portfolios}` — owned by postgres
  - `/mnt/images/kwork/{products,suppliers,services,portfolios}` — owned by postgres

Large binary data (images, files) should go to `/mnt/images`, not the SSD rootfs.

## Backup

### Daily automated backup (CT 231)

Script: `/root/scripts/pg-backup.sh`
Cron: `0 3 * * *` (03:00 daily)
Output: `/root/backups/pg_dumpall_<date>.sql.gz`
Retention: 7 daily + 4 weekly

```bash
# Manual backup
pg_dumpall -h localhost -U postgres | gzip > /root/backups/manual_$(date +%Y%m%d).sql.gz

# Restore
gunzip -c /root/backups/pg_dumpall_20260101.sql.gz | psql -h localhost -U postgres
```

### Offsite backup

Weekly rsync to hiplet-66136 (193.168.199.43). Runs Sunday 04:00 from CT 231.

### Verify backup integrity

```bash
# Check if dump is valid (parse without restoring)
gunzip -c backup.sql.gz | grep "PostgreSQL database dump complete" | tail -1
```

## Troubleshooting

### Connection refused

```bash
# Check PostgreSQL is running (on CT 231)
ssh root@100.93.132.32 "pct exec 231 -- bash -c 'systemctl status postgresql'"

# Check listen_addresses
ssh root@100.93.132.32 "pct exec 231 -- bash -c \"psql -U postgres -c 'SHOW listen_addresses'\""
# Should return: *

# Check pg_hba.conf allows your IP range
ssh root@100.93.132.32 "pct exec 231 -- bash -c 'grep -v ^# /etc/postgresql/16/main/pg_hba.conf | grep -v ^$'"
```

### Disk full on SSD

```bash
# Check SSD usage
ssh root@100.93.132.32 "pct exec 231 -- bash -c 'df -h /'"

# Find large tables
psql "<conn>" -c "SELECT schemaname, tablename, pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) FROM pg_tables ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC LIMIT 10;"

# VACUUM to reclaim space
psql "<conn>" -c "VACUUM FULL ANALYZE;"
```

### Slow queries

```bash
# Enable query logging temporarily
psql "<conn>" -c "SET log_min_duration_statement = 1000;"  # log queries > 1s

# Check for missing indexes
psql "<conn>" -c "SELECT * FROM pg_stat_user_tables WHERE n_live_tup > 1000 AND seq_scan > idx_scan ORDER BY seq_scan DESC LIMIT 10;"
```
