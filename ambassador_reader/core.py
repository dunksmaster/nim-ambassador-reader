"""Fetch a program page and extract a validated JSON record with NVIDIA NIM.

The NIM call uses the OpenAI-compatible endpoint at
https://integrate.api.nvidia.com/v1. The API key is read from the
NVIDIA_API_KEY environment variable and is never included in errors or output.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"
FALLBACK_MODEL = "mistralai/mistral-nemotron"
# Bound a single NIM call. The SDK default is a 600s timeout and 2 retries.
NIM_TIMEOUT_SECONDS = 90.0
NIM_MAX_RETRIES = 1
TEXT_LIMIT = 20_000
HTML_LIMIT = 1_000_000
SHORT_TEXT_CHARS = 200

# Injected in tests. Signature: (model, messages) -> response text.
ChatComplete = Callable[[str, list[dict[str, str]]], str]

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_THINK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_FIELD_NAMES = (
    "program_name",
    "company",
    "deadline",
    "eligibility",
    "benefits",
    "apply_url",
)

SYSTEM_PROMPT = """You extract facts from one web page about an ambassador, campus ambassador, or developer-champion program.

Reply with a single JSON object and no other text. Use exactly these keys:
{"program_name": null, "company": null, "deadline": null, "eligibility": null, "benefits": null, "apply_url": null}

