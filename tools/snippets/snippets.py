"""
Snippet extractor: long call transcript -> short suspicious snippets with highlighted words.

Sits between transcription and the LLM. Instead of sending a whole call to the LLM, it keeps
only the passages worth judging, each with the turns, speaker roles, matched words
highlighted in **bold**, and why it was kept.

Three independent finders; a passage is kept if ANY of them fires:
  1. Keywords   - Stichwortliste.json (Inventx format: families -> keyword IDs -> de/gsw phrases).
                  Respects `enabled` and `partial_words_count_as_full_hit`. Fuzzy matching only
                  for ASR typos in long phrases.
  2. Digits     - spoken digit sequences ("neun, eins, null, sieben"): codes, card numbers,
                  references. No keyword covers these, but they decide the access/numbers cases.
  3. Semantic   - a multilingual embedding model (BAAI/bge-m3) compares turns with the check
                  definitions in checks.json. Catches passages with no keyword at all.

Input formats (auto-detected):
  - Inventx scripts, M-files:   "T001 B: text"          (B = Beratung, K = Kunde)
  - Inventx scripts, C/D-files: "T001 · Beratung" then the text on the next line(s)
  - ASR output JSON:            [{"speaker", "start", "end", "text"}, ...] or {"segments": [...]}
  - generic:                    "[mm:ss] Speaker: text"

    pip install rapidfuzz sentence-transformers
    python snippets.py Stufe1_D02.txt --keywords Stichwortliste.json --checks checks.json
    python snippets.py ... --semantic tfidf      # no GPU / no model download (weaker)
    python snippets.py ... --semantic off        # keywords + digits only
"""
import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz

EMBED_MODEL = "BAAI/bge-m3"     # multilingual, MIT license, runs locally
CONTEXT = 1                     # extra turns kept around every hit
FUZZY_MIN = 90                  # rapidfuzz score for typo matches
FUZZY_MIN_LEN = 8               # only phrases at least this long are fuzzy-matched
MIN_DIGITS = 4                  # spoken digits in a row that count as a number sequence
SEM_TOP_K = 2                   # semantic hits kept per call (at most)
SEM_MIN = {"bge-m3": 0.50, "tfidf": 0.10}   # minimum similarity; calibrate on training calls


# ---------------- transcript parsing ----------------

@dataclass
class Turn:
    idx: int                    # position in the call, 0-based (used internally and by the LLM)
    speaker: str                # advisor / customer / unknown
    text: str
    start: float | None = None  # seconds; None for scripts without timing
    end: float | None = None
    tid: str = ""               # original label, e.g. "T018"
    quality: float | None = None  # ASR quality exp(avg_logprob) in 0..1; None for scripts


ROLE = {"b": "advisor", "beratung": "advisor", "berater": "advisor", "beraterin": "advisor",
        "bankberatung": "advisor", "advisor": "advisor",
        "k": "customer", "kunde": "customer", "kundin": "customer", "kundschaft": "customer",
        "customer": "customer"}

HEAD_DOT = re.compile(r"^\s*(T\d{2,4})\s*[·•|-]\s*(\w+)\s*$")            # T001 · Beratung
HEAD_COLON = re.compile(r"^(T\d{2,4})\s+([A-Za-zÄÖÜäöü]\w*)\s*:\s*(.*)$")  # T001 B: text
TS = r"\[?(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)\]?"
GENERIC = re.compile(rf"^\s*{TS}\s*[-–]?\s*([^:\[\]]{{1,30}}?)\s*:\s*(.+)$")    # [00:12] Kunde: text


def role(label: str) -> str:
    return ROLE.get(label.strip().lower(), label.strip().lower() or "unknown")


def _secs(ts: str) -> float:
    parts = [float(p.replace(",", ".")) for p in ts.split(":")]
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))


