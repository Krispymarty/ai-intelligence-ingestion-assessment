from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, HttpUrl, TypeAdapter

NonNegative = Annotated[int, Field(ge=0, strict=True)]
Pricing = Literal["FREE", "FREEMIUM", "PAID", "ENTERPRISE"]
Family = Literal["Engineering", "Research", "Data", "Product", "Design", "Operations", "Sales"]


class Source(BaseModel):
    name: str
    url: HttpUrl


class StartupData(BaseModel):
    employeeCount: NonNegative | None = None


class StartupContent(BaseModel):
    entityName: str
    domain: str | None = None
    data: StartupData = Field(default_factory=StartupData)


class ProductContent(BaseModel):
    productName: str
    startupName: str
    pricingModel: Pricing | None = None
    productUrl: HttpUrl


class PaperContent(BaseModel):
    title: str
    authors: list[str] = Field(min_length=1)
    paper_url: HttpUrl
    github_url: HttpUrl
    github_stars: NonNegative
    github_stars_observed_at: AwareDatetime
    published_date: AwareDatetime
    repository_evidence_url: HttpUrl


class JobContent(BaseModel):
    title: str
    company: str | None = None
    date: AwareDatetime
    is_remote: bool | None = None
    role_family: Family | None = None
    excerpt: str = ""


class NewsContent(BaseModel):
    title: str
    date: AwareDatetime
    excerpt: str = ""


class BaseRecord(BaseModel):
    schemaVersion: Literal["1.0"] = "1.0"
    id: str = Field(min_length=1)
    source: Source
    collectedAt: AwareDatetime
    evidence: dict[str, str] = Field(default_factory=dict)


class Startup(BaseRecord):
    recordType: Literal["STARTUP"] = "STARTUP"
    content: StartupContent


class Product(BaseRecord):
    recordType: Literal["PRODUCT"] = "PRODUCT"
    content: ProductContent


class Paper(BaseRecord):
    recordType: Literal["RESEARCH_PAPER"] = "RESEARCH_PAPER"
    content: PaperContent


class Job(BaseRecord):
    recordType: Literal["JOB"] = "JOB"
    content: JobContent


class News(BaseRecord):
    recordType: Literal["NEWS"] = "NEWS"
    content: NewsContent


Record = Startup | Product | Paper | Job | News
RECORD = TypeAdapter(Annotated[Record, Field(discriminator="recordType")])
TARGETS = {"STARTUP": 1000, "PRODUCT": 1000, "RESEARCH_PAPER": 1000}


class Mapping(BaseModel):
    source_url: HttpUrl
    raw_name: str
    canonical_name: str | None = None
    reason: str


class SourceResult(BaseModel):
    source: str
    status: Literal["ok", "partial", "failed"]
    fetched: int = 0
    eligible: int = 0
    undated: int = 0
    details: list[str] = Field(default_factory=list)


class Page(BaseModel):
    records: list[Annotated[Record, Field(discriminator="recordType")]] = Field(default_factory=list)
    mappings: list[Mapping] = Field(default_factory=list)
    undated: dict[str, datetime] = Field(default_factory=dict)
    full_text: dict[str, str] = Field(default_factory=dict)
