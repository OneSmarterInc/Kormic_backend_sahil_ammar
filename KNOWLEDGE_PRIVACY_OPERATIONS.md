# Knowledge, model telemetry and privacy operations

This is the operational contract for changes 10.1–10.5. See [SYSTEM.md](SYSTEM.md), [DEPLOYMENT.md](DEPLOYMENT.md) and [CHAT_OPERATIONS.md](CHAT_OPERATIONS.md) for the application/environment matrix and existing chat limits.

## Deployment

1. Back up PostgreSQL and test restoration. Compose and CI now use `pgvector/pgvector:pg16` (PostgreSQL 16). Managed databases must have the `vector` extension available; the migration role must be allowed to create it. Apply migrations before starting the updated web/worker processes. Do not change PostgreSQL major versions against an existing volume.
2. Set `VOYAGE_API_KEY` in the backend secret manager. `KNOWLEDGE_EMBEDDING_MODEL` defaults to `voyage-4-lite` with 1,024 dimensions. Do not change dimensions without a schema migration. Model changes invalidate the previous model's index until re-embedded. University public knowledge and queries are sent to this embedding provider; include this processor in the published privacy policy.
3. Configure `MODEL_COST_RATES_JSON` with the current per-million input/output token rates for each exact model identifier you use. Example shape: `{"model-id":{"input":1.0,"output":2.0}}` (illustrative rates, not provider prices). An unconfigured price is **unknown**, never zero. Conservative chat quota reservations remain separate and continue to apply even after provider timeouts.
4. Run the existing Celery default worker, chat worker and **one** Beat scheduler. Default-worker jobs discover/recrawl sources every five minutes, embed up to 64 chunks each minute, retry deletion requests each minute, and run retention daily. Shared Redis enforces origin rate limits. PostgreSQL stores source leases and deletion state.
5. Inspect `/api/v1/university-admin/knowledge-sources/` as the university owner. The Knowledge Base page shows index readiness and source health and can queue recrawls. Previously scraped facts without successful freshness records are withheld until recrawled. Human-verified/manual/seed facts remain available; their provenance is retained. Do not advertise semantic retrieval as active until indexed chunks exist.
6. Inspect Superuser → Agent telemetry for model counts, known token usage/cost, unknown usage/pricing, tool calls, university fan-out, and chat p50/p95/p99 latency. Unknown usage and unpriced models must be investigated, not treated as free calls. Sample real staging traffic before relying on latency percentiles.

## Retrieval and citations

Knowledge chunks retain their entry, source URL, department, source type and content hash. Lexical relevance and PostgreSQL cosine vector ranks are fused using reciprocal-rank fusion, then weighted by existing source authority, confidence and source freshness. University IDs constrain both retrieval paths. Usage/popularity alone cannot make an unrelated fact relevant. Embedding outages fall back to lexical retrieval; chat budgets still unwind the whole turn.

Human-verified exact/phrase answers keep precedence. Generated answers cite only stored IDs selected from retrieved evidence; invented IDs/URLs are discarded. University chat returns `answer` (and the compatible `reply` field), `confidence`, `sources`, `last_verified_at`, and `human_verified`. Dates may be null when there is no recorded human verification. `sources` include provenance, URL, human-review status, fetch time and verification time. A generated answer citing a human-reviewed source is **not** itself labeled human-verified. The history stores the same citation metadata. Student university tools receive these references with their answers.

The crawler hashes normalized page text, records fetch/success/change timestamps, and atomically replaces only machine-scraped facts when a page changes. Human-reviewed entries are preserved. A changed page awaiting extraction, a robots denial, disabled source, deleted page (404/410), or stale source cannot contribute scraped evidence. The default stale threshold is 30 days and recrawl interval is 24 hours; owner-only API settings can shorten them for admissions deadlines. Fetch errors preserve recent evidence only until that threshold.

Crawler requests use a named bot, robots rules/crawl delays, shared origin rate limits, Retry-After, bounded redirects/body size/timeouts, public-address validation and pinned connections to prevent DNS rebinding. Cross-origin redirects require source reapproval. Inspect `health`, `http_status`, `failures`, `last_success_at` and overdue `next_fetch_at`; a failed worker must not silently be mistaken for fresh knowledge.

