# Repeatable school knowledge import

Owner: the authorized school operator. Reviewed October 27, 2026.
This procedure is school-independent. Do not copy another school's UUID,
approval record, contacts, source snapshot or expected answers.

## 1. Identify the school and release

Record the exact school name and UUID from its authenticated university portal,
official domain, target academic year/term, applicant population, operator and
deployed backend commit. Confirm the selected school before approving sources.
Record the four knowledge groups and their authorized escalation contacts in
private evidence. A second school needs its own contacts and test identities.

## 2. Discover and review sources

Use the university portal's URL discovery workflow. Start with a bounded scope:
admissions, international admissions, tuition/fees, housing and orientation.
Include effective-date notices and programme exceptions, not just marketing pages.
Review the proposed URLs and cluster membership before approval. Exclude unrelated
schools, unofficial copies, login pages and links without relevant evidence.

For each source record: requested URL, final URL, actual HTTP status, fetch time
in UTC, raw snapshot, extracted text, character count and SHA-256 of the UTF-8
text. Preserve the snapshot used for answers separately from later crawls.
Discovery/classification or a search snippet is not proof of reading the page.
If the normal workflow does not retain an item, capture it explicitly before
claiming the evidence is complete. Never fabricate a successful fetch.

## 3. Approve and import

| Review group | Sources |
| --- | --- |
| admissions | Applications, academic requirements and programme eligibility |
| international | Immigration, international applications and English requirements |
| money | Tuition, fee changes, deposits, scholarships and financial aid |
| campus_life | Housing, orientation, arrival and student services |

Approve only the reviewed cluster URLs. Record approval ID, reviewer, timestamp,
department and the immutable selected URL list. Approval queues a selected-source
scrape; wait for completion and inspect failures. Group mapping records review
and escalation ownership; auto-scraped facts can legitimately be ungrouped.
Do not duplicate every fact into all groups or rewrite all group IDs.

## 4. Verify persisted knowledge

Record job IDs and before/after counts. For every approved URL inspect persisted
content, source URL, periods, degree level and conditions against its snapshot.
Confirm the facts needed by the acceptance questions survived extraction, including
qualifiers such as new versus continuing students. A nonzero count alone is not
enough. Preserve old evidence with its period; do not relabel it as current.
Exercise retrieval through the selected school's agent and check visible citations.
Verify another school's sources do not leak into the result.

## 5. Retry and recover

Do not start a duplicate scrape while one is active. Inspect the failed job and
worker logs; distinguish fetch/403/429 failures, queue delivery failures and
extraction failures. Retry only the reviewed failed sources through the supported
workflow after correcting the cause. Preserve failed attempts and snapshot hashes.
Do not disable SSRF/TLS checks, bulk-delete knowledge, or invent HTTP 200 records.
Check deduplication on a repeated import; changed content needs a new snapshot.
The Franklin replay and Fall 2026 commands are school-specific exceptions, not
templates to edit for another school. See the Franklin appendix for those commands.

## 6. Citation-based acceptance

Choose ten school-specific questions before the run. Include eligibility, tuition
periods, applicant categories and student services. Record each exact question,
answer, citation, saved source passage, reviewer and verdict. Open each citation
and compare the answer with the saved evidence actually used. Check amounts,
dates, exceptions and scope rather than matching a few keywords.

A supported cited answer passes. When evidence cannot answer a question, a real
query to the appropriate department can pass safe escalation handling: record its
ID, pending state and student-facing explanation. Guessing, incorrect citations,
generation errors, unnecessary escalation of loaded policy or merely saying a
query was sent do not pass. Verify notification email and the human reply returning
to the original question separately.

## 7. Evidence package and release decision

Keep a private evidence package with school/release identity, approved manifest,
snapshots/hashes, jobs, source checks, ten outcomes, escalation receipt/round trip,
fresh disposable student claim, and reviewer decision. Redact tokens, credentials,
MFA setup secrets and backup codes. Use authorized demo inboxes, retain live contact
settings, and record any test-contact restoration. The source import is complete
only after source checks; Gate 2 additionally needs accepted production outcomes
and the demo review. Do not label a pending check as passed.
