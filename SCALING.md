# Agent execution and capacity

The student runtime compiles one LangGraph per worker/checkpointer. A session
passes mutable student context through LangGraph Runtime, not through a shared
object or persisted graph configuration. Tool calls within a turn are sequential.
The prompt includes at most 20 complete conversation turns. University execution
uses a fresh lightweight session, versioned cached persona, and bounded retrieval.
It does not load the full knowledge corpus or launch a scraper at construction.

## Request flow

1. Authenticated POSTs to the existing student/university chat endpoints return
   HTTP 202 with `job_id`, `status` and `poll_after_ms` in queued mode.
2. The student UUID or university UUID is derived from server authorization.
   `Idempotency-Key` deduplicates submissions, including after completion.
3. A short PostgreSQL admission transaction caps pending/processing jobs globally
   and a partial unique constraint allows one active job per conversation owner.
4. Celery processes the job on `agent_chat`; Redis leases exclude concurrent
   execution and clear/edit operations. Results and the assistant message commit
   together. Student edit/regeneration is queued too.
5. GET `/api/chat/jobs/{job_id}/` returns a result only to the owning student or
   university officer. `/api/chat/jobs/active/` resumes a pending job after refresh.
   Both frontends poll with capped backoff; they also accept legacy synchronous
   responses in development.

Normal web workers no longer wait for model responses in production. Worker
execution is deliberately synchronous inside independently scalable Celery
processes; this does not require an ASGI rewrite or async ORM/checkpointer mix.
University consultations run inside the student's job and remain synchronous
subcalls, with limited comparison fan-out. They are not separate microservices.

## Recovery and limits

Jobs are saved before broker publication. Beat republishes queued work whose
delivery may have been lost. Duplicate delivery cannot claim an already-running
or completed job. A queued job expires after 900 seconds; worker hard timeout is
600 seconds and the dispatcher marks stranded processing jobs failed after 660.
Redis thread leases expire after 900 seconds, longer than the hard worker limit.
Production requires a Linux prefork worker so hard timeouts actually terminate
work. Do not replace it with a thread/solo pool in production.

Mid-turn failures are not automatically replayed: tools may already have updated
profile data or generated an assessment. This is at-most-once job claiming, not
an exactly-once guarantee for every external side effect. Students can inspect
history and explicitly retry. Notification and scraping services remain separate.

Redis controls model concurrency, model requests/minute, per-owner submissions,
per-university executions and conversation leases. Limits are shared across
replicas. Redis outages fail admission/model execution closed. Lua uses Redis
server time and owner tokens; an expired holder cannot remove a replacement.
Configure request/minute limits below your actual Anthropic allowance. This does
not implement token-per-minute quota accounting; provider 429s still use the
SDK's bounded retry. Measure tokens and adjust concurrency and request budgets.

## Discovery and knowledge

`list_universities(query, country, location)` searches up to ten candidates.
Existing comparison tool names remain compatible, but compare only selected
candidates. `AGENT_MAX_UNIVERSITIES` caps distinct university contacts across all
tool calls in one student turn. Default: five. Search by text does not establish
eligibility or affordability. The existing schema has structured contacts,
location and eligibility criteria; it does not have normalized tuition/program
tables, so the runtime does not invent SQL budget filters.

Knowledge saves, deletes, queryset updates and bulk creates increment a durable
per-university indexing revision. Beat sends dirty revisions to `knowledge_index`.
Edits during indexing leave a later revision pending. Changed text invalidates
old vectors immediately. Chat only embeds the question and reads stored vectors;
it never creates document embeddings. Existing data is marked dirty by migration.
Raw SQL imports must explicitly mark `KnowledgeIndexWork.revision` dirty or run
`index_university_knowledge`; ORM signals cannot observe external SQL writes.

Migration 0005 adds HNSW cosine and trigram indexes on PostgreSQL. pgvector 0.8+
is needed for iterative scans with tenant filtering. Each search applies the
university scope and bounds lexical candidates to 128 plus semantic hits. Highly
selective tenant filters can affect approximate recall; measure retrieval recall
and query plans on representative data before choosing partitioning.

Persona cache keys contain university UUID and updated_at version. Configurations
are serialized to avoid mutable object sharing. Production uses a separate 128 MB
Redis instance with allkeys-lru; development uses a 128-entry in-process cache.
Broker/lease Redis uses AOF and noeviction. Do not put expiring safety leases in
an evicting cache. Corpus contents and private student records are never cached
as shared university persona configuration.

## Start and scale

Deploy backend and both frontend builds together because production responses are
asynchronous. Set PostgreSQL, Redis and the existing Claude key in the environment.
Compose runs migrations before workers start; otherwise run `python manage.py
migrate` once with an extension-capable database role.

```sh
docker compose up -d --build
docker compose up -d --scale agent_worker=4 --scale index_worker=2
```

