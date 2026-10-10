# Canonical result JSON — MVP

Each `runs/eval-*/<recording_id>.json` is a decision artefact. The existing
decision fields (`label`, `reasons`, `events`, conditions and grounded evidence)
remain stable. New release artefacts add `schema_version:
"callguard.canonical-result.v1"`; older cache results without it remain readable.

## Traceability envelope

| Field | Meaning |
| --- | --- |
| `schema_version` | Version of this release-result envelope. |
| `run_id` | Folder/run that emitted the result; it also appears in `manifest.json`. |
| `policy_version` | Alias of the existing `policies_version`; both are retained for compatibility. |
| `recording` | Stable recording `id`, SHA-256 (or `null` when no audio exists), repository-relative source `path`, and duration when known. |
| `evaluated_rules` | Stable policy keys (`policy.<family>` and `policy.<family>.<condition>`), event index and outcome. It records every evaluated condition separately from `alarm_generating_rules`. |
| `alarm_generating_rules` | Only family decisions that actually produced an `alarm`; an empty list does not mean that no policy was evaluated. |
| `uncertainty` | Separates missing call facts from extraction/schema, grounding and provider problems. |
| `explanation` | Deterministic rendering of existing decision/event content and open questions; no extra LLM call. |
| `rationale` | Short post-decision template with the deciding event, existing reason codes, and pointers to the grounded conditions/evidence that justify the label. It never adds facts or LLM-written prose. |
| `asr_quality` | States that the ASR value is an uncalibrated transcription-quality heuristic, not a fraud probability. Unavailable per-passage quality remains `null`. |

Each grounded evidence reference keeps its `segment_ids`, `quote`, `start`,
`end` and `quality`; audio releases additionally contain `clip_start` and
`clip_end`. Clip bounds use the configured surrounding context and are clamped
to the recording duration.

## Decision chain

```text
label → evaluated_rules / condition state → grounded evidence → segment IDs,
timestamps and clip bounds → recording ID/hash and run manifest
```

`state` is always `true`, `false`, or `unknown`. `false` means explicitly
excluded by grounded context; `unknown` is not a negative fact. Technical
problems remain distinct under `uncertainty.technical` and may coexist with a
`review` label.

## Deterministic rationale

`rationale` is produced after the deterministic decision and has this stable
shape:

```json
{"text":"…","template":"…","reason_codes":["…"],"event_index":0,"conditions":[{"rule_id":"policy.access.secret_active","state":"unknown"}],"evidence":[{"event_index":0,"condition":"secret_active","segment_ids":["s007"],"start":42.1,"end":44.0,"role":"relevant_context"}]}
```

`conditions` and `evidence` are pointers to the facts already stored under
`events`; they do not restate, supplement, or override them. `reason_codes`
are the existing call/event review reasons. In particular, an empty array for
an alarm or ordinary no-alert does not mean that a new reason code was
invented. An evidence pointer is tagged `supports_condition`,
`excludes_condition`, or `relevant_context`; the last form accompanies an
unknown review fact and is not proof of that absent fact.

| Final label | Template basis |
| --- | --- |
| `alarm` | All required conditions of the deciding event are `true`, with their grounded evidence pointers. |
| `no_alert` | A `false` required condition and its evidence, or an explicit `no_candidate_event` when no event exists. Classification-only number events have their own template. |
| `review` | Unknown required fact / actor role, or the existing technical / ASR-threshold reason code. A technical review does not claim a missing business fact. |
| `null` | An unresolved technical failure; it is never rendered as no-alert. |

The templates are intentionally short. Full facts, quotations and timestamps
remain in the evidence-bearing event and condition objects, preserving the
separation between extracted facts and deterministic policy rationale.

## Minimal examples

These are structural examples only; they are not dataset results.

```json
{"schema_version":"callguard.canonical-result.v1","label":"alarm","run_id":"eval-…","policy_version":"policies-0.5","recording":{"id":"Call-A","sha256":"…","path":"data/Audio/Call-A.wav","duration_s":120.0},"alarm_generating_rules":[{"rule_id":"policy.access","event_index":0}],"evaluated_rules":[{"rule_id":"policy.access.secret_active","event_index":0,"kind":"condition","outcome":"true","alarm_role":"required_condition"}],"rationale":{"text":"Alarm: all required conditions are supported by grounded evidence.","template":"alarm_supported_conditions","reason_codes":[],"event_index":0,"conditions":[{"rule_id":"policy.access.secret_active","state":"true"}],"evidence":[{"event_index":0,"condition":"secret_active","segment_ids":["s007"],"start":42.1,"end":44.0,"role":"supports_condition"}]}}
```

```json
{"schema_version":"callguard.canonical-result.v1","label":"review","reasons":["missing_policy_fact"],"rationale":{"text":"Review: a required fact is not established in the call.","template":"review_missing_required_fact","reason_codes":["missing_policy_fact"],"event_index":0,"conditions":[{"rule_id":"policy.access.secret_active","state":"unknown"}],"evidence":[]},"explanation":{"open_questions":["Was the value valid for an active login?"]},"uncertainty":{"missing_call_facts":["Was the value valid for an active login?"],"technical":{"extraction_or_schema":[],"grounding":[],"provider":[]}}}
```

```json
{"schema_version":"callguard.canonical-result.v1","label":"no_alert","alarm_generating_rules":[],"evaluated_rules":[{"rule_id":"policy.trade.own_trade_request","event_index":0,"kind":"condition","outcome":"false","alarm_role":"required_condition"}],"rationale":{"text":"No alert: a required condition is explicitly excluded by grounded evidence.","template":"no_alert_required_condition_excluded","reason_codes":[],"event_index":0,"conditions":[{"rule_id":"policy.trade.own_trade_request","state":"false"}],"evidence":[{"event_index":0,"condition":"own_trade_request","segment_ids":["s003"],"start":12.0,"end":14.2,"role":"excludes_condition"}]}}
```

`validate_canonical_result(result)` checks the stable core and deliberately
accepts pre-envelope artifacts. `validate_canonical_result(result, release=True)`
requires the envelope before a new run writes it.
