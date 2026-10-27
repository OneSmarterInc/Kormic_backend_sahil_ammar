# Repeatable school import and Gate 2 verification

Owner: the operator for the school's university account. Updated 2026-10-27.
Production backend release required for Franklin replay: `a0a88a4` or a descendant.
Do not mark Gate 2 passed merely because a crawl or import completes.

For a second school, start with [the generic school-import procedure](school-import-runbook.md).
The UUIDs, snapshots and demo identities below are Franklin-specific.

## 1. Identify and review the school

Record the university UUID, official domain, operator, review time, academic year,
and intended applicant population. Never reuse Franklin's UUID for another school.
Start with admissions, international admissions, tuition/fees, and housing or
student-life pages. Include effective-date notices and program exceptions.
Use the university portal's URL discovery workflow with the real official site.
Keep crawl scope bounded; review the proposed URLs before approval.

For each selected page retain the final URL, HTTP status, retrieval time, extracted
text, text length, and SHA-256 of the exact extracted UTF-8 text. Preserve the
original snapshot separately from later extracts. A search result snippet, link
discovery, or classification alone is not proof the page was read. Failed fetches
must remain failures, not fabricated HTTP 200 records.

## 2. Approve and load through the normal workflow

Review the discovered URLs and remove unrelated or unofficial sources before
approving each relevant cluster in the university portal. Cluster approval records
the reviewer and queues a selected-URL scrape job. Wait for each job to complete;
inspect failed jobs and retry only the affected approved sources.

| Knowledge group | Review scope |
|---|---|
| admissions | Admission requirements, programs, academic eligibility |
| international | Immigration, English proficiency, international applications |
| money | Tuition, fees, deposits, scholarships and financial aid |
| campus_life | Housing, arrival, orientation, campus services |

Group mapping is approval/escalation metadata. Automatically scraped facts can
remain ungrouped by design; do not bulk-relabel every fact or duplicate it into all
four groups. Verify the source URL and content on persisted knowledge entries.
Record job IDs, approvals, counts before/after, failed URLs, and exceptions.

For a second school repeat this workflow with its own UUID, source manifest,
approvals, questions and contacts. Do not edit Franklin's replay script to pretend
an unreviewed school has already been approved.

## 3. Franklin's existing approved snapshot

University UUID: `12ea2efd-73d0-4982-b022-9e76b25060f7`.
Approved import: discovery job 15, marker `franklin-gate2-20261026T114702Z`.
Snapshot on AWS:
`~/kormic-gate2/franklin-source-review-20261026T114702Z.json`.

After deploying the release, run:

```bash
./venv/bin/python manage.py shell < deploy/refresh_franklin_gate2.py
```

This verifies the six saved URLs against the existing approvals and hashes, then
replays saved text with full-content deduplication. It is transactional and
idempotent. It neither fetches current pages nor approves additional sources.
Keep old evidence: do not delete contradictory facts without reviewing them.

**Additional source to review and import:**
https://www.franklin.edu/tuition-fee-changes-fall-2026

This page is outside the original six-page snapshot. Use the operator-approved
single-page command documented in [the Fall 2026 importer](../../deploy/FRANKLIN_FALL2026.md).
It saves actual fetch evidence and Money approval metadata, and rejects failed
fetches and unexpected pages. Do not modify the original six-source manifest.
The page has been cited in the operator's production test; this confirms retrieval,
not that Question 5 is fully answered.

## 4. Fresh ten-question run

Use a fresh production university-agent conversation after replay completes.
Record deployment SHA, start/end UTC times, operator and question order.
Use the production student conversation with Franklin selected. A shell or mocked
unit test is not a substitute for the authenticated browser acceptance run.

For each answer save its exact text and visible citations. Open every cited page,
check the saved read evidence, and confirm that the cited passage supports the
claim for the correct year, degree and applicant category. Returned retrieval
records alone are not visible answer citations. Mark missing citations, wrong
amounts, unsupported eligibility claims and context-budget refusals as failures.
Record reviewer/pass/fail per question; never auto-pass from keywords alone.

