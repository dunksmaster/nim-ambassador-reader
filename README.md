# nim-ambassador-reader

Given the URL of an ambassador, campus, or developer-champion program page, this tool fetches the page, extracts the readable text, and asks an NVIDIA-hosted chat model for a small JSON record. Any field the page text does not state is `null`.

It is built on [NVIDIA NIM](https://build.nvidia.com) through the OpenAI-compatible API at `https://integrate.api.nvidia.com/v1`. The default model is `nvidia/nemotron-3-super-120b-a12b`. If that model returns an API error, or its reply is still not valid JSON after one retry, the tool tries `mistralai/mistral-nemotron`. You can override the model with `--model` or the `NVIDIA_MODEL` environment variable. An override does not fall back to another model.

The model reply is parsed even when it is wrapped in markdown code fences or surrounded by prose, then checked with pydantic. `source_url` and `model` are set by this tool from the request. They are not taken from the model's JSON.

## Setup

Get a free API key from [build.nvidia.com](https://build.nvidia.com) and set `NVIDIA_API_KEY`. The key is read only from that environment variable (or from a local `.env` file loaded by the CLI). It is not printed, logged, or written into output files.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# Put your key on the NVIDIA_API_KEY= line in .env, or export it yourself.
set -a
source .env
set +a
```

`.env` is listed in `.gitignore`. Do not commit a real key.

## Usage

```bash
ambassador-reader https://example.com/campus-ambassador
ambassador-reader https://example.com/campus-ambassador --out examples/campus-ambassador.json
python -m ambassador_reader https://example.com/campus-ambassador
ambassador-reader https://example.com/campus-ambassador --model mistralai/mistral-nemotron
```

Pretty-printed JSON is written to stdout. `--out` also writes that same JSON to the given path, creating parent directories if needed.

## Web demo

Live demo: https://nim-ambassador-reader-doris-projects-c046e15b.vercel.app

`public/index.html` is a single page with a URL field, a submit button, a loading state, and pretty-printed JSON. It POSTs the URL to `/api/extract`. The Python function in `api/extract.py` calls `read_ambassador_page` from `ambassador_reader.core`. The extraction prompt, the pydantic schema, and the CLI output are unchanged.

The CLI still prints redirect and short-text warnings on stderr, and those warnings are still not fields of the CLI JSON. The web response adds the same warning strings in a `warnings` list.

The function reads `NVIDIA_API_KEY` from the environment. The key is not hardcoded and is not written into responses. If the variable is unset, the function returns HTTP 500 and a JSON `error` field.

Web-only limits:

- The URL must use `http` or `https`.
- The host is checked after DNS resolution. Loopback, private, link-local, multicast, reserved, and other non-public addresses are rejected. That includes `127.0.0.1`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.169.254`, `::1`, and `localhost`. The same check runs again on every redirect before that request is sent.
- The download stops at about 2 MB. The fetch times out after 8 seconds.
- Each client IP can make about 5 requests per minute. The counter is in memory on that function instance. On Vercel this is best-effort: instances do not share counts, and a new instance starts from zero.
- `vercel.json` sets `maxDuration` to 60 seconds for `api/extract.py`. The web path calls NIM with a 12 second timeout and no SDK retries, so one fetch plus a JSON retry on the default model and on the fallback model stays under 60 seconds. The CLI still uses a 90 second NIM timeout.

Status codes from the function: `400` for a rejected URL or a bad request, `429` when that instance's limit is exceeded, `502` when the page or the model returns an error, `504` when the fetch or the model times out, and `500` when `NVIDIA_API_KEY` is missing.

To deploy, import the repository in Vercel and set `NVIDIA_API_KEY` in the project environment. Vercel installs the packages listed in `requirements.txt`. Static files are served from `public/`. `.python-version` selects Python 3.12.

To record a real example file:

```bash
ambassador-reader <url> --out examples/<slug>.json
```

Real outputs from 2026-09-27 are in [examples/](examples/README.md), with field checks in [examples/VERIFICATION.md](examples/VERIFICATION.md). The all-null object below is only the shape of a record, not one of those runs.

## Output

| Field | Meaning |
| --- | --- |
| `program_name` | Program name stated on the page, or `null`. |
| `company` | Organization that runs the program, or `null`. |
| `deadline` | Deadline wording stated on the page, or `null`. |
| `eligibility` | Who may apply, including any geographic, student, or age gate the page states, or `null`. |
| `benefits` | What a participant receives, including any stipend the page states, or `null`. |
| `apply_url` | Application link, or `null`. Relative links are resolved against the fetched page URL. |
| `source_url` | The page that was fetched, after redirects. |
| `model` | The NIM model id that produced the extraction. `null` when the page had no readable text and no model was called. |

Shape only — these `null`s are not an extraction from a real page:

```json
{
  "program_name": null,
  "company": null,
  "deadline": null,
  "eligibility": null,
  "benefits": null,
  "apply_url": null,
  "source_url": "https://example.com/campus-ambassador",
  "model": "nvidia/nemotron-3-super-120b-a12b"
}
```

## Nulls

`null` means that fact was not in the text extracted from the page. The prompt tells the model not to guess or fill gaps from general knowledge. `null` does not mean the program lacks that fact. The detail may be in an image, a script-rendered section, or another page. Check the source page.

If the model returns a list of strings for one field, the strings are joined with `"; "`. Empty strings become `null`. A value that is not an `http`, `https`, or `mailto` link does not become `apply_url`.

## Validation

If the reply is empty or does not match the schema, the same model is called once more. If the second reply is also unusable, the command exits with an error that names the model. It does not include the API key.

Each NIM request uses a 90 second timeout and `max_retries=1`. That limit is separate from the one retry this tool makes when the JSON is unusable.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The tests mock the page fetch and the NIM call. They pass without `NVIDIA_API_KEY`.

## Limitations

- Pages that render in the browser, or that redirect to a login page, yield little or no text, so most fields come back `null`. The command prints a warning on stderr when the fetched URL's host or path differs from the URL you passed, and when the extracted text is under 200 characters. Those warnings are not fields in the JSON.
- Application status, such as paused applications, is not its own field. A page can state that applications are paused and the record can still leave `deadline` null, because no deadline was stated. The Twilio Champions note in [examples/VERIFICATION.md](examples/VERIFICATION.md) is an example.
- The model can stitch a FAQ answer into a field without the question, or infer an eligibility rule the page does not state. Two of the six runs on 2026-09-27 did this, and those outputs were dropped. See [examples/VERIFICATION.md](examples/VERIFICATION.md).
- This is an extraction aid. It can miss a fact or copy one incorrectly. Always check the source page before you rely on a field.
- Script, style, and similar tags are removed. Anchor targets are appended so a link that is only in `href` can still be seen. Text longer than 20,000 characters is truncated. Navigation and footer text counts toward that limit.
- If `NVIDIA_API_KEY` is missing, the command stops before it fetches the page.

## License

MIT. See [LICENSE](LICENSE).
