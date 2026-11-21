import json
import re

from pydantic import HttpUrl

from .catalog import numeric_price
from .contract import Mapping, Page, Pricing, Product, ProductContent, Source, SourceResult
from .identity import Resolver
from .network import Fetcher
from .products import product_id, product_name
from .store import Store

ENDPOINT = "https://openrouter.ai/api/v1/models?output_modalities=all"
SOURCE = "OpenRouter model offers"
# OpenRouter identifies creators with author namespaces. Ignore anonymous/community publishers.
AUTHORS = {
    "aion-labs": "Aion Labs", "alibaba": "Alibaba Cloud", "amazon": "Amazon",
    "anthropic": "Anthropic", "assemblyai": "AssemblyAI",
    "baidu": "Baidu", "baseten": "Baseten", "black-forest-labs": "Black Forest Labs", "bytedance": "ByteDance",
    "bytedance-seed": "ByteDance", "cloudflare": "Cloudflare", "cohere": "Cohere", "deepgram": "Deepgram",
    "deepseek": "DeepSeek", "elevenlabs": "ElevenLabs", "fish-audio": "Fish Audio", "google": "Google",
    "heygen": "HeyGen", "inclusionai": "Ant Group", "krea": "Krea", "liquid": "Liquid AI",
    "meta": "Meta", "meta-llama": "Meta", "microsoft": "Microsoft", "minimax": "MiniMax",
    "mistralai": "Mistral AI", "mongodb": "MongoDB", "moondream": "Moondream",
    "moonshotai": "Moonshot AI", "morph": "Morph", "nace-ai": "Nace AI", "nex-agi": "Nex AGI",
    "nvidia": "NVIDIA", "openai": "OpenAI", "perceptron": "Perceptron", "perplexity": "Perplexity",
    "prism-ml": "Prism ML", "qwen": "Alibaba Cloud", "radixark": "RadixArk", "recraft": "Recraft",
    "rekaai": "Reka AI", "relace": "Relace", "respan": "Respan", "runway": "Runway", "sakana": "Sakana AI",
    "scaledown": "ScaleDown", "sourceful": "Sourceful", "speechify": "Speechify", "tencent": "Tencent",
    "thinkingmachines": "Thinking Machines Lab", "upstage": "Upstage", "x-ai": "xAI", "xiaomi": "Xiaomi",
    "z-ai": "Z.ai",
}
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*/[a-zA-Z0-9][a-zA-Z0-9_.-]*")
NON_PRODUCTION = re.compile(
    r"(^|[-_ /\s])(?:preview|experimental|beta|alpha|test|nightly|rc)(?:$|[-_ .\s])"
    r"|[-_]20\d{2}(?:-?\d{2}){2}(?:$|[-_.])", re.I,
)
BASE_RATES = ("prompt", "completion", "request", "image", "audio", "image_output", "audio_output")


def rate_evidence(row: dict) -> tuple[bool, dict[str, str]] | None:
    prices = row.get("pricing")
    if not isinstance(prices, dict) or not {"prompt", "completion"} <= prices.keys():
        return None
    amounts = {k: numeric_price(prices[k]) for k in BASE_RATES if k in prices}
    if any(value is None for value in amounts.values()):
        return None
    return any(value is not None and value > 0 for value in amounts.values()), {
        key: str(value) for key, value in amounts.items()
    }


async def collect_model_offers(fetch: Fetcher, store: Store, resolver: Resolver, limit: int) -> SourceResult:
    result = SourceResult(source=SOURCE, status="ok")
    remaining = limit - store.priced_products()
    if remaining <= 0:
        return result
    body = (await fetch.get(ENDPOINT, max_bytes=8_000_000)).json()
    rows = body.get("data")
    if not isinstance(rows, list):
        raise ValueError("unexpected OpenRouter model catalog shape")
    result.pages = 1
    result.fetched = len(rows)
    free_variants = set()
    for row in rows:
        if isinstance(row, dict) and (identifier := str(row.get("id") or "")).endswith(":free"):
            if (rates := rate_evidence(row)) and not rates[0]:
                free_variants.add(identifier.removesuffix(":free"))
    accepted = set()
    page = Page()
    for row in sorted((r for r in rows if isinstance(r, dict)), key=lambda r: str(r.get("id") or "")):
        identifier = str(row.get("id") or "")
        canonical_slug = str(row.get("canonical_slug") or identifier)
        raw_title = str(row.get("name") or "")
        _, _, suffix = raw_title.partition(": ")
        title = suffix or raw_title
        if (not title.strip() or not IDENTIFIER.fullmatch(identifier) or not IDENTIFIER.fullmatch(canonical_slug)
                or NON_PRODUCTION.search(identifier) or NON_PRODUCTION.search(title) or canonical_slug in accepted):
            continue
        namespace, _ = identifier.split("/", 1)
        company = AUTHORS.get(namespace)
        rates = rate_evidence(row)
        if not company or not rates:
            continue
        paid, amounts = rates
        price: Pricing = "FREEMIUM" if paid and identifier in free_variants else "PAID" if paid else "FREE"
        url = f"https://openrouter.ai/{identifier}"
        mapping = resolver.resolve(company, url)
        canonical_company = mapping.canonical_name or company
        canonical_title = product_name(canonical_company, title.strip())
        identity = product_id(canonical_company, canonical_title)
        if identity in accepted or (isinstance(previous := store.get_record(identity), Product) and previous.content.pricingModel):
            continue
        evidence = {
            "pricing": "OpenRouter published base rates in USD: " + json.dumps(amounts, sort_keys=True),
            "pricing_url": url, "catalog_snapshot": ENDPOINT, "company_namespace": namespace,
            "canonical_offer": canonical_slug, "offer_kind": "hosted_model_api",
            "price_scope": "OpenRouter hosted offer; company is the model author",
        }
        if price == "FREEMIUM":
            evidence["free_route"] = f"https://openrouter.ai/{identifier}:free"
        page.records.append(Product(
            id=identity, source=Source(name=SOURCE, url=HttpUrl(url)), collectedAt=fetch.now(), evidence=evidence,
            content=ProductContent(productName=canonical_title, startupName=canonical_company,
                                   pricingModel=price, productUrl=HttpUrl(url)),
        ))
        page.mappings.extend([mapping, Mapping(
            source_url=HttpUrl(url), raw_name=str(row["name"]), canonical_name=canonical_title,
            entity_type="PRODUCT", reason="canonical production API offer",
        )])
        accepted.update((canonical_slug, identity))
        if len(page.records) >= remaining:
            break
    store.commit_page(SOURCE, None, page)
    result.eligible = len(page.records)
    return result
