# Historical University API notes

Archived integration notes, not the current API contract. Some endpoints described below have been removed. Use API_VERSIONING.md, SYSTEM.md and the actual URL configuration for current behavior.

# University APIs — Full Reference for Frontend Integration

Stack: **Django + Django REST Framework**, JWT auth (`rest_framework_simplejwt`). There is no separate university microservice — `agents/`, `personas/`, `knowledge/` are internal Python libraries invoked from inside these Django views.

## Base URL prefixes (`korgut_backend/urls.py`)

| Prefix | Routed to |
|---|---|
| `/api/auth/` | `accounts.urls` |
| `/api/verification/` | `verification.urls` |
| `/api/university-admin/` | `universities.urls` (university's own self-service setup/admin) |
| `/api/` | `django_api.urls` (dashboard, chat, queries, assessments — includes `/api/university/<university_id>/...`) |

## Auth header

All authenticated endpoints require:

```
Authorization: Bearer <jwt_access_token>
```

## Permission tiers used below

- **Public** — `AllowAny`, no token.
- **Auth** — `IsAuthenticated` only.
- **University** — `IsAuthenticated + IsTOTPEnrolled + IsUniversityRole` (caller's `account.role == "university"`, and TOTP must be enrolled/confirmed).
- **University (own)** — same as above **plus `ScopedToOwnUniversityId`**: the `university_id` in the URL path must equal the caller's own `account.university_id`, or the API returns `403 {"detail":"You may only access your own university's data."}`.
- **Student or University (own)** — `IsAuthenticated + IsTOTPEnrolled + IsStudentOrUniversityRole`, with manual ownership checks inside the view (see individual endpoints).

Standard permission-denial responses (from DRF / custom permission classes), for any endpoint below:
- `401` — not authenticated / invalid or expired token.
- `403` — `{"detail": "This endpoint is only available to university accounts."}` (role mismatch), or `{"detail": "TOTP enrollment is required before using this endpoint."}` (TOTP not enrolled), or `{"detail": "You may only access your own university's data."}` (cross-tenant path param).

---

## Table of contents

1. [Registration / Auth touching University](#1-registration--auth-touching-university)
2. [University Self-Service Admin (`/api/university-admin/`)](#2-university-self-service-admin-apiuniversity-admin)
3. [University Dashboard APIs (`/api/university/<university_id>/...`)](#3-university-dashboard-apis-apiuniversityuniversity_id)
4. [Cross-cutting Queries / Assessments APIs](#4-cross-cutting-queries--assessments-apis)
5. [Data model reference](#5-data-model-reference)
6. [Known stale docs / traps to avoid](#6-known-stale-docs--traps-to-avoid)

---

## 1. Registration / Auth touching University

File: `accounts/urls.py`, `accounts/views.py`, `accounts/serializers.py`

### 1.1 `POST /api/auth/register/`
**Permission:** Public. `accounts/views.py:129` (`RegisterView`).

Request body:
```json
{
  "email": "string, required",
  "password": "string, required",
  "role": "student | university, required",
  "name": "string, optional",
  "university_id": "string, optional — join an EXISTING university by id",
  "institution_name": "string, optional — CREATE a new university (required if role=university and university_id is not given)"
}
```

Response `201`:
```json
{
  "message": "Account created. Complete TOTP enrollment to finish setup.",
  "must_enroll_totp": true,
  "access": "<jwt access token>",
  "user": {
    "id": 42,
    "email": "officer@school.edu",
    "name": "Jane Doe",
    "role": "university",
    "student_id": null,
    "university_id": "acme_school_of_engineering",
    "totp_enrolled": false,
    "university_setup_status": {
      "profile_exists": true,
      "has_description": false,
      "has_contacts": false,
      "has_eligibility_criteria": false,
      "has_scrape_urls": false,
      "has_knowledge_facts": false,
      "setup_complete": false,
      "completion_percentage": 0,
      "missing_steps": [
        "Add a program description.",
        "Add at least one contact detail (email, phone, website, or address).",
        "Add eligibility criteria.",
        "Save at least one official page URL to scrape.",
        "Add or scrape at least one knowledge base fact."
      ]
    }
  }
}
```

Errors `400`:
```json
{ "email": ["An account with this email already exists."] }
{ "university_id": ["Unknown university_id: some_bad_id"] }
{ "institution_name": ["Provide institution_name to register a new university, or university_id to join an already-registered one."] }
```

Notes:
- Passing `institution_name` (no `university_id`) auto-creates the `University` row (`universities/services.py:45` `register_university()`), auto-generating a slug `id` and a unique `agent_name`.
- Passing an existing `university_id` joins that university (multiple officer accounts can share one university).

### 1.2 `POST /api/auth/login/`
`accounts/views.py:156`. Public. Body: `{"email": "...", "password": "..."}`.
- If TOTP not yet enrolled → `200 {"must_enroll_totp": true, "access": "<jwt>", "user": {...same shape as above...}}`
- If TOTP already enrolled → `200 {"must_enroll_totp": false, "mfa_token": "<opaque>", "totp_required": true, "expires_in": 300}` (client must call 1.3 next)
- `401 {"detail": "Invalid email or password."}`

### 1.3 `POST /api/auth/verify-totp/`
`accounts/views.py:290`. Public. Body: `{"mfa_token": "...", "code": "123456 (TOTP) or 10-char backup code"}`.
- `200`: issues real `access`/`refresh` tokens (step 2 of login).
- `401 {"detail": "Session expired or invalid. Please log in again."}`
- `429 {"detail": "Too many incorrect attempts. Please try again later."}`
- `400 {"detail": "TOTP is not enrolled."}`

### 1.4 `POST /api/auth/totp/enroll/`
`accounts/views.py:208`. Auth only. No body.
- `200 {"secret": "BASE32SECRET", "provisioning_uri": "otpauth://..."}` (idempotent — same secret returned on repeat calls until confirmed)
- `400 {"detail": "TOTP is already enrolled."}`

### 1.5 `POST /api/auth/totp/verify-enrollment/`
`accounts/views.py:255`. Auth only. Body: `{"code": "123456"}`.
- `200 {"backup_codes": ["ABCD123456", ...]}`
- `400 {"detail": "Invalid TOTP code."}` / `{"detail": "TOTP enrollment has not been started."}` / `{"detail": "TOTP is already enrolled."}`

### 1.6 `POST /api/auth/refresh/` — standard SimpleJWT `TokenRefreshView`. Body `{"refresh": "..."}` → `{"access": "..."}`.

### 1.7 `POST /api/auth/logout/` — Auth only. Blacklists refresh token.

### 1.8 `GET /api/auth/me/`
`accounts/views.py:383` (`CurrentUserView`). Auth only.
- Response `200`: same `user` object shape as §1.1/§1.2, including `university_setup_status` when `role == "university"`.

---

## 2. University Self-Service Admin (`/api/university-admin/`)

File: `universities/urls.py`, `universities/views.py`. **All endpoints in this section: permission = University**, and always act on **the caller's own** university — there is no `university_id` path param, it's resolved from the JWT via `account.university_id`.

### `University` fields (`universities/models.py`)
`id` (slug, PK) · `name` · `agent_name` (unique) · `location` · `tagline` · `description` · `contact_email` · `contact_phone` · `website_url` · `admissions_office_address` · `eligibility_criteria` (`[{criterion, detail}]`) · `scrape_urls` (`["https://...", ...]`) · `tone_descriptors` (`[str, ...]`) · `best_fit_notes` · `not_best_fit_notes` · `communication_style_notes` · `never_do_notes` · `created_at` · `updated_at`.

### 2.1 `GET /api/university-admin/profile/`
`universities/views.py:100` (`UniversityProfileAPIView.get`).
- Response `200`:
```json
{
  "id": "acme_school_of_engineering",
  "name": "Acme School of Engineering",
  "agent_name": "Nova",
  "location": "Springfield, USA",
  "tagline": "Engineering for the real world",
  "description": "...",
  "contact_email": "admissions@acme.edu",
  "contact_phone": "+1-555-0100",
  "website_url": "https://acme.edu",
  "admissions_office_address": "123 Main St",
  "eligibility_criteria": [ { "criterion": "GPA", "detail": "3.0 minimum" } ],
  "scrape_urls": [ "https://acme.edu/admissions" ],
  "tone_descriptors": [ "warm", "direct" ],
  "best_fit_notes": "...",
  "not_best_fit_notes": "...",
  "communication_style_notes": "...",
  "never_do_notes": "...",
  "setup_status": {
    "profile_exists": true,
    "has_description": true,
    "has_contacts": true,
    "has_eligibility_criteria": true,
    "has_scrape_urls": true,
    "has_knowledge_facts": true,
    "setup_complete": true,
    "completion_percentage": 100,
    "missing_steps": []
  },
  "created_at": "2026-01-10T12:00:00Z",
  "updated_at": "2026-07-20T09:30:00Z"
}
```
- `404 {"status":"error","message":"No university profile found for this account."}`

### 2.2 `PATCH /api/university-admin/profile/`
Same view, `.patch` (`universities/views.py:106`).
- Request body — any subset of these fields (all optional, only sent fields are updated):
```json
{
  "name": "string",
  "location": "string",
  "tagline": "string",
  "description": "string",
  "contact_email": "string",
  "contact_phone": "string",
  "website_url": "string",
  "admissions_office_address": "string",
  "eligibility_criteria": [ { "criterion": "string", "detail": "string" } ],
  "tone_descriptors": ["string", "..."],
  "best_fit_notes": "string",
  "not_best_fit_notes": "string",
  "communication_style_notes": "string",
  "never_do_notes": "string"
}
```
- Response `200`: full profile object, same shape as 2.1.
- Side effect: editing `description`, `contact_email`, `contact_phone`, `website_url`, `admissions_office_address`, or `eligibility_criteria` re-syncs derived "seed" facts into the knowledge base (visible via §2.6/§2.7 with `source_type=seed`).
- `404`: same as 2.1.

### 2.3 `GET /api/university-admin/profile/completion/`
`universities/views.py:129` (`UniversityProfileCompletionAPIView`). GET only.
- Response `200`: the `setup_status` object standalone (same shape as the nested one in 2.1) — for a lightweight "setup progress" widget that shouldn't need the full profile payload.
- `404`: same as 2.1.

### 2.4 `GET /api/university-admin/agent-name/`
`universities/views.py:156`.
- Response `200`: `{"agent_name": "Nova"}`

### 2.5 `PATCH /api/university-admin/agent-name/`
`universities/views.py:162`.
- Request: `{"agent_name": "Zara"}` (required, non-empty, ≤100 chars, case-insensitive unique across all universities).
- Response `200`: `{"agent_name": "Zara"}`
- `400`: `{"status":"error","message":"agent_name is required."}` or `"agent_name must be 100 characters or fewer."`
- `409`: `{"status":"error","message":"This agent name is already taken. Please choose another."}`

### 2.6 `GET /api/university-admin/scrape-urls/`
`universities/views.py:191`.
- Response `200`: `{"scrape_urls": ["https://acme.edu/admissions", "https://acme.edu/programs"]}`

### 2.7 `PUT /api/university-admin/scrape-urls/`
`universities/views.py:197`. **Replaces the entire list.**
- Request: `{"scrape_urls": ["https://...", "https://..."]}` (must be a list of non-empty strings).
- Response `200`: `{"scrape_urls": [...]}` (as saved)
- `400`: `{"status":"error","message":"scrape_urls must be a list of non-empty URL strings."}`

### 2.8 `POST /api/university-admin/scrape-urls/scrape-now/`
`universities/views.py:221` (`ScrapeNowAPIView`). No request body. Synchronously scrapes every saved URL, one at a time (a failure on one page doesn't lose results from others).
- Response `200`:
```json
{
  "total_facts_stored": 7,
  "results": [
    { "url": "https://acme.edu/admissions", "status": "ok", "facts_stored": 5 },
    { "url": "https://acme.edu/broken-page", "status": "failed", "facts_stored": 0, "error": "..." }
  ]
}
```
- `400`: `{"status":"error","message":"No scrape URLs are saved yet. Save some with PUT /api/university-admin/scrape-urls/ first."}`

### 2.9 `GET /api/university-admin/knowledge/`
`universities/views.py:247` (`KnowledgeFactListCreateAPIView.get`).
- Query params (optional): `?section=<source_type>` (one of `seed | manual | scraped | conversation | human_verified`), `?source_url=<exact url>`.
- Response `200`:
```json
{
  "knowledge": [
    {
      "id": 12,
      "topic": "Program Overview",
      "content": "Our engineering program focuses on...",
      "source_type": "seed",
      "source_url": null,
      "confidence": 1.0,
      "times_used": 4,
      "created_at": "2026-01-10T12:05:00Z"
    }
  ]
}
```

### 2.10 `POST /api/university-admin/knowledge/`
`universities/views.py:266`. Always stores `source_type: "manual"` regardless of any value sent.
- Request: `{"topic": "string, required", "content": "string, required", "confidence": "float, optional, default 1.0"}`
- Response `201`: single knowledge-entry object, same shape as list items in 2.9.
- `400`: `{"status":"error","message":"topic and content are required."}`

### 2.11 `GET /api/university-admin/knowledge/sections/`
`universities/views.py:304` (`KnowledgeSectionsAPIView`). Backs a sidebar/tab view grouped by section.
- Response `200`: `{"sections": [ {"section": "seed", "count": 4}, {"section": "scraped", "count": 12}, {"section": "manual", "count": 2} ]}`

### 2.12 `GET /api/university-admin/knowledge/urls/`
`universities/views.py:336` (`KnowledgeSourceUrlsAPIView`). Groups facts by the page they were scraped from; facts with no `source_url` (seed/manual/conversation) are omitted.
- Response `200`: `{"urls": [ {"source_url": "https://acme.edu/admissions", "count": 5}, ... ]}`

### 2.13 `PATCH /api/university-admin/knowledge/<int:fact_id>/`
`universities/views.py:397` (`KnowledgeFactDetailAPIView.patch`). **Only allowed when the fact's `source_type` is `manual` or `seed`** — editing scraped/conversation/human_verified facts is blocked (re-scrape, or resolve the related pending query, are the correct paths for those).
- Request: any subset of `{"topic": "string", "content": "string", "confidence": "float 0.0–1.0"}`.
- Response `200`: updated knowledge-entry object (shape per 2.9).
- `400`: empty topic/content, non-numeric confidence, no fields provided, or `"Cannot modify a '<source_type>' fact through this endpoint. ..."`
- `404`: `{"status":"error","message":"Knowledge fact not found."}` (also returned if the fact belongs to another university).

### 2.14 `DELETE /api/university-admin/knowledge/<int:fact_id>/`
Same view, `.delete`. Same `manual`/`seed`-only restriction.
- Response `204` (empty body).
- `400` / `404`: same as 2.13.

---

## 3. University Dashboard APIs (`/api/university/<university_id>/...`)

File: `django_api/urls.py`, `django_api/views.py`. **All endpoints in this section: permission = University (own)** — the `university_id` path segment must equal the caller's own `account.university_id`.

### 3.1 `GET /api/university/<university_id>/profiles/`
`django_api/views.py:1184` (`UniversityProfilesListView`). Dashboard listing of every student profile, with that university's fit assessment (if generated) flattened in.
- Response `200`:
```json
{
  "university_id": "acme_school_of_engineering",
  "profiles": [
    {
      "profile_id": "stu_a1b2c3",
      "name": "Jordan Lee",
      "profile_image_url": "https://.../api/profile/stu_a1b2c3/image/",
      "institution": "State University",
      "major": "Computer Science",
      "gpa": 3.7,
      "gpa_scale": "4.0",
      "gre_quant": 165,
      "toefl": 110,
      "budget": 40000,
      "work_months": 12,
      "academic_intelligence": {},
      "technical_intelligence": {},
      "research_intelligence": {},
      "behaviour_intelligence": {},
      "overall_profile_score": 82,
      "overall_profile": {},
      "profile_completeness": {},
      "strengths": ["Strong GPA", "Relevant internship"],
      "weaknesses": ["Limited research experience"],
      "recommendations": ["Consider adding a publication"],
      "ai_summary": "A strong applicant with...",
      "summary": "...",
      "skills": ["Python", "React"],
      "technical_skills": ["Django", "PostgreSQL"],
      "projects": [],
      "research": null,
      "research_interests": [],
      "publications": [],
      "match_tier": "strong_fit",
      "match_score": 88,
      "fit_summary": "Great alignment with program focus areas.",
      "recommendation": "admit"
    }
  ]
}
```
Notes: `match_tier` defaults to `"unassessed"`, `recommendation` defaults to `"review"`, `match_score` is `null` until a `FitAssessment` exists for that student+university pair.

### 3.2 `POST /api/university/<university_id>/profile/<student_id>/chat/`
`django_api/views.py:1245` (`university_profile_presenter_chat`). Officer-facing chat **about one student's profile** (`ProfilePresenterAgent`) — distinct from §3.4, which is about the program itself. `student_id` is unrestricted (any student may be asked about); only `university_id` is ownership-scoped.
- Request: `{"question": "string, required", "history": [ {"role": "user|assistant", "content": "string"} ], "optional, default []"}`
- Response `200`: `{"answer": "string"}`
- On internal failure (still `200`): `{"answer": "Profile Presenter failed.", "error": "AI profile explanation failed. Check API key, model name, credits, network, or profile data. Details: <exc>"}`
- If profile doesn't exist: `{"answer": "Profile not found."}`
- `400`: `{"status":"error","message":"question is required."}`
- Side effect: auto-generates a `FitAssessment` for this student+university if none exists yet; every turn is logged to `ChatMessage` (`channel="presenter"`).

### 3.3 `GET /api/university/<university_id>/profile/<student_id>/chat/history/`
`django_api/views.py:1297`.
- Response `200`:
```json
{
  "count": 3,
  "messages": [
    { "sender": "user", "content": "How strong is this candidate?", "created_at": "2026-07-20T10:00:00Z", "meta": {} },
    { "sender": "assistant", "content": "This candidate shows...", "created_at": "2026-07-20T10:00:02Z", "meta": {} }
  ]
}
```

### 3.4 `POST /api/university/<university_id>/chat/`
`django_api/views.py:1312` (`university_agent_chat`). Officer-facing chat with **the university's own program agent** (`agents.university_agent.UniversityAgent` — the exact same agent a student's `ask_university` tool consults), letting an officer preview/test how it answers.
- Request: `{"message": "string, required"}` (also accepts `"question"` as an alias key)
- Response `200`:
```json
{
  "university_id": "acme_school_of_engineering",
  "agent_name": "Nova",
  "reply": "Our program requires a minimum GPA of 3.0...",
  "pending": false,
  "pending_query": null,
  "confidence": 0.92,
  "trust": "high"
}
```
- When the agent can't answer confidently, it escalates instead of guessing: `"pending": true`, `"pending_query": {...}` referencing a newly created `PendingQuery` row (visible via §3.6–3.8 / §4.1).
- `400`: `{"status":"error","message":"message is required."}`
- `404`: `{"status":"error","message":"<ValueError from commons.get_university_agent>"}`
- `500`: `{"status":"error","message":"University agent chat failed: <exc>"}`
- Side effect: logs turn to `ChatMessage` (`channel="university"`) with `meta.confidence/trust/pending/pending_query/source`.

### 3.5 `GET /api/university/<university_id>/chat/history/`
`django_api/views.py:1364`. Same response shape as §3.3, but `channel="university"`.

### 3.6 `GET /api/university/<university_id>/questions/`
`django_api/views.py:1376` (`UniversityQuestionsView`). Raw officer-facing question log (`UniversityQuestionLog`).
- Response `200`:
```json
{
  "university_id": "acme_school_of_engineering",
  "questions": [
    { "university_id": "acme_school_of_engineering", "student_name": "Jordan Lee", "question": "Is there a merit scholarship?", "topic": "financial_aid", "created_at": "2026-07-19T08:00:00Z" }
  ]
}
```

### 3.7 `GET /api/university/<university_id>/queries/`
`django_api/views.py:1396` (`UniversityQueriesView`). **All** `PendingQuery` rows for this university (pending + resolved).
- Response `200`: `{"university_id": "...", "queries": [ <pending-query object — see shape below> ] }`

**Pending-query object shape** (used by §3.7, §3.8, §3.9, §4.1 — with minor key-set differences noted):
```json
{
  "id": 7,
  "query_id": 7,
  "university_id": "acme_school_of_engineering",
  "university": "Acme School of Engineering",
  "agent_name": "Nova",
  "student_id": "stu_a1b2c3",
  "student_name": "Jordan Lee",
  "program": "Computer Science",
  "question": "Do you accept spring admits?",
  "status": "pending",
  "priority": "normal",
  "urgency_reason": null,
  "display_status": "pending",
  "escalation_chain": [],
  "answer": null,
  "answered_by": null,
  "answered_at": null,
  "timestamp": "2026-07-19T08:05:00Z"
}
```
`status`: `pending | resolved`. `priority`: `normal | urgent`. `display_status`: `urgent | pending | answered`.

### 3.8 `GET /api/university/<university_id>/queries/active/`
`django_api/views.py:1407` (`UniversityActiveQueriesView`). Same shape as §3.7 but excludes `status=resolved`.

### 3.9 `GET /api/university/<university_id>/queries/archive/`
`django_api/views.py:1418` (`UniversityArchiveQueriesView`). Same shape as §3.7 but only `status=resolved`.

### 3.10 `GET /api/university/<university_id>/knowledge/verified/`
`django_api/views.py:1429` (`VerifiedKnowledgeView`). Durable, human-verified answers (`VerifiedAnswer` table — populated when an officer resolves a pending query, see §4.2/§4.3).
- Response `200`:
```json
{
  "university_id": "acme_school_of_engineering",
  "verified_answers": [
    {
      "query_id": 7,
      "university_id": "acme_school_of_engineering",
      "question": "Do you accept spring admits?",
      "answer": "Yes, spring admits are accepted for the MS program only.",
      "answered_by": "Admin",
      "source": "human_verified",
      "source_type": "human_verified",
      "confidence": 1.0,
      "synced_at": "2026-07-19T09:00:00Z"
    }
  ]
}
```

---

## 4. Cross-cutting Queries / Assessments APIs

Not under `/university/<id>/...` but scoped to the caller's own university via the account, and directly relevant to the dashboard.

### 4.1 `GET /api/queries/pending/`
`django_api/urls.py:48`, `django_api/views.py:1001` (`PendingQueriesView`). **Permission: University** (auto-filtered server-side to `request.user.account.university_id`; no path param). Excludes resolved queries.
- Response `200`:
```json
{
  "pending_queries": [
    {
      "id": 7, "query_id": 7, "student_id": "stu_a1b2c3", "student_name": "Jordan Lee",
      "university_id": "acme_school_of_engineering", "agent_name": "Nova", "program": "Computer Science",
      "question": "Do you accept spring admits?", "priority": "normal", "urgency_reason": null,
      "status": "pending", "timestamp": "2026-07-19T08:05:00Z"
    }
  ],
  "count": 1
}
```
(Note: this object omits `university`, `display_status`, `escalation_chain`, `answer`, `answered_by`, `answered_at` compared to the §3.7 shape — check for the fields you need.)

### 4.2 `POST /api/queries/answer/`
`django_api/urls.py:49`, `django_api/views.py:1035` (`AnswerPendingQueryView`). **Permission: University.** Refuses to touch an already-resolved query (use §4.3 to edit those).
- Request: `{"query_id": "int, required", "answer": "string, required", "answered_by": "string, optional, default \"Admin\""}`
- Response `200`: `{"status":"success","message":"Saved to Knowledge Base","query_id":7,"university_id":"acme_school_of_engineering"}`
- `400`: `{"status":"failed","message":"query_id is required"}` / `"answer is required"` / `"query_id must be a number"` / `"Query 7 is already resolved."` / `"university_id is missing in pending query record."` / `"Could not resolve pending query."`
- `403`: `{"status":"failed","message":"You may only answer queries for your own university."}`
- `404`: `{"status":"failed","message":"Pending query not found for query_id: 7"}`
- `500`: `{"status":"failed","message":"Failed to save human-verified answer.","error":"<exc>"}`
- Side effect: writes a `VerifiedAnswer` row (surfaces in §3.10) and marks the `PendingQuery` resolved.

### 4.3 `POST /api/queries/<int:query_id>/edit/`
`django_api/urls.py:50`, `django_api/views.py:1454` (`EditPendingQueryView`). **Permission: University.** Works even if the query is already resolved (the one difference from §4.2).
- Request: `{"answer": "string, required", "answered_by": "string, optional, default \"Admin\""}`
- Response `200`: `{"status":"success","message":"Query answer updated","query_id":7,"university_id":"acme_school_of_engineering"}`
- `400`: `{"status":"failed","message":"answer is required"}` / `"university_id is missing in query record."` / `"Could not edit query."`
- `403`: `{"status":"failed","message":"You may only edit queries for your own university."}`
- `404`: `{"status":"failed","message":"Query not found for query_id: 7"}`

### 4.4 `GET /api/assessments/<student_id>/`
`django_api/urls.py:38`, `django_api/views.py:787` (`AssessmentHistoryView`). **Permission: Student or University (own)** — dual-mode: a student sees their own history across every university; a university officer sees only their own university's rows for that student.
- Response `200`:
```json
{
  "student_id": "stu_a1b2c3",
  "count": 2,
  "assessments": [
    { "university_id": "acme_school_of_engineering", "assessment": { "match_tier": "strong_fit", "match_score": 88, "...": "..." }, "created_at": "2026-07-18T00:00:00Z" }
  ]
}
```
- `403`: `{"detail":"You may only access your own assessment history."}` (student requesting someone else's `student_id`).

### 4.5 `GET /api/assessments/<university_id>/<student_id>/`
`django_api/urls.py:40`, `django_api/views.py:817` (`AssessmentDetailView`). Latest assessment for one university+student pair only.
- Response `200`: `{"student_id": "...", "university_id": "...", "assessment": {...}, "created_at": "..."}`
- `403`: `{"detail":"You may only access your own assessment history."}` (student) or `{"detail":"You may only access your own university's assessment history."}` (officer, cross-tenant).
- `404`: `{"detail":"No fit assessment found for student_id=..., university_id=...."}`

### 4.6 `GET /api/` (API index / health check)
`django_api/urls.py:6`, `django_api/views.py:87` (`api_home`). **Public.** Returns a directory of available endpoint groups, including `"university_dashboard_apis"` (mirrors §3) and `"university_ids": [...]` (every registered university id — useful for populating a university picker/dropdown).

---

## 5. Data model reference

Backing tables for the endpoints above (for context, not directly exposed):

- `universities/models.py`: **`University`** — see field list in §2.
- `django_api/models.py`:
  - `UniversityKnowledgeEntry` — backs §2.9–2.14. Fields: `id, university_id, topic, content, source_type (seed|manual|scraped|conversation|human_verified), source_url, confidence, times_used, created_at`.
  - `PendingQuery` — backs §3.7–3.9, §4.1–4.3. Fields: `id, university_id, university_name, agent_name, student_id, student_name, program, question, status (pending|resolved), priority (normal|urgent), urgency_reason, display_status, escalation_chain, answer, answered_by, answered_at, created_at`.
  - `VerifiedAnswer` — backs §3.10. Fields: `query_id, university_id, question, answer, answered_by, source_type, confidence, synced_at`.
  - `UniversityQuestionLog` — backs §3.6. Fields: `university_id, student_name, question, topic, created_at`.
  - `FitAssessment` — backs §3.1 (`assessments` map), §4.4, §4.5. Fields: `student, university_id, assessment (JSON), created_at`.
  - `ChatMessage` — backs all `*/chat/history/` endpoints. `channel` values used here: `presenter` (§3.3) and `university` (§3.5).

---

## 6. Known stale docs / traps to avoid

Two other markdown files in this repo describe university-related endpoints that **no longer exist** in the current codebase — don't use them as ground truth for student-facing chat/assessment paths:

- `university_onboarding_flow.md` — accurate for §1/§2 above, but **missing** `knowledge/sections/`, `knowledge/urls/`, and the `PATCH` case of `knowledge/<id>/` (only documents `DELETE`).
- `frontend_integration_guide.md` and `postman_test.json` — both still reference `POST /api/chat/university/<university_id>/`, `GET /api/chat/university/<university_id>/history/`, and `POST /api/assessments/generate/<university_id>/` as **student-facing** endpoints, and mention hardcoded university ids (`wright_state_cs`, `franklin_cs`) from a removed legacy persona dict. **None of these paths exist in `django_api/urls.py` today.** A code comment at `django_api/views.py:1154-1159` confirms the replacement: student-facing chat is now solely `POST /api/chat/agent/` (`agent_chat` view), which triggers university-fit/university-agent calls **internally** — there is no direct student-facing "chat with a specific university" or "generate assessment" endpoint anymore.

No OpenAPI/Swagger spec exists in this repo; this document is generated directly from the current `urls.py`/`views.py`/`models.py`/`services.py` source (verified 2026-07-22).

