# CallGuard pipeline flow

Diagrams for [`PIPELINE.md`](PIPELINE.md), which has the full explanation. One rule holds everywhere
below: the language model only reports facts with quotations; deterministic code makes every decision.

## 1. End-to-end flow

```mermaid
flowchart TD
    INBOX["ingest.py: watches data/Inbox/<br/>moves a stable WAV into data/Audio/"]
    WAV["Audio<br/>data/Audio/*.wav"]
    ASR_CACHE{"Transcribed already?<br/>(content hash, profile, model)"}
    INBOX --> WAV
    ASR["callguard/asr.py<br/>Whisper via the model router<br/>cache/asr/&lt;hash&gt;.json"]
    TRANSCRIPT["Segments: id · start · end · text · avg_logprob<br/>no speakers (no diarization)"]
    EXPORT["asr.export<br/>data/Transcriptions/*.json + .md<br/>(asr CLI or ingest)"]

    WAV --> ASR_CACHE
    ASR_CACHE -- No --> ASR --> TRANSCRIPT
    ASR_CACHE -- Yes --> TRANSCRIPT
    TRANSCRIPT -- "asr CLI or ingest" --> EXPORT

    TRANSCRIPT --> KEYWORDS["Stichwortliste.json<br/>keyword matchers"]
    KEYWORDS --> HIGHLIGHTS["Highlights + coverage counts<br/>display only, never decisive"]

    TRANSCRIPT --> SNIPPETS["snippets.view<br/>keywords + cue words + spoken digits<br/>nearby and linked context, max. 12 snippets"]
    KEYWORDS --> SNIPPETS
    SNIPPET_CONFIG["config/snippets.json<br/>cues · fuzzy · max_snippets"] --> SNIPPETS
    SNIPPETS --> DASHBOARD
    TRANSCRIPT --> EXTRACT_CACHE

    EXTRACT_CACHE{"Extraction cached?<br/>(model, prompt/schema, policies,<br/>rendered model input)"}
    POLICIES["config/policies.json<br/>families, predicates, enabled flags, threshold"] --> EXTRACT_CACHE
    EXTRACT_CACHE -- No --> LLM["One LLM request · extract.py<br/>model input, every enabled family"]
    EXTRACT_CACHE -- Yes --> FACTS
    LLM --> FACTS["Facts by family: events, actor, conditions<br/>true · false · unknown + segment id + quote"]

    FACTS --> DECIDE["decide.py (pure code)<br/>anchor quotes → 3-valued rules → ASR threshold"]
    POLICIES --> DECIDE
    HIGHLIGHTS --> RESULT
    DECIDE --> RESULT["Canonical result per call<br/>label · reasons · events · versions"]
    RESULT --> XAI["counterfactual.annotate (pure code)<br/>same 3-valued rule, re-applied to hypothetical states"]

    XAI --> FILES["runs/eval-&lt;ts&gt;-&lt;mode&gt;/&lt;call&gt;.json<br/>atomic per-call checkpoint + manifest/summary progress"]
    XAI --> CLIPS["evidence.py: ±10 s clips from the original WAV"]
    WAV --> CLIPS
    XAI --> DASHBOARD["ui.py dashboard / API"]
    EXPORT --> DASHBOARD
    CLIPS --> DASHBOARD
```

The dashboard also keeps two human-in-the-loop overlays: a reviewer can resolve a `Review` as alert or
no alert, producing a bounded calibration example for future model extraction, and can correct any ASR
segment. Neither action rewrites the ASR source or retrospectively changes a decision. A later pipeline
run reads the corrected text and the prior-review examples, producing a separately cached extraction.

For the MVP, every extraction receives the complete transcript within the context budget. Keyword,
cue-word and spoken-digit snippets add nearby and linked context only for dashboard highlighting and audio
navigation: they are **not** evidence, routing or a classification rule. Adaptive routing is deferred
until recall and latency have been measured against this full-transcript baseline. Every extraction covers
every *enabled* family (§3); changing the snippet configuration changes the highlights, not the cached
model input.

`ingest.py` is one way a WAV arrives; `pipeline eval --audio` reading `data/Audio/*.wav` directly is the
other — both join at `WAV`. The latter uses the ASR cache but does not itself export
`data/Transcriptions`; run `callguard.asr` or use the watched ingest flow when a dashboard export is
needed.

Evaluation creates its run folder before starting the batch. Each completed call is atomically published
as canonical JSON and updates the atomic `manifest.json` and partial `summary.json`; an interrupted run
therefore remains inspectable with `artifact_status: in_progress` or `interrupted`. Re-running evaluates
only the uncached ASR/extraction work, while retaining the completed checkpoint artefacts of the prior run.

## 2. Evidence anchoring and decision