Each agent worker starts four execution processes. Global model concurrency
defaults to 16, so replicas share that capacity. The checkpointer pool defaults
to five connections per process; include ORM, background workers and admin traffic
in the PostgreSQL connection budget. Tune pools before increasing process counts.
Scale web replicas behind a load balancer with deployment-specific port mappings;
the development Compose port 8000 is fixed and cannot be bound by multiple web
replicas on one host. Compose uses one persistent PostgreSQL and Redis instance;
production high availability/managed services are an operator deployment concern.

All these controls are environment settings in `.env.template`:

| Setting | Default |
| --- | --- |
| AGENT_QUEUE_CAPACITY | 1000 active/queued jobs |
| AGENT_MODEL_CONCURRENCY | 16 model calls |
| AGENT_MODEL_REQUESTS_PER_MINUTE | 120 calls |
| AGENT_STUDENT_REQUESTS_PER_MINUTE | 10 submissions per owner |
| AGENT_UNIVERSITY_CONCURRENCY | 4 executions per university |
| AGENT_MAX_UNIVERSITIES | 5 distinct universities per turn |

DEBUG mode defaults to synchronous chat for local development without Redis.
Set AGENT_QUEUE_ENABLED=true and AGENT_DISTRIBUTED_LIMITS=true to exercise the
production path locally with workers. Compose explicitly enables both. Queued mode
always disables Celery eager execution so HTTP submission cannot run the job inline.

## Verification and load testing

```sh
pip install -r requirements-scaling-test.txt
python manage.py test pure_multi_agent.test_scaling knowledge.test_vectors
python manage.py benchmark_agent_runtime --students 500 --concurrency 100
python scripts/load_chat.py --base-url https://staging.example/api/ --tokens-file staging-tokens.json --students 500 --concurrency 500 --allow-model-cost
```

The synthetic benchmark checks graph reuse/isolation using a fake model and memory
checkpoints; it is not an end-to-end capacity claim. HTTP load testing requires
distinct authenticated staging student tokens and incurs real model costs.
Use one staging student per token (distinct token strings alone are not sufficient).
Measure admission failures, queue age, model 429s, completion rate, p95 latency,
database connections, CPU/memory, and cost with both ordinary and comparison traffic.
Completion logs include queue and execution duration. Increase load progressively
through 100/250/500/1000 conversations; do not treat configured queue capacity as
proven simultaneous response capacity.

## Agent data retention

Retention is disabled by default. After an operator restores a PostgreSQL backup
to an isolated staging database, validates it, and approves the policy, a nightly
Celery task applies one bounded pass (up to 500 rows per class). The management
command is a dry run unless `--apply` is explicitly supplied. This is an
operational policy, not a claim that a particular legal retention period is
universally required; the data owner must review local audit/privacy duties.

| Data class | Policy | Why |
| --- | --- | --- |
| Completed student chat job execution payload/result | After 90 days, compact only when the exact assistant reply is proven present in the student's visible chat history. Keep job ID, status, idempotency key, message IDs, minimal reply metadata and any single-university follow-up candidate. Do not compact a pending escalation or unpublished university evidence. Legacy jobs without a verified assistant-message link are skipped. | Removes duplicate tool state while retaining polling, retry deduplication and follow-up semantics. Old job status can reconstruct its reply from chat history. |
| GitHub agent checkpoints and pending writes | Delete after the parent sync run has been terminal and unchanged for 30 days. | Recovery uses these only while queued/running. GitHub reports, profile snapshots and run result/status remain available. |
| University chat jobs, CV/LinkedIn upload jobs and GitHub sync run results | Keep the job/result records until their delivery, provenance and notification semantics are separately reviewed. | A blind age-based deletion could break a late result fetch or evidence publication. Their durable profile/chat artefacts are preserved regardless. |
| Chat messages/attachments, CV/LinkedIn analyses, GitHub reports, profile data | No scheduled purge. | Student-visible history and source-backed profile evidence require a separate product/account-deletion policy. |
| Student/university LangGraph conversational checkpoints | No scheduled purge. | The latest state contains turn-to-turn context and institution follow-up state; deleting by age would silently change the next answer. Measure growth and design a checkpoint compactor that retains a restorable latest state before enabling cleanup. |
| Pending or unresolved queries and face enrollment/verification records | No retention job touches these. | Query workflow and biometric/account recovery need separate approved policies. |
| Agent audit logs | Previous 90-day automatic purge is disabled. Keep until audit/legal owners define an export, hold and deletion period. | Logs may be needed to investigate changes or incidents. |

An active `DataRetentionHold` with `subject_key="student:<student UUID>"` blocks
both managed classes for that student; `subject_key="*"` stops all retention.
Set a reason and leave `released_at` null. Release only after review by setting
`released_at`. Holds live in PostgreSQL and are included in backups. Never use
the retention command to bypass a hold.

