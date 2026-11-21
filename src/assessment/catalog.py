import re
from datetime import date
from decimal import Decimal, InvalidOperation

from pydantic import HttpUrl

from .contract import Mapping, Page, Pricing, Product, ProductContent, Source, SourceResult, Startup
from .identity import Resolver, canonical_url, domain, normalized_name, public_url
from .network import Fetcher
from .products import AI_FEATURE, product_id, product_name
from .store import Store

GLOBAL = "https://globalaiproductindex.com/dataset/globalaiproductindex-products.json"
TIORAI = "https://raw.githubusercontent.com/tioraicom/ai-tools-dataset/main/data/ai-tools.json"
COSTKITE = "https://costkite.com/data/tools.json"
CC_BY = "https://creativecommons.org/licenses/by/4.0/"
PRICES: dict[str, Pricing] = {"free": "FREE", "freemium": "FREEMIUM", "paid": "PAID", "enterprise": "ENTERPRISE"}
INACTIVE = {"archived", "retired", "discontinued", "shutdown", "closed"}


def valid_review_date(value: object, today: date) -> str | None:
    if isinstance(value, str):
        try:
            if date.fromisoformat(value) <= today and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return value
        except ValueError:
            pass
    return None


def same_vendor(url: str, website: str) -> bool:
    left, right = domain(url), domain(website)
    return bool(left and right and (left == right or left.endswith("." + right) or right.endswith("." + left)))


def global_offer(row: dict, today: date):
    website = public_url(row.get("website"))
    slug, title, company = (str(row.get(key) or "").strip() for key in ("slug", "name", "company"))
    reviewed = valid_review_date(row.get("updated"), today)
    price = PRICES.get(str(row.get("business_model") or "").casefold())
    if (not website or not title or not company or not reviewed or not price or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug)
            or str(row.get("status") or "").casefold() in INACTIVE):
        return None
    supplied = row.get("sources")
    if not isinstance(supplied, list):
        return None
    citations = [(str(s.get("title") or ""), url) for s in supplied
                 if isinstance(s, dict) and (url := public_url(s.get("url"))) and same_vendor(url, website)]
    if not citations:
        return None
    listing = f"https://globalaiproductindex.com/products/{slug}/"
    price_url = next((url for label, url in citations if re.search(r"\b(?:pricing|plans?|subscription)\b", label, re.I)), listing)
    return title, company, website, listing, price, {
        "pricing": f"Index business model: {row['business_model']}", "pricing_url": price_url,
        "pricing_checked_at": reviewed, "catalog_category": str(row.get("category") or ""),
        "vendor_citation": citations[0][1], "pricing_license": CC_BY, "catalog_snapshot": GLOBAL,
    }


def tiorai_price(values: object) -> Pricing | None:
    if not isinstance(values, list):
        return None
    terms = {str(value).strip().casefold() for value in values}
    if "freemium" in terms or {"free", "paid"} <= terms:
        return "FREEMIUM"
    if "paid" in terms:
        return "PAID"
    if "free" in terms:
        return "FREE"
    if "enterprise" in terms:
        return "ENTERPRISE"
    return None


def numeric_price(value: object) -> Decimal | None:
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and amount >= 0 else None
    except InvalidOperation:
        return None


def costkite_offer(row: dict, today: date):
    title, company = (str(row.get(k) or "").strip() for k in ("name", "company"))
    price_url = public_url(row.get("pricing_url"))
    checked = valid_review_date(row.get("date_checked"), today) or valid_review_date(row.get("verified_at"), today)
    citations = row.get("source_urls")
    if not title or not company or not price_url or not checked or row.get("discontinued") or not isinstance(citations, list):
        return None
    sources = [url for item in citations if (url := public_url(item)) and same_vendor(url, price_url)]
    if not sources:
        return None
    status = str(row.get("pricing_status") or "").casefold()
    plans = row.get("plans")
    if status not in {"listed", "free_only", "custom", "usage_based"} or not isinstance(plans, list):
        return None
    paid, free = [], []
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        rates = [numeric_price(plan.get(k)) for k in ("monthly_usd", "annual_usd_per_month")]
        label = str(plan.get("name") or "").strip()
        if any(rate is not None and rate > 0 for rate in rates):
            paid.append(label)
        elif label and 0 in rates and not re.search(
            r"\b(?:trial|(?:no|excludes?) .{0,60}(?:access|included))\b",
            f"{label} {plan.get('included_usage', '')} {plan.get('note', '')}", re.I,
        ):
            free.append(label)
    price: Pricing
    if status == "free_only" and not paid:
        price = "FREE"
    elif paid and free:
        price = "FREEMIUM"
    elif paid and not row.get("free_tier"):
        price = "PAID"
    elif status == "custom" and not paid and not free and not row.get("free_tier") and row.get("enterprise_plan") is True:
        price = "ENTERPRISE"
    else:
        return None
    site = next((url for url in sources if url.rstrip("/") != price_url.rstrip("/")), price_url)
    evidence = {
        "pricing": f"Status: {status}; free plans: {', '.join(free)}; paid plans: {', '.join(paid)}",
        "pricing_url": price_url, "pricing_checked_at": checked, "pricing_license": CC_BY,
        "catalog_snapshot": COSTKITE, "vendor_citation": sources[0],
    }
    return title, company, site, COSTKITE, price, evidence


