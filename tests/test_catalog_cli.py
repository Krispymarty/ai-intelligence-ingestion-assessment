import csv
import json
import subprocess
import sys


def test_catalog_accepts_reported_pricing_and_skips_open_weights(tmp_path):
    endpoint = "https://globalaiproductindex.com/dataset/globalaiproductindex-products.json"
    common = {"company": "Open AI", "website": "https://openai.com/chatgpt/",
              "category": "assistant", "availability": "global", "updated": "2026-11-01",
              "sources": [{"title": "Pricing", "url": "https://openai.com/pricing/"}]}
    rows = [
        {**common, "slug": "chatgpt", "name": "ChatGPT", "business_model": "freemium"},
        {**common, "slug": "chatgpt-official", "name": "ChatGPT Official", "business_model": "freemium"},
        {**common, "slug": "open-weights", "name": "Open Weights", "business_model": "open"},
        {**common, "slug": "uncited", "name": "Uncited", "business_model": "free", "sources": []},
    ]
    fixture = tmp_path / "feed.json"
    fixture.write_text(json.dumps({"clock": "2026-11-21T12:00:00Z", "responses": {
        endpoint: {"json": {"license": "https://creativecommons.org/licenses/by/4.0/", "products": rows}},
    }}), encoding="utf-8")
    base = [sys.executable, "-m", "assessment", "--db", str(tmp_path / "database.sqlite")]
    for _ in range(2):
        subprocess.run([*base, "--fixture", str(fixture), "backfill", "--vertical", "products",
                        "--product-source", "global"], check=True)
    out = tmp_path / "out"
    subprocess.run([*base, "export", "--output", str(out)], check=True)
    with (out / "Products.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert [(r["content.productName"], r["content.startupName"], r["content.pricingModel"])
                for r in csv.DictReader(stream)] == [("ChatGPT", "OpenAI", "FREEMIUM")]


def test_model_offer_groups_free_and_paid_routes_and_excludes_previews(tmp_path):
    url = "https://openrouter.ai/api/v1/models?output_modalities=all"
    stable = {"id": "openai/example", "canonical_slug": "openai/example-20261120",
              "name": "OpenAI: Example API", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}
    rows = [
        stable, {**stable, "id": "openai/example:free", "pricing": {"prompt": "0", "completion": "0"}},
        {**stable, "id": "openai/example-preview", "canonical_slug": "openai/example-preview",
         "name": "OpenAI: Example Preview"},
        {**stable, "id": "openai/example-20261120"},
        {**stable, "id": "community/example", "canonical_slug": "community/example"},
    ]
    fixture = tmp_path / "feed.json"
    fixture.write_text(json.dumps({"clock": "2026-11-21T12:00:00Z", "responses": {url: {"json": {"data": rows}}}}),
                       encoding="utf-8")
    base = [sys.executable, "-m", "assessment", "--db", str(tmp_path / "database.sqlite")]
    subprocess.run([*base, "--fixture", str(fixture), "backfill", "--vertical", "products",
                    "--product-source", "model-apis"], check=True)
    out = tmp_path / "out"
    subprocess.run([*base, "export", "--output", str(out)], check=True)
    report = json.loads((out / "run-report.json").read_text(encoding="utf-8"))
    assert report["product_offer_types"] == {"hosted_model_api": 1}
    with (out / "Products.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert [(r["content.productName"], r["content.pricingModel"]) for r in csv.DictReader(stream)] == [
            ("Example API", "FREEMIUM"),
        ]
