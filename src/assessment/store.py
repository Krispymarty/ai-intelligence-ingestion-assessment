import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contract import RECORD, Mapping, Page, Record, SourceResult


class Store:
    def __init__(self, path: Path, mode: str, *, create: bool = True, connection: Any = None):
        self.db: Any
        if connection is None:
            if not create and not path.is_file():
                raise ValueError(f"database does not exist: {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path, timeout=30)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
        self.db = connection
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS by_kind ON records(kind);
            CREATE UNIQUE INDEX IF NOT EXISTS by_storefront_app ON records(json_extract(body,'$.evidence.storefront_app_id'))
                WHERE kind='PRODUCT' AND json_extract(body,'$.evidence.storefront_app_id') IS NOT NULL;
            CREATE TABLE IF NOT EXISTS checkpoints (source TEXT PRIMARY KEY, cursor TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mapping (url TEXT, raw TEXT, body TEXT, PRIMARY KEY(url, raw));
            CREATE TABLE IF NOT EXISTS product_mapping (url TEXT, raw TEXT, body TEXT, PRIMARY KEY(url, raw));
            CREATE TABLE IF NOT EXISTS repos (slug TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS discovery (url TEXT PRIMARY KEY, first_seen TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS full_text (url TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS field_results (
                work_key TEXT PRIMARY KEY, token TEXT NOT NULL, expires_at TEXT NOT NULL, result TEXT
            );
            CREATE TABLE IF NOT EXISTS observations (
                url TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                content_hash TEXT NOT NULL, last_changed TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                seq INTEGER PRIMARY KEY, command TEXT NOT NULL, as_of TEXT NOT NULL,
                source TEXT NOT NULL, body TEXT NOT NULL
            );
        """)
        with self.db:
            if mode != "export":
                self.db.execute("INSERT INTO meta VALUES ('mode', ?) ON CONFLICT DO NOTHING", (mode,))
                if self.mode != mode:
                    self.db.close()
                    raise ValueError("live and replay data must use separate databases")

    @property
    def mode(self) -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
        return str(row[0]) if row else "unknown"

    def close(self):
        self.db.close()

    def cursor(self, source: str) -> str | None:
        row = self.db.execute("SELECT cursor FROM checkpoints WHERE source=?", (source,)).fetchone()
        return str(row[0]) if row else None

    def commit_page(self, source: str, cursor: str | None, page: Page):
        with self.db:
            self.assert_write_lease()
            self.db.executemany(
                """INSERT INTO records VALUES (?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET body=excluded.body
                   WHERE records.kind != 'PRODUCT'
                      OR json_extract(records.body,'$.content.pricingModel') IS NULL
                      OR json_extract(excluded.body,'$.content.pricingModel') IS NOT NULL""",
                ((r.id, r.recordType, r.model_dump_json()) for r in page.records),
            )
            for entity, table in (("COMPANY", "mapping"), ("PRODUCT", "product_mapping")):
                self.db.executemany(
                    f"""INSERT INTO {table} VALUES (?, ?, ?)
                       ON CONFLICT(url, raw) DO UPDATE SET body=excluded.body""",
                    ((str(m.source_url), m.raw_name, m.model_dump_json()) for m in page.mappings if m.entity_type == entity),
                )
            self.db.executemany(
                "INSERT INTO discovery VALUES (?, ?) ON CONFLICT DO NOTHING",
                ((url, ts.isoformat()) for url, ts in page.undated.items()),
            )
            self.db.executemany(
                "INSERT INTO full_text VALUES (?, ?) ON CONFLICT(url) DO UPDATE SET body=excluded.body",
                page.full_text.items(),
            )
            self.db.executemany(
                """INSERT INTO observations VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET last_seen=excluded.last_seen,
                       content_hash=excluded.content_hash,
                       last_changed=CASE WHEN observations.content_hash != excluded.content_hash
                           THEN excluded.last_seen ELSE observations.last_changed END
                   WHERE excluded.last_seen >= observations.last_seen""",
                ((url, stamp.isoformat(), stamp.isoformat(), digest, stamp.isoformat())
                 for url, (stamp, digest) in page.observations.items()),
            )
            if cursor is not None:
                self.db.execute(
                    """INSERT INTO checkpoints VALUES (?, ?)
                       ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor""", (source, cursor)
                )

    def records(self, kind: str) -> Iterable[Record]:
        with closing(self.db.execute("SELECT body FROM records WHERE kind=? ORDER BY id", (kind,))) as cursor:
            for (body,) in cursor:
                yield RECORD.validate_json(body)

    def get_record(self, record_id: str) -> Record | None:
        row = self.db.execute("SELECT body FROM records WHERE id=?", (record_id,)).fetchone()
        return RECORD.validate_json(row[0]) if row else None

    def count(self, kind: str) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM records WHERE kind=?", (kind,)).fetchone()[0])

    def priced_products(self) -> int:
        return int(self.db.execute("""SELECT COUNT(*) FROM records WHERE kind='PRODUCT'
            AND json_extract(body,'$.content.pricingModel') IS NOT NULL
            AND json_extract(body,'$.evidence.pricing') IS NOT NULL""").fetchone()[0])

    def product_candidates(self, source: str) -> int:
        return int(self.db.execute("""SELECT COUNT(*) FROM records
            WHERE kind='PRODUCT' AND json_extract(body,'$.source.name')=?""", (source,)).fetchone()[0])

    def find_app_id(self, app_id: str) -> str | None:
        row = self.db.execute("""SELECT id FROM records WHERE kind='PRODUCT'
            AND json_extract(body,'$.evidence.storefront_app_id')=?""", (app_id,)).fetchone()
        return str(row[0]) if row else None

    def mappings(self) -> Iterable[Mapping]:
        for (body,) in self.db.execute("""
            SELECT body FROM (
                SELECT url,raw,body FROM mapping UNION ALL SELECT url,raw,body FROM product_mapping
            ) AS all_mappings ORDER BY url, raw, body
        """):
            yield Mapping.model_validate_json(body)

    def repo(self, slug: str) -> dict | None:
        row = self.db.execute("SELECT body FROM repos WHERE slug=?", (slug,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_repo(self, slug: str, body: dict):
        with self.db:
            self.assert_write_lease()
            self.db.execute(
                "INSERT INTO repos VALUES (?, ?) ON CONFLICT(slug) DO UPDATE SET body=excluded.body", (slug, json.dumps(body))
            )

    def save_results(self, command: str, as_of: str, results: list[SourceResult]):
        with self.db:
            self.assert_write_lease()
            self.db.executemany("INSERT INTO runs(command,as_of,source,body) VALUES (?,?,?,?)", (
                (command, as_of, r.source, r.model_dump_json()) for r in results
            ))

    def results(self) -> list[dict]:
        return [
            {"command": command, "as_of": cutoff, **json.loads(body)}
            for command, cutoff, body in self.db.execute("""
                SELECT command, as_of, body FROM runs
                WHERE seq IN (SELECT MAX(seq) FROM runs GROUP BY command, source)
                ORDER BY seq
            """)
        ]

    def latest_cutoff(self) -> str | None:
        row = self.db.execute(
            "SELECT as_of FROM runs WHERE command='monitor' ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return str(row[0]) if row else None

    def discoveries(self) -> list[dict[str, str]]:
        return [
            {"url": url, "first_seen": ts}
            for url, ts in self.db.execute("SELECT url,first_seen FROM discovery ORDER BY url")
        ]

    def observation_hash(self, url: str) -> str | None:
        row = self.db.execute("SELECT content_hash FROM observations WHERE url=?", (url,)).fetchone()
        return str(row[0]) if row else None

    def full_text(self, url: str) -> str | None:
        row = self.db.execute("SELECT body FROM full_text WHERE url=?", (url,)).fetchone()
        return str(row[0]) if row else None

    def claim_extraction(self, key: str) -> tuple[str | None, str | None]:
        now = datetime.now(UTC)
        token = uuid4().hex
        with self.db:
            self.assert_write_lease()
            claimed = self.db.execute("""
                INSERT INTO field_results VALUES (?, ?, ?, NULL)
                ON CONFLICT(work_key) DO UPDATE SET token=excluded.token, expires_at=excluded.expires_at
                WHERE field_results.result IS NULL AND field_results.expires_at <= ?
                RETURNING token
            """, (key, token, (now + timedelta(minutes=8)).isoformat(), now.isoformat())).fetchone()
            if claimed:
                return token, None
            row = self.db.execute("SELECT result FROM field_results WHERE work_key=?", (key,)).fetchone()
            return None, (str(row[0]) if row and row[0] is not None else None)

    def complete_extraction(self, key: str, token: str, result: str | None) -> None:
        with self.db:
            self.assert_write_lease()
            if result is None:
                self.db.execute("DELETE FROM field_results WHERE work_key=? AND token=? AND result IS NULL", (key, token))
            else:
                self.db.execute("UPDATE field_results SET result=? WHERE work_key=? AND token=? AND result IS NULL",
                                (result, key, token))

    def assert_write_lease(self) -> None:
        """Local SQLite runs have one writer. Distributed stores override this fence."""