Rules:
- Copy only facts that appear in the page text. If a fact is not stated, the value is null. Do not guess, infer, or use outside knowledge.
- program_name: the program's name.
- company: the organization that runs the program.
- deadline: the application deadline as stated (a date, or a phrase such as "rolling" only if the page says so).
- eligibility: who may apply. Include geographic, student, and age requirements when the page states them.
- benefits: what a participant receives. Include a stipend or other payment when the page states one.
- apply_url: the application URL as written on the page. Keep relative links relative. Use null if the page has no application link.
- Each value is a string or null. If several stated facts belong in one field, join them into one string.
"""


class AmbassadorReaderError(Exception):
    """Failure the CLI can print directly."""


class MissingApiKeyError(AmbassadorReaderError):
    """NVIDIA_API_KEY is unset."""


class PageFetchError(AmbassadorReaderError):
    """The program page could not be fetched."""


class ModelCallError(AmbassadorReaderError):
    """The NIM request failed."""


class ModelOutputError(AmbassadorReaderError):
    """The model reply was not valid program JSON after a retry."""


class ExtractionError(Exception):
    """The reply text did not contain a JSON object."""


class ProgramExtraction(BaseModel):
    """Facts taken from the model. Missing keys are invalid, not silently filled."""

    model_config = ConfigDict(extra="ignore")

    program_name: str | None
    company: str | None
    deadline: str | None
    eligibility: str | None
    benefits: str | None
    apply_url: str | None

    @field_validator(*_FIELD_NAMES, mode="before")
    @classmethod
    def coerce_field(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if item is None:
                    continue
                text = str(item).strip()
                if text:
                    parts.append(text)
            return "; ".join(parts) or None
        if isinstance(value, bool):
            raise ValueError("expected a string, a list of strings, or null")
        if isinstance(value, (int, float)):
            return str(value)
        raise ValueError("expected a string, a list of strings, or null")


class AmbassadorRecord(BaseModel):
    """Public JSON record. source_url and model are set by this tool."""

    model_config = ConfigDict(protected_namespaces=())

    program_name: str | None = None
    company: str | None = None
    deadline: str | None = None
    eligibility: str | None = None
    benefits: str | None = None
    apply_url: str | None = None
    source_url: str
    model: str | None = Field(description="NIM model id that produced the extraction")


@dataclass(frozen=True)
class FetchedPage:
    text: str
    source_url: str


def require_api_key() -> str:
    key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not key:
        raise MissingApiKeyError(
            "NVIDIA_API_KEY is not set. Get a key at https://build.nvidia.com "
            "and export NVIDIA_API_KEY (or put it in a local .env file) before "
            "running ambassador-reader. The key is read only from the environment "
            "and is never printed."
        )
    return key


def select_model(explicit: str | None) -> tuple[str, bool]:
    """Return (model id, whether the default-model fallback is allowed)."""
    if explicit and explicit.strip():
        return explicit.strip(), False
    env_model = os.environ.get("NVIDIA_MODEL", "").strip()
    if env_model:
        return env_model, False
    return DEFAULT_MODEL, True


def html_to_text(html: str, *, limit: int = TEXT_LIMIT) -> str:
    """Turn HTML into readable text, keeping anchor hrefs so apply links survive."""
    soup = BeautifulSoup(html[:HTML_LIMIT], "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if href and href not in {"#"}:
            anchor.append(f" ({href})")
    lines = [line.strip() for line in soup.get_text("\n").splitlines()]
    text = "\n".join(line for line in lines if line)
    if len(text) > limit:
        return text[:limit] + "\n[truncated]"
    return text


def fetch_page(url: str) -> FetchedPage:
    timeout = httpx.Timeout(30.0, connect=10.0)
    headers = {
        "User-Agent": "ambassador-reader/0.1",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            response = client.get(url)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise PageFetchError(
            f"Could not fetch {url}: HTTP {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        raise PageFetchError(f"Could not fetch {url}: {exc.__class__.__name__}") from exc
    return FetchedPage(text=html_to_text(response.text), source_url=str(response.url))


def _path_for_compare(path: str) -> str:
    if path in ("", "/"):
        return "/"
    return path


def fetched_url_differs(requested_url: str, fetched_url: str) -> bool:
    """True when the final URL's host or path is not the one that was requested.

    Scheme, query, and fragment changes do not count. An empty path and ``/`` are the same path.
    """
    requested = urlparse(requested_url)
    fetched = urlparse(fetched_url)
    requested_host = (requested.hostname or "").lower()
    fetched_host = (fetched.hostname or "").lower()
    if requested_host != fetched_host:
        return True
    return _path_for_compare(requested.path) != _path_for_compare(fetched.path)


def fetch_warning_messages(requested_url: str, page: FetchedPage) -> list[str]:
    """Redirect and short-text warnings. The CLI prints these; they are not record fields."""
    messages: list[str] = []
    if fetched_url_differs(requested_url, page.source_url):
        messages.append(f"warning: redirected from {requested_url} to {page.source_url}")
    text_length = len(page.text.strip())
    if text_length < SHORT_TEXT_CHARS:
        messages.append(
            "warning: extracted page text is very short "
            f"({text_length} characters); the result is likely thin or about the wrong page"
        )
    return messages


def warn_about_fetch(requested_url: str, page: FetchedPage) -> None:
    """Print fetch problems to stderr. They are not part of the JSON record."""
    for message in fetch_warning_messages(requested_url, page):
        print(message, file=sys.stderr)


def resolve_apply_url(apply_url: str | None, source_url: str) -> str | None:
    """Resolve a relative application link against the page URL.

    Values that are not http(s) or mailto links become null.
    """
    if apply_url is None:
        return None
    candidate = apply_url.strip()
    if not candidate or any(character.isspace() for character in candidate):
        return None
    resolved = urljoin(source_url, candidate)
    parsed = urlparse(resolved)
    if parsed.scheme == "mailto" and parsed.path:
        return resolved
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return resolved
    return None


def _balanced_objects(text: str) -> list[str]:
    objects: list[str] = []
    start: int | None = None
    depth = 0
    in_string = False
    escape = False
    for index, character in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif character == "\\":
                escape = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(text[start : index + 1])
                start = None
    return objects


def json_object_candidates(text: str) -> list[str]:
    """Yield likely JSON objects from a reply that may include prose or fences."""
    cleaned = _THINK_RE.sub("", text or "").lstrip("\ufeff").strip()
    seen: list[str] = []

    def add(chunk: str) -> None:
        item = chunk.strip()
        if item and item not in seen:
            seen.append(item)

    if cleaned:
        add(cleaned)
    for match in _FENCE_RE.finditer(cleaned):
        add(match.group(1))
    for obj in _balanced_objects(cleaned):
        add(obj)
    return seen


def parse_program_json(text: str) -> ProgramExtraction:
    """Parse and validate a model reply. Raises if nothing matches the schema."""
    if text is None or not str(text).strip():
        raise ExtractionError("The model returned an empty response.")

    errors: list[str] = []
    found_object = False
    for candidate in json_object_candidates(str(text)):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError as exc:
            errors.append(str(exc))
            continue
        if not isinstance(value, dict):
            errors.append("JSON value was not an object.")
            continue
        found_object = True
        try:
            return ProgramExtraction.model_validate(value)
        except ValidationError as exc:
            errors.append(str(exc).splitlines()[0])
    if not found_object:
        detail = errors[-1] if errors else "no JSON object found"
        raise ExtractionError(f"No JSON object found in the model response ({detail}).")
    raise ExtractionError(
        "JSON object did not match the program schema. " + (errors[-1] if errors else "")
    )


def _preview(text: str, limit: int = 240) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def _redact(message: str, secret: str) -> str:
    if secret and secret in message:
        return message.replace(secret, "[redacted]")
    return message


def _message_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
            else:
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content)


def nim_chat(
    model: str,
    messages: list[dict[str, str]],
    *,
    timeout: float = NIM_TIMEOUT_SECONDS,
    max_retries: int = NIM_MAX_RETRIES,
) -> str:
    """Call the hosted NIM chat completions API. Does not log the API key.

    The CLI uses the default timeout. The web demo passes a shorter one so the
    request can finish inside the platform limit. The API key is still read
    only from the environment.
    """
    key = require_api_key()
    client = OpenAI(
        base_url=NIM_BASE_URL,
        api_key=key,
        timeout=timeout,
        max_retries=max_retries,
    )
    try:
        completion = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.2,
            max_tokens=2048,
        )
    except Exception as exc:
        raise ModelCallError(
            f"NIM request failed for model {model}: {_redact(str(exc), key)}"
        ) from exc
    try:
        content = completion.choices[0].message.content
    except (AttributeError, IndexError) as exc:
        raise ModelCallError(f"NIM response for model {model} did not include a message.") from exc
    return _message_text(content)


def build_messages(source_url: str, page_text: str) -> list[dict[str, str]]:
    user = f"Page URL: {source_url}\n\nPage text:\n{page_text}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _check_http_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AmbassadorReaderError("URL must be an http or https address.")
    return url.strip()


def extract_with_retry(
    chat: ChatComplete,
    model: str,
    messages: list[dict[str, str]],
) -> ProgramExtraction:
    """Call the model and, if the JSON is unusable, call it once more."""
    last_error: Exception | None = None
    last_text = ""
    for attempt in (1, 2):
        try:
            text = chat(model, messages)
        except AmbassadorReaderError:
            raise
        except Exception as exc:
            secret = os.environ.get("NVIDIA_API_KEY", "")
            raise ModelCallError(
                f"NIM request failed for model {model}: {_redact(str(exc), secret)}"
            ) from exc
        last_text = text if isinstance(text, str) else _message_text(text)
        try:
            return parse_program_json(last_text)
        except (ExtractionError, ValidationError) as exc:
            last_error = exc
            if attempt == 1:
                continue
    raise ModelOutputError(
        f"{model} did not return valid program JSON after 2 attempts ({last_error}). "
        f"Last response started with: {_preview(last_text)!r}"
    )


def _call_models(
    chat: ChatComplete,
    model: str,
    messages: list[dict[str, str]],
    *,
    allow_fallback: bool,
) -> tuple[ProgramExtraction, str]:
    try:
        return extract_with_retry(chat, model, messages), model
    except (ModelCallError, ModelOutputError) as first_error:
        if not allow_fallback or model == FALLBACK_MODEL:
            raise
        try:
            return extract_with_retry(chat, FALLBACK_MODEL, messages), FALLBACK_MODEL
        except (ModelCallError, ModelOutputError) as second_error:
            raise AmbassadorReaderError(
                f"Default model {model} failed ({first_error}). "
                f"Fallback model {FALLBACK_MODEL} also failed ({second_error})."
            ) from second_error


def _record(extracted: ProgramExtraction, source_url: str, model: str | None) -> dict:
    apply_url = resolve_apply_url(extracted.apply_url, source_url)
    record = AmbassadorRecord(
        program_name=extracted.program_name,
        company=extracted.company,
        deadline=extracted.deadline,
        eligibility=extracted.eligibility,
        benefits=extracted.benefits,
        apply_url=apply_url,
        source_url=source_url,
        model=model,
    )
    return record.model_dump(mode="json")


def read_ambassador_page(
    url: str,
    *,
    model: str | None = None,
    chat_complete: ChatComplete | None = None,
    fetch: Callable[[str], FetchedPage] | None = None,
) -> dict:
    """Fetch ``url`` and return the validated program record as a dict.

    ``chat_complete`` and ``fetch`` exist so tests can run without a key or network.
    When ``chat_complete`` is omitted, NVIDIA_API_KEY must be set.
    """
    requested = _check_http_url(url)
    chosen_model, allow_fallback = select_model(model)
    if chat_complete is None:
        require_api_key()
        chat_complete = nim_chat

    page = (fetch or fetch_page)(requested)
    warn_about_fetch(requested, page)
    if not page.text.strip():
        empty = ProgramExtraction(
            program_name=None,
            company=None,
            deadline=None,
            eligibility=None,
            benefits=None,
            apply_url=None,
        )
        return _record(empty, page.source_url, None)

    messages = build_messages(page.source_url, page.text)
    extracted, used_model = _call_models(
        chat_complete,
        chosen_model,
        messages,
        allow_fallback=allow_fallback,
    )
    return _record(extracted, page.source_url, used_model)
