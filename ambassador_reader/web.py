"""Web-demo guards: public URLs only, a short fetch, and a per-instance rate limit.

The CLI is unchanged. This module is what the Vercel function calls.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from urllib.parse import urlparse

import httpx

from ambassador_reader.core import (
    AmbassadorReaderError,
    FetchedPage,
    MissingApiKeyError,
    PageFetchError,
    fetch_warning_messages,
    html_to_text,
    nim_chat,
    read_ambassador_page,
    require_api_key,
)

# Stay under the 60s maxDuration in vercel.json.
# Worst case is one fetch plus two JSON attempts on the default model and two
# on the fallback model, with no extra SDK retries: 8 + 4 * 12 = 56 seconds.
WEB_FETCH_TIMEOUT_SECONDS = 8.0
WEB_NIM_TIMEOUT_SECONDS = 12.0
WEB_NIM_MAX_RETRIES = 0
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
RATE_LIMIT = 5
RATE_WINDOW_SECONDS = 60.0
MAX_URL_LENGTH = 2048

MISSING_KEY_ERROR = (
    "NVIDIA_API_KEY is not set on the server. "
    "Set it in the environment. The key is not included in this response."
)

Resolver = Callable[[str, int], list[str]]

_BLOCKED_HOST_NAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.goog",
}


class UnsafeUrlError(Exception):
    """The URL is not a public http(s) address."""


class PageTooLargeError(Exception):
    """The response exceeded the web download cap."""


class WebFetchTimeout(Exception):
    """The page fetch did not finish within the web timeout."""


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for loopback, private, link-local, metadata, and other non-public addresses."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return is_blocked_ip(ip.ipv4_mapped)
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def _literal_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    # inet_aton accepts decimal, hex, octal, and short dotted forms such as 127.1.
    try:
        packed = socket.inet_aton(host)
    except OSError:
        return []
    return [ipaddress.IPv4Address(packed)]


def _host_is_blocked_name(host: str) -> bool:
    if host in _BLOCKED_HOST_NAMES:
        return True
    return host.endswith(".localhost") or host.endswith(".local")


def _default_resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError("Could not resolve that host.") from exc
    found = [str(info[4][0]) for info in infos if info[4]]
    if not found:
        raise UnsafeUrlError("Could not resolve that host.")
    return found


def assert_public_http_url(url: str, *, resolve: Resolver | None = None) -> str:
    """Return a stripped http(s) URL, or raise UnsafeUrlError.

    IP literals are checked directly. Other names are checked after DNS
    resolution. Pass ``resolve`` in tests so this never opens a socket.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise UnsafeUrlError("A URL is required.")
    if len(candidate) > MAX_URL_LENGTH:
        raise UnsafeUrlError("URL is too long.")

    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeUrlError("Only http and https URLs are accepted.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("URL port is not valid.") from exc

    host = parsed.hostname.lower().rstrip(".")
    if not host or _host_is_blocked_name(host):
        raise UnsafeUrlError(
            "That URL is blocked because it points at a local or internal address."
        )

    literals = _literal_ips(host)
    if literals:
        if any(is_blocked_ip(ip) for ip in literals):
            raise UnsafeUrlError(
                "That URL is blocked because it points at a local or internal address."
            )
        return candidate

    lookup = resolve or _default_resolve
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    try:
        addresses = lookup(host, port)
    except UnsafeUrlError:
        raise
    except OSError as exc:
        raise UnsafeUrlError("Could not resolve that host.") from exc
    if not addresses:
        raise UnsafeUrlError("Could not resolve that host.")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise UnsafeUrlError("Could not resolve that host.") from exc
        if is_blocked_ip(ip):
            raise UnsafeUrlError(
                "That URL is blocked because it points at a local or internal address."
            )
    return candidate


class SlidingWindowRateLimiter:
    """In-memory limiter. One process, one counter. Not shared across instances."""

    def __init__(self, limit: int = RATE_LIMIT, window_seconds: float = RATE_WINDOW_SECONDS):
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        identity = key or "unknown"
        with self._lock:
            hits = self._hits.setdefault(identity, deque())
            boundary = moment - self.window_seconds
            while hits and hits[0] <= boundary:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(moment)
            return True


SHARED_LIMITER = SlidingWindowRateLimiter()


def client_key_from_headers(headers: Mapping[str, str]) -> str:
    """Client address from the platform headers. Falls back to one shared bucket."""
    lowered = {str(name).lower(): str(value) for name, value in headers.items()}
    real = lowered.get("x-real-ip", "").strip()
    if real:
        return real.split(",")[0].strip() or "unknown"
    forwarded = lowered.get("x-forwarded-for", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    return "unknown"


def _reject_declared_size(response: httpx.Response) -> None:
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > MAX_PAGE_BYTES:
        raise PageTooLargeError("The page is larger than 2 MB and was not extracted.")


def _read_capped(response: httpx.Response, deadline: float) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        if time.monotonic() > deadline:
            raise WebFetchTimeout("The page fetch timed out.")
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_PAGE_BYTES:
            raise PageTooLargeError("The page is larger than 2 MB and was not extracted.")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_public_page(
    url: str,
    *,
    resolve: Resolver | None = None,
    transport: httpx.BaseTransport | None = None,
) -> FetchedPage:
    """GET a public page. Re-checks the URL, including every redirect, before connecting."""
    assert_public_http_url(url, resolve=resolve)

    def guard(request: httpx.Request) -> None:
        assert_public_http_url(str(request.url), resolve=resolve)

    timeout = httpx.Timeout(
        WEB_FETCH_TIMEOUT_SECONDS,
        connect=min(5.0, WEB_FETCH_TIMEOUT_SECONDS),
    )
    headers = {
        "User-Agent": "ambassador-reader-web/0.1",
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
    }
    deadline = time.monotonic() + WEB_FETCH_TIMEOUT_SECONDS
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            headers=headers,
            event_hooks={"request": [guard]},
            transport=transport,
        ) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                _reject_declared_size(response)
                raw = _read_capped(response, deadline)
                final_url = str(response.url)
                encoding = response.encoding or "utf-8"
    except UnsafeUrlError:
        raise
    except PageTooLargeError:
        raise
    except WebFetchTimeout:
        raise
    except httpx.TimeoutException as exc:
        raise WebFetchTimeout("The page fetch timed out.") from exc
    except httpx.HTTPStatusError as exc:
        raise PageFetchError(
            f"Could not fetch the page: HTTP {exc.response.status_code}."
        ) from exc
    except httpx.HTTPError as exc:
        raise PageFetchError(
            f"Could not fetch the page: {exc.__class__.__name__}."
        ) from exc

    text = raw.decode(encoding, errors="replace")
    return FetchedPage(text=html_to_text(text), source_url=final_url)


def web_nim_chat(model: str, messages: list[dict[str, str]]) -> str:
    """NIM call with the shorter web timeout. The key stays in the environment."""
    return nim_chat(
        model,
        messages,
        timeout=WEB_NIM_TIMEOUT_SECONDS,
        max_retries=WEB_NIM_MAX_RETRIES,
    )


class _RecordingFetch:
    def __init__(self, fetch: Callable[[str], FetchedPage]):
        self._fetch = fetch
        self.requested: str | None = None
        self.page: FetchedPage | None = None

    def __call__(self, url: str) -> FetchedPage:
        self.requested = url
        page = self._fetch(url)
        self.page = page
        return page


def _redact(message: str) -> str:
    secret = os.environ.get("NVIDIA_API_KEY", "").strip()
    if secret and secret in message:
        return message.replace(secret, "[redacted]")
    return message


def _is_timeout(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, httpx.TimeoutException, WebFetchTimeout)):
            return True
        if "Timeout" in type(current).__name__:
            return True
        lowered = str(current).lower()
        if "timed out" in lowered or "timeout" in lowered:
            return True
        current = current.__cause__
    return False


