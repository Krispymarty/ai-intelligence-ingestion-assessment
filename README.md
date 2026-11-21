# AI Intelligence Ingestion

An asynchronous Python pipeline that turns public AI startup, application, research, news, and job data into validated, traceable spreadsheet rows. The local submission format is six CSVs, a six-tab workbook, and a JSON run report. See [architecture.pdf](architecture.pdf) for the production design and [DECISIONS.md](DECISIONS.md) for assessment interpretations.

## Setup

Install Python 3.13 and uv. From this directory:

~~~powershell
uv sync --locked --python 3.13
uv run --locked ai-assessment --help
~~~

The pipeline reads GitHub credentials from the process environment and LLM credentials from the process environment or the ignored local .env file. Environment values take precedence. Set a free GitHub token in GITHUB_TOKEN before a 1,000-paper run. Authenticated requests batch public star observations through GitHub GraphQL with REST for unresolved links. Quota failures retain the checkpoint.

The LLM order is Groq (default qwen/qwen3.8-27b), Gemini (default gemini-2.5-flash), then Cerebras if configured. The accepted key names are GROQ_API_KEY, GEMINI_API_KEY and CEREBRAS_API_KEY. Override a model with the corresponding _MODEL variable. Set CEREBRAS_MODEL to a JSON-capable free-tier model to enable the third tier. Do not put tokens in command history, source control, or the spreadsheet.

~~~powershell
uv run --locked ai-assessment backfill --vertical all --limit 25
uv run --locked ai-assessment backfill --vertical all --limit 1000 --max-pages 200
uv run --locked ai-assessment backfill --vertical products --product-source pricing
uv run --locked ai-assessment backfill --vertical products --product-source apple --max-pages 200
uv run --locked ai-assessment backfill --vertical products --product-source model-apis
uv run --locked ai-assessment monitor
uv run --locked ai-assessment export --output exports
~~~

The default database is state/assessment.sqlite. Use one ingest process per database. The limit is the target total per vertical. Rerun the command to resume. Separate processes may use different databases. For reproducible paper input, download the linked Papers with Code archive locally and pass --paper-links to backfill. To pin the signal window, pass monitor --as-of with an ISO-8601 UTC timestamp. A run returning exit 2 has source errors or partial source coverage. Check the JSON report even when exit is 0, since several sources may legitimately yield fewer records than the target.

Feeds that explicitly include a full-content field are retained locally for eligible records. Pass --full-text-host for permitted publisher page requests following robots.txt. The monitor reports pages, full text, first-seen items, changed content and partial coverage. An ambiguous publication date stays outside the fresh export. Pass --llm-host for permitted source text and --llm-app-store for permitted app descriptions. The default LLM budget is 10 calls per run and includes retries. Run the provider smoke test with uv run --locked python scripts/smoke_llm.py after configuring credentials.

## Sources and limits

