# Action plan: call compliance pipeline (CallGuard)

Handover document. Whoever resumes the work (another person or another Claude Code session) must be able to continue **using only this document and the code**. To resume in Claude Code:

> Read `PLAN.md` and `README.md`, then continue from the first step marked ⏳ in section 6.

Deadline: final submission **Sunday, 2026-10-11 at 08:30**, pitch 09:00–11:00 (see `README.md`).

---

## 1. Objective (from the README)

Turn Swiss German call audio into a reasoned **alarm / review / no_alert** decision, with suspicious passages as audio clips (timestamp and reason), an **adjustable threshold**, a **metric** for false alarms and missed cases, and no manual steps per call. The threshold and keyword list must be editable without changing code.

**Models: team decision (2026-10-10).**
- **Chat (fact extraction): Claude Sonnet 5 (`claude-sonnet-5`)**, through the Claude Code CLI (`claude -p`), using the `claude_code` profile, the default in `config/models.json`. Uses the active account login, without API keys.
- **Anyone can switch to another model at any time**, locally or through an API, by changing only the profile (`CALLGUARD_CHAT=...` or `config/models.json`), without changing code. Every model change must pass the **smoke test** (step 4), followed by the full evaluation.
- The README requires self-hostable models for the core pipeline. The router warns on stderr about profiles that are not self-hostable. This is a deliberate team choice, not an oversight.
- **ASR: OpenAI `whisper-1`** (decision of 2026-10-10), using the `openai` profile, already the default for `asr` in `config/models.json`. Only the key in `.env` is needed (see section 5). Claude does not transcribe audio. Here too, the README requires a self-hostable model: the same deliberate team choice applies, and switching to a Whisper server (`speaches`, `private`) requires only a profile change.
  - **Why `whisper-1`:** it returns timestamped segments and `avg_logprob` (`verbose_json`), needed for clips and the quality threshold. Although cheaper or newer, `gpt-4o(-mini)-transcribe` models do not, to our knowledge, return timestamped segments: verify before switching.
  - **Budget: USD 75 in credits.** `whisper-1` price: USD 0.006/minute (verified on 2026-10-10).

    | Item | Minutes | Cost |
    |---|---|---|
    | 42 dataset WAV files (one full pass) | 191.6 | ~USD 1.15 |
    | Hidden test (30%, estimated) | ~82 | ~USD 0.50 |
    | Targeted segment retranscription (extra) | Negligible | < USD 1 |

    With file-hash caching, each WAV is transcribed **only once**; even 10 full passes remain below USD 12. Largest file: 18.5 MB, below the API's 25 MB limit (no splitting needed). Calls last from 2.4 to 9.6 minutes.

**Development constraint:** less than 16 GB RAM, no GPU, no model installed locally.

## 2. What we know about the data (verified)

- 21 dialogues × 2 recordings (K1/K3 clean, K2/K4 noisy). `Transkript/` contains the scripts; `Skript_mit_Sollbewertung/` contains the same scripts with expected assessment (`Bearbeitung`), family (`Testziel`), and evidence turns (`Belege`).
- **Expected labels:** 8 alarm, 10 no_alert, 3 review (C17, C19, C20).
- **Keywords:** only Stufe1 (D02–D09) contains literal keywords. **Stufe2 and Stufe3 contain none**: context decides. D03 has the same keywords as D02 but is `no_alert`. K07 never occurs. Keywords therefore support highlighting and the demo, **not decisions**.
- **Context pairs** (same text, only an early clue matters): C01/C02, C03/C04 (numbers), C05/C06 (trade), C09/C10 (access), C11/C12 (disclosure), C15/C16 (splitting). The same pattern applies to D02/D03, C09/D06, and D09/C19.
- In annotations, "K"/"B" identifies the speaker: Kunde (customer) / Berater (advisor).
- **Rule:** filenames, scripts, and expected assessments must not enter the pipeline. `pipeline.gold()` reads them **only** to calculate metrics. Using `Transkript/` as input is allowed during development.

## 3. Decisions made (and why)

