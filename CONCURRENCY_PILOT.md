# Local concurrency deployment

This initial rollout note is superseded by [QUEUE_ARCHITECTURE.md](QUEUE_ARCHITECTURE.md), which describes tenant fairness, safe model-checkpoint recovery and the dedicated document queue added afterward. The observations below describe the earlier pilot only.

The local Windows installation uses persistent AgentJob rows and `python manage.py agent_worker --concurrency 10`. HTTP admission returns 202 and existing clients poll the job. Each conversation admits one active turn; university presenter chats have separate conversation keys. Two Qwen calls can execute concurrently across workflows. Run-Qwen.bat starts Ollama separately; Run-Kormic.bat starts the application and chat worker without starting Ollama.

Backend .env settings:
```
AGENT_QUEUE_ENABLED=true
AGENT_QUEUE_BACKEND=database
AGENT_DISTRIBUTED_LIMITS=true
AGENT_CAPACITY_BACKEND=database
GITHUB_QWEN_CONCURRENCY=2
```

Inference tickets are shared database records. Chat gets a 10-second advantage over unclassified interactive requests and 20 seconds over GitHub extraction. Ranking is by creation time plus this offset, so older background tickets can precede newer chat. This is bounded priority, not strict round-robin tenant scheduling. The provider pool enforces concurrency and token/request budgets. Slots are released before graph tools run. Capacity pressure yields graph checkpoints instead of triggering Claude spending. Provider failures retain Claude fallback.

Queue delay and execution duration are logged separately, without prompt bodies. Duplicate deliveries are fenced by a conversation lease and status compare-and-swap. Queued jobs survive process restarts; capacity yields persist resume state and retry time. Abruptly interrupted processing jobs become failed after 16 minutes rather than blindly replaying potentially committed mutation tools. Automatic crash recovery at every tool boundary is not yet implemented.

Run `python manage.py benchmark_ai` for ten short synthetic Qwen requests (no user data and no Claude fallback). September 28 validation completed 10/10 in 12.594 seconds including initial model loading. This does not establish latency for full agent workflows, long prompts or ten simultaneous document uploads.

This machine still uses SQLite. PostgreSQL/Redis/Celery deployment remains in docker-compose.yml with explicit Celery/Redis overrides. Docker and WSL are not installed here; no production database migration was performed. Do not describe this pilot as a horizontally scaled production deployment. Additional caching, strict per-tenant fairness and long mixed-workload soak tests remain future work.
