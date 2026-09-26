# University chatbot and retrieval

The officer chat and the student agent's `ask_university` / comparison tools use
the same `UniversityAgent`. Each answer reads that university's current profile
from the database and searches only its knowledge entries. Previous bot exchanges
are scoped to both the student and the university. Officer history stays in the
university's own chat channel. Student context is not saved as public knowledge.

The student advisor includes the student's career goals and preferences in its
prompt. Chat can update intake, location and funding preferences as well as
academic facts. University consultations receive the latest in-turn corrections
and a canonical student ID, without private notes or raw documents. Fit assessments
are reused only while the student's admissions context, university profile and
knowledge facts remain unchanged.

Responses still use `ANTHROPIC_API_KEY` and the existing Claude Haiku model.
Embeddings use local CPU inference with FastEmbed's `BAAI/bge-small-en-v1.5`
(384 dimensions), without an additional API key. PostgreSQL ranks vectors by
cosine distance within the university and merges them with keyword results.
Content hashes refresh embeddings after scraped/manual/verified facts change,
including bulk ORM updates. The first query can index existing facts; pre-index
large knowledge bases using the command below to avoid first-query latency.

## PostgreSQL setup

1. Install backend requirements: `python -m pip install -r requirements.txt`.
2. Use PostgreSQL with the `vector` extension installed. Compose now uses
   `pgvector/pgvector:pg16`, retaining PostgreSQL major version 16 and its existing
   data volume. For an external database, install pgvector on that server first.
3. Configure `DB_ENGINE=postgresql` and the existing `POSTGRES_*` credentials.
   The migration account must be able to create the extension, or an administrator
   can run `CREATE EXTENSION IF NOT EXISTS vector` before migrations.
4. Run `python manage.py migrate`.
5. Run `python manage.py index_university_knowledge`. This downloads the model once
   and indexes existing facts. Use `--university-id UUID` to index one university.
6. Restart backend workers. Claude credentials remain unchanged.

For Compose, run migrations/indexing with the backend service, e.g.
`docker compose exec web python manage.py index_university_knowledge`.
The model cache uses a persistent, writable volume shared by backend workers.
For direct runs its default is `.embedding_cache/`; override with
`UNIVERSITY_EMBEDDING_CACHE_DIR` when needed. Model download requires access to
Hugging Face. `--download-model-only` warms the cache without a PostgreSQL database.

Changing `DB_ENGINE` does not transfer data from SQLite. Use the repository's
database migration procedure before switching an existing installation. SQLite
development intentionally keeps lexical retrieval; it cannot execute pgvector
queries. If the encoder is unavailable, retrieval logs the failure and retains
keyword answers instead of taking chat offline.

## Verification

Authenticated university officer chat exposes bounded, read-only Claude tools for
the university dashboard, interested students, eligibility evidence, individual
academic profiles, pending/resolved queries, agent exchanges and knowledge search.
The university identity is bound server-side. Student-facing calls never receive
these private tools. Operational records are retrieved live, not embedded into
the public knowledge base. Lists are paginated at 25 records; the model can request
additional pages. Qualification means meeting configured, supported requirements,
not admission; missing academic evidence requires review. Unsupported requirements
remain unassessed. The tool loop allows six rounds, up to six calls per round.

`python manage.py test agents.tests agents.test_university_retrieval agents.test_university_officer_tools knowledge.test_vectors pure_multi_agent.tests pure_multi_agent.test_student_personalization django_api.test_university_chat --noinput`

The pgvector cosine/isolation test runs on PostgreSQL and is explicitly skipped on
SQLite. Tests cover fresh profile data, edited embeddings, university isolation,
per-student exchange history, officer authorization, and fallback retrieval.
