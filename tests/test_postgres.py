import os
from datetime import UTC, datetime

import pytest
from pydantic import HttpUrl

from assessment.contract import Page, Source, Startup, StartupContent
from assessment.postgres import LeaseLost, PostgresStore


@pytest.mark.skipif(not os.getenv("ASSESSMENT_POSTGRES_TEST_DSN"), reason="requires an isolated PostgreSQL test database")
def test_expired_lease_cannot_overwrite_reclaimed_partition():
    dsn = os.environ["ASSESSMENT_POSTGRES_TEST_DSN"]
    a, b = PostgresStore(dsn), PostgresStore(dsn)
    try:
        stamp = datetime.now(UTC)
        assert a.enqueue("test-run", "backfill", ["startups"], stamp.isoformat(), row_limit=500_000) == 1
        assert b.enqueue("test-run", "backfill", ["startups"], stamp.isoformat()) == 0
        first = a.claim()
        assert first is not None
        assert first.row_limit == 500_000
        a.lease = first
        assert b.claim() is None
        b.db.execute("UPDATE tasks SET lease_until=clock_timestamp() WHERE task_key=?", (first.key,))
        second = b.claim()
        assert second is not None and second.token != first.token
        b.lease = second
        token, cached = b.claim_extraction("fields:test")
        assert token and cached is None
        a.lease = None
        assert a.claim_extraction("fields:test") == (None, None)
        a.lease = first
        b.complete_extraction("fields:test", token, '[{"field":"role_family","value":"Research","quote":"Research scientist"}]')
        assert b.claim_extraction("fields:test")[1] is not None
        row = Startup(id="startup:test", source=Source(name="Fixture", url=HttpUrl("https://example.org/startup")),
                      collectedAt=stamp, content=StartupContent(entityName="Fixture"))
        with pytest.raises(LeaseLost):
            a.commit_page("startup:source", "stale", Page(records=[row]))
        assert not a.finish(first, True)
        for _ in range(2):
            b.commit_page("startup:source", "second", Page(records=[row]))
        assert b.finish(second, True)
        assert b.count("STARTUP") == 1
        assert b.cursor("startup:source") == "second"
        assert b.claim() is None
    finally:
        a.close()
        b.close()