| Decision | Reason | Rejected |
|---|---|---|
| One LLM request per call, extracting **facts** (true/false/unknown predicates with quotations), not labels | Decisions remain in code: reproducible and explainable | LLM deciding alarm or providing a score |
| Five families as **configuration** (`config/policies.json`), plus `numbers`, which never generates alarm | Families can be added or changed without code | Five separate models or classifiers |
| Only **two deterministic techniques**: (1) quotation anchoring (exact, then approximate alignment with a negation check), (2) three-valued rules | Few, strong techniques | Weighted scores, negation lexicons as a decision signal, Luhn/IBAN, statistics |
| **Every `true` and every `false` must have anchored evidence**, otherwise it becomes `unknown` | Unsupported `false` would close an event as `no_alert` without justification. Checked on 42 real Sonnet extractions: no unsupported `false`, so the rule adds no cost and protects against weaker models | Accepting `false` based on absence |
| Threshold = **ASR-quality heuristic** (step 5): every decisive condition must have at least one quotation with quality ≥ threshold | Connects the threshold to noise, the point where audio makes evidence uncertain | A "fraud probability"; a more elaborate score before measuring it on audio |
| The LLM processes **every** call and its entire transcript | Stufe2/3 have no keywords: an upstream filter would miss cases | Keyword filtering, routing classifier |
| Router with two adapters, standard library only: `openai_compatible` (HTTP) and `claude_cli` (`claude -p`). **Adapters frozen**: only `claude_code` (chat) and `openai` (ASR) in use | The first covers OpenAI, vLLM, Ollama, llama.cpp, speaches, and private gateways; the second uses Claude without API keys. Other profiles remain in `config/models.json` as switching options, neither maintained nor tested | Provider-specific SDKs; new adapters |
| **One result per family** in the schema (`families.<family>.events[]`, all families required) | Completeness is enforced structurally: missing family = incomplete response | Flat event list plus separate coverage field |
| **One canonical JSON per call** (`runs/<run>/<call>.json`) | The same structure feeds UI, metrics, and text explanations | Multiple report formats |
| **UI: HTML page served using only the standard library** (like `server.py`) | No installation, runs anywhere; an endpoint recomputes the threshold from cache | Streamlit dependency, frontend framework |
| Per-stage JSON disk cache (`cache/`) | Changing threshold, rules, or keywords does not require another model request | SQLite (deferred) |
| No audio preprocessing | WAVs are already mono, 16 kHz, and around 8 MB; the ASR server handles VAD and denoising | Local filters, normalization, denoising |
| Template-based explanations and summaries | No second LLM request, less output to validate | LLM-generated explanations |
| Trigger API **optional**, after the mandatory path | The README marks it optional; audio clips are mandatory | Trigger API in the main path |
| UI limited to threshold, keywords, and decision details | Editing predicates changes the extractor's task and requires re-extraction: it cannot be recomputed from cache | General policy editor in the UI |
| Deferred: RAG, cross-call memory, agent, verifier, diarization | Add only when justified by a metric | — |

## 4. Architecture

```
data/Transkript/*.txt  (development)    data/Audio/*.wav  (step 5)
        │                                         │ models.transcribe (router, asr profile)
        ▼                                         ▼
  segments [{id, speaker?, start?, end?, text, avg_logprob?}]
        │
        ├── extract.keyword_hits → highlights and counts (Stichwortliste.json), not decisive
        │
        ▼ extract.extract → 1 models.chat_json request (chat profile), JSON schema, cache
  {"families": {<family>: {"events": [{object_type?, evidence?, conditions: {pred: {state, evidence}}}]}}}
   ← from step 3: all families required; "events": [] = assessed, nothing to flag
        │
        ▼ decide.decide (pure code)
   1. Anchoring: every quotation must occur in its cited segment (ignoring case and punctuation).
      Exact match in the cited segment or in the window cited segment + neighbour (before or after);
      then approximate alignment in the same window (≥ grounding.min_similarity, identical negations),
      which reports the real transcript words. Record all involved IDs. Never search the entire call.
      → "true" or "false" without anchored evidence becomes unknown (downgraded)
   2. Three-valued family rules: any false → absent/no_alert; any unknown → review;
      all true → present
   3. Present: ASR-quality threshold on decisive conditions → alarm, otherwise review
   4. Call label = max(alarm > review > no_alert); template-based explanation
        ▼
  Canonical JSON per call → UI, metrics, text · (step 5) clips ±10 s from original WAV · (extra) Trigger API
```

**Review reasons** (the displayed label is always `review`, but the reason remains distinct):

