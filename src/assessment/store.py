import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

from .contract import RECORD, Mapping, Page, Record, SourceResult


class Store:
    def __init__(self, path: Path, mode: str, *, create: bool = True):
        if not create and not path.is_file():
            raise ValueError(f"database does not exist: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS by_kind ON records(kind);
            CREATE TABLE IF NOT EXISTS checkpoints (source TEXT PRIMARY KEY, cursor TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mapping (url TEXT, raw TEXT, body TEXT, PRIMARY KEY(url, raw));
            CREATE TABLE IF NOT EXISTS repos (slug TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS discovery (url TEXT PRIMARY KEY, first_seen TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS full_text (url TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                seq INTEGER PRIMARY KEY, command TEXT NOT NULL, as_of TEXT NOT NULL,
                source TEXT NOT NULL, body TEXT NOT NULL
            );
        """)
        with self.db:
            if mode != "export":
                self.db.execute("INSERT OR IGNORE INTO meta VALUES ('mode', ?)", (mode,))
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
            self.db.executemany(
                """INSERT INTO records VALUES (?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET body=excluded.body""",
                ((r.id, r.recordType, r.model_dump_json()) for r in page.records),
            )
            self.db.executemany(
                """INSERT INTO mapping VALUES (?, ?, ?)
                   ON CONFLICT(url, raw) DO UPDATE SET body=excluded.body""",
                ((str(m.source_url), m.raw_name, m.model_dump_json()) for m in page.mappings),
            )
            self.db.executemany(
                "INSERT OR IGNORE INTO discovery VALUES (?, ?)",
                ((url, ts.isoformat()) for url, ts in page.undated.items()),
            )
            self.db.executemany("INSERT OR REPLACE INTO full_text VALUES (?, ?)", page.full_text.items())
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

    def mappings(self) -> Iterable[Mapping]:
        for (body,) in self.db.execute("SELECT body FROM mapping ORDER BY url, raw"):
            yield Mapping.model_validate_json(body)

    def repo(self, slug: str) -> dict | None:
        row = self.db.execute("SELECT body FROM repos WHERE slug=?", (slug,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_repo(self, slug: str, body: dict):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO repos VALUES (?, ?)", (slug, json.dumps(body)))

    def save_results(self, command: str, as_of: str, results: list[SourceResult]):
        with self.db:
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
