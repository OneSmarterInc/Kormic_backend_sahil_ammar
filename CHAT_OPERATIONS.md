# Chat generation policies (P1-06 / P1-07)

## Deploy in this order

1. Deploy the updated student app first. It accepts both the old completed response and new job responses.
2. Apply `python manage.py migrate`. Start the dedicated **Linux prefork** chat worker before routing traffic to the new backend:
   `celery -A kormic_backend worker -l info -Q chat --pool=prefork --concurrency=2 --prefetch-multiplier=1 -n chat@%h`.
   Compose includes this as `chat_worker`. The ordinary Celery worker keeps consuming the default queue.
3. Deploy the backend, then verify authenticated submit -> queued/running -> completed and notification delivery.
   Older installed mobile binaries must be upgraded before the backend switches to job responses.
4. Watch queue depth, errors, budget rejections, and `python manage.py chat_latency`.
   Load-test multiple web/worker processes and confirm no duplicate generations or university over-admission.

## Contract

`POST /api/chat/agent/` and `PATCH /api/chat/agent/<message_id>/edit/` return HTTP 202 with
`job_id`, `message_id`, and `poll_after_ms`. Poll `GET /api/chat/agent/jobs/<job_id>/` with
the same authenticated student session. Results are owner-scoped, persist across web restarts,
and return `status` = queued, running, completed or failed. Completion contains the original
reply fields. Failure contains `code` and a safe user-facing `message`; it is never saved as a
successful assistant answer. The app retains its waiting state while polling.

Editing, generating and clearing share a per-student admission lock. One generation per student
is allowed, including queued work. Duplicate task delivery cannot claim an already-running job.
Broker-publish ambiguity retains the lease rather than permitting duplicate work. Queued tasks
must start within 60 seconds; abandoned or killed jobs expire and return `CHAT_TIMEOUT`.
Unknown provider costs stay charged. No automatic model or job retry occurs.

## Default policy

| Control | Default |
|---|---:|
| Individual provider call | 20 seconds, no retry |
| Agent turn soft budget | 75 seconds |
| Dedicated worker hard limit | 90 seconds |
| New/edit requests per student | 6/minute |
| Concurrent queued/running jobs per student | 1 |
| Global queued/running admission cap | 100 |
| Daily reserved tokens per student (UTC) | 250,000 |
| Daily conservative cost allowance | USD 5 |
| Model calls / tool calls per turn | 10 / 8 |
| Universities per turn / concurrent calls per university | 4 / 2 |
| Attachments per message | 3 |
| Attachment size / aggregate size | 5 MB / 10 MB |
| Message length | 8,000 characters |

Environment names are in `.env.template`. Call and turn values are clamped to 30/80 seconds;
worker hard limit stays 90 seconds. Gunicorn's configured 120 seconds is no longer the model
execution deadline: generation happens in a separate process. Keep the worker in prefork mode;
thread/solo workers cannot provide the required process termination guarantee.

The deadline and call counters propagate through LangGraph context into raw Anthropic helper
calls. University answers and fit assessments take shared, expiring database leases. Hard-killed
workers cannot hold those leases indefinitely. The graph also has a 12-step recursion bound.

## Cost accounting and telemetry

`ChatModelCall` records job, model, reported input/output tokens, conservative charged tokens,
reserved estimated USD, call status and elapsed milliseconds. Reserve under a student row lock
before generation, across every worker. Text reservations use UTF-8 bytes plus a protocol margin;
image/document inputs use the provider token-count endpoint plus the margin. Keep reservations
charged even after completion (or unknown usage): these are intentionally conservative quotas,
not billing reconciliation. The configured input/output USD rates are operator-selected upper
bounds, **not a current provider price quote**. Verify that they cover every enabled model and
cache mode before production, and calibrate allowance sizes against actual student workloads.

`ChatGeneration` records end-to-end latency including queue time and terminal outcome.
`python manage.py chat_latency` reports 24-hour p50/p95/p99, sample count, and separate outcome
counts, including expired jobs. Export these records/command results to the existing monitoring
system; real production percentiles cannot be inferred from mocked tests. Apply the deployment's
retention policy to completed jobs/model-call records after the quota day and audit window.

The new allowance covers the main student chat and its nested tools, not unrelated standalone
profile uploads or administrator model workflows. Those require a separate product quota decision.
Tool side effects already committed before a timeout are not rolled back. After failure the next
turn rebuilds the graph checkpoint from the visible transcript, avoiding incomplete tool-call state.

## Runtime verification

The regression suite tests ownership, duplicate delivery, timeouts, rate and cost budgets,
attachments, university leases, and a PostgreSQL concurrent-admission race. Run:

```sh
python manage.py test django_api.test_chat_jobs django_api.tests.ChatHistoryTests django_api.tests.ChatEditAndAttachmentTests
python manage.py chat_latency
```

Before release, test an intentionally slow provider/tool against a real prefork worker, confirm
process termination at 90 seconds, wait for job expiration, and verify a later turn succeeds.
Test Redis/broker interruption, worker termination, and multiple web workers. No provider billing,
production load or deployed worker settings are certified by source changes alone.
