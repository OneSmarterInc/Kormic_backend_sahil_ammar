# Qwen 0.6B switch

The default model and deployment examples now select `qwen3:0.6b` instead of
`qwen3:1.7b`. Both student-agent and GitHub-analysis model settings must agree.
An existing environment variable overrides the default, so pulling code alone
does not change an already configured production installation.

## What is preserved

- Tool schemas, argument validation, confirmation rules and access controls.
- Task-specific context sizing and expansion; sources are not shortened for this switch.
- Existing output allowances, bounded correction attempts and evidence checks.
- The 900-second model HTTP read timeout and existing background-job time limits.
- Existing provider policy; no new paid fallback or second resident model is added.
- The extraction cache already keys results by model identity, so the changed tag
  does not reuse a cached extraction generated under the old tag.

## Quality limitation

This is a model change, not a guarantee of equivalent reasoning. On 2026-10-07,
86 automated regression tests passed. A seven-case synthetic live-model smoke
check passed six checks with the existing router and validation paths. The smaller
model failed to extract the explicit country and requested number from a university
shortlist question. Existing guards removed invented institution names, but did
not recover those missing constraints.

Tool argument and eligibility checks required a correction attempt. Structured
extraction preserved the supplied IELTS value and null missing fields, but its
single quote covered the missing fields rather than the IELTS fact. These are
reasons to evaluate representative conversations before production rollout.
Passing schema checks is not evidence of equal answer quality.

The smoke check used a local GPU, bypassed scheduling/telemetry, and did not execute
tools. It is not an AWS performance benchmark or a full end-to-end test.
No CPU-only latency, concurrent-user capacity or quality parity is claimed.

## Existing AWS installation with Supervisor (no Docker)

1. Install the model on the Ollama server used by your backend:

   ```bash
   ollama pull qwen3:0.6b
   ollama show qwen3:0.6b
   ```

2. Edit the environment file actually loaded by your Supervisor API and workers:

   ```dotenv
   GITHUB_OLLAMA_MODEL=qwen3:0.6b
   STUDENT_OLLAMA_MODEL=qwen3:0.6b
   ```

   Keep the existing Ollama base URLs. If Supervisor sets either variable in its
   own `environment=` configuration, update that override too.

3. Run `sudo supervisorctl status` to identify the actual Kormic program names.
   Restart the Kormic API and model-using Celery/GitHub worker programs using those
   exact names. Restarting is necessary because Django settings and model clients
   are cached in running processes. If Supervisor configuration changed, run
   `sudo supervisorctl reread` and `sudo supervisorctl update` first.

4. Check effective settings in the same virtual environment as Supervisor:

   ```bash
   python manage.py shell -c "import os; from django.conf import settings; print('GitHub:', settings.GITHUB_OLLAMA_MODEL); print('Student:', os.getenv('STUDENT_OLLAMA_MODEL', settings.GITHUB_OLLAMA_MODEL))"
   ```

5. Test chat, named university comparisons, constrained shortlists, document
   extraction, profile-change confirmation and GitHub analysis. Check worker logs
   for the selected model and `ollama ps` during a request. A frontend or Android
   rebuild is not needed for a server-side model selection change.

## Rollback

Keep the existing 1.7B model until the smaller model passes your acceptance tests.
Set both environment variables back to `qwen3:1.7b` and restart the same processes.
This does not require a database migration. Keeping a model on disk does not make
it resident in RAM; loaded models follow the existing keep-alive policy.
