import csv
import json
import subprocess
import sys


def test_storefront_uses_offer_evidence_and_deduplicates_app_ids(tmp_path):
    from assessment.products import queries

    query_list = queries()
    url, next_url = next(query_list), next(query_list)
    items = [
        {"trackId": 11, "trackName": "AI Writer", "sellerName": "Sample Labs LLC", "price": 4.99,
         "currency": "USD", "description": "An AI writing assistant.", "trackViewUrl": "https://apps.apple.com/us/app/id11"},
        {"trackId": 11, "trackName": "AI Writer", "sellerName": "Sample Labs LLC", "price": 4.99,
         "currency": "USD", "description": "An AI writing assistant.", "trackViewUrl": "https://apps.apple.com/gb/app/id11"},
        {"trackId": 12, "trackName": "AI Transcriber", "sellerName": "Sample Labs LLC", "price": 0,
         "description": "An AI transcription app.", "trackViewUrl": "https://apps.apple.com/us/app/id12"},
        {"trackId": 13, "trackName": "AI Video", "sellerName": "Sample Labs LLC", "price": 0,
         "description": "An AI video app. No subscription is required to use this app. No free plan. Premium plan available.",
         "trackViewUrl": "https://apps.apple.com/us/app/id13"},
    ]
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"clock": "2026-11-21T12:00:00Z", "responses": {
        url: {"json": {"results": items}},
        next_url: {"json": {"results": items}},
    }}), encoding="utf-8")
    base = [sys.executable, "-m", "assessment", "--db", str(tmp_path / "state.sqlite")]
    for _ in range(2):
        subprocess.run([*base, "--fixture", str(fixture), "backfill", "--vertical", "products",
                        "--product-source", "apple", "--max-pages", "1"], check=True)
    out = tmp_path / "out"
    subprocess.run([*base, "export", "--output", str(out)], check=True)
    with (out / "Products.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert [(r["content.productName"], r["content.pricingModel"]) for r in csv.DictReader(stream)] == [("AI Writer", "PAID")]
    report = json.loads((out / "run-report.json").read_text(encoding="utf-8"))
    assert report["unresolved_product_pricing"]["count"] == 2
