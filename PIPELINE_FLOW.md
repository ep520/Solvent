# CallGuard pipeline flow

This is the operational flow described in [PIPELINE.md](PIPELINE.md). The published default run is `runs/eval-20261011-070834-audio`, containing 42 authentic calls.

## Inference flow

```mermaid
flowchart TD
    WAV["WAV recording"] --> ASR["callguard/asr.py<br/>whisper_local or another configured ASR profile"]
    ASR --> SEGMENTS["Timestamped ASR segments<br/>id · start · end · text · quality"]
    SEGMENTS --> EXTRACT["callguard/extract.py<br/>qwen3_fast or another configured chat profile"]
    POLICIES["config/policies.json<br/>policy families · predicates · thresholds"] --> EXTRACT
    EXTRACT --> FACTS["Structured facts<br/>true · false · unknown + quoted segment evidence"]
    FACTS --> DECIDE["callguard/decide.py<br/>strict evidence grounding + deterministic rules"]
    POLICIES --> DECIDE
    DECIDE --> RESULT["Canonical call result<br/>alarm · review · no_alert + reasons"]
    RESULT --> RUN["runs/eval-<timestamp>-audio/<br/>per-call JSON, metadata, and run-owned audio"]
    RUN --> UI["callguard/ui.py<br/>Dashboard"]
```

Keyword matching runs over the transcript for highlighting and coverage only. It never filters the input before extraction and never changes a decision.

## Decision flow

```mermaid
flowchart TD
    CONDITION["Condition: state + segment ID + quote"] --> ANCHOR{"Quote anchors exactly<br/>to the cited segment(s)?"}
    ANCHOR -- No --> UNKNOWN["Unknown<br/>grounding issue"]
    ANCHOR -- Yes --> RULES{"Required conditions"}
    RULES -- "any false" --> NOALERT["No alert"]
    RULES -- "any unknown" --> REVIEW["Review<br/>missing fact, role uncertainty, or technical uncertainty"]
    RULES -- "all true" --> QUALITY{"Decisive evidence meets<br/>the ASR-quality threshold?"}
    QUALITY -- Yes --> ALARM["Alarm"]
    QUALITY -- No --> LOWQUALITY["Review<br/>below escalation threshold"]
    UNKNOWN --> REVIEW
```

The call label is the highest severity across all events. Review settings offer fixed thresholds of 0.6, 0.7, and 0.8; changing one recomputes decisions over stored facts and does not rerun ASR or extraction.

## Dashboard data flow

```mermaid
flowchart LR
    POINTER["runs/_default_run.txt"] --> RUN["runs/eval-20261011-070834-audio"]
    RUN --> RESULTS["42 canonical results<br/>label + events"]
    RUN --> AUDIO["42 run-owned WAV files"]
    RUN --> META["manifest · summary · index · checksums"]
    RESULTS --> API["/api/data"]
    AUDIO --> PLAYBACK["/audio/<call>.wav"]
    META --> FILTER["Metadata is not a call result"]
    API --> DASHBOARD["localhost:8090"]
    PLAYBACK --> DASHBOARD
```

On a fresh clone, `python3 -m callguard.ui` follows the pointer and opens this committed run. It therefore displays the processed list and permits evidence and audio playback without `data/Audio`, model endpoints, or a local cache.
