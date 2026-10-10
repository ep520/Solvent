"""
LLM checks: (1) judge each keyword candidate in context, (2) sweep the whole call for
passages without keywords. Every result carries risk, check, reason, verbatim quote,
timestamps, and uncertainty flags.

Talks to a SELF-HOSTED OpenAI-compatible server (vLLM, llama.cpp, Ollama). Nothing leaves
the machine. The `openai` package is only the client library; it is pointed at localhost.

    pip install openai
    vllm serve <model> --max-model-len 16384 --gpu-memory-utilization 0.6
    export LLM_BASE_URL=http://localhost:8000/v1  LLM_MODEL=<model>
    python llm_checks.py            # demo against the server
    python llm_checks.py --mock     # demo without a GPU
"""
import argparse
import hashlib
import json
import sys
import os
import re
import sqlite3
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

PROMPT_VERSION = "v1"     # bump after editing prompts so the cache refreshes
SAMPLES = 3               # votes per passage; disagreement => "review"
TEMPERATURE = 0.6
CONTEXT_TURNS = 2         # turns shown before/after a candidate
SWEEP_CHUNK, SWEEP_OVERLAP = 40, 6
SNIPPET_PAD = 10.0        # seconds before/after for the audio snippet
REASON_LANGUAGE = "English"


@dataclass
class Turn:
    idx: int
    speaker: str          # advisor / customer / unknown
    start: float          # seconds
    end: float
    text: str


def mmss(s):
    return None if s is None else f"{int(s // 60):02d}:{int(s % 60):02d}"


def tnum(t) -> int:
    """The turn number the LLM sees and cites: from the script label (T018 -> 18) if present."""
    tid = getattr(t, "tid", "") or ""
    return int(tid[1:]) if tid[1:].isdigit() else t.idx


def render(turns, mark=()):
    return "\n".join(
        f"{'>> ' if t.idx in mark else '   '}[T{tnum(t)}]"
        f"{' ' + mmss(t.start) if t.start is not None else ''} {t.speaker.upper()}: {t.text}"
        for t in turns
    )


def load_checks(path="checks.json"):
    return [c for c in json.loads(Path(path).read_text(encoding="utf-8")) if c.get("enabled", True)]


def render_checks(checks):
    lines = []
    for c in checks:
        lines.append(f"- {c['id']}: {c['description']}")
        for ex in c.get("examples", []):
            lines.append(f'    e.g. {ex["label"]}: "{ex["text"]}" ({ex["why"]})')
    return "\n".join(lines)


# ---------- prompts and output schemas ----------

SYSTEM = """You are a compliance analyst at a Swiss bank reviewing transcripts of phone calls \
between bank advisors and customers. Transcripts are Swiss German or Standard German and may \
contain speech-recognition errors.

Rules:
- A keyword alone is not evidence. Talking ABOUT a topic (news, rules, training, refusing a \
request) is harmless. Suspicious means someone actually does, offers or asks for the prohibited behaviour.
- Either side can be the offender: advisor or customer.
- Quote evidence verbatim from the transcript and cite turn numbers (the number after T).
- Write `reason` in {lang}: one or two plain sentences a compliance officer understands.
- `risk` is your probability (0-1) that a compliance officer would escalate this passage.

Active checks:
{checks}"""

JUDGE_USER = """The passage marked with >> was flagged by the keyword "{term}" (family: {family}). \
Surrounding turns are context.

{window}

Judge only the marked passage. Is it suspicious under any active check?"""

SWEEP_USER = """Read this part of a call. List passages that are suspicious under an active check, \
including ones that avoid obvious keywords. Return an empty list if nothing is suspicious.

{chunk}"""