| Vertical | Source and eligibility |
|---|---|
| Startups | [YC OSS snapshot](https://github.com/yc-oss/api), attributed to the original [YC directory](https://www.ycombinator.com/companies). Requires an AI signal from the source. Snapshot fingerprints protect resumed offsets from snapshot revisions. |
| Products | [Apple App Store Search](https://performance-partners.apple.com/search-api) contributes AI apps with company sellers. [Global AI Product Index](https://globalaiproductindex.com/dataset/) supplies company and business-model labels with vendor citations. [TiorAI](https://github.com/tioraicom/ai-tools-dataset) requires a unique company-domain match, and [CostKite](https://costkite.com/data) supplies company names and documented plans. These three catalogs are CC BY 4.0. [Hugging Face Spaces](https://huggingface.co/docs/hub/en/api), [Harpd](https://harpd.com/data/), and [AIToolBuzz](https://github.com/devadigax/aitoolbuzz-dataset) supply candidates. [AGIPicks](https://agipicks.com/data/) supplies dated pricing evidence. The workbook only counts products with a supported pricing enum. |
| Papers | [arXiv metadata](https://info.arxiv.org/help/bulk_data/index.html) joined to the [July 2025 Papers with Code archive](https://huggingface.co/datasets/pwc-archive/links-between-paper-and-code), distributed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Association evidence identifies the archive and repository. Stars are time-stamped observations from the [GitHub API](https://docs.github.com/en/rest/repos/repos#get-a-repository). The PDF and export report describe measured coverage separately from design targets. |
| News | TechCrunch AI, The Verge AI, Ars Technica AI, MIT Technology Review AI, and WIRED. Reads the exposed feed window. Exports attributed headlines, links and short feed excerpts. |
| Jobs | Remote OK, Jobicy, [Mo AIJobs](https://www.moaijobs.com/rss), We Work Remotely, and Arbeitnow. Mo AIJobs supplies a dated feed with full descriptions, replacing Himalayas whose public pubDate is an update time. Only publication timestamps are eligible. Full descriptions are retained locally. |

Access failures and source-side restrictions are recorded. Review attribution and reuse conditions before making the workbook public. To replace a failing source, replace the relevant Feed definition with a documented endpoint and update or add its adapter when its response format differs.

The reviewed workbook includes production model APIs. The explicit [OpenRouter catalog](https://openrouter.ai/docs/api/api-reference/models/get-models) step groups paid and free routes for the same stable model ID and omits preview and dated snapshot IDs. The model author and the scope of OpenRouter's hosted price are recorded separately. The report counts these API offers separately from the other AI products.

The Harpd website previously returned HTTP 403 from the collection environment. Its publisher distributes the same catalog through an explicitly published CC BY 4.0 GitHub export. The product adapter uses that export and retains the listing URL and attribution. A challenged page is recorded as inaccessible. Authorized JavaScript-rendered pages can be handled with an approved browser adapter or a publisher-provided feed. Respect authentication, robots rules, and publisher limits.

## Distributed operation and replay

Install the optional driver with uv sync --locked --extra postgres. Set ASSESSMENT_POSTGRES_DSN in the process environment to a database where the application can create its assessment schema. Queue one task per source or bulk vertical, then start workers against the same database.

~~~powershell
uv run --locked --extra postgres ai-assessment enqueue backfill --run-id bulk-1
uv run --locked --extra postgres ai-assessment enqueue monitor --run-id signals-1
uv run --locked --extra postgres ai-assessment worker
uv run --locked --extra postgres ai-assessment export --postgres --output exports
uv run --locked python scripts/replay_benchmark.py --db state/replay-500k.sqlite
~~~

The queue uses stable task keys, renewable leases and fenced commits. Repeating a run ID leaves its tasks unchanged. Workers serialize active tasks for a given source partition, while different partitions can progress in parallel. The local benchmark persisted and revalidated 500,000 synthetic records across the five schemas, with 100 replayed rows and no count changes. Ingestion took 20.38 seconds and revalidation 18.51 seconds on the development machine. These measurements exclude network and model calls.

## Review and handoff

Install the PostgreSQL extra for the type check, which includes the PostgreSQL adapter.

~~~powershell
uv sync --locked --python 3.13 --extra postgres
uv run --locked ruff check src tests scripts
uv run --locked --extra postgres pyright
uv run --locked --extra postgres pytest
uv run --locked python scripts/build_architecture.py
~~~

Fixtures exercise the CLI and provider HTTP interface. Use --fixture with a separate --db to keep test data isolated. Export includes a run report with mode, target counts, field coverage, source errors, and uncertain publication timestamps. Formula-like source values are escaped in the CSV and written as text in the workbook.

For submission, run the last monitor near the deadline, export from the live database, and import AI Assessment.xlsx into a Google Sheet. Verify the reader link in a signed-out browser. Push the source and PDF to GitHub. Review the actual counts and disclose any quota, source, or coverage shortfall when sending the two links through the supplied submission form. Keep local state and credentials out of Git. After submission, tag the submitted version and add scheduling, health metrics, and a full-size replay benchmark in the portfolio phase.

To refresh the reviewed workbook in the original workspace, use its existing state/live.sqlite database. A fresh clone starts with an empty local database.

~~~powershell
$submissionCutoff = [DateTimeOffset]::UtcNow.ToString("o")
uv run --locked ai-assessment --db state/live.sqlite monitor --as-of $submissionCutoff --max-pages 40 --full-text-host techcrunch.com --llm-host moaijobs.com
uv run --locked ai-assessment --db state/live.sqlite export --output exports/submission
~~~
