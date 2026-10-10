"""Deterministic decision: ground the evidence, apply three-valued rules, ASR-quality threshold, explain."""
import math

LABEL_RANK = {"no_alert": 0, "review": 1, "alarm": 2}
STATES = ("true", "false", "unknown")


def _normalise_whitespace(text):
    """Compare quotations exactly apart from whitespace and letter case.

    Punctuation is deliberately retained.  A quotation is evidence only when it
    can be found in the cited ASR text, not when it merely resembles it.
    """
    return " ".join(text.casefold().split()) if isinstance(text, str) else ""


def quality(seg):
    """ASR-quality heuristic exp(avg_logprob); None when the source gives no score (a script, some ASR APIs)."""
    lp = seg.get("avg_logprob")
    return round(math.exp(lp), 3) if lp is not None else None


def actor_for_event(raw, grounder):
    """Normalize one extracted actor without inventing a role for legacy results."""
    supplied = isinstance(raw.get("actor"), dict)
    actor = raw.get("actor") if supplied else {}
    role, status = actor.get("role"), actor.get("status")
    ids = actor.get("segment_ids")
    valid_ids = isinstance(ids, list) and all(isinstance(sid, str) and grounder.resolve(sid) for sid in ids)
    inferred = role in ("customer", "advisor") and status == "inferred" and bool(ids) and valid_ids
    if inferred:
        return {"role": role, "status": "inferred", "segment_ids": [grounder.resolve(sid) for sid in ids],
                "supplied": True}
    return {"role": "unknown", "status": "unknown", "segment_ids": [], "supplied": supplied}


def role_open_question(required):
    return (f"Was the person making the trade request the {required}?" if required == "customer"
            else f"Was the person disclosing the information the {required}?")


class Grounder:
    """Ground a quotation exactly in its cited segment or its immediate successor.

    An LLM supplies only a candidate segment ID and quote.  This class derives
    timestamps and segment spans exclusively from the canonical ASR segments.
    It intentionally has no fuzzy alignment, ID aliases or backward-neighbour
    fallback: those conveniences could make invented or misattributed evidence
    appear valid.
    """

    def __init__(self, segments):
        self.order = [s["id"] for s in segments]
        self.pos = {sid: i for i, sid in enumerate(self.order)}
        self.by_id = {s["id"]: s for s in segments}

    def resolve(self, sid):
        """Return an existing canonical segment ID; never guess an alias."""
        return sid if isinstance(sid, str) and sid in self.pos else None

    def _ref(self, ids, quote, match):
        segs = [self.by_id[x] for x in ids]
        qs = [quality(s) for s in segs]
        return {"segment_ids": ids, "quote": quote, "match": match, "start": segs[0].get("start"),
                "end": segs[-1].get("end"), "quality": None if None in qs else min(qs)}

    def ground(self, ref):
        if not isinstance(ref, dict):
            return None, "evidence reference must be an object"
        raw_id = ref.get("segment_id")
        sid = self.resolve(raw_id)
        if sid is None:
            return None, f"segment ID {raw_id!r} does not exist"
        quote = _normalise_whitespace(ref.get("quote"))
        if not quote:
            return None, "quote is missing or empty"

        current = _normalise_whitespace(self.by_id[sid]["text"])
        if quote in current:
            return self._ref([sid], ref["quote"], "exact"), None

        # A cross-segment quote must start in the cited segment and end in its
        # immediate successor.  Quotes found solely in a neighbour are invalid.
        i = self.pos[sid]
        if i + 1 < len(self.order):
            next_id = self.order[i + 1]
            joined = current + " " + _normalise_whitespace(self.by_id[next_id]["text"])
            start = joined.find(quote)
            if start >= 0 and start < len(current) and start + len(quote) > len(current):
                return self._ref([sid, next_id], ref["quote"], "exact"), None
        return None, "quote does not exactly match the cited segment or its immediate successor"

    def ground_all(self, refs, where):
        kept = []
        issues = []
        if not isinstance(refs, list):
            return kept, [f"{where}: evidence must be a list"]
        for ref in refs:
            g, problem = self.ground(ref)
            if g:
                kept.append(g)
            else:
                issues.append(f"{where}: grounding failed: {problem}")
        return kept, issues


