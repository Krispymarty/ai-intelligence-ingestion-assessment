import re
from collections.abc import Iterator
from urllib.parse import urlencode

from pydantic import HttpUrl

from .contract import Mapping, Page, Pricing, Product, ProductContent, Source, SourceResult, Startup
from .enrichment import extract_once
from .identity import Resolver, domain, normalized_name, public_url
from .llm import Extractor, positive_match
from .network import FetchError, Fetcher
from .store import Store

APPLE = "https://itunes.apple.com/search"
PRICING_SOURCE = "https://agipicks.com/data/tools.json"
REGIONS = ("US", "GB", "CA", "AU", "IN")
TERMS = (
    "ai photo generator", "ai meeting notes", "ai writing assistant", "ai music generator",
    "ai video generator", "ai voice generator", "ai transcription", "ai translator", "ai document chat",
    "ai coding assistant", "ai chatbot", "ai presentation generator", "ai study tutor", "ai note assistant",
    "ai image editor", "ai voice changer", "ai resume builder", "ai podcast editor", "ai audio cleaner",
    "ai design generator", "ai spreadsheet assistant", "ai resume interview", "ai language tutor",
    "ai avatar generator", "ai object removal", "ai captions", "ai noise removal", "ai logo generator",
    "generative ai keyboard", "ai watercolor generator", "ai 3d generator", "ai interior design",
    "photo upscaler ai", "ai outfit generator", "ai hairstyle simulator", "ai worksheet maker",
    "ai fitness planner", "ai meal planner", "ai photo restoration", "ai screen reader",
)
AI_FEATURE = re.compile(
    r"\b(?:generative ai|machine learning|artificial intelligence|ai[- ]powered|powered by ai|"
    r"ai (?:writing|photo|image|music|video|voice|meeting|transcri\w*|translat\w*|document|"
    r"coding|chatbot|assistant|generat\w*|caption\w*|avatar|editor|tutor|summari\w*|background|"
    r"photo restoration|recommendation|keyboard|design)|gpt-[345]\w*|large language model)\b", re.I,
)
ORGANIZATION = re.compile(
    r"\b(?:limited|ltd|llc|l\.l\.c|incorporated|inc|corp|corporation|gmbh|pvt|pte|llp|"
    r"s\.a\.s|b\.v|a\.s|oy|株式会社|有限公司)\b|株式会社|有限公司", re.I,
)
FREE = re.compile(r"\b(?:completely free|100% free|free forever|free plan|free tier|free version)\b", re.I)
PAID = re.compile(r"\b(?:paid plans?|premium plans?|optional subscription|upgrade to (?:pro|premium)|"
                  r"in-app purchases|premium subscription)\b", re.I)
REQUIRED_PAYMENT = re.compile(r"\b(?:subscription (?:is )?required|requires (?:an? )?(?:active |paid )?subscription)\b", re.I)
NO_PAYMENT = re.compile(r"\b(?:no (?:in-app purchases|subscriptions?|payments?)|without (?:subscriptions?|in-app purchases))\b", re.I)
PRICE_ENUM: dict[str, Pricing] = {"free": "FREE", "freemium": "FREEMIUM", "paid": "PAID", "contact-sales": "ENTERPRISE"}
AI_CATALOG_CATEGORIES = {
    "automation-agents", "chatbots-assistants", "image-generation", "ai-app-builders",
    "ai-security", "ai-detection", "geo-ai-visibility", "ai-healthcare",
}
ALIASES = {
    ("openai", "chatgptofficial"): "ChatGPT",
    ("openai", "chatgpt"): "ChatGPT",
    ("anthropic", "claudebyanthropic"): "Claude",
    ("anthropic", "claude"): "Claude",
    ("perplexity", "perplexityaskanything"): "Perplexity",
}


def queries() -> Iterator[str]:
    for region in REGIONS:
        for term in TERMS:
            yield f"{APPLE}?" + urlencode(
                {"term": term, "country": region, "media": "software", "entity": "software", "limit": 200},
            )


def product_name(company: str, title: str) -> str:
    return ALIASES.get((normalized_name(company), normalized_name(title)), title)


def product_id(company: str, title: str) -> str:
    return f"product:{normalized_name(company)}/{normalized_name(product_name(company, title))}"


