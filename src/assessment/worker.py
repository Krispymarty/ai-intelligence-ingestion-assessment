import asyncio
import logging
from argparse import Namespace
from contextlib import suppress
from datetime import UTC, datetime

from .bulk import KINDS
from .monitor import FEEDS
from .postgres import LeaseLost, PostgresStore, Work


async def heartbeat(store: PostgresStore, task: Work):
    while True:
        await asyncio.sleep(20)
        if not await asyncio.to_thread(store.renew, task):
            raise LeaseLost(f"could not renew task lease: {task.key}")


async def run_distributed(dsn: str, args: Namespace, ingest):
    store = PostgresStore(dsn)
    try:
        if args.command == "enqueue":
            now = datetime.now(UTC)
            as_of = args.as_of or now
            if as_of > now:
                raise ValueError("queued cutoff cannot be in the future")
            partitions = [f.name for f in FEEDS] if args.kind == "monitor" else list(KINDS)
            inserted = store.enqueue(args.run_id, args.kind, partitions, as_of.isoformat(),
                                     row_limit=args.limit, page_limit=args.max_pages)
            print(f"Queued {inserted} tasks.")
            return 0
        lease_store = PostgresStore(dsn)
        try:
            while True:
                task = store.claim()
                if not task:
                    if args.once:
                        return 0
                    await asyncio.sleep(3)
                    continue
                store.lease = task
                success = False
                config = Namespace(
                    command=task.kind, fixture=None, vertical=[task.partition], limit=task.row_limit, max_pages=task.page_limit,
                    paper_links=args.paper_links, product_source=None, llm_app_store=False,
                    source=task.partition, as_of=datetime.fromisoformat(task.as_of),
                    full_text_host=args.full_text_host, llm_host=args.llm_host, llm_budget=args.llm_budget,
                )
                renewal = asyncio.create_task(heartbeat(lease_store, task))
                work = asyncio.create_task(ingest(config, store))
                try:
                    done, _ = await asyncio.wait({work, renewal}, return_when=asyncio.FIRST_COMPLETED)
                    if renewal in done:
                        await renewal
                    success = await work == 0
                except (ValueError, OSError) as exc:
                    logging.error("task %s: %s", task.key, exc)
                finally:
                    work.cancel()
                    renewal.cancel()
                    await asyncio.gather(work, renewal, return_exceptions=True)
                    if not store.finish(task, success):
                        logging.warning("task %s lease is no longer current", task.key)
                    store.lease = None
                if args.once:
                    return 0 if success else 2
        finally:
            with suppress(Exception):
                lease_store.close()
    finally:
        store.close()
