# Security fixes 1.1–1.6

## Fetch boundary

URL discovery pages, robots.txt, sitemaps, and initial knowledge scraping use a shared public-only fetcher. Every redirect is checked against the original domain policy. DNS failure rejects the request; no DNS verdict is cached. Immediately before opening each connection, every resolved address must be public and the socket connects to one validated IP literal. TLS retains the original hostname for SNI/certificate verification. There is no insecure retry, proxy environment inheritance or automatic redirect handling. HTTPS-to-HTTP redirects are rejected. Certificate failures appear in discovery failure records for the university.

Responses are limited to 2 MiB, including bodies without Content-Length and decompressed gzip content/sitemaps. Redirects are limited to five. Robots failures other than missing files stop crawling rather than silently granting permission. Pages blocked by these checks are not parsed into knowledge.

## Institute authentication and claims

All six institute roster/upload/download/invitation endpoints require authentication and confirmed TOTP enrollment. A project-wide URL permission test has explicit exceptions only for TOTP setup, own-session status and logout.

New OTP records use a versioned SHA-256 HMAC with the deployment signing secret and a random per-issuance nonce; comparison is constant-time. The delivery task verifies the same format. Legacy bare SHA-256 OTPs are rejected: users with a code issued before this deployment must request a new code. Redis delivery payloads are plaintext for at most 120 seconds, never Celery arguments; delivery, successful verification and resend cleanup delete the applicable payload. Verification locks the row and consumes the code. Keep Redis private and access-controlled; a cache compromise during that short delivery window can still expose the code.

When DEBUG is false, a missing/blank DJANGO_SECRET_KEY prevents startup. Debug-only development may use an ephemeral key that invalidates sessions on restart. Supply a strong persistent secret in staging/production. This change does not rotate any deployed secret automatically.

## Roster files

CSV uploads have configurable 5 MiB and 5,000-row defaults. Size is checked before parsing and counted again while streaming; CSV extension/content type, UTF-8, unique required headers and row shape are validated before database writes. Valid rows retain the existing partial-acceptance behavior. Downloads stream an owned private file with no-store/nosniff headers.

New stored paths are relative to MEDIA_ROOT. Migration 0002 converts existing absolute paths within the configured media root. Deploy/migrate before relocating the volume. Unknown historical absolute roots remain untouched and inaccessible until an operator reviews and relocates them; never blindly rebase an untrusted stored path. Path traversal and symlinks outside MEDIA_ROOT are rejected.

The daily privacy retention task removes raw CSVs after INSTITUTE_SOURCE_FILE_RETENTION_DAYS (default 30), or earlier if institutional/global roster retention is shorter. It clears file metadata while retaining structured rows until their existing retention deadline. Confirm Celery beat/general worker are running; back up and apply the retention policy intentionally. No existing production files were deleted during development.

At the reverse proxy set an upload body limit appropriate to the largest supported endpoint. For the roster upload route, 6 MiB allows the 5 MiB file plus multipart fields. Django's application checks remain mandatory regardless of proxy configuration.

## Portal transport

University, Institute and Superuser clients have identical policies except PORTAL. API origins are normalized, Axios cancellation remains recognizable, password recovery never triggers token refresh, and server/network errors expose a generic message with no raw body/details/original error. Validation messages and request IDs remain available. Each portal runs the same regression tests.

## Verification

Backend: institutes_list.test_security, institutes_list.test_claim_otp_delivery, url_discovery.test_security, accounts.test_secret_configuration, existing claim/privacy/knowledge suites, plus complete/shuffled PostgreSQL CI. Portals: tests/components/apiSecurity.test.jsx and existing browser journeys. These are deterministic adversarial fixtures, not requests to metadata services or private networks.


## Medium-severity follow-up

Claim start always returns the same 200 placeholder/message, including unknown or consumed invitations and queue/cache failures. Internal logs retain delivery failures; users can retry after the usual throttle. No email address or domain is inferred from stored roster data. Wrong/unknown/expired/exhausted codes share the same 400 response. Rate-limit responses still return 429 based on caller traffic. This removes the status/body membership oracle; it is not a claim of constant-time network behavior.

Verification reserves attempts with a conditional SQL increment inside a row-locked transaction. Confirmation has separate IP/session throttles and locks the row through profile creation/claim consumption. PostgreSQL parallel-request tests verify five maximum guesses and one successful confirmation. The dead synchronous start handler is removed.

Production defaults enable HTTPS redirects, secure session cookies and one-hour HSTS. Only the non-sensitive health URL is exempt from redirect for container health probes. Forwarded HTTPS is disabled unless `DJANGO_TRUSTED_PROXY_CIDRS` lists the actual connecting proxy IP/CIDR; middleware removes forwarding headers from every other peer. The proxy must overwrite incoming forwarding headers, and Gunicorn must stay private. See [Django's proxy-header guidance](https://docs.djangoproject.com/en/5.2/ref/settings/#secure-proxy-ssl-header). Keep local development DEBUG=true; the CI HTTP tests explicitly disable redirects and separately run production deployment checks. HSTS subdomain/preload settings remain deliberate deployment opt-ins; the deployment CI tests those opt-ins without changing production DNS.

The database password no longer has a built-in value. Compose still requires a non-empty password. CI generates both encryption keys per run. Historical CI keys remain in Git history and must never be used for a deployment. If a deployment copied one, rotate it using the existing encrypted-data key-rotation procedure; no deployed keys were changed here.

Python `requirements.in` expresses direct dependencies and `requirements.txt` is the complete Python 3.11 lock with hashes. Docker/CI install with `--require-hashes`; CI regenerates without upgrading existing pins and fails on drift. Update intentionally with uv 0.12.15, `uv pip compile requirements.in --python-version 3.11 --generate-hashes --no-strip-extras --upgrade --output-file requirements.txt`, then regenerate without `--upgrade` to retain the canonical header, review the diff, audit and run all gates. Pytest is removed from the application dependencies; the suite uses Django's runner.
