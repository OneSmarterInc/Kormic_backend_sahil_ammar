# Audit disposition: medium, structural and cleanup findings

This review applies to the sahil_ammar repositories and the existing fix PRs, not unmerged main or an independently deployed service.

| Finding | Decision and implemented response |
|---|---|
| 2.1 | Necessary: uniform claim-start status/body for known, unknown, consumed and delivery failure. |
| 2.2 | Necessary: independent confirmation IP/session throttles, including authenticated callers. |
| 2.3 | Previous security PR already added a row lock. Added a conditional SQL counter and real PostgreSQL concurrent guessing/confirmation tests. |
| 2.4 | The last schema workflow was green: database-free SimpleTestCase suites skip DB setup in the current runner. Added PostgreSQL/Redis services so future integration tests cannot make the workflow silently infrastructure-dependent. |
| 2.5 | Necessary: agent workflow push trigger now main. |
| 2.6 | Necessary: backend serializer-to-snapshot gate, generated client-to-snapshot gate, daily/manual HTTPS deployed-schema comparison. Failures/unavailable schema fail the monitor. Scheduled jobs activate on main after merge; deploy backend contracts before clients. |
| 2.7 | Already replaced ositicketing production examples in earlier fixes. All local examples now use localhost:8000 explicitly; production remains backend.kormic.ai from deploy/environments.json. No claim that DNS/deployed configuration has been changed or verified. |
| 2.8 | Necessary cleanup: removed unused synchronous claim/start. |
| 2.9 | Necessary: Superuser ErrorBoundary with recovery and no production exception dump. |
| 2.10 | Old requests/cryptography pins already updated. Added complete hashed Python lock and drift gate; removed unused pytest application dependency. |
| 2.11 | Necessary: HTTPS/HSTS/secure session cookies, trusted proxy peer boundary, no DB password fallback, spectacular app registration. JSON-only production renderer was already fixed. |
| 2.12 | Necessary: fresh OAuth/TOTP encryption keys every CI run. |
| 2.13 | Prior required-journey browser tests already cover security actions. Added component tests for cancel/confirm, target/password payloads, server rejection and self-account protection. |
| 3.2 | Product decision: careers Person/Corridor/per-claim verification is absent here. These repositories implement the student platform. Cannot infer whether careers exists elsewhere. No invented careers models or healthcare compliance claim. |
| 3.3 | Full TypeScript conversion is optional. Added serializer-derived portal account snapshots, generated JSDoc/runtime validation, blocking generation checks and deployed-schema monitoring. Other portal endpoints remain a documented incremental typing task. |
| Cleanup | READMEs already repaired and role-specific; retained that work. Centralized student permissions, standardized Node 22, guarded null university routes, removed stray comments, moved historical API notes into backend docs. |
| HashRouter/claim | HashRouter is intentional for static portals. The backend app-link service owns /claim and the nginx config forwards that exact route to it. Switching portal routers is unnecessary and does not implement mobile deep links. |

## Architecture and retention boundary

The student platform currently accepts source documents, including institute rosters, and runs profile-level verification. Its existing privacy task removes raw roster files after 30 days (or earlier institutional policy) and keeps structured rows only for their own retention period. This is not the careers spine's source-reference/evidence-hash-only design. Do not reuse this storage/verification model for a healthcare corridor without a separate approved identity, evidence, consent and retention specification. Removing existing intake documents or migrating the identity model is not authorized by a finding that they differ from an unimplemented product architecture.

## Contract workflow

Run `python manage.py export_client_contracts` after serializer changes and commit the backend snapshots. Copy the matching snapshot to each client and regenerate (`npm run api:generate-profile` for student; `npm run api:generate` for portals). The backend gate detects serializer drift; client gates detect generated drift. Daily `Deployed API contract monitor` checks compare actual deployed schema types, required fields, nullability, formats and bounds, ignoring prose. Optional repository variable KORMIC_SCHEMA_URL selects another HTTPS schema. There is no private cross-repository token requirement. Coordinated unreleased changes can pass local gates before deployment; a mismatch with deployed clients fails the daily monitor once it runs. This is scheduled drift detection, not an atomic five-repository release mechanism.

Deployment requirements and security boundaries are in SECURITY_HARDENING.md. No production DNS, secret rotation, retention execution, deployment or branch-protection mutation occurred.
