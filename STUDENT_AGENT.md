# Student advising agents

## Run locally

From the directory containing both repositories, double-click `Run-Kormic.bat`.
It starts the API, frontend, GitHub worker and university research worker in the
background. It applies pending migrations when starting the API. It never starts
Ollama or downloads a model. `Run-Kormic.bat --rebuild` rebuilds the frontend;
`Run-Kormic.bat --check` reports local process readiness.

The versioned launcher is `scripts/Run-Kormic.bat`. From the backend repository,
copy it outside both repositories with `Copy-Item scripts/Run-Kormic.bat ../Run-Kormic.bat`.
Both checkouts must have their existing repository directory names and installed
Python/Node dependencies. The launcher uses the local runtime when available,
otherwise the backend's `venv/Scripts/python.exe`.

Start Qwen separately with Ollama installed:

```powershell
ollama serve
```

In another terminal:

```powershell
ollama pull qwen3:1.7b
ollama run qwen3:1.7b
```

If the Ollama Windows service is already running, skip `ollama serve`. The model
name must match `STUDENT_OLLAMA_MODEL` and `GITHUB_OLLAMA_MODEL`. A larger
tool-capable Qwen model can be configured when hardware permits. Keep Ollama on
a trusted private host; configure its hostname in `GITHUB_OLLAMA_ALLOWED_HOSTS`.
`ANTHROPIC_API_KEY` enables Claude fallback. Never put it in frontend settings.

## What runs

`pure_multi_agent/student_graph.py` compiles a shared LangGraph graph with a
model → LangChain tools → model loop. Each student gets isolated runtime context
and durable conversation checkpoints. Models select tools, interpret evidence,
ask clarifying questions, and compose the answer. Python validates and executes
tools, handles authorization and persistence, and reports operational failures;
it does not generate course-fit scores or canned advising answers.

`model_router.py` tries native LangChain `ChatOllama` first, then
`ChatAnthropic` when Qwen is unavailable or emits invalid tool arguments.
Uploaded images use Claude because the configured Qwen is text-only. A short
shared circuit breaker prevents every request from probing an offline Qwen.
Capacity exhaustion defers work instead of bursting into unlimited paid calls.
Claude tool-argument errors return to the graph for model correction; tools
still validate their schemas before executing.

Available tools cover current student evidence, profile updates, resume and
LinkedIn review/drafts, GitHub processing/status, course recommendations,
university comparisons and fit, official university search, current dates,
verification items, study resources, scholarships, application plans and saved
advice. Advice documents are private to the owning student. Missing documents
are unknown evidence, not proof that the student lacks experience.

Registered university consultations use a separate scoped LangGraph adviser
with retrieval and clarification tools. Unresolved officer questions keep the
existing pending-query and conversation-log flow.

## University evidence and research

1. Look in the directory of universities with active university accounts.
2. Look in the shared research database.
3. Discover official domains, read the official identity page, and resolve the
   institution. Multiple candidates require the student's city, address,
   campus or explicit selection; search pages are not counted as institutions.
4. Search facts inside the resolved official domain. Aggregator and social
   sources are rejected. Fetching validates public addresses and redirects,
   stays within the institution's domain, respects robots rules, and has size
   and time limits. Page text is untrusted evidence, never agent instructions.
5. Compose a cited answer from available evidence; enqueue research after that
   answer is generated. A separate research LangGraph chooses pages and emits
   typed facts, courses and intakes with exact supporting quotes.

Research uses up to eight HTML pages and a bounded number of graph steps per
run. Robots restrictions, inaccessible pages and incomplete catalogs are
reported as coverage gaps. PDFs and JavaScript-only catalogs are not currently
extracted. No claim is made that every course or intake has been discovered.
Valid records are checkpointed while rejected citations are repaired. If the
processing budget is exhausted, only those validated records are published,
with an explicit coverage-limit note; unsupported records are excluded.

Tables in `university_research` separate institutions, research jobs, fetched
pages, cited facts, courses, intakes, candidate searches and private advising
artifacts. An optional relation connects an enrolled university to its website
cache without overwriting officer-maintained data. Student responses include
source links and dates, but no enrollment badge or Update information control.

The superuser **Update information** tab owns the freshness setting (30 days by
default, configurable 1–365) and manual background refresh controls. Students
can see that dated information may be outdated. A refresh retains old dates on
unvisited pages; it does not falsely make the entire catalog fresh.

PostgreSQL stores 384-dimensional vectors with an HNSW cosine index, using the
existing local embedding adapter. Facts, courses and intakes are searchable as
evidence. SQLite development falls back to lexical retrieval; it does not
exercise pgvector. Migrations enable the production vector schema/index.

## Concurrency and deployment

The local `.bat` is for development. For multiple app instances use the
PostgreSQL + Redis + Celery configuration in `docker-compose.yml`; never share
SQLite across production instances. See `SCALING.md` and `GITHUB_PROFILE.md`.

Multiple students run concurrently. A student's chat turn is serialized to
protect its memory, while different students use separate jobs. Shared database
model slots and rate/token budgets cover chat, GitHub and university research.
GitHub and research workers release a job after a graph step for fair scheduling.
Leases, conditional writes and active-job constraints prevent duplicate claims
and stale workers from publishing results. Student chat resumes its checkpoint
after a capacity delay without replaying completed tools.

Initial defaults: four GitHub worker threads, two research worker threads, four
Celery chat workers per agent worker instance, one shared Qwen model slot and
four Claude slots. Tune model slots to GPU capacity and provider quotas before
increasing worker counts. One hundred signed-in users does not require one
hundred model copies. The queue absorbs bursts while the UI keeps responding;
it cannot remove model inference time or provider limits.

## Verification

Regression checks cover provider routing, tool validation, actual LangGraph
execution with controlled model responses, ownership, ambiguity, source quotes,
official-domain filtering, lease ownership, fair scheduling, GitHub processing,
profile-merge safety and checkpoint resumption without duplicate side effects.
Frontend tests cover names-only repository pagination and automatic polling
recovery after repeated network failures.

The synthetic 100-student benchmark checks isolation/shared graph reuse, not
live GPU or Claude throughput. Local end-to-end verification uses disposable
accounts and real Claude fallback; PostgreSQL/Redis deployment load testing and
live Qwen quality/throughput testing must run in their configured environment.
