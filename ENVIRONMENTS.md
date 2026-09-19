# Shared environment matrix (P1-09)

`deploy/environments.json` is the only authored URL matrix. Generate the environment files for
all five repositories with `scripts/configure_environment.py`; do not maintain independent portal
API URLs. Secret values stay outside this matrix. The generator rejects unresolved placeholders,
credentials in URLs, non-origin URLs and non-HTTPS deployed origins.

| Environment | API origin | Student / claim link origin | Superuser | University | Institute |
|---|---|---|---|---|---|
| Local | http://localhost:8000 | http://localhost:8081 | http://localhost:5173 | http://localhost:5174 | http://localhost:5175 |
| Staging | KORMIC_STAGING_API_ORIGIN | KORMIC_STAGING_STUDENT_ORIGIN | KORMIC_STAGING_SUPERUSER_ORIGIN | KORMIC_STAGING_UNIVERSITY_ORIGIN | KORMIC_STAGING_INSTITUTE_ORIGIN |
| Production | https://backend.kormic.ai | https://app.kormic.ai | KORMIC_PRODUCTION_SUPERUSER_ORIGIN | KORMIC_PRODUCTION_UNIVERSITY_ORIGIN | KORMIC_PRODUCTION_INSTITUTE_ORIGIN |

Production API/student values match existing project configuration; this does not certify DNS,
TLS or deployed service health. Staging and production portal hostnames require operator-supplied
HTTPS origins; they are not guessed or provisioned by this change. Student native claim links
continue to use the existing app association/custom scheme configuration.

```sh
python Kormic_backend_sahil_ammar/scripts/configure_environment.py local --root .
```

Run with side-by-side checkouts. The generator writes URL-only `.env.urls` for the backend and
`.env.local` for each frontend, refusing to replace manually maintained files. Backend settings
load `.env.urls` before `.env`; explicit process variables retain precedence. Vite/Expo consume
`.env.local`. For hosted CI, inject the generated values into the corresponding project's build
variables. For EAS, upload the student variables into the selected EAS build environment before
building; the three EAS profiles no longer all point to production.

Portal API values are origins **without `/api`**; the generator adds `/api` only for the student
app. GitHub OAuth redirect, claim URL, allowed hosts and CORS are generated from the same origins.
Register the generated callback URL with GitHub OAuth for each environment. Compose exposes
8000:8000 and the local template uses that same port. For physical mobile devices use a reachable
host via a `--matrix` local override, since the phone's loopback is not the developer machine.

Native staging app-link hosts also require app association files and a matching signed mobile
build (Expo/native domain allowlists); configuring a web claim URL alone does not establish a
verified Android/iOS link. Provision these after choosing the staging domain, or use the staging
web claim flow. Localhost hosts remain allowed for container health checks in every environment.

## Local browser cookies

Use `http://localhost:8081` for the student app and `http://localhost:8000/api` for its API.
Do not mix `localhost` and `127.0.0.1`: browsers treat them as different sites and block
SameSite=Lax authentication cookies. In development, the student app aligns loopback API
hostnames with the browser page; native/LAN and remote API addresses remain explicit.
Restart Expo after changing `.env.local` and reload the browser. Keep cookies enabled.
Production app/API must use HTTPS on the same site, or a same-site API proxy; do not disable CSRF.
