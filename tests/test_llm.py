import asyncio
import json

import httpx

from assessment.llm import Extractor, Provider

PROVIDERS = [
    Provider("Groq", "https://groq.example/complete", "test", "fixture"),
    Provider("Cerebras", "https://cerebras.example/complete", "test", "fixture"),
]


def claim(role, quote):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
        "role_family": role, "quote": quote,
    })}}]})


def run(handler, text, budget=10, title=""):
    pauses = []

    async def pause(delay):
        pauses.append(delay)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            extractor = Extractor(client, PROVIDERS, budget, sleep=pause)
            return await extractor.extract(text, title=title)

    return asyncio.run(scenario()), pauses


def test_retries_429_and_uses_fallback_without_ignoring_retry_after():
    requests = []

    def respond(request):
        requests.append(request.url.host)
        if request.url.host == "groq.example":
            return httpx.Response(429, headers={"Retry-After": "3"})
        return claim("Research", "AI Research Scientist")

    (result, events), pauses = run(respond, "AI Research Scientist")
    assert result and result.role_family == "Research"
    assert pauses == [3]
    assert requests == ["groq.example", "groq.example", "cerebras.example"]
    assert "Groq: 429" in events


def test_long_retry_after_falls_back_without_retrying_throttled_provider():
    def respond(request):
        if request.url.host == "groq.example":
            return httpx.Response(429, headers={"Retry-After": "120"})
        return claim("Research", "AI Research Scientist")

    (result, events), pauses = run(respond, "AI Research Scientist")
    assert result and result.role_family == "Research"
    assert pauses == []
    assert events == ["Groq: 429", "Cerebras: accepted"]


def test_413_splits_paragraphs_and_preserves_the_grounded_role():
    first = "AI Research Scientist"
    second = "Our company works on language models."

    def respond(request):
        text = json.loads(request.content)["messages"][1]["content"]
        if "\n\n" in text:
            return httpx.Response(413)
        if text == first:
            return claim("Research", first)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    (result, events), _ = run(respond, f"{first}\n\n{second}")
    assert result and result.quote == first
    assert events == ["Groq: payload too large", "Groq: accepted"]
    (partial, _), _ = run(respond, f"{first}\n\n{second}", budget=2)
    assert partial is None


def test_invalid_output_and_real_quote_for_wrong_role_are_rejected():
    def respond(request):
        if request.url.host == "groq.example":
            return httpx.Response(200, json={"choices": [{"message": {"content": "{broken"}}]})
        return claim("Engineering", "work with our engineering team")

    (result, events), _ = run(respond, "AI Product Manager\n\nwork with our engineering team",
                              title="AI Product Manager")
    assert result is None
    assert events == ["Groq: invalid or unsupported output", "Cerebras: invalid or unsupported output",
                      "provider chain exhausted"]


def test_fabricated_quote_falls_back():
    def respond(request):
        if request.url.host == "groq.example":
            return claim("Engineering", "Software Engineer")
        return claim("Research", "AI Research Scientist")

    (result, events), _ = run(respond, "AI Research Scientist")
    assert result and result.quote == "AI Research Scientist"
    assert events == ["Groq: invalid or unsupported output", "Cerebras: accepted"]


def test_pricing_fallback_rejects_quote_that_omits_negation():
    def respond(request):
        paid = request.url.host == "groq.example"
        claim = {
            "field": "pricingModel", "value": "PAID" if paid else "FREEMIUM",
            "quote": "paid subscription required" if paid else "a free plan plus a paid plan",
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"claims": [claim]})}}]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as client:
            return await Extractor(client, PROVIDERS, 4).extract_fields(
                "PRODUCT", "No paid subscription required. Choose a free plan plus a paid plan for optional features.",
            )

    claims, events = asyncio.run(scenario())
    assert [(claim.field, claim.value) for claim in claims] == [("pricingModel", "FREEMIUM")]
    assert events == ["Groq: invalid or unsupported output", "Cerebras: accepted"]
