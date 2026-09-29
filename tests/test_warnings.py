"""Stderr warnings for redirects and thin pages. No API key and no network."""

import json

from ambassador_reader.cli import main
from ambassador_reader.core import (
    NIM_MAX_RETRIES,
    NIM_TIMEOUT_SECONDS,
    FetchedPage,
    fetched_url_differs,
    read_ambassador_page,
)

PAGE = "https://example.edu/programs/ambassador"
LONG_TEXT = "Example Campus Ambassador\n" + ("Open to students. " * 20)


def _json_reply() -> str:
    return json.dumps(
        {
            "program_name": "Example Campus Ambassador",
            "company": None,
            "deadline": None,
            "eligibility": None,
            "benefits": None,
            "apply_url": None,
        }
    )


def _read(monkeypatch, fetch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_MODEL", raising=False)

    def chat(model, messages):
        return _json_reply()

    return read_ambassador_page(PAGE, chat_complete=chat, fetch=fetch)


def test_timeout_constants():
    assert NIM_TIMEOUT_SECONDS == 90.0
    assert NIM_MAX_RETRIES == 1


def test_same_page_and_long_text_are_quiet(monkeypatch, capsys):
    record = _read(monkeypatch, lambda url: FetchedPage(text=LONG_TEXT, source_url=url))
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "warning" not in json.dumps(record)
    assert record["program_name"] == "Example Campus Ambassador"


def test_query_or_scheme_change_is_not_a_redirect_warning():
    assert not fetched_url_differs(PAGE, PAGE + "?ref=1")
    assert not fetched_url_differs(
        "http://example.edu/programs/ambassador",
        "https://example.edu/programs/ambassador",
    )
    assert not fetched_url_differs("https://example.edu", "https://example.edu/")


def test_different_host_warns_and_stays_out_of_json(monkeypatch, capsys):
    final = "https://github.com/education/students"
    record = _read(monkeypatch, lambda url: FetchedPage(text=LONG_TEXT, source_url=final))
    err = capsys.readouterr().err
    assert f"warning: redirected from {PAGE} to {final}" in err
    assert "very short" not in err
    assert record["source_url"] == final
    assert "warning" not in json.dumps(record)


def test_different_path_warns(monkeypatch, capsys):
    final = "https://example.edu/learn/"
    _read(monkeypatch, lambda url: FetchedPage(text=LONG_TEXT, source_url=final))
    err = capsys.readouterr().err
    assert f"warning: redirected from {PAGE} to {final}" in err


def test_short_text_warns_with_length(monkeypatch, capsys):
    text = "Microsoft Student Ambassadors"
    record = _read(monkeypatch, lambda url: FetchedPage(text=text, source_url=url))
    err = capsys.readouterr().err
    assert f"warning: extracted page text is very short ({len(text)} characters)" in err
    assert "redirected" not in err
    assert record["model"] == "nvidia/nemotron-3-super-120b-a12b"
    assert "warning" not in json.dumps(record)


def test_empty_text_warns_and_skips_the_model(monkeypatch, capsys):
    def fetch(url):
        return FetchedPage(text=" \n", source_url=url)

    def chat(model, messages):
        raise AssertionError("model should not be called")

    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    record = read_ambassador_page(PAGE, chat_complete=chat, fetch=fetch)
    err = capsys.readouterr().err
    assert "very short (0 characters)" in err
    assert record["model"] is None
    assert record["program_name"] is None


def test_redirect_and_short_text_both_warn(monkeypatch, capsys):
    final = "https://app.notion.com/campus"
    _read(monkeypatch, lambda url: FetchedPage(text="Notion", source_url=final))
    err = capsys.readouterr().err
    assert f"warning: redirected from {PAGE} to {final}" in err
    assert "very short (6 characters)" in err


def test_cli_warnings_go_to_stderr_not_the_json_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NVIDIA_API_KEY", "present-but-unused")
    monkeypatch.delenv("NVIDIA_MODEL", raising=False)
    final = "https://www.postman.com/learn/"

    monkeypatch.setattr(
        "ambassador_reader.core.fetch_page",
        lambda url: FetchedPage(text="Learn", source_url=final),
    )
    monkeypatch.setattr(
        "ambassador_reader.core.nim_chat",
        lambda model, messages: _json_reply(),
    )
    destination = tmp_path / "out.json"
    code = main(["https://www.postman.com/student-program/", "--out", str(destination)])
    captured = capsys.readouterr()
    assert code == 0
    assert "warning: redirected from https://www.postman.com/student-program/" in captured.err
    assert "very short" in captured.err
    assert "warning" not in captured.out
    written = destination.read_text(encoding="utf-8")
    assert "warning" not in written
    assert json.loads(written)["source_url"] == final
