# Franklin Fall 2026 policy import

Run after deploying the command, from the production backend directory:

```bash
./venv/bin/python manage.py import_franklin_fall2026 --approved-by "Ammar (production operator)"
```

Running this command records the named operator's approval of only
`https://www.franklin.edu/tuition-fee-changes-fall-2026` for Franklin's Money
knowledge group. Use the actual operator name. It fetches the page using the
shared SSRF/redirect/response-size policy, checks the final URL and policy
sections, and saves the HTML, extracted text, hashes, fetch time and approval.
Verbatim text chunks are stored as scraped evidence; no generated claims or
human-verified answers are inserted. Existing school sources are not deleted.

An identical rerun reuses the import record and knowledge entries. Changed
content creates a new source snapshot; existing evidence remains available for
review. A non-200 response, unexpected redirect or challenge page stops the
import without writing knowledge. Do not bypass those failures or paste search
snippets as if the page had been fetched.

The output must say `loaded; retest Question 5` and list persisted knowledge IDs.
Restart the Kormic workers after import, then test in a fresh conversation.
Verify that the answer cites this notice, distinguishes new and existing
students, preserves the guarantee duration and separates fees from tuition.
Successful import alone does not prove the production answer is correct.

`refresh_franklin_gate2.py` replays the original six-source snapshot and does
not import this additional page. No migration is required for this command.