def app_pricing(row: dict, description: str) -> tuple[Pricing | None, str]:
    price = row.get("price")
    if isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0 and row.get("currency"):
        return "PAID", f"Storefront download offer: {price} {row['currency']}"
    required = positive_match(REQUIRED_PAYMENT, description)
    # A subscription for an optional feature is not evidence that the whole product is paid.
    if required and re.search(
        r"to use (?:this |the )?app",
        description[max(0, required.start()-55):required.end()+80], re.I,
    ):
        return "PAID", required[0]
    free = positive_match(FREE, description)
    if not free or required:
        return None, ""
    no_payment = NO_PAYMENT.search(description)
    paid = positive_match(PAID, description)
    if paid and not no_payment:
        return "FREEMIUM", f"{free[0]} | {paid[0]}"
    if no_payment:
        return "FREE", f"{free[0]} | {no_payment[0]}"
    return None, ""


async def collect_apps(fetch: Fetcher, store: Store, resolver: Resolver, target: int, max_pages: int,
                       extractor: Extractor | None = None) -> SourceResult:
    name = "Apple App Store"
    result = SourceResult(source=name, status="ok")
    cursor = int(store.cursor(name) or "0")
    requests = list(queries())
    priced = store.priced_products()
    for index in range(cursor, min(cursor + max_pages, len(requests))):
        if priced >= target:
            break
        try:
            body = (await fetch.get(requests[index])).json()
            rows = body.get("results")
            if not isinstance(rows, list):
                raise ValueError("unexpected Apple Search API shape")
        except (FetchError, ValueError) as exc:
            result.status = "partial" if result.pages else "failed"
            result.details.append(str(exc))
            break
        result.pages += 1
        result.fetched += len(rows)
        page = Page()
        known_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or type(row.get("trackId")) is not int:
                continue
            app_id = str(row["trackId"])
            url = public_url(row.get("trackViewUrl"))
            title = str(row.get("trackName") or "").strip()
            seller = str(row.get("sellerName") or "").strip()
            description = str(row.get("description") or "")
            ai = AI_FEATURE.search(f"{title}\n{description}")
            if not url or domain(url) not in {"apps.apple.com", "itunes.apple.com"} or not ai or not title or not seller:
                continue
            company_map = resolver.resolve(seller, url)
            if company_map.canonical_name is None and not ORGANIZATION.search(seller):
                continue
            company = company_map.canonical_name or seller
            canonical = product_name(company, title)
            record_id = store.find_app_id(app_id) or product_id(company, canonical)
            if record_id in known_ids:
                continue
            known_ids.add(record_id)
            pricing, proof = app_pricing(row, description)
            previous = store.get_record(record_id)
            if isinstance(previous, Product) and previous.content.pricingModel:
                continue
            evidence = {"ai_relevance": ai[0], "seller": seller, "storefront_app_id": app_id,
                        "storefront_query": requests[index], "pricing_url": url}
            if vendor_url := public_url(row.get("sellerUrl")):
                evidence["seller_website"] = vendor_url
            if pricing is None and description and extractor and extractor.remaining:
                claims, attempts = await extract_once(store, extractor, "PRODUCT", description)
                if claims and isinstance(claims[0].value, str):
                    pricing = PRICE_ENUM.get(claims[0].value.casefold().replace("enterprise", "contact-sales"))
                    proof = claims[0].quote
                    result.llm_fields += int(pricing is not None)
                if len(result.details) < 12:
                    result.details.append(f"LLM App Store {app_id}: {', '.join(attempts)}")
            if pricing:
                evidence["pricing"] = proof
                result.eligible += 1
            page.records.append(Product(
                id=record_id, source=Source(name=name, url=HttpUrl(url)), collectedAt=fetch.now(), evidence=evidence,
                content=ProductContent(productName=canonical, startupName=company,
                                       productUrl=HttpUrl(url), pricingModel=pricing),
            ))
            page.mappings.extend([company_map, Mapping(
                source_url=HttpUrl(url), raw_name=title, canonical_name=canonical, entity_type="PRODUCT",
                reason="known company/product alias" if canonical != title else "seller-backed product name",
            )])
            if priced + result.eligible >= target:
                # This search must be replayable if a larger target is requested later.
                break
        store.commit_page(name, str(index + (priced + result.eligible < target)), page)
        if priced + result.eligible >= target:
            break
    return result