| # | Question | Expected result to verify against the current page |
|---|---|---|
| 1 | What is the standard minimum GPA for master's admission? | 2.50/4.00 for standard non-nursing master's admission; distinguish exceptions. |
| 2 | Can a domestic master's applicant with a 2.30 GPA receive conditional admission? | Explain the 2.250–2.499 conditional pathway and its conditions; do not promise unconditional admission. |
| 3 | Can an international applicant email their own transcript and have it count as official? | No; explain official direct-delivery/evaluation requirements and applicable alternatives without overgeneralizing. |
| 4 | Does Franklin require GRE or GMAT for master's admission? | Verify the no-GRE/GMAT policy and cite the master's admissions source; do not imply other requirements are waived. |
| 5 | What is standard master's tuition per credit for 2026–2027? | New master's students entering fall 2026: $690/credit. Existing master's students retain their guaranteed rate. $828 is doctoral tuition, not standard master's tuition. Guarantee: three years/nine terms for masters; existing students retain their rate for up to three additional years from Fall 2026. Resource fee is separate and charged per trimester, with documented exemptions. Cite the effective-Fall-2026 notice. |
| 6 | What is the F-1 new-student deposit, and is it applied toward tuition? | Distinguish the $2,000 tuition deposit credited to first-term tuition from the separate $200 confirmation deposit; verify refund terms. |
| 7 | Does Franklin offer on-campus housing? | No; support with a page actually read. |
| 8 | Who helps newly admitted international students find housing? | OISP guidance; do not imply housing is guaranteed. |
| 9 | How is International Student Orientation delivered? | Online interactive orientation; distinguish it from Immigration Check-In and predeparture material. |
| 10 | What must students bring to Immigration Check-In? | Electronic passport/visa/I-20/I-94, local address, US phone number and access to myFranklin; never request credentials in chat. |

Question 5 was checked against the directly retrieved official HTML on October
26, 2026. The reviewer retained `work/franklin-fall2026.html` outside the repository;
obtain and retain the production crawler snapshot before marking that source imported. The notice
also distinguishes tuition from additional fees. The expectation above does not
claim one rate applies to every continuing student or special tuition arrangement.

## 5. Escalation and disposable student claim

The operator reported completing the SSGMCE demo claim. Treat the original demo
identity as used, not as a fresh invitation. For another recorded claim, the
operator must choose a new inbox/alias they control and check that no account or
roster entry already uses it. Record the institute and roster ID in private test
evidence rather than publishing mailbox addresses or personal identifiers here.

The existing Campus Life contact was confirmed team-controlled. The GRE/GMAT
query round trip was demonstrated separately. Neither proves mailbox delivery:
record the actual query ID, routed department, configured recipient and received
notification timestamp in private evidence. Preserve existing live contacts.
Before creating a new claim, check for an existing account or roster entry for
that email. Do not delete an existing account, reset its credentials, or treat an
already claimed profile as a successful fresh claim. Resolve the institute by
exact case-insensitive name; if ambiguous, select its UUID explicitly.
Do not use a real school's staff mailbox without authorization. Do not overwrite
live group contacts with a demo inbox on a school serving real students. Use an
isolated demo school/group or an agreed demo window with recorded original contact
values, then restore them and verify restoration before reopening normal use.

Configure the contact name/email through the university knowledge-group settings.
The authenticated endpoint is PATCH
`/api/university-admin/knowledge-groups/<slug>/`, with
`escalation_contact_name` and `escalation_contact_email`. Map all four groups to
their approved contacts. Do not put credentials in shared scripts.

Ask a deliberately unsupported question, such as a guarantee of accessible
off-campus housing. Record its query ID, routed group, delivery, recipient reply,
and the reply shown under the original student question. The response must be
labelled test-only and must not invent university policy. An email send alone
does not prove the round trip.

Use the institute portal's supported CSV template for one clearly labelled demo
student and the disposable inbox. Use synthetic profile data, send one invitation,
and claim via the received link in a private browser session. Verify token
prefill, confirmation, profile ownership and rejection of reuse. Do not save tokens,
passwords, backup codes or MFA secrets in the transcript. Clean up the test profile
using the normal deletion workflow and remove the test roster entry when safe.
An internal disposable claim does not replace the separate outside-volunteer test.

## 6. Evidence and sign-off

Keep the reviewed manifest, job/approval IDs, ten answer/citation results,
escalation round trip and claim result together. Record remaining failures explicitly.
Accept a supported cited answer or a genuine escalation for an unconfirmed answer;
never accept invented facts, a nonexistent escalation, or omitted loaded policy
disguised as a successful answer. Gate 2 requires ten reviewed outcomes plus the
escalation and claim evidence;
the deployment or refresh command alone never closes it.
