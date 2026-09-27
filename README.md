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

To record a real example file:

```bash
ambassador-reader <url> --out examples/<slug>.json
```

This repository does not include fabricated example output. See [examples/README.md](examples/README.md).

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

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The tests mock the page fetch and the NIM call. They pass without `NVIDIA_API_KEY`.

## Limitations

- Pages that render their content with JavaScript often yield little or no text, so most fields come back `null`.
- This is an extraction aid. It can miss a fact or copy one incorrectly. Always check the source page before you rely on a field.
- Script, style, and similar tags are removed. Anchor targets are appended so a link that is only in `href` can still be seen. Text longer than 20,000 characters is truncated.
- If `NVIDIA_API_KEY` is missing, the command stops before it fetches the page.

## License

MIT. See [LICENSE](LICENSE).
