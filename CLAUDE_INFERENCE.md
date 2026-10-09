# Claude inference and algorithmic website collection

All active generative inference paths use Claude: student and university agents, legacy Anthropic-shaped agents, GitHub analysis, resume/LinkedIn/document analysis and verification. A local-only argument retained by older callers does not dispatch to Ollama. Claude failure is reported or queued; there is no local model fallback.

The default is `claude-haiku-4-5-20251001`, selected centrally with `KORMIC_CLAUDE_MODEL`. Existing `ANTHROPIC_API_KEY` is used. No credentials belong in this document.

## Cost controls

- Router output allowances: routing 512, ordinary replies 2,400, evidence 3,200, documents 4,000 tokens.
- Legacy SDK calls retain smaller requested limits and are capped at 4,000 output tokens.
- Estimated input maximum: 32,000 tokens via `KORMIC_CLAUDE_MAX_INPUT_TOKENS`. Oversized inputs must be retrieved in smaller stages; no silent source truncation by the model transport.
- SDK transport retries are disabled. The main router permits one validation repair; explicit single-attempt research permits none. Existing queue and rate controls remain.
- Long system prefixes request five-minute Anthropic prompt caching. Actual eligibility and cache hits are recorded by the provider; short prompts are not padded.
- GitHub defaults: 40 model calls / 200,000 reserved tokens per sync, six decisions per repository, three sync requests per day. These are bounds, not expected consumption or a dollar cap.
- SDK and LangChain requests report actual provider usage to existing audit telemetry when available. GitHub additionally emits `GITHUB_INFERENCE_USAGE`, linked to the sync run and student, with provider request ID, prompt character count, and actual input/output/cache tokens (including invalid paid responses). This diagnostic event must not be added to `MODEL_USAGE` totals a second time. Missing usage is marked unavailable, never zero. Do not equate reserved tokens with billed tokens.

## GitHub grouped analysis

- Connecting GitHub collects all accessible repository names without Claude. Students search the list and explicitly select one to five repositories before starting analysis. Ownership and the five-repository limit are enforced by the API and worker. Repository reports, portfolio summaries and student assessments use only that selection.
- Completed student profiles have no analyse-again control. Refreshing the displayed profile only reads saved results. An agent request can reuse a prior explicit selection, but cannot choose repositories on the student's behalf.
- Initial file discovery, a dependency/implementation sample and contribution lookup run algorithmically. Claude receives the source excerpts and available paths together.
- Explicit generated assets, build/dependency directories and known lockfiles are excluded. Dependency manifests and implementation/test sources remain eligible. The initial sample stays at up to three files; the existing individual investigation can request more when findings are uncertain or invalid.
- Prompt JSON is compact. Identical source excerpts within one repository are sent once, with reference IDs for duplicates; each path and evidence ID is retained. Reference IDs never cross repositories. Source URLs, commit URL lists and provider bookkeeping are omitted from reasoning prompts but retained in saved evidence/reports. No additional source truncation is used to save tokens.
- Up to three repositories share one bounded Claude request. Oversized groups split without truncating evidence. Every result is validated against that repository's evidence IDs before saving.
- Missing, invalid or insufficient findings fall back to the existing durable per-repository tool investigation, using the evidence already collected. Existing source/file limits and final student assessment contracts remain.
- Paid grouped results are persisted before consumption; queue resumption does not deliberately repeat completed requests. Existing in-progress legacy investigations resume their original checkpoints.
- Unchanged commit reports remain reusable. The portfolio overview and general development suggestions are now assembled algorithmically from saved findings, preserving project summaries, technology/domain counts, contribution caveats and academic guidance. There is no Claude overview/outline call. Changed findings immediately appear in the next finalized overview without inference.
- This is immediate request grouping, not Anthropic's asynchronous Batch API. No waiting period or different billing tier was introduced. Incremental changed-file analysis is not enabled: changed commits still receive fresh evidence investigation.
- For five small selected repositories, the fast path uses two analysis requests total, down from two analysis requests plus an outline call. Extra evidence requests, oversized-group splits and validation recovery add calls. Actual token savings must be measured on real repositories; call counts alone do not establish billing savings or equivalent analysis quality.

## No model calls for website scraping

Full-site discovery, URL ranking, HTML/PDF fetching, named DOM records, and source-text fact extraction are algorithmic. Public research page selection and catalogue extraction also avoid inference. Unknown fields remain empty; source excerpts retain information the structural parser cannot map. Website parsing caches are versioned separately from old Qwen extraction caches. An explicit *reasoning/research answer* about evidence can use Claude; fetching or extracting the page itself does not.

The full crawl resumes with `python manage.py scrape_full_university <uuid> --resume <job-id>`. Existing manual/verified corrections remain protected. A malformed source URL is skipped rather than failing the entire crawl.

## Student chat cost controls

