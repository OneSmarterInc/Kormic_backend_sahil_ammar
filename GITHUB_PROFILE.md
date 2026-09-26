# Student GitHub LangGraph agent

The student GitHub connection, Analyze action, GitHub Profile tab, names-only
repository pagination and existing assessment contract are retained. The source
project's collection, redaction, evidence limits and overview algorithms are
retained, but source investigation is now a real model-directed agent.

`github_profiles/agent.py` builds a LangGraph `StateGraph`. Its model node chooses
one LangChain `StructuredTool` and arguments. The tool node executes that choice,
records the observation, and loops back to the model until a finding is accepted
or the budget expires. Tools are `list_files`, `read_files`, `read_readme`,
`inspect_contributions`, and `submit_finding`. Invalid arguments/citations return
an observation so the model can correct its choice. Two invalid tool calls switch
subsequent decisions to Claude. These are executable tools, not prompt-only labels.

## Run

Apply migrations and run the persistent worker alongside Django:

```powershell
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
# Separate process:
python manage.py github_worker --concurrency 4
```

On this Windows workspace, the restored Python 3.11 interpreter is
`C:\onesmarter\Kormic\.runtime\python311\python.exe`; it reuses the existing
backend virtualenv packages. Docker Compose includes `github_worker` and starts
it only after migrations finish. No new Python packages are required.

The parent directory's `Run-Kormic.bat` starts the API, worker and frontend without
starting Ollama. The default worker now has four execution slots. `--once` advances
one queue slice, not one entire extraction.

## Concurrency and deployment

Use shared PostgreSQL for multiple application/worker instances. SQLite remains
supported for local development, not a production 100-user deployment. The queue
is in the shared database; no process-local queue is authoritative. Existing
project Redis limits for Anthropic remain in effect as an additional global cap.

100 users can create 100 isolated sessions immediately. Workers claim different
sessions concurrently with atomic compare-and-swap leases. Each slice collects
one API page, one repository's facts, one LangGraph node, or one overview model
batch, then puts that session at the back of the ready queue. At most one worker
owns a given session at once. A large portfolio cannot monopolize a worker through
all its repositories. Busy inference is rescheduled, not sent to a paid fallback.

Leases last ten minutes and renew at progress boundaries; model calls are bounded
below this interval. An expired lease is reclaimed automatically. A fencing token
prevents an old worker from writing after another worker takes over. Resource
failures retry twice with backoff before recording partial coverage; unexpected
worker failures also have bounded retries. Final assessment and completion commit
atomically. Database outages leave resumable leased work rather than discarding it.

LangGraph checkpoints and pending writes use `DjangoSaver` in the same database,
scoped by run/repository/commit/version. A resumed tool step reuses its saved model
decision. A crash between an external model response and its durable checkpoint
can repeat that model request; external inference is not exactly-once. Source
evidence and reports use idempotent unique keys. Keep the latest two checkpoints
per repository; their state retains the tool history. Arbitrary historical time
travel is intentionally not provided.

Initial configurable limits (not a measured throughput guarantee):

| Setting | Default | Scope |
| --- | --- | --- |
| `GITHUB_WORKER_CONCURRENCY` | 4 | Per worker process; CLI can override |
| `GITHUB_QWEN_CONCURRENCY` | 1 | Shared across all workers |
| `GITHUB_CLAUDE_CONCURRENCY` | 4 | Shared across all workers |
| `GITHUB_QWEN_RPM` / `GITHUB_CLAUDE_RPM` | 60 / 40 | Shared requests per minute |
| `GITHUB_QWEN_TPM` / `GITHUB_CLAUDE_TPM` | 300000 / 150000 | Shared estimated token reservations per minute |
| `GITHUB_AGENT_MAX_STEPS` | 12 | Model decisions per repository |
| `GITHUB_RUN_MAX_MODEL_CALLS` | 400 | Provider calls per extraction |
| `GITHUB_RUN_MAX_TOKENS` | 2000000 | Estimated token reservations per extraction |
| `GITHUB_RUN_MAX_SECONDS` | 86400 | Session lifetime including queue waits |
| `GITHUB_DAILY_SYNC_LIMIT` | 20 | New runs per connected profile in 24 hours |

Token reservations conservatively estimate input from character count and reserve
maximum output; they are admission budgets, not billed usage measurements. Set
provider limits to your actual account entitlement and hardware. Worker count is
independent of model capacity; adding workers alone will not accelerate one GPU.
For example, `docker compose up -d --scale github_worker=2` starts two worker
instances sharing PostgreSQL and the configured model limits. Use the existing
production web service, not Django's development server, for production traffic.

## Model selection

```dotenv
GITHUB_OLLAMA_BASE_URL=http://127.0.0.1:11434
GITHUB_OLLAMA_MODEL=qwen3:1.7b
GITHUB_OLLAMA_TIMEOUT=180
```

