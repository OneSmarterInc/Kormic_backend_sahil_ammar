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

## Local Development

The easiest way to run the backend is via Docker Compose, which sets up PostgreSQL, Redis, Celery, and the web server automatically.

```bash
docker compose build
docker compose up -d
```

To run migrations:
```bash
docker compose exec web python manage.py migrate
```

## Deployment

For production deployment instructions, please read [DEPLOYMENT.md](DEPLOYMENT.md)."# Kormic_backend_sahil_ammar" 
