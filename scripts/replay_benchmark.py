import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from pydantic import HttpUrl

from assessment.contract import (
    Job, JobContent, News, NewsContent, Page, Paper, PaperContent, Product, ProductContent,
    Record, Source, Startup, StartupContent,
)
from assessment.store import Store

KINDS = ("STARTUP", "PRODUCT", "RESEARCH_PAPER", "JOB", "NEWS")
STAMP = datetime(2026, 11, 21, 12, tzinfo=UTC)
SOURCE = Source(name="Synthetic benchmark", url=HttpUrl("https://example.org/benchmark"))


def record(i: int) -> Record:
    common = {"id": f"benchmark:{i}", "source": SOURCE, "collectedAt": STAMP}
    name = f"Fixture {i}"
    link = SOURCE.url
    match i % len(KINDS):
        case 0:
            return Startup(**common, content=StartupContent(entityName=name))
        case 1:
            return Product(**common, evidence={"pricing": "synthetic offer"},
                           content=ProductContent(productName=name, startupName="Fixture", pricingModel="FREE", productUrl=link))
        case 2:
            return Paper(**common, content=PaperContent(
                title=name, authors=["Fixture author"], paper_url=link, github_url=link, github_stars=0,
                github_stars_observed_at=STAMP, published_date=STAMP, repository_evidence_url=link,
            ))
        case 3:
            return Job(**common, content=JobContent(
                title=name, date=STAMP, company="Fixture", is_remote=True, role_family="Engineering",
            ))
        case _:
            return News(**common, content=NewsContent(title=name, date=STAMP))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--records", type=int, default=500_000)
    args = parser.parse_args()
    if args.db.exists() or not (5 <= args.records <= 500_000):
        parser.error("use a new replay database and 5–500000 records")
    store = Store(args.db, "replay")
    try:
        started = perf_counter()
        for start in range(0, args.records, 1000):
            end = min(start + 1000, args.records)
            store.commit_page("benchmark", str(end), Page(records=[record(i) for i in range(start, end)]))
        elapsed = perf_counter() - started
        # Model validation runs again when data is read, as it does for a submission export.
        read_started = perf_counter()
        counts = {kind: sum(1 for _ in store.records(kind)) for kind in KINDS}
        validation_seconds = perf_counter() - read_started
        store.commit_page("benchmark", str(args.records), Page(records=[record(i) for i in range(100 if args.records >= 100 else args.records)]))
        assert sum(counts.values()) == args.records
        assert {kind: store.count(kind) for kind in KINDS} == counts
        print(json.dumps({
            "mode": store.mode, "counts": counts, "ingest_seconds": round(elapsed, 2),
            "validated_records_per_second": round(args.records / elapsed, 2),
            "revalidation_seconds": round(validation_seconds, 2), "replayed_rows": min(100, args.records),
        }, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
