"""Deterministic parts of the copilot. Nothing in this file calls a model.

- screen():     raw-transcript risk alerts that still work when inference is down
- validate():   accepts a model-extracted fact only if its quote is in the transcript,
                mentions the field, and agrees in polarity (negated vs. affirmed)
- missing():    which required facts no accepted fact covers, with the question to ask
- cards():      fixed protocol cards chosen by rules, never written by the model
"""
import re

# ---------------------------------------------------------------- scenarios (synthetic)

SCENARIOS = {
    "medical": {
        "title": "Medical: collapsed man at a bus stop",
        "transcript": (
            "Caller: 911? My neighbor just collapsed at the bus stop on 5th and Pine. "
            "He's not breathing, I don't think he's breathing. "
            "Dispatcher: Is he awake? "
            "Caller: No, he's unconscious, he won't wake up. He's maybe sixty. "
            "Caller: There's no blood that I can see. He said his chest hurt right before he fell."
        ),
    },
    "fire": {
        "title": "Fire: kitchen fire in an apartment",
        "transcript": (
            "Caller: There's a fire in my kitchen, apartment 3B at 1200 Lake Street. "
            "There's a lot of smoke in the hallway. "
            "Caller: I'm outside now but my grandmother is still inside, she can't get out by herself. "
            "Caller: I don't see any flames in the bedroom yet."
        ),
    },
}

# ---------------------------------------------------------------- negation

NEGATION_WORDS = {
    "no", "not", "never", "none", "without", "nobody", "nothing", "cannot", "can't", "cant",
    "isn't", "isnt", "wasn't", "wasnt", "won't", "wont", "don't", "dont", "doesn't", "doesnt",
    "didn't", "didnt", "aren't", "arent", "haven't", "hasn't", "stopped", "unable",
}
# Words that carry their own negative meaning for a field.
NEGATIVE_FORMS = {"unconscious", "unresponsive", "breathless"}


def _norm(text):
    text = text.lower().replace("’", "'").replace("‘", "'")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return " ".join(text.split())


def _tokens(text):
    return _norm(text).split()


# Phrases whose negation word affirms the field rather than denying it ("can't get out" = trapped).
AFFIRMING_PHRASES = {
    "people_trapped": ["can't get out", "cant get out", "cannot get out", "unable to get out", "can't leave",
                       "won't come out", "not out yet", "not able to get out"],
    "breathing": ["can't stop breathing hard"],
}


def negated(text, field=None):
    """True if the text carries a negation cue, ignoring phrases that affirm this field."""
    norm = _norm(text)
    for phrase in AFFIRMING_PHRASES.get(field, []):
        norm = norm.replace(_norm(phrase), " ")
    return any(t in NEGATION_WORDS or t in NEGATIVE_FORMS for t in norm.split())


# ---------------------------------------------------------------- independent risk screen

ALERT_RULES = [
    # (code, label, severity, regex)
    ("NOT_BREATHING", "Possible not breathing", "critical",
     r"\b(not|isn'?t|stopped|no longer|can'?t stop) (breathing|breath)|\bno pulse\b|\bnot breathing\b"),
    ("UNCONSCIOUS", "Possibly unconscious", "critical",
     r"\b(unconscious|unresponsive|passed out|won'?t wake|not waking|not responding)\b"),
    ("CHEST_PAIN", "Chest pain mentioned", "high", r"\bchest (pain|hurt|hurts|hurting|pressure)\b"),
    ("SEVERE_BLEEDING", "Bleeding mentioned", "high", r"\b(bleeding|blood everywhere|lots of blood)\b"),
    ("PERSON_TRAPPED", "Person may be trapped", "critical",
     r"\b(trapped|stuck inside|still inside|can'?t get out|cannot get out)\b"),
    ("FIRE", "Fire mentioned", "high", r"\b(fire|flames?|burning)\b"),
    ("SMOKE", "Smoke mentioned", "high", r"\bsmoke\b"),
    ("WEAPON", "Weapon mentioned", "critical", r"\b(gun|knife|weapon|shot|stabbed)\b"),
    ("CHILD", "Child involved", "high", r"\b(baby|infant|child|toddler|my son|my daughter)\b"),
]


def screen(transcript):
    """Keyword alerts over the raw transcript. Runs before, and independent of, any model call.

    It errs toward alerting: 'no blood' still shows the bleeding rule as negated rather than dropping it,
    so a human sees it. Each alert carries the exact matched text and its position.
    """
    alerts = []
    for code, label, severity, pattern in ALERT_RULES:
        for m in re.finditer(pattern, transcript, flags=re.IGNORECASE):
            # The sentence around the match decides whether this is a negated mention.
            start = max(transcript.rfind(".", 0, m.start()), transcript.rfind("?", 0, m.start())) + 1
            window = transcript[start:m.start()][-40:]
            alert_is_itself_negative = code in {"NOT_BREATHING", "UNCONSCIOUS", "PERSON_TRAPPED"}
            is_negated = (not alert_is_itself_negative) and negated(window)
            alerts.append({
                "code": code, "label": label, "severity": "info" if is_negated else severity,
                "match": m.group(0), "start": m.start(), "negated": is_negated,
            })
            break  # one alert per rule is enough for the dispatcher
    order = {"critical": 0, "high": 1, "info": 2}
    return sorted(alerts, key=lambda a: (order[a["severity"]], a["start"]))


# ---------------------------------------------------------------- fields the model may extract