def parse_transcript(raw: str, is_json: bool = False) -> list[Turn]:
    if is_json:
        data = json.loads(raw)
        segs = data.get("segments", data) if isinstance(data, dict) else data
        segs = [s for s in segs if s.get("text", "").strip()]

        def quality(s):                          # Whisper gives avg_logprob per segment
            if s.get("quality") is not None:
                return float(s["quality"])
            if s.get("avg_logprob") is not None:
                return round(math.exp(float(s["avg_logprob"])), 3)
            return None
        return [Turn(i, role(s.get("speaker", "unknown")), s["text"].strip(), s.get("start"), s.get("end"),
                     s.get("tid", f"T{i + 1:03d}"), quality(s)) for i, s in enumerate(segs)]

    lines = raw.splitlines()
    turns: list[Turn] = []
    if any(HEAD_DOT.match(l) or HEAD_COLON.match(l) for l in lines):
        cur = None
        for line in lines:
            m1, m2 = HEAD_DOT.match(line), HEAD_COLON.match(line)
            if m1 or m2:
                tid, spk = (m1 or m2).group(1), (m1 or m2).group(2)
                cur = Turn(len(turns), role(spk), (m2.group(3).strip() if m2 else ""), tid=tid)
                turns.append(cur)
            elif cur is not None and line.strip():       # text lines belong to the open turn;
                cur.text = (cur.text + " " + line.strip()).strip()   # header lines before T001 are skipped
        return [t for t in turns if t.text]

    for line in lines:                                   # generic "[mm:ss] Speaker: text"
        m = GENERIC.match(line)
        if m:
            ts, spk, text = m.groups()
            turns.append(Turn(len(turns), role(spk), text.strip(), _secs(ts) if ts else None,
                              tid=f"T{len(turns) + 1:03d}"))
        elif turns and line.strip():
            turns[-1].text += " " + line.strip()
    for a, b in zip(turns, turns[1:]):
        if a.start is not None and a.end is None and b.start is not None:
            a.end = b.start
    return turns


def load_transcript(path: str) -> list[Turn]:
    return parse_transcript(Path(path).read_text(encoding="utf-8"), path.endswith(".json"))


# ---------------- keyword list ----------------

@dataclass
class Keyword:
    family: str
    kid: str
    label: str
    phrases: list[str]
    partial_ok: bool = False
    harmless: str = ""


def norm(s: str) -> str:
    s = s.casefold()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("é", "e"), ("è", "e"), ("à", "a")):
        s = s.replace(a, b)
    return s


