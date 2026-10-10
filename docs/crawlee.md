# University crawling with Crawlee

All discovery jobs now use Crawlee 1.10.4's BasicCrawler scheduler. The existing
discovery API, university domain policy, link classification, SQL checkpoints,
source selection and knowledge/form extraction remain in the pipeline:

Website → bounded discovery → saved HTML → source selection → extraction → review.

The scheduler uses three concurrent requests by default, two retries per failed
request, and at most 60 scheduled requests per minute. Every run has a unique
in-memory Crawlee queue; Django discovery rows are the durable resume checkpoint.
Completed pages are retained when resuming an interrupted job. The page budget
applies to each run, including full-site mode (default 150; API maximum 400).
At most three sitemap files and 5,000 discovered candidates are admitted by
default. These limits prevent large sitemap partitions from dominating a job.
Existing robots.txt, domain, redirect, TLS and response-size checks still apply.
If the homepage fails, a small set of conventional admissions, academics,
programs, tuition and sitemap paths is tried within the same page budget.
The document limit defaults to 10 MiB and is configurable with the job's
`max_response_mb` setting, capped at 16 MiB. Oversized pages are reported as
failed instead of silently truncating evidence; permanent policy failures are
not retried.

Thin HTML shells containing scripts use Playwright Chromium. Ordinary HTML does
not incur browser startup. Browser documents/frames remain in university scope;
public CDN resources may cross domains, with public-IP checks on every redirect.
Service workers, WebSockets, non-GET/HEAD requests, media and private destinations
are blocked. Rendering is bounded to 80 resource requests, 10 MiB per response and
20 MiB of fulfilled resource bodies. It is not an anti-bot bypass and cannot
guarantee extraction from every JavaScript application.

Saved HTML (including rendered HTML) is reused for extraction for 24 hours, scoped
to the university. The usual structured-information and fact-extraction code
still processes it. Extraction does not independently render previously unseen
URLs: run discovery first. PDFs continue through the existing document pipeline.

`coverage.status` is `partial` when limits, failed requests or unvisited records
remain. `completed` means the crawl run ended, not that all university facts are
present or reviewed. The source page displays this distinction. Existing source
approval and manual review behavior is unchanged; auto-apply still uses the
configured source selection policy.

## Deploy

Back up the database and deploy the backend and frontend commits together.
In the backend virtual environment:

```sh
pip install -r requirements.txt
python -m playwright install --with-deps chromium
python manage.py migrate
python manage.py check
```

Install Chromium as the service account running Celery, or configure a shared
`PLAYWRIGHT_BROWSERS_PATH` readable by that account. Restart the backend and Celery
workers after installation and migration. The Dockerfile installs Chromium and
its OS dependencies into `/opt/playwright` before switching to the app user;
rebuild the image to deploy. A browser-install failure affects JS fallback jobs
and must not be mistaken for successful extraction.

For a bounded crawl plus extraction using the management command:

```sh
python manage.py scrape_full_university UNIVERSITY_UUID --max-pages 150
```

The command reports crawl coverage separately from extraction. Interrupted jobs
can be resumed using `--resume JOB_ID` after their worker has stopped and the
heartbeat guard expires. Review extracted records and failed URLs afterward.

## Verify

```sh
python manage.py test url_discovery knowledge.test_scraper_security
KORMIC_BROWSER_TEST=1 python -m unittest url_discovery.test_browser_fetch
```

The second command launches real Chromium against intercepted fixture responses;
it requires installed browser binaries and does not crawl a live site.
