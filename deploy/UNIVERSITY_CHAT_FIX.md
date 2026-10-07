# University chat recovery on AWS (Supervisor, no Docker)

These changes are local until explicitly published or copied to the server.
Do not expect a Git pull to install unpublished files.

## What failed

The October 7 10:57 API traceback names the missing physical column
`django_api_agentjob.retention_checked_at`. Migration
`django_api.0015_data_retention_hold` adds it. The supplied logs do not show the
same error after the 11:00 restart. Never fake this migration or suppress database
exceptions to make an incompatible schema appear healthy.

Separately, the officer policy plus every tool schema reserved 16,952 tokens in
the local estimator even before university data, above the default 16,384-token
window. The basic read-only request now reserves 11,194 in that same measurement.
These are scheduling/context estimates, not measured model token counts or RAM.

The agent now reads relevant university sections through its tools rather than
injecting the entire profile on every step. Large proposal schemas load on demand.
Large tool results have explicit partial-evidence markers and exact character
pages; original checkpoint contents remain available. Permissions, validation,
later-turn approval and the context ceiling remain enforced. Very large user
messages or pending proposals can still exceed the ceiling; failures now log the
budget reason without logging profile content. Live inference quality on AWS
still needs verification.

## Apply after installing the updated backend files

Use the same virtual environment, settings and database environment as the
Supervisor web process. Back up the production database before migrating. Run:

```bash
cd /var/www/Kormic_backend_sahil_ammar
./venv/bin/python manage.py migrate --noinput
./venv/bin/python manage.py check_backend_ready
```

The readiness command verifies migration history AND physical AgentJob columns,
then checks the production HMAC signing key length. `check_deployment` also runs
this preflight before its Redis, worker and Ollama probes. If migration history
claims success but columns are missing, check that Supervisor and the shell use
the same database/settings before attempting any schema repair.

## Replace the weak signing key reported by the server

The supplied warning reports a 7-byte HS256 key. This project normally uses
`DJANGO_SECRET_KEY` for JWT signing. Generate a new private value on the server:

```bash
./venv/bin/python -c "import secrets; print(secrets.token_urlsafe(64))"
sudo nano /var/www/Kormic_backend_sahil_ammar/.env
```

Replace only the `DJANGO_SECRET_KEY` value with the generated value. Keep it
private; never paste it into chat or commit it. If Supervisor supplies this variable
directly, update that source too: inherited environment values override `.env`.
Do not change `TOTP_SECRET_KEYS`; existing encrypted TOTP seeds need those keys.
Rotating the signing key invalidates existing JWTs and signed links/sessions;
users must sign in again. Restart all application processes together after the
configuration is ready. Do not restart Ollama just for a signing-key change.

```bash
./venv/bin/python manage.py check_backend_ready
```

Only after it passes:

Wait for active jobs to finish before the first restart if the current worker
configuration has not yet been corrected as described below.

```bash
sudo supervisorctl restart 'kormic:*'
sudo supervisorctl status
```

If you edited Supervisor's configuration itself, use `supervisorctl reread` and
`supervisorctl update` before restarting so it loads the new environment.

## Verify a fresh request

Sign in again, clear the browser Network list and reload the university agent.
`GET /api/chat/jobs/active/` should return 200 with idle or a job state. Ask for
saved admission requirements and check the response against the profile. Verify
that an edit still produces a proposal and requires a later confirmation. Test
large evidence and a second university account to confirm isolation.

```bash
sudo supervisorctl tail -16000 kormic:kormic-web stderr
sudo supervisorctl tail -16000 kormic:kormic-agent-worker stderr
```

Only fresh timestamps demonstrate the post-deployment outcome. Context errors
now include `officer_context_budget_exceeded` and the estimator's reason. Do not
raise context or timeout settings to work around a missing database column.

## Ollama cancellation during a Supervisor restart

The October 7 11:36:17 logs correlate Ollama's cancelled request with SIGTERM
in both the Celery worker pool and Gunicorn. A backend shutdown interrupted that
request. The logs do not identify who initiated the shutdown. The subsequent
`SystemExit` JSON serialization failure occurred in Celery's shutdown handling;
changing serializers or increasing the Ollama timeout does not prevent the
process from being terminated.

Find the existing worker configuration (do not create a duplicate program):

```bash
sudo grep -R -l '^\[program:kormic-agent-worker\]' /etc/supervisor
```

Edit the returned file. In its existing `[program:kormic-agent-worker]` section,
replace or add these four settings, retaining the command, queues, paths, user
and environment. Apply them to `[program:kormic-document-worker]` too if that
section also runs a Celery worker with the 1800-second agent task limit:

```ini
stopsignal=TERM
stopasgroup=false
killasgroup=true
stopwaitsecs=2000
```

The program must launch Celery directly, or use `exec` if a shell wrapper is
essential, so Supervisor signals the worker parent. Ensure `REMAP_SIGTERM` is
not set to `SIGQUIT`. Celery's parent handles TERM as a warm shutdown and lets
active tasks finish. `stopasgroup=true` sends TERM directly to children too,
which prevents relying on that parent-managed drain. `killasgroup=true` cleans
up children only if the grace period expires. 2000 seconds exceeds this
project's 1800-second agent task limit. Other workers need a grace period based
on their own longest task; do not blindly give every worker the same limit.

The first update uses the old settings to stop the existing process. Wait until
current requests/jobs have finished, and avoid submitting more work, before:

```bash
sudo supervisorctl reread
sudo supervisorctl update kormic
sudo supervisorctl status
```

`update kormic` may restart the changed group; do not immediately issue another
`restart kormic:*`. Let one chat finish without service restarts and compare
fresh worker/Ollama logs. Graceful shutdown still delays a deployment until the
active task finishes; it cannot make a forced kill safe.

If you need to restart the Supervisor system service itself, also inspect its
systemd stop policy (`systemctl show supervisor -p KillMode -p TimeoutStopUSec`).
A short systemd timeout or a group-wide TERM can bypass the worker's grace
period. For this deployment, a reviewed service override can use
`KillMode=mixed` and `TimeoutStopSec=2100s`; allow more time if any supervised
program has a longer stop grace. Do not restart the Supervisor daemon merely
to apply a program configuration change.

References: [Celery worker shutdown](https://docs.celeryq.dev/en/stable/userguide/workers.html#worker-shutdown)
and [Supervisor program settings](https://supervisord.org/configuration.html#program-x-section-settings).
