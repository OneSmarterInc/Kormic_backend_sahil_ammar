# Shared-model agent architecture

Two admission stages keep workflow concurrency separate from GPU concurrency:

1. API stores an idempotent AgentJob and user message, then returns 202.
2. Workflow workers select tenants round-robin, admitting at most two workflows per tenant. A university and all its presenter chats are one tenant; conversation locks remain separate.
3. Each individual model call requests shared inference admission. Within priority classes the least recently served tenant goes first. A tenant holds at most one inference slot. Old waiting requests eventually precede newer higher-priority work.
4. Qwen has two shared slots. Background GitHub/research calls hold at most one. Tools execute outside these slots.
5. LangGraph saves synchronous checkpoints. Workers heartbeat and use execution tokens to fence stale results. If a worker disappears in a model node, its saved turn context resumes without appending another human message. Capacity yields also resume saved state.
6. A crash during a mutation tool is deliberately not replayed automatically: its effects may already have committed. The job is marked failed with a review message. External side effects cannot be promised exactly once without idempotent downstream APIs.

Local operation uses the existing SQLite database and database workers. Run-Kormic.bat starts the workers; Run-Qwen.bat separately starts the two-slot Ollama service. No APK build is needed for the backend queue.

## PostgreSQL / Redis deployment

docker-compose.yml supplies pgvector PostgreSQL, persistent Redis, Celery chat/index workers, ingestion workers and migrations. It explicitly overrides local database-queue settings with Redis/Celery and disables Django debug mode. PostgreSQL also holds LangGraph checkpoints. Configure POSTGRES_PASSWORD and existing application secrets in the backend environment before running `docker compose config --quiet`, then `docker compose up -d --build` on a Docker-capable host. Verify the host Ollama endpoint is reachable from containers; keep inference private.

This machine currently has no Docker engine or installed PostgreSQL/Redis services. The production stack has not been launched, and live SQLite data has not been migrated. Before production cutover: back up SQLite/uploads/checkpoints, rehearse data transfer into an isolated PostgreSQL database, verify row counts and authentication, then switch traffic. Do not point an empty production database at current users.

## Validation and operations

`python manage.py test pure_multi_agent.test_inference_admission pure_multi_agent.test_job_recovery pure_multi_agent.test_qwen_routing`

`python manage.py benchmark_ai` sends ten synthetic Qwen requests with no real student data or Claude fallback. Log entries separate inference wait from execution time. Job polling includes recovery_attempts. Check chat-worker.err.log for worker failures and monitor queue length, oldest queued age, provider failures and p95 queue time before increasing concurrency.

Résumé and LinkedIn uploads opt into a dedicated agent_documents queue using Prefer: respond-async. Updated mobile/web source polls the authorized job endpoint. Older installed clients retain synchronous responses. A separate local document worker uses two workflow slots; extraction calls still compete fairly for the same model capacity. Successful jobs clean staged inputs, preserve normal source history and do not create chat messages.

Queued-work durability is implemented. Recovery of uncertain external writes and a production PostgreSQL/Redis soak test remain separate rollout work; this is not a claim of fully automatic recovery for every tool. The two-slot synthetic ten-request check completed 10/10 in 4.656 seconds on September 28, 2026; full agent conversations and document extraction take longer.
