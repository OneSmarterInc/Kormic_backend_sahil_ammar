# AWS backend + Ollama, Vercel frontend

Use `compose.aws.yml` as a **standalone** Compose project. It does not change the
local Windows setup. It creates project-scoped `kormic-v2` containers and volumes;
it neither reuses nor deletes the old Kormic database. No public ports are exposed
for PostgreSQL, either Redis, or Ollama. The host's existing Nginx proxies to
127.0.0.1:8030; only this API port is published, on loopback.

## Capacity and prerequisites

The shared t3.medium has only 4 GiB RAM and previously had 4.7 GB disk free.
Expand its EBS disk before building (60 GB is a reasonable test allocation).
This configuration is a constrained trial, not a ten-user throughput guarantee.
It uses one Gunicorn process with two threads, one process per Celery queue group,
and single-slot research/GitHub workers. The background worker consumes both
document and indexing queues, so those jobs can delay each other. Celery's default
prefork pool retains task time limits. Beat must run as exactly one instance.

Ollama gets one CPU, a 2 GiB RAM cap and 3 GiB combined RAM+swap cap by default.
These limits do NOT reserve capacity for the other applications and do NOT prove
the model fits. The application's existing 16K context settings are preserved;
we do not silently truncate tool instructions to fit RAM. Real inference must pass
before opening testing. Increase RAM/reduce other load if it fails; do not disable
OOM protection. CPU credit depletion and swapping can cause long delays.

Requires Docker Engine and Compose v2. Do not restart the host Docker daemon or
stop MIR/8K/MPL. Stop the OLD Kormic web service if it still occupies 8030.

## Fresh deployment

```bash
cd /home/ubuntu
git clone https://github.com/OneSmarterInc/Kormic_backend_sahil_ammar.git kormic-backend
cd kormic-backend
cp .env.aws.example .env.aws
chmod 600 .env.aws
nano .env.aws
```

Fill every secret, real SMTP setting, API domain and frontend origin. Do not use
example domains. Generate independent Django, database and TOTP secrets (Fernet
for TOTP). For a fresh database only, after building the image, you can generate:

```bash
docker build -t kormic-backend:local .
docker run --rm kormic-backend:local python -c "import secrets; print(secrets.token_urlsafe(48))"
docker run --rm kormic-backend:local python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Run the token generator separately for each non-TOTP secret. Store the generated
values privately in .env.aws. A missing SMTP configuration fails Django startup;
valid SMTP credentials are also necessary for signup/password-reset email.
Set Anthropic credentials for permitted research fallback. Configure OAuth keys
and GitHub callback when using GitHub profile integration.

Use this shell helper for every command (run it again in a new SSH session):

```bash
dc() { docker compose --env-file .env.aws -f compose.aws.yml "$@"; }
dc config --quiet
dc build web
dc up -d
dc ps -a
dc logs --tail=80 migrate model_init web
```

`migrate` and `model_init` must finish with exit code 0; they are intentionally
one-shot containers. Model initialization pulls qwen3:0.6b into a persistent volume.
All application workers wait for database migrations and model initialization.
Do not share `dc config` without `--quiet`: it expands secrets.

```bash
dc exec web python manage.py check --deploy
dc exec web python manage.py createsuperuser
dc exec web python manage.py check_deployment --model --timeout 240
dc logs --tail=50 celery_beat university_worker github_worker
```

The deployment check performs database/Redis/cache round trips, executes a real
side-effect-free task through EACH queue and the result backend, checks the model
download, and optionally generates text at the agent's 16K context size. It does
not send email or call Claude. Beat and the two database-queue workers also need
workflow testing; a running process alone does not prove jobs are completing.

## HTTPS and Vercel

Point api.YOURDOMAIN at EC2 and retain the existing host Nginx. Adapt the supplied
aws.nginx.conf to that domain and existing site; avoid duplicate server_name
entries. Preserve all other sites. Test `sudo nginx -t` before reloading. Configure
an HTTPS certificate using the host's existing certificate manager. Django expects
the proxy to overwrite X-Forwarded-Proto, and its production HTTP redirect stays on.

Prefer Vercel custom domains under the same parent domain: app.YOURDOMAIN,
admin.YOURDOMAIN, university.YOURDOMAIN and API api.YOURDOMAIN. Put the EXACT
frontend HTTPS origins in DJANGO_CORS_ALLOWED_ORIGINS (comma separated, no paths).
Keep WEB_COOKIE_SAMESITE=Lax for these same-site domains.

For a *.vercel.app frontend and an unrelated API domain, WEB_COOKIE_SAMESITE=None
enables Secure cross-site auth/CSRF cookies. Some browsers block third-party cookies
even then; same-site custom domains (or a reviewed same-origin proxy) are the reliable
solution. CORS alone cannot fix blocked cookies. Do not disable CSRF or allow '*'.

Set each frontend's existing API-origin build setting to the HTTPS AWS API, rebuild
on Vercel, and test login/TOTP/reload/logout in a real browser. Android separately
uses EXPO_PUBLIC_API_BASE_URL=https://api.YOURDOMAIN/api and needs an APK rebuild.
Keep API secrets in AWS only, never in public frontend build variables.

```bash
curl -f https://api.YOURDOMAIN/api/health/
docker stats --no-stream
free -h
df -h /
```

Then test registered-university chat, new-university research, CV/LinkedIn uploads,
GitHub extraction, notifications and department queries. Increase concurrent users
gradually. Monitor failed/queued jobs, worker logs, memory, swap and CPU credits.

## Updates, persistence and recovery

Uploads, database, Redis broker, embeddings, model files and beat schedule have
named volumes. Container code is built into the image; there is no source bind mount.
Take database dumps and upload backups off the instance before updates. Never use
`down -v` or global Docker prune as a routine deployment step.

For an update, back up first, pull the reviewed revision, then:

```bash
dc build web
dc stop web celery_worker agent_worker background_worker celery_beat university_worker github_worker
dc run --rm migrate
# Continue ONLY if migration succeeded.
dc up -d
dc exec web python manage.py check_deployment --model --timeout 240
```

Only this Compose project's applications stop; PostgreSQL, Redis, Ollama and other
projects remain running. Do not scale model/worker concurrency on a 4 GiB host
without measurements. Pin OLLAMA_IMAGE to the tested release/digest after validation.
