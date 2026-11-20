# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn>=0.30", "openai>=1.40", "psycopg[binary]>=3.2", "python-dotenv>=1.0"]
# ///
"""Emergency Dispatch Copilot: decision support for a human dispatcher.

    uv run app.py      then open http://localhost:8000

Built on the Akamai workshop starter. Inference is Qwen 3 on Akamai Cloud Inference.
PostgreSQL (Akamai Managed Database) stores incidents, revisions and review decisions when PGHOST is set.
"""
import base64
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

import psycopg
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).parent))
import core  # noqa: E402

load_dotenv(override=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("copilot")  # never logs transcripts, quotes or keys: only ids, counts, latency

BASE_URL = os.environ.get("AKAMAI_BASE_URL", "")
API_KEY = os.environ.get("AKAMAI_API_KEY", "")
MODEL = os.environ.get("CHAT_MODEL", "qwen3-8-27b")
DATABASE = bool(os.environ.get("PGHOST"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")  # set it to require a login (user: dispatcher)
AI_TIMEOUT = float(os.environ.get("AI_TIMEOUT", "30"))
MAX_TRANSCRIPT = 8000

_client = None


def client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(base_url=BASE_URL, api_key=API_KEY, timeout=AI_TIMEOUT, max_retries=0)
    return _client


SYSTEM_PROMPT = (
    "You extract facts from an emergency call transcript for a human dispatcher. "
    "Only report what the caller actually said. For every fact give `quote`: an exact, contiguous, "
    "word-for-word excerpt copied from the transcript that supports it. Do not paraphrase the quote. "
    "`value` is short and uses the caller's own words. "
    "`polarity` is `present` when the quote affirms the field (e.g. 'he is breathing'), `absent` when it "
    "denies it (e.g. 'not breathing', 'no blood', 'unconscious' for the conscious field), and `unknown` "
    "when the caller is unsure. If something was not said, leave it out. Never give medical advice."
)


def extract(transcript):
    """One Qwen 3 call returning (raw model JSON, metadata). Raises on any failure."""
    if not (BASE_URL and API_KEY):
        raise RuntimeError("inference is not configured (AKAMAI_BASE_URL / AKAMAI_API_KEY)")
    started = time.perf_counter()
    response = client().chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": f"Transcript:\n{transcript}"}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "dispatch_facts", "schema": core.EXTRACTION_SCHEMA}},
        temperature=0,
        max_tokens=1200,
    )
    latency_ms = round((time.perf_counter() - started) * 1000)
    choice = response.choices[0]
    if choice.finish_reason != "stop":
        raise RuntimeError(f"model reply stopped early ({choice.finish_reason})")
    text = re.sub(r"<think>.*?</think>", "", choice.message.content or "", flags=re.DOTALL).strip()
    data = json.loads(text)
    usage = response.usage
    meta = {"provider": "Akamai Cloud Inference", "endpoint": BASE_URL, "model": response.model,
            "request_id": response.id, "latency_ms": latency_ms,
            "tokens": usage.total_tokens if usage else None}
    return data, meta


# ---------------------------------------------------------------- storage: PostgreSQL, or memory

SCHEMA = """
create table if not exists incidents (
  id bigserial primary key,
  scenario text,
  revision int not null default 1,
  transcript text not null,
  analysis jsonb not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create table if not exists reviews (
  id bigserial primary key,
  incident_id bigint not null references incidents(id),
  revision int not null,
  field text not null,
  decision text not null,
  note text,
  created_at timestamptz not null default now()
);
"""


class Store:
    """Same interface for PostgreSQL and the in-memory fallback, so the page shows which one is live."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mem = {}
        self.mem_reviews = []
        self.ready = False

    def conn(self):
        c = psycopg.connect(connect_timeout=5, autocommit=True)
        if not self.ready:
            c.execute(SCHEMA)
            self.ready = True
        return c

    def save(self, incident_id, scenario, transcript, analysis):
        """Create an incident, or add a revision. Returns (id, revision)."""
        if DATABASE:
            with self.conn() as c:
                if incident_id is None:
                    row = c.execute("insert into incidents (scenario, transcript, analysis) values (%s,%s,%s) "
                                    "returning id, revision", (scenario, transcript, json.dumps(analysis))).fetchone()
                else:
                    row = c.execute("update incidents set transcript=%s, analysis=%s, revision=revision+1, "
                                    "updated_at=now() where id=%s returning id, revision",
                                    (transcript, json.dumps(analysis), incident_id)).fetchone()
                    if row is None:
                        raise HTTPException(404, "No such incident.")
                return row[0], row[1]
        with self.lock:
            if incident_id is None:
                incident_id = len(self.mem) + 1
                self.mem[incident_id] = {"id": incident_id, "scenario": scenario, "revision": 0}
            if incident_id not in self.mem:
                raise HTTPException(404, "No such incident.")
            rec = self.mem[incident_id]
            rec.update(transcript=transcript, analysis=analysis, revision=rec["revision"] + 1,
                       updated_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
            return incident_id, rec["revision"]

    def get(self, incident_id):
        if DATABASE:
            with self.conn() as c:
                row = c.execute("select id, scenario, revision, transcript, analysis, updated_at from incidents "
                                "where id=%s", (incident_id,)).fetchone()
                if not row:
                    raise HTTPException(404, "No such incident.")
                reviews = c.execute("select field, decision, revision, note, created_at from reviews "
                                    "where incident_id=%s order by id", (incident_id,)).fetchall()
            return {"id": row[0], "scenario": row[1], "revision": row[2], "transcript": row[3],
                    "analysis": row[4], "updated_at": row[5].isoformat(),
                    "reviews": [{"field": f, "decision": d, "revision": r, "note": n, "at": t.isoformat()}
                                for f, d, r, n, t in reviews]}
        rec = self.mem.get(incident_id)
        if not rec:
            raise HTTPException(404, "No such incident.")
        return {**rec, "reviews": [r for r in self.mem_reviews if r["incident_id"] == incident_id]}

    def review(self, incident_id, revision, field, decision, note):
        """Record a human decision. Refused if the incident moved on to a newer revision (revision guard)."""
        if DATABASE:
            with self.conn() as c:
                with c.transaction():
                    row = c.execute("select revision from incidents where id=%s for update", (incident_id,)).fetchone()
                    if not row:
                        raise HTTPException(404, "No such incident.")
                    if row[0] != revision:
                        raise HTTPException(409, f"Stale review: incident is at revision {row[0]}, "
                                                 f"you reviewed revision {revision}. Reload and review again.")
                    c.execute("insert into reviews (incident_id, revision, field, decision, note) "
                              "values (%s,%s,%s,%s,%s)", (incident_id, revision, field, decision, note))
            return
        with self.lock:
            rec = self.mem.get(incident_id)
            if not rec:
                raise HTTPException(404, "No such incident.")
            if rec["revision"] != revision:
                raise HTTPException(409, f"Stale review: incident is at revision {rec['revision']}, "
                                         f"you reviewed revision {revision}. Reload and review again.")
            self.mem_reviews.append({"incident_id": incident_id, "field": field, "decision": decision,
                                     "revision": revision, "note": note, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})

    def recent(self):
        if DATABASE:
            with self.conn() as c:
                rows = c.execute("select id, scenario, revision, analysis->'ai'->>'status', updated_at "
                                 "from incidents order by id desc limit 20").fetchall()
            return [{"id": i, "scenario": s, "revision": r, "ai_status": a, "updated_at": t.isoformat()}
                    for i, s, r, a, t in rows]
        return [{"id": r["id"], "scenario": r["scenario"], "revision": r["revision"],
                 "ai_status": r["analysis"]["ai"]["status"], "updated_at": r["updated_at"]}
                for r in sorted(self.mem.values(), key=lambda r: -r["id"])][:20]


store = Store()
app = FastAPI(title="Emergency Dispatch Copilot")


# ---------------------------------------------------------------- optional login

@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if APP_PASSWORD and request.url.path != "/health":
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, APP_PASSWORD) and user == "dispatcher"
            except Exception:
                ok = False
        if not ok:
            return JSONResponse({"detail": "login required"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="copilot"'})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------- routes

class AnalyzeBody(BaseModel):
    transcript: str = Field(min_length=1, max_length=MAX_TRANSCRIPT)
    scenario: str | None = None
    incident_id: int | None = None
    simulate_ai_failure: bool = False


class ReviewBody(BaseModel):
    revision: int
    field: str
    decision: str  # confirm | reject
    note: str | None = Field(default=None, max_length=300)


@app.get("/", response_class=HTMLResponse)
def page():
    return (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


@app.get("/health")
def health():
    status = {"app": "ok", "host": os.uname().nodename,
              "inference": f"configured ({MODEL})" if BASE_URL and API_KEY else "not set",
              "database": "not set (memory only, lost on restart)", "login": "on" if APP_PASSWORD else "off"}
    if DATABASE:
        try:
            with store.conn() as c:
                n = c.execute("select count(*) from incidents").fetchone()[0]
            status["database"] = f"connected ({n} incidents stored)"
        except Exception as error:
            status["database"] = f"error: {str(error).strip().splitlines()[0][:150]}"
    return status


@app.get("/api/scenarios")
def scenarios():
    return core.SCENARIOS


@app.post("/api/analyze")
def analyze(body: AnalyzeBody):
    transcript = body.transcript.strip()
    alerts = core.screen(transcript)  # first, and with no model involved
    ai = {"status": "ok"}
    accepted, rejected, incident_type, raw = [], [], "unknown", None
    try:
        if body.simulate_ai_failure:
            raise RuntimeError("simulated inference outage (demo switch)")
        raw, meta = extract(transcript)
        incident_type = raw.get("incident_type", "unknown")
        accepted, rejected = core.validate(transcript, raw.get("facts", []))
        ai.update(meta)
    except Exception as error:
        ai = {"status": "unavailable", "error": str(error).splitlines()[0][:200]}
    if incident_type == "unknown":  # fall back to the screen's view so missing-fact prompts still work
        codes = {a["code"] for a in alerts if not a["negated"]}
        incident_type = "fire" if codes & {"FIRE", "SMOKE"} else "medical" if codes else "unknown"
    analysis = {
        "alerts": alerts, "ai": ai, "incident_type": incident_type,
        "accepted": accepted, "rejected": rejected, "raw_model_output": raw,
        "missing": core.missing(incident_type, accepted), "cards": core.cards(alerts),
    }
    incident_id, revision = store.save(body.incident_id, body.scenario, transcript, analysis)
    log.info("incident=%s rev=%s ai=%s latency_ms=%s accepted=%d rejected=%d alerts=%d",
             incident_id, revision, ai["status"], ai.get("latency_ms"), len(accepted), len(rejected), len(alerts))
    return {"incident_id": incident_id, "revision": revision, "persisted": DATABASE, **analysis}


@app.post("/api/incidents/{incident_id}/review")
def review(incident_id: int, body: ReviewBody):
    if body.decision not in {"confirm", "reject"}:
        raise HTTPException(400, "decision must be confirm or reject")
    if body.field not in core.FIELDS:
        raise HTTPException(400, "unknown field")
    store.review(incident_id, body.revision, body.field, body.decision, body.note)
    return {"saved": True, "persisted": DATABASE}


@app.get("/api/incidents")
def incidents():
    return store.recent()


@app.get("/api/incidents/{incident_id}")
def incident(incident_id: int):
    return store.get(incident_id)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0" if "PORT" in os.environ else "127.0.0.1", port=int(os.environ.get("PORT", 8000)))
