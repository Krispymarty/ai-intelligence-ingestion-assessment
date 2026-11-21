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
from .identity import AI, AI_JOB, Resolver, canonical_url, domain, public_url
from .llm import FAMILY, Extractor, configured_providers
from .network import FetchError, Fetcher
from .store import Store

RELATIVE_DATE = re.compile(r"(\d+)\s+(minute|hour|day)s?\s+ago", re.I)


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
    Feed("Himalayas", "https://himalayas.app/jobs/api?limit=20", "jobs", ("himalayas.app",), remote=True),
    Feed("We Work Remotely", "https://weworkremotely.com/remote-jobs.rss", "jobs", ("weworkremotely.com",), remote=True),
    Feed("Arbeitnow", "https://www.arbeitnow.com/api/job-board-api", "jobs", ("arbeitnow.com",)),
]


@dataclass
class Entry:
    title: str
    url: str
    published: object = None
    company: str = ""
    description: str = ""
    tags: str = ""
    remote: bool | None = None
    first_seen_only: bool = False


def clean(html: object) -> str:
    if not isinstance(html, str):
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style, noscript"):
        node.decompose()
    return " ".join(soup.stripped_strings)


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
        if feed.name == "We Work Remotely" and ": " in title:
            company, title = title.split(": ", 1)
        if url and title:
            entries.append(Entry(
                title, url, node.findtext("published") or node.findtext("pubDate") or node.findtext("date"),
                company, node.findtext("encoded") or node.findtext("content") or node.findtext("description")
                or node.findtext("summary") or "",
                " ".join(n.get("term") or n.text or "" for n in node.findall("category")), feed.remote,
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
        if feed.name == "Himalayas":
            guid = row.get("guid")
            if isinstance(guid, str) and public_url(guid):
                url = guid
            elif isinstance(guid, str) and guid.startswith("/jobs/"):
                url = urljoin("https://himalayas.app", guid)
            elif isinstance(row.get("companySlug"), str) and isinstance(row.get("slug"), str):
                url = f"https://himalayas.app/companies/{row['companySlug']}/jobs/{row['slug']}"
        if not title or not url:
            continue
        date_keys = {"Remote OK": ("date", "epoch"), "Jobicy": ("pubDate",),
                     "Himalayas": ("datePosted", "publishedAt"), "Arbeitnow": ("created_at",)}
        date = next((row[key] for key in date_keys[feed.name] if row.get(key)), None)
        description = row.get("jobDescription") or row.get("description") or row.get("jobExcerpt") or ""
        tags = row.get("tags") or row.get("categories") or row.get("jobIndustry") or ""
        entries.append(Entry(title, url, date, company, description, str(tags),
                             row["remote"] if isinstance(row.get("remote"), bool) else feed.remote,
                             first_seen_only=feed.name == "Himalayas" and date is None))
    return entries, len(rows)


def article_date(html: str, kind: str):
    soup = BeautifulSoup(html, "html.parser")
    permitted_keys = ("datePosted",) if kind == "jobs" else ("datePublished",)

    def find(tree):
        if isinstance(tree, dict):
            for key in permitted_keys:
                if key in tree:
                    return tree[key]
            if "@graph" in tree:
                return find(tree["@graph"])
        if isinstance(tree, list):
            return next((date for item in tree if (date := find(item))), None)
        return None

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            if value := find(json.loads(script.get_text())):
                return value
        except (ValueError, TypeError):
            continue
    if kind == "news":
        tag = soup.find("meta", attrs={"property": "article:published_time"})
        if isinstance(tag, Tag):
            return tag.get("content")
    return None


class Monitor:
    def __init__(self, fetch: Fetcher, store: Store, resolver: Resolver, llm_budget: int,
                 full_text_hosts=(), llm_hosts=()):
        self.fetch = fetch
        self.store = store
        self.resolver = resolver
        self.full_text_hosts = set(full_text_hosts)
        self.llm_hosts = set(llm_hosts)
        providers = configured_providers() if fetch.fixture is None else []
        self.extractor = Extractor(fetch.client, providers, llm_budget)
        self.robots: dict[str, RobotFileParser | None] = {}

    async def run(self, as_of: datetime) -> list[SourceResult]:
        return list(await asyncio.gather(*(self.source(feed, as_of) for feed in FEEDS)))

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
            entries, observed = await self.entries(feed)
            result.fetched = observed
            seen = self.fetch.now()
            for entry in entries:
                if (feed.kind == "news" and not feed.ai_only and not AI.search(f"{entry.title} {entry.tags}")) or (
                    feed.kind == "jobs" and not AI_JOB.search(f"{entry.title} {entry.tags}")
                ):
                    continue
                try:
                    await self.materialize(entry, feed, as_of, seen, page, result)
                except ValueError as exc:
                    result.status = "partial"
                    if len(result.details) < 12:
                        result.details.append(f"invalid entry {entry.url}: {type(exc).__name__}")
        except (FetchError, DefusedXmlException, ValueError, TypeError, AttributeError, SyntaxError) as exc:
            result.status = "failed"
            result.details.append(str(exc))
        self.store.commit_page(feed.name, None, page)
        result.eligible = len({record.id for record in page.records})
        return result

    async def entries(self, feed: Feed) -> tuple[list[Entry], int]:
        response = await self.fetch.get(feed.url, max_bytes=8_000_000)
        if feed.kind == "news" or feed.name == "We Work Remotely":
            entries = xml_entries(response.content, feed)
            return entries, len(entries)
        return json_jobs(response.json(), feed)

    async def materialize(self, entry: Entry, feed: Feed, as_of: datetime, seen: datetime,
                          page: Page, result: SourceResult):
        url = canonical_url(entry.url)
        published = published_at(entry.published, seen)
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
        if article and (observed := published_at(article_date(article, feed.kind), seen)):
            published = observed
            timestamp_source = "page publication metadata"
        if published is None:
            page.undated.setdefault(url, seen)
            result.undated += 1
            return
        relative = RELATIVE_DATE.fullmatch(str(entry.published).strip())
        if (relative and timestamp_source == "feed publication field"
                and published - timedelta(**{f"{relative[2].lower()}s": 1}) < as_of - timedelta(hours=24)):
            page.undated.setdefault(url, seen)
            result.undated += 1
            return
        if not as_of - timedelta(hours=24) <= published <= as_of:
            return
        full = main_text(article, include_comments=False, include_tables=False) if article else None
        evidence = {"publication_source": timestamp_source, "publication_raw": str(
            entry.published if timestamp_source.startswith("feed") else article_date(article or "", feed.kind)
        ), "feed": feed.url}
        if full:
            page.full_text[url] = full
        source = Source(name=feed.name, url=HttpUrl(entry.url))
        excerpt = clean(entry.description)[:240] if feed.kind == "news" else ""
        if feed.kind == "news":
            page.records.append(News(
                id="news:" + hashlib.sha256(url.encode()).hexdigest(), source=source,
                collectedAt=seen, evidence=evidence,
                content=NewsContent(title=entry.title, date=published, excerpt=excerpt),
            ))
            return
        record_id = "job:" + hashlib.sha256(url.encode()).hexdigest()
        company = entry.company or None
        if company:
            mapping = self.resolver.resolve(company, entry.url)
            page.mappings.append(mapping)
            company = mapping.canonical_name or company
        previous = self.store.get_record(record_id)
        family: Family | None = None
        text = full or clean(entry.description)
        evidence_input = f"{entry.title}\n\n{text}"
        input_hash = hashlib.sha256(evidence_input.encode()).hexdigest()
        if (isinstance(previous, Job) and previous.content.date == published
                and previous.content.role_family and previous.evidence.get("role_family")
                and previous.evidence.get("role_input_sha256") == input_hash):
            family = previous.content.role_family
            evidence["role_family"] = previous.evidence["role_family"]
        elif domain(entry.url) in self.llm_hosts and self.extractor.providers:
            if text:
                claim, attempts = await self.extractor.extract(evidence_input, title=entry.title)
                if claim:
                    family = claim.role_family
                    evidence["role_family"] = claim.quote
                if len(result.details) < 12:
                    result.details.append(f"LLM {entry.url}: {', '.join(attempts)}")
        if family is None:
            for label, pattern in FAMILY.items():
                match = re.search(pattern, entry.title, re.I)
                if match:
                    family = label
                    evidence["role_family"] = match[0]
                    break
        if family:
            evidence["role_input_sha256"] = input_hash
        page.records.append(Job(
            id=record_id, source=source, collectedAt=seen, evidence=evidence,
            content=JobContent(title=entry.title, company=company, date=published,
                               is_remote=entry.remote, role_family=family, excerpt=excerpt),
        ))