async def enrich_pricing(fetch: Fetcher, store: Store, resolver: Resolver, target: int) -> SourceResult:
    result = SourceResult(source="AGIPicks pricing", status="ok")
    body = (await fetch.get(PRICING_SOURCE, max_bytes=12_000_000)).json()
    if body.get("license") != "CC-BY-4.0" or not isinstance(body.get("tools"), list):
        raise ValueError("unexpected pricing dataset license or shape")
    result.pages = 1
    index: dict[tuple[str, str], list[dict]] = {}
    for row in body["tools"]:
        website = public_url(row.get("website"))
        if website and row.get("name") and not row.get("retired"):
            key = (normalized_name(row["name"]), domain(website) or "")
            index.setdefault(key, []).append(row)
    page = Page()
    known_companies: dict[str, set[str]] = {host: set(names) for host, names in resolver.domains.items()}
    for startup in store.records("STARTUP"):
        if isinstance(startup, Startup) and startup.content.domain:
            known_companies.setdefault(startup.content.domain, set()).add(startup.content.entityName)
    product_ids = set()
    for product in store.records("PRODUCT"):
        if not isinstance(product, Product):
            continue
        product_ids.add(product.id)
        website = product.evidence.get("seller_website", str(product.content.productUrl))
        if seller := product.evidence.get("seller_website"):
            if seller_domain := domain(seller):
                known_companies.setdefault(seller_domain, set()).add(product.content.startupName)
        if product.content.pricingModel:
            continue
        key = (normalized_name(product.content.productName), domain(website) or "")
        lookup = index.get(key, [])
        if len(lookup) != 1:
            continue
        row = lookup[0]
        raw_price = row.get("pricingModel")
        price = PRICE_ENUM.get(raw_price) if isinstance(raw_price, str) else None
        checked = row.get("pricesCheckedAt")
        if price and isinstance(checked, str) and checked and public_url(row.get("url")):
            evidence = {**product.evidence, "pricing": str(row["pricingModel"]),
                        "pricing_url": row["url"], "pricing_snapshot": PRICING_SOURCE,
                        "pricing_checked_at": checked, "pricing_license": "CC BY 4.0"}
            page.records.append(product.model_copy(update={
                "content": product.content.model_copy(update={"pricingModel": price}), "evidence": evidence,
            }))
            result.eligible += 1
    result.fetched = len(body["tools"])
    existing = store.priced_products()
    for candidates in index.values():
        if existing + result.eligible >= target:
            break
        if len(candidates) != 1:
            continue
        row = candidates[0]
        title, website = row["name"], row["website"]
        host = domain(website) or ""
        companies = known_companies.get(host, set())
        raw_price = row.get("pricingModel")
        price = PRICE_ENUM.get(raw_price) if isinstance(raw_price, str) else None
        checked = row.get("pricesCheckedAt")
        listing = public_url(row.get("url"))
        ai = str(row.get("category") or "")
        feature = AI_FEATURE.search(title)
        if (not listing or domain(listing) != "agipicks.com" or len(companies) != 1 or not price
                or not isinstance(checked, str) or not checked or (not feature and ai not in AI_CATALOG_CATEGORIES)):
            continue
        company = next(iter(companies))
        canonical = product_name(company, title)
        identity = product_id(company, canonical)
        if identity in product_ids:
            continue
        product_ids.add(identity)
        page.records.append(Product(
            id=identity, source=Source(name="AGIPicks pricing", url=HttpUrl(PRICING_SOURCE)),
            collectedAt=fetch.now(),
            evidence={"company_domain": host, "ai_relevance": feature[0] if feature else ai,
                      "pricing": str(raw_price), "pricing_url": listing, "pricing_checked_at": checked,
                      "pricing_license": "CC BY 4.0"},
            content=ProductContent(productName=canonical, startupName=company, productUrl=HttpUrl(website), pricingModel=price),
        ))
        page.mappings.append(Mapping(source_url=HttpUrl(listing), raw_name=title, canonical_name=canonical,
                                     entity_type="PRODUCT", reason="vendor domain and catalog price"))
        result.eligible += 1
    store.commit_page(result.source, None, page)
    return result