| `reason` | When |
|---|---|
| `missing_policy_fact` | A predicate is `unknown` because the call does not establish it (open question = predicate text) |
| `technical_uncertainty` | Extraction failed or is incomplete (network, JSON, a family missing from the result), or a predicate was downgraded because its quotation is not anchored |
| `below_escalation_threshold` | The event is present, but a decisive condition has no quotation with ASR quality ≥ threshold |

**ASR-quality threshold** (`escalation.min_asr_quality`, final name from step 5). This is an **ASR-quality-based escalation threshold**, not the probability that the event or transcript is correct. Segment quality = `exp(avg_logprob)`.

| ASR diagnostics | Behavior |
|---|---|
| Available, above threshold | No downgrade |
| Available, below threshold | `review / below_escalation_threshold`, with the affected passage |
| Unavailable (text, or ASR without scores) | Report "quality unavailable". Never substitute a perfect score and do not downgrade |

Limitations: it cannot detect a confidently mistranscribed negation or a correctly transcribed but misinterpreted passage. It applies only to quotations supporting decisive conditions, never supplementary quotations.

**What is recomputed after a change:**

| Change | Recomputed | Reused from cache |
|---|---|---|
| Threshold | `decide` | Transcription, extraction |
| Keyword list | Keyword matching (milliseconds) + report | Transcription, extraction |
| Rules in `decide.py` | `decide` | Transcription, extraction |
| Predicates, prompt, or chat model | Extraction + `decide` | Transcription |
| ASR model | Everything | — |

## 5. Files

| File | Status | Role |
|---|---|---|
| `.env.example` → `.env` | ✅ | Keys and profile overrides; `.env` is not tracked |
| `config/models.json` | ✅ | Profiles: `claude_code` (default chat, Sonnet 5), `openai`, `private`, `ollama`, `speaches`; defaults for `chat` and `asr` |
| `config/policies.json` | ✅ | Families, predicates (text = prompt definition + open question), threshold |
| `data/Stichwortliste.json` | Unchanged | Keywords (`de` + `gsw`), whole-phrase matching |
| `callguard/models.py` | ✅ | Router: `check`, `chat_json`, `transcribe`; `openai_compatible` and `claude_cli` adapters; retries; warning for non-self-hostable profiles |
| `callguard/extract.py` | ✅ (`extract-3`) | Script parser, keywords, prompt + schema (`PROMPT_VERSION`), extraction cache |
| `callguard/decide.py` | ✅ | Anchoring (exact + approximate alignment), rules, threshold, reasons, canonical result, `explain()` |
| `callguard/pipeline.py` | ✅ | CLI `text` / `eval` (`--smoke`, `--only A,B`) / `freeze`, three-class metrics, `runs/eval-<ts>-<mode>/<call>.json` + `summary.json` |
| `tests/test_models.py`, `tests/test_decide.py` | ✅ | Router with mock HTTP server and mock `claude` command; rule engine on synthetic cases; keywords compared against all annotations; cache |
| `tests/test_policies.py` + `tests/fixtures/` | ✅ | Policies on real dataset. (1) **Oracle** = `fixtures/oracle.json`: manually written correct extractions for all 21 scripts, quoting actual turns; must produce expected assessment and cite an evidence turn. (2) **Replay** = `fixtures/replay/<model>/`: real extractions rerun through current rules, with `KNOWN_MISSES` for known errors. (3) Configuration/fixture consistency |
| `callguard/asr.py` | ✅ | Audio → segments via router (`whisper-1`, verbatim prompt), cache on content hash + profile + format (`cache/asr/`), exports to `data/Transcriptions/` (JSON + Markdown); roles and speakers kept as `UNKNOWN` (no diarization). `python3 -m callguard.asr [path] --workers N` |
| `data/Transcriptions/` | ✅ | The 42 Whisper transcriptions (verified 2026-10-10, see step 5) |
| `dashboard/` | ✅ | Review dashboard (DaisyUI/Tailwind from CDN). Live through `callguard/ui.py`; falls back to `mock-data.js` when served statically |
| `callguard/evidence.py` | ✅ | ±10 s clips from the original WAV with the standard-library `wave` module |
| `callguard/ui.py` | ✅ | Dashboard server + read-only API over cached results, audio with Range, clip download (`python3 -m callguard.ui`) |
| `tests/test_ui.py` | ✅ | Clips, mapping to dashboard fields, server routes, path safety |

