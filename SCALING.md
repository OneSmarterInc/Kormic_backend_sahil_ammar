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

Completed job/result retention is currently explicit (no automatic deletion).
Set an operational retention policy before large-scale deployment; messages and
LangGraph checkpoints also accumulate even though model prompt history is bounded.

Implementation references: [LangGraph Runtime](https://reference.langchain.com/python/langgraph/runtime/Runtime),
[Celery task delivery](https://docs.celeryq.dev/en/stable/userguide/tasks.html),
[pgvector indexing/filtering](https://github.com/pgvector/pgvector).