def _summary(result):
    names = lambda state: ", ".join(n for n, c in result["conditions"].items() if c["state"] == state)
    if result["status"] == "not_applicable":
        return f"Number classified as {result.get('object_type', 'unknown')}; numbers never raise an alarm."
    if result["status"] == "absent":
        return f"Excluded: {names('false')} established as false."
    if result["status"] == "undetermined":
        return f"Not established: {names('unknown')}."
    return "All conditions established."


def evaluate_event(family, raw, grounder, policies, threshold):
    fam = policies["families"][family]
    raw = raw if isinstance(raw, dict) else {}
    issues = []
    conditions = {}
    actor = actor_for_event(raw, grounder)
    role_requirements = fam.get("role_requirements", {})
    for name in fam["conditions"]:
        c = (raw.get("conditions") or {}).get(name)
        if not isinstance(c, dict) or c.get("state") not in STATES:
            issues.append(f"{name}: missing or invalid in extraction")
            conditions[name] = {"state": "unknown", "evidence": [], "downgraded": True,
                                "uncertainty": [], "grounding_issues": [f"{name}: missing or invalid in extraction"]}
            continue
        refs, grounding_issues = grounder.ground_all(c.get("evidence"), name)
        issues += grounding_issues
        state, downgraded = c["state"], False
        uncertainty = []
        if state in ("true", "false") and not refs:
            state, downgraded = "unknown", True
            issue = f"{name}: '{c['state']}' has no valid grounded evidence, set to unknown"
            issues.append(issue)
            grounding_issues.append(issue)
        required_role = role_requirements.get(name)
        # A valid explicit exclusion remains an exclusion.  Otherwise, only the
        # predicates that materially depend on the actor are made uncertain.
        if actor["supplied"] and required_role and state != "false" and (actor["role"] != required_role or actor["status"] != "inferred"):
            state = "unknown"
            uncertainty.append("actor_role_unknown")
            issues.append(f"{name}: required {required_role} role is not established, set to unknown")
        conditions[name] = {"state": state, "evidence": refs, "downgraded": downgraded,
                            "uncertainty": uncertainty, "grounding_issues": grounding_issues}

    event_refs, event_grounding_issues = grounder.ground_all(raw.get("evidence"), "event") if not fam["conditions"] else ([], [])
    issues += event_grounding_issues
    result = {"family": family, "title": fam["title"], "actor": actor, "conditions": conditions,
              "evidence": event_refs,
              "issues": issues, "reason": None}
    if raw.get("object_type"):
        result["object_type"] = raw["object_type"]
    states = [c["state"] for c in conditions.values()]
    if not fam.get("can_alarm", True):
        result.update(status="not_applicable", label="no_alert")
    elif "false" in states:
        result.update(status="absent", label="no_alert")
    elif "unknown" in states:
        technical = any(c["downgraded"] for c in conditions.values() if c["state"] == "unknown")
        actor_unknown = any("actor_role_unknown" in c["uncertainty"] for c in conditions.values()
                            if c["state"] == "unknown")
        questions = [fam["conditions"][n] for n, c in conditions.items() if c["state"] == "unknown"]
        questions += [role_open_question(role_requirements[n]) for n, c in conditions.items()
                      if "actor_role_unknown" in c["uncertainty"]]
        result.update(status="undetermined", label="review",
                      reason="technical_uncertainty" if technical else "actor_role_unknown" if actor_unknown else "missing_policy_fact",
                      open_questions=questions)
    else:
        best = {}
        for name, c in conditions.items():
            known = [r["quality"] for r in c["evidence"] if r["quality"] is not None]
            best[name] = max(known) if known else None
        result["quality"] = best
        result["quality_unavailable"] = [n for n, q in best.items() if q is None]
        low = [n for n, q in best.items() if q is not None and q < threshold]
        if low:
            result.update(status="present", label="review", reason="below_escalation_threshold", low_quality=low)
        else:
            result.update(status="present", label="alarm")
    result["summary"] = _summary(result)
    return result