**Keys and secrets:** in a `.env` file at the root, **never tracked** (`.gitignore` and `.dockerignore`). Run `cp .env.example .env`, then set `OPENAI_API_KEY=...`. The router (`models.load_env`) reads it at startup using only the standard library; existing shell variables take precedence. `.env.example` is the tracked template containing all supported variables.

**Commands:**

```bash
python3 -m unittest discover -s tests           # all tests, no network or keys (seconds)
python3 -m callguard.models                      # show active profiles
# default chat: Sonnet 5 through Claude Code CLI (claude on PATH and active login; no key)
# another model: CALLGUARD_CHAT=openai|private|ollama (+ profile URL and key variables)
python3 -m callguard.pipeline text data/Transkript/Stufe1_D02.txt
python3 -m callguard.pipeline eval --workers 4               # 21 scripts with speaker labels (~4 min)
python3 -m callguard.pipeline eval --workers 4 --no-speakers # speaker-free diagnostic
python3 -m callguard.pipeline eval --smoke --workers 5       # five-call regression set
python3 -m callguard.pipeline eval --only C09,D02            # selected calls
python3 -m callguard.pipeline audio data/Audio/Stufe1_D02-K1.wav   # one call from audio
python3 -m callguard.pipeline eval --audio --workers 6       # 42 WAVs (Whisper transcripts are cached)
python3 -m callguard.pipeline freeze [--no-speakers]         # freeze cached extractions as fixtures
python3 -m callguard.ui                                       # dashboard on http://127.0.0.1:8090
```

**`claude_cli` adapter:** executes `claude -p --model claude-sonnet-5 --output-format json --json-schema <schema> --system-prompt <prompt> --tools "" --no-session-persistence --setting-sources ""`, with the transcript on stdin, in a temporary directory. Thus no tools, user hooks or plugins, CLAUDE.md, or project memory. Reads `structured_output` from the response. Notes:
- Do not use `--bare`: it requires `ANTHROPIC_API_KEY` and does not work with OAuth login.
- Temperature cannot be set, so repeatability depends on the cache (`cache/extract/`).
- Chat only: ASR requires an `openai_compatible` profile.
- Indicative cost: about USD 0.01–0.03 and about 30 s per call.

**Two testing levels:**

| Level | When | What |
|---|---|---|
| **During development** | After every change | `unittest` (rules, oracle, existing fixture replay) + `eval --smoke` (five-call regression set, step 4) |
| **Release evaluation** | Once, before submission | Full `eval` on text and audio, one final `freeze`, update `KNOWN_MISSES` |

No `freeze` or full evaluations at intermediate steps. Previously frozen fixtures (`extract-1`, event-list format) **are not modified**: tests convert them to the per-family format and check how current rules handle historical extractions. `KNOWN_MISSES` documents known errors; the `eval` report continues to show them.

## 6. Steps

Each step leaves a working pipeline. Close a step only when its acceptance criteria pass. The mandatory path (audio, clips, metrics) takes priority over further text refinement.

### Step 0: data analysis and design ✅
### Step 1: model router ✅
### Step 2: text pipeline (policies, extraction, decisions, CLI, tests on real dataset) ✅
- First real execution with Sonnet 5: results in section 8. Oracle covers 21/21 scripts; replay covers 42 extractions.

### Step 3: correctness fixes ✅ (2026-10-10)
- Implemented: evidence required for `false` as well as `true`; prompt rules ("missing/uncertain → unknown", ambiguous object → unknown, withdrawal or refusal is never evidence of a request, quotations of 3–10 consecutive words copied exactly); contiguous-window anchoring; per-family schema; leaner schema (no `summary`, template summary); synthetic cases trade→review, disclosure→review, documentation→no_alert; three-class metrics; canonical JSON per call; `--smoke`; `--only A,B,C`. Versions: `extract-3`, `policies-0.3`.
- `concealment_requested` refined: a caller protecting their own unrelated data (masking fields, not sending personal details) does not count (fixed C01).
- **Approximate quotation alignment** (`decide.Grounder`, `grounding.min_similarity = 0.92`). The model normalises Swiss German while copying ("hundertachtgtuusig" for "hundertachtzgtuusig", "Sie händ" for "händ Sie", dropped words); neither shorter quotations nor stricter prompts removed it, the errors only moved to other calls. The quotation only serves to **locate** the passage:
  - exact match first, then a word-level alignment (`difflib`) restricted to the cited segment and one neighbour;
  - rejected below 0.92 or when negations (nöd, nicht, kei, nie…) differ. Measured on 2026-10-10: observed copy slips ≥ 0.943; quotations with a wrong meaning ≤ 0.864, including a flipped negation at 0.864 that the threshold alone would not have stopped;
  - the evidence shows **the real transcript words**, never the model's paraphrase, with `match` = similarity (`"exact"` otherwise);
  - result: 6 of 311 quotations aligned approximately (~2%), all correct on reading.
