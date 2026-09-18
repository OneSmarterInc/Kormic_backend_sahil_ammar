# Kormic system guide

## Repository ownership

| Repository | Responsibility |
| --- | --- |
| Kormic_backend_sahil_ammar | Django API, data, authentication, verification, AI workers and policy |
| Kormic_frontend_student_sahil_ammar | Expo student app: claims, onboarding, profile, chat and notifications |
| Kormic_frontend_University_sahil_ammar | Destination-university administration, departmental knowledge and staff |
| Kormic_frontend_Institutes_sahil_ammar | Partner-institute student imports and claim invitations |
| Kormic_frontend_Superuser_sahil_ammar | Platform provisioning and administration |

A university owns knowledge and AI services; an institute supplies student lists. Their identities and permissions are separate.

## Environment matrix and generation

`deploy/environments.json` is the source of truth for public origins. No application secrets belong there. Run from the backend checkout with all five repositories checked out as siblings:

```sh
python scripts/configure_environment.py local --root ..
```

The generator writes URL-only `.env.local` files to frontends and `.env.urls` to the backend. Backend settings load `.env.urls`; keep secrets in `.env` or the deployment secret manager. Manually maintained files are not overwritten. Frontend public environment values are embedded at build time; rebuild after changes.

| Environment | API origin | Student/app link | Superuser | University | Institute |
| --- | --- | --- | --- | --- | --- |
| Local | http://localhost:8000 | http://localhost:8081; native scheme `kormicstudent://` | http://localhost:5173 | http://localhost:5174 | http://localhost:5175 |
| Staging | KORMIC_STAGING_API_ORIGIN | KORMIC_STAGING_STUDENT_ORIGIN | KORMIC_STAGING_SUPERUSER_ORIGIN | KORMIC_STAGING_UNIVERSITY_ORIGIN | KORMIC_STAGING_INSTITUTE_ORIGIN |
| Production | https://backend.kormic.ai | https://app.kormic.ai | KORMIC_PRODUCTION_SUPERUSER_ORIGIN | KORMIC_PRODUCTION_UNIVERSITY_ORIGIN | KORMIC_PRODUCTION_INSTITUTE_ORIGIN |

Export the named environment variables with the actual deployed HTTPS origins before running the generator with `staging` or `production`. These variables are deployment inputs, not claims that domains have been provisioned. HTTPS is required outside local. The generator also derives backend CORS/CSRF origins, GitHub callback, and claim links. On a physical device replace loopback in an explicit local matrix (`--matrix`) with the reachable development host. Use the same host spelling for local browser and API cookies.

## Authentication and API errors

Web portals and the student web build use an HttpOnly refresh cookie and a CSRF bootstrap token from `/api/auth/web/csrf/`; native uses secure token storage. TOTP gates protected APIs. Follow [WEB_AUTH.md](WEB_AUTH.md) for cookie, origin, and reverse-proxy configuration.

HTTP status indicates success or failure. Every `/api/` HTTP error has this wire shape, including validation, permission, throttling, and Django middleware failures:

```json
{"error":{"code":"PROFILE_NOT_FOUND","message":"Profile not found.","details":{},"request_id":"server-generated-uuid"}}
```

`X-Request-ID` matches `error.request_id`. Field validation errors appear in `details`; non-field validation appears in `details.non_field_errors`. Existing explicit codes such as chat quota/timeout codes are retained. Unhandled errors return a generic message and never serialize exception text. Success payloads are unchanged. Clients consume `code`, `message`, `details`, and `request_id`; legacy parsing remains during coordinated rollout.

## Student navigation and onboarding

React Navigation native stack owns history and Android back behavior. Guest, TOTP and student screen groups are keyed by authentication phase/account, so logout and account switches remove inaccessible history. Claim links use the validated claim adapter; notification destinations go through the same guarded navigation controller and wait for authentication/TOTP. The reducer stores form/domain data and an active-screen snapshot only.

