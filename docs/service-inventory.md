# Verified Akamai service inventory

Checked on 2026-11-20. Sources: the organizer starter kit (`akamai-workshop-seattle-hackathon`), the event page
(luma.com/akamai-build-nov20), and live requests from this laptop and from the Linode. No secrets appear here.

## Event

| Item | Value | Evidence | Status |
|---|---|---|---|
| Date and venue | Friday, November 20 (2026), GitHub Bellevue, Bellevue, WA | Event page; starter README "Friday, November 20, 2026" | Verified |
| Submission deadline | 3:00 PM local (Pacific). Judging starts at 3:00 PM, top projects demo at 4:00 PM | Event page: "3:00 PM: Project submissions due; judging begins". The page shows no time zone; the venue is in Bellevue | Verified (time zone inferred from venue) |
| Submission URL | Not published | Starter `docs/judging.md`: "Get the submission link from event staff." The event page has no link | **Blocked: ask event staff** |
| Judging weights | 40% other services, 20% inference, 20% usefulness, 20% creativity | Starter `docs/judging.md` | Verified |

## Akamai Cloud Inference (Qwen 3)

| Item | Value | Evidence |
|---|---|---|
| Base URL | `https://api.akamai-inference.com/v1` (OpenAI-compatible) | Starter `.env.example`, `01-inference/README.md` |
| Auth | `Authorization: Bearer <AKAMAI_API_KEY>`. Event key starts `sk_aka_`, issued by event staff | Starter `check.py` |
| Chat model ID | `qwen3-8-27b` (131,072-token context; chat, tools, JSON output) | Starter Lab 01 model table; live `GET /models` |
| Models this key can use | `gemma-4-26b-a4b-it`, `qwen3-8-27b`, `qwen3-embedding-8b` | Live `GET /models` |
| Request schema | `POST /chat/completions` with `{model, messages:[{role, content}], max_tokens, temperature, response_format}`; `response_format` accepts `{"type":"json_schema","json_schema":{name, schema}}` or `{"type":"json_object"}` | Starter `json_output.py`; app's live runs |
| Response fields used | `id`, `model`, `choices[0].message.content`, `choices[0].finish_reason`, `usage.total_tokens` | Live responses |
| "Chat mode" / thinking | No reasoning output was returned with or without `chat_template_kwargs.enable_thinking=false`. The app uses the default chat call | Live probe: both returned plain `ok`, no reasoning field |

**Smoke request (synthetic), from this laptop:**

```
GET  /v1/models -> ['gemma-4-26b-a4b-it', 'qwen3-8-27b', 'qwen3-embedding-8b']
POST /v1/chat/completions -> 1577 ms, id chatcmpl-a7b638da2e49ea7f, model qwen3-8-27b, finish stop, 81 tokens
content: {"breathing": "absent", "quote": "My neighbor is not breathing at 5th and Pine."}
```

**From the deployed app on the Linode:** model `qwen3-8-27b`, 13,680 ms, id `chatcmpl-908b594ea5252241`;
6 medical facts accepted, the planted false fact rejected ("quote is not in the transcript").

## Akamai Cloud Compute (Linode) and Cloud Firewall

| Item | Value | Evidence |
|---|---|---|
| Procedure | Starter Lab 02: Seattle, Ubuntu 24.04, Nanode 1 GB, SSH key, `cloud-init.yaml` as User Data (installs uv), Cloud Firewall with Drop inbound plus allow TCP 22, 80 from your /32, systemd service | Starter `02-linode/README.md`; followed here |
| Instance | `sea-copilot-web`, ID 111517492, us-sea, 172.232.188.234 | Cloud Manager summary |
| Firewall | `sea-copilot-fw` (ID 233297022), rule `ssh-and-web`, TCP 22, 80 from 4.53.158.54/32 | Cloud Manager; port tests before and after saving |
| Service | `copilot.service`, `uv run /root/copilot/app.py` on port 80 | `journalctl`: "Uvicorn running on http://0.0.0.0:80" |
| Health | `GET http://172.232.188.234/health` -> `{"app":"ok","host":"sea-copilot-web","inference":"configured (qwen3-8-27b)","login":"on",...}` | Live curl |
| Access control | Login required (`dispatcher` + `APP_PASSWORD`); without it `/api/*` returns 401 | Live curl |
| Deploy | `bash deploy/deploy.sh 172.232.188.234` | This repo |

## Managed PostgreSQL

| Item | Value | Status |
|---|---|---|
| Procedure | Starter Lab 03: Seattle, Nanode 1 GB, 1 node, Specific Access allow list, connection details into `PGHOST`/`PGPORT`/`PGPASSWORD` | Verified from starter docs |
| Cluster | Not created or not yet configured in `.env` | **Pending: in progress** |
| Without it | The app runs with an in-memory store, and `/health` says "memory only, lost on restart". It does not claim persistence | Verified |

## Credentials you configure yourself (never paste them into chat)

- [x] `AKAMAI_API_KEY` in `.env` (from event staff; saved by `uv run check.py`)
- [x] SSH public key on the Linode (`~/.ssh/authorized_keys`)
- [x] `APP_PASSWORD` in `.env` (generated; app login user `dispatcher`)
- [ ] `PGHOST`, `PGPORT`, `PGPASSWORD` in `.env` (from the database's Connection Details)
- [ ] Submission link (from event staff)

## If inference becomes unavailable

There is no mock. `app.extract()` is the only model call. On any failure (missing key, timeout, HTTP error, bad JSON)
the app returns `ai.status = "unavailable"` with the error. The model-free alerts, missing-fact questions and protocol
cards still show. The "Simulate AI outage" switch triggers the same path for the demo and is labelled as simulated.
