# Verification of example outputs

The JSON files in this folder are exactly what `ambassador-reader <url> --out <file>` wrote.
Nobody edited them by hand. Source: branch `cursor/ambassador-reader-735d` of
dunksmaster/nim-ambassador-reader, installed from the GitHub tarball into a local venv.
Offline test suite: `pytest` → 37 passed.

All pages were fetched on **2026-09-27** (Europe/Budapest, around 23:59 CEST).
Each non-null field was checked against the page text the tool itself fetched
(`html_to_text` output). A null is counted as correct when the page does not state that fact.

## Kept examples

| File | Source URL | Model that answered | Run time | Accuracy note |
|---|---|---|---|---|
| `docker-captains.json` | https://www.docker.com/community/captains/ | nvidia/nemotron-3-super-120b-a12b | ~11.6 s | Accurate. The page states every eligibility item, application category, benefit, and the Advocu apply link. `deadline` is null, which is correct because the page gives no deadline. |
| `grafana-champions.json` | https://grafana.com/community/champions/ | nvidia/nemotron-3-super-120b-a12b | ~16.6 s | Accurate. The name, company ("Grafana Labs" appears in the page title and footer), perks, and Google Forms apply link all match the page. The eligibility text is taken from the "this program is for you" paragraph. It leaves out the "four contributions per year" expectation, which is an omission and not an error. |
| `mongodb-champions.json` | https://www.mongodb.com/community/champions | nvidia/nemotron-3-super-120b-a12b | ~7.6 s | Accurate. The four benefit groups and the nomination-only eligibility match the page. `apply_url` is null, which is correct: Champions are nominated, and the only apply link on the page is for the separate Creators Program. |
| `twilio-champions.json` | https://www.twilio.com/en-us/champions | nvidia/nemotron-3-super-120b-a12b | ~10.7 s | Accurate. Eligibility and benefits are near-verbatim. `apply_url` is null, which is correct because the page has no apply link. Caveat: the page says "Applications are currently paused. Stay tuned for applications reopening in September 2026." The default model left `deadline` null. That is defensible because no deadline is stated, but the record does not show that applications are paused. |

## Dropped outputs (unchanged tool output, kept in `dropped/` for reference)

- `dropped/cursor-ambassadors.json` (https://cursor.com/ambassadors, nvidia/nemotron-3-super-120b-a12b, ~18.6 s). Benefits, the 18+ requirement, the apply link (Typeform), and the company ("Anysphere, Inc." from the page footer) are all correct. Dropped because `eligibility` ends with a FAQ answer pasted out of context: "No need! We're excited to partner with Ambassadors who speak different languages." The question it answers ("Do I have to be fluent in English?") is missing, so the field is confusing.
- `dropped/google-developer-experts.json` (https://developers.google.com/community/experts, nvidia/nemotron-3-super-120b-a12b, ~7.0 s). Nomination-only eligibility, `benefits` null, and `apply_url` null are correct. Dropped because `eligibility` begins with "Open to developers globally". The page only calls the GDE program "a global community"; it never states an open or global eligibility rule. The model inferred it, which breaks the tool's "do not infer" rule.

## Pages not run (failed the pre-check fetch or had no usable server-rendered text)

The pre-check used the tool's own `fetch_page()`, with User-Agent `ambassador-reader/0.1`.

- GitHub Campus Experts, https://education.github.com/experts: redirects to https://github.com/education/students. The Campus Experts page is gone. https://github.com/education/experts → HTTP 404.
- Microsoft Learn Student Ambassadors, https://studentambassadors.microsoft.com/: redirects to https://mvp.microsoft.com/studentambassadors. The page is rendered client-side, and only 29 characters of text come back ("Microsoft Student Ambassadors").
- Postman Student program, https://www.postman.com/student-program/ and `/student-program/student-expert/`: both redirect to https://www.postman.com/learn/, a generic page, not the program page.
- n8n Ambassadors, https://n8n.io/ambassadors/: redirects to a Notion page (app.notion.com), which returns only 6 characters of text ("Notion").
- AWS Community Builders, https://aws.amazon.com/developer/community/community-builders/: redirects to https://builder.aws.com/community/community-builders, which is rendered client-side (18 characters: "AWS Builder Center").
- Notion Campus Leaders, https://www.notion.com/campus: 307 redirect to app.notion.com/campus, then HTTP 401. A browser User-Agent gets the same 401.
- HashiCorp Ambassadors, https://www.hashicorp.com/en/ambassadors: HTTP 429. A browser User-Agent gets the same 429.
- Also not usable: Cloudflare (/ambassadors/) and Supabase (/ambassadors) return 404; Elastic (/community/contributors) returns 404.

## Tool rough edges seen during this run

1. **Fallback model reliability and latency.** Test run: `--model mistralai/mistral-nemotron` on the Twilio page. The first try failed after about 60 s with: `error: NIM request failed for model mistralai/mistral-nemotron: Error code: 500 - {'type': 'urn:inference-connection:problem-details:internal-server-error', 'title': 'Internal Server Error', 'status': 500, 'detail': 'Inference connection error while making inference request'}`. The retry succeeded but took about 222 s. `nim_chat` builds `OpenAI(...)` with no explicit `timeout` or `max_retries`, so the SDK defaults apply (600 s timeout, 2 retries). One run can hang for minutes, and a worst case with default failure plus fallback could take much longer.
2. **Thin or client-rendered pages are still sent to the model.** Only fully empty text skips the model. The MS Student Ambassadors page (29 chars of text) was sent anyway. The model behaved well and returned only the name and company, with the other fields null. Still, a minimum-text threshold or a warning would be safer.
3. **Silent redirects.** For example, education.github.com/experts → github.com/education/students, and postman.com/student-program → postman.com/learn. `source_url` honestly shows the final URL, but the tool gives no warning that the page it read is not the one requested.
4. **Truncation at 20,000 characters with no boilerplate removal.** The Docker and Twilio pages were both cut to 20,012 chars, mostly because of nav and footer text. The key content made it in this time, but `<nav>`, `<header>`, and `<footer>` are not stripped before the limit is applied.
5. **The schema has no status field.** The deadline prompt allows only a date or "rolling". The default model left Twilio's "applications paused, reopening September 2026" out, while the fallback model put that sentence into `deadline`. The two models behave differently here.
6. **Model quality issues** (prompt-level, not crashes): FAQ answers get concatenated out of context (Cursor), and there was one inferred claim (GDE, "Open to developers globally").
7. **Minor:** passing `--model` or setting `NVIDIA_MODEL` turns off the fallback, even when the chosen model is the default. This is by design in `select_model`, and the README already says it ("An override does not fall back to another model").
