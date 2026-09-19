# Backend deployment

Start with [SYSTEM.md](SYSTEM.md) for environment generation and coordinated frontend rollout. This file describes the committed `docker-compose.yml`.

## Configure and start

Copy `.env.template` to `.env` and supply database credentials, Django secret, independent TOTP encryption keys, OAuth/model credentials, and email delivery settings. Set `DJANGO_DEBUG=false`, exact allowed hosts and HTTPS frontend origins in production. Generate `.env.urls` for the target environment. See [WEB_AUTH.md](WEB_AUTH.md) and [TOTP_SECURITY.md](TOTP_SECURITY.md).

```sh
docker compose config
docker compose up --build -d
docker compose ps
docker compose logs --tail=100 migrate web chat_worker
curl http://127.0.0.1:8000/api/v1/health/
```

| Service | Purpose |
| --- | --- |
| postgres | PostgreSQL 16; loopback port 5438 → container 5432 |
| redis | Redis 7; shared cache, Celery broker and results |
| migrate | One-shot migrations; web/workers wait for success |
| web | collectstatic, then Gunicorn on container port 8000 / host loopback port 8000 |
| celery_worker | General Celery tasks |
| chat_worker | Dedicated `chat` queue, prefork, concurrency 2; required for chat jobs |
| celery_beat | Scheduled tasks; persistent schedule volume under /home/app |

Compose injects `postgres:5432` and Redis database indexes 0 (broker), 1 (results), 2 (cache). A host-run Django process uses PostgreSQL port 5438 for this Compose database. `POSTGRES_PASSWORD` is mandatory. Worker processes must share the same database, cache, encryption keys and code version as web.

Named volumes `postgres_data`, `redis_data` (AOF enabled), `static_data`, `uploads_data`, and `beat_data` persist data. Back up PostgreSQL and uploads before migrations. Do not run `docker compose down -v` on a deployment with data to retain. Compose runs the built image without mounting the host checkout over it. Rebuild after code changes. Keep one beat instance; its schedule volume is not a shared multi-scheduler database.

## Reverse proxy

Terminate TLS at the proxy and preserve host/protocol headers. The upstream is port 8000:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

Database/API published ports default to loopback. For a deliberate LAN pilot, set `API_BIND_ADDRESS=0.0.0.0` and restrict access at the firewall; keep PostgreSQL local. Serve static files appropriately; keep private uploads behind their authorized download paths. Configure the frontend host to serve its SPA entry point for claim links.

Gunicorn defaults to a 120-second timeout. Chat generation runs in the dedicated worker with its own shorter bounded execution; increasing the web timeout is not a replacement for running `chat_worker`. Inspect generation states and sanitized telemetry to detect stalled work.

## Verify and upgrade

```sh
docker compose exec web python manage.py check --deploy
docker compose exec web python manage.py showmigrations
docker compose logs --tail=100 celery_worker chat_worker celery_beat
```

A healthy web process alone does not prove queue processing, email or provider connectivity. Run staging login/TOTP, CSRF refresh, staff permission, upload and chat smoke tests. Monitor worker failures and latency percentiles under representative traffic. On upgrade, retain secrets and volumes, rebuild all services, and confirm the migration job completes before accepting traffic. Restore a tested backup if a schema rollback is necessary; do not assume reversing a migration preserves user data.

See [Knowledge, telemetry and privacy operations](KNOWLEDGE_PRIVACY_OPERATIONS.md) for pgvector migration, embedding configuration, source freshness, model pricing, data export/deletion and retention rollout.

New clients use `/api/v1/`; `/api/` remains a compatibility alias. See [API version policy](API_VERSIONING.md).

## CI gates and production evolution

See [CI_CD.md](CI_CD.md) for checks and merge protection. The Docker job builds this exact image, starts an isolated PostgreSQL/Redis/web/worker/beat stack, applies migrations, checks API/worker health and destroys only its ephemeral CI volumes. It uses generated test secrets and no production provider credentials.

Stay on the single-host pilot until capacity or availability requirements justify migration. The later target is static web portals on Vercel/CDN, signed mobile releases through EAS/app stores, the API and separate scalable Celery workers on managed containers, RDS PostgreSQL, ElastiCache Redis, S3 private uploads, centralized logs/traces, and AWS Secrets Manager/Parameter Store. These are future deployment choices, not services provisioned by this change. Preserve private-file authorization, encryption keys, queue routing and a tested data migration/rollback plan when adopting them.