For example, from `manage.py shell` an operator can create a hold with
`DataRetentionHold.objects.get_or_create(subject_key="student:<UUID>",
defaults={"reason": "Incident review <ticket>"})` after importing
`DataRetentionHold` from `django_api.models`. Use `subject_key="*"` for a
global hold. Do not include personal details in the reason; store a case ID.

Before enabling a production purge:

1. Take a PostgreSQL backup including `django_api_agentjob`,
   `django_api_githubagentcheckpoint`, `django_api_githubagentwrite`,
   `django_api_chatmessage`, `django_api_dataretentionhold`, audit records,
   LangGraph checkpoint tables, and associated schema. Record the backup ID.
2. Restore that backup into an **isolated staging database**. Verify the restore
   exits successfully, compare counts and representative rows for each class,
   check an old job/status response, a student transcript, an active GitHub run,
   an unresolved query and a face enrollment record. Record the restore date,
   operator and backup ID. Retain this evidence outside the database.
3. Review legal holds and data-class windows with the data owner. Apply any
   `DataRetentionHold` rows before cleanup. Run a dry run:

   ```sh
   python manage.py prune_agent_retention --max-rows 500
   ```

4. For a manual bounded pass, use the actual restore-tested backup ID:

   ```sh
   python manage.py prune_agent_retention --apply --policy-approved \
     --restore-tested-backup-id=BACKUP_ID --restore-tested-at=YYYY-MM-DD \
     --max-rows 500
   ```

5. Only after that review, set `AGENT_RETENTION_ENABLED=true`,
   `AGENT_RETENTION_POLICY_APPROVED=true`, and
   `AGENT_RETENTION_RESTORE_TESTED_BACKUP_ID=BACKUP_ID`, and
   `AGENT_RETENTION_RESTORE_TESTED_AT=YYYY-MM-DD` in the worker/beat
   environment. The task stops when the restore test is over 30 days old;
   repeat the restore check and update both values before resuming. Monitor PostgreSQL table/index sizes and
   `pg_stat_user_tables` vacuum statistics; deletion does not immediately shrink
   an on-disk PostgreSQL data file.

Rows that cannot be proved to have a matching visible reply are marked checked
and left intact, so they do not block later batches. They can be reviewed and
rechecked manually after correcting the underlying evidence. The 500-row cap
means a large backlog drains over successive nights.

Implementation references: [LangGraph Runtime](https://reference.langchain.com/python/langgraph/runtime/Runtime),
[Celery task delivery](https://docs.celeryq.dev/en/stable/userguide/tasks.html),
[pgvector indexing/filtering](https://github.com/pgvector/pgvector).

## Local Qwen context profiles

Routing starts at a 4,096-token context with 512 output tokens; general advice
starts at 8,192 with 2,400 output tokens; university and document evidence
starts at 16,384 with at least 2,400 output tokens. These are initial profiles,
not caps on the student's message. The request sizer includes messages, tool
schemas and output space, then expands to the next available window. The
default maximum is `KORMIC_QWEN_MAX_CONTEXT=16384`. Increase it only after
checking the installed model's context capacity and measuring host RAM and
latency; the [Qwen3:0.6b model listing](https://ollama.com/library/qwen3%3A0.6b)
currently advertises a 40K context window.

Ollama's `prompt_eval_count` is checked after each local response. If the prompt
approaches the configured window, the request is retried at a larger context
before any tool call is executed. Requests that cannot fit or whose prompt size
cannot be verified fail explicitly; source text is not silently shortened.
Compare logged `Qwen context` windows and observed prompt counts with idle RAM,
peak RAM, and warm/cold response latency before adjusting the profiles or
maximum. [Ollama's API usage metrics](https://github.com/ollama/ollama/blob/main/docs/api/usage.mdx)
describe `prompt_eval_count` and related timings.

## Shared public research and inference scheduling

Public university collection now uses a durable, institution-scoped job key
containing programme, intake, applicant category and requested topic. One
request owns a five-minute lease; concurrent requests for that exact scope wait
up to 25 seconds, then answer from existing evidence with an explicit gap if
collection is still running. Expired leases can be reclaimed. Successful
results can be reused for one day, and an hourly Celery task removes old job
coordination rows after seven days. The scope and shared collection query omit
student scores, names and conversations. Each student's final answer still
uses their own profile. Apply the `university_research` migrations and restart
the web, worker and beat processes when deploying this change.

University adviser prompts now select up to four relevant prior exchanges
from the recent history. Explicit references to older discussion allow a wider
history lookup before relevance selection; saved conversation history is not
deleted. Existing adviser tools remain selected for the current step.

Inference reservations count message and tool-schema input, the configured
output allowance for the request profile, image/document allowances and a
safety margin. The short routing profile reserves less output; general and
evidence profiles retain their larger output allowance. Truncated Claude
responses are rejected for correction instead of treated as complete.
Admission polling starts at 150 ms and backs off to at most one check per
second during a long wait, preserving the 30-second deadline and current
queue ordering. Compare queue wait time, database queries, truncation rate,
provider usage and answer quality before changing these limits further.
