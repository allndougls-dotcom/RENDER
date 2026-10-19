"""Independent split attestation gate. Never trust source labels alone.

Attestation is an externally reviewed JSON per ticker, not generated from Yahoo.
Coverage must span earliest financial period to latest downloaded price history.
Every event must match Yahoo date and ratio exactly, including future events.
"""
from datetime import date
from math import isclose

def verify_attestation(yahoo,attestation,asof,earliest_period):
    if not isinstance(attestation,dict):return False,"missing_independent_attestation"
    if attestation.get("review_status")!="confirmed":
        return False,"attestation_not_confirmed"
    if not attestation.get("independent_source_url") or not attestation.get("reviewer"):
        return False,"missing_source_or_reviewer"
    try:
        lower=date.fromisoformat(attestation["coverage_start"])
        upper=date.fromisoformat(attestation["coverage_end"])
        history_end=date.fromisoformat(yahoo["history_end"])
        cutoff=min(date.fromisoformat(earliest_period),date.fromisoformat(asof))
        if lower>cutoff or upper<history_end:return False,"insufficient_attestation_coverage"
        def normalized(events):
            events=events or []
            pairs=[(date.fromisoformat(e["date"]),float(e["ratio"])) for e in events]
            if any(r<=0 or isclose(r,1) for _,r in pairs):raise ValueError()
            if len({d for d,_ in pairs})!=len(pairs):raise ValueError()
            return sorted(pairs)
        declared=normalized(attestation["events"])
        observed=normalized(yahoo["events"])
        if len(declared)!=len(observed):return False,"split_event_count_mismatch"
        if any(d1!=d2 or not isclose(r1,r2,rel_tol=1e-7) for (d1,r1),(d2,r2) in zip(declared,observed)):
            return False,"split_events_disagree"
        return True,"independent_attestation_match"
    except (KeyError,TypeError,ValueError):
        return False,"invalid_attestation"
