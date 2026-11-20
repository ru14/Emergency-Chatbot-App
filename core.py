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
    ("BREATHING_UNCERTAIN", "Breathing status uncertain - confirm", "high",
     r"\b(don'?t think|do not think|not sure|can'?t tell|cannot tell|unsure)\b[^.?!]{0,40}\bbreath|\b(barely|hardly) breath"),
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


TRAPPED_DENIAL = re.compile(r"\b(no one|no-one|noone|nobody|nobody'?s|not|isn'?t|aren'?t)\b", re.IGNORECASE)
UNSURE = re.compile(r"\b(know|sure|think|tell|if|whether)\b", re.IGNORECASE)
CLAUSE_BREAK = re.compile(r"[,;]|\bbut\b|\balthough\b", re.IGNORECASE)
SPEAKER = re.compile(r"\b(Caller|Dispatcher|Call[- ]?taker|Operator)\s*:", re.IGNORECASE)


def caller_segments(transcript):
    """[(offset, text)] for what the caller said. Text after a Dispatcher/Operator/Call-taker label is left out.

    A transcript with no speaker labels is treated as all caller speech.
    """
    labels = list(SPEAKER.finditer(transcript))
    if not labels:
        return [(0, transcript)]
    segments = []
    if transcript[:labels[0].start()].strip():
        segments.append((0, transcript[:labels[0].start()]))  # unlabelled lead-in counts as caller
    for i, m in enumerate(labels):
        end = labels[i + 1].start() if i + 1 < len(labels) else len(transcript)
        if m.group(1).lower() == "caller":
            segments.append((m.end(), transcript[m.end():end]))
    return segments


def screen(transcript):
    """Keyword alerts over what the caller said. Runs before, and independent of, any model call.

    The dispatcher's own questions ("Is anyone trapped?") never raise an alert.
    It errs toward alerting: 'no blood' still shows the bleeding rule as negated rather than dropping it,
    so a human sees it. Each alert carries the exact matched text and its position in the transcript.
    """
    alerts = []
    for code, label, severity, pattern in ALERT_RULES:
        mentions = []  # every caller mention of this rule, each with its own negation
        for offset, text in caller_segments(transcript):
            for m in re.finditer(pattern, text, flags=re.IGNORECASE):
                # The clause before the match decides whether this mention is negated.
                start = max(text.rfind(".", 0, m.start()), text.rfind("?", 0, m.start())) + 1
                window = CLAUSE_BREAK.split(text[start:m.start()])[-1][-40:]  # "No, but his chest hurts" -> "his "
                if code == "PERSON_TRAPPED":
                    # "No one is trapped" denies; "she can't get out" / "I don't know if anyone is inside" do not
                    is_negated = bool(TRAPPED_DENIAL.search(window)) and not UNSURE.search(window)
                elif code in {"NOT_BREATHING", "BREATHING_UNCERTAIN", "UNCONSCIOUS"}:
                    is_negated = False  # the rule itself is the negative finding
                else:
                    is_negated = negated(window)
                mentions.append((offset, m, is_negated))
        if not mentions:
            continue
        positive = [x for x in mentions if not x[2]]
        offset, m, is_negated = (positive or mentions)[0]  # any real mention outweighs a denial
        # a correction: the caller first denied, then mentioned it ("No chest pain. Now his chest hurts.")
        first_pos = mentions.index(positive[0]) if positive else -1
        conflict = first_pos > 0 and any(x[2] for x in mentions[:first_pos])
        alerts.append({
            "code": code, "label": label + (" (conflicting mentions - confirm)" if conflict else ""),
            "severity": "info" if is_negated else severity,
            "match": m.group(0), "start": offset + m.start(), "negated": is_negated, "conflict": conflict,
        })
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


class InvalidModelOutput(ValueError):
    """The model answered, but its reply does not have the shape EXTRACTION_SCHEMA requires."""


