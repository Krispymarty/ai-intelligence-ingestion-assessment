import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from .bulk import KINDS, Bulk
from .catalog import collect_catalog
from .export import export
from .llm import Extractor, configured_providers
from .model_offers import collect_model_offers
from .identity import Resolver
from .network import FetchError, Fetcher
from .products import collect_apps, enrich_pricing
from .contract import SourceResult
from .store import Store


def positive(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return result


def cutoff(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is not None and "T" in value:
            return result.astimezone(UTC)
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("expected an ISO-8601 timestamp with time zone")


async def ingest(args: argparse.Namespace, store: Store) -> int:
    fetch = Fetcher(args.fixture)
    resolver = Resolver()
    try:
        if args.command == "backfill":
            verticals = KINDS if "all" in args.vertical else dict.fromkeys(args.vertical)
            results = []
            if args.product_source is not None and set(verticals) != {"products"}:
                raise ValueError("--product-source requires --vertical products")
            if args.product_source is None:
                results = await Bulk(fetch, store, resolver).run(verticals, args.limit, args.max_pages, args.paper_links)
            for source, name in (("global", "Global AI Product Index"), ("tiorai", "TiorAI"), ("costkite", "CostKite")):
                if "products" in verticals and args.product_source in {None, source}:
                    try:
                        results.append(await collect_catalog(fetch, store, resolver, args.limit, source))
                    except (FetchError, ValueError) as exc:
                        results.append(SourceResult(source=name, status="failed", details=[str(exc)]))
            if "products" in verticals and args.product_source in {None, "pricing"}:
                try:
                    results.append(await enrich_pricing(fetch, store, resolver, args.limit))
                except (FetchError, ValueError) as exc:
                    results.append(SourceResult(source="AGIPicks pricing", status="failed", details=[str(exc)]))
            if "products" in verticals and args.product_source in {None, "apple"}:
                providers = configured_providers() if args.llm_app_store and fetch.fixture is None else []
                engine = Extractor(fetch.client, providers, args.llm_budget) if providers else None
                results.append(await collect_apps(fetch, store, resolver, args.limit, args.max_pages, engine))
            if "products" in verticals and args.product_source == "model-apis":
                results.append(await collect_model_offers(fetch, store, resolver, args.limit))
            as_of = fetch.now()
        else:
            from .monitor import Monitor

            as_of = args.as_of or fetch.now()
            if as_of > fetch.now():
                raise ValueError("monitor cutoff cannot be in the future")
            results = await Monitor(fetch, store, resolver, args.llm_budget,
                                    args.full_text_host, args.llm_host, args.max_pages).run(as_of, args.source)
        store.save_results(args.command, as_of.isoformat(), results)
        for result in results:
            logging.info("%s status=%s fetched=%s eligible=%s undated=%s", result.source, result.status,
                         result.fetched, result.eligible, result.undated)
            for detail in result.details:
                logging.warning("%s: %s", result.source, detail)
        return 2 if any(r.status != "ok" for r in results) else 0
    finally:
        await fetch.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="ai-assessment")
    parser.add_argument("--db", type=Path, default=Path("state/assessment.sqlite"))
    parser.add_argument("--fixture", type=Path, help="offline replay file; requires a separate database")
    commands = parser.add_subparsers(dest="command", required=True)
    bulk = commands.add_parser("backfill")
    bulk.add_argument("--vertical", action="append", choices=["all", *KINDS])
    bulk.add_argument("--limit", type=positive, default=1000, help="target total unique records per vertical")
    bulk.add_argument("--max-pages", type=positive, default=100)
    bulk.add_argument("--paper-links", type=Path, help="local Papers with Code archive JSON or JSON.gz")
    bulk.add_argument("--product-source", choices=["apple", "pricing", "global", "tiorai", "costkite", "model-apis"],
                      help="run a single product source")
    bulk.add_argument("--llm-app-store", action="store_true", help="send permitted app descriptions to the configured LLMs")
    bulk.add_argument("--llm-budget", type=int, choices=range(0, 31), metavar="0..30", default=10)
    monitor = commands.add_parser("monitor")
    monitor.add_argument("--as-of", type=cutoff)
    monitor.add_argument("--max-pages", type=positive, default=40)
    monitor.add_argument("--llm-budget", type=int, choices=range(0, 31), metavar="0..30", default=10)
    monitor.add_argument("--full-text-host", action="append", default=[], help="allowlisted host for permitted full-text extraction")
    monitor.add_argument("--llm-host", action="append", default=[], help="allowlisted host whose text may be sent to configured LLMs")
    output = commands.add_parser("export")
    output.add_argument("--output", type=Path, default=Path("exports"))
    output.add_argument("--postgres", action="store_true")
    enqueue = commands.add_parser("enqueue", help="queue one PostgreSQL task per source or bulk vertical")
    enqueue.add_argument("kind", choices=["monitor", "backfill"])
    enqueue.add_argument("--run-id", required=True)
    enqueue.add_argument("--as-of", type=cutoff)
    enqueue.add_argument("--limit", type=positive, default=1000)
    enqueue.add_argument("--max-pages", type=positive, default=200)
    work = commands.add_parser("worker", help="process PostgreSQL tasks until stopped")
    work.add_argument("--once", action="store_true")
    work.add_argument("--full-text-host", action="append", default=[])
    work.add_argument("--llm-host", action="append", default=[])
    work.add_argument("--llm-budget", type=int, choices=range(0, 31), metavar="0..30", default=10)
    work.add_argument("--paper-links", type=Path)
    monitor.set_defaults(source=None)
    args = parser.parse_args()
    if args.command == "backfill" and not args.vertical:
        args.vertical = ["all"]
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    mode = "export" if args.command == "export" else "replay" if args.fixture else "live"
    try:
        if args.command in {"enqueue", "worker"} or getattr(args, "postgres", False):
            if args.fixture:
                raise ValueError("--fixture requires a separate local replay database")
            from .postgres import PostgresStore, postgres_dsn
            from .worker import run_distributed

            dsn = postgres_dsn()
            if args.command in {"enqueue", "worker"}:
                return asyncio.run(run_distributed(dsn, args, ingest))
            store = PostgresStore(dsn, "export")
        else:
            store = Store(args.db, mode, create=mode != "export")
        try:
            if mode == "export":
                print(json.dumps(export(store, args.output)["counts"]))
                return 0
            return asyncio.run(ingest(args, store))
        finally:
            store.close()
    except (OSError, ValueError) as exc:
        logging.error("%s", exc)
        return 1
