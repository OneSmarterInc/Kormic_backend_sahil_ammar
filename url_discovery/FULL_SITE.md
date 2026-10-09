# Complete university website ingestion

Use the existing application pipeline to create the university and its account first. Then run:

```powershell
python manage.py scrape_full_university UNIVERSITY_UUID
```

This command creates a full-site discovery job through `start_discovery`, crawls reachable
public HTML and document links across the university domain and its subdomains, and sends
each successful source through `apply_selected_urls` and `scrape_selected_urls`. It does not
seed university facts or bypass the knowledge-base pipeline. Existing manual and
human-verified entries remain authoritative.

Full-site mode has no curated top-N, page-count or relevance/depth cutoff. URL deduplication,
domain checks, robots rules, excluded login/admin paths, fetch size limits and crawl delays
remain active. Unreachable, robots-blocked, authenticated and unsupported binary content
cannot be extracted; discovery failures are recorded rather than represented as knowledge.
PDFs with a text layer are supported. Scanned PDFs need OCR, which this pipeline does not provide.

Extraction runs incrementally while discovery continues. Long documents are split into
overlapping bounded inputs. Structured facts require source excerpts; failed extraction
retains actual page text at lower confidence. No TOTP enrollment is performed by this command.

Progress is stored in `DiscoveryJob.scrape_result`, and discovered URLs are durable. After
stopping a job through the existing discovery stop action, resume with:

```powershell
python manage.py scrape_full_university UNIVERSITY_UUID --resume JOB_ID
```

A crashed worker's heartbeat must expire (10 minutes) before resuming. Use `--reextract`
after an extraction fix to process successful discovered sources again. Stop the old
worker before starting a replacement. The standalone command avoids the short Celery
task limits intended for curated discovery jobs. Keep its process running until both
discovery and extraction finish; a large university website can take many hours.


### Fixed university information forms

`knowledge.scraper.fetch_page` sends cleaned source DOM through `universities.structured_information` during normal extraction. The adapter creates named courses, scholarships, housing options, and fee schedules with field evidence. Generic headings remain source knowledge, not form records. `/api/university-admin/information/entities/` serves typed records; `/information/` continues serving all source information. Manual and verified corrections take precedence on subsequent scrapes. Multiple source pages for the same named entity are merged without replacing population-specific criteria.

Backfill existing discovered pages through the same source adapter (no hand-seeded facts):

```powershell
python manage.py autofill_university_information <university-uuid> --watch
```

The optional watch mode also processes newly discovered pages while a full-site discovery job is active. Unknown fields remain blank. Form collections and labels are fixed independently of source headings.
