import asyncio
import json
from collections import defaultdict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def retry_delay(value: str | None, now: datetime) -> float:
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - now).total_seconds())
        except (TypeError, ValueError):
            return 0


class FetchError(Exception):
    def __init__(self, url: str, reason: str, status: int | None = None):
        self.status = status
        self.url = url
        super().__init__(f"{status or 'network'} {url}: {reason}")


class Fetcher:
    def __init__(self, fixture: Path | None = None):
        self.fixture = json.loads(fixture.read_text(encoding="utf-8")) if fixture else None
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(20, read=45),
            trust_env=self.fixture is None,
            headers={"User-Agent": "AI-Assessment/0.1 (research metadata collector)"},
            limits=httpx.Limits(max_connections=12),
        )
        self.host_slots = defaultdict(lambda: asyncio.Semaphore(2))
        self.host_slots["export.arxiv.org"] = asyncio.Semaphore(1)
        self.host_locks = defaultdict(asyncio.Lock)
        self.next_request: dict[str, float] = {}
        self.pause_until: dict[str, float] = {}
        self.interval = {"export.arxiv.org": 3.1, "api.github.com": 0.5}

    def now(self) -> datetime:
        if self.fixture and self.fixture.get("clock"):
            return datetime.fromisoformat(self.fixture["clock"]).astimezone(UTC)
        return datetime.now(UTC)

    async def close(self):
        await self.client.aclose()

    async def _pace(self, host: str):
        loop = asyncio.get_running_loop()
        async with self.host_locks[host]:
            while delay := max(self.next_request.get(host, 0), self.pause_until.get(host, 0)) - loop.time():
                if delay <= 0:
                    break
                if delay > 12:
                    raise FetchError(host, "source cooldown exceeds run retry budget", 429)
                await asyncio.sleep(delay)
            self.next_request[host] = loop.time() + self.interval.get(host, 0.2)

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None, max_bytes: int = 10_000_000,
        allow_archive_redirect: bool = False, accept_redirect: bool = False,
    ) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        if self.fixture is not None:
            item = self.fixture.get("responses", {}).get(url)
            if item is None:
                raise FetchError(url, "missing fixture")
            args = {"json": item["json"]} if "json" in item else {"content": item.get("body", "")}
            response = httpx.Response(item.get("status", 200), request=httpx.Request("GET", url), headers=item.get("headers"), **args)
            if not response.is_success and not (accept_redirect and response.is_redirect):
                raise FetchError(url, "replayed response", response.status_code)
            return response
        async with self.host_slots[host]:
            for attempt in range(3):
                await self._pace(host)
                try:
                    async with self.client.stream(
                        "GET", url, headers=headers, follow_redirects=allow_archive_redirect,
                    ) as response:
                        status = response.status_code
                        if accept_redirect and response.is_redirect:
                            return httpx.Response(status, request=response.request, headers={"Location": response.headers.get("Location", "")})
                        if response.is_success:
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > max_bytes:
                                    raise FetchError(url, f"response exceeded {max_bytes} bytes")
                            decoded_headers = httpx.Headers(response.headers)
                            for field in ("Content-Encoding", "Content-Length", "Transfer-Encoding"):
                                decoded_headers.pop(field, None)
                            return httpx.Response(status, request=response.request, headers=decoded_headers, content=bytes(body))
                        throttled = status == 429 or (
                            status == 403 and (response.headers.get("x-ratelimit-remaining") == "0"
                                               or "retry-after" in response.headers)
                        )
                        delay = retry_delay(response.headers.get("retry-after"), self.now())
                        if throttled:
                            if host == "api.github.com" and not delay:
                                delay = 60
                            if response.headers.get("x-ratelimit-remaining") == "0":
                                reset = response.headers.get("x-ratelimit-reset", "")
                                if reset.isdecimal():
                                    delay = max(delay, float(reset) - self.now().timestamp())
                            delay = max(delay, 2 ** attempt)
                            self.pause_until[host] = max(
                                self.pause_until.get(host, 0), asyncio.get_running_loop().time() + delay,
                            )
                        if attempt == 2 or (not throttled and status < 500) or delay > 10:
                            raise FetchError(url, "rate limited" if throttled else "request rejected", 429 if throttled else status)
                except httpx.RequestError as exc:
                    if attempt == 2:
                        raise FetchError(url, type(exc).__name__) from exc
                await asyncio.sleep(0.4 * 2 ** attempt)
        raise AssertionError("request attempts exhausted")