def decide(extraction, segments, policies, threshold=None, error=None):
    """Turn one extraction into the canonical call result. error: extraction failed (model or JSON)."""
    threshold = policies["escalation"]["min_asr_quality"] if threshold is None else threshold
    base = {"threshold": threshold, "meta": (extraction or {}).get("_meta", {}),
            "policies_version": policies.get("version")}
    if error:
        return {**base, "status": "failed", "label": "review", "reasons": ["technical_uncertainty"], "events": [],
                "issues": [], "disabled_families": [], "error": str(error)}
    grounder = Grounder(segments)
    families = (extraction or {}).get("families")
    families = families if isinstance(families, dict) else {}
    disabled = [name for name, fam in policies["families"].items() if not fam.get("enabled", True)]
    issues, events = [], []
    for name, fam in policies["families"].items():
        if not fam.get("enabled", True):
            continue  # a bank switched this check off; absent by configuration, not a technical gap
        out = families.get(name)
        if not isinstance(out, dict) or not isinstance(out.get("events"), list):
            issues.append(f"family '{name}' missing from the extraction")
            continue
        events += [evaluate_event(name, raw, grounder, policies, threshold) for raw in out["events"]]
    label = max((e["label"] for e in events), key=LABEL_RANK.get, default="no_alert")
    reasons = set()
    if issues:
        label = max(label, "review", key=LABEL_RANK.get)
        reasons.add("technical_uncertainty")
    if label == "review":
        reasons |= {e["reason"] for e in events if e["label"] == "review"}
    else:
        reasons = set()
    return {**base, "status": "ok", "label": label, "reasons": sorted(reasons), "events": events,
            "issues": issues, "disabled_families": disabled}


def _where(ref, segments):
    first = segments.get(ref["segment_ids"][0], {})
    who = f" ({first['speaker']})" if first.get("speaker") else ""
    at = f" @ {ref['start']:.1f}s" if ref.get("start") is not None else ""
    q = f" q={ref['quality']}" if ref.get("quality") is not None else ""
    approx = f" ~{ref['match']}" if ref.get("match") not in (None, "exact") else ""
    return f"{'+'.join(ref['segment_ids'])}{who}{at}{q}{approx}: \"{ref['quote']}\""


def explain(result, segments):
    """Plain-text explanation read from the canonical result only."""
    by_id = {s["id"]: s for s in segments}
    lines = [f"DECISION: {result['label'].upper()}" + (f"  (review: {', '.join(result['reasons'])})" if result["reasons"] else "")]
    if result.get("error"):
        lines.append(f"  extraction failed: {result['error']}")
    lines += [f"  issue: {i}" for i in result.get("issues", [])]
    if not result["events"] and not result.get("error"):
        lines.append("  No candidate event in any family.")
    for e in sorted(result["events"], key=lambda e: -LABEL_RANK[e["label"]]):
        head = f"\n[{e['label']}] {e['family']}: {e['title']}" + (f"  -> {e['reason']}" if e.get("reason") else "")
        lines += [head, f"  {e['summary']}"]
        for name, c in e["conditions"].items():
            lines.append(f"  {c['state']:>7}  {name}" + ("  (downgraded: no grounded evidence)" if c["downgraded"] else ""))
            lines += [f"           {_where(r, by_id)}" for r in c["evidence"]]
        lines += [f"           {_where(r, by_id)}" for r in e["evidence"]]
        lines += [f"  OPEN: {q}" for q in e.get("open_questions", [])]
        if e.get("low_quality"):
            lines.append(f"  ASR quality below {result['threshold']} for: {', '.join(e['low_quality'])}")
        if e.get("quality_unavailable"):
            lines.append(f"  ASR quality not available for: {', '.join(e['quality_unavailable'])}")
        lines += [f"  issue: {i}" for i in e["issues"]]
    return "\n".join(lines)