- `extract-1` fixtures unchanged; the replay showed C12 without speakers is now correct (removed from `KNOWN_MISSES`).
- `eval` folders carry the mode (`runs/eval-<ts>-speakers|nospeakers/`), so two evaluations in the same second no longer collide.
- 63 passing tests.

### Step 4: smoke test (development regression and check for every model change) ✅ (`eval --smoke`, 5/5 with Sonnet 5)
- `eval --smoke`: five representative calls: D02 (trade alarm), D03 (harmless trade with keywords), C09 (active code), C10 (expired code), C17 (missing-fact review).
- Entry check for **every** model or profile change (Claude, local, API): smoke test first, then full evaluation.
- **Acceptance:** 5/5 with Sonnet 5, in a few minutes.

### Step 5: audio and clips ✅ (ASR = `whisper-1`; requires `OPENAI_API_KEY` in `.env`)
- **2026-10-10 test on `Stufe1_D02-K1.wav` (clean, 10 s, ~USD 0.02):** key and profile work; 25 segments with `start`/`end`, `avg_logprob`, `no_speech_prob`, `compression_ratio`. Observations:
  - Whisper **renders speech in Standard German** ("Diese sind noch nicht publiziert", "Finanzabteilung", "vertraulich"): D02's decisive facts remain readable.
  - Typical errors: "Under eus" → "Und auch alles" (K05 keyword lost), "Übersicht" → "Reposicht", "Aktie" → "Akte", "Schwöschter" → "Schwestern".
  - **No speakers** and **segments different from turns** (T004 becomes s005–s007): confirms the need for contiguous-window anchoring (step 3.3).
  - **Quality `exp(avg_logprob)` ≈ 0.75–0.78 on clean audio**, with repeated values across consecutive segment blocks: `whisper-1` appears to estimate it per decoding window (~30 s), not per sentence. Default threshold 0.6 must be recalibrated on noisy versions; granularity limits its ability to distinguish individual passages.
- **Done (outside this session, verified 2026-10-10):** `callguard/asr.py` and all **42 transcriptions** in `data/Transcriptions/` and `cache/asr/`. Checks: one file per WAV, `source_sha256` matches every WAV, all from `whisper-1`, timestamps cover the full audio (last segment end ≈ duration), `avg_logprob` present, no leak of the verbatim prompt into the text, word counts close to the scripts (D02: 326 vs 327).
  - Despite the verbatim prompt, Whisper still **outputs Standard German** ("hier ist Claudia frei", "Akte" for "Aktie"). Segments are sentence-level (~4 s), finer than turns.
  - Keyword hits drop versus scripts, more on noisy audio (Stufe1, script / clean / noisy): D02 7/6/3, D03 8/5/4, D04 6/5/5, D06 8/5/4, D07 3/3/2, D09 2/2/1. Confirms keywords cannot drive decisions.
  - Quality `exp(avg_logprob)`: average 0.74–0.81 per call, clean and noisy almost identical (e.g. D02 0.77/0.74); minima down to 0.47. The heuristic barely separates noisy from clean audio: measure before relying on it.
- **Done 2026-10-10:** `pipeline.py audio FILE` and `eval --audio` (expected label from the filename only in eval; run folders `runs/eval-<ts>-audio/`; metrics split clean K1/K3 vs noisy K2/K4; `extraction_errors` counted apart from classification errors). `callguard/evidence.py` cuts ±10 s clips from the original WAV.
- **Fixes found on audio:** (1) the model cites Whisper ids without leading zeros (`s17` for `s017`): `Grounder.resolve` maps them by prefix and number; (2) "timing alone proves no intent" was read as `false` for `evasion_purpose` (C20 → no_alert): predicate and prompt now say "not proven / not established → unknown" (`extract-4`, `policies-0.4`); (3) an account session limit of the Claude CLI surfaced as `technical_uncertainty` on 10 calls: correct fallback, reported separately as `extraction_errors`.
- **Result:** 42/42 on audio (see section 8). Acceptance met.

