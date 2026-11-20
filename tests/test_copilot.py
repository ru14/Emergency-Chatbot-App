"""Run:  uv run --with pytest --with httpx --with fastapi --with openai --with "psycopg[binary]" --with python-dotenv pytest -q
No network and no key needed: the model call is replaced with fixed outputs."""
import os
import sys
from pathlib import Path

os.environ.pop("PGHOST", None)
os.environ["AKAMAI_BASE_URL"] = "http://test.invalid/v1"
os.environ["AKAMAI_API_KEY"] = "test"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as appmod  # noqa: E402
import core  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

MED = core.SCENARIOS["medical"]["transcript"]
FIRE = core.SCENARIOS["fire"]["transcript"]
appmod.load_dotenv = lambda **k: None
appmod.DATABASE = False
appmod.APP_PASSWORD = ""  # a local .env may turn the login on; these tests exercise the API itself
client = TestClient(appmod.app)


def codes(alerts, include_negated=False):
    return {a["code"] for a in alerts if include_negated or not a["negated"]}


# ---------------- independent screen

def test_screen_medical():
    c = codes(core.screen(MED))
    assert {"NOT_BREATHING", "UNCONSCIOUS", "CHEST_PAIN"} <= c


def test_screen_marks_negated_bleeding():
    alerts = core.screen(MED)
    bleed = [a for a in alerts if a["code"] == "SEVERE_BLEEDING"]
    assert bleed == [] or bleed[0]["negated"]


def test_screen_fire():
    assert {"FIRE", "SMOKE", "PERSON_TRAPPED"} <= codes(core.screen(FIRE))


# ---------------- validation

def test_rejects_quote_not_in_transcript():
    acc, rej = core.validate(MED, [{"field": "bleeding", "value": "heavy bleeding", "polarity": "present",
                                    "quote": "he is bleeding heavily from the head"}])
    assert not acc and rej[0]["reason"] == "quote is not in the transcript"


def test_rejects_polarity_flip():
    acc, rej = core.validate(MED, [{"field": "breathing", "value": "breathing", "polarity": "present",
                                    "quote": "He's not breathing"}])
    assert not acc and "polarity" in rej[0]["reason"]


def test_accepts_supported_negated_fact():
    acc, rej = core.validate(MED, [{"field": "breathing", "value": "not breathing", "polarity": "absent",
                                    "quote": "He's not breathing"}])
    assert acc and not rej


def test_rejects_quote_about_other_field():
    acc, rej = core.validate(MED, [{"field": "bleeding", "value": "none", "polarity": "absent",
                                    "quote": "he won't wake up"}])
    assert not acc and "does not mention" in rej[0]["reason"]


def test_location_value_must_be_in_quote():
    good = {"field": "location", "value": "5th and Pine", "polarity": "present", "quote": "bus stop on 5th and Pine"}
    bad = {**good, "value": "7th and Union"}
    assert core.validate(MED, [good])[0] and core.validate(MED, [bad])[1]


def test_missing_and_cards():
    m = {x["field"] for x in core.missing("medical", [])}
    assert {"location", "breathing"} <= m
    assert any(c["code"] == "NOT_BREATHING" for c in core.cards(core.screen(MED)))


# ---------------- API

def test_alerts_survive_ai_outage():
    r = client.post("/api/analyze", json={"transcript": MED, "simulate_ai_failure": True}).json()
    assert r["ai"]["status"] == "unavailable"
    assert "NOT_BREATHING" in codes(r["alerts"]) and r["cards"]
    assert r["incident_type"] == "medical" and r["missing"]


def test_real_failure_path_also_keeps_alerts(monkeypatch):
    def boom(_):
        raise TimeoutError("timed out")
    monkeypatch.setattr(appmod, "extract", boom)
    r = client.post("/api/analyze", json={"transcript": FIRE}).json()
    assert r["ai"]["status"] == "unavailable" and "PERSON_TRAPPED" in codes(r["alerts"])


def test_model_output_validated_and_revision_guard(monkeypatch):
    fake = {"incident_type": "medical", "facts": [
        {"field": "breathing", "value": "not breathing", "polarity": "absent", "quote": "He's not breathing"},
        {"field": "bleeding", "value": "heavy", "polarity": "present", "quote": "blood everywhere"},
    ]}
    monkeypatch.setattr(appmod, "extract", lambda t: (fake, {"model": "fake", "latency_ms": 1}))
    r = client.post("/api/analyze", json={"transcript": MED}).json()
    assert [f["field"] for f in r["accepted"]] == ["breathing"]
    assert r["rejected"][0]["field"] == "bleeding"
    iid, rev = r["incident_id"], r["revision"]
    assert client.post(f"/api/incidents/{iid}/review", json={"revision": rev, "field": "breathing", "decision": "confirm"}).status_code == 200
    r2 = client.post("/api/analyze", json={"transcript": MED + " Caller: He's breathing now.", "incident_id": iid}).json()
    assert r2["revision"] == rev + 1
    stale = client.post(f"/api/incidents/{iid}/review", json={"revision": rev, "field": "breathing", "decision": "confirm"})
    assert stale.status_code == 409


def test_health():
    h = client.get("/health").json()
    assert h["app"] == "ok" and "memory" in h["database"]


def test_cant_get_out_counts_as_trapped():
    acc, rej = core.validate(FIRE, [{"field": "people_trapped", "value": "can't get out", "polarity": "present",
                                     "quote": "she can't get out by herself"}])
    assert acc and not rej


def test_screen_endpoint_and_planted_fact(monkeypatch):
    s = client.post("/api/screen", json={"transcript": MED}).json()
    assert "NOT_BREATHING" in codes(s["alerts"]) and s["cards"]
    monkeypatch.setattr(appmod, "extract", lambda t: ({"incident_type": "medical", "facts": []}, {"model": "fake"}))
    r = client.post("/api/analyze", json={"transcript": MED, "plant_false_fact": True}).json()
    assert r["rejected"][0]["source"] == "planted test" and not r["accepted"]


def test_dispatcher_words_do_not_raise_alerts():
    t = "Dispatcher: Is anyone trapped inside? Is he bleeding? Caller: I don't know, I'm outside."
    assert codes(core.screen(t), include_negated=True) == set()
    # medical scenario: the dispatcher's "Is he awake?" is ignored, the caller's alerts remain
    assert {"NOT_BREATHING", "UNCONSCIOUS", "CHEST_PAIN"} <= codes(core.screen(MED))


def test_fact_quoting_dispatcher_is_rejected():
    t = "Dispatcher: Is anyone trapped inside? Caller: I don't know, I'm outside."
    acc, rej = core.validate(t, [{"field": "people_trapped", "value": "trapped inside", "polarity": "present",
                                  "quote": "Is anyone trapped inside"}])
    assert not acc and rej[0]["reason"] == "quote is the dispatcher's words, not the caller's"
    # unlabelled transcripts still count as caller speech
    acc, _ = core.validate("He's not breathing.", [{"field": "breathing", "value": "not breathing",
                                                     "polarity": "absent", "quote": "He's not breathing"}])
    assert acc