Install/start Qwen in Ollama separately to use local inference. Loopback is allowed
by default. For a shared private model server, set `GITHUB_OLLAMA_BASE_URL` and add
its exact hostname to `GITHUB_OLLAMA_ALLOWED_HOSTS`; protect it using your private
network and use HTTPS where appropriate. Model URLs cannot come from user input.
Unreachable Ollama, missing models,
timeouts and invalid JSON/schema responses fall back to the project's existing
Claude Haiku client and `ANTHROPIC_API_KEY`. Fallback may send the redacted sampled
source excerpts to Claude, as requested. No OAuth tokens are sent to either model.
Claude uses a forced structured tool response and both providers' output is schema
validated. Qwen failures open a shared 60-second cooldown. A busy model or exhausted
rate allowance queues the existing graph step rather than creating a fallback burst.

Provider/model provenance is stored on every source report. If neither model can
respond, profile facts, repository records, collected excerpts and the factual
overview remain saved with warnings. A later sync retries missing analyses.

## Collection and storage

- Only the student's own OAuth token is used. Expired/revoked access requires
  reconnecting; there is no fallback to a shared token or another identity.
- Every `/user/repos` page is collected. Existing OAuth scopes are retained:
  private repositories are available only if already granted to that token.
- Repository metadata, language bytes, redacted README (12,000 characters),
  accessible organizations and up to 100 recent events are persisted.
- Source analysis samples at most 10 eligible files and 18,000 characters per
  repository, pinned to a commit SHA. Generated/dependency paths, `.env`, key
  files and known token patterns are excluded/redacted; no code is executed.
- Findings are schema-validated; submissions with nonexistent source IDs are rejected
  and returned to the agent for correction. Citation checks establish that a cited
  excerpt exists, not that every semantic claim has been independently proven.
  Commit metadata is a sample of up to 10 account-linked commits, not proof of
  authorship or a personal skill rating.
- Reports are cached by repository, commit SHA and analysis version. Revoked or
  removed repos are retired only after a complete repository listing. Incomplete
  listings retain prior facts and record a warning.

`GitHubProfileSnapshot` belongs to both the student and the OAuth connection.
`GitHubRepository` has a unique GitHub ID per profile, typed ownership/visibility
columns, metadata and collection fields. `GitHubSourceEvidence` stores unique
repository/SHA/path excerpts and line links. `GitHubRepositoryReport` stores the
validated finding, coverage, contribution sample and inference provenance.
`GitHubSyncRun` tracks stage, fair scheduling time, lease, budgets and outcomes.
`GitHubAgentCheckpoint`/`GitHubAgentWrite` store resumable graph state/tool history.
`GitHubModelPool`/`GitHubModelSlot` enforce shared provider admission and cooldowns.
Deleting the OAuth
connection cascades these new records. The existing `GitHubAnalysis` history and
student assessment/evidence fields are still populated for existing consumers.

## API and UI

All endpoints require the existing student authentication and TOTP checks. The
student/connection are resolved server-side; callers cannot choose another owner.

| Endpoint | Behaviour |
| --- | --- |
| `POST /api/profile/github/` | Queue/deduplicate extraction; HTTP 202 and job ID |
| `GET /api/profile/github/jobs/{id}/` | Own job's progress and existing analysis result on completion |
| `GET /api/profile/github/overview/` | Identity, aggregate overview, technologies, languages and coverage; excludes repository descriptions and source bodies |
| `GET /api/profile/github/repos/?page=1` | Names only, exactly 10 per page except the final page; deterministic order |

The frontend's `analyzeGithub` wrapper continues to resolve the original analysis
shape after polling, preserving onboarding behaviour. The new **GitHub Profile**
menu item restores active progress on refresh, provides Sync and Refresh, and
contains a collapsed **Repos** section. The student app uses the source's cream,
sage and green theme while retaining existing navigation and layout.

## Verification

```powershell
python manage.py test github_profiles --noinput
# From frontend/apps/student:
node node_modules/typescript/bin/tsc --noEmit
node node_modules/jest/bin/jest.js --runInBand
```

Tests cover real LangGraph execution with controlled model responses, different
tool paths, rejected evidence/correction, checkpoint resume without repeating a
completed decision, step budgets, stale-worker fencing, concurrent claims, shared
model/rate limits, 100 independent queued sessions, Qwen/Claude routing, collection,
cache reuse, complete extractions for eight users across four concurrent workers,
resumable multi-batch overviews, database persistence and ownership boundaries. Network/model responses
are mocked in that suite. A separate disposable synthetic-repository smoke check
has exercised the live Claude fallback and actual tool loop. Live Qwen and a
100-user PostgreSQL/GPU throughput test require the target deployment hardware;
the scheduler tests do not establish completion times for 100 real portfolios.