### Step 6: review dashboard ✅ (2026-10-10)
- **Decision:** keep the existing `dashboard/` (DaisyUI/Tailwind) and connect it; `callguard/ui.py` serves it with the standard library: `python3 -m callguard.ui` → <http://127.0.0.1:8090>.
- API `GET /api/data`: reads only `data/Transcriptions/` and the extraction cache (never calls a model, no key needed), recomputes `decide` for the three threshold presets in `policies.json` (`escalation.presets`: sensitive 0.5, balanced 0.6, conservative 0.75; `default_preset`), returns calls, keyword groups (from `Stichwortliste.json`) and the calls not analysed yet (`pending`, shown in the header). `GET /audio/<file>.wav` with Range support; `GET /clip/<file>.wav?start=&end=` downloads a ±10 s clip.
- Dashboard: live data with fallback to `mock-data.js` when the API is absent (the README's static mode still works); real audio player, jump buttons start 10 s before each supporting passage, clip download per passage; threshold buttons show their numeric value; "confidence" replaced by the ASR quality of the decisive passage (labelled as a heuristic, not a probability); review explanations for each reason (open question, threshold routing, technical uncertainty).
- Keyword toggles change highlights only, never classifications (as the dashboard already stated).
- Checked in a browser (Playwright): live data, threshold switch D02-K2 Alarm → Review at "conservative", audio playback and seek, layout fix for long evidence lists, mock fallback.

### Step 7: complete audio evaluation and audit 🟡
- ✅ `eval --audio` on all 42 WAVs: 42/42.
- ✅ Threshold sensitivity (2026-10-10, 42 audio calls): sensitive 0.5 and balanced 0.6 give identical results (42/42, 16 alarms); conservative 0.75 moves one call, `Stufe1_D02-K2` (noisy, quality 0.72), from Alarm to Review (41/42). The threshold works but has little leverage, because Whisper's quality score barely separates clean and noisy audio (step 5).
- **Manual audit** of decisive evidence on demo calls and selected alarm/review clips: every decisive condition must be supported by a passage that actually establishes it. Separate from the automatic metric: `evidence_hit` only measures whether an expected turn is cited, not whether the quotation is correct.

### Step 8: freeze and demo ⏳
- Release evaluation (text + audio), one final `freeze`, frozen configuration, results in section 8.
- Demo (README "Sunday"): one audio-to-alarm call with clips, a threshold or keyword change with visible effect, a borderline case (D03: keywords present but no_alert), and metrics.

### Extras: only after step 8 or when justified by metrics
- **Trigger API** (`server.py`, `POST /triggers`) with clip links.
- **Targeted verifier** for contradictory alarms; **targeted retranscription** of low-quality decisive segments; **diarization**; **cross-call patterns**; **notifications**.

## 7. Known risks

- **Extractor recall:** if the LLM does not create an event, the result is silent `no_alert`. Rules cannot recover it; per-family schema exposes only incomplete responses. Measure on the pairs.
- **Interpretation:** anchoring verifies that a quotation exists, not that it establishes the condition (see D02 T012). Hence the step 7 audit.
- **ASR:** Whisper tends to produce Standard German, so quotations and `gsw` keywords differ from scripts (`de` keywords cover this), and segments differ from turns.
- **Swiss German ASR:** `whisper-1` quality on these clean/noisy recordings is not yet measured. Compare against scripts (WER, evaluation only) in step 5.
- **Threshold:** an unvalidated ASR-quality heuristic; measure on clean/noisy pairs before presenting it.
- **Reproducibility:** Claude does not expose a temperature setting; model/prompt-version caching makes executions repeatable.
- **Model change:** another model may perform worse; every change therefore goes through the smoke test.
- **Current results** come from the development model on scripts, not the complete audio pipeline. The 42 fixtures are 21 dialogues × 2 text configurations, not 42 audio evaluations.

## 8. Results log

| Date | Profile / model | Mode | Accuracy | False alarms | Missed (strict/lenient) | Notes |
|---|---|---|---|---|---|---|
| 2026-10-10 | claude_code / claude-sonnet-5 | Text, with speakers | 20/21 (0.952) | 1/13 | 0/8 · 0/8 | Error: C19 (review→alarm). `evidence_hit` 11/11, but D02 T012 quotation is wrong (traceability error). `runs/eval-20261010-021526.json` |
| 2026-10-10 | claude_code / claude-sonnet-5 + whisper-1, extract-4, policies-0.4 | **Audio, 42 WAVs** | **42/42 (1.000)** | 0/26 | 0/16 · 0/16 | Clean 21/21, noisy 21/21; review 6/42 (C17, C19, C20 × 2); 0 extraction errors. `runs/eval-20261010-090616-audio/` |
| 2026-10-10 | claude_code / claude-sonnet-5, extract-4, policies-0.4 | Text, with and without speakers | 21/21 · 21/21 | 0/13 | 0/8 · 0/8 | Regression check after the audio fixes. `runs/eval-20261010-090852-speakers/`, `runs/eval-20261010-091145-nospeakers/` |
| 2026-10-10 | claude_code / claude-sonnet-5 + whisper-1, extract-3, policies-0.3 | Audio, 42 WAVs | 40/42 (0.952) | 0/26 | 0/16 · 0/16 | C20 ×2 review→no_alert ("timing proves no intent" read as false; fixed in extract-4). `runs/eval-20261010-085453-audio/` |
| 2026-10-10 | claude_code / claude-sonnet-5, extract-3, policies-0.3 | Text, with speakers | **21/21 (1.000)** | 0/13 | 0/8 · 0/8 | No errors; review 3/21 (C17, C19, C20). `runs/eval-20261010-073207/` |
| 2026-10-10 | claude_code / claude-sonnet-5, extract-3, policies-0.3 | Text, `--no-speakers` | **21/21 (1.000)** | 0/13 | 0/8 · 0/8 | No errors; 6/311 quotations aligned approximately. `runs/eval-20261010-073213/` |
| 2026-10-10 | claude_code / claude-sonnet-5, extract-2, policies-0.1 | Text, with speakers | 20/21 (0.952) | 0/13 | 0/8 · 0/8 | Error: C03 (no_alert→technical review, miscopied quotation). `runs/eval-20261010-031414/` |
| 2026-10-10 | claude_code / claude-sonnet-5, extract-2, policies-0.1 | Text, `--no-speakers` | 20/21 (0.952) | 0/13 | 0/8 · 0/8 | Error: C01 (no_alert→review, fixed by policies-0.2). `runs/eval-20261010-031806/` |
| 2026-10-10 | claude_code / claude-sonnet-5 | Text, `--no-speakers` | 18/21 (0.857) | 1/13 | 1/8 · 0/8 | Errors: C19 (review→alarm), C12 (alarm→technical review, model typo), C20 (review→no_alert). `runs/eval-20261010-021950.json` |

## 9. Plan review of 2026-10-10 (accepted proposals)

| Proposal | Outcome |
|---|---|
| Move migration to self-hosted models to the next step | **Not accepted:** team uses Claude. Replaced by smoke testing for every model change (step 4) |
| `false` requires evidence; prompt distinguishes "missing → unknown, excluded → false" | Accepted (step 3.1–3.2), checked on data: no additional cost with Sonnet |
| Threshold described as ASR-quality heuristic; unavailable quality flagged, never 1.0 | Accepted (section 4, step 5) |
| Limited reference repair (adjacent/contiguous, recorded) | Accepted, then simplified to contiguous window (step 3.3) |
| D02 T012 quotation treated as traceability error, not minor; decisive-evidence audit | Accepted (section 8, step 7) |
| `family_assessments` for completeness | Accepted, then replaced by per-family schema (step 3.4) |
| Fast loop and release gate; immutable versioned fixtures | Accepted, then simplified: lightweight development tests, one final `freeze` (section 5) |
| Optional Trigger API; no predicate editor in UI | Accepted (step 6 and Extras) |
| Correct description of keyword-change recomputation | Accepted (section 4) |
| `--no-speakers` diagnostic only | Accepted (step 5) |
| Leaner schema | Accepted (step 3.5) |
| Order: smoke test → audio and clips → minimal page → audio evaluation → demo → extras | Accepted, without migration step |
| Synthetic cases for missing combinations; three-class metrics | Accepted (step 3.6–3.7) |
| MVP simplification: frozen adapters, one result per family, lightweight development tests, contiguous-window anchoring, one canonical JSON, immediate UI choice | Accepted. Alternative profiles remain switching options; standard-library UI |
| ASR selection (open decision) | Closed: OpenAI `whisper-1`, estimated ~USD 1.15 per full pass from USD 75 credits |
