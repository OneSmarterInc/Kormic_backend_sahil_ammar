# Required testing matrix (section 11)

These checks are release gates, not a claim that mocked providers prove production delivery. `Full baseline CI` runs the entire backend suite with PostgreSQL/pgvector and Redis, then reruns it in shuffled order. `REQUIRE_POSTGRES_CHECKPOINT_TEST=true` makes an incorrect SQLite setup fail the checkpoint gate rather than silently skip it. All four clients run their complete unit suites; web portals run Playwright against the built app, and the student job exports Android/iOS/web before running its browser journeys. No gate uses `continue-on-error`.

## Backend coverage

| Requirement | Executable coverage |
|---|---|
| Registration, login, TOTP, backup codes | `accounts.tests.AuthFlowTests`, `accounts.test_totp_security`, `accounts.test_web_auth` |
| Password reset, code expiry/reuse, session revocation | `accounts.tests.ForgotPasswordFlowTests` |
| Student and university ownership | `django_api.tests.OwnershipTests`, `universities.test_staff` |
| Institute ownership | `institutes_list.tests.ClaimFlowTests` |
| Superuser role isolation and first-account bootstrap | `project_superuser.tests.SuperuserAccessTests` and `make_superuser_client` (real management command and TOTP) |
| Claim OTP attempts/rate limits and replay | `institutes_list.tests.ClaimFlowTests`, `ClaimRateLimitTests`, `institutes_list.test_claim_otp_delivery` |
| Claim → register canonical identity | `accounts.test_required_journeys.RequiredStudentJourneys.test_claim_then_registration_reload_preserves_single_identity_and_correction` |
| Register → claim canonical identity | `accounts.test_required_journeys.RequiredStudentJourneys.test_registration_then_claim_keeps_student_values_and_records_correction` |
| BasicInfo persistence/reload | `accounts.test_basic_info_persistence`, `accounts.test_required_journeys` |
| Resume/GitHub/LinkedIn onboarding | `accounts.tests.AuthFlowTests`, `accounts.test_onboarding_preferences`, `django_api.tests.SubResourceHistoryTests` |
| Authoritative verification and private items | `verification.tests.VerificationStatusGates` |
| University escalation/routing | `agents.tests.UniversityAgentEscalationRoutingTests`, `notifications.tests.PendingQueryResolutionNotificationTests` |
| Knowledge persistence/retrieval | `knowledge.tests`, `universities.tests.ManualKnowledgeFactGroupTaggingTests` |
| Checkpoint persistence across workers/restart | `pure_multi_agent.test_checkpoint_persistence`: real independent PostgreSQL pools, close/reopen, rebuild graphs, preserve history, isolate and delete threads |
| Notification registration/polling/logout | `notifications.tests`, `accounts.test_required_journeys` |
| Superuser provision/recovery/audit/telemetry | `project_superuser.tests`, `django_api.test_telemetry` |

## Client journeys

The browser suites exercise production components and network transports with explicit stateful API fixtures. Unknown requests fail; expected request bodies and displayed responses are asserted. Real backend authentication, ownership, single-profile counts, correction provenance, OTP signatures and database writes are separately asserted above. A browser fixture cannot itself prove these server properties.

| Client | Mandatory automated journeys |
|---|---|
| Student | `e2e/studentJourneys.spec.ts`: Welcome → BasicInfo/register → TOTP → reload → source skips → CV upload → Profile ready → Agent Live → queued chat; invitation token → OTP → corrected prefill → confirm → password/TOTP → reload; returning login/TOTP → restore/refresh → history/profile → logout |
| Student native notification boundary | `__tests__/nativeNotificationJourney.test.ts`: actual service registration, simulated Expo tap callback, recognized chat event, listener cleanup, unregister. `navigation.test.tsx` proves protected chat destination; `sessionFeature.test.tsx` covers cold-start notification routing and logout cleanup. |
| Superuser | `tests/e2e/auth.spec.js`, `management.spec.js`, `requiredJourneys.spec.js`: login/TOTP/reload, university/institute/student creation, password reset, remove TOTP, revoke sessions, audit and model telemetry |
| Institute | `tests/e2e/auth.spec.js`, `management.spec.js`: login/TOTP, CSV validation/rejected rows, roster, bulk invite, one-student resend, foreign-roster denial |
| University | `tests/e2e/auth.spec.js`, `management.spec.js`, `requiredJourneys.spec.js`: login/TOTP, saved profile, source URLs, discovery/scrape polling, KB CRUD, groups/routing contacts, interested student, query answer |

Claim links themselves have strict parser/cold-start tests in `claimLinks.test.ts` and `sessionFeature.test.tsx`; browser invitation entry uses the token-entry UI because exported local web URLs are not registered production app links. Optional roster columns may be empty; international phones require a country code when supplied. `MM/YYYY` graduation prefills are displayed as years without silently rewriting the original value.

## Running the gates

Backend: configure the documented test PostgreSQL/Redis services, then `python manage.py test --verbosity 1` and `python manage.py test --shuffle 20260917 --verbosity 1`. For only the new backend gates: `python manage.py test accounts.test_required_journeys verification.tests pure_multi_agent.test_checkpoint_persistence`.

Student: `npm ci`, `npm run lint`, `npm run typecheck`, `npm test`, `npx playwright install chromium`, `npx expo export --platform all`, then `RUN_APP_SMOKE=true npx playwright test`. Each administrative portal: `npm ci`, `npm run lint`, `npm test`, `npx playwright install chromium`, `npm run test:e2e`.

## Deployment acceptance still required

Before distributing a signed mobile release, test one Android and one iOS physical device with the staging backend and real Expo credentials: register token, send `agent_initiated` notification, tap in foreground/background/terminated states, verify the signed-in student's chat, log out, and confirm the token is inactive server-side. Record OS/app/backend revisions and receipt. Also exercise the registered HTTPS claim link on both platforms. CI's simulated Expo callback is not evidence of OS push delivery or app-link association.

Run a staging browser-to-live-backend smoke for each role after deploying the five compatible revisions. The automated browser contract fixtures do not contact live email, GitHub OAuth, Anthropic/Voyage or university websites. Provider credentials, real network delivery and production data must never be required by pull-request tests.
