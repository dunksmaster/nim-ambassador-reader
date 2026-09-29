"""Offline tests for the web demo. No network and no API key."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from ambassador_reader.core import (
    NIM_TIMEOUT_SECONDS,
    FetchedPage,
    PageFetchError,
    fetch_warning_messages,
)
from ambassador_reader.web import (
    MAX_PAGE_BYTES,
    WEB_FETCH_TIMEOUT_SECONDS,
    WEB_NIM_MAX_RETRIES,
    WEB_NIM_TIMEOUT_SECONDS,
    PageTooLargeError,
    SlidingWindowRateLimiter,
    UnsafeUrlError,
    WebFetchTimeout,
    assert_public_http_url,
    client_key_from_headers,
    extract_for_web,
    fetch_public_page,
    web_nim_chat,
)

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_IP = "93.184.216.34"

NULL_OBJECT = {
    "program_name": "Example Campus Ambassador",
    "company": None,
    "deadline": None,
    "eligibility": None,
    "benefits": None,
    "apply_url": None,
}


def _public_resolve(host: str, port: int) -> list[str]:
    return [PUBLIC_IP]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:9/latest",
        "https://10.1.2.3/secret",
        "http://10.255.0.1/",
        "http://172.16.0.5/",
        "http://172.16.255.10/admin",
        "http://192.168.0.1/",
        "https://192.168.1.20/router",
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.169.254/",
        "http://[::1]/",
        "http://[::1]:8080/",
        "http://localhost/",
        "http://localhost:8080/page",
        "http://LOCALHOST/admin",
        "file:///etc/passwd",
        "file:///tmp/page.html",
        "ftp://example.com/readme",
        "ftp://127.0.0.1/file",
    ],
)
def test_blocked_urls_do_not_use_dns(url):
    def resolve(host, port):
        raise AssertionError(f"DNS should not run for {url}")

    with pytest.raises(UnsafeUrlError):
        assert_public_http_url(url, resolve=resolve)


def test_file_and_ftp_schemes_are_rejected_as_non_http():
    for url in ("file:///etc/passwd", "ftp://example.com/readme"):
        with pytest.raises(UnsafeUrlError, match="http"):
            assert_public_http_url(url, resolve=_public_resolve)


def test_internal_literals_are_blocked():
    for url in (
        "http://127.0.0.1/",
        "http://10.0.0.8/",
        "http://172.16.5.5/",
        "http://192.168.50.2/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://localhost/",
    ):
        with pytest.raises(UnsafeUrlError, match="blocked"):
            assert_public_http_url(url, resolve=_public_resolve)


def test_public_ip_literal_is_allowed_without_dns():
    def resolve(host, port):
        raise AssertionError("DNS should not run for an IP literal")

    assert assert_public_http_url("https://8.8.8.8/path", resolve=resolve) == "https://8.8.8.8/path"


def test_public_hostname_is_allowed_after_dns():
    seen = []

    def resolve(host, port):
        seen.append((host, port))
        return [PUBLIC_IP]

    assert assert_public_http_url("https://example.com/programs", resolve=resolve) == (
        "https://example.com/programs"
    )
    assert seen == [("example.com", 443)]


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.9.8.7"],
        ["172.16.0.4"],
        ["192.168.2.9"],
        ["169.254.169.254"],
        ["::1"],
        [PUBLIC_IP, "10.0.0.2"],
    ],
)
def test_dns_to_internal_address_is_blocked(addresses):
    def resolve(host, port):
        assert host == "example.com"
        return addresses

    with pytest.raises(UnsafeUrlError, match="blocked"):
        assert_public_http_url("https://example.com/", resolve=resolve)


def test_redirect_to_metadata_is_blocked_before_the_second_request():
    seen = []

    def mock(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "example.com":
            return httpx.Response(
                302,
                headers={"Location": "http://169.254.169.254/latest/meta-data/"},
            )
        return httpx.Response(200, text="secret")

    with pytest.raises(UnsafeUrlError, match="blocked"):
        fetch_public_page(
            "https://example.com/start",
            resolve=_public_resolve,
            transport=httpx.MockTransport(mock),
        )
    assert seen == ["example.com"]


def test_redirect_to_loopback_and_private_ranges_is_blocked():
    for location in (
        "http://127.0.0.1/",
        "http://10.1.1.1/",
        "http://172.16.8.8/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://localhost/admin",
    ):
        seen = []

        def mock(request: httpx.Request, location=location) -> httpx.Response:
            seen.append(str(request.url))
            if request.url.host == "example.com":
                return httpx.Response(302, headers={"Location": location})
            return httpx.Response(200, text="secret")

        with pytest.raises(UnsafeUrlError, match="blocked"):
            fetch_public_page(
                "https://example.com/start",
                resolve=_public_resolve,
                transport=httpx.MockTransport(mock),
            )
        assert seen == ["https://example.com/start"]


def test_public_redirect_is_rechecked_and_followed():
    def mock(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"Location": "https://example.org/landed"})
        return httpx.Response(
            200,
            text="<html><body><p>Example Campus Ambassador program.</p></body></html>",
        )

    def resolve(host, port):
        return {"example.com": [PUBLIC_IP], "example.org": ["93.184.216.35"]}[host]

    page = fetch_public_page(
        "https://example.com/start",
        resolve=resolve,
        transport=httpx.MockTransport(mock),
    )
    assert page.source_url == "https://example.org/landed"
    assert "Example Campus Ambassador" in page.text


def test_declared_page_size_over_2mb_is_rejected():
    blob = b"a" * (MAX_PAGE_BYTES + 1)

    def mock(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=blob)

    with pytest.raises(PageTooLargeError, match="2 MB"):
        fetch_public_page(
            "https://example.com/",
            resolve=_public_resolve,
            transport=httpx.MockTransport(mock),
        )


def test_streamed_page_over_2mb_is_rejected():
    blob = b"b" * (MAX_PAGE_BYTES + 8)

    def mock(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, content=blob)
        del response.headers["content-length"]
        return response

    with pytest.raises(PageTooLargeError, match="2 MB"):
        fetch_public_page(
            "https://example.com/",
            resolve=_public_resolve,
            transport=httpx.MockTransport(mock),
        )


def test_fetch_timeout_is_a_web_timeout():
    def mock(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(WebFetchTimeout, match="timed out"):
        fetch_public_page(
            "https://example.com/",
            resolve=_public_resolve,
            transport=httpx.MockTransport(mock),
        )


def test_rate_limiter_allows_five_then_blocks_until_the_window_moves():
    limiter = SlidingWindowRateLimiter(limit=5, window_seconds=60)
    for second in range(5):
        assert limiter.allow("203.0.113.5", now=float(second))
    assert limiter.allow("203.0.113.5", now=5) is False
    assert limiter.allow("203.0.113.9", now=5) is True
    assert limiter.allow("203.0.113.5", now=60) is True


def test_extract_rate_limit_does_not_fetch(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    limiter = SlidingWindowRateLimiter(limit=5, window_seconds=60)
    calls = []

    def fetch(url):
        calls.append(url)
        raise AssertionError("fetch should not run")

    for _ in range(5):
        status, body = extract_for_web(
            "http://127.0.0.1/",
            client_key="198.51.100.4",
            limiter=limiter,
            now=1,
            fetch=fetch,
        )
        assert status == 400
        assert "blocked" in body["error"]
    status, body = extract_for_web(
        "http://127.0.0.1/",
        client_key="198.51.100.4",
        limiter=limiter,
        now=1,
        fetch=fetch,
    )
    assert status == 429
    assert "Too many requests" in body["error"]
    assert calls == []


def test_missing_key_is_500_and_does_not_fetch(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)

    def fetch(url):
        raise AssertionError("fetch should not run without a key")

    status, body = extract_for_web(
        "https://example.com/program",
        client_key="198.51.100.8",
        limiter=SlidingWindowRateLimiter(),
        now=0,
        resolve=_public_resolve,
        fetch=fetch,
    )
    assert status == 500
    assert "NVIDIA_API_KEY" in body["error"]
    text = json.dumps(body)
    assert "nvapi" not in text


def test_warnings_are_added_for_the_web_response_only(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "unit-test-key-not-a-real-secret")
    final = "https://example.org/other"
    page = FetchedPage(text="Hi", source_url=final)

    def fetch(url):
        return page

    def chat(model, messages):
        return json.dumps(NULL_OBJECT)

    status, body = extract_for_web(
        "https://example.com/start",
        client_key="198.51.100.10",
        limiter=SlidingWindowRateLimiter(),
        now=0,
        resolve=_public_resolve,
        fetch=fetch,
        chat_complete=chat,
    )
    assert status == 200
    assert body["warnings"] == fetch_warning_messages("https://example.com/start", page)
    assert any("redirected from https://example.com/start" in item for item in body["warnings"])
    assert any("very short" in item for item in body["warnings"])
    assert "unit-test-key-not-a-real-secret" not in json.dumps(body)
    assert body["program_name"] == "Example Campus Ambassador"


def test_quiet_page_has_an_empty_warnings_list(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "unit-test-key-not-a-real-secret")
    text = "Example Campus Ambassador\n" + ("Open to students. " * 20)

    status, body = extract_for_web(
        "https://example.com/start",
        client_key="198.51.100.11",
        limiter=SlidingWindowRateLimiter(),
        now=0,
        resolve=_public_resolve,
        fetch=lambda url: FetchedPage(text=text, source_url=url),
        chat_complete=lambda model, messages: json.dumps(NULL_OBJECT),
    )
    assert status == 200
    assert body["warnings"] == []


def test_upstream_fetch_error_is_502_and_timeout_is_504(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "unit-test-key-not-a-real-secret")
    limiter = SlidingWindowRateLimiter()

    def fail_fetch(url):
        raise PageFetchError("Could not fetch the page: HTTP 404.")

    def time_out(url):
        raise WebFetchTimeout("The page fetch timed out.")

    status, body = extract_for_web(
        "https://example.com/missing",
        client_key="198.51.100.12",
        limiter=limiter,
        now=0,
        resolve=_public_resolve,
        fetch=fail_fetch,
    )
    assert status == 502
    assert "HTTP 404" in body["error"]

    status, body = extract_for_web(
        "https://example.com/slow",
        client_key="198.51.100.12",
        limiter=limiter,
        now=1,
        resolve=_public_resolve,
        fetch=time_out,
    )
    assert status == 504
    assert "timed out" in body["error"]


def test_model_timeout_is_504_and_key_is_redacted(monkeypatch):
    secret = "nvapi-web-unit-test-secret"
    monkeypatch.setenv("NVIDIA_API_KEY", secret)

    class FakeCompletions:
        def create(self, **kwargs):
            raise TimeoutError(f"request timed out for {secret}")

    class FakeChat:
        completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, *, base_url, api_key, timeout, max_retries):
            assert api_key == secret
            assert timeout == WEB_NIM_TIMEOUT_SECONDS
            assert max_retries == 0
            assert timeout < NIM_TIMEOUT_SECONDS
            self.chat = FakeChat()

    monkeypatch.setattr("ambassador_reader.core.OpenAI", FakeOpenAI)
    status, body = extract_for_web(
        "https://example.com/program",
        client_key="198.51.100.13",
        limiter=SlidingWindowRateLimiter(),
        now=0,
        resolve=_public_resolve,
        fetch=lambda url: FetchedPage(text="A long enough page. " * 20, source_url=url),
    )
    assert status == 504
    rendered = json.dumps(body)
    assert secret not in rendered
    assert "[redacted]" in rendered


def test_web_budget_fits_under_vercel_max_duration():
    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    assert config["functions"]["api/extract.py"]["maxDuration"] == 60
    # fetch + JSON retry on the default model + JSON retry on the fallback
    assert WEB_FETCH_TIMEOUT_SECONDS + 4 * WEB_NIM_TIMEOUT_SECONDS < 60
    assert WEB_NIM_MAX_RETRIES == 0
    assert WEB_NIM_TIMEOUT_SECONDS < NIM_TIMEOUT_SECONDS


def test_web_nim_chat_uses_the_shorter_timeout(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "unit-test-key-not-a-real-secret")
    seen = {}

    class FakeMessage:
        content = json.dumps(NULL_OBJECT)

    class FakeChoice:
        message = FakeMessage()

    class FakeCompletion:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs):
            return FakeCompletion()

    class FakeChat:
        completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, *, base_url, api_key, timeout, max_retries):
            seen["timeout"] = timeout
            seen["max_retries"] = max_retries
            self.chat = FakeChat()

    monkeypatch.setattr("ambassador_reader.core.OpenAI", FakeOpenAI)
    text = web_nim_chat("nvidia/nemotron-3-super-120b-a12b", [{"role": "user", "content": "hi"}])
    assert "Example Campus Ambassador" in text
    assert seen == {"timeout": WEB_NIM_TIMEOUT_SECONDS, "max_retries": 0}


def test_client_key_prefers_real_ip_then_forwarded_for():
    assert client_key_from_headers({"X-Real-Ip": "203.0.113.9"}) == "203.0.113.9"
    assert client_key_from_headers({"X-Forwarded-For": "203.0.113.4, 10.0.0.1"}) == "203.0.113.4"
    assert client_key_from_headers({}) == "unknown"


def test_handler_module_exports_a_vercel_handler():
    path = ROOT / "api" / "extract.py"
    spec = importlib.util.spec_from_file_location("extract_handler", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from http.server import BaseHTTPRequestHandler

    assert issubclass(module.handler, BaseHTTPRequestHandler)


def test_page_credits_nim_and_the_repository():
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    assert "https://build.nvidia.com" in html
    assert "nvidia/nemotron-3-super-120b-a12b" in html
    assert "https://github.com/dunksmaster/nim-ambassador-reader" in html
    assert 'id="url"' in html
    assert 'id="submit"' in html
    assert "Reading" in html
    assert "JSON.stringify" in html
