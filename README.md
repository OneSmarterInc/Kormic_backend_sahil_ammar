# Kormic Django Backend

This is the core backend for the Kormic platform. It powers the student chat agents (Aria), university specific agents, knowledge base scraping, and verification systems.

## Project Architecture & Apps

The project is broken down into several Django apps, each handling a distinct domain:

- **`accounts`**: Authentication (JWT), user models, password resets, and TOTP (Two-Factor Auth).
- **`django_api`**: Core student-facing API. Handles student profiles, resumes, LinkedIn parsing, and the main student agent (`Aria`) chat endpoints.
- **`universities`**: Destination schools. Manages the university agent personas, their knowledge bases, and the escalation routing system (`KnowledgeGroup`).
- **`institutes`**: Local partner orgs, coaching centers, or feeder schools. They do not have AI agents; they upload student lists for claim flows.
- **`institutes_list`**: The pre-fill claim flow. Manages the student lists uploaded by institutes and the secure invitation process.
- **`verification`**: The trusted-data layer. Handles requests from universities for students to verify specific claims (e.g. TOEFL scores, transcripts).
- **`pure_multi_agent`**: The LangGraph AI runtime. Houses the underlying multi-agent reasoning loops, checkpointers, and LangChain setup.
- **`agents`**: The specific agent implementations (e.g., `UniversityAgent`).
- **`knowledge`**: Web scraping and RAG (Retrieval-Augmented Generation) utilities for building university knowledge bases.
- **`url_discovery`**: Automated link discovery to help seed university knowledge bases from root domains.
- **`project_superuser`**: Administrative API for creating universities, institutes, and managing platform-wide settings.
- **`notifications`**: Email delivery and tracking.

## Institute vs University

These are two distinct concepts and are not meant to converge:

- **`University`** (`universities/`) is a destination school: it has an AI officer agent (persona, name, its own knowledge base), fit scoring against student profiles, `KnowledgeGroup`s for routing escalations to the right department, and `url_discovery` jobs to help build its knowledge base. Only a superuser can create one, via `POST /api/superuser/universities/`.

- **`Institute`** (`institutes/`) is a local org -- a school, coaching center, or an agent's partner institution -- that uploads student lists for the claim flow (`institutes_list/`). An institute never gets an agent; it only ever needs an identity (id, name, contact info) for provenance and one admin login to upload lists. Also only created by a superuser, via `POST /api/superuser/institutes/`.

## Canonical Backend Port

Kormic uses **port 8000** for the Django/Gunicorn backend everywhere:

- Docker container application port: `8000`
- Local Docker host URL: `http://127.0.0.1:8000`
- Health endpoint: `http://127.0.0.1:8000/api/health/`
- Local browser frontends: `VITE_API_BASE_URL=http://127.0.0.1:8000`
- Production Nginx upstream: `http://127.0.0.1:8000`

Do not use port `8030` for this backend.

## Local Development

### Direct local run with SQLite

```bat
copy .env.template .env
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

The default local database is `db.sqlite3`. LangGraph conversation checkpoints use `agent_checkpoints.sqlite3`. Set `DB_ENGINE=postgresql` to use the existing PostgreSQL configuration instead.

For converting an existing PostgreSQL database without deleting it, follow [SQLITE_MIGRATION.md](SQLITE_MIGRATION.md).

### Docker Compose / PostgreSQL

For direct local development, the backend now defaults to SQLite. Docker Compose remains explicitly PostgreSQL-backed for the multi-process production-style stack.

1. Create the local environment file:

```bash
cp .env.template .env
```

2. Set at least a local database password in `.env` because Compose intentionally refuses to start without one:

```ini
POSTGRES_DB=kormic
POSTGRES_USER=kormic
POSTGRES_PASSWORD=choose-a-local-password
```

Inside Docker, Django connects to the database service as `postgres:5432`; you do not need to change that host for the Compose stack.

3. Start the stack:

```bash
docker compose config
docker compose build
docker compose up -d
```

4. Verify the backend:

```bash
curl http://127.0.0.1:8000/api/health/
```

The API is available locally at:

```text
http://127.0.0.1:8000
```

For a browser-based Kormic frontend running on the same computer, use:

```ini
VITE_API_BASE_URL=http://127.0.0.1:8000
```

For a physical phone or another computer on your LAN, `127.0.0.1` refers to that device itself. Use the backend computer's LAN IP instead, for example `http://192.168.1.50:8000` (and include that host/origin in the appropriate Django local-development settings).

Compose automatically runs the one-shot `migrate` service before the web/Celery services. If you need to run migrations manually:

```bash
docker compose exec web python manage.py migrate
```

## Deployment

Production uses the same application port, `8000`; Nginx proxies HTTPS traffic to `127.0.0.1:8000` on the server. For the complete production instructions, see [DEPLOYMENT.md](DEPLOYMENT.md).
