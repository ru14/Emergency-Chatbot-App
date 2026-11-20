"""Offline benchmark: baseline vs the copilot's screening and fact validation.

    python3 eval/benchmark.py

No model, network or database. Uses synthetic, hand-labelled cases in eval/cases.json.
This is a small demo benchmark, not a clinical evaluation.

Baseline screening: the same keyword patterns matched anywhere in the raw transcript (both speakers),
with no negation handling and without the uncertain-breathing rule - what a first version does.
Baseline facts: trust every fact the model returns.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import core  # noqa: E402

CATEGORY = {"NOT_BREATHING": "BREATHING", "BREATHING_UNCERTAIN": "BREATHING", "UNCONSCIOUS": "UNCONSCIOUS",
            "CHEST_PAIN": "CHEST_PAIN", "SEVERE_BLEEDING": "BLEEDING", "PERSON_TRAPPED": "TRAPPED",
            "FIRE": "FIRE", "SMOKE": "SMOKE"}


def baseline_screen(t):
    return {CATEGORY[code] for code, _, _, pattern in core.ALERT_RULES
            if code in CATEGORY and code != "BREATHING_UNCERTAIN" and re.search(pattern, t, re.IGNORECASE)}


def ours_screen(t):
    return {CATEGORY[a["code"]] for a in core.screen(t) if not a["negated"] and a["code"] in CATEGORY}


def score(cases, fn):
    tp = fp = fn_ = 0
    false_alarm_cases, missed_cases = [], []
    for c in cases:
        got, exp = fn(c["t"]), set(c["expected"])
        tp += len(got & exp)
        fp += len(got - exp)
        fn_ += len(exp - got)
        if got - exp:
            false_alarm_cases.append(c["id"])
        if exp - got:
            missed_cases.append(c["id"])
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn_) if tp + fn_ else 1.0
    return {"precision": precision, "recall": recall, "false_alarms": fp, "missed": fn_,
            "false_alarm_cases": false_alarm_cases, "missed_cases": missed_cases}


def fact_scores(cases):
    base = {"accepted_unsupported": 0, "rejected_supported": 0}
    ours = {"accepted_unsupported": 0, "rejected_supported": 0}
    wrong = []
    for c in cases:
        # baseline: everything accepted
        if not c["supported"]:
            base["accepted_unsupported"] += 1
        _, facts, malformed, _ = core.check_extraction({"incident_type": "unknown", "facts": [c["fact"]]})
        accepted, _ = core.validate(c["t"], facts)
        ok = bool(accepted) and not malformed
        if ok and not c["supported"]:
            ours["accepted_unsupported"] += 1
            wrong.append(c["id"])
        if not ok and c["supported"]:
            ours["rejected_supported"] += 1
            wrong.append(c["id"])
    return base, ours, wrong


def main():
    data = json.loads((ROOT / "eval" / "cases.json").read_text())
    s, f = data["screening"], data["facts"]
    b, o = score(s, baseline_screen), score(s, ours_screen)
    fb, fo, fwrong = fact_scores(f)
    n_unsup = sum(1 for c in f if not c["supported"])
    n_sup = len(f) - n_unsup
    pct = lambda x: f"{x * 100:.0f}%"
    print(f"Risk screening - {len(s)} synthetic calls, {sum(len(c['expected']) for c in s)} expected alerts")
    print(f"  {'':28}{'Baseline':>12}{'Ours':>12}")
    print(f"  {'Precision':28}{pct(b['precision']):>12}{pct(o['precision']):>12}")
    print(f"  {'Recall (life-threats caught)':28}{pct(b['recall']):>12}{pct(o['recall']):>12}")
    print(f"  {'False alarms':28}{b['false_alarms']:>12}{o['false_alarms']:>12}")
    print(f"  {'Missed alerts':28}{b['missed']:>12}{o['missed']:>12}")
    print(f"  baseline false alarms in {b['false_alarm_cases']}, misses in {b['missed_cases']}")
    print(f"  ours     false alarms in {o['false_alarm_cases']}, misses in {o['missed_cases']}")
    print()
    print(f"AI fact validation - {len(f)} model facts ({n_unsup} unsupported, {n_sup} supported)")
    print(f"  {'':28}{'Baseline':>12}{'Ours':>12}")
    print(f"  {'Unsupported facts accepted':28}{fb['accepted_unsupported']:>12}{fo['accepted_unsupported']:>12}")
    print(f"  {'Supported facts rejected':28}{fb['rejected_supported']:>12}{fo['rejected_supported']:>12}")
    print(f"  ours wrong on {fwrong}")


if __name__ == "__main__":
    main()