Route restoration stores only version, user ID and a safe Profile/BotScreen destination in AsyncStorage. It never persists tokens, claim codes, reset state, form data or arbitrary navigation parameters; logout clears it. Existing session restoration and claim-link precedence remain authoritative.

`PATCH /api/auth/onboarding/preferences/` accepts `github_onboarding_state` and/or `linkedin_onboarding_state` with `skipped` or `required`. Preferences belong to the authenticated student and survive sign-in/device changes. Login and `/api/auth/me/` onboarding payloads derive `connected`/`uploaded` from actual source data, taking precedence over skipped preferences. A client cannot claim a connection by writing preference metadata. Skipping does not verify a profile; verification UI uses the backend verification result. Onboarding may finish with sources skipped while still showing missing sources.

## University staff

Each staff member has a separate login and authenticator. Existing university accounts migrate to Owner/Admin to preserve access. New staff must be given an explicit role:

| Role | API scope |
| --- | --- |
| Owner/Admin (`owner`) | University administration, staff, all knowledge groups |
| Admissions (`admissions`) | Admissions knowledge and escalations |
| International Office (`international`) | International knowledge and escalations |
| Financial Aid (`financial_aid`) | Money knowledge and escalations |
| Campus Life (`campus_life`) | Campus-life knowledge and escalations |
| Viewer/Auditor (`viewer`) | Read-only university APIs; no staff administration |

Owners use **University staff** in the Staff navigation section to create individual accounts, assign roles and deactivate access. Department users and viewers land in **Department workspace**. Authorization is enforced on the server, including group ownership and tenant boundaries; hiding navigation is only a usability measure.

`GET/POST /api/university-admin/staff/` lists/creates staff. `PATCH /api/university-admin/staff/{user_id}/` changes role or active status. Creation requires a validated initial password, never returns it, and does not mark TOTP enrolled. Deliver that password securely; this version does not send invitations. At least one active owner must remain. Updates serialize on the university row and recheck the actor's authority; deactivation blacklists refresh tokens and disables authentication. Role changes apply on subsequent requests. `StaffAuditEvent` records actor, subject and non-secret changes. Department staff cannot change escalation email destinations or manage other groups.

## Chat and incomplete features

Student chat submits an asynchronous generation job and polls its status; dedicated Celery `chat_worker` processes the queue with bounded model/turn/tool execution and usage limits. Runtime latency/cost telemetry requires live traffic for meaningful percentiles. See `.env.template` and the chat policy implementation for configurable budgets. The roadmap feature is disabled until its planner is available. Mock liveness is removed; profile preparation does not claim a backend agent construction operation.

## Rollout and verification

1. Back up the database and uploads; configure public origins, secrets, Redis and workers.
2. Stage compatible student binaries and web clients before switching chat to job responses, following [CHAT_OPERATIONS.md](CHAT_OPERATIONS.md). React Navigation adds native dependencies: rebuild native binaries; an OTA JavaScript-only update is insufficient for older installed binaries. Older mobile versions need an upgrade before the chat contract changes.
3. Coordinate the client/backend cutover: apply all migrations, including account onboarding/staff fields and staff audit events, and start the backend and workers. The new durable-skip and staff features require this backend; until it is available, skip saves fail visibly without advancing. Use a maintenance window if the clients and backend cannot be released together.
4. Verify browser CSRF/login/TOTP, claim links, student resume/notification/back behavior, source skip persistence, chat jobs, owner/staff/viewer access and cross-tenant denial in staging.

Run the checks in each repository README. The backend suite tests authorization, canonical errors, preferences and migrations; the student suite covers actual navigator transitions, safe persistence and feature flows. CI performs native/web export and browser checks. Production model latency, provider billing and device notification delivery still require deployment observation. See [DEPLOYMENT.md](DEPLOYMENT.md) for the Compose services and operational commands.
