# Deploying IranDoc News Generation

This guide covers a fresh deployment on a Linux VPS, plus the operational
details that are easy to get wrong: secrets, database persistence, encrypted
credentials, reverse proxying, and upgrades.

The stack is a Docker Compose application:

| Service | Role |
|---|---|
| `postgres` | PostgreSQL 16, persistent `pg_data` volume |
| `migrate` | One-shot schema migration runner |
| `api` | FastAPI machine API + Swagger UI |
| `worker` | Generation Worker |
| `sender` | Delivery Worker |
| `web` | Server-rendered review panel |
| `demo` | Optional static API demo |

---

## 1. Prerequisites

- A Linux VPS with Docker Engine and the Compose plugin
- At least 2 GB RAM (4 GB recommended), 2 CPU cores, and disk for PostgreSQL
- A domain or hostname if you want HTTPS
- An OpenAI-compatible LLM endpoint and API key
- Bale or Telegram bot tokens if news will be published
- Outbound HTTPS access from the VPS to the LLM and messaging providers

### Install Docker (Ubuntu example)

```bash
sudo apt update
sudo apt install -y ca-certificates curl
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
newgrp docker
docker compose version
```

### Firewall

Only expose what users need:

| Port | Purpose |
|---|---|
| `443` / `8443` | HTTPS panel (recommended) |
| `8445` | HTTPS API/Swagger (if you use Caddy) |
| `22` | SSH administration |
| `8000` | Direct API, only if you intentionally expose it |
| `8080` | Optional demo, only if needed |

Do **not** expose PostgreSQL (`5432`) publicly. The Compose file binds it to
loopback. The API also binds to loopback by default (`API_BIND=127.0.0.1`);
publish it through the reverse proxy. Set `API_BIND=0.0.0.0` only if you
intentionally want direct port-8000 access.

---

## 2. Get the project

```bash
git clone https://github.com/MohadeseAnsari/project.git
cd project
git checkout main
```

---

## 3. Create the production environment file

```bash
cp .env.example .env
```

Edit `.env` and set real values. Generate secrets with:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Minimum required production values:

```dotenv
POSTGRES_PASSWORD=<long random password>
POSTGRES_DB=irandoc_news

SHARED_SECRET=<long random API secret>
PANEL_SECRET_KEY=<long random cookie/CSRF secret>
PANEL_ADMIN_USERNAME=admin
PANEL_ADMIN_PASSWORD=<strong panel password>

AVALAI_API_KEY=<provider API key>
AVALAI_BASE_URL=https://api.avalai.ir/v1
LLM_MODEL=gpt-5.6-luna
```

Set these according to the deployment:

```dotenv
# API listens on loopback; the reverse proxy is the public door.
API_BIND=127.0.0.1
API_PORT=8000

# Panel listens on loopback; the reverse proxy is the public door.
PANEL_PORT=127.0.0.1:8081

# Set true when the panel is served over HTTPS.
PANEL_COOKIE_SECURE=true

# Required when a host proxy reaches the web container through the Docker
# bridge. Find the subnet with:
#   docker network inspect project_default --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}'
PANEL_TRUSTED_PROXIES=172.19.0.0/16

# Automatic publishing is the default. Set true for manual approval.
REQUIRE_HUMAN_CONFIRMATION=false

# What happens when no category qualifies.
UNMATCHED_CATEGORY_BEHAVIOR=review
```

Bot tokens:

```dotenv
BALE_BOT_TOKEN=<bale bot token or empty>
TELEGRAM_BOT_TOKEN=<telegram bot token or empty>
```

Do not commit `.env`.

---

## 4. Start the stack

```bash
docker compose up -d --build
docker compose ps
```

The `migrate` service applies every SQL migration exactly once. Wait until it
exits successfully; the API, Worker, Sender, and panel depend on it.

Check the API:

```bash
curl http://127.0.0.1:8000/health
```

Expected:

```json
{"status":"ok","database":"connected"}
```

Check the panel:

```bash
curl -I http://127.0.0.1:8081/login
```

Check logs:

```bash
docker compose logs -f api
docker compose logs -f worker
docker compose logs -f sender
docker compose logs -f web
```

---

## 5. Put HTTPS in front of the panel

The panel and API should not be exposed directly. Use a reverse proxy.

### Caddy example

Install Caddy on the host, then use a configuration like:

```caddyfile
https://panel.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8081
    header {
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "SAME-ORIGIN"
        Referrer-Policy "same-origin"
        -Server
    }
}

https://api.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
    header {
        X-Content-Type-Options "nosniff"
        -Server
    }
}
```

Then:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Open the panel at `https://panel.example.com`, log in with the admin credentials,
and configure:

1. **Models** — verify the active model, endpoint, key, and run a connection test.
2. **Channels** — add Bale/Telegram destinations and bot tokens.
3. **Categories** — select the channels each category routes to.
4. **Submit** — create a test thesis and verify generation and delivery.

### Nginx alternative

Proxy `panel.example.com` to `127.0.0.1:8081` and `api.example.com` to
`127.0.0.1:8000`. Set `PANEL_COOKIE_SECURE=true` and forward the standard
`X-Forwarded-*` headers.

---

## 6. Database

### Persistence

PostgreSQL data lives in the named volume `pg_data`. Do not run:

```bash
docker compose down -v
```

unless you intentionally want to delete the database.

### Backup

```bash
mkdir -p backups
docker compose exec -T postgres pg_dump -U postgres -d irandoc_news \
  -Fc > "backups/irandoc_$(date +%Y%m%d_%H%M).dump"
```

Restore:

```bash
docker compose stop api worker sender web
docker compose exec -T postgres pg_restore -U postgres -d irandoc_news \
  --clean --if-exists < backups/irandoc_YYYYMMDD_HHMM.dump
docker compose up -d
```

