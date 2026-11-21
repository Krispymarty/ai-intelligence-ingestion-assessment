import asyncio
import csv
import gzip
import hashlib
import io
import json
import os
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx
from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException
from pydantic import HttpUrl, ValidationError

from .contract import Mapping, Page, Paper, PaperContent, Product, ProductContent, Source
from .contract import SourceResult, Startup, StartupContent, StartupData
from .identity import AI, Resolver, canonical_url, domain, normalized_name, public_url
from .network import FetchError, Fetcher
from .products import product_id, product_name
from .store import Store

YC = "https://yc-oss.github.io/api/companies/all.json"
HF = "https://huggingface.co/api/spaces?sort=likes&direction=-1&limit=100"
HARPD = "https://raw.githubusercontent.com/harpd-dev/harpd-ai-datasets/main/data/products/products.json"
HARPD_ATTRIBUTION = "https://harpd.com/data/"
HARPD_LICENSE = "https://creativecommons.org/licenses/by/4.0/"
TOOLBUZZ = (
    "https://raw.githubusercontent.com/devadigax/aitoolbuzz-dataset/main/data/"
    "Complete%20AI%20Tools%20Dataset%202025%20-%2016763%20Tools%20from%20AIToolBuzz.csv"
)
TOOLBUZZ_ATTRIBUTION = "https://github.com/devadigax/aitoolbuzz-dataset"
PWC = "https://huggingface.co/datasets/pwc-archive/files/resolve/main/jul-28-links-between-papers-and-code.json.gz"
PWC_EVIDENCE = "https://huggingface.co/datasets/pwc-archive/links-between-paper-and-code"
ARXIV = "https://export.arxiv.org/api/query"
ATOM = {"a": "http://www.w3.org/2005/Atom"}
AI_CATEGORIES = {"cs.AI", "cs.LG", "cs.CV", "cs.CL", "cs.RO", "cs.NE", "stat.ML"}
KINDS = {"startups": "STARTUP", "products": "PRODUCT", "papers": "RESEARCH_PAPER"}
APP_FEATURE = re.compile(
    r"\b(?:chatbot|text.to.image|text.to.speech|speech.to.text|image generat(?:or|ion)|"
    r"speech recognition|transcri(?:be|ption)|upscal(?:e|er|ing)|object detection|"
    r"voice clon(?:e|ing)|language translation|inference|ai assistant)\b", re.I,
)
VENDOR_CLAIM = re.compile(
    r"\b(?:developed|created|built|published|offered|made|provided|operated|launched|model|assistant|platform|tool) "
    r"by\s+([\w][\w.'’& -]{0,80})", re.I,
)


def arxiv_id(value: object) -> str:
    match = re.fullmatch(r"(\d{4}\.\d{4,5}|[a-z][a-z.]+/\d{7})(?:v\d+)?", str(value).strip(), re.I)
    return match[1] if match else ""