FIELDS = {
    # field: (question to ask when missing, keywords a supporting quote must contain; None = value-checked)
    "location": ("What is the exact address or nearest cross street?", None),
    "patient_age": ("About how old is the patient?", None),
    "conscious": ("Is the patient awake and able to talk to you?",
                  ["awake", "conscious", "unconscious", "responsive", "unresponsive", "wake", "talking", "alert"]),
    "breathing": ("Is the patient breathing normally?", ["breathing", "breath", "breathe", "pulse"]),
    "bleeding": ("Is there any serious bleeding?", ["blood", "bleeding", "bleed"]),
    "chest_pain": ("Does the patient have chest pain or pressure?", ["chest"]),
    "fire_visible": ("Can you see flames, and where?", ["fire", "flame", "flames", "burning"]),
    "smoke": ("Is there smoke, and where?", ["smoke", "smoky"]),
    "people_trapped": ("Is anyone still inside or unable to get out?",
                       ["inside", "trapped", "get out", "stuck", "everyone out", "nobody", "no one"]),
    "caller_safe": ("Are you in a safe place away from the danger?", ["outside", "safe", "out of", "left"]),
    "hazards": ("Are there gas tanks, chemicals, or other hazards?", ["gas", "propane", "chemical", "tank", "oxygen"]),
}
REQUIRED = {
    "medical": ["location", "patient_age", "conscious", "breathing", "bleeding"],
    "fire": ["location", "people_trapped", "fire_visible", "smoke", "caller_safe", "hazards"],
    "other": ["location"],
    "unknown": ["location"],
}

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "incident_type": {"type": "string", "enum": ["medical", "fire", "other", "unknown"]},
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": list(FIELDS)},
                    "value": {"type": "string"},
                    "polarity": {"type": "string", "enum": ["present", "absent", "unknown"]},
                    "quote": {"type": "string"},
                },
                "required": ["field", "value", "polarity", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["incident_type", "facts"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- evidence and polarity validation

def validate(transcript, facts):
    """Split model facts into accepted and rejected, each rejected one with its reason."""
    accepted, rejected = [], []
    norm_transcript = _norm(transcript)
    for fact in facts:
        field = fact.get("field", "")
        quote = (fact.get("quote") or "").strip()
        value = (fact.get("value") or "").strip()
        polarity = fact.get("polarity", "unknown")
        reason = None
        nq = _norm(quote)
        if field not in FIELDS:
            reason = f"unknown field {field!r}"
        elif len(nq) < 3:
            reason = "no supporting quote"
        elif nq not in norm_transcript:
            reason = "quote is not in the transcript"
        else:
            keywords = FIELDS[field][1]
            if keywords is None:
                if _norm(value) and _norm(value) not in nq and not any(
                        tok in nq.split() for tok in _norm(value).split() if tok.isdigit() or len(tok) > 3):
                    reason = "value does not appear in the quote"
            elif not any(k in nq for k in keywords):
                reason = f"quote does not mention {field.replace('_', ' ')}"
            if reason is None and keywords is not None:
                neg = negated(quote, field)
                if polarity == "present" and neg:
                    reason = "polarity conflict: quote is negated but fact says present"
                elif polarity == "absent" and not neg:
                    reason = "polarity conflict: quote is affirmative but fact says absent"
        item = {"field": field, "value": value, "polarity": polarity, "quote": quote}
        if fact.get("source"):
            item["source"] = fact["source"]
        (rejected if reason else accepted).append({**item, "reason": reason} if reason else item)
    return accepted, rejected


def missing(incident_type, accepted):
    covered = {f["field"] for f in accepted if f["polarity"] != "unknown"}
    return [{"field": f, "ask": FIELDS[f][0]} for f in REQUIRED.get(incident_type, REQUIRED["unknown"])
            if f not in covered]


# ---------------------------------------------------------------- deterministic protocol cards

DISCLAIMER = "Demo card for a synthetic scenario. Not clinical guidance. Follow your agency's approved protocol."
CARDS = {
    "NOT_BREATHING": ("Possible cardiac / respiratory arrest",
                      ["Dispatch highest-priority medical response now.",
                       "Confirm breathing: ask the caller to look for chest rise for 10 seconds.",
                       "Start your agency's telephone CPR instructions if not breathing normally.",
                       "Ask whether an AED is nearby."]),
    "UNCONSCIOUS": ("Unconscious person",
                    ["Confirm whether the patient is breathing normally.",
                     "Keep the caller on the line and with the patient."]),
    "CHEST_PAIN": ("Chest pain",
                   ["Dispatch medical response per your chest-pain determinant.",
                    "Ask about age, breathing difficulty, and cardiac history."]),
    "SEVERE_BLEEDING": ("Bleeding",
                        ["Ask where the bleeding is and how heavy it is.",
                         "Follow your agency's bleeding-control instructions."]),
    "PERSON_TRAPPED": ("Person may be trapped",
                       ["Tell responding units someone may still be inside, and where.",
                        "Do not tell the caller to go back inside."]),
    "FIRE": ("Structure fire",
             ["Dispatch fire response per your structure-fire determinant.",
              "Tell the caller to get out, stay out, and meet responders outside."]),
    "SMOKE": ("Smoke", ["Ask where the smoke is and whether exits are blocked."]),
    "WEAPON": ("Weapon involved", ["Notify law enforcement and stage medical units per policy.",
                                   "Ask whether the caller is safe."]),
    "CHILD": ("Child involved", ["Use your agency's pediatric protocol and ask the child's age."]),
}


def cards(alerts):
    """One card per non-negated alert, from the fixed table above."""
    out = []
    for a in alerts:
        if a["negated"] or a["code"] not in CARDS:
            continue
        title, steps = CARDS[a["code"]]
        out.append({"code": a["code"], "title": title, "steps": steps, "disclaimer": DISCLAIMER})
    return out
