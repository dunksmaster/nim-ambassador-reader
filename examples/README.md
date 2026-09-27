# Examples

These four files are the unedited JSON that `ambassador-reader` wrote on 2026-09-27. Every run used `nvidia/nemotron-3-super-120b-a12b`.

| File | Source URL | Fetched | Model |
| --- | --- | --- | --- |
| [docker-captains.json](docker-captains.json) | https://www.docker.com/community/captains/ | 2026-09-27 | nvidia/nemotron-3-super-120b-a12b |
| [grafana-champions.json](grafana-champions.json) | https://grafana.com/community/champions/ | 2026-09-27 | nvidia/nemotron-3-super-120b-a12b |
| [mongodb-champions.json](mongodb-champions.json) | https://www.mongodb.com/community/champions | 2026-09-27 | nvidia/nemotron-3-super-120b-a12b |
| [twilio-champions.json](twilio-champions.json) | https://www.twilio.com/en-us/champions | 2026-09-27 | nvidia/nemotron-3-super-120b-a12b |

Field checks, the two runs that were dropped and why, and the pages that could not be fetched are in [VERIFICATION.md](VERIFICATION.md).

To record another example, set `NVIDIA_API_KEY` and run:

```bash
ambassador-reader <url> --out examples/<slug>.json
```

Commit the file that command writes. Do not hand-edit it.
