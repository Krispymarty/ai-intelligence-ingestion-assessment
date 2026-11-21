# AI Intelligence Ingestion

An asynchronous Python pipeline that turns public AI startup, application, research, news, and job data into validated, traceable spreadsheet rows. The local submission format is six CSVs, a six-tab workbook, and a JSON run report. See [architecture.pdf](architecture.pdf) for the production design and [DECISIONS.md](DECISIONS.md) for assessment interpretations.

## Setup

Install Python 3.13 and uv. From this directory:

~~~powershell
uv sync --locked --python 3.13
uv run --locked ai-assessment --help
~~~

The program reads credentials only from the process environment. Set a free, minimally scoped GitHub token in GITHUB_TOKEN before a 1,000-paper run. Authenticated requests batch public star observations through GitHub GraphQL and use REST for unresolved repository links. Without a token, the unauthenticated REST quota can stop the run after a partial batch. A permission or quota failure retains the checkpoint. For live LLM calls, configure a free, JSON-capable model and key for each provider via GEMINI_MODEL and GEMINI_API_KEY, GROQ_MODEL and GROQ_API_KEY, and CEREBRAS_MODEL and CEREBRAS_API_KEY. An unconfigured provider is skipped. Do not put tokens in command history, source control, or the spreadsheet.

~~~powershell
uv run --locked ai-assessment backfill --vertical all --limit 25
uv run --locked ai-assessment backfill --vertical all --limit 1000 --max-pages 200
uv run --locked ai-assessment monitor
uv run --locked ai-assessment export --output exports
~~~

The default database is state/assessment.sqlite. Use one ingest process per database. The limit is the target total per vertical. Rerun the command to resume. Separate processes may use different databases. For reproducible paper input, download the linked Papers with Code archive locally and pass --paper-links to backfill. To pin the signal window, pass monitor --as-of with an ISO-8601 UTC timestamp. A run returning exit 2 has source errors or partial source coverage. Check the JSON report even when exit is 0, since several sources may legitimately yield fewer records than the target.

Pass --full-text-host once per publisher domain for which you have full-text collection rights. This enables eligible article requests following robots.txt and stores extracted text locally. Pass --llm-host once per domain whose source text may be sent to the configured providers. The default LLM call budget is 10 per monitor run and counts every retry. Jobs without a grounded LLM result may still use source title evidence. Run the provider smoke test with uv run --locked python scripts/smoke_llm.py after configuring credentials.

## Sources and limits

| Vertical | Source and eligibility |
|---|---|
| Startups | [YC OSS snapshot](https://github.com/yc-oss/api), attributed to the original [YC directory](https://www.ycombinator.com/companies). Requires an AI signal from the source. Snapshot fingerprints protect resumed offsets from snapshot revisions. |
| Products | [Hugging Face Spaces](https://huggingface.co/docs/hub/en/api) with an eligible runtime, functional AI evidence, and a publisher matched through a company record or a seed alias and organization profile. [Harpd AI Datasets](https://harpd.com/data/) and [AIToolBuzz](https://github.com/devadigax/aitoolbuzz-dataset) provide supplemental entries with AI evidence and a company domain or an explicit vendor claim matched to a known company. Both catalogs are CC BY 4.0. AIToolBuzz activity is an October 2025 snapshot observation. Pricing stays blank unless supported. |
| Papers | [arXiv metadata](https://info.arxiv.org/help/bulk_data/index.html) joined to the [July 2025 Papers with Code archive](https://huggingface.co/datasets/pwc-archive/links-between-paper-and-code), distributed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Association evidence identifies the archive and repository. Stars are time-stamped observations from the [GitHub API](https://docs.github.com/en/rest/repos/repos#get-a-repository). The PDF and export report describe measured coverage separately from design targets. |
| News | TechCrunch AI, The Verge AI, Ars Technica AI, MIT Technology Review AI, and WIRED. Reads the exposed feed window. Exports attributed headlines, links and short feed excerpts. |
| Jobs | Remote OK, Jobicy, Himalayas, We Work Remotely, and Arbeitnow. Filters available feed results for AI roles and reports access and date limitations. Only original publication timestamps are eligible. Himalayas documents its public pubDate as last updated. No full job descriptions are included in the public export. |

Access failures and source-side restrictions are recorded. Review attribution and reuse conditions before making the workbook public. To replace a failing source, replace the relevant Feed definition with a documented endpoint and update or add its adapter when its response format differs.

## Review and handoff

~~~powershell
uv run --locked ruff check src tests scripts
uv run --locked pyright
uv run --locked pytest
uv run --locked python scripts/build_architecture.py
~~~

Fixtures exercise the CLI and provider HTTP interface. Use --fixture with a separate --db to keep test data isolated. Export includes a run report with mode, target counts, field coverage, source errors, and uncertain publication timestamps. Formula-like source values are escaped in the CSV and written as text in the workbook.

For submission, run the last monitor near the deadline, export from the live database, and import AI Assessment.xlsx into a Google Sheet. Verify the reader link in a signed-out browser. Push the source and PDF to GitHub. Review the actual counts and disclose any quota, source, or coverage shortfall when sending the two links through the supplied submission form. Keep local state and credentials out of Git. After submission, tag the submitted version and add scheduling, health metrics, and a full-size replay benchmark in the portfolio phase.
