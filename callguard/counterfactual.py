"""Counterfactual explanations: what would have to be different for the decision to change?

Pure functions over an event that decide.evaluate_event already produced. Nothing here calls a model
or reads the transcript again. Each counterfactual re-applies the same three-valued rule as decide.py
to a hypothetical set of condition states, so an explanation can never disagree with the decision rule:

  any condition FALSE   -> no_alert
  any condition UNKNOWN -> review
  all conditions TRUE   -> alarm, if the decisive ASR quality reaches the escalation threshold

Two kinds of counterfactual are produced:
  - condition flips: "If <condition> were FALSE, the decision would be NO ALERT"
  - threshold:       "With the <preset> preset (min ASR quality q) this would be ALARM"
"""

LABEL_TEXT = {"alarm": "ALARM", "review": "REVIEW", "no_alert": "NO ALERT"}
STATE_TEXT = {"true": "TRUE", "false": "FALSE", "unknown": "UNKNOWN"}


def label_for(states, low_quality=False):
    """The decision rule from decide.evaluate_event, on plain condition states."""
    if "false" in states:
        return "no_alert"
    if "unknown" in states:
        return "review"
    return "review" if low_quality else "alarm"


def _question(name, family):
    return family["conditions"].get(name, name.replace("_", " "))


def condition_flips(event, family):
    """One counterfactual per (condition, alternative state) that changes the event's label.

    Flips that leave the label unchanged are omitted: they do not explain anything.
    Quality is held fixed: a hypothetical TRUE is assumed to come with the same ASR quality as
    the existing evidence, so a below-threshold event stays in review unless the flip excludes it.
    """
    conditions = event.get("conditions") or {}
    current = {n: c["state"] for n, c in conditions.items()}
    low = bool(event.get("low_quality"))
    out = []
    for name, state in current.items():
        for alt in ("true", "false", "unknown"):
            if alt == state:
                continue
            hypo = dict(current, **{name: alt})
            new = label_for(hypo.values(), low and alt != "false")
            if new == event["label"]:
                continue
            out.append({
                "kind": "condition",
                "condition": name,
                "question": _question(name, family),
                "from": state,
                "to": alt,
                "label": new,
                "text": f"If '{name.replace('_', ' ')}' were {STATE_TEXT[alt]} instead of {STATE_TEXT[state]}, "
                        f"the decision would be {LABEL_TEXT[new]}.",
            })
    # Most useful first: the open (UNKNOWN) facts that would settle a review, then exclusions, then the rest.
    order = {"alarm": 0, "no_alert": 1, "review": 2}
    return sorted(out, key=lambda c: (c["from"] != "unknown", order[c["label"]], c["condition"]))


def threshold_flips(event, presets):
    """For an established event: at which presets would the label differ?"""
    if event.get("status") != "present":
        return []
    qualities = [q for q in (event.get("quality") or {}).values() if q is not None]
    if not qualities:
        return []
    weakest = min(qualities)
    out = []
    for name, threshold in sorted(presets.items(), key=lambda kv: kv[1]):
        new = "alarm" if weakest >= threshold else "review"
        if new != event["label"]:
            out.append({"kind": "threshold", "preset": name, "threshold": threshold, "label": new,
                        "text": f"With the '{name}' preset (min ASR quality {threshold}) the decision would be "
                                f"{LABEL_TEXT[new]}: the weakest decisive passage has quality {weakest}."})
    return out


def for_event(event, policies):
    family = policies["families"][event["family"]]
    if not family.get("can_alarm", True) or not event.get("conditions"):
        return []  # numbers: classification only, nothing to flip
    presets = policies.get("escalation", {}).get("presets", {})
    return condition_flips(event, family) + threshold_flips(event, presets)


def annotate(result, policies):
    """Add 'counterfactuals' to every event of a decide() result, in place; returns the result."""
    for event in result.get("events", []):
        event["counterfactuals"] = for_event(event, policies)
    return result
