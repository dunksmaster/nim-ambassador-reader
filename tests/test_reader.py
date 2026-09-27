"""Pipeline tests with a mocked fetch and a mocked NIM call. No API key required."""

import json

import httpx
import pytest

from ambassador_reader.core import (
    DEFAULT_MODEL,
    FALLBACK_MODEL,
    AmbassadorReaderError,
    FetchedPage,
    MissingApiKeyError,
    ModelOutputError,
    html_to_text,
    read_ambassador_page,
)

@pytest.fixture(autouse=True)
def _clear_model_override(monkeypatch):
    monkeypatch.delenv("NVIDIA_MODEL", raising=False)


PAGE_URL = "https://example.edu/programs/ambassador"
HTML = """
<html>
  <head>
    <title>Example Campus Ambassador</title>
    <style>.nav { display: none; }</style>
  </head>
  <body>
    <main>
      <h1>Example Campus Ambassador</h1>
      <p>Run by Example University. Open to students in Canada who are 18 or older.</p>
      <p>Includes a $500 stipend.</p>
      <a href="/apply">Apply</a>
    </main>
    <script>var secret = "should-not-appear";</script>
  </body>
</html>
"""

NULL_OBJECT = {
    "program_name": None,
    "company": None,
    "deadline": None,
    "eligibility": None,
    "benefits": None,
    "apply_url": None,
}


def _json_reply(**overrides) -> str:
    payload = dict(NULL_OBJECT)
    payload.update(overrides)
    return json.dumps(payload)


def _page(text: str | None = None, source_url: str = PAGE_URL) -> FetchedPage:
    body = html_to_text(HTML) if text is None else text
    return FetchedPage(text=body, source_url=source_url)


def test_documented_model_ids():
    assert DEFAULT_MODEL == "nvidia/nemotron-3-super-120b-a12b"
    assert FALLBACK_MODEL == "mistralai/mistral-nemotron"


def test_html_to_text_drops_script_and_keeps_href():
    text = html_to_text(HTML)
    assert "Example Campus Ambassador" in text
    assert "(/apply)" in text
    assert "should-not-appear" not in text
    assert "display: none" not in text


