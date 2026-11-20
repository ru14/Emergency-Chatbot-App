"""Run:  uv run --with pytest --with httpx --with fastapi --with openai --with "psycopg[binary]" --with python-dotenv pytest -q
No network and no key needed: the model call is replaced with fixed outputs."""
import json
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
appmod.APP_PASSWORD = ""
appmod.RATE_LIMIT = 10_000  # the rate-limit test lowers this itself  # a local .env may turn the login on; these tests exercise the API itself
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


# ---------------- screening and validation review (Prompt 3)

def test_quote_cut_from_negated_sentence_is_rejected():
    # The caller's sentence is negated; a fact quoting only its tail must not flip it to "present".
    t = "Caller: I don't think he's breathing."
    acc, rej = core.validate(t, [{"field": "breathing", "value": "breathing", "polarity": "present",
                                  "quote": "he's breathing"}])
    assert not acc and "polarity" in rej[0]["reason"]


def test_no_then_symptom_still_alerts():
    # "No," answers an earlier question; it must not cancel the symptom that follows "but".
    alerts = {a["code"]: a for a in core.screen("Caller: No, but his chest hurts a lot.")}
    assert "CHEST_PAIN" in alerts and not alerts["CHEST_PAIN"]["negated"]


def test_uncertain_breathing_raises_alert():
    # Conservative screening: uncertainty about breathing is flagged for the dispatcher to confirm.
    for t in ["Caller: I don't think he's breathing.",
              "Caller: I can't tell if he's breathing.",
              "Caller: He's barely breathing."]:
        found = codes(core.screen(t))
        assert found & {"NOT_BREATHING", "BREATHING_UNCERTAIN"}, t


def test_negation_cases_locked_in():
    def alert(t, code):
        return {a["code"]: a for a in core.screen(t)}.get(code)
    assert alert("Caller: He has no chest pain.", "CHEST_PAIN")["negated"]
    assert not alert("Caller: He's not breathing.", "NOT_BREATHING")["negated"]
    assert alert("Caller: He isn't bleeding.", "SEVERE_BLEEDING")["negated"]
    assert core.screen("Dispatcher: Is he breathing? Is there chest pain? Caller: Yes.") == []


def test_unknown_polarity_stays_missing():
    acc, _ = core.validate("Caller: I'm not sure if he's breathing.",
                           [{"field": "breathing", "value": "unsure", "polarity": "unknown",
                             "quote": "I'm not sure if he's breathing"}])
    assert acc and "breathing" in {m["field"] for m in core.missing("medical", acc)}


# ---------------- model reply checks (Prompt 4)

import types  # noqa: E402


def _fake_reply(monkeypatch, content, finish="stop"):
    msg = types.SimpleNamespace(content=content)
    resp = types.SimpleNamespace(choices=[types.SimpleNamespace(finish_reason=finish, message=msg)],
                                 usage=None, model="qwen3-8-27b", id="chatcmpl-test")
    fake = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **k: resp)))
    monkeypatch.setattr(appmod, "_client", fake)


def test_invalid_json_is_invalid_output_not_unavailable(monkeypatch):
    _fake_reply(monkeypatch, "not json at all")
    r = client.post("/api/analyze", json={"transcript": MED}).json()
    assert r["ai"]["status"] == "invalid_output" and r["ai"]["error"] == "reply is not valid JSON"
    assert r["ai"]["request_id"] == "chatcmpl-test" and not r["accepted"]
    assert "NOT_BREATHING" in codes(r["alerts"]) and r["cards"]


def test_cut_off_reply_is_invalid_output(monkeypatch):
    _fake_reply(monkeypatch, '{"incident_type":"medical","facts":[', finish="length")
    r = client.post("/api/analyze", json={"transcript": MED}).json()
    assert r["ai"]["status"] == "invalid_output" and "cut off" in r["ai"]["error"]
    assert "NOT_BREATHING" in codes(r["alerts"])


def test_malformed_fact_is_rejected_with_reason(monkeypatch):
    _fake_reply(monkeypatch, json.dumps({"incident_type": "medical", "facts": [
        "breathing absent",
        {"field": "breathing", "value": "not breathing", "polarity": "absent", "quote": 5},
        {"field": "breathing", "value": "not breathing", "polarity": "maybe", "quote": "He's not breathing"},
        {"field": "breathing", "value": "not breathing", "polarity": "absent", "quote": "He's not breathing"}]}))
    r = client.post("/api/analyze", json={"transcript": MED}).json()
    assert r["ai"]["status"] == "ok"
    assert [f["field"] for f in r["accepted"]] == ["breathing"]
    reasons = [f["reason"] for f in r["rejected"]]
    assert reasons == ["malformed model output: fact is not an object", "malformed model output: quote is not text",
                       "malformed model output: polarity 'maybe' is not allowed"]


def test_unknown_incident_type_is_corrected(monkeypatch):
    _fake_reply(monkeypatch, json.dumps({"incident_type": "banana", "facts": []}))
    r = client.post("/api/analyze", json={"transcript": FIRE}).json()
    assert r["ai"]["status"] == "ok" and r["incident_type"] == "fire"  # falls back to the screen's view
    assert "not allowed" in r["ai"]["notes"][0]
    _fake_reply(monkeypatch, json.dumps({"incident_type": "medical", "facts": "none"}))
    r = client.post("/api/analyze", json={"transcript": MED}).json()
    assert r["ai"]["status"] == "invalid_output" and r["ai"]["error"] == "'facts' is not a list"


# ---------------- demo gaps (cards, rate limit)

def test_cards_carry_id_version_and_not_approved_label():
    cs = core.cards(core.screen(MED))
    assert cs and all(c["id"] == "PC-" + c["code"] and c["version"] == core.CARD_VERSION for c in cs)
    assert all("not clinically approved" in c["disclaimer"] for c in cs)
    assert core.cards(core.screen("Caller: I'd like to report a noise complaint.")) == []
    assert core.NO_RULE_MATCHED == "No rule matched. This is not an assessment of safety."


def test_analyze_is_rate_limited(monkeypatch):
    monkeypatch.setattr(appmod, "RATE_LIMIT", 2)
    monkeypatch.setattr(appmod, "_recent_calls", {})
    body = {"transcript": MED, "simulate_ai_failure": True}
    assert [client.post("/api/analyze", json=body).status_code for _ in range(3)] == [200, 200, 429]
    assert client.post("/api/screen", json={"transcript": MED}).status_code == 200  # screening is never limited


# ---------------- external audit fixes

def test_trapped_denial_is_not_a_positive_alert():
    a = {x["code"]: x for x in core.screen("Caller: No one is trapped inside.")}
    assert a["PERSON_TRAPPED"]["negated"] and core.cards(list(a.values())) == []
    for t in ["Caller: She can't get out by herself.", "Caller: My grandmother is still inside.",
              "Caller: I don't know if anyone is still inside."]:
        assert "PERSON_TRAPPED" in codes(core.screen(t)), t


def test_later_positive_mention_wins_and_is_flagged():
    a = {x["code"]: x for x in core.screen("Caller: No chest pain. Now his chest hurts.")}
    assert not a["CHEST_PAIN"]["negated"] and a["CHEST_PAIN"]["conflict"]
    assert "conflicting" in a["CHEST_PAIN"]["label"]
    assert any(c["code"] == "CHEST_PAIN" for c in core.cards(list(a.values())))


def test_whitespace_transcript_rejected():
    assert client.post("/api/analyze", json={"transcript": "   "}).status_code == 400
    assert client.post("/api/screen", json={"transcript": "  \n "}).status_code == 400
