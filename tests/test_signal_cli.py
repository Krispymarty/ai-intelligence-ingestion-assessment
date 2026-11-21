import csv
import json
import subprocess
import sys

from openpyxl import load_workbook

from assessment.monitor import FEEDS


def test_recent_exports_keep_verified_times_and_preserve_failed_sources(tmp_path):
    clock = "2026-11-21T12:00:00Z"
    rss = """<rss version="2.0"><channel>
        <item><title>AI edge</title><link>https://techcrunch.com/edge</link><pubDate>2026-11-20T12:00:00Z</pubDate></item>
        <item><title>AI unclear</title><link>https://techcrunch.com/undated</link><pubDate>2026-11-21</pubDate></item>
        <item><title>AI old</title><link>https://techcrunch.com/old</link><pubDate>2026-11-20T11:59:59Z</pubDate></item>
        <item><title>AI relative</title><link>https://techcrunch.com/relative</link><pubDate>24 hours ago</pubDate></item>
        <item><title>AI future</title><link>https://techcrunch.com/future</link><pubDate>2026-11-21T12:01:00Z</pubDate></item>
        <item><title>=1+1</title><link>https://techcrunch.com/formula</link><pubDate>2026-11-21T11:30:00Z</pubDate></item>
    </channel></rss>"""
    responses = {
        feed.url: {"body": "<rss><channel/></rss>"} if feed.kind == "news" or feed.name == "We Work Remotely"
        else {"json": [] if feed.name == "Remote OK" else {"jobs": []}} for feed in FEEDS
    }
    responses[FEEDS[0].url] = {"body": rss}
    responses[FEEDS[1].url] = {"status": 403}
    responses[next(f.url for f in FEEDS if f.name == "Himalayas")] = {"json": {"jobs": [{
        "title": "Machine Learning Engineer", "companyName": "Example", "pubDate": 1795262400,
        "applicationLink": "https://example.org/apply",
    }]}}
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"clock": clock, "responses": responses}), encoding="utf-8")
    base = [sys.executable, "-m", "assessment", "--db", str(tmp_path / "db.sqlite")]
    for _ in range(2):
        result = subprocess.run([*base, "--fixture", str(fixture), "monitor", "--as-of", clock], check=False)
        assert result.returncode == 2
    output = tmp_path / "export"
    subprocess.run([*base, "export", "--output", str(output)], check=True)
    with (output / "News.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert set(rows[0]) == {
        "schemaVersion", "recordType", "id", "source.name", "source.url",
        "collectedAt", "content.title", "content.date", "content.excerpt", "evidence",
    }
    assert {row["content.title"] for row in rows} == {"AI edge", "'=1+1"}
    book = load_workbook(output / "AI Assessment.xlsx", read_only=True)
    assert book.sheetnames == ["Startups", "Products", "Research Papers", "Jobs", "News", "Entity Mapping Log"]
    assert all(cell.data_type != "f" for row in book["News"].rows for cell in row)
    book.close()
    report = json.loads((output / "run-report.json").read_text())
    assert report["mode"] == "replay"
    assert {entry["url"] for entry in report["unverified_publication"]} == {
        "https://techcrunch.com/undated", "https://techcrunch.com/relative", "https://example.org/apply",
    }
    health = {source["source"]: source for source in report["sources"]}
    assert health["The Verge AI"]["status"] == "failed"
    assert health["Ars Technica AI"]["status"] == "ok"
    assert health["Ars Technica AI"]["eligible"] == 0
    assert report["counts"]["JOB"] == 0
