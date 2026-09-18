# Kormic backend

Django API, authentication/TOTP, student profiles and verification, asynchronous AI chat, university knowledge and staff permissions, institute claim imports, and platform administration.

Read [System guide](SYSTEM.md) for repository ownership, environment configuration, API contracts, roles, and coordinated rollout. See [Deployment](DEPLOYMENT.md), [Web authentication](WEB_AUTH.md), and [TOTP security](TOTP_SECURITY.md).

## Run locally

Use Python 3.11+ and Docker Compose v2. Copy `.env.template` to `.env`; set a unique `POSTGRES_PASSWORD`, `DJANGO_SECRET_KEY`, and `TOTP_SECRET_KEYS` (generate with `cryptography.fernet.Fernet.generate_key()`). Set `EMAIL_MODE=dev` for console email, and configure model/OAuth credentials for those integrations.

```sh
cp .env.template .env
# Fill in the required settings before starting.
docker compose config
docker compose up --build -d
curl http://127.0.0.1:8000/api/health/
```

Compose runs migrations before web and workers. API: `http://localhost:8000/api`; PostgreSQL from the host: `localhost:5438`, inside Compose: `postgres:5432`. See `.env.template` for supported settings.

Local clients use `VITE_API_BASE_URL=http://localhost:8000` (web portals) and `EXPO_PUBLIC_API_BASE_URL=http://localhost:8000/api` (student). Generate these consistently using the system guide; physical devices need a reachable development host.

## Verify

```sh
docker compose exec web python manage.py check
docker compose exec web python manage.py makemigrations --check --dry-run
docker compose exec web python manage.py test
python scripts/test_configure_environment.py
```

Tests require a dedicated test database and Redis cache. CI exercises the complete suite in normal and shuffled order, plus schema and Compose configuration checks. Do not point test commands at production.