async def collect_catalog(fetch: Fetcher, store: Store, resolver: Resolver, limit: int, source: str) -> SourceResult:
    name, endpoint = {
        "global": ("Global AI Product Index", GLOBAL), "tiorai": ("TiorAI", TIORAI), "costkite": ("CostKite", COSTKITE),
    }[source]
    result = SourceResult(source=name, status="ok")
    remaining = limit - store.priced_products()
    if remaining <= 0:
        return result
    payload = (await fetch.get(endpoint, max_bytes=24_000_000)).json()
    if source == "global":
        if not isinstance(payload, dict) or payload.get("license") not in {CC_BY, "CC BY 4.0", "CC-BY-4.0"}:
            raise ValueError("Global AI Product Index license or envelope changed")
        rows = payload.get("products")
    elif source == "costkite":
        if not isinstance(payload, dict) or payload.get("license_url") != CC_BY:
            raise ValueError("CostKite license or envelope changed")
        rows = payload.get("tools")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ValueError(f"unexpected {name} catalog shape")
    result.pages = 1
    result.fetched = len(rows)
    page = Page()
    existing = {r.id: r for r in store.records("PRODUCT") if isinstance(r, Product)}
    host_owners = {host: set(companies) for host, companies in resolver.domains.items()}
    by_name_url: dict[tuple[str, str], set[str]] = {}
    for startup in store.records("STARTUP"):
        if isinstance(startup, Startup) and startup.content.domain:
            host_owners.setdefault(startup.content.domain, set()).add(startup.content.entityName)
    for record in existing.values():
        site = str(record.content.productUrl)
        if host := domain(site):
            by_name_url.setdefault((normalized_name(record.content.productName), canonical_url(site)), set()).add(record.id)
            if record.source.name == "Global AI Product Index":
                host_owners.setdefault(host, set()).add(record.content.startupName)
        if (vendor := record.evidence.get("seller_website")) and (host := domain(vendor)):
            host_owners.setdefault(host, set()).add(record.content.startupName)
    for row in rows:
        if not isinstance(row, dict):
            continue
        if source == "global":
            offer = global_offer(row, fetch.now().date())
        elif source == "costkite":
            offer = costkite_offer(row, fetch.now().date())
        else:
            website = public_url(row.get("official_url"))
            title = str(row.get("name") or "").strip()
            owners = host_owners.get(domain(website or "") or "", set())
            price = tiorai_price(row.get("pricing_model"))
            reviewed = valid_review_date(row.get("last_verified"), fetch.now().date())
            ai = AI_FEATURE.search(f"{title} {row.get('short_description', '')}")
            if not website or not title or len(owners) != 1 or not price or not reviewed or not ai:
                continue
            offer = (title, next(iter(owners)), website, endpoint, price, {
                "pricing": f"TiorAI pricing model: {', '.join(map(str, row['pricing_model']))}",
                "pricing_url": endpoint, "pricing_checked_at": reviewed, "pricing_license": CC_BY,
                "company_domain": domain(website) or "", "ai_relevance": ai[0], "catalog_snapshot": TIORAI,
            })
        if offer is None:
            continue
        title, raw_company, website, listing, price, evidence = offer
        mapping = resolver.resolve(raw_company, listing)
        company = mapping.canonical_name or raw_company
        canonical = product_name(company, title)
        url_key = (normalized_name(canonical), canonical_url(website))
        matches = by_name_url.get(url_key, set())
        if len(matches) > 1:
            continue
        identity = next(iter(matches), product_id(company, canonical))
        previous = existing.get(identity)
        if previous and previous.content.pricingModel:
            continue
        if previous:
            row_record = previous.model_copy(update={
                "content": previous.content.model_copy(update={"pricingModel": price}),
                "evidence": {**previous.evidence, **evidence, "pricing_catalog": name},
            })
        else:
            row_record = Product(
                id=identity, source=Source(name=name, url=HttpUrl(listing)), collectedAt=fetch.now(),
                evidence=evidence, content=ProductContent(productName=canonical, startupName=company,
                                                          pricingModel=price, productUrl=HttpUrl(website)),
            )
        page.records.append(row_record)
        page.mappings.extend([mapping, Mapping(
            source_url=HttpUrl(listing), raw_name=title, canonical_name=canonical, entity_type="PRODUCT",
            reason="catalog and vendor evidence",
        )])
        existing[identity] = row_record
        by_name_url.setdefault(url_key, set()).add(identity)
        if len(page.records) >= remaining:
            break
    store.commit_page(name, None, page)
    result.eligible = len(page.records)
    return result