def load_keywords(path: str) -> list[Keyword]:
    """Inventx format (families -> keyword IDs -> de/gsw), plus simple {family: [phrases]}."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("keywords"), list):
        fam_of = {kid: fam for fam, f in data.get("families", {}).items() for kid in f.get("keywords", [])}
        out = []
        for k in data["keywords"]:
            if not k.get("enabled", True):
                continue
            phrases = list(dict.fromkeys(p for p in k.get("de", []) + k.get("gsw", []) + k.get("terms", []) if p))
            out.append(Keyword(fam_of.get(k["id"], k.get("family", "other")), k["id"], k.get("label", k["id"]),
                               phrases, bool(k.get("partial_words_count_as_full_hit", False)),
                               k.get("harmless", "")))
        return out
    return [Keyword(fam, fam, fam, list(terms)) for fam, terms in data.items() if isinstance(terms, list)]


@dataclass
class Hit:
    family: str
    kid: str
    term: str
    turn: int
    matched: str
    span: tuple[int, int]
    how: str      # exact / partial / fuzzy / digits / cue / context
    link: int | None = None   # for context hits: the anchor turn they explain


def find_keywords(turns: list[Turn], keywords: list[Keyword], fuzzy=True) -> list[Hit]:
    hits = []
    for t in turns:
        words = [(m.group(), m.start(), m.end()) for m in re.finditer(r"\w+", t.text)]
        nwords = [norm(w) for w, _, _ in words]
        for kw in keywords:
            for phrase in kw.phrases:
                nt = norm(phrase).split()
                n, target = len(nt), " ".join(nt)
                for i in range(len(words) - n + 1):
                    cand = " ".join(nwords[i:i + n])
                    how = None
                    if cand == target:
                        how = "exact"
                    elif kw.partial_ok and n == 1 and len(target) >= 5 and target in cand:
                        how = "partial"
                    elif fuzzy and len(target) >= FUZZY_MIN_LEN and fuzz.ratio(cand, target) >= FUZZY_MIN:
                        how = "fuzzy"
                    if how:
                        s, e = words[i][1], words[i + n - 1][2]
                        hits.append(Hit(kw.family, kw.kid, phrase, t.idx, t.text[s:e], (s, e), how))
    best: dict[tuple, Hit] = {}
    rank = {"exact": 0, "partial": 1, "fuzzy": 2}
    for h in hits:                           # one hit per text span, most specific wins
        k = (h.turn, h.span)
        if k not in best or rank[h.how] < rank[best[k].how]:
            best[k] = h
    return sorted(best.values(), key=lambda h: (h.turn, h.span))


# ---------------- spoken digit sequences ----------------

DIGIT_WORDS = {norm(w) for w in """null nul nulle eis eins ein ei zwei zwöi zwo drei drü drüü vier
    füf füüf fünf fuf sechs sächs sachs sibe sibä siebe sieben acht nün nüün nüün neun""".split()}


def find_digits(turns: list[Turn], min_len=MIN_DIGITS) -> list[Hit]:
    """Runs of spoken digits ('neun, eins, null, sieben') or written digit groups."""
    hits = []

    def flush(run, t):
        count = sum(len(w) if w.isdigit() else 1 for w, _, _ in run)
        if count >= min_len:
            s, e = run[0][1], run[-1][2]
            hits.append(Hit("numbers", "DIGITS", f"{count} digits", t.idx, t.text[s:e], (s, e), "digits"))

    for t in turns:
        run = []
        for m in re.finditer(r"\w+", t.text):
            w = m.group()
            if norm(w) in DIGIT_WORDS or w.isdigit():
                run.append((w, m.start(), m.end()))
            else:
                flush(run, t)
                run = []
        flush(run, t)
    return hits



# ---------------- cues: anchors + linked context anywhere in the call ----------------

MAX_LINKS = 2          # context turns attached per anchor
MAX_ANCHORS = 2        # strongest anchors kept per family and call
MIN_CONTEXT_CUES = 2   # distinct context cues a turn needs to be attached


def load_cues(path: str) -> dict:
    if not Path(path).exists():                       # bare name -> the file next to this module
        path = Path(__file__).resolve().parent / path
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_") and v.get("enabled", True)}


AMOUNT = re.compile(r"(tuusig|tausend|hundert|millione|million)", re.I)


def is_amount(word: str) -> bool:
    """'zwölftuusig', 'füfhundert', '4500' -> True. Spoken amounts in Swiss/Standard German."""
    return bool(AMOUNT.search(norm(word))) or (word.isdigit() and len(word) >= 3)


def amounts_in(text: str) -> list[tuple[str, tuple[int, int]]]:
    return [(m.group(), (m.start(), m.end())) for m in re.finditer(r"\w+", text) if is_amount(m.group())]


def match_phrases(text: str, phrases: list[str]) -> list[tuple[str, tuple[int, int]]]:
    """Whole-word match on normalized text. Words of 5+ letters also match as prefix
    ('veröffentlicht' finds 'veröffentlichte'); '*xyz' matches xyz anywhere inside a word.
    Returns (phrase, span) per match."""
    words = [(m.group(), m.start(), m.end()) for m in re.finditer(r"\w+", text)]
    nwords = [norm(w) for w, _, _ in words]
    out = []
    for ph in phrases:
        if ph.startswith("*"):
            sub = norm(ph[1:])
            out += [(ph, (w[1], w[2])) for w, nw in zip(words, nwords) if sub in nw]
            continue
        nt = norm(ph).split()
        n = len(nt)
        for i in range(len(words) - n + 1):
            ok = all(nwords[i + k] == nt[k] or (len(nt[k]) >= 5 and nwords[i + k].startswith(nt[k]))
                     for k in range(n))
            if ok:
                out.append((ph, (words[i][1], words[i + n - 1][2])))
    return out


def find_cues(turns: list[Turn], cues: dict, digit_hits: list = (), keyword_hits: list = (),
              max_links=MAX_LINKS, min_ctx=MIN_CONTEXT_CUES, max_anchors=MAX_ANCHORS) -> list[Hit]:
    """Per family: find ANCHOR turns (something happens) and attach CONTEXT turns (what explains it)
    from anywhere in the call. Options per family in cues.json:
      action / request      anchor = action cue (+ request marker if the list is non-empty)
      speaker               anchors only from this role (advisor / customer)
      require_amount        anchor must also contain a spoken amount ('zwölftuusig')
      anchor_min_amounts    a turn with at least N amounts is an anchor (e.g. a split: 'hüt 4500 und morn 4500')
      anchor_from_digits    read-out digit sequences are anchors
      context / strong_context   context cues; strong ones count double
      first_intent          also link the customer's FIRST stated wish before the main anchor
      last_intent           also link the customer's LAST statement about it (e.g. "no order today")
      amount_markers        a multi-amount anchor also needs one of these words ('hüt', 'morn')
      requires_context_in_call   drop anchors if no context turn exists anywhere in the call
    """
    hits = []
    digit_turns = {h.turn for h in digit_hits}
    for fam, c in cues.items():
        strong = c.get("strong_context", [])
        ctx = []                                   # (score, turn, matches) for every turn
        for t in turns:
            m = match_phrases(t.text, c.get("context", []))
            ms = match_phrases(t.text, strong)
            score = len({sp for _, sp in m} - {sp for _, sp in ms}) + 2 * len({sp for _, sp in ms})
            ctx.append((score, t, m + ms))

        spk = c.get("speaker")
        scored = []                                # (strength, turn, matches)
        for t in turns:
            if spk and t.speaker != spk:
                continue
            act = match_phrases(t.text, c.get("action", []))
            req = c.get("request", [])
            rq = match_phrases(t.text, req) if req else []
            amt = amounts_in(t.text)
            ok = bool(act) and (not req or rq) and (not c.get("require_amount") or amt)
            if not ok and c.get("anchor_min_amounts") and len(amt) >= c["anchor_min_amounts"] \
                    and match_phrases(t.text, c.get("amount_markers", [])):
                ok, act = True, amt
            if ok:
                act = act + (amt if c.get("require_amount") else [])
                strength = len({sp for _, sp in act}) + len({sp for _, sp in rq}) + ctx[t.idx][0]
                scored.append((strength, t, act))
        if c.get("requires_context_in_call") and not any(sc >= min_ctx for sc, _, _ in ctx):
            scored = []                            # e.g. disclosure: no third-party/authority talk at all
        scored.sort(key=lambda x: (-x[0], -x[1].idx))   # strongest first; later turn wins ties
        anchors = []
        for strength, t, act in scored[:max_anchors]:
            anchors.append(t.idx)
            hits += [Hit(fam, "CUE", p, t.idx, t.text[a:b], (a, b), "cue") for p, (a, b) in act]
        anchors += [h.turn for h in keyword_hits      # official keyword hits are anchors too
                    if h.family == fam and h.turn not in anchors]
        if c.get("anchor_from_digits"):            # read-out numbers are anchors for these families
            anchors += [i for i in digit_turns if i not in anchors]

        for a in anchors:
            cands = [(sc, t, m) for sc, t, m in ctx if sc >= min_ctx and t.idx != a]
            cands.sort(key=lambda x: (-x[0], abs(x[1].idx - a)))
            for sc, t, m in cands[:max_links]:
                hits += [Hit(fam, "CTX", p, t.idx, t.text[x:y], (x, y), "context", link=a) for p, (x, y) in m]

        fi = c.get("first_intent")
        if fi and anchors:                         # the customer's first stated wish, before the anchors
            main = min(anchors)
            first = None
            for t in turns[:main]:
                if t.speaker != "customer":
                    continue
                act = match_phrases(t.text, c.get("action", []))
                req = match_phrases(t.text, c.get("request", []))
                if act and req:
                    first = (t, act)
                    break
            if first is None:
                for t in turns[:main]:
                    it = match_phrases(t.text, fi.get("intent", []))
                    if t.speaker == "customer" and it and (not fi.get("need_amount") or amounts_in(t.text)):
                        first = (t, it)
                        break
            if first:
                t, m = first
                hits += [Hit(fam, "CTX", p, t.idx, t.text[x:y], (x, y), "context", link=main) for p, (x, y) in m]

        li = c.get("last_intent")
        if li and anchors:                         # the customer's LAST statement about the request
            main = max(anchors)
            for t in reversed(turns[main + 1:]):
                m = match_phrases(t.text, li.get("intent", []))
                if t.speaker == "customer" and m:
                    hits += [Hit(fam, "CTX", p, t.idx, t.text[x:y], (x, y), "context", link=main)
                             for p, (x, y) in m]
                    break
    return hits


# ---------------- semantic finder ----------------

class Embedder:
    def __init__(self, kind: str):
        self.kind = kind
        if kind == "bge-m3":
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer(EMBED_MODEL)
        elif kind != "tfidf":
            raise ValueError(kind)

    def similarity(self, docs: list[str], queries: list[str]):
        if self.kind == "bge-m3":
            d = self.model.encode(docs, normalize_embeddings=True, batch_size=32)
            q = self.model.encode(queries, normalize_embeddings=True)
            return d @ q.T
        from sklearn.feature_extraction.text import TfidfVectorizer
        vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5)).fit(docs + queries)
        return (vec.transform([norm(x) for x in docs]) @ vec.transform([norm(x) for x in queries]).T).toarray()


def check_queries(path: str) -> list[tuple[str, str]]:
    """(family, text) pairs for the semantic finder: from policies.json (title + conditions)
    or the older checks.json format (description + suspicious examples)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    if isinstance(data, dict) and "families" in data:
        for fam, f in data["families"].items():
            if f.get("enabled", True) and f.get("conditions"):
                out.append((fam, f["title"] + ". " + " ".join(f["conditions"].values())))
        return out
    for c in data:
        if not c.get("enabled", True):
            continue
        out.append((c["id"], c.get("description_de") or c["description"]))
        out += [(c["id"], ex["text"]) for ex in c.get("examples", []) if ex.get("label") == "suspicious"]
    return out


