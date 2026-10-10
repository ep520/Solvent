"""Deterministic decision: ground the evidence, apply three-valued rules, ASR-quality threshold, explain."""
import difflib
import math
import re

LABEL_RANK = {"no_alert": 0, "review": 1, "alarm": 2}
STATES = ("true", "false", "unknown")
NEGATIONS = {"nöd", "nid", "ned", "nicht", "nüt", "nichts", "kei", "kein", "keine", "keini", "keis", "keinen",
             "keiner", "nie", "niemer", "niemand", "nümme", "nimmer", "nein", "nei"}


def _norm(text):
    return " ".join(re.sub(r"[^\w]+", " ", text.casefold()).split())


def quality(seg):
    """ASR-quality heuristic exp(avg_logprob); None when the source gives no score (a script, some ASR APIs)."""
    lp = seg.get("avg_logprob")
    return round(math.exp(lp), 3) if lp is not None else None


class Grounder:
    """Locates a quote in its cited segment, or in that segment joined with one neighbour.

    Exact match first. Otherwise a word-level approximate alignment (difflib) that tolerates the model's copy
    slips in dialect text; it is rejected below min_similarity or when negations differ, and the evidence then
    shows the real transcript words, never the model's paraphrase.
    """

    def __init__(self, segments, min_similarity=0.92):
        self.order = [s["id"] for s in segments]
        self.pos = {sid: i for i, sid in enumerate(self.order)}
        self.by_id = {s["id"]: s for s in segments}
        self.by_number = {key: sid for sid in self.order if (key := self._number(sid))}
        self.min_similarity = min_similarity

    @staticmethod
    def _number(sid):
        m = re.match(r"^([A-Za-z]+)0*(\d+)$", str(sid or "").strip())
        return (m.group(1).casefold(), int(m.group(2))) if m else None

    def resolve(self, sid):
        """The cited id, or the segment with the same prefix and number ('s17' -> 's017')."""
        return sid if sid in self.pos else self.by_number.get(self._number(sid))

    def _windows(self, sid):
        i = self.pos[sid]
        windows = [[sid]]
        if i > 0:
            windows.append([self.order[i - 1], sid])
        if i + 1 < len(self.order):
            windows.append([sid, self.order[i + 1]])
        return windows

    def _ref(self, ids, quote, match):
        segs = [self.by_id[x] for x in ids]
        qs = [quality(s) for s in segs]
        return {"segment_ids": ids, "quote": quote, "match": match, "start": segs[0].get("start"),
                "end": segs[-1].get("end"), "quality": None if None in qs else min(qs)}

    def _align(self, quote_words, ids):
        tokens = [(w, sid) for sid in ids for w in re.findall(r"\w+", self.by_id[sid]["text"])]
        target = " ".join(quote_words)
        best = (0.0, 0, 0)
        for n in range(max(1, len(quote_words) - 2), len(quote_words) + 3):
            for i in range(0, max(1, len(tokens) - n + 1)):
                cand = " ".join(w.casefold() for w, _ in tokens[i:i + n])
                ratio = difflib.SequenceMatcher(None, target, cand).ratio()
                if ratio > best[0]:
                    best = (ratio, i, i + n)
        ratio, i, j = best
        span = tokens[i:j]
        if ratio < self.min_similarity or not span:
            return None
        if set(quote_words) & NEGATIONS != {w.casefold() for w, _ in span} & NEGATIONS:
            return None
        span_ids = [sid for sid in ids if any(s == sid for _, s in span)]
        return self._ref(span_ids, " ".join(w for w, _ in span), round(ratio, 3))

    def ground(self, ref):
        sid, quote = self.resolve(ref.get("segment_id")), _norm(ref.get("quote") or "")
        if sid is None or not quote:
            return None
        for ids in self._windows(sid):
            if quote in _norm(" ".join(self.by_id[x]["text"] for x in ids)):
                return self._ref(ids, ref["quote"], "exact")
        for ids in self._windows(sid):
            found = self._align(quote.split(), ids)
            if found:
                return found
        return None

    def ground_all(self, refs, issues, where):
        kept = []
        for ref in refs or []:
            g = self.ground(ref) if isinstance(ref, dict) else None
            if g:
                kept.append(g)
            else:
                ref = ref if isinstance(ref, dict) else {}
                issues.append(f"{where}: quote not found at {ref.get('segment_id')}: {str(ref.get('quote', ''))[:80]!r}")
        return kept


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
    for name in fam["conditions"]:
        c = (raw.get("conditions") or {}).get(name)
        if not isinstance(c, dict) or c.get("state") not in STATES:
            issues.append(f"{name}: missing or invalid in extraction")
            conditions[name] = {"state": "unknown", "evidence": [], "downgraded": True}
            continue
        refs = grounder.ground_all(c.get("evidence"), issues, name)
        state, downgraded = c["state"], False
        if state in ("true", "false") and not refs:
            state, downgraded = "unknown", True
            issues.append(f"{name}: '{c['state']}' has no grounded evidence, set to unknown")
        conditions[name] = {"state": state, "evidence": refs, "downgraded": downgraded}

    result = {"family": family, "title": fam["title"], "conditions": conditions,
              "evidence": grounder.ground_all(raw.get("evidence"), issues, "event") if not fam["conditions"] else [],
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
        result.update(status="undetermined", label="review",
                      reason="technical_uncertainty" if technical else "missing_policy_fact",
                      open_questions=[fam["conditions"][n] for n, c in conditions.items() if c["state"] == "unknown"])
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
    base = {"threshold": threshold, "meta": (extraction or {}).get("_meta", {})}
    if error:
        return {**base, "label": "review", "reasons": ["technical_uncertainty"], "events": [], "issues": [],
                "error": str(error)}
    grounder = Grounder(segments, policies.get("grounding", {}).get("min_similarity", 0.92))
    families = (extraction or {}).get("families")
    families = families if isinstance(families, dict) else {}
    issues, events = [], []
    for name in policies["families"]:
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
    return {**base, "label": label, "reasons": sorted(reasons), "events": events, "issues": issues}


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