def judge_schema(check_ids):
    # Reason comes before the verdict so the model "thinks" before it scores.
    return {
        "type": "object",
        "properties": {
            "evidence_quote": {"type": "string"},
            "evidence_turns": {"type": "array", "items": {"type": "integer"}},
            "reason": {"type": "string"},
            "check": {"type": "string", "enum": check_ids + ["none"]},
            "suspicious": {"type": "boolean"},
            "risk": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["evidence_quote", "evidence_turns", "reason", "check", "suspicious", "risk"],
    }


def sweep_schema(check_ids):
    item = {
        "type": "object",
        "properties": {
            "evidence_turns": {"type": "array", "items": {"type": "integer"}},
            "check": {"type": "string", "enum": check_ids},
            "why": {"type": "string"},
        },
        "required": ["evidence_turns", "check", "why"],
    }
    return {"type": "object", "properties": {"findings": {"type": "array", "items": item, "maxItems": 5}},
            "required": ["findings"]}


# ---------- model client ----------

class LLM:
    def __init__(self, mock=False):
        self.mock = mock
        self.model = os.environ.get("LLM_MODEL", "local-model")
        if not mock:
            base = os.environ.get("LLM_BASE_URL", "http://localhost:8000/v1")
            if "openai.com" in base or "anthropic.com" in base:
                raise SystemExit("Cloud LLMs are not allowed for this case. Point LLM_BASE_URL at your own server.")
            from openai import OpenAI
            self.client = OpenAI(base_url=base, api_key="local")

    def complete(self, system, user, schema, n):
        if self.mock:
            return mock_complete(user, schema, n)
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            n=n,
            temperature=TEMPERATURE if n > 1 else 0.0,
            max_tokens=700,
            response_format={"type": "json_schema", "json_schema": {"name": "out", "schema": schema}},
        )
        out = []
        for choice in resp.choices:
            try:
                out.append(json.loads(choice.message.content))
            except (json.JSONDecodeError, TypeError):
                pass
        return out