## Data lifecycle

Defaults are application retention limits, not a claim of legal compliance. Review them before enabling the new scheduled cleanup in a deployment. Platform administrators manage defaults at `/api/v1/auth/privacy/retention/`; university owners can shorten their transcript retention and institutes can shorten roster retention. Cross-tenant policy edits and extending platform limits are rejected. Relevant portals include retention controls.

| Data | Default retention | Account deletion |
| --- | --- | --- |
| Resumes, LinkedIn screenshots/analyses | 365 days | Files and rows erased |
| Profile images | 365 days since profile update | Current and superseded per-student files erased |
| Chat transcripts and attachments | 180 days | Files and rows erased |
| AI conversation checkpoints, derived memory | At most transcript/memory retention; reset when old turns expire | Checkpoint, long-term memory, intake and conversation insights erased |
| Verification checks/items | 365 days; earlier when source evidence expires | Erased |
| Notification tokens/logs | 90 days since token update / log creation | Erased with account |
| Institute source rosters | 365 days, or shorter institute policy | Student rows erased; any containing verbatim roster file removed, other structured rows preserved |
| Model telemetry | 90 days | Student-linked calls/jobs erased |
| GitHub OAuth grant | Active account lifetime | Provider authorization revoked before completion, encrypted local grant erased |
| Profile and remaining derived analyses | Account lifetime; inactive account deletion after 730 days | Erased with account |

Authenticated requests update a measured activity clock at most every five minutes. Inactive account cleanup only applies when that clock exists; legacy accounts are not guessed inactive from JWT `last_login`. Completing an old account's first new request starts its measured retention clock.

Student Profile → Data and privacy supports password-confirmed export and permanent deletion. Export is an authenticated ZIP of that student's records and files (512 MiB self-service source-file limit); it excludes credentials, push tokens, other students' data and full roster files. Larger exports need a support-managed export. Exports are temporary server files, responses use no-store, and the mobile sharing copy is removed afterwards. Saved copies are controlled by the student.

Deletion immediately disables login, blacklists refresh tokens and cancels queued generations. A ten-minute drain interval allows existing bounded workers to finish. The durable deletion worker then revokes GitHub authorization, deletes the LangGraph checkpoint and files, removes related records including string-linked legacy records, and erases the account. Provider/filesystem/checkpoint failure leaves `retry_pending` and retries after one hour; it never reports successful erasure prematurely. Monitor non-completed `StudentDeletion` rows with overdue `not_before`, plus worker/Beat health. A random receipt can be checked at `/api/v1/auth/privacy/deletions/<receipt>/` without returning student data; completed receipts expire after 90 days. Do not log receipts or passwords.

AI memory has an independent reset clock, so editing a profile or clearing visible chat history cannot keep older memory indefinitely. Expiry resets the whole checkpoint and derived memory; this can also remove newer context. Existing reset clocks begin at migration; expired transcripts can trigger an earlier reset.

The old presenter/question logs were keyed by names; deletion conservatively removes matching entries because stronger attribution is unavailable. Institution source files are removed wholesale when they contain a deleted student's row. Keep these limitations in support guidance. Source facts originating from a student's escalated question are removed with that question; reviewed public university facts remain public institutional knowledge.

Backup snapshots, externally delivered email/push payloads, provider-side logs and student-downloaded exports are outside database/file deletion. Configure backup/log expiry and provider retention separately, document them in the published privacy policy, and replay deletion requests before serving any restored backup. Application completion refers to live application storage and confirmed GitHub revocation, not erasure from external backups. Institutional contracts and privacy notices must describe these boundaries.

## Verification

`python manage.py test knowledge accounts.test_privacy django_api.test_telemetry` covers citations, source changes/failures, authority and tenant isolation, ZIP isolation, erasure retries, memory retention and telemetry access. The pgvector semantic test runs on PostgreSQL in CI (skipped on SQLite). Full baseline CI also checks migrations and runs all backend tests in normal and shuffled order. Frontends run their existing tests/lint/build gates; mobile CI additionally exports platforms and exercises browser authentication/navigation.

New clients use `/api/v1/`; `/api/` remains a compatibility alias. See [API version policy](API_VERSIONING.md).
