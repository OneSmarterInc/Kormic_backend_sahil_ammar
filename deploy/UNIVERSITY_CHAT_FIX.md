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