def extract_for_web(
    url: str,
    *,
    client_key: str,
    limiter: SlidingWindowRateLimiter | None = None,
    now: float | None = None,
    resolve: Resolver | None = None,
    fetch: Callable[[str], FetchedPage] | None = None,
    chat_complete: Callable[[str, list[dict[str, str]]], str] | None = None,
) -> tuple[int, dict]:
    """Run one demo extraction. Returns an HTTP status and a JSON-ready object."""
    active_limiter = SHARED_LIMITER if limiter is None else limiter
    if not active_limiter.allow(client_key or "unknown", now=now):
        return (
            429,
            {
                "error": (
                    "Too many requests from this address. "
                    f"The limit is {active_limiter.limit} requests per "
                    f"{int(active_limiter.window_seconds)} seconds on this server instance."
                )
            },
        )

    try:
        safe_url = assert_public_http_url(url, resolve=resolve)
    except UnsafeUrlError as exc:
        return 400, {"error": str(exc)}

    try:
        require_api_key()
    except MissingApiKeyError:
        return 500, {"error": MISSING_KEY_ERROR}

    def default_fetch(target: str) -> FetchedPage:
        return fetch_public_page(target, resolve=resolve)

    recorder = _RecordingFetch(fetch or default_fetch)
    try:
        record = read_ambassador_page(
            safe_url,
            chat_complete=chat_complete or web_nim_chat,
            fetch=recorder,
        )
    except WebFetchTimeout as exc:
        return 504, {"error": _redact(str(exc))}
    except PageTooLargeError as exc:
        return 400, {"error": _redact(str(exc))}
    except UnsafeUrlError as exc:
        return 400, {"error": _redact(str(exc))}
    except PageFetchError as exc:
        return 502, {"error": _redact(str(exc))}
    except MissingApiKeyError:
        return 500, {"error": MISSING_KEY_ERROR}
    except AmbassadorReaderError as exc:
        message = _redact(str(exc))
        if _is_timeout(exc):
            return 504, {"error": message}
        if str(exc).startswith("URL must be"):
            return 400, {"error": message}
        return 502, {"error": message}
    except Exception:
        return 500, {"error": "Unexpected server error."}

    warnings: list[str] = []
    if recorder.requested is not None and recorder.page is not None:
        warnings = fetch_warning_messages(recorder.requested, recorder.page)
    body = dict(record)
    body["warnings"] = warnings
    return 200, body


__all__ = [
    "MAX_PAGE_BYTES",
    "MISSING_KEY_ERROR",
    "RATE_LIMIT",
    "RATE_WINDOW_SECONDS",
    "WEB_FETCH_TIMEOUT_SECONDS",
    "WEB_NIM_MAX_RETRIES",
    "WEB_NIM_TIMEOUT_SECONDS",
    "PageTooLargeError",
    "SlidingWindowRateLimiter",
    "UnsafeUrlError",
    "WebFetchTimeout",
    "assert_public_http_url",
    "client_key_from_headers",
    "extract_for_web",
    "fetch_public_page",
    "is_blocked_ip",
    "web_nim_chat",
]
