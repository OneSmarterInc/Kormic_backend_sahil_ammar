# Student and university chat agents

Both portals use native LangGraph model/tool loops and LangChain tools. University
assistant chat and the chat on an interested student's profile share the officer
engine, with separate persisted conversations. Qwen is attempted first; provider
failure or invalid Qwen tool calls use Claude. Images and scanned PDFs require
Claude vision. Provider capacity exhaustion defers queued work rather than
starting unbounded fallback calls. See [SCALING.md](SCALING.md) for worker,
PostgreSQL, Redis and shared provider limits.

## Student changes and source separation

- An explicit typed fact fills a missing field immediately. An unchanged value
  is a no-op. Replacing an existing value creates a before/after proposal.
- A later explicit confirmation applies that proposal. Declining keeps the saved
  field and treats the proposed value only as a conversation assumption. Clearing
  chat or retracting the assumption removes it; it is not shared with universities.
- Uploaded résumé and LinkedIn files are separate evidence sources. The agent
  reads PDF, DOCX, text or images, extracts supported facts and persists a draft
  before asking for confirmation. A résumé can update the main profile; LinkedIn
  updates only LinkedIn evidence. Every document update requires confirmation.
- Interrupted document extraction resumes from a persisted draft stage. A
  review-only question does not update any saved source. GitHub stays separate
  and chat sync requests use the linked OAuth account's background extraction.

## University tools and completeness

Officers can inspect their own entry, settings, contacts, requirements, knowledge,
official sources, researched courses/intakes/facts, interested students, queries
and exchanges. Student results include scoped profile cards. Tools can propose
profile/settings/contact edits and add or revise policies and admission rules.
Corrections to scraped facts become attributed officer knowledge, retaining the
original website evidence. No tool sends messages to students or makes admission
decisions.

Every university write needs a persisted preview and explicit approval in a later
turn. The agent asks for missing details first. Structured validation requires:

- CGPA: minimum, maximum, grading scale and applicant/program scope.
- Test scores: test/version, accepted range, scale and scope.
- Experience: duration and units; degrees/documents: accepted qualifications or
  required documents; deadlines: dated intake and scope.
- Scholarships: amount/currency/basis or coverage, eligibility, application
  process, deadline, scope and effective period.
- Courses: level, duration, study mode and requirements. Tuition, intakes and
  other policies have category-specific completeness checks.

Ambiguous consent, incomplete values and placeholders cannot be treated as
approved complete entries. The model chooses tools and writes conversational
answers; transactional code validates and stores its actions. Proposals are
actor-scoped, expire after seven days, are idempotent and reject stale snapshots.
Confirmed university edits invalidate knowledge vectors for reindexing.

## Setup and verification

Install `requirements.txt` (including pypdf) and run `python manage.py migrate`.
Migrations 0008–0010 add change proposals, document evidence and policy details.
Existing launcher behavior is unchanged: `Run-Kormic.bat` starts the app/workers
without launching Qwen. Start Ollama/Qwen separately using the existing setup in
[STUDENT_AGENT.md](STUDENT_AGENT.md). PostgreSQL/pgvector integration tests require a PostgreSQL test DB;
SQLite development does not exercise that backend.

Useful regression command:

```powershell
python manage.py test pure_multi_agent university_research.tests django_api.test_university_chat github_profiles knowledge --noinput
```
