import asyncio

import httpx

from assessment.llm import Extractor, configured_providers


async def main() -> int:
    providers = configured_providers()
    if not providers:
        print("No providers have both a free-tier model and a key configured.")
        return 2
    failed = False
    title = "AI Research Scientist"
    async with httpx.AsyncClient(timeout=20) as client:
        for provider in providers:
            result, events = await Extractor(client, [provider], 2).extract(
                f"{title}\n\nWe are hiring an AI Research Scientist.", title=title,
            )
            passed = result is not None and result.role_family == "Research"
            print(f"{provider.name}: {'PASS' if passed else 'FAIL'} ({', '.join(events)})")
            failed |= not passed
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
