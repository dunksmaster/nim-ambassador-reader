"""CLI output and the missing-key path."""

import json

from ambassador_reader.cli import main
from ambassador_reader.core import MissingApiKeyError


def test_cli_prints_and_writes_json(tmp_path, monkeypatch, capsys):
    def fake(url, model=None):
        assert url == "https://example.com/program"
        assert model == "custom/model"
        return {
            "program_name": None,
            "company": None,
            "deadline": None,
            "eligibility": None,
            "benefits": None,
            "apply_url": None,
            "source_url": url,
            "model": "custom/model",
        }

    monkeypatch.setattr("ambassador_reader.cli.read_ambassador_page", fake)
    destination = tmp_path / "examples" / "slug.json"
    code = main(
        [
            "https://example.com/program",
            "--model",
            "custom/model",
            "--out",
            str(destination),
        ]
    )
    assert code == 0
    written = json.loads(destination.read_text(encoding="utf-8"))
    printed = json.loads(capsys.readouterr().out)
    assert written == printed
    assert written["source_url"] == "https://example.com/program"
    assert written["model"] == "custom/model"


def test_cli_missing_key_message(monkeypatch, capsys):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_MODEL", raising=False)

    def boom(url, model=None):
        raise MissingApiKeyError(
            "NVIDIA_API_KEY is not set. Get a key at https://build.nvidia.com "
            "and export NVIDIA_API_KEY."
        )

    monkeypatch.setattr("ambassador_reader.cli.read_ambassador_page", boom)
    code = main(["https://example.com/program"])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "NVIDIA_API_KEY" in captured.err
    assert "build.nvidia.com" in captured.err


def test_cli_missing_key_does_not_fetch(monkeypatch, capsys):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_MODEL", raising=False)
    code = main(["https://example.com/program"])
    captured = capsys.readouterr()
    assert code == 1
    assert "NVIDIA_API_KEY" in captured.err
    assert "build.nvidia.com" in captured.err