Test a restore on a non-production database before relying on it.

### Migrations

- Files in `migrations/` are immutable and applied in filename order.
- `_migrations` records what has been applied.
- New deployments apply everything automatically.
- Existing deployments apply only new files on the next `docker compose up`.
- There is no automatic down migration. Back up before upgrading.

---

## 7. Encrypted credentials

Provider keys and per-channel bot tokens stored from the panel are encrypted
with a Fernet master key.

The key is:

1. `CREDENTIALS_KEY` from the environment, or
2. `data/.credentials_key` inside the shared `credentials_data` volume.

The volume is shared by `api`, `worker`, `sender`, and `web`.

**Back up the key.** If it is lost, every encrypted credential in the database
becomes undecryptable.

```bash
docker run --rm -v project_credentials_data:/data -v "$PWD/backups:/backup" \
  alpine tar czf /backup/credentials_data.tar.gz -C /data .
```

On a new VPS, restore that volume before starting the services, or set the same
`CREDENTIALS_KEY` in `.env` for every service.

---

## 8. Model and channel setup

### Model

The panel’s `/models` page is authoritative. A row can define:

- provider and model identifier
- endpoint (`base_url`)
- API key or environment-variable name
- temperature and output-token cap
- connection-test result

`LLM_MODEL` and `AVALAI_BASE_URL` in `.env` are fallbacks only.

### Channels

Add channels on `/channels`. A channel needs a valid platform, numeric chat id
or `@username`, and a usable bot token (per-channel or platform-wide).

### Routing

On `/categories`, select the channels for each category. A category with no
channels publishes nothing. This is intentional.

---

## 9. Worker behavior

### Generation Worker

- Claims `ready` jobs with `FOR UPDATE SKIP LOCKED`.
- Makes one LLM call for news + ISCED classification.
- Stores the article and `main_category_matches`.
- Moves the record to `review`.
- Recovers stale `processing` jobs after the lease.
- Rolls back an aborted transaction before recording a failure.

### Sender Worker

- Resolves the union of qualifying categories’ channels.
- Deduplicates channels.
- Honors `channels.activated_at`.
- Retries transient failures with backoff.
- Records provider message ids and final status.

### Automatic vs gated publishing

```dotenv
# Automatic: generate and publish without manual approval
REQUIRE_HUMAN_CONFIRMATION=false

# Gated: deliveries wait for an editor to approve the record
REQUIRE_HUMAN_CONFIRMATION=true
```

An `other`-only record never auto-publishes.

---

## 10. Updating an existing deployment

```bash
cd project
git pull --ff-only
docker compose up -d --build
docker compose ps
```

Rebuild **all** services after schema or backend changes, not only `web`:

```bash
docker compose up -d --build api worker sender web
```

If only templates or CSS changed, rebuilding `web` is enough.

After a deploy, verify:

```bash
curl http://127.0.0.1:8000/health
docker compose logs --tail=50 worker
docker compose logs --tail=50 sender
```

The panel footer shows the asset/build version.

---

## 11. Troubleshooting

| Symptom | Check |
|---|---|
| Compose refuses to start | Missing required `.env` value (`POSTGRES_PASSWORD`, `SHARED_SECRET`, `PANEL_SECRET_KEY`, `PANEL_ADMIN_PASSWORD`) |
| API 401 | `SHARED_SECRET` mismatch; the value is in `.env`, not the panel password |
| Panel login loops | `PANEL_COOKIE_SECURE` must match HTTP/HTTPS; check proxy headers |
| All clients share one lockout IP | Set `PANEL_TRUSTED_PROXIES` to the Docker bridge subnet |
| Worker running but no progress | Check `main.status`, `main.error_message`, model key/endpoint, and worker logs |
| Job stuck in `processing` | Wait for the lease or reset after confirming no worker is processing it; check DB errors |
| No deliveries | Verify qualifying `main_category_matches` and category channel routes |
| Delivery pending forever | Check `next_attempt_at`, sender logs, bot token, channel activation time |
| Delivery failed | Read `deliveries.error_message`; retry from the panel |
| `column ... does not exist` | Old container image against new schema; rebuild all services |
| 500 after migration | Check migration logs and `docker compose logs web/api`; rebuild all services |
| Panel old design after deploy | Hard refresh; check the build stamp; ensure the proxy is not caching static assets |
| `CREDENTIALS_KEY` error | Restore `credentials_data` or set the same key on every service |
| Disk full | Check PostgreSQL volume and container logs; prune old images carefully |

---

## 12. Security checklist

- [ ] `POSTGRES_PASSWORD` is long and random
- [ ] `SHARED_SECRET` is long and random and is not the panel password
- [ ] `PANEL_SECRET_KEY` is long and random
- [ ] `PANEL_ADMIN_PASSWORD` is strong
- [ ] `PANEL_COOKIE_SECURE=true` behind HTTPS
- [ ] `PANEL_TRUSTED_PROXIES` is set to the proxy/bridge subnet
- [ ] PostgreSQL port is not public
- [ ] API/panel are behind HTTPS
- [ ] `.env` is not committed
- [ ] `credentials_data` is backed up
- [ ] PostgreSQL backups are scheduled and restore-tested
- [ ] The demo UI is disabled or firewalled if not needed
- [ ] Unattended deployments use a sandbox/container boundary

---

## 13. Migrating from an existing VPS

1. Back up the database and credentials volume.
2. Copy `.env` (or recreate it with the same secrets).
3. Restore `pg_data`/database and `credentials_data` on the new host.
4. `git checkout` the same or newer release.
5. `docker compose up -d --build`.
6. Verify `/health`, panel login, model connection, and a test generation.
7. Update DNS/reverse proxy.
8. Keep the old host available until the new one is verified.