def find_semantic(turns, queries, embedder, top_k=SEM_TOP_K, min_sim=None):
    """Score every turn (with its predecessor as context) against the check definitions."""
    if not turns or not queries:
        return []
    docs = [(turns[i - 1].text + " " if i else "") + turns[i].text for i in range(len(turns))]
    sims = embedder.similarity(docs, [q for _, q in queries])
    min_sim = SEM_MIN[embedder.kind] if min_sim is None else min_sim
    scored = []
    for i, row in enumerate(sims):
        j = int(row.argmax())
        if row[j] >= min_sim:
            scored.append({"turns": [i], "check": queries[j][0], "score": float(row[j])})
    scored.sort(key=lambda s: -s["score"])
    return scored[:top_k]


# ---------------- merge into snippets ----------------

def mmss(s):
    return None if s is None else f"{int(s // 60):02d}:{int(s % 60):02d}"


def highlight(text: str, spans: list[tuple[int, int]]) -> str:
    merged = []                                   # merge overlapping/touching spans first
    for s, e in sorted(set(spans)):
        if merged and s <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    for s, e in reversed(merged):
        text = text[:s] + "**" + text[s:e] + "**" + text[e:]
    return text


def build_snippets(turns, hits, sem_hits, context=CONTEXT):
    """Group hits into snippets. A snippet is a SET of turns: the anchor with its neighbours
    plus linked context turns from anywhere in the call (gaps shown as '...')."""
    last = len(turns) - 1
    near = lambda i: set(range(max(0, i - context), min(last, i + context) + 1))
    groups = []                                     # [turn set, hits, semantic hits]
    for h in hits:
        if h.how == "context":
            groups.append([{h.turn} | near(h.link), [h], []])   # context turn alone + its anchor area
        else:
            groups.append([near(h.turn), [h], []])
    for sm in sem_hits:
        groups.append([set().union(*(near(i) for i in sm["turns"])), [], [sm]])

    merged = True                                   # merge groups that overlap or touch
    while merged:
        merged = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                a, b = groups[i][0], groups[j][0]
                if a & b or any(abs(x - y) <= 1 for x in a for y in b):
                    groups[i] = [a | b, groups[i][1] + groups[j][1], groups[i][2] + groups[j][2]]
                    del groups[j]
                    merged = True
                    break
            if merged:
                break

    out = []
    for ids_set, hs, sems in groups:
        ids = sorted(ids_set)
        hit_turns = sorted({h.turn for h in hs} | {i for sm in sems for i in sm["turns"]})
        lines, prev = [], None
        for i in ids:
            if prev is not None and i > prev + 1:
                lines.append("   ...")
            t = turns[i]
            mark = ">>" if any(h.turn == i and h.how != "context" for h in hs) or \
                any(i in sm["turns"] for sm in sems) else ("+ " if i in hit_turns else "  ")
            ts = f"[{mmss(t.start)}] " if t.start is not None else ""
            lines.append(f"{mark} {t.tid} {ts}{t.speaker}: {highlight(t.text, [h.span for h in hs if h.turn == i])}")
            prev = i
        strong = [h for h in hs if h.how in ("exact", "partial", "fuzzy")]
        families = sorted({h.family for h in hs if h.how != "context"})
        sem_best = max(sems, key=lambda x: x["score"]) if sems else None
        priority = min(1.0, 0.25 * len({h.family for h in strong}) + 0.1 * len(strong)
                       + (0.3 if any(h.how == "digits" for h in hs) else 0)
                       + 0.1 * len({h.family for h in hs if h.how == "cue"})
                       + (0.1 if any(h.how == "context" for h in hs) else 0)
                       + (sem_best["score"] * 0.5 if sem_best else 0))
        found_by = sorted({{"exact": "keyword", "partial": "keyword", "fuzzy": "keyword"}.get(h.how, h.how)
                           for h in hs} | ({"semantic"} if sems else set()))
        starts = [turns[i].start for i in ids if turns[i].start is not None]
        ends = [turns[i].end if turns[i].end is not None else turns[i].start for i in ids if turns[i].start is not None]
        out.append({
            "turn_ids": ids,
            "turn_labels": [turns[i].tid for i in ids],
            "hit_turns": hit_turns,
            "anchor_turns": sorted({h.turn for h in hs if h.how != "context"} | {i for sm in sems for i in sm["turns"]}),
            "context_turns": sorted({h.turn for h in hs if h.how == "context"}),
            "start": min(starts) if starts else None, "end": max(ends) if ends else None,
            "timestamp": mmss(min(starts)) if starts else None,
            "found_by": found_by,
            "hits": [{"family": h.family, "keyword_id": h.kid, "term": h.term, "matched": h.matched,
                      "turn": turns[h.turn].tid, "how": h.how,
                      **({"explains": turns[h.link].tid} if h.link is not None else {})} for h in hs],
            "families": families,
            "semantic_check": sem_best["check"] if sem_best else None,
            "semantic_score": round(sem_best["score"], 3) if sem_best else None,
            "priority": round(priority, 3),
            "text": "\n".join(lines),
        })
    out.sort(key=lambda x: -x["priority"])
    for i, sn in enumerate(out):
        sn["snippet_id"] = i
    return out


