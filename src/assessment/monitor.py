import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup, Tag
from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException
from pydantic import HttpUrl
from trafilatura import extract as main_text

from .contract import Family, Job, JobContent, News, NewsContent, Page, Source, SourceResult
from .enrichment import extract_once
from .identity import AI, AI_JOB, Resolver, canonical_url, domain, public_url
from .llm import FAMILY, Extractor, classify_title, configured_providers
from .network import FetchError, Fetcher
from .store import Store

RELATIVE_DATE = re.compile(r"(\d+)\s+(minute|hour|day)s?\s+ago", re.I)
JOB_POLICY = re.compile(r"\b(?:(?:company|employer|workplace|location)\s*:|(?:fully remote|on.site|in office|hybrid)\b)", re.I)


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    kind: str
    article_hosts: tuple[str, ...] = ()
    ai_only: bool = False
    remote: bool | None = None


FEEDS = [
    Feed("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/", "news", ("techcrunch.com",), True),
    Feed("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "news", ("theverge.com",), True),
    Feed("Ars Technica AI", "https://arstechnica.com/ai/feed/", "news", ("arstechnica.com",), True),
    Feed("MIT Technology Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed", "news",
         ("technologyreview.com",), True),
    Feed("WIRED", "https://www.wired.com/feed/rss", "news", ("wired.com",)),
    Feed("Remote OK", "https://remoteok.com/api", "jobs", ("remoteok.com",), remote=True),
    Feed("Jobicy", "https://jobicy.com/api/v2/remote-jobs?count=100", "jobs", ("jobicy.com",), remote=True),
    Feed("Mo AIJobs", "https://www.moaijobs.com/ai-jobs.rss", "jobs", ("moaijobs.com",), ai_only=True),
    Feed("We Work Remotely", "https://weworkremotely.com/remote-jobs.rss", "jobs", ("weworkremotely.com",), remote=True),
    Feed("Arbeitnow", "https://www.arbeitnow.com/api/job-board-api", "jobs", ("arbeitnow.com",)),
]
RSS_JOB_BOARDS = {"We Work Remotely", "Mo AIJobs"}


@dataclass
class Entry:
    title: str
    url: str
    published: object = None
    company: str = ""
    description: str = ""
    tags: str = ""
    remote: bool | None = None
    full_body: str = ""


def clean(html: object) -> str:
    if not isinstance(html, str):
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style, noscript"):
        node.decompose()
    return " ".join(soup.stripped_strings)


def extraction_input(kind: str, title: str, text: str) -> str:
    if kind != "jobs":
        return f"{title}\n\n{text}"
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    relevant = [s for s in sentences if len(s) <= 400 and JOB_POLICY.search(s)][:4]
    if not relevant:
        relevant = [text[:900]]
    return "\n".join([title, "", *relevant])


def published_at(value: object, seen: datetime) -> datetime | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return datetime.fromtimestamp(value, UTC)
        if not isinstance(value, str):
            return None
        text = value.strip()
        if re.fullmatch(r"\d{10}", text):
            return datetime.fromtimestamp(int(text), UTC)
        if match := RELATIVE_DATE.fullmatch(text):
            return seen - timedelta(**{f"{match[2].lower()}s": int(match[1])})
        if not re.search(r"\d{1,2}:\d{2}", text):
            return None
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            result = parsedate_to_datetime(text)
        return result.astimezone(UTC) if result.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def xml_entries(body: bytes, feed: Feed) -> list[Entry]:
    root = fromstring(body)
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    if root.tag not in {"rss", "RDF", "feed"}:
        raise ValueError("not an RSS or Atom feed")
    entries = []
    for node in [*root.findall(".//item"), *root.findall("./entry")]:
        link = next((n for n in node.findall("link") if n.get("rel", "alternate") == "alternate"), None)
        url = public_url((link.get("href") or link.text) if link is not None else None)
        title = clean(node.findtext("title"))
        company = clean(node.findtext("company"))
        remote = feed.remote
        if feed.name == "We Work Remotely" and ": " in title:
            company, title = title.split(": ", 1)
        if feed.name == "Mo AIJobs":
            if company and title.endswith(f" at {company}"):
                title = title.removesuffix(f" at {company}")
            location = clean(node.findtext("location"))
            if re.search(r"\bremote\b", location, re.I) and not re.search(r"\b(?:non.remote|no remote)\b", location, re.I):
                remote = True
            elif re.search(r"\b(?:onsite|on-site|hybrid|in.office)\b", location, re.I):
                remote = False
        if url and title:
            full = node.findtext("encoded") or node.findtext("content") or ""
            if feed.name == "Mo AIJobs" and not full:
                full = node.findtext("description") or ""
            entries.append(Entry(
                title, url, node.findtext("published") or node.findtext("pubDate") or node.findtext("date"),
                company, node.findtext("description") or node.findtext("summary") or full,
                " ".join(n.get("term") or n.text or "" for n in node.findall("category")), remote,
                full_body=full,
            ))
    return entries


def json_jobs(body, feed: Feed) -> tuple[list[Entry], int]:
    rows = body if feed.name == "Remote OK" else body.get("jobs", body.get("data"))
    if not isinstance(rows, list):
        raise ValueError("unexpected job feed shape")
    entries = []
    for row in rows:
        title = clean(row.get("position") or row.get("jobTitle") or row.get("title"))
        company = clean(row.get("companyName") or row.get("company_name") or row.get("company"))
        url = public_url(row.get("url") or row.get("applicationLink"))
        if not title or not url:
            continue
        date_keys = {"Remote OK": ("date", "epoch"), "Jobicy": ("pubDate",),
                     "Arbeitnow": ("created_at",)}
        date = next((row[key] for key in date_keys[feed.name] if row.get(key)), None)
        full = row.get("jobDescription") or row.get("description") or ""
        description = row.get("jobExcerpt") or full
        tags = row.get("tags") or row.get("categories") or row.get("jobIndustry") or ""
        entries.append(Entry(title, url, date, company, description, str(tags),
                             row["remote"] if isinstance(row.get("remote"), bool) else feed.remote,
                             full_body=full))
    return entries, len(rows)


def article_date(html: str, kind: str, url: str):
    soup = BeautifulSoup(html, "html.parser")
    date_key = "datePosted" if kind == "jobs" else "datePublished"
    permitted_types = {"JobPosting"} if kind == "jobs" else {"Article", "NewsArticle", "BlogPosting"}
    dates = []

    def find(tree):
        if isinstance(tree, dict):
            entity_types = tree.get("@type", [])
            if isinstance(entity_types, str):
                entity_types = [entity_types]
            if permitted_types.intersection(entity_types) and isinstance(tree.get(date_key), str):
                link = tree.get("url") or tree.get("mainEntityOfPage") or tree.get("@id")
                if isinstance(link, dict):
                    link = link.get("@id") or link.get("url")
                if not link or (isinstance(link, str) and canonical_url(urljoin(url, link)) == canonical_url(url)):
                    dates.append(tree[date_key])
            if "@graph" in tree:
                find(tree["@graph"])
        if isinstance(tree, list):
            for item in tree:
                find(item)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            find(json.loads(script.get_text()))
        except (ValueError, TypeError):
            continue
    if len(set(dates)) == 1:
        return dates[0]
    if dates:
        return None
    if kind == "news":
        tag = soup.find("meta", attrs={"property": "article:published_time"})
        if isinstance(tag, Tag):
            return tag.get("content")
    return None


class Monitor:
    def __init__(self, fetch: Fetcher, store: Store, resolver: Resolver, llm_budget: int,
                 full_text_hosts=(), llm_hosts=(), max_pages: int = 12):
        self.fetch = fetch
        self.store = store
        self.resolver = resolver
        self.full_text_hosts = set(full_text_hosts)
        self.llm_hosts = set(llm_hosts)
        self.max_pages = max_pages
        providers = configured_providers() if fetch.fixture is None else []
        self.extractor = Extractor(fetch.client, providers, llm_budget)
        self.robots: dict[str, RobotFileParser | None] = {}

    async def run(self, as_of: datetime, source: str | None = None) -> list[SourceResult]:
        return list(await asyncio.gather(*(self.source(feed, as_of) for feed in FEEDS if source in {None, feed.name})))

    async def get_article(self, entry: Entry, feed: Feed):
        host = domain(entry.url)
        if host not in feed.article_hosts or host not in self.full_text_hosts:
            return None
        parts = urlsplit(entry.url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            robots = RobotFileParser()
            try:
                robots.parse((await self.fetch.get(origin + "/robots.txt", max_bytes=512_000)).text.splitlines())
            except FetchError as exc:
                if exc.status == 404:
                    robots.parse([])
                else:
                    self.robots[origin] = None
                    raise
            self.robots[origin] = robots
        policy = self.robots[origin]
        if policy is None or not policy.can_fetch("AI-Assessment", entry.url):
            return None
        return (await self.fetch.get(entry.url, max_bytes=2_500_000)).text

    async def source(self, feed: Feed, as_of: datetime) -> SourceResult:
        result = SourceResult(source=feed.name, status="ok")
        page = Page()
        try:
            url = feed.url
            visited = set()
            while url and result.pages < self.max_pages:
                if url in visited:
                    raise ValueError("feed repeated its pagination cursor")
                visited.add(url)
                entries, observed, url = await self.entries(feed, url)
                result.pages += 1
                result.fetched += observed
                seen = self.fetch.now()
                for entry in entries:
                    if (feed.kind == "news" and not feed.ai_only and not AI.search(f"{entry.title} {entry.tags}")) or (
                        feed.kind == "jobs" and not feed.ai_only and not AI_JOB.search(f"{entry.title} {entry.tags}")
                    ):
                        continue
                    try:
                        await self.materialize(entry, feed, as_of, seen, page, result)
                    except ValueError as exc:
                        result.status = "partial"
                        if len(result.details) < 12:
                            result.details.append(f"invalid entry {entry.url}: {type(exc).__name__}")
            if url:
                result.status = "partial"
                result.details.append(f"pagination budget reached ({self.max_pages} pages)")
        except (FetchError, DefusedXmlException, ValueError, TypeError, AttributeError, SyntaxError) as exc:
            result.status = "partial" if result.pages else "failed"
            result.details.append(str(exc))
        self.store.commit_page(feed.name, None, page)
        result.eligible = len({record.id for record in page.records})
        result.full_text = len(page.full_text)
        return result

    async def entries(self, feed: Feed, url: str) -> tuple[list[Entry], int, str | None]:
        response = await self.fetch.get(url, max_bytes=24_000_000)
        if feed.kind == "news" or feed.name in RSS_JOB_BOARDS:
            entries = xml_entries(response.content, feed)
            return entries, len(entries), None
        body = response.json()
        entries, count = json_jobs(body, feed)
        next_url = None
        if feed.name == "Arbeitnow" and isinstance(body.get("links"), dict) and body["links"].get("next"):
            next_url = urljoin(feed.url, str(body["links"]["next"]))
        if next_url:
            expected, candidate = urlsplit(feed.url), urlsplit(next_url)
            if (candidate.scheme, candidate.netloc, candidate.path) != (expected.scheme, expected.netloc, expected.path):
                raise ValueError("feed next link points outside the configured API")
        return entries, count, next_url

    async def materialize(self, entry: Entry, feed: Feed, as_of: datetime, seen: datetime,
                          page: Page, result: SourceResult):
        url = canonical_url(entry.url)
        raw_publication = entry.published
        published = published_at(raw_publication, seen)
        timestamp_source = "feed publication field"
        if published and not as_of - timedelta(hours=24) <= published <= as_of:
            return
        article = None
        try:
            article = await self.get_article(entry, feed)
        except FetchError as exc:
            result.status = "partial"
            if len(result.details) < 12:
                result.details.append(str(exc))
        if article and published is None:
            page_date = article_date(article, feed.kind, entry.url)
            if observed := published_at(page_date, seen):
                published = observed
                raw_publication = page_date
                timestamp_source = "page publication metadata"
        full = clean(entry.full_body)
        if article and (article_text := main_text(article, include_comments=False, include_tables=False)):
            full = article_text
        text = full or clean(entry.description)
        input_text = f"{entry.title}\n\n{text}"
        input_hash = hashlib.sha256(input_text.encode()).hexdigest()
        prior_hash = self.store.observation_hash(url)
        if url not in page.observations:
            result.first_seen += prior_hash is None
            result.content_changed += prior_hash is not None and prior_hash != input_hash
        page.observations[url] = (seen, input_hash)
        if published is None:
            page.undated.setdefault(url, seen)
            result.undated += 1
            return
        relative = RELATIVE_DATE.fullmatch(str(raw_publication).strip())
        if relative and published - timedelta(**{f"{relative[2].lower()}s": 1}) < as_of - timedelta(hours=24):
            page.undated.setdefault(url, seen)
            result.undated += 1
            return
        if not as_of - timedelta(hours=24) <= published <= as_of:
            return
        evidence = {"publication_source": timestamp_source, "publication_raw": str(raw_publication), "feed": feed.url}
        if full:
            page.full_text[url] = full
            evidence["full_text_source"] = "publisher page" if article and full != clean(entry.full_body) else "feed full-content field"
        source = Source(name=feed.name, url=HttpUrl(entry.url))
        excerpt = clean(entry.description)[:240] if feed.kind == "news" else ""
        record_id = f"{'news' if feed.kind == 'news' else 'job'}:" + hashlib.sha256(url.encode()).hexdigest()
        previous = self.store.get_record(record_id)
        claims = {}
        if text and domain(entry.url) in self.llm_hosts and self.extractor.providers:
            if isinstance(previous, (Job, News)) and previous.evidence.get("llm_input_sha256") == input_hash:
                evidence.update({k: v for k, v in previous.evidence.items() if k.startswith("llm_")})
            else:
                extracted, attempts = await extract_once(self.store, self.extractor,
                    "NEWS" if feed.kind == "news" else "JOB",
                    extraction_input(feed.kind, entry.title, text), title=entry.title,
                )
                claims = {claim.field: claim for claim in extracted}
                result.llm_fields += len(claims)
                for claim in extracted:
                    if claim.field == "excerpt" and claim.quote not in excerpt:
                        evidence["llm_excerpt_sha256"] = hashlib.sha256(claim.quote.encode()).hexdigest()
                    else:
                        evidence[f"llm_{claim.field}"] = claim.quote
                if claims:
                    evidence["llm_input_sha256"] = input_hash
                if len(result.details) < 12:
                    result.details.append(f"LLM {entry.url}: {', '.join(attempts)}")
        if feed.kind == "news":
            selected = evidence.get("llm_excerpt")
            if selected and selected in excerpt:
                excerpt = selected
            page.records.append(News(
                id=record_id, source=source,
                collectedAt=seen, evidence=evidence,
                content=NewsContent(title=entry.title, date=published, excerpt=excerpt),
            ))
            return
        company = entry.company or None
        remote = entry.remote
        if company is None and (claim := claims.get("company")) and isinstance(claim.value, str):
            company = claim.value
        if remote is None and (claim := claims.get("is_remote")) and isinstance(claim.value, bool):
            remote = claim.value
        if isinstance(previous, Job) and evidence.get("llm_input_sha256") == input_hash:
            if evidence.get("llm_company"):
                company = company or previous.content.company
            if remote is None and evidence.get("llm_is_remote"):
                remote = previous.content.is_remote
        if company:
            mapping = self.resolver.resolve(company, entry.url)
            page.mappings.append(mapping)
            company = mapping.canonical_name or company
        family: Family | None = None
        if (isinstance(previous, Job) and previous.content.date == published
                and previous.content.role_family and previous.evidence.get("role_family")
                and previous.evidence.get("role_input_sha256") == input_hash):
            family = previous.content.role_family
            evidence["role_family"] = previous.evidence["role_family"]
        elif (claim := claims.get("role_family")) and isinstance(claim.value, str):
            for label in FAMILY:
                if claim.value == label:
                    family = label
                    evidence["role_family"] = claim.quote
                    break
        if family is None and (match := classify_title(entry.title)):
            family, evidence["role_family"] = match
        if family:
            evidence["role_input_sha256"] = input_hash
        page.records.append(Job(
            id=record_id, source=source, collectedAt=seen, evidence=evidence,
            content=JobContent(title=entry.title, company=company, date=published,
                               is_remote=remote, role_family=family, excerpt=excerpt),
        ))
