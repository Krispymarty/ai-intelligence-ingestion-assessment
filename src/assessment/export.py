import csv
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from .contract import Job, News, TARGETS
from .store import Store
from .workbook import build_workbook

COMMON = ["schemaVersion", "recordType", "id", "source.name", "source.url", "collectedAt"]
TABS = {
    "STARTUP": ("Startups", ["content.entityName", "content.domain", "content.data.employeeCount"]),
    "PRODUCT": ("Products", ["content.productName", "content.startupName", "content.pricingModel", "content.productUrl"]),
    "RESEARCH_PAPER": ("Research Papers", ["content.title", "content.authors", "content.paper_url",
        "content.github_url", "content.github_stars", "content.github_stars_observed_at",
        "content.published_date", "content.repository_evidence_url"]),
    "JOB": ("Jobs", ["content.title", "content.company", "content.date", "content.is_remote",
        "content.role_family", "content.excerpt"]),
    "NEWS": ("News", ["content.title", "content.date", "content.excerpt"]),
}
MAPPINGS = ["source_url", "raw_name", "canonical_name", "reason"]


def field(body: dict, column: str):
    value = body
    for part in column.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def cell(value):
    if isinstance(value, (dict, list, bool)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export(store: Store, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    cutoff = store.latest_cutoff()
    as_of = datetime.fromisoformat(cutoff) if cutoff else None
    counts: dict[str, int] = {}
    coverage: dict[str, dict[str, int]] = {}
    for kind, (title, fields) in TABS.items():
        populated: Counter[str] = Counter({k: 0 for k in fields})
        count = 0
        with (output / f"{title}.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            columns = COMMON + fields + ["evidence"]
            writer.writerow(columns)
            for record in store.records(kind):
                if isinstance(record, (Job, News)) and (
                    as_of is None or not as_of - timedelta(hours=24) <= record.content.date <= as_of
                ):
                    continue
                body = record.model_dump(mode="json")
                writer.writerow([cell(field(body, column)) for column in columns])
                populated.update(k for k in fields if field(body, k) not in (None, "", []))
                count += 1
        counts[kind] = count
        coverage[kind] = dict(populated)
    with (output / "Entity Mapping Log.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(MAPPINGS)
        mapping_count = 0
        for entry in store.mappings():
            data = entry.model_dump(mode="json")
            writer.writerow([cell(data.get(name)) for name in MAPPINGS])
            mapping_count += 1
    report = {
        "mode": store.mode, "monitor_as_of": cutoff, "counts": counts, "mapping_count": mapping_count,
        "targets": TARGETS, "shortfalls": {k: max(0, v - counts[k]) for k, v in TARGETS.items()},
        "field_coverage": coverage, "sources": store.results(), "unverified_publication": store.discoveries(),
    }
    (output / "run-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    build_workbook(output)
    return report
