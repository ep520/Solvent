(function () {
  "use strict";

  const decision = window.CallGuardDecision;
  let calls, keywordGroups, keywordConfig, keywordCoverage, allKeywords, state, live = false, thresholdValues = null;

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
  const formatTime = (seconds) => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
  const slug = (value) => value.toLowerCase().replace(/\s+/g, "-");

  function classificationFor(call) {
    return decision.classificationFor(call, state.threshold);
  }

  function assessmentFor(call) {
    return decision.assessmentFor(call, state.threshold);
  }

  function displayedQuality(call) {
    return live ? call.asrQuality : call.confidence;
  }

  function actorLabel(actor) {
    if (actor && actor.status === "inferred" && (actor.role === "customer" || actor.role === "advisor")) {
      return `${actor.role[0].toUpperCase()}${actor.role.slice(1)} · Inferred`;
    }
    return "Role unknown";
  }

  function actorTooltip(actor) {
    return actor && actor.status === "inferred" && (actor.role === "customer" || actor.role === "advisor")
      ? "Role inferred from transcript context; not independently verified."
      : "The available evidence does not establish the speaker’s role.";
  }

  function clipBounds(item, call) {
    return {
      start: Number.isFinite(item.clip_start) ? item.clip_start : Math.max(0, item.time - 10),
      end: Number.isFinite(item.clip_end) ? item.clip_end : Math.min(call.duration, item.end + 10)
    };
  }

  function statusMarkup(classification) {
    const statusClass = classification === "Alarm" ? "alarm" : classification === "Review" ? "review" : classification === "Failed" ? "failed" : "no-alert";
    const indicator = classification === "Alarm" ? "error" : classification === "Review" ? "warning" : classification === "Failed" ? "neutral" : "success";
    return `<span class="status-label ${statusClass}"><span class="status status-${indicator}" aria-hidden="true"></span>${escapeHtml(classification)}</span>`;
  }

  function icon(name) {
    const paths = {
      calls: '<path d="M7 4h10M6 8h12v11H6zM9 12h6M9 15h4"/>',
      alarm: '<path d="M12 3 2.8 19h18.4L12 3Z"/><path d="M12 9v4M12 16h.01"/>',
      review: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
      clear: '<circle cx="12" cy="12" r="9"/><path d="m8 12 2.5 2.5L16 9"/>',
      reason: '<path d="M5 4h14v12H9l-4 4V4Z"/><path d="M8 8h8M8 11h5"/>',
      evidence: '<path d="M5 3h10l4 4v14H5z"/><path d="M15 3v5h4M8 12h8M8 16h6"/>',
      audio: '<path d="M6 9v6M10 6v12M14 8v8M18 10v4"/>',
      policy: '<path d="M12 3 5 6v5c0 4 2.8 7.6 7 8.8 4.2-1.2 7-4.8 7-8.8V6l-7-3Z"/><path d="m9 11 2 2 4-4"/>',
      question: '<circle cx="12" cy="12" r="9"/><path d="M9.8 9a2.3 2.3 0 1 1 3.6 1.9c-.8.5-1.4 1-1.4 2.1M12 17h.01"/>',
      threshold: '<path d="M4 7h10M18 7h2M4 17h2M10 17h10M14 5v4M6 15v4"/>',
      transcript: '<path d="M4 5h16M4 10h16M4 15h10M4 20h7"/>',
      back: '<path d="m15 18-6-6 6-6"/>',
      replay: '<path d="M5 8v5h5"/><path d="M7 17a7 7 0 1 0-1.5-7"/>'
    };
    return `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name]}</svg>`;
  }

  function renderSummary() {
    const counts = calls.reduce((acc, call) => { acc[classificationFor(call)]++; return acc; },
                                { Alarm: 0, Review: 0, "No alert": 0, Failed: 0 });
    const attention = counts.Alarm + counts.Review;
    const operationalNote = [state.pendingCount ? `${state.pendingCount} pending` : "", counts.Failed ? `${counts.Failed} failed` : ""].filter(Boolean).join(" · ");
    const operationalSuffix = operationalNote ? ` · ${operationalNote}` : "";
    $("#summary-counters").innerHTML = `<div class="attention-summary"><strong>${attention} calls need attention</strong><span><b>${counts.Alarm}</b> alarms · <b>${counts.Review}</b> reviews</span></div><span class="summary-muted">${calls.length + state.pendingCount} total calls · ${counts["No alert"]} no alert${operationalSuffix}</span>`;
  }

  function renderThreshold() {
    const labels = { sensitive: "Sensitive", balanced: "Balanced", conservative: "Conservative" };
    const descriptions = { sensitive: "More supported cases become alarms", balanced: "Default escalation behaviour", conservative: "Fewer supported cases become alarms; more go to review" };
    $("#threshold-control").innerHTML = Object.entries(labels).map(([value, label]) => `
      <button type="button" class="btn btn-sm join-item threshold-button" role="radio" aria-checked="${state.threshold === value}" aria-label="${label}: ${descriptions[value]}" title="${descriptions[value]}${live ? ` · minimum ASR quality ${thresholdValues[value]}` : ""}" data-threshold="${value}">${label}${live ? ` · ${thresholdValues[value]}` : ""}</button>
    `).join("");
  }

  function renderFilters() {
    const hasFailed = calls.some((call) => call.runStatus === "failed");
    const options = ["Needs attention", "All", "Alarm", "Review", "No alert"].concat(hasFailed ? ["Failed"] : []);
    $("#classification-filters").innerHTML = options.map((filter) => `
      <button type="button" class="btn btn-xs filter-chip ${state.filter === filter ? "active" : ""}" data-filter="${filter}" aria-pressed="${state.filter === filter}">${filter}</button>
    `).join("");
  }

  function filteredCalls() {
    const query = state.query.toLowerCase();
    return calls.filter((call) => {
      const classification = classificationFor(call);
      const matchesFilter = state.filter === "All" || (state.filter === "Needs attention" && (classification === "Alarm" || classification === "Review")) || classification === state.filter;
      const haystack = `${call.id} ${call.family} ${classification} ${assessmentFor(call)} ${call.date} ${call.time}`.toLowerCase();
      return matchesFilter && haystack.includes(query);
    });
  }

  function renderQueue() {
    const shown = filteredCalls();
    $("#result-count").textContent = `${shown.length} of ${calls.length} ${live ? "analysed calls" : "mock calls"}`;
    $("#empty-state").hidden = shown.length > 0;
    $("#call-list").innerHTML = shown.map((call) => {
      const classification = classificationFor(call);
      return `<button type="button" class="call-row ${call.id === state.selectedId ? "selected" : ""}" role="option" aria-selected="${call.id === state.selectedId}" data-call-id="${call.id}">
        <span class="call-primary"><strong>${escapeHtml(shortCallName(call))}</strong><span>${escapeHtml(call.family)}${escapeHtml(recordingVariant(call))}</span></span>
        <span>${statusMarkup(classification)}</span>
        <span class="duration-cell">${formatTime(call.duration)}</span>
      </button>`;
    }).join("");
  }

  function selectedCall() { return calls.find((call) => call.id === state.selectedId); }

  function shortCallName(call) {
    const match = call.id.match(/_([A-Z]\d+)-/i);
    return match ? `Call ${match[1].toUpperCase()}` : call.id;
  }

  function recordingVariant(call) {
    return /^(clean|noisy) audio$/i.test(call.time || "") ? ` · ${call.time.replace(" audio", "")}` : "";
  }

  function userConditionLabel(label) {
    const names = {
      "Own trade request": "A trade was requested", "Information nonpublic": "The information was not public",
      "Information market relevant": "The information could affect the market", "Request based on information": "The request was linked to that information",
      "Value spoken": "A value was spoken", "Access secret": "It relates to an access process", "Secret active": "The value was still valid",
      "Advisor disclosed": "The adviser disclosed information", "Third party detail": "A third party received the detail",
      "Authority absent": "No authority was established"
    };
    return names[label] || label;
  }

  function activeKeywordMatches(text) {
    return [...state.enabledKeywords].filter((keyword) => text.toLowerCase().includes(keyword.toLowerCase()));
  }

  function markedText(text, support = "") {
    const supportParts = support ? support.split("|").filter(Boolean) : [];
    const ranges = [];
    supportParts.forEach((phrase) => {
      const start = text.toLowerCase().indexOf(phrase.toLowerCase());
      if (start >= 0) ranges.push({ start, end: start + phrase.length, type: "support" });
    });
    [...state.enabledKeywords].forEach((keyword) => {
      const lower = text.toLowerCase();
      let start = 0;
      while ((start = lower.indexOf(keyword.toLowerCase(), start)) >= 0) {
        const end = start + keyword.length;
        if (!ranges.some((range) => start < range.end && end > range.start)) ranges.push({ start, end, type: "keyword" });
        start = end;
      }
    });
    ranges.sort((a, b) => a.start - b.start || b.end - a.end);
    const clean = ranges.filter((range, index) => !ranges.slice(0, index).some((prior) => range.start < prior.end));
    let output = "", cursor = 0;
    clean.forEach((range) => {
      output += escapeHtml(text.slice(cursor, range.start));
      output += `<mark class="${range.type === "support" ? "support-mark" : "keyword-mark"}">${escapeHtml(text.slice(range.start, range.end))}</mark>`;
      cursor = range.end;
    });
    return output + escapeHtml(text.slice(cursor));
  }

  function conditionMarkup(condition) {
    const cls = condition.state.toLowerCase();
    const symbol = condition.state === "Supported" ? "✓" : condition.state === "Excluded" ? "—" : "?";
    const stateLabel = condition.state === "Excluded" ? "Explicitly excluded" : condition.state === "Unknown" ? "Not established" : "Supported";
    return `<div class="condition"><span>${symbol} ${escapeHtml(userConditionLabel(condition.label))}</span><span class="status-label condition-state ${cls}">${stateLabel}</span></div>`;
  }

  function liveReasons(call) {
    return live ? call.reasonByThreshold[state.threshold] : [];
  }

  function unresolvedMarkup(call, classification) {
    if (live) {
      const reasons = liveReasons(call);
      let html = "";
      if (reasons.includes("missing_policy_fact") && call.missingFact) html += `
      <section id="open-question-section" class="detail-card open-question guide-target">
        <div class="section-head"><div class="section-title"><span class="section-icon">${icon("question")}</span><div><h3>Missing fact / Open question</h3><p>Required before a decision</p></div></div></div>
        <strong>${escapeHtml(call.missingFact)}</strong><p>The recording does not establish this fact, so the call stays in Review at every threshold.</p>
      </section>`;
      if (reasons.includes("below_escalation_threshold")) html += `
      <section id="threshold-note-section" class="detail-card threshold-note guide-target">
        <div class="section-head"><div class="section-title"><span class="section-icon">${icon("threshold")}</span><div><h3>Threshold routing</h3><p>Supported event · ASR quality below ${thresholdValues[state.threshold]}</p></div></div></div>
        <p class="assessment-copy">All conditions are supported, but the transcription quality of a decisive passage is below the <strong>${escapeHtml(state.threshold)}</strong> threshold, so a person should listen to it before an alarm is raised.</p>
      </section>`;
      if (reasons.includes("technical_uncertainty")) html += `
      <section id="threshold-note-section" class="detail-card threshold-note guide-target">
        <div class="section-head"><div class="section-title"><span class="section-icon">${icon("question")}</span><div><h3>Technical uncertainty</h3><p>Evidence could not be verified</p></div></div></div>
        <p class="assessment-copy">The model's evidence could not be located in the transcript, or the extraction was incomplete. The call is routed to review instead of guessing.</p>
        ${call.groundingIssues && call.groundingIssues.length ? `<ul class="grounding-issues">${call.groundingIssues.map((issue) => `<li>${escapeHtml(issue)}</li>`).join("")}</ul>` : ""}
      </section>`;
      if (reasons.includes("actor_role_unknown")) html += `
      <section id="actor-role-section" class="detail-card open-question guide-target">
        <div class="section-head"><div class="section-title"><span class="section-icon">${icon("question")}</span><div><h3>Review reason</h3><p>Speaker role is unresolved</p></div></div></div>
        <p class="assessment-copy">A necessary policy condition cannot be established because the actor’s role is unresolved.</p>
        <strong>${escapeHtml(call.missingFact || "Was the person acting in the required role?")}</strong>
      </section>`;
      return html;
    }
    if (call.decisionType === "missing-fact") return `
      <section id="open-question-section" class="detail-card open-question guide-target">
        <div class="section-head"><div class="section-title"><span class="section-icon">${icon("question")}</span><div><h3>Missing fact / Open question</h3><p>Required before a decision</p></div></div></div>
        <strong>${escapeHtml(call.missingFact)}</strong><p>This fact remains unknown in the recording, so the call stays in Review at every threshold.</p>
      </section>`;
    if (call.decisionType === "threshold" && classification === "Review") return `
      <section id="threshold-note-section" class="detail-card threshold-note guide-target">
        <div class="section-head"><div class="section-title"><span class="section-icon">${icon("threshold")}</span><div><h3>Threshold routing</h3><p>Supported event · simulated escalation</p></div></div></div>
        <p class="assessment-copy">The event is supported, but the selected <strong>${escapeHtml(state.threshold)}</strong> threshold routes it to review. No necessary fact is missing.</p>
      </section>`;
    return "";
  }

  function operationalNarrative(call, classification) {
    const family = call.family.toLowerCase();
    const reasons = liveReasons(call);
    if (classification === "Review" && reasons.includes("missing_policy_fact")) {
      if (family === "access") return "An access value was spoken, but the call does not establish whether it was still valid.";
      return "The call contains relevant information, but it does not establish a fact required to resolve this case.";
    }
    if (classification === "Review" && reasons.includes("technical_uncertainty")) return "The available extraction or evidence could not be verified. Review the passage before deciding.";
    if (classification === "Review" && reasons.includes("below_escalation_threshold")) return "The required facts are supported, but the supporting passage needs a human listen before escalation.";
    if (classification === "Alarm" && family === "access") return "The caller read a value identified as valid for an active login.";
    if (classification === "No alert" && family === "access") return "The spoken value was explicitly described as an expired example.";
    return assessmentFor(call);
  }

  function caseHeading(call, classification) {
    if (classification === "Review") return "Review required";
    if (classification === "Alarm" && call.family.toLowerCase() === "access") return "Access secret disclosed";
    if (classification === "Alarm") return "Compliance concern detected";
    return "No alert detected";
  }

  function renderDetail(options = {}) {
    const call = selectedCall();
    const classification = classificationFor(call);
    const evidenceIndex = Math.min(state.evidenceIndex || 0, Math.max(0, call.evidence.length - 1));
    const evidence = call.evidence[evidenceIndex];
    const bounds = clipBounds(evidence, call);
    const hasEvidence = evidence.segments.length > 0;
    const reasons = liveReasons(call);
    const token = ++state.selectionToken;
    stopReveal();
    stopAudio();
    const detail = $("#call-detail");
    detail.innerHTML = `<div class="detail-inner detail-change">
      <div class="mobile-detail-head"><button id="mobile-back" class="btn btn-sm btn-ghost" type="button">${icon("back")} Call queue</button><span class="badge badge-ghost badge-sm">${live ? call.cacheStatus === "legacy" ? "Legacy cached extraction" : "Pipeline result" : "Mock call"}</span></div>
      <header id="classification-section" class="detail-header guide-target">
        <div><div class="eyebrow">${escapeHtml(call.family)} · ${formatTime(call.duration)}</div><div class="detail-title-line"><h2>${escapeHtml(shortCallName(call))}</h2>${statusMarkup(classification)}</div><p class="detail-meta">${escapeHtml(call.family)} review · automatic transcript</p></div>
      </header>
      <div class="review-workspace">
        ${classification === "Review" && (reasons.includes("missing_policy_fact") || reasons.includes("actor_role_unknown") || (!live && call.decisionType === "missing-fact")) ? `<section id="open-question-section" class="review-question guide-target"><span class="eyebrow">Question to resolve</span><h3>${escapeHtml(call.missingFact || "What required fact is still unresolved?")}</h3><p>This answer is required before the case can be resolved.</p></section>` : ""}
        ${classification === "Review" && reasons.includes("technical_uncertainty") ? `<section class="review-question technical-review"><span class="eyebrow">Technical review</span><h3>Evidence needs verification</h3><p>The extraction or its transcript grounding could not be verified. Listen to the relevant context before making a compliance decision.</p></section>` : ""}
        <section id="assessment-section" class="main-review-card guide-target"><div class="section-head"><div><span class="eyebrow">${escapeHtml(caseHeading(call, classification))}</span><h3>${escapeHtml(operationalNarrative(call, classification))}</h3></div></div>
          <div id="evidence-section" class="evidence-window"><div class="evidence-meta"><span><strong>${hasEvidence ? `${formatTime(evidence.evidence_start ?? evidence.time)}–${formatTime(evidence.evidence_end ?? evidence.end)}` : "No supporting passage"}</strong></span><span>${hasEvidence ? `Context ${formatTime(bounds.start)}–${formatTime(bounds.end)}` : ""}</span></div><p class="evidence-context">${classification === "Review" && (reasons.includes("missing_policy_fact") || reasons.includes("actor_role_unknown")) ? "Relevant context — it does not establish the unresolved fact." : hasEvidence ? "Supporting passage in the automatic transcript." : "Audio or grounded evidence is not available."}</p><p id="evidence-text" class="evidence-text passage-change" aria-hidden="true"></p><p class="sr-only">${escapeHtml(evidence.text)}</p></div>
          ${live ? `<audio id="audio-element" preload="metadata" src="/audio/${encodeURIComponent(call.id)}.wav"></audio>` : ""}
          <div id="audio-section" class="evidence-audio"><button class="btn btn-primary play-evidence" type="button" ${hasEvidence ? "" : "disabled"} data-clip-start="${bounds.start}" data-clip-end="${bounds.end}">${icon("audio")} Play evidence</button><span>${hasEvidence ? "Includes up to 10 seconds of surrounding audio." : "Audio is not available for this passage."}</span>${live && hasEvidence ? `<a class="btn btn-xs btn-ghost" href="/audio/${encodeURIComponent(call.id)}.wav">Full recording</a><a class="btn btn-xs btn-ghost" href="/clip/${encodeURIComponent(call.id)}.wav?clip_start=${bounds.start}&clip_end=${bounds.end}" download>Download clip</a>` : ""}</div>
          ${call.evidence.length > 1 ? `<div class="passage-nav">Passage ${evidenceIndex + 1} of ${call.evidence.length}${call.evidence.map((_, index) => `<button class="btn btn-xs ${index === evidenceIndex ? "btn-primary" : "btn-ghost"}" data-evidence-index="${index}" type="button">${index + 1}</button>`).join("")}</div>` : ""}
          <div class="audio-player secondary-audio"><button id="play-button" class="btn btn-ghost btn-xs play-button" type="button" aria-label="Play full recording" ${live ? "" : ""}><svg viewBox="0 0 24 24" aria-hidden="true"><path id="play-icon-path" d="m9 7 8 5-8 5V7Z" fill="currentColor" stroke="none"/></svg></button><div class="audio-track"><input id="audio-range" class="range range-xs audio-range" type="range" min="0" max="${call.duration}" step="0.1" value="0" aria-label="Full recording position"><div class="audio-times"><span id="elapsed-time">0:00</span><span>Full recording · ${formatTime(call.duration)}</span></div></div></div>
        </section>
        <section id="policy-section" class="detail-card facts-card guide-target"><div class="section-head"><div class="section-title"><span class="section-icon">${icon("policy")}</span><div><h3>What we know</h3><p>Facts used for this review</p></div></div></div><div class="condition-list">${call.conditions.map(conditionMarkup).join("")}</div></section>
        <details class="detail-card transcript-collapse"><summary><span class="section-title"><span class="section-icon">${icon("transcript")}</span><span><strong>Full transcript</strong><small style="display:block;color:var(--muted);font-size:9.5px">Automatic transcript</small></span></span></summary><div class="transcript-body">${call.transcript.map((line) => `<div class="transcript-line"><span class="transcript-time">${formatTime(line.time)}</span><span class="transcript-speaker">${escapeHtml(line.speaker)}</span><span>${markedText(line.text)}</span></div>`).join("")}</div></details>
        <details class="detail-card technical-details"><summary>Technical details</summary><dl><dt>Full call ID</dt><dd>${escapeHtml(call.id)}</dd><dt>ASR quality</dt><dd>${displayedQuality(call) === null ? "Unavailable" : `${displayedQuality(call)}% heuristic — not a probability of fraud`}</dd><dt>Model</dt><dd>${escapeHtml(call.model || "Mock data")}</dd><dt>Extraction</dt><dd>${escapeHtml(call.extractionVersion || "Mock data")}</dd><dt>Policy version</dt><dd>${escapeHtml(call.policiesVersion || "Mock data")}</dd>${call.groundingIssues?.length ? `<dt>Grounding issues</dt><dd>${escapeHtml(call.groundingIssues.join("; "))}</dd>` : ""}</dl></details>
      </div>
    </div>`;

    bindDetailEvents(token);
    finishReveal(token, false);
  }

  function startReveal(call, token, startGuide) {
    const textElement = $("#evidence-text");
    const button = $("#show-full-text");
    const text = call.evidence[state.evidenceIndex || 0].text;
    textElement.classList.add("reveal-cursor");
    let index = 0;
    const delay = Math.max(18, Math.min(38, Math.floor(2300 / text.length)));
    state.revealTimer = setInterval(() => {
      if (token !== state.selectionToken) return stopReveal();
      index += 2;
      textElement.textContent = text.slice(0, index);
      if (index >= text.length) finishReveal(token, startGuide);
    }, delay);
    if (button) button.hidden = false;
  }

  function finishReveal(token, startGuide) {
    if (token !== state.selectionToken) return;
    stopReveal();
    const call = selectedCall();
    const element = $("#evidence-text");
    if (!element) return;
    element.classList.remove("reveal-cursor");
    const evidence = call.evidence[state.evidenceIndex || 0];
    element.innerHTML = markedText(evidence.text, evidence.support);
    const button = $("#show-full-text");
    if (button) button.hidden = true;
    if (startGuide && !state.autoGuideUsed) {
      state.autoGuideUsed = true;
      sessionStorage.setItem("callguard-guide-seen", "1");
      startCallGuide(true);
    }
  }

  function stopReveal() {
    clearInterval(state.revealTimer);
    state.revealTimer = null;
  }

  function bindDetailEvents(token) {
    const fullText = $("#show-full-text");
    if (fullText) fullText.addEventListener("click", () => finishReveal(token, true));
    const mobileBack = $("#mobile-back");
    if (mobileBack) mobileBack.addEventListener("click", closeMobileDetail);
    $("#play-button").addEventListener("click", toggleAudio);
    const el = audioElement();
    if (el) {
      el.addEventListener("timeupdate", () => updateAudio(el.currentTime, true));
      const setIcon = (playing) => {
        $("#play-button").setAttribute("aria-label", playing ? "Pause audio" : "Play audio");
        $("#play-icon-path").setAttribute("d", playing ? "M8 7h3v10H8zM13 7h3v10h-3z" : "m9 7 8 5-8 5V7Z");
      };
      el.addEventListener("play", () => setIcon(true));
      el.addEventListener("pause", () => setIcon(false));
    }
    $("#audio-range").addEventListener("input", (event) => updateAudio(Number(event.target.value)));
    $$(".play-evidence").forEach((button) => button.addEventListener("click", () => {
      playEvidence(Number(button.dataset.clipStart), Number(button.dataset.clipEnd));
    }));
    $$('[data-evidence-index]').forEach((button) => button.addEventListener("click", () => {
      state.evidenceIndex = Number(button.dataset.evidenceIndex);
      renderDetail({ immediate: true, startGuide: false });
    }));
    $$("#classification-section, #assessment-section, #evidence-section, #audio-section, #policy-section, #open-question-section").forEach((section) => {
      if (section) section.addEventListener("pointerdown", pauseGuideTimer);
    });
  }

  function audioElement() { return live ? $("#audio-element") : null; }

  function toggleAudio() {
    stopGuide();
    state.evidenceEnd = null;
    const el = audioElement();
    if (el) {
      if (el.paused) el.play(); else el.pause();
      return;
    }
    state.playing = !state.playing;
    const button = $("#play-button");
    if (!button) return;
    button.setAttribute("aria-label", state.playing ? "Pause simulated audio" : "Play simulated audio");
    $("#play-icon-path").setAttribute("d", state.playing ? "M8 7h3v10H8zM13 7h3v10h-3z" : "m9 7 8 5-8 5V7Z");
    if (state.playing) {
      state.audioTimer = setInterval(() => {
        const call = selectedCall();
        updateAudio(state.audioElapsed + .25);
        if (state.audioElapsed >= call.duration) stopAudio();
      }, 250);
    } else clearInterval(state.audioTimer);
  }

  function updateAudio(value, fromElement = false) {
    const call = selectedCall();
    state.audioElapsed = Math.max(0, Math.min(value, call.duration));
    const el = audioElement();
    if (fromElement && el && state.evidenceEnd !== null && state.audioElapsed >= state.evidenceEnd) {
      state.audioElapsed = state.evidenceEnd;
      el.currentTime = state.evidenceEnd;
      state.evidenceEnd = null;
      el.pause();
    }
    if (el && !fromElement) {
      el.currentTime = state.audioElapsed;
      if (el.paused) el.play();
    }
    const range = $("#audio-range");
    const elapsed = $("#elapsed-time");
    if (range) range.value = state.audioElapsed;
    if (elapsed) elapsed.textContent = formatTime(state.audioElapsed);
  }

  function playEvidence(start, end) {
    stopGuide();
    state.evidenceEnd = end;
    updateAudio(start);
  }

  function stopAudio() {
    const el = audioElement();
    if (el) el.pause();
    clearInterval(state.audioTimer);
    state.audioTimer = null;
    state.playing = false;
    state.audioElapsed = 0;
    state.evidenceEnd = null;
  }

  function renderKeywordPopover() {
    if (state.keywordEditor && live) return renderKeywordEditor();
    const query = state.keywordQuery.toLowerCase();
    const groups = Object.entries(keywordGroups).map(([family, keywords]) => {
      const visible = keywords.filter((keyword) => keyword.toLowerCase().includes(query) || family.toLowerCase().includes(query));
      if (!visible.length) return "";
      return `<div class="keyword-group"><h4>${escapeHtml(family)}</h4>${visible.map((keyword) => `<label class="keyword-option"><input class="checkbox checkbox-sm checkbox-primary" type="checkbox" value="${escapeHtml(keyword)}" ${state.enabledKeywords.has(keyword) ? "checked" : ""}><span>${escapeHtml(keyword)}</span></label>`).join("")}</div>`;
    }).join("");
    const coverage = live && keywordCoverage ? `<div class="keyword-coverage"><strong>${keywordCoverage.families} families · ${keywordCoverage.enabledKeywords} enabled keywords</strong><span>${keywordCoverage.configuredVariants} configured DE / Swiss German variants (${keywordCoverage.distinctTerms} distinct terms) · ${keywordCoverage.occurrences} observed occurrences in ${keywordCoverage.callsWithOccurrences}/${keywordCoverage.transcriptCalls} cached calls</span><div>${keywordCoverage.byFamily.map((row) => `<small>${escapeHtml(row.label)}: ${row.enabledKeywords} keywords · ${row.occurrences} hits</small>`).join("")}</div></div>` : "";
    const manage = live ? '<button class="btn btn-xs btn-outline" id="manage-keywords" type="button">Manage list</button>' : "";
    $("#keyword-popover").innerHTML = `<div class="keyword-head"><div><h3>Keyword highlights</h3><p>Highlights only · classifications stay unchanged</p></div><div class="keyword-actions"><button class="btn btn-xs btn-ghost" id="select-all-keywords" type="button">All</button><button class="btn btn-xs btn-ghost" id="clear-keywords" type="button">Clear</button></div></div>${coverage}<div class="keyword-manage">${manage}</div><label class="input input-sm keyword-search"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg><input id="keyword-search" type="search" value="${escapeHtml(state.keywordQuery)}" placeholder="Find a keyword"></label>${groups || '<p class="empty-state">No keywords found.</p>'}`;
    $("#keyword-count").textContent = state.enabledKeywords.size;
    $("#keyword-search").addEventListener("input", (event) => { state.keywordQuery = event.target.value; renderKeywordPopover(); $("#keyword-search").focus(); });
    $("#select-all-keywords").addEventListener("click", () => { state.enabledKeywords = new Set(allKeywords); updateKeywords(); });
    $("#clear-keywords").addEventListener("click", () => { state.enabledKeywords.clear(); updateKeywords(); });
    const manageButton = $("#manage-keywords");
    if (manageButton) manageButton.addEventListener("click", () => { state.keywordEditor = true; state.keywordSaveError = ""; renderKeywordPopover(); });
    $$(".keyword-option input", $("#keyword-popover")).forEach((checkbox) => checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.enabledKeywords.add(checkbox.value); else state.enabledKeywords.delete(checkbox.value);
      updateKeywords();
    }));
  }

  function familyForKeyword(id) {
    return Object.entries(keywordConfig.families).find(([, family]) => family.keywords.includes(id))?.[0] || Object.keys(keywordConfig.families)[0];
  }

  function editorKeywordMarkup(keyword) {
    const families = Object.entries(keywordConfig.families).map(([id, family]) =>
      `<option value="${escapeHtml(id)}" ${familyForKeyword(keyword.id) === id ? "selected" : ""}>${escapeHtml(family.label || id)}</option>`).join("");
    return `<fieldset class="keyword-editor-row" data-keyword-id="${escapeHtml(keyword.id)}"><div class="keyword-editor-row-head"><strong>${escapeHtml(keyword.id)}</strong><label class="keyword-enabled">Enabled <input data-field="enabled" type="checkbox" ${keyword.enabled !== false ? "checked" : ""}></label><button class="btn btn-xs btn-ghost remove-keyword" type="button">Remove</button></div><label>Label<input class="input input-sm" data-field="label" value="${escapeHtml(keyword.label)}"></label><label>Family<select class="select select-sm" data-field="family">${families}</select></label><label>German variants <textarea data-field="de" rows="2" placeholder="One exact phrase per line">${escapeHtml(keyword.de.join("\n"))}</textarea></label><label>Swiss German variants <textarea data-field="gsw" rows="2" placeholder="One exact phrase per line">${escapeHtml(keyword.gsw.join("\n"))}</textarea></label></fieldset>`;
  }

  function renderKeywordEditor() {
    $("#keyword-popover").innerHTML = `<div class="keyword-head"><div><h3>Manage keyword list</h3><p>Saved changes rerun matching, highlights and counts only. Decisions remain unchanged.</p></div><button id="close-keyword-editor" class="btn btn-xs btn-ghost" type="button">Back</button></div>${state.keywordSaveError ? `<p class="keyword-save-error" role="alert">${escapeHtml(state.keywordSaveError)}</p>` : ""}<form id="keyword-editor-form" class="keyword-editor">${keywordConfig.keywords.map(editorKeywordMarkup).join("")}<div class="keyword-editor-actions"><button id="add-keyword" class="btn btn-xs btn-ghost" type="button">Add keyword</button><button id="save-keywords" class="btn btn-xs btn-primary" type="submit">Save matching list</button></div></form>`;
    $("#close-keyword-editor").addEventListener("click", () => { state.keywordEditor = false; renderKeywordPopover(); });
    $("#add-keyword").addEventListener("click", () => {
      keywordConfig = configFromEditor();
      const taken = new Set(keywordConfig.keywords.map((keyword) => keyword.id));
      let ordinal = 1; while (taken.has(`NEW${ordinal}`)) ordinal++;
      keywordConfig.keywords.push({ id: `NEW${ordinal}`, label: "New keyword", de: [], gsw: [], enabled: true });
      const firstFamily = Object.keys(keywordConfig.families)[0];
      keywordConfig.families[firstFamily].keywords.push(`NEW${ordinal}`);
      renderKeywordEditor();
    });
    $$(".remove-keyword", $("#keyword-editor-form")).forEach((button) => button.addEventListener("click", () => {
      keywordConfig = configFromEditor();
      const id = button.closest("[data-keyword-id]").dataset.keywordId;
      keywordConfig.keywords = keywordConfig.keywords.filter((keyword) => keyword.id !== id);
      Object.values(keywordConfig.families).forEach((family) => { family.keywords = family.keywords.filter((keywordId) => keywordId !== id); });
      renderKeywordEditor();
    }));
    $("#keyword-editor-form").addEventListener("submit", saveKeywordConfig);
  }

  function termsFromEditor(value) { return value.split("\n").map((term) => term.trim()).filter(Boolean); }

  function configFromEditor() {
    const next = JSON.parse(JSON.stringify(keywordConfig));
    next.keywords = $$(".keyword-editor-row", $("#keyword-editor-form")).map((row) => ({
      ...next.keywords.find((keyword) => keyword.id === row.dataset.keywordId),
      id: row.dataset.keywordId,
      label: $("[data-field=label]", row).value.trim(),
      de: termsFromEditor($("[data-field=de]", row).value),
      gsw: termsFromEditor($("[data-field=gsw]", row).value),
      enabled: $("[data-field=enabled]", row).checked
    }));
    Object.values(next.families).forEach((family) => { family.keywords = []; });
    $$(".keyword-editor-row", $("#keyword-editor-form")).forEach((row) => next.families[$("[data-field=family]", row).value].keywords.push(row.dataset.keywordId));
    return next;
  }

  function applyLiveData(data) {
    ({ calls, keywordGroups, keywordConfig, keywordCoverage } = data);
    thresholdValues = data.thresholds;
    allKeywords = Object.values(keywordGroups).flat();
    state.enabledKeywords = new Set(allKeywords);
    if (!calls.some((call) => call.id === state.selectedId)) state.selectedId = calls[0].id;
  }

  async function saveKeywordConfig(event) {
    event.preventDefault();
    try {
      const response = await fetch("/api/keywords", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(configFromEditor()) });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      const data = await loadData();
      if (!data) throw new Error("The list was saved, but cached call data could not be reloaded.");
      state.keywordEditor = false; state.keywordSaveError = "";
      applyLiveData(data);
      renderSummary(); renderQueue(); renderKeywordPopover(); renderDetail({ immediate: true, startGuide: false });
      $("#sr-status").textContent = "Keyword list saved. Matching, highlights and coverage counts were recalculated; classifications are unchanged.";
    } catch (error) {
      state.keywordSaveError = error.message;
      renderKeywordEditor();
    }
  }

  function updateKeywords() {
    stopGuide();
    const classificationBefore = classificationFor(selectedCall());
    renderKeywordPopover();
    renderDetail({ immediate: true, startGuide: false });
    $("#sr-status").textContent = `${state.enabledKeywords.size} keywords enabled. Classification remains ${classificationBefore}.`;
  }

  function selectCall(id) {
    stopGuide();
    stopReveal();
    stopAudio();
    state.selectedId = id;
    state.evidenceIndex = 0;
    state.audioElapsed = 0;
    renderQueue();
    renderDetail();
    if (innerWidth <= 760) openMobileDetail();
    $("#sr-status").textContent = `${id} selected. ${classificationFor(selectedCall())}.`;
  }

  function openMobileDetail() { $("#call-detail").classList.add("mobile-open"); $("#mobile-backdrop").hidden = false; }
  function closeMobileDetail() { stopGuide(); stopAudio(); $("#call-detail").classList.remove("mobile-open"); $("#mobile-backdrop").hidden = true; }

  function openSettings() {
    const drawer = $("#settings-drawer");
    state.settingsOpener = document.activeElement;
    drawer.hidden = false;
    drawer.setAttribute("aria-hidden", "false");
    $("#settings-backdrop").hidden = false;
    $("#settings-button").setAttribute("aria-expanded", "true");
    setTimeout(() => $("#settings-close").focus(), 0);
  }

  function closeSettings({ restoreFocus = true } = {}) {
    const drawer = $("#settings-drawer");
    if (drawer.hidden) return;
    $("#keyword-popover").hidden = true;
    $("#keyword-button").setAttribute("aria-expanded", "false");
    drawer.setAttribute("aria-hidden", "true");
    drawer.hidden = true;
    $("#settings-backdrop").hidden = true;
    $("#settings-button").setAttribute("aria-expanded", "false");
    if (restoreFocus && state.settingsOpener instanceof HTMLElement) state.settingsOpener.focus();
  }

  function trapSettingsFocus(event) {
    const drawer = $("#settings-drawer");
    if (drawer.hidden || event.key !== "Tab") return;
    const focusable = $$('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled])', drawer)
      .filter((node) => !node.closest("[hidden]"));
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }

  function callGuideSteps() {
    const call = selectedCall();
    const classification = classificationFor(call);
    const steps = [
      { target: "#classification-section", title: "Classification", copy: "Alarm, Review, or No alert indicates whether this call needs attention; mock confidence is illustrative, not a verified probability." },
      { target: "#assessment-section", title: "Assessment reason", copy: "This explains which behaviour led to the assessment, so you can understand the decision before checking the evidence." },
      { target: "#evidence-section", title: "Evidence transcript", copy: "This passage contains the supporting evidence; read it in context rather than judging isolated keywords." },
      { target: "#audio-section", title: "Audio replay", copy: "This simulated player demonstrates how you would replay the supporting passage with surrounding context." },
      { target: "#policy-section", title: "Policy conditions", copy: "These conditions show what is supported, excluded, or unknown—and how those facts lead to the assessment." }
    ];
    if (live) {
      steps[0].copy = "Alarm, Review, or No alert indicates whether this call needs attention; the percentage is the ASR quality of the decisive passage, not a probability.";
      steps[3].copy = "Play the original recording; the jump buttons start 10 seconds before each supporting passage, and each clip can be downloaded.";
      if ($("#open-question-section")) steps.push({ target: "#open-question-section", title: "Open question", copy: "This identifies the missing fact that prevents a decision, so you know what needs clarification." });
      if ($("#threshold-note-section")) steps.push({ target: "#threshold-note-section", title: "Why review", copy: "This explains why a supported or partly verified event is routed to a person instead of raising an alarm." });
    } else if (call.decisionType === "missing-fact") steps.push({ target: "#open-question-section", title: "Open question", copy: "This identifies the missing fact that prevents a decision, so you know what needs clarification." });
    else if (call.decisionType === "threshold" && classification === "Review") steps.push({ target: "#threshold-note-section", title: "Threshold routing", copy: "The event is supported, but the selected threshold routes it to review rather than an alarm." });
    return steps;
  }

  function startCallGuide(auto) {
    finishReveal(state.selectionToken, false);
    startGuide(callGuideSteps(), auto, "Call review");
  }

  function startPageGuide() {
    const steps = [
      { target: "#threshold-section", title: "Threshold", copy: "Choose how readily supported cases become alarms; missing facts still require review." },
      { target: "#keyword-section", title: "Keywords", copy: "Choose which terms to highlight; a keyword match helps locate content but does not determine the classification." }
    ];
    startGuide(steps, false, "Page guide");
  }

  function startGuide(steps, auto, label) {
    stopGuide();
    state.guide = { steps, index: 0, auto: auto && !reducedMotion(), label, token: state.selectionToken };
    showGuideStep();
  }

  function showGuideStep() {
    clearTimeout(state.guideTimer);
    if (!state.guide || state.guide.token !== state.selectionToken) return stopGuide();
    $$(".guide-active").forEach((element) => element.classList.remove("guide-active"));
    const step = state.guide.steps[state.guide.index];
    const target = $(step.target);
    if (!target) return advanceGuide(1);
    target.classList.add("guide-active");
    target.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "nearest" });
    const hint = $("#guide-hint");
    hint.hidden = false;
    $("#guide-progress").textContent = `${state.guide.label} · ${state.guide.index + 1} of ${state.guide.steps.length}`;
    $("#guide-title").textContent = step.title;
    $("#guide-copy").textContent = step.copy;
    $("#guide-back").disabled = state.guide.index === 0;
    $("#guide-next").textContent = state.guide.index === state.guide.steps.length - 1 ? "Done" : "Next";
    positionGuide(hint, target);
    if (state.guide.auto) state.guideTimer = setTimeout(() => advanceGuide(1), 6500);
  }

  function positionGuide(hint, target) {
    if (innerWidth <= 760) { hint.style.cssText = ""; return; }
    const rect = target.getBoundingClientRect();
    const width = 310;
    const spaceRight = innerWidth - rect.right;
    let left = spaceRight > width + 20 ? rect.right + 10 : Math.max(12, rect.left - width - 10);
    let top = Math.min(innerHeight - 180, Math.max(68, rect.top));
    hint.style.left = `${left}px`;
    hint.style.top = `${top}px`;
    hint.style.right = "auto";
    hint.style.bottom = "auto";
  }

  function advanceGuide(delta) {
    if (!state.guide) return;
    const next = state.guide.index + delta;
    if (next < 0) return;
    if (next >= state.guide.steps.length) return stopGuide();
    state.guide.index = next;
    showGuideStep();
  }

  function pauseGuideTimer() {
    if (!state.guide) return;
    clearTimeout(state.guideTimer);
    state.guide.auto = false;
  }

  function stopGuide() {
    clearTimeout(state.guideTimer);
    state.guideTimer = null;
    state.guide = null;
    const hint = $("#guide-hint");
    if (hint) hint.hidden = true;
    $$(".guide-active").forEach((element) => element.classList.remove("guide-active"));
  }

  function bindGlobalEvents() {
    $("#threshold-control").addEventListener("click", (event) => {
      const button = event.target.closest("[data-threshold]");
      if (!button || button.dataset.threshold === state.threshold) return;
      stopGuide();
      state.threshold = button.dataset.threshold;
      renderThreshold(); renderSummary(); renderQueue(); renderDetail({ immediate: true, startGuide: false });
      $("#threshold-feedback").textContent = `${button.textContent.trim()} selected. Cached classifications were updated.`;
      $("#sr-status").textContent = `${button.textContent} ${live ? "" : "simulated "}threshold selected. Queue classifications updated.`;
    });
    $("#classification-filters").addEventListener("click", (event) => {
      const button = event.target.closest("[data-filter]");
      if (!button) return;
      state.filter = button.dataset.filter; renderFilters(); renderQueue();
    });
    $("#call-list").addEventListener("click", (event) => { const row = event.target.closest("[data-call-id]"); if (row) selectCall(row.dataset.callId); });
    $("#call-search").addEventListener("input", (event) => { state.query = event.target.value; renderQueue(); });
    $("#keyword-button").addEventListener("click", () => {
      const popover = $("#keyword-popover");
      popover.hidden = !popover.hidden;
      $("#keyword-button").setAttribute("aria-expanded", String(!popover.hidden));
      const keywordSearch = $("#keyword-search");
      if (!popover.hidden && keywordSearch) keywordSearch.focus();
    });
    $("#settings-button").addEventListener("click", openSettings);
    $("#settings-close").addEventListener("click", () => closeSettings());
    $("#settings-backdrop").addEventListener("click", () => closeSettings());
    $("#page-guide-button").addEventListener("click", startPageGuide);
    $("#guide-next").addEventListener("click", () => { pauseGuideTimer(); advanceGuide(1); });
    $("#guide-back").addEventListener("click", () => { pauseGuideTimer(); advanceGuide(-1); });
    $("#guide-skip").addEventListener("click", stopGuide);
    $("#guide-hint").addEventListener("pointerenter", pauseGuideTimer);
    $("#mobile-backdrop").addEventListener("click", closeMobileDetail);
    addEventListener("resize", () => { if (state.guide) showGuideStep(); if (innerWidth > 760) closeMobileDetail(); });
    document.addEventListener("keydown", (event) => {
      trapSettingsFocus(event);
      if (event.key === "Escape") {
        stopGuide(); closeMobileDetail();
        if (!$("#settings-drawer").hidden) closeSettings();
        else { $("#keyword-popover").hidden = true; $("#keyword-button").setAttribute("aria-expanded", "false"); }
      }
    });
  }

  function init() {
    renderSummary(); renderThreshold(); renderFilters(); renderQueue(); renderKeywordPopover(); renderDetail(); bindGlobalEvents();
  }

  async function loadData() {
    try {
      const response = await fetch("/api/data", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (!data.calls.length) throw new Error("no analysed calls yet");
      return data;
    } catch (error) {
      return null;
    }
  }

  async function boot() {
    const data = await loadData();
    if (data) {
      live = true;
      ({ calls, keywordGroups, keywordConfig, keywordCoverage } = data);
      thresholdValues = data.thresholds;
      $("#data-badge").hidden = true;
      $("#threshold-badge").textContent = "ASR quality";
    } else {
      ({ calls, keywordGroups } = window.CallGuardData);
      keywordConfig = null;
      keywordCoverage = null;
    }
    allKeywords = Object.values(keywordGroups).flat();
    state = window.CallGuardState.createUiState(calls, allKeywords);
    state.pendingCount = live ? (data.pending || []).length : 0;
    if (live) state.threshold = data.defaultThreshold;
    init();
  }

  boot();
})();