```mermaid
flowchart TD
    COND["Condition: state + cited segment id + quote"]
    ID{"Segment id exists?"}
    EXACT{"Quote found verbatim in that segment,<br/>or starting there and ending in the next one?<br/>(case/whitespace folded, punctuation kept,<br/>no fuzzy match, no other neighbour)"}
    GROUNDED["Evidence accepted:<br/>real ASR text, exact segment ids and times"]
    UNANCHORED["true/false with no anchored evidence → unknown"]

    COND --> ID -- No --> UNANCHORED
    ID -- Yes --> EXACT -- Yes --> GROUNDED
    EXACT -- No --> UNANCHORED
    GROUNDED --> RULE
    UNANCHORED --> RULE{"All of the event's<br/>required conditions"}

    RULE -- "any false" --> ABSENT["Absent → no_alert"]
    RULE -- "any unknown" --> UNDET{"Why unknown?"}
    RULE -- "all true" --> PRESENT

    UNDET -- "extraction incomplete<br/>or evidence unanchored" --> TECH["review / technical_uncertainty"]
    UNDET -- "actor role required<br/>but not inferred" --> ROLE["review / actor_role_unknown"]
    UNDET -- "call doesn't establish it" --> MISSING["review / missing_policy_fact<br/>+ open question"]

    PRESENT{"Present"} --> SCORE{"Every decisive condition has a quote<br/>at/above the ASR-quality threshold?<br/>(no score at all → not blocking)"}
    SCORE -- Yes --> ALARM["Alarm"]
    SCORE -- No --> LOWQ["review / below_escalation_threshold"]

    ABSENT & TECH & ROLE & MISSING & ALARM & LOWQ --> CALL["Call label = highest-severity event<br/>alarm > review > no_alert"]
```

Thresholds: `sensitive = 0.50`, `balanced = 0.60` (default), `conservative = 0.75` — an ASR-quality
heuristic, not a fraud probability. A grounding or role failure only touches its own predicate; other
conditions and other events keep deciding independently.

## 3. Policy families

Every enabled family is required in the extraction schema; `"events": []` means "assessed, nothing
found". A bank can switch a whole family off (`policies.json`'s `enabled` flag) — it is then dropped
from the LLM request and treated as absent by configuration, not as a gap.

```mermaid
flowchart LR
    FACTS["Extraction"] --> GATE{"Family enabled?"}
    GATE -- No --> OFF["Not requested, not evaluated<br/>(bank configuration)"]
    GATE -- Yes --> FAMS["Trade · Access · Disclosure<br/>Documentation · Splitting · Numbers"]
    FAMS --> EVENTS["Per-event decisions (§2)"]
    EVENTS --> MAX["Call label = maximum severity"]
```

| Family | Required for an alarm |
|---|---|
| Trade | `own_trade_request`, `information_nonpublic`, `information_market_relevant`, `request_based_on_information` |
| Access | `value_spoken`, `access_secret`, `secret_active` |
| Disclosure | `advisor_disclosed`, `third_party_detail`, `authority_absent` |
| Documentation | `concealment_requested`, `fact_relevant`, `designated_recipient` |
| Splitting | `split_requested`, `movements_connected`, `evasion_purpose` |
| Numbers | none — classified only, never an alarm |

## 4. Model routing

```mermaid
flowchart LR
    TASK{"Task"} -- transcription --> ASRP["ASR profile"]
    TASK -- extraction --> CHATP["Chat profile"]
    ASRP & CHATP --> ROUTER["models.py"]
    ROUTER --> HTTP["openai_compatible: OpenAI, vLLM, Ollama,<br/>llama.cpp, speaches, private gateway"]
    ROUTER --> CLI["claude_cli: claude -p, JSON schema,<br/>no tools, no user settings"]

    SWITCH["Change profile"] --> SMOKE["eval --smoke"]
    SMOKE -- pass --> FULL["Full eval"]
    SMOKE -- fail --> FIX["Fix profile/model"] --> SMOKE
```

Current profiles: Claude Sonnet 5 (chat) and OpenAI `whisper-1` (ASR) — not self-hostable; every
`eval` run records this in its `manifest.json`. A self-hosted deployment is the same pipeline with
different profiles, gated by the same smoke test.

## 5. Dashboard

```mermaid
flowchart TD
    CMD{"Command"} -- "pipeline audio &lt;file&gt;" --> ONE["One call, printed explanation"]
    CMD -- "pipeline eval [--audio|--smoke]" --> MANY["Batch eval → files + manifest"]
    CMD -- "callguard.ingest" --> WATCH["Watch data/Inbox/, process each new WAV,<br/>report alarm/review to the Trigger API"]
    CMD -- "callguard.ui" --> UI["Dashboard, http://127.0.0.1:8090"]

    UI --> PER_CALL{"Per WAV: transcript export<br/>and cached extraction?"}
    PER_CALL -- No --> PENDING["Pending"]
    PER_CALL -- "Yes" --> RECOMPUTE["decide() for every threshold preset"]
    RECOMPUTE --> VIEW["Alarm / Review / No alert<br/>+ evidence, clips, conditions, keywords,<br/>what the model read"]
```

The dashboard never calls a model. A threshold switch only reruns `decide()` over cached facts. Saving
the keyword list updates matching, highlights and the *current* snippet view, but does not call a model
or alter the decision derived from an existing cache entry. Snippet changes do not invalidate the
full-transcript MVP baseline.
**Pending** means that the transcript export or compatible extraction cache is missing. A failed ASR or
extraction run is recorded by the CLI/ingest result and must be retried; it is never a compliance read.
