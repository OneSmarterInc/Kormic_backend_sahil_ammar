# Onboarding environments

Localhost and a Cloudflare tunnel may use the same backend database. Set
`GITHUB_OAUTH_REDIRECT_URI` to that tunnel's HTTPS origin followed by
`/api/auth/github/callback/`. Configure the **same complete URL** in the GitHub
OAuth application's registered redirect URIs (exact matching; no wildcard). A temporary tunnel URL changes
when the tunnel is recreated, so both settings must then be updated.

A separately deployed `backend.kormic.ai` keeps its
`https://backend.kormic.ai/api/auth/github/callback/` callback. GitHub's current
OAuth app settings support multiple redirect URIs: the app owner can add the
local tunnel callback without removing the production callback. Separate
development and production OAuth apps are also an option for credential isolation.
Changing the local `.env` does not register a redirect URI with GitHub; the app
owner must save it in GitHub settings before connections will work. Remove old
temporary tunnel redirect URIs when they are no longer used.
Do not start OAuth on the local database and complete it on that production
database. All workers of one environment must share the same database.

Apply `python manage.py migrate` before deploying the OAuth state change.
States are stored as hashes in the database, expire after ten minutes, and can
be consumed only once. Cache resets no longer invalidate active connections.

LinkedIn extraction is adapted from `OneSmarterInc/Linkedin_agent`, commit
`01e64eaf39198e64b42b9809347ee4fcae3255eb`. Install requirements on each backend.
Local RapidOCR is used if a Tesseract executable is unavailable. Qwen 3 1.7B
receives OCR text; Claude is the fallback. Supported facts, source evidence,
action traces and warnings are saved in the owner-scoped LinkedIn analysis.
Partial extraction warnings are retained; empty results never overwrite data.
LinkedIn evidence does not overwrite the main résumé profile.

Résumé onboarding supports PDF and DOCX. Convert legacy DOC files first.
Text-bearing PDFs use local text extraction followed by Qwen; scanned inputs
may require the vision-capable fallback.