def check_extraction(raw):
    """Server-side check of the model reply against EXTRACTION_SCHEMA. Never trusts the model's own formatting.

    Returns (incident_type, facts, malformed, notes):
      facts      well-formed fact objects, still to be checked against the transcript by validate()
      malformed  fact entries that break the schema, each with a rejection reason
      notes      problems that were corrected (e.g. an incident type outside the allowed list)
    Raises InvalidModelOutput when the reply as a whole is unusable.
    """
    if not isinstance(raw, dict):
        raise InvalidModelOutput("reply is not a JSON object")
    facts = raw.get("facts")
    if not isinstance(facts, list):
        raise InvalidModelOutput("'facts' is not a list")
    notes = []
    incident_type = raw.get("incident_type")
    allowed_types = EXTRACTION_SCHEMA["properties"]["incident_type"]["enum"]
    if incident_type not in allowed_types:
        notes.append(f"incident type {str(incident_type)[:30]!r} is not allowed; treated as unknown")
        incident_type = "unknown"
    item_schema = EXTRACTION_SCHEMA["properties"]["facts"]["items"]
    good, malformed = [], []
    for item in facts:
        problem = None
        if not isinstance(item, dict):
            problem = "fact is not an object"
        else:
            for key in item_schema["required"]:
                if not isinstance(item.get(key), str):
                    problem = f"{key} is not text"
                    break
            else:
                for key, spec in item_schema["properties"].items():
                    if "enum" in spec and item[key] not in spec["enum"]:
                        problem = f"{key} {item[key][:30]!r} is not allowed"
                        break
        if problem:
            d = item if isinstance(item, dict) else {}
            malformed.append({"field": str(d.get("field", "?"))[:40], "value": str(d.get("value", ""))[:80],
                              "polarity": str(d.get("polarity", ""))[:20], "quote": str(d.get("quote", ""))[:200],
                              "reason": f"malformed model output: {problem}"})
        else:
            good.append({k: item[k] for k in item_schema["required"]})
    return incident_type, good, malformed, notes


# ---------------------------------------------------------------- evidence and polarity validation

def _clause_before(segment_text, nq):
    """Normalized words of the caller's clause that come before the quote, within its sentence ("" if none)."""
    for sentence in re.split(r"[.?!]", segment_text):
        marked = _norm(CLAUSE_BREAK.sub(" zzclause ", sentence)).split()
        plain = " ".join(w for w in marked if w != "zzclause")
        i = plain.find(nq)
        if i < 0:
            continue
        k, before, n = len(plain[:i].split()), [], 0
        for w in marked:
            if n == k:
                break
            if w == "zzclause":
                before = []
            else:
                before.append(w)
                n += 1
        return " ".join(before)
    return ""


def validate(transcript, facts):
    """Split model facts into accepted and rejected, each rejected one with its reason."""
    accepted, rejected = [], []
    norm_transcript = _norm(transcript)
    caller_texts = [text for _, text in caller_segments(transcript)]
    norm_caller = [_norm(text) for text in caller_texts]
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
        elif not any(nq in seg for seg in norm_caller):
            reason = "quote is the dispatcher's words, not the caller's"
        else:
            keywords = FIELDS[field][1]
            if keywords is None:
                if _norm(value) and _norm(value) not in nq and not any(
                        tok in nq.split() for tok in _norm(value).split() if tok.isdigit() or len(tok) > 3):
                    reason = "value does not appear in the quote"
            elif not any(k in nq for k in keywords):
                reason = f"quote does not mention {field.replace('_', ' ')}"
            if reason is None and keywords is not None:
                # "he's breathing" cut from "I don't think he's breathing" keeps the caller's negation
                context = next((_clause_before(t, nq) for t, nt in zip(caller_texts, norm_caller) if nq in nt), "")
                neg = negated(quote, field) or negated(context, field)
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

DISCLAIMER = "Demo-only; not clinically approved. Follow your agency's approved protocol."
CARD_VERSION = "demo-0.1"
NO_RULE_MATCHED = "No rule matched. This is not an assessment of safety."
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
        out.append({"id": f"PC-{a['code']}", "version": CARD_VERSION, "code": a["code"], "title": title,
                    "steps": steps, "disclaimer": DISCLAIMER})
    return out
