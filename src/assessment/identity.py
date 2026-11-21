import csv
import re
import unicodedata
from importlib.resources import files
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import HttpUrl

from .contract import Mapping

AI = re.compile(
    r"\b(?:ai|genai|aigc|artificial intelligence|machine learning|deep learning|computer vision|"
    r"llms?|large language models?|nlp|natural language processing|generative ai|mlops|"
    r"predictive models?|diffusion models?|text.to.image|text.to.speech|speech recognition)\b",
    re.IGNORECASE,
)
AI_JOB = re.compile(
    r"\b(?:ai|ml|mlops|llms?|genai|machine learning|artificial intelligence|deep learning|"
    r"computer vision|nlp|research scientist|applied scientist)\b", re.I,
)


def domain(value: str) -> str | None:
    try:
        host = urlsplit(value if "://" in value else f"https://{value}").hostname
    except ValueError:
        return None
    return host.casefold().removeprefix("www.").rstrip(".") if host and "." in host else None


def public_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
            return None
        if parts.port not in {None, 80, 443} or not domain(value):
            return None
        HttpUrl(value)
    except ValueError:
        return None
    return value.strip()


def canonical_url(value: str) -> str:
    parts = urlsplit(value)
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
              if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit(("https", domain(value) or parts.netloc, parts.path.rstrip("/") or "/", urlencode(sorted(params)), ""))


def normalized_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold().strip()
    text = re.sub(r"(?:[\s,]+(?:incorporated|inc|llc|ltd|corp|limited|gmbh)\.?)$", "", text).strip()
    return "".join(c for c in text if c.isalnum())


class Resolver:
    def __init__(self):
        self.aliases: dict[str, set[str]] = {}
        self.domains: dict[str, set[str]] = {}
        with files("assessment").joinpath("companies.csv").open(encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                canonical = row["canonical"]
                for alias in [canonical, *row["aliases"].split("|")]:
                    if key := normalized_name(alias):
                        self.aliases.setdefault(key, set()).add(canonical)
                if host := domain(row["domain"]):
                    self.domains.setdefault(host, set()).add(canonical)

    def resolve(self, raw: str, url: str, company_domain: str | None = None) -> Mapping:
        alias = self.aliases.get(normalized_name(raw), set())
        domains = self.domains.get(company_domain or "", set())
        if len(alias | domains) > 1:
            return Mapping(source_url=HttpUrl(url), raw_name=raw, reason="conflicting evidence")
        if canonical := next(iter(alias | domains), None):
            return Mapping(source_url=HttpUrl(url), raw_name=raw, canonical_name=canonical,
                           reason="domain match" if domains else "exact normalized alias")
        return Mapping(source_url=HttpUrl(url), raw_name=raw, reason="no seed match")
