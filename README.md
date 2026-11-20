# Emergency Dispatch Copilot

AI-assisted decision support for human 911 dispatchers, running on Akamai Cloud. Built on the Akamai Seattle hackathon starter (FastAPI + Akamai Cloud Inference + Linode).

> Synthetic demo data only. Not a medical device and not clinical guidance. The dispatcher makes every decision.

## Judging summary

```text
User and task: A 911 dispatcher taking a medical or fire call who must spot life threats, know what is still
  unknown, and follow protocol, while typing and talking at the same time.
Benefit: Life-threat alerts in under a second, a checklist of facts still missing (with the exact question to
  ask), and the matching protocol card. Every AI-extracted fact shows the caller's own words as evidence.
Services:
  - Akamai Cloud Compute (Linode, us-sea): runs the app as a systemd service; /health shows the host name.
  - Akamai Cloud Firewall: only the team's IP reaches ports 22 and 80.
  - Akamai Managed Database (PostgreSQL): stores each incident, every transcript revision, the model output
    and the dispatcher's confirm/reject decisions. History survives an app restart; the revision guard
    (SELECT ... FOR UPDATE) refuses a review made on an outdated revision.
Inference: Qwen 3 (qwen3-8-27b) on Akamai Cloud Inference, JSON-schema output. Task: extract dispatch facts
  (location, breathing, consciousness, bleeding, people trapped, ...) each with polarity and a verbatim quote.
  Chosen for reliable structured output and tool/JSON support at low latency. Checked by code, not trust:
  a fact is rejected if its quote is not in the transcript, does not mention the field, or contradicts
  the stated polarity ("not breathing" can never become "breathing: present").
Technical choice: Reliability. The raw keyword risk screen runs before the model and does not depend on it;
  when inference fails or times out (30 s, no retries) alerts, missing-fact questions and protocol cards
  still appear. Protocol text is a fixed table chosen by rules: the model never writes guidance.
Our idea: Treat the LLM as an untrusted witness. Its facts must cite evidence, rejected facts are shown with
  the reason, and a non-AI safety net keeps working when the AI does not.
Demo: 1) Medical scenario -> alerts, Qwen facts with quotes, missing age/location questions, CPR card.
  2) Tick "Simulate AI outage" -> alerts and cards remain. 3) Fire scenario -> person-trapped alert.
  4) Confirm a fact, edit the transcript, re-analyze -> revision 2; the old review is refused (409).
  5) Restart the service -> incidents still listed (PostgreSQL).
Known limits: English keyword rules only; no speech-to-text; no CAD integration; protocol cards are demo text,
  not an agency-approved protocol set; basic-auth login only.
```

## Architecture

```
Browser ──HTTP(S)──> Cloud Firewall ──> Linode (us-sea) : FastAPI app.py
                                          │  1. core.screen()      keyword alerts (no AI)
                                          │  2. Qwen 3 extract ───> Akamai Cloud Inference (api.akamai-inference.com)
                                          │  3. core.validate()    quote + field + polarity checks
                                          │  4. core.missing() / core.cards()   fixed rules
                                          └─> Managed PostgreSQL   incidents, revisions, reviews
```

| Path | What it does |
|---|---|
| `GET /health` | host name, inference config, database status and incident count |
| `POST /api/analyze` | `{transcript, scenario?, incident_id?, simulate_ai_failure?}` → alerts, AI metadata (model, latency, request id), accepted/rejected facts, missing facts, cards |
| `POST /api/incidents/{id}/review` | `{revision, field, decision}`; 409 if the incident has a newer revision |
| `GET /api/incidents[/{id}]` | recent incidents, or one with its reviews |

## Run locally

Install [uv](https://docs.astral.sh/uv/), then:

```bash
uv run check.py          # makes .env from .env.example and asks for the sk_aka_ key
uv run app.py            # http://localhost:8000
```

Tests (no key or network needed; the model call is stubbed):

```bash
uv run --python 3.12 --with pytest --with httpx --with fastapi --with openai --with "psycopg[binary]" --with python-dotenv --with uvicorn pytest -q tests
```

## Deploy to a Linode

Create a Nanode in Seattle with `deploy/cloud-init.yaml` as User Data and a Cloud Firewall allowing your IP on 22 and 80 (see the starter's Lab 02). For PostgreSQL, create a Managed Database, add the Linode's IPs to its allow list, and fill the `PG*` lines in `.env`. Then:

```bash
IP=PASTE_YOUR_LINODE_IP
ssh root@$IP "mkdir -p copilot"
scp .env app.py core.py index.html deploy/app.service root@$IP:copilot/
ssh root@$IP "cp copilot/app.service /etc/systemd/system/copilot.service && systemctl daemon-reload && systemctl enable --now copilot && systemctl restart copilot"
curl http://$IP/health
```

Logs: `ssh root@$IP journalctl -u copilot -n 30`. Logs record only incident ids, counts and latency, never transcripts or keys. Set `APP_PASSWORD` in `.env` before letting anyone outside the firewall reach it.