def to_llm_candidates(snippets):
    """Format expected by llm_checks.LLMChecks.run()."""
    return [{"family": ", ".join(s["families"]) or f"semantic:{s['semantic_check']}",
             "term": ", ".join(sorted({h["term"] for h in s["hits"]})) or "(no keyword)",
             "turn_ids": s["hit_turns"]} for s in snippets]


def extract(transcript, keywords, checks=None, semantic="bge-m3", embedder=None, fuzzy=True, cues="cues_general.json"):
    turns = load_transcript(transcript) if isinstance(transcript, str) else transcript
    kws = load_keywords(keywords) if isinstance(keywords, str) else keywords
    digits = find_digits(turns)
    kw_hits = find_keywords(turns, kws, fuzzy)
    hits = kw_hits + digits
    if cues:
        cue_def = load_cues(cues) if isinstance(cues, str) else cues
        hits += find_cues(turns, cue_def, digits, kw_hits)
    sem = []
    if semantic != "off" and checks:
        sem = find_semantic(turns, check_queries(checks), embedder or Embedder(semantic))
    snips = build_snippets(turns, hits, sem)
    kept = {i for s in snips for i in s["turn_ids"]}
    total = sum(len(t.text) for t in turns) or 1
    return {"stats": {"turns": len(turns), "snippets": len(snips), "kept_turns": len(kept),
                      "kept_text_pct": round(100 * sum(len(turns[i].text) for i in kept) / total, 1),
                      "keyword_hits": sum(h.how in ("exact", "partial", "fuzzy") for h in hits),
                      "cue_hits": sum(h.how in ("cue", "context") for h in hits),
                      "digit_hits": sum(h.how == "digits" for h in hits), "semantic_hits": len(sem)},
            "turns": turns, "snippets": snips}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("transcript")
    ap.add_argument("--keywords", required=True)
    ap.add_argument("--checks", help="checks.json; enables the semantic finder")
    ap.add_argument("--semantic", default="bge-m3", choices=["bge-m3", "tfidf", "off"])
    ap.add_argument("--no-fuzzy", action="store_true")
    ap.add_argument("--cues", default="cues_general.json", help="cue file; 'off' to disable")
    ap.add_argument("--out", help="write snippets JSON here")
    a = ap.parse_args()

    res = extract(a.transcript, a.keywords, a.checks, a.semantic, fuzzy=not a.no_fuzzy,
                  cues=None if a.cues == "off" else a.cues)
    if a.out:
        Path(a.out).write_text(json.dumps({k: v for k, v in res.items() if k != "turns"},
                                          indent=2, ensure_ascii=False), encoding="utf-8")
    s = res["stats"]
    print(f"{s['turns']} turns -> {s['snippets']} snippets, {s['kept_text_pct']}% of the text kept "
          f"({s['keyword_hits']} keyword, {s['digit_hits']} digit, {s['cue_hits']} cue, {s['semantic_hits']} semantic hits)\n"
          "   >> anchor turn   + linked context turn   ... skipped turns\n",
          file=sys.stderr)
    for sn in res["snippets"]:
        sem = f"  semantic: {sn['semantic_check']} {sn['semantic_score']}" if sn["semantic_check"] else ""
        fam = f"  families: {', '.join(sn['families'])}" if sn["families"] else ""
        print(f"--- snippet {sn['snippet_id']}  {sn['turn_labels'][0]}..{sn['turn_labels'][-1]}"
              f"  priority {sn['priority']}  found by {'+'.join(sn['found_by'])}{fam}{sem}")
        print(sn["text"] + "\n")