- A complete, explicit `Compare <institution> and <institution> for <criteria>` request can bypass paid intent classification. Each name must include University, College or Institute, and the original full question still reaches the existing directory resolution and separate university advisers. Ambiguous names, compound actions, attachments and document tasks retain ordinary routing.
- The classifier schema is sent once by the router. Conversation, tool-result and university-adviser data use compact JSON without deleting values or links.
- Exact duplicate evidence records are removed before the existing ranking/record limits, so duplicate rows cannot crowd out distinct facts. Different sources, dates, amounts and verification states are preserved. Tool JSON is never cut mid-record; the model context guard remains active.
- Student chat already avoids a second rewrite of completed university answers and duplicate execution of identical tools within a turn. These protections remain. Adviser validation, one bounded correction attempt, citations, officer clarification and fresh evidence retrieval for subsequent turns are unchanged.
- A straightforward three-university comparison can use three adviser calls instead of one routing call plus three adviser calls. Ten such requests can therefore use 30 calls instead of 40, assuming available evidence and no repair or research calls. Two-university comparisons can use 20 calls across ten requests. Routing calls are smaller, so the percentage reduction in calls is not the percentage reduction in the bill. Use actual `MODEL_USAGE` telemetry for input/output/cache billing estimates; the old 460k/61k token example was an assumption, not a benchmark.

## Further student-chat savings

Standalone saved-profile reviews and improvement questions use conservative whole-message routing, then read fresh profile/source evidence before answering. They can use one answer call instead of classification plus answer. Attachments, named institutions, compound requests, updates and ambiguous follow-ups keep the existing paths. The general-definition allowlist is broader and recognized standalone definitions receive a focused explanatory prompt without unrelated profile and university workflow instructions. Answer validation and output allowances remain unchanged.

Student and university adviser prompts share identical long source strings within each individual tool result using explicit local references. The original saved tool results and validator evidence remain untouched. Each URL, date, verification flag, unknown value and conflicting record is preserved; only byte-identical text is shared. Packing is used only when the complete representation is at least 10% smaller in UTF-8 bytes. This is not a token-savings guarantee. References never cross tool results or universities. Visual payloads and non-JSON results stay unchanged.

University comparisons retain a separately scoped adviser for each institution and fresh retrieval on every turn. Their call count is unchanged by this optimization; input savings depend on actual duplicated source content. No shared answer cache, lower model tier, reduced output allowance or shortened evidence was introduced. Monthly dollar savings cannot be calculated reliably from the earlier assumed usage table without provider measurements.

## University admin chat cost controls

University officer chat also skips the initial model-selected read for clear, single-topic requests. It still authenticates the officer and executes the existing scoped read tools. Explicit edits can preload the appropriate validated proposal schema, avoiding a separate enable-tool model round. Bare confirmation messages only trigger a live proposal-status read; no proposal is automatically created or approved. Exact-message, later-turn approval, completeness, stale-version checks and knowledge-index updates remain enforced by the existing tools. Compact JSON preserves all evidence and paginated retrieval. Straightforward reviews can use one answer call; preparing a complete edit can use two calls (proposal plus final explanation). Ambiguous tasks, additional evidence pages, missing terms and repairs may require more. The earlier 30-call/$0.405 example was assumed, not measured billing.

## Small-workflow cost controls

- Complete greetings and thanks use local responses. Strictly recognized standalone concept questions skip classification but still receive a Claude answer. Personalized, compound and uncertain requests retain model routing. Attachments and pending actions take precedence.
- Standalone reviews of one uploaded document automatically execute the existing ownership-checked reader. Text PDFs are parsed locally, then Claude receives the full extracted evidence and a focused review prompt. No paid tool-selection or finish-review step is needed. Scanned sources retain visual analysis. Profile updates still require proposals and later confirmation; review-only requests do not save facts.
- Resume inspection, reading, extraction, validation and finalization run in their established dependency order. Claude performs structured extraction once on the normal path, rather than deciding each fixed step. All schema fields, source validation, trace entries and scanned-PDF handling remain. Validation repairs can add calls.
- LinkedIn text exports use local sequencing of all required chunks, keeping section extraction, validation and merging. Screenshots retain model-directed OCR recovery. Multiple sections/chunks and repairs can still require multiple extraction calls; LinkedIn is not universally a one-call task.
- JSON schemas are serialized compactly and supplied once. No source fields or answer allowances were removed for these savings.
- Arbitrary dependent tool tasks retain the agent loop: later arguments may depend on earlier results. Only known fixed workflows bypass paid orchestration. Three genuinely model-dependent tool steps may still need four Claude calls.

Verified with mocked provider responses and real local parsing/database operations: a recognized concept question uses one answer call; a standalone text-PDF review uses one answer call; a valid resume uses one extraction call; a single-section LinkedIn text extraction skips all controller calls. These are call-count regression tests, not live-model quality evaluations or measured invoices. The earlier token/cost tables were assumptions. Actual tokens, cache hits and repairs must be read from provider usage before reporting a new bill.

## Expected token use per completed task: high to low

1. Multi-repository GitHub analysis: several source reads and model/tool rounds per repository.
2. Complex student advice/comparison across universities: retrieval, multiple institutions, follow-up reasoning, final synthesis.
3. University officer assistant tasks: long evidence/tool context and multi-step edits or checks.
4. Long document or image/PDF analysis: large input, usually fewer requests than agent workflows.
5. Resume and LinkedIn extraction/profile presentation: bounded structured input/output; depends on document length.
6. Short questions, clarification and routing: short context and concise output, often a single request.
7. Website crawling/scraping/extraction: zero Claude tokens.

This is a workload estimate, not measured billing. Monthly totals depend on frequency, history length, document size, repairs, and cache hits. There is no monthly dollar cap configured.
