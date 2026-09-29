"""Command-line entry point for ambassador-reader."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ambassador_reader.core import DEFAULT_MODEL, AmbassadorReaderError, read_ambassador_page


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from a local .env file without overriding the environment.

    Values already set in the process environment win. Empty values are skipped.
    This never prints the values it reads.
    """
    env_path = path or Path.cwd() / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ambassador-reader",
        description=(
            "Fetch an ambassador, campus, or developer-champion program page "
            "and extract a JSON record with an NVIDIA NIM chat model."
        ),
    )
    parser.add_argument("url", help="http(s) URL of the program page")
    parser.add_argument(
        "--out",
        metavar="FILE",
        help="Also write the JSON to this path (for example examples/<slug>.json)",
    )
    parser.add_argument(
        "--model",
        help=(
            "NIM model id. Overrides the NVIDIA_MODEL environment variable. "
            f"Default: {DEFAULT_MODEL}"
        ),
    )
    args = parser.parse_args(argv)

    load_dotenv()
    try:
        record = read_ambassador_page(args.url, model=args.model)
    except AmbassadorReaderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
