from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

ROOT = Path(__file__).resolve().parents[1]
INK = colors.HexColor("#142436")
MUTED = colors.HexColor("#415467")
ACCENT = colors.HexColor("#0D6666")


def style(name, font, size, leading, color, space=0):
    return ParagraphStyle(name, fontName=font, fontSize=size, leading=leading,
                          textColor=color, spaceAfter=space, alignment=TA_LEFT)


label = style("label", "Helvetica-Bold", 10, 14, ACCENT, 12)
title = style("title", "Helvetica-Bold", 27, 32, INK, 12)
intro = style("intro", "Helvetica", 12, 18, MUTED, 18)
header = style("header", "Helvetica-Bold", 13, 18, ACCENT, 5)
body = style("body", "Helvetica", 10.5, 15.4, INK, 11)
flow = style("flow", "Courier-Bold", 9, 14, INK, 10)


def paragraph(value, format_):
    return Paragraph(escape(value), format_)


def section(heading, text):
    return [paragraph(heading, header), paragraph(text, body)]


def page(canvas, document):
    width, height = A4
    canvas.setStrokeColor(ACCENT)
    canvas.setLineWidth(1)
    canvas.line(48, height - 32, width - 48, height - 32)
    canvas.setStrokeColor(colors.HexColor("#D4DEE5"))
    canvas.line(48, 47, width - 48, 47)
    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(MUTED)
    canvas.drawString(48, 32, "AI Engineer Assessment  /  Technical Architecture")
    canvas.drawRightString(width - 48, 32, f"{document.page} / 3")


def build():
    story = [
        paragraph("01  /  PIPELINE AND DATA QUALITY", label),
        paragraph("Auditable AI intelligence ingestion", title),
        paragraph("One asynchronous Python CLI gathers bulk entities and fresh signals. A validated record contract and traceable source observations support reproducible exports.", intro),
        paragraph("Source adapters -> validation -> SQLite checkpoint and records", flow),
        paragraph("                 -> six spreadsheet tabs + JSON run report", flow),
        Spacer(1, 11),
        *section("Bulk acquisition", "Read the YC snapshot and retain original directory URLs. Combine company-attributed web and mobile products with stable hosted model API offers. Require pricing evidence and merge aliases for the same offer. Join arXiv metadata to Papers with Code repository links by unversioned arXiv ID. Prefer official links and keep the GitHub star count with its observation time."),
        *section("Strict freshness", "Run five AI news feeds and five job boards asynchronously. Evaluate authoritative publication timestamps against a recorded UTC cutoff and its preceding 24 hours. Keep first-seen values separately. Updated timestamps or insufficiently precise dates do not establish publication. Report source errors separately from a successful source with zero eligible entries."),
        *section("Validation and entity identity", "Use versioned Pydantic records and field evidence. Keep optional data null when unsupported. Resolve explicit normalized aliases and domain matches against the 50-company seed. Conflicting evidence stays unresolved. Canonical URLs and source-specific IDs identify records, while a separate log records entity mapping outcomes."),
        *section("Access and content handling", "Use public feeds, APIs and licensed snapshots. Harpd returned HTTP 403 on its site; the publisher's separate CC BY 4.0 GitHub export supplied the catalog. Log inaccessible or challenged paths. Fetch permitted article pages under publisher and robots rules, and retain full text locally."),
        PageBreak(),
        paragraph("02  /  EXTRACTION RELIABILITY", label),
        paragraph("Bound work and preserve evidence", title),
        paragraph("Source fields carry their existing evidence. The model handles permitted unstructured text through a bounded, testable provider interface.", intro),
        paragraph("Permitted text -> byte/token limits -> Groq / Gemini / Cerebras", flow),
        paragraph("               -> schema + quote checks -> accepted field or null", flow),
        Spacer(1, 11),
        *section("Oversize requests", "Check serialized UTF-8 bytes and a conservative input token bound before sending. Set a separate output allowance. Split oversized input at paragraph boundaries with a midpoint fallback. Treat both provider HTTP 413 and reported context-window overflow as split-and-retry cases. Cap split depth, provider attempts and the total run call budget."),
        *section("Throttling and failover", "Apply exponential backoff with jitter and honor Retry-After. Cool down the rate-limited provider and advance to the next configured tier when the delay exceeds the retry budget. In production, enforce provider-wide requests/minute, tokens/minute and daily allowances with shared counters ahead of dispatch. Reconcile quota estimates with provider headers and actual usage."),
        *section("Evidence and failure reporting", "Validate output JSON, require a verbatim quote in the input and check whether the quote supports the proposed field. Conflicting or exhausted extraction attempts leave the field unsupported. Store a reason and preserve the underlying source record. In replay, provider responses are controlled at the HTTP seam."),
        *section("Restart behavior", "The local CLI commits page records and checkpoints together. A transient page error may persist validated observations while retaining the previous cursor. A repeated source read upserts stable record IDs. Replay databases are isolated from submission data."),
        PageBreak(),
        paragraph("03  /  PRODUCTION DESIGN", label),
        paragraph("From trial volume to 500,000 records", title),
        paragraph("Keep the same source adapters and record contract. Move scheduling, shared rate control and persistence behind distributed adapters.", intro),
        *section("Partitioning and scale model", "Partition bulk jobs by source snapshot, category, cursor range or source ID. Stream large snapshots to object storage and emit bounded tasks. At a sustained aggregate 25 accepted records/second, 500,000 rows take about 5.6 hours of active ingestion. At a 20% model-enrichment rate and 120 successful calls/minute across providers, enrichment alone takes about 13.9 hours before retries. These are planning scenarios, not measured throughput. Source density, provider quotas and approval for access determine practical rates."),
        *section("Distributed deduplication and recovery", "PostgreSQL workers claim stable source tasks with renewable leases. A transaction checks lease ownership when it writes observations and advances the checkpoint. Expired leases can be reclaimed. Unique record keys make commits idempotent across retries. Lease loss may repeat an upstream call. Ambiguous records retain discovery timestamps outside fresh exports."),
        *section("Storage and entity relationships", "Keep PostgreSQL as the source of truth. Store immutable raw inputs and evidence blobs in object storage, referenced by hash and source URL. Model company, product, paper and repository links in indexed relational tables. Consider pgvector for measured semantic search needs and a graph store only when graph traversals prove too expensive in PostgreSQL."),
        *section("Operate and measure", "A local replay persisted 500,000 synthetic records across all five schemas in 20.38 seconds and revalidated them in 18.51 seconds. Replaying 100 rows left all counts unchanged. The test excludes network and provider calls. Monitor freshness, source yield, queue age, validation failures and provider quotas in live operation."),
    ]
    report = SimpleDocTemplate(str(ROOT / "architecture.pdf"), pagesize=A4, leftMargin=48,
                               rightMargin=48, topMargin=54, bottomMargin=57,
                               title="AI Engineer Assessment - Technical Architecture",
                               author="AI Engineer Assessment")
    report.build(story, onFirstPage=page, onLaterPages=page)


if __name__ == "__main__":
    build()
