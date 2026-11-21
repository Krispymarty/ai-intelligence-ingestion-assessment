import csv
import json
import subprocess
import sys


def test_paper_observed_zero_survives_replay(tmp_path):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({
        "clock": "2026-11-21T12:00:00Z",
        "responses": {
            "https://api.github.com/repos/example/research": {
                "status": 301, "headers": {"Location": "https://api.github.com/repositories/12"}
            },
            "https://api.github.com/repositories/12": {
                "json": {"id": 12, "html_url": "https://github.com/example/renamed", "stargazers_count": 0,
                         "private": False}
            },
            "https://export.arxiv.org/api/query?id_list=2410.00001&max_results=1": {"body": """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>https://arxiv.org/abs/2410.00001v1</id><title>Example AI Paper</title>
<published>2024-10-01T12:00:00Z</published><author><name>Example Author</name></author>
<category term="cs.AI"/></entry></feed>"""},
        },
        "paper_links": [{"paper_arxiv_id": "2410.00001v2", "repo_url": "https://github.com/example/research", "is_official": True}],
    }), encoding="utf-8")
    base = [sys.executable, "-m", "assessment", "--db", str(tmp_path / "db.sqlite")]
    for _ in range(2):
        subprocess.run([*base, "--fixture", str(fixture), "backfill", "--vertical", "papers", "--limit", "1"], check=True)
    output = tmp_path / "export"
    subprocess.run([*base, "export", "--output", str(output)], check=True)
    with (output / "Research Papers.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [(row["content.title"], row["content.github_stars"]) for row in rows] == [("Example AI Paper", "0")]
    assert rows[0]["content.github_url"] == "https://github.com/example/renamed"


def test_startup_checkpoint_resumes_and_records_conflicting_alias(tmp_path):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({
        "clock": "2026-11-21T12:00:00Z",
        "responses": {"https://yc-oss.github.io/api/companies/all.json": {"json": [
            *[{"name": f"Placeholder {i}", "slug": f"placeholder-{i}", "one_liner": "General services"}
              for i in range(100)],
            {"name": "Open AI", "slug": "example-conflict", "website": "https://anthropic.com",
             "one_liner": "Builds AI applications"},
        ]}},
    }), encoding="utf-8")
    base = [sys.executable, "-m", "assessment", "--db", str(tmp_path / "db.sqlite")]
    for _ in range(3):
        subprocess.run([*base, "--fixture", str(fixture), "backfill", "--vertical", "startups",
                        "--limit", "2", "--max-pages", "1"], check=True)
    output = tmp_path / "export"
    subprocess.run([*base, "export", "--output", str(output)], check=True)
    with (output / "Entity Mapping Log.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [(row["raw_name"], row["canonical_name"], row["reason"]) for row in rows] == [
        ("Open AI", "", "conflicting evidence")
    ]
