# Context, review and audio audit

Status: 2026-10-10. This is an offline/code and cached-artifact audit. It does not claim a new model
evaluation or an audio listening review.

## Scope and method

- Inspected the supplied scripts and gold annotations, current policy/decision code, ASR and extraction paths,
  existing audio replay `runs/eval-20261010-164409-audio/`, and its canonical per-recording JSON.
- The replay is a cache replay (`extract-6`); it verifies existing artifacts and deterministic grounding, not
  fresh ASR/LLM performance. Its `extract-6` cache metadata does not preserve the rendered model input, so it
  cannot retrospectively prove which passages an old model request received.
- Current code sends `view=None` from `run_segments`: every LLM request receives the complete transcript.
  Snippets are generated separately as reviewer highlights. `tests/test_context_audit.py` locks that contract by
  checking an early and a late C16 passage in the actual prompt and the `view=None` call.
- Gold is read by `dataset_contract()` and `gold()` only after inference for scoring. `run_audio()` obtains
  segments only through `asr.transcribe()` and passes them to extraction. The new isolation test makes any
  `gold()` call fail.

## Stufe2: context pairs

The pairs are derived from their matching headings, annotations and altered early context, not from a rule on
the filename. “Observed” is both clean/noisy rows in the cached audio replay unless noted. Evidence IDs are
ASR segment IDs, not script turns.

| Pair | Differing clue and policy field | Expected → observed | Grounded evidence in replay |
| --- | --- | --- | --- |
| C01 / C02 | Card-field origin vs invoice-reference origin; `numbers.object_type` | `full_payment_card_number` / `invoice_reference`; both `no_alert` → both `no_alert` | C01: `s012`/`s031`, card-front wording; C02: `s004`/`s010`, “Rechnungsreferenz” |
| C03 / C04 | Masked payment-card tail vs hotel-reservation tail; `numbers.object_type` | `masked_payment_card_digits` / `reservation_reference`; both `no_alert` → both `no_alert` | C03: `s041`/`s060`, last four digits; C04: `s007`/`s009`, reservation reference |
| C05 / C06 | Published source vs project-only source; `trade.information_nonpublic` | `false` / `true`; `no_alert` / `alarm` → same | C05: `s017`/`s018`, published/everyone can read; C06: `s018`/`s020`, project team/no public source |
| C09 / C10 | Current login flow vs expired help sample; `access.secret_active` | `true` / `false`; `alarm` / `no_alert` → same | C09: `s007`/`s010`, active flow; C10: `s006`/`s008`, expired sample/cannot be used |
| C11 / C12 | Authority checked vs explicitly absent; `disclosure.authority_absent` | `false` / `true`; `no_alert` / `alarm` → same | C11: `s008`/`s009`, authority recorded/checked; C12: `s008`/`s010`, no right to information |
| C15 / C16 | Independent invoices and stated due-date purpose vs one invoice and avoidance of approval; `splitting.movements_connected`, `splitting.evasion_purpose` | `false,false` / `true,true`; `no_alert` / `alarm` → same | C15: `s006`/`s008`, independent invoices/right due dates; C16: `s006`/`s007`, one invoice/avoid approval |

The classification-only number pairs deliberately retain `no_alert`; their context changes the object type,
not the final label. The offline regression mutates only the decisive fact in a complete extraction fixture and
checks the predicate state, grounded evidence and expected label. For the access pair it also removes the clue:
`secret_active` becomes `unknown` and the label is `review`, not automatically the opposite `no_alert`.

## Stufe3: semantic review versus technical review

| Case | Necessary fact not established | Cached observed result | Evidence/diagnostic |
| --- | --- | --- | --- |
| C17 | `access.secret_active`: the read digits cannot be tied to an active flow | both takes: `review`, `missing_policy_fact` | `value_spoken` and `access_secret` are true; `secret_active` is unknown; no grounding issue |
| C19 | `documentation.fact_relevant`: the erased line may be a financing obligation or private matter | both takes: `review`, `missing_policy_fact` | concealment and recipient are true; relevance is unknown; no grounding issue |
| C20 | `splitting.movements_connected` and `splitting.evasion_purpose` | both takes: `review`, `missing_policy_fact` | split request is true; both necessary facts are unknown; no grounding issue |

The new tests keep three routes separate:

- missing policy fact: unknown condition, `missing_policy_fact`, and its exact policy question;
- missing actor role: unknown role-dependent condition and `actor_role_unknown`, not a provider error;
- malformed/ungrounded evidence: condition is downgraded to unknown and yields `technical_uncertainty`.

Thus a technical `review` can be label-coincident with Stufe3 gold but is not treated as a correct semantic
review. The inspected cached Stufe3 rows have no technical or grounding failure.

## Script, ASR and listening checklist

Scripts and gold are reference material for scoring. They are not verified audio transcripts and must never be
used as model input, passage selection or inference fallback. Grounding proves only that a quote occurs in the
ASR transcript; semantic support and audio fidelity remain distinct checks.

Before calling audio fidelity verified, a reviewer should listen to both clean and noisy clips for:

1. C05/C06: published versus project-only source;
2. C09/C10: active login versus expired help sample;
3. C11/C12: authority confirmed versus absent;
4. C15/C16: invoice linkage and approval-avoidance purpose;
5. C17, C19 and C20: the precise missing fact, including negations and uncertainty;
6. each cited ASR segment: wording, speaker/actor attribution and timestamp clip boundary.

For every item record: audio heard, ASR wording faithful, semantic predicate support, gold/script consistency,
reviewer and date. All audio-fidelity entries are **pending** until this listening occurs.

## Minimal findings and follow-up

- Corrected README language: snippets no longer limit detection; the dashboard highlights them only. It now also
  distinguishes reference scripts from ASR transcripts.
- Existing replay results are useful artifact checks but cannot certify fresh-model performance or historical
  snippet coverage. Run an uncached/full evaluation with `extract-7` if such a claim is needed; do not reuse the
  cached 42/42 result as that measurement.
- No policy or gold was changed, and no dataset identifier is used by the decision path.
