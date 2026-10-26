# Production deployment: AWS EC2 with Supervisor

The existing Kormic production installation uses **Supervisor, not Docker**.
The application directory is `/var/www/Kormic_backend_sahil_ammar`; its Python
executable is `./venv/bin/python`. Nginx forwards `backend.kormic.ai` to
`127.0.0.1:8400`. Local development uses port 8000. Confirm the host configuration
before changing ports; other applications share this server.

Docker Compose is an alternative deployment, documented in
[deploy/DOCKER_COMPOSE.md](deploy/DOCKER_COMPOSE.md) and [deploy/AWS.md](deploy/AWS.md).
Do not run that alternative alongside Supervisor on the existing host.

## CSP report-only observation

Deploy the collector before publishing the frontend reporting header. After the
reviewed code is present on AWS, use the production web environment:

```bash
cd /var/www/Kormic_backend_sahil_ammar
./venv/bin/python manage.py migrate security_reports --noinput
sudo supervisorctl restart kormic:kormic-web
curl --fail-with-body -i https://backend.kormic.ai/api/security/csp-report/ \
  -H 'Content-Type: application/csp-report' \
  --data '{"csp-report":{"document-uri":"https://platform.kormic.ai/","effective-directive":"script-src","blocked-uri":"https://csp-smoke-test.invalid"}}'
./venv/bin/python manage.py csp_report_summary --days 7
```

Expect HTTP 204 and a `csp-smoke-test.invalid` counter. This synthetic counter is
a health check, not a browser violation. Publish the frontend report-only header
only after this check passes. Verify it with `curl -I https://platform.kormic.ai/`
and trigger a controlled inline-script violation in a test browser session to
confirm browser delivery, then verify its counter. Record that timestamp as the
start of the seven full days; code creation or a curl check alone does not start
the observation period. Inspect counters daily and exercise all four portal
flows. Missing reports alone do not establish that the collector is healthy.

The endpoint accepts legacy CSP reports and Reporting API batches, bounds payloads
to 16 KiB and batches to 20 entries, and uses the shared Redis cache to allow
120 requests per minute globally. Cache failure returns 503; excess requests
return 429. Monitor those status codes in Nginx logs because reports may be lost
during failures or throttling. Daily aggregates retain only date, directive,
blocked origin and count, without raw payloads, paths, queries, IPs or samples.
Reports can be spoofed and must be reproduced before changing the policy.

Keep `Content-Security-Policy-Report-Only` throughout observation **and afterward**.
Do not automatically enable enforcement after seven days. Change the header to
`Content-Security-Policy` only after the user explicitly approves enforcement and
legitimate violations have been addressed. This collector creates no Celery jobs
and does not change authentication.

## Service status

```bash
cd /var/www/Kormic_backend_sahil_ammar
sudo supervisorctl status
```

The current group is `kormic`, with programs `kormic-web`, `kormic-celery`,
`kormic-celery-beat`, `kormic-agent-worker`, `kormic-document-worker`,
`kormic-index-worker`, `kormic-university-worker` and `kormic-github-worker`.
Use these names only after confirming them in the status output. Redis and
PostgreSQL are separate dependencies. Keep both private to the application host.

Use the same environment and database configuration as the supervised processes.
Inherited Supervisor environment variables override `.env`; inspect configuration
privately, never paste secrets into logs or tickets. Production requires PostgreSQL,
`DJANGO_DEBUG=false`, HTTPS origins and strong independent signing and TOTP keys.
See [TOTP_SECURITY.md](TOTP_SECURITY.md) and [APP_LINKS.md](APP_LINKS.md).
Inference follows [CLAUDE_INFERENCE.md](CLAUDE_INFERENCE.md); no Ollama service is required.
Crawler installation is covered in [docs/crawlee.md](docs/crawlee.md).

## Deploy an update

1. Schedule a maintenance window, prevent new work and let active jobs drain.
   Record the deployed commit and create a verified database backup using the
   site's existing backup procedure. Preserve uploads, `.env` and TOTP key rings.
2. Inspect the checkout before updating:

   ```bash
   cd /var/www/Kormic_backend_sahil_ammar
   git status --short
   git rev-parse HEAD
   git fetch origin main
   git pull --ff-only origin main
   ```

   If the branches diverge or files are modified, reconcile the deployment changes
   explicitly. Do not reset the server checkout or force a merge to hide differences.
3. After jobs drain, stop the application group before applying the password-reset
   schema change: old processes still refer to the removed `otp_hash` column.
   Install dependencies and migrate with the web environment:

   ```bash
   sudo supervisorctl stop 'kormic:*'
   ./venv/bin/python -m pip install -r requirements.txt
   ./venv/bin/python manage.py migrate --noinput
   ./venv/bin/python manage.py check_backend_ready
   ```

   Stop if any command fails. Readiness checks migration history, physical
   `AgentJob` columns and the production signing-key length. Never fake a migration
   or suppress a database error to make readiness pass.
4. Once readiness passes, start the application group on the new code:

   ```bash
   sudo supervisorctl start 'kormic:*'
   sudo supervisorctl status
   ```

   If Supervisor configuration changed, use `sudo supervisorctl reread` followed
   by `sudo supervisorctl update kormic` instead; update may already restart changed
   programs. Avoid an unnecessary second restart. Follow the worker grace-period
   guidance in [the recovery note](deploy/UNIVERSITY_CHAT_FIX.md).
5. Check fresh web and worker logs, then verify login and a read-only university
   request during the approved verification window. An HTTP health response alone
   does not prove authentication, worker execution or the database schema works.

   ```bash
   sudo supervisorctl tail -16000 kormic:kormic-web stderr
   sudo supervisorctl tail -16000 kormic:kormic-agent-worker stderr
   ```

## Recovery and signing keys

For the missing `django_api_agentjob.retention_checked_at` incident, use
[deploy/UNIVERSITY_CHAT_FIX.md](deploy/UNIVERSITY_CHAT_FIX.md). Confirm the shell and
web use the same database before repairing inconsistent migration history.

The incident reported a weak signing key. Its rotation is **not confirmed by this
repository**. Run `check_backend_ready` on the host and record the change in the
private operations log. Passing the length check proves minimum length, not
randomness or historical rotation. Do not print the key.

A signing-key rotation invalidates JWTs and signed links; coordinate a maintenance
window and restart every application process with the same new secret. Preserve
`TOTP_SECRET_KEYS` so enrolled authenticators remain decryptable. Do not silently
rotate production secrets as part of a code cleanup.

For rollback, restore the recorded application revision only if compatible with
the migrated schema. Otherwise restore the verified database backup under a
maintenance window using the site's recovery procedure; never delete new user data
or reverse migrations blindly.

## Preparing a review package

Share repository access or an archive of tracked source, not a zipped working folder:

```bash
git archive --format=zip --output=../kormic-backend-source.zip HEAD
```

This excludes ignored uploads, checkpoint databases and `.env`. Check that the
chosen revision contains the changes intended for review. If an earlier package
included private data, revoke its sharing and request deletion by recipients;
removing a local archive does not recall copies already distributed.
