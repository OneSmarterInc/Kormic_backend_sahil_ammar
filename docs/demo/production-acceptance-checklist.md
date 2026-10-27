# Production acceptance evidence — October 27, 2026

Operator: Ammar (operates production browser and supplies results).
Status fields below are pending unless separately supported by dated evidence.
Record results privately; never put passwords, invitation tokens, MFA seeds or
backup codes in Git, screenshots, transcripts or the recording.

## Email receipt (pending)

Use the existing team-controlled department contact; do not overwrite contacts.
For the already demonstrated query, record query ID, department, masked recipient,
event (new query or answer), send/delivery status and mailbox received time.
An SMTP acceptance is not proof of inbox receipt. Check the actual inbox, including
spam. Do not resend an already delivered notification merely to create evidence.
The previously demonstrated university answer returning to the student remains
valid evidence and need not be repeated unless the flow changed.

## Browser CSP recheck (pending after latest deployment)

Keep report-only; enforcement requires Vikram's written approval. Preserve the
original observation start `2026-10-26T12:09:30Z`. In a signed-in test browser on
platform.kormic.ai verify the response's report-only header, then run this reviewed
code in the browser DevTools console, not the AWS shell:

```javascript
const probe = document.createElement('script');
probe.textContent = 'console.log("Kormic CSP observation recheck")';
document.head.appendChild(probe);
probe.remove();
```

Record the inline counter before and after, report request status, and UTC recheck
time. On AWS use `./venv/bin/python manage.py csp_report_summary --days 7`.
If the browser blocks report delivery, investigate the network error; console
warnings alone do not prove collection. Preserve the original start separately.

## MFA acceptance (pending)

Use authorized test accounts. For institute, university and superuser separately:
start an unenrolled account's setup; verify authenticator enrollment; save backup
codes privately; confirm workspace entry; sign out; sign in again with a fresh
authenticator code; confirm correct role/workspace and refresh persistence.
Do not reset a real user's existing MFA merely to repeat enrollment. If no unused
test account is available, mark enrollment blocked and test existing-account login
separately. Also test student web login and its actual configured verification flow.

| Role | Enrollment | Backup codes saved privately | Workspace | Repeat TOTP login | Refresh |
| --- | --- | --- | --- | --- | --- |
| Institute | Pending | Pending | Pending | Pending | Pending |
| University | Pending | Pending | Pending | Pending | Pending |
| Superuser | Pending | Pending | Pending | Pending | Pending |
| Student web | Pending | If applicable | Pending | Actual student flow | Pending |

## Fresh claim and recording (pending)

The original SSGMCE demo claim was reported successful. Choose a new controlled
unused alias for a fresh recorded claim, verify no existing account or roster row,
then upload one synthetic student using the institute CSV template. Confirm the
invitation is received and claim it through the normal flow. Never reuse or delete
the already claimed account just to produce a fresh-looking demonstration.

Use the one-page demo script. Rehearse first, then record one continuous production
run. Keep secrets out of the capture by recording only the application window and
using the separate phone/private window for sensitive setup. If secrets appear,
discard the take and start over; do not publish them or splice the recording.
Watch the entire recording, confirm all steps and audio are readable, record its
duration and evidence location, and submit it through the agreed Vikram review
channel. No review recipient has been supplied and no review message is sent here.