def test_fenced_reply_nulls_and_relative_link(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    seen = {}

    def chat(model, messages):
        seen["model"] = model
        seen["messages"] = messages
        body = _json_reply(
            program_name="Example Campus Ambassador",
            company="Example University",
            eligibility="Students in Canada who are 18 or older",
            benefits="A $500 stipend",
            apply_url="/apply",
        )
        return f"```json\n{body}\n```"

    record = read_ambassador_page(PAGE_URL, chat_complete=chat, fetch=lambda url: _page())

    assert seen["model"] == DEFAULT_MODEL
    assert "should-not-appear" not in seen["messages"][1]["content"]
    assert record == {
        "program_name": "Example Campus Ambassador",
        "company": "Example University",
        "deadline": None,
        "eligibility": "Students in Canada who are 18 or older",
        "benefits": "A $500 stipend",
        "apply_url": "https://example.edu/apply",
        "source_url": PAGE_URL,
        "model": DEFAULT_MODEL,
    }


def test_unstated_fields_stay_null(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)

    def chat(model, messages):
        return "The page does not list a deadline.\n" + _json_reply(
            program_name="Advocate Program"
        )

    record = read_ambassador_page(PAGE_URL, chat_complete=chat, fetch=lambda url: _page())
    assert record["program_name"] == "Advocate Program"
    assert record["company"] is None
    assert record["deadline"] is None
    assert record["eligibility"] is None
    assert record["benefits"] is None
    assert record["apply_url"] is None
    assert record["model"] == DEFAULT_MODEL


def test_retries_once_then_accepts_valid_json(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    calls = {"n": 0}

    def chat(model, messages):
        calls["n"] += 1
        if calls["n"] == 1:
            return "Sorry, I cannot format that yet."
        return _json_reply(program_name="Second try")

    record = read_ambassador_page(PAGE_URL, chat_complete=chat, fetch=lambda url: _page())
    assert calls["n"] == 2
    assert record["program_name"] == "Second try"


def test_fails_clearly_after_second_bad_reply(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)

    def chat(model, messages):
        return "still not json"

    with pytest.raises(ModelOutputError, match="after 2 attempts"):
        read_ambassador_page(
            PAGE_URL,
            model="custom/model",
            chat_complete=chat,
            fetch=lambda url: _page(),
        )


def test_fallback_model_on_default_api_error(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    calls = []

    def chat(model, messages):
        calls.append(model)
        if model == DEFAULT_MODEL:
            raise RuntimeError("model unavailable")
        return _json_reply(program_name="From fallback", apply_url="https://apply.example.edu/form")

    record = read_ambassador_page(PAGE_URL, chat_complete=chat, fetch=lambda url: _page())
    assert calls == [DEFAULT_MODEL, FALLBACK_MODEL]
    assert record["model"] == FALLBACK_MODEL
    assert record["program_name"] == "From fallback"
    assert record["apply_url"] == "https://apply.example.edu/form"


def test_explicit_model_does_not_fall_back(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setenv("NVIDIA_MODEL", DEFAULT_MODEL)
    calls = []

    def chat(model, messages):
        calls.append(model)
        raise RuntimeError("nope")

    with pytest.raises(AmbassadorReaderError, match="custom/model"):
        read_ambassador_page(
            PAGE_URL,
            model="custom/model",
            chat_complete=chat,
            fetch=lambda url: _page(),
        )
    assert calls == ["custom/model"]


def test_env_model_override(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setenv("NVIDIA_MODEL", "mistralai/mistral-nemotron")

    def chat(model, messages):
        assert model == "mistralai/mistral-nemotron"
        return "```\n" + _json_reply(company="Env Co") + "\n```"

    record = read_ambassador_page(PAGE_URL, chat_complete=chat, fetch=lambda url: _page())
    assert record["company"] == "Env Co"
    assert record["model"] == "mistralai/mistral-nemotron"


def test_empty_page_does_not_call_the_model(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)

    def chat(model, messages):
        raise AssertionError("model should not be called")

    record = read_ambassador_page(
        PAGE_URL,
        chat_complete=chat,
        fetch=lambda url: FetchedPage(text="  \n", source_url=url),
    )
    assert record["program_name"] is None
    assert record["apply_url"] is None
    assert record["source_url"] == PAGE_URL
    assert record["model"] is None


def test_missing_key_before_any_fetch(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)

    def fetch(url):
        raise AssertionError("fetch should not run without a key")

    with pytest.raises(MissingApiKeyError, match="NVIDIA_API_KEY"):
        read_ambassador_page(PAGE_URL, fetch=fetch)


def test_rejects_non_http_url(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(AmbassadorReaderError, match="http"):
        read_ambassador_page("file:///tmp/page.html", chat_complete=lambda m, msgs: "")


def test_http_fetch_and_nim_client_are_mocked(monkeypatch):
    """Exercise the real fetch and NIM wrappers with fakes. No network, no real key."""
    secret = "nvapi-test-secret-value"
    monkeypatch.setenv("NVIDIA_API_KEY", secret)

    class FakeResponse:
        status_code = 200
        text = HTML
        url = httpx.URL(PAGE_URL)

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            assert url == PAGE_URL
            return FakeResponse()

    class FakeMessage:
        content = "```json\n" + _json_reply(program_name="Wired", apply_url="apply") + "\n```"

    class FakeChoice:
        message = FakeMessage()

    class FakeCompletion:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs):
            assert kwargs["model"] == DEFAULT_MODEL
            blob = json.dumps(kwargs["messages"])
            assert secret not in blob
            assert "Example Campus Ambassador" in blob
            return FakeCompletion()

    class FakeChat:
        completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, *, base_url, api_key):
            assert base_url == "https://integrate.api.nvidia.com/v1"
            assert api_key == secret
            self.chat = FakeChat()

    monkeypatch.setattr("ambassador_reader.core.httpx.Client", FakeClient)
    monkeypatch.setattr("ambassador_reader.core.OpenAI", FakeOpenAI)

    record = read_ambassador_page(PAGE_URL)
    assert secret not in json.dumps(record)
    assert record["program_name"] == "Wired"
    assert record["apply_url"] == "https://example.edu/programs/apply"
    assert record["source_url"] == PAGE_URL
    assert record["model"] == DEFAULT_MODEL


def test_nim_error_redacts_key(monkeypatch):
    secret = "nvapi-test-secret-value"
    monkeypatch.setenv("NVIDIA_API_KEY", secret)

    class FakeCompletions:
        def create(self, **kwargs):
            raise RuntimeError(f"rejected key {secret}")

    class FakeChat:
        completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, *, base_url, api_key):
            self.chat = FakeChat()

    monkeypatch.setattr("ambassador_reader.core.OpenAI", FakeOpenAI)

    with pytest.raises(AmbassadorReaderError) as caught:
        read_ambassador_page(PAGE_URL, fetch=lambda url: _page())
    message = str(caught.value)
    assert secret not in message
    assert "[redacted]" in message