class Cache:
    """LLM answers keyed by model + prompt. Moving the threshold never calls the model again."""

    def __init__(self, path="llm_cache.sqlite"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS c (k TEXT PRIMARY KEY, v TEXT)")
        self.lock = threading.Lock()

    @staticmethod
    def key(*parts):
        return hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()

    def get(self, k):
        with self.lock:
            row = self.db.execute("SELECT v FROM c WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO c VALUES (?, ?)", (k, json.dumps(v, ensure_ascii=False)))
            self.db.commit()
        return v


# ---------- the checks ----------

def _norm(s):
    return re.sub(r"\W+", " ", s.lower()).strip()


class LLMChecks:
    def __init__(self, checks, llm, cache):
        self.checks, self.llm, self.cache = checks, llm, cache
        self.ids = [c["id"] for c in checks]
        self.system = SYSTEM.format(lang=REASON_LANGUAGE, checks=render_checks(checks))

    def _ask(self, kind, user, schema, n):
        k = Cache.key(PROMPT_VERSION, self.llm.model, kind, n, self.system, user)
        hit = self.cache.get(k)
        return hit if hit is not None else self.cache.put(k, self.llm.complete(self.system, user, schema, n))

    def judge(self, turn_ids, turns, term="(none)", family="(full-call sweep)", source="keyword"):
        by_num = {tnum(t): t for t in turns}
        lo, hi = min(turn_ids), max(turn_ids)
        window = [t for t in turns if lo - CONTEXT_TURNS <= t.idx <= hi + CONTEXT_TURNS]
        user = JUDGE_USER.format(term=term, family=family, window=render(window, mark=set(range(lo, hi + 1))))
        votes = [v for v in self._ask("judge", user, judge_schema(self.ids), SAMPLES) if v]
        if not votes:
            return None

        risks = [v["risk"] if v["suspicious"] else min(v["risk"], 0.3) for v in votes]
        majority = Counter(v["suspicious"] for v in votes).most_common(1)[0][0]
        side = [v for v in votes if v["suspicious"] == majority]
        mean = sum(risks) / len(risks)
        rep = min(side, key=lambda v: abs(v["risk"] - mean))
        check = Counter(v["check"] for v in side).most_common(1)[0][0]

        ev_turns = sorted({by_num[n].idx: by_num[n] for n in rep.get("evidence_turns", []) if n in by_num}.values(),
                          key=lambda t: t.idx) or [t for t in turns if lo <= t.idx <= hi]
        starts = [t.start for t in ev_turns if t.start is not None]
        ends = [t.end if t.end is not None else t.start for t in ev_turns if t.start is not None]
        start, end = (min(starts), max(ends)) if starts else (None, None)

        uncertain = []
        if len(side) < len(votes) or max(risks) - min(risks) > 0.35:
            uncertain.append("model answers disagree")
        quote = rep.get("evidence_quote", "")
        if quote and _norm(quote) not in _norm(" ".join(t.text for t in window)):
            uncertain.append("quoted evidence not found verbatim in transcript")

        return {
            "source": source,
            "keyword": None if source == "sweep" else {"term": term, "family": family},
            "check": check,
            "suspicious": majority,
            "risk": round(mean, 3),
            "agreement": f"{len(side)}/{len(votes)}",
            "reason": rep["reason"],
            "evidence_quote": quote,
            "evidence_turns": [t.idx for t in ev_turns],
            "evidence_labels": [getattr(t, "tid", "") or f"T{t.idx}" for t in ev_turns],
            "speakers": sorted({t.speaker for t in ev_turns}),
            "start": start, "end": end,
            "timestamp": mmss(start),
            "snippet": None if start is None else [max(0.0, start - SNIPPET_PAD), end + SNIPPET_PAD],
            "uncertain_reasons": uncertain,
        }

    def sweep(self, turns):
        """Find suspicious turns without keywords. Proposals are then scored by judge()."""
        found, step = [], SWEEP_CHUNK - SWEEP_OVERLAP
        for i in range(0, max(1, len(turns)), step):
            chunk = turns[i:i + SWEEP_CHUNK]
            if not chunk:
                break
            out = self._ask("sweep", SWEEP_USER.format(chunk=render(chunk)), sweep_schema(self.ids), 1)
            for f in (out[0]["findings"] if out else []):
                nums = {tnum(t): t.idx for t in chunk}
                ids = [nums[x] for x in f["evidence_turns"] if x in nums]
                if ids:
                    found.append(sorted(ids))
            if i + SWEEP_CHUNK >= len(turns):
                break
        return found

    def run(self, turns, candidates, workers=8):
        covered = {i for c in candidates for i in c["turn_ids"]}
        jobs = [(c["turn_ids"], c["term"], c["family"], "keyword") for c in candidates]
        jobs += [(ids, "(none)", "(full-call sweep)", "sweep")
                 for ids in self.sweep(turns) if not covered.intersection(ids)]
        with ThreadPoolExecutor(workers) as pool:     # vLLM batches parallel requests
            results = list(pool.map(lambda j: self.judge(j[0], turns, *j[1:]), jobs))
        return sorted((r for r in results if r), key=lambda r: -r["risk"])


# ---------- mock model so the pipeline runs without a GPU ----------

_SUSPICIOUS = {"vor de meldig": "trade", "ufteile": "splitting", "gilt grad no": "access",
               "söll use": "documentation"}
_HARMLESS = ["podcast", "nöd vor de meldig handle"]


def mock_complete(user, schema, n):
    """Fake model for plumbing tests only. It knows a few phrases, it does NOT judge."""
    lines = user.splitlines()
    if "findings" in schema["properties"]:
        hits = [{"evidence_turns": [int(m.group(1))], "check": chk, "why": "mock"}
                for ln in lines if (m := re.search(r"\[T(\d+)\]", ln))
                for w, chk in _SUSPICIOUS.items() if w in ln.lower()]
        return [{"findings": hits}]
    marked = [ln for ln in lines if ln.startswith(">>")]
    text = " ".join(marked).lower()
    ids = [int(x) for x in re.findall(r"\[T(\d+)\]", " ".join(marked))]
    chk = next((c for w, c in _SUSPICIOUS.items() if w in text), None)
    bad = chk is not None and not any(h in text for h in _HARMLESS)
    quote = re.sub(r"^.*?: ", "", marked[0]) if marked else ""
    return [{
        "evidence_quote": quote, "evidence_turns": ids,
        "reason": "Mock: suspicious phrase found." if bad else "Mock: no suspicious behaviour found.",
        "check": chk if bad else "none", "suspicious": bad,
        "risk": (0.85 if bad else 0.1) + 0.02 * k,
    } for k in range(n)]


if __name__ == "__main__":
    import snippets as S

    ap = argparse.ArgumentParser(description="transcript -> snippets -> LLM judgments")
    ap.add_argument("transcript", help="script .txt or ASR .json")
    ap.add_argument("--keywords", default="Stichwortliste.json")
    ap.add_argument("--checks", default=str(Path(__file__).with_name("checks.json")))
    ap.add_argument("--semantic", default="bge-m3", choices=["bge-m3", "tfidf", "off"])
    ap.add_argument("--mock", action="store_true", help="fake model, plumbing test only")
    args = ap.parse_args()

    ex = S.extract(args.transcript, args.keywords, args.checks, args.semantic)
    candidates = S.to_llm_candidates(ex["snippets"])
    print(f"{ex['stats']['turns']} turns -> {len(candidates)} snippets for the LLM", file=sys.stderr)
    cache = Cache(":memory:" if args.mock else "llm_cache.sqlite")
    checker = LLMChecks(load_checks(args.checks), LLM(mock=args.mock), cache)
    print(json.dumps(checker.run(ex["turns"], candidates), indent=2, ensure_ascii=False))