def repo_slug(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parsed = urlsplit(value)
    match = re.fullmatch(r"/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/?", parsed.path)
    return match[1].removesuffix(".git").lower() if parsed.hostname in {"github.com", "www.github.com"} and match else ""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Bulk:
    def __init__(self, fetch: Fetcher, store: Store, resolver: Resolver):
        self.fetch = fetch
        self.store = store
        self.resolver = resolver
        self.companies: list[dict] | None = None
        self.yc_lock = asyncio.Lock()

    async def yc(self):
        async with self.yc_lock:
            if self.companies is None:
                payload = (await self.fetch.get(YC, max_bytes=32_000_000)).json()
                if not isinstance(payload, list):
                    raise ValueError("unexpected YC snapshot shape")
                self.companies = payload
            return self.companies

    async def startups(self, limit: int, pages: int) -> SourceResult:
        source = "YC Startup Directory"
        result = SourceResult(source=source, status="ok")
        if self.store.count("STARTUP") >= limit:
            return result
        companies = await self.yc()
        cursor = self.store.cursor(source)
        if cursor == "done":
            return result
        version = digest(json.dumps(companies, sort_keys=True))
        parts = cursor.split(":") if cursor else []
        start = int(parts[1]) if len(parts) == 2 and version == parts[0] else 0
        for _ in range(pages):
            page = Page()
            remaining = limit - self.store.count("STARTUP")
            end = min(len(companies), start + 100)
            for index in range(start, end):
                data = companies[index]
                result.fetched += 1
                raw = str(data.get("name") or "").strip()
                slug = str(data.get("slug") or "")
                website = public_url(data.get("website"))
                host = domain(website or "")
                evidence = " | ".join(str(data.get(k) or "") for k in ("tags", "one_liner", "industry", "long_description"))
                signal = AI.search(evidence)
                if not raw or not re.fullmatch(r"[\w-]+", slug) or not signal:
                    continue
                url = f"https://www.ycombinator.com/companies/{slug}"
                mapping = self.resolver.resolve(raw, url, host)
                page.mappings.append(mapping)
                size = data.get("team_size")
                page.records.append(Startup(
                    id=f"startup:{host or 'yc/' + slug}", source=Source(name=source, url=HttpUrl(url)),
                    collectedAt=self.fetch.now(), evidence={"ai_relevance": signal[0], "snapshot": YC},
                    content=StartupContent(entityName=mapping.canonical_name or raw, domain=host,
                        data=StartupData(employeeCount=size if type(size) is int and size > 0 else None)),
                ))
                if len(page.records) >= remaining:
                    end = index + 1
                    break
            self.store.commit_page(source, "done" if end == len(companies) else f"{version}:{end}", page)
            result.eligible += len(page.records)
            start = end
            if end == len(companies) or self.store.count("STARTUP") >= limit:
                break
        return result

    async def products(self, limit: int, pages: int) -> SourceResult:
        source = "Hugging Face Spaces"
        result = SourceResult(source=source, status="ok")
        url = self.store.cursor(source) or HF
        if url == "done" or self.store.product_candidates(source) >= limit:
            return result
        companies = await self.yc()
        names = defaultdict(set)
        for row in companies:
            raw = str(row.get("name") or "").strip()
            if not raw:
                continue
            for alias in (raw, str(row.get("slug") or "")):
                if key := normalized_name(alias):
                    names[key].add(raw)
        for _ in range(pages):
            response = await self.fetch.get(url)
            items = response.json()
            if not isinstance(items, list):
                raise ValueError("unexpected Spaces page shape")
            result.fetched += len(items)
            candidates = [(row, str(row.get("id") or "")) for row in items]
            page = Page()
            failures = []
            for row, app in candidates:
                if not re.fullmatch(r"[\w.-]+/[\w.-]+", app) or row.get("private"):
                    continue
                publisher = app.split("/")[0]
                if normalized_name(publisher) not in names and not self.resolver.aliases.get(normalized_name(publisher)):
                    continue
                if len(names[normalized_name(publisher)]) > 1:
                    continue
                try:
                    record = await self._product(row, app, names)
                except FetchError as exc:
                    if exc.status not in {404, 410}:
                        failures.append(str(exc))
                    continue
                if record:
                    product, mapping, app_mapping = record
                    page.records.append(product)
                    page.mappings.extend([mapping, app_mapping])
            next_url = response.links.get("next", {}).get("url")
            if next_url:
                parsed = urlsplit(next_url)
                if parsed.scheme != "https" or parsed.netloc != "huggingface.co" or parsed.path != "/api/spaces":
                    raise ValueError("unexpected Spaces pagination URL")
                if next_url == url:
                    raise ValueError("Spaces repeated its cursor")
            self.store.commit_page(source, None if failures else (next_url or "done"), page)
            result.eligible += len(page.records)
            if failures:
                result.status = "partial"
                result.details = failures[:8]
                break
            if not next_url or self.store.product_candidates(source) >= limit:
                break
            url = next_url
        return result

    async def _product(self, row: dict, app: str, names: dict[str, set[str]]):
        publisher = app.split("/")[0]
        url = f"https://huggingface.co/spaces/{app}"
        payload = (await self.fetch.get(f"https://huggingface.co/api/spaces/{app}/runtime")).json()
        if payload.get("stage") not in {"RUNNING", "RUNNING_BUILDING", "SLEEPING"}:
            return None
        org_url = f"https://huggingface.co/{publisher}"
        if publisher not in self.product_orgs:
            org = (await self.fetch.get(f"https://huggingface.co/api/organizations/{publisher}/overview")).json()
            match = self.resolver.resolve(str(org.get("fullname") or ""), org_url)
            self.product_orgs[publisher] = (
                org.get("name") == publisher
                and (normalized_name(str(org.get("fullname") or "")) == normalized_name(publisher)
                     or (match.canonical_name is not None
                         and match.canonical_name in self.resolver.aliases.get(normalized_name(publisher), ())))
            )
        if not self.product_orgs[publisher]:
            return None
        title = str(row.get("title") or app.split("/")[1]).strip()
        evidence = APP_FEATURE.search(" ".join([title, *map(str, row.get("tags") or [])]))
        model_tags = [tag for tag in row.get("tags") or [] if str(tag).startswith("models:")]
        if not evidence and not model_tags:
            return None
        mapping = self.resolver.resolve(publisher, url)
        company = mapping.canonical_name or next(iter(names[normalized_name(publisher)]), None)
        if not company or not normalized_name(title):
            return None
        canonical = product_name(company, title)
        app_mapping = Mapping(source_url=HttpUrl(url), raw_name=title, canonical_name=canonical,
                              entity_type="PRODUCT", reason="publisher-backed product name")
        product = Product(
            id=product_id(company, canonical),
            source=Source(name="Hugging Face Spaces", url=HttpUrl(url)), collectedAt=self.fetch.now(),
            evidence={"publisher_profile": org_url, "publisher": publisher, "runtime": payload["stage"],
                      "ai_relevance": evidence[0] if evidence else str(model_tags[0])},
            content=ProductContent(productName=canonical, startupName=company, productUrl=HttpUrl(url)),
        )
        return product, mapping, app_mapping

    async def catalog_products(self, limit: int, pages: int) -> SourceResult:
        source = "Harpd AI Datasets"
        result = SourceResult(source=source, status="ok")
        if self.store.product_candidates(source) >= limit:
            return result
        catalog = (await self.fetch.get(HARPD)).json()
        rows = catalog.get("products")
        if catalog.get("license") != HARPD_LICENSE or not isinstance(rows, list):
            raise ValueError("Harpd catalog license or schema changed")
        return await self._catalog(rows, result, limit, pages, "harpd.com", HARPD_ATTRIBUTION)

    async def toolbuzz_products(self, limit: int, pages: int) -> SourceResult:
        result = SourceResult(source="AIToolBuzz", status="ok")
        if self.store.product_candidates(result.source) >= limit:
            return result
        response = await self.fetch.get(TOOLBUZZ, max_bytes=30_000_000)
        reader = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))
        required = {"Name", "Link", "Website", "Website Status", "Short Description", "Primary Task"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("AIToolBuzz CSV header changed")
        rows = [{
            "name": row["Name"], "url": row["Link"], "website": row["Website"],
            "description": row["Short Description"], "productType": row["Primary Task"],
            "availability_evidence": "Active in October 2025 dataset snapshot",
        } for row in reader if (row["Website Status"] or "").strip().casefold() == "active"]
        return await self._catalog(rows, result, limit, pages, "aitoolbuzz.com", TOOLBUZZ_ATTRIBUTION)

    async def _catalog(self, rows: list[dict], result: SourceResult, limit: int, pages: int,
                       source_domain: str, attribution: str) -> SourceResult:
        source = result.source
        cursor = self.store.cursor(source)
        version = digest(json.dumps(rows, sort_keys=True))
        parts = cursor.split(":") if cursor else []
        start = int(parts[1]) if len(parts) == 2 and version == parts[0] else 0
        company_domains: dict[str, set[str]] = defaultdict(set)
        company_names: dict[str, set[str]] = defaultdict(set)
        for row in await self.yc():
            website = public_url(row.get("website"))
            raw_name = str(row.get("name") or "").strip()
            if website and raw_name and (host := domain(website)):
                name = self.resolver.resolve(raw_name, website, host).canonical_name or raw_name
                company_domains[host].add(name)
                company_names[normalized_name(raw_name)].add(name)
        for _ in range(pages):
            page = Page()
            remaining = limit - self.store.product_candidates(source)
            end = min(start + 100, len(rows))
            for index in range(start, end):
                row = rows[index]
                result.fetched += 1
                title = str(row.get("name") or "").strip()
                listing = public_url(row.get("url"))
                website = public_url(row.get("website"))
                signal = AI.search(f"{row.get('description', '')} {row.get('productType', '')}") or APP_FEATURE.search(
                    f"{row.get('description', '')} {row.get('productType', '')}"
                )
                if not normalized_name(title) or not listing or domain(listing) != source_domain or not website or not signal:
                    continue
                host = domain(website) or ""
                possible = self.resolver.domains.get(host, set()) | company_domains[host]
                claim = VENDOR_CLAIM.search(str(row.get("description") or ""))
                vendor_text = ""
                if claim:
                    words = claim[1].split()[:7]
                    for size in range(len(words), 0, -1):
                        vendor_text = " ".join(words[:size]).rstrip(".")
                        key = normalized_name(vendor_text)
                        cited = company_names[key] | self.resolver.aliases.get(key, set())
                        if cited:
                            possible |= cited
                            break
                    else:
                        vendor_text = ""
                if len(possible) != 1:
                    continue
                company = next(iter(possible))
                mapping = self.resolver.resolve(company, listing, host)
                if mapping.reason == "no seed match":
                    mapping = mapping.model_copy(update={
                        "canonical_name": company, "reason": "catalog vendor claim" if vendor_text else "YC website domain",
                    })
                page.mappings.append(mapping)
                canonical = product_name(company, title)
                page.mappings.append(Mapping(
                    source_url=HttpUrl(listing), raw_name=title, canonical_name=canonical,
                    entity_type="PRODUCT", reason="catalog-backed product name",
                ))
                evidence = {"attribution": attribution, "license": HARPD_LICENSE,
                            "source_snapshot": HARPD if source_domain == "harpd.com" else TOOLBUZZ,
                            "catalog_listing": listing, "company_domain": host, "ai_relevance": signal[0]}
                if vendor_text:
                    evidence["catalog_vendor_claim"] = f"{claim[0][:claim.start(1) - claim.start()]}{vendor_text}" if claim else vendor_text
                if availability := row.get("availability_evidence"):
                    evidence["availability"] = availability
                page.records.append(Product(
                    id=product_id(company, canonical),
                    source=Source(name=source, url=HttpUrl(evidence["source_snapshot"])), collectedAt=self.fetch.now(),
                    evidence=evidence,
                    content=ProductContent(productName=canonical, startupName=company, productUrl=HttpUrl(website)),
                ))
                if len(page.records) >= remaining:
                    end = index + 1
                    break
            self.store.commit_page(source, f"{version}:{end}", page)
            result.eligible += len(page.records)
            if end == len(rows) or self.store.product_candidates(source) >= limit:
                break
            start = end
        return result

    product_orgs: dict[str, bool]

    async def papers(self, limit: int, pages: int, links_path: Path | None) -> SourceResult:
        source = "arXiv + Papers with Code + GitHub"
        result = SourceResult(source=source, status="ok")
        cursor = self.store.cursor(source)
        if cursor == "done" or self.store.count("RESEARCH_PAPER") >= limit:
            return result
        if self.fetch.fixture is not None:
            links = self.fetch.fixture.get("paper_links", [])
            data = json.dumps(links, sort_keys=True).encode()
        else:
            data = links_path.read_bytes() if links_path else (
                await self.fetch.get(PWC, max_bytes=40_000_000, allow_archive_redirect=True)
            ).content
            links = json.loads(gzip.decompress(data) if data.startswith(b"\x1f\x8b") else data)
        candidates: dict[str, dict[str, bool]] = defaultdict(dict)
        for row in links:
            pid = arxiv_id(row.get("paper_arxiv_id"))
            try:
                repo = repo_slug(row.get("repo_url"))
            except ValueError:
                continue
            if pid and repo:
                candidates[pid][repo] = candidates[pid].get(repo, False) or row.get("is_official") is True
        ids = sorted(candidates, key=lambda pid: (not any(candidates[pid].values()), pid))
        version = hashlib.sha256(data).hexdigest()
        parts = cursor.split(":") if cursor else []
        start = int(parts[1]) if len(parts) == 2 and version == parts[0] else 0
        for _ in range(pages):
            remaining = limit - self.store.count("RESEARCH_PAPER")
            ids_page = ids[start:start + min(50, remaining)]
            if not ids_page:
                self.store.commit_page(source, "done", Page())
                break
            metadata = await self._arxiv(ids_page)
            result.fetched += len(ids_page)
            records, failures = await self._papers_page(ids_page, candidates, metadata)
            end = start + len(ids_page)
            self.store.commit_page(source, None if failures else ("done" if end == len(ids) else f"{version}:{end}"),
                                   Page(records=records))
            result.eligible += len(records)
            if failures:
                result.status = "partial"
                result.details = failures[:8]
                break
            if end == len(ids) or self.store.count("RESEARCH_PAPER") >= limit:
                break
            start = end
        return result

    async def _arxiv(self, ids: list[str]):
        url = f"{ARXIV}?id_list={','.join(quote(pid, safe='/') for pid in ids)}&max_results={len(ids)}"
        try:
            root = fromstring((await self.fetch.get(url)).content)
        except (SyntaxError, DefusedXmlException) as exc:
            raise ValueError("arXiv returned invalid XML") from exc
        if root.tag != "{http://www.w3.org/2005/Atom}feed":
            raise ValueError("unexpected arXiv response")
        rows = {}
        for row in root.findall("a:entry", ATOM):
            row_id = row.findtext("a:id", "", ATOM)
            if "/api/errors" in row_id:
                raise ValueError("arXiv reported a query error")
            pid = arxiv_id(row_id.split("/abs/")[-1])
            if pid and any(e.get("term") in AI_CATEGORIES for e in row.findall("a:category", ATOM)):
                rows[pid] = {
                    "title": " ".join(row.findtext("a:title", "", ATOM).split()),
                    "published_date": row.findtext("a:published", "", ATOM),
                    "authors": [" ".join(a.text.split()) for a in row.findall("a:author/a:name", ATOM) if a.text],
                }
        return rows

    async def _papers_page(self, ids, candidates, metadata):
        await self.prefetch_stars([
            min(candidates[pid], key=lambda repo: (not candidates[pid][repo], repo))
            for pid in ids if pid in metadata
        ])
        in_flight = {}
        records = []
        failures = []

        async def materialize(pid):
            if pid not in metadata or not metadata[pid]["title"]:
                return None
            for candidate in sorted(candidates[pid], key=lambda name: (not candidates[pid][name], name)):
                if candidate not in in_flight:
                    in_flight[candidate] = asyncio.create_task(self._stars(candidate))
                repository = await in_flight[candidate]
                if repository:
                    return Paper(
                        id=f"arxiv:{pid}", source=Source(name="arXiv", url=HttpUrl(f"https://arxiv.org/abs/{pid}")),
                        collectedAt=self.fetch.now(),
                        evidence={"archive_relation": candidate, "official": str(candidates[pid][candidate]).lower()},
                        content=PaperContent(
                            **metadata[pid], paper_url=HttpUrl(f"https://arxiv.org/abs/{pid}"),
                            github_url=repository["url"], github_stars=repository["stars"],
                            github_stars_observed_at=repository["observed_at"], repository_evidence_url=HttpUrl(PWC_EVIDENCE),
                        ),
                    )
            return None

        for row in await asyncio.gather(*(materialize(pid) for pid in ids), return_exceptions=True):
            if isinstance(row, (FetchError, ValueError, ValidationError)):
                failures.append(str(row))
            elif isinstance(row, BaseException):
                raise row
            elif row:
                records.append(row)
        return records, failures

    async def prefetch_stars(self, candidates: list[str]) -> int:
        token = os.environ.get("GITHUB_TOKEN")
        if not token or self.fetch.fixture is not None:
            return 0
        now = self.fetch.now()
        pending = []
        for slug in dict.fromkeys(candidates):
            existing = self.store.repo(slug)
            if not existing or now - datetime.fromisoformat(existing["observed_at"]) > timedelta(hours=24):
                pending.append(slug)
        updated = 0
        for start in range(0, len(pending), 25):
            slugs = pending[start:start + 25]
            fields = []
            for index, slug in enumerate(slugs):
                owner, name = (json.dumps(part) for part in slug.split("/", 1))
                fields.append(f"r{index}:repository(owner:{owner},name:{name}) {{ url isPrivate stargazerCount }}")
            try:
                response = await self.fetch.client.post(
                    "https://api.github.com/graphql",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"query": "{" + " ".join(fields) + "}"},
                )
                if not response.is_success:
                    break
                data = response.json().get("data")
            except (httpx.RequestError, ValueError):
                break
            if not isinstance(data, dict):
                break
            observed = self.fetch.now().isoformat()
            for index, slug in enumerate(slugs):
                item = data.get(f"r{index}")
                if not isinstance(item, dict) or item.get("isPrivate") is not False:
                    continue
                stars, url = item.get("stargazerCount"), public_url(item.get("url"))
                if type(stars) is int and stars >= 0 and url and repo_slug(url):
                    self.store.save_repo(slug, {"url": canonical_url(url), "stars": stars, "observed_at": observed})
                    updated += 1
            if start + 25 < len(pending):
                await asyncio.sleep(0.5)
        return updated

    async def _stars(self, candidate: str) -> dict | None:
        cached = self.store.repo(candidate)
        if cached and self.fetch.now() - datetime.fromisoformat(cached["observed_at"]) <= timedelta(hours=24):
            return cached
        token = os.environ.get("GITHUB_TOKEN")
        url = f"https://api.github.com/repos/{candidate}"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = await self.fetch.get(url, headers=headers, accept_redirect=True)
            if response.is_redirect:
                target = urljoin(url, response.headers.get("Location", ""))
                path = urlsplit(target)
                if path.scheme != "https" or path.netloc != "api.github.com" or not (
                    re.fullmatch(r"/repositories/\d+", path.path) or re.fullmatch(r"/repos/[\w.-]+/[\w.-]+", path.path)
                ):
                    raise ValueError(f"unexpected repository redirect for {candidate}")
                response = await self.fetch.get(target, headers=headers)
        except FetchError as exc:
            if exc.status in {404, 410}:
                return None
            raise
        body = response.json()
        if body.get("private", self.fetch.fixture is None) or body.get("visibility", "public") != "public":
            return None
        stars = body.get("stargazers_count")
        if type(stars) is not int or stars < 0 or not repo_slug(body.get("html_url")):
            raise ValueError(f"missing repository observation for {candidate}")
        cached = {"url": canonical_url(body["html_url"]), "stars": stars, "observed_at": self.fetch.now().isoformat()}
        self.store.save_repo(candidate, cached)
        return cached

    async def run(self, verticals: Iterable[str], limit: int, pages: int, links: Path | None):
        actions: dict[str, Callable] = {
            "startups": self.startups, "products": self.products,
            "papers": lambda n, p: self.papers(n, p, links),
        }
        self.product_orgs = {}

        async def collect(vertical, action, source):
            try:
                return await action(limit, pages)
            except (FetchError, ValueError) as exc:
                return SourceResult(source=source, status="failed", details=[str(exc)])

        names = {"startups": "YC Startup Directory", "products": "Hugging Face Spaces",
                 "papers": "arXiv + Papers with Code + GitHub"}
        results = list(await asyncio.gather(*(collect(v, actions[v], names[v]) for v in verticals)))
        if "products" in verticals and self.store.priced_products() < limit:
            results.append(await collect("products", self.catalog_products, "Harpd AI Datasets"))
        if "products" in verticals and self.store.priced_products() < limit:
            results.append(await collect("products", self.toolbuzz_products, "AIToolBuzz"))
        return results
