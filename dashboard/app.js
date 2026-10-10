(function () {
  "use strict";

  const decision = window.CallGuardDecision;
  let calls, keywordGroups, allKeywords, state, live = false, thresholdValues = null;

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

  function statusMarkup(classification) {
    const statusClass = classification === "Alarm" ? "alarm" : classification === "Review" ? "review" : "no-alert";
    return `<span class="status-label ${statusClass}"><span class="status status-${classification === "Alarm" ? "error" : classification === "Review" ? "warning" : "success"}" aria-hidden="true"></span>${escapeHtml(classification)}</span>`;
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
    const counts = calls.reduce((acc, call) => { acc[classificationFor(call)]++; return acc; }, { Alarm: 0, Review: 0, "No alert": 0 });
    const items = [
      ["Total calls", calls.length, "calls", "calls"],
      ["Alarms", counts.Alarm, "alarm", "alarm"],
      ["Reviews", counts.Review, "review", "review"],
      ["No alerts", counts["No alert"], "clear", "clear"]
    ];
    $("#summary-counters").innerHTML = items.map(([label, value, type, iconName]) => `
      <div class="summary-item ${type}"><span class="summary-icon">${icon(iconName)}</span><span class="summary-copy"><strong class="summary-value">${value}</strong><span class="summary-label">${label}</span></span></div>
    `).join("");
  }

  function renderThreshold() {
    const labels = { sensitive: "Sensitive", balanced: "Balanced", conservative: "Conservative" };
    const descriptions = { sensitive: "More supported cases become alarms", balanced: "Default escalation behaviour", conservative: "Fewer supported cases become alarms; more go to review" };
    $("#threshold-control").innerHTML = Object.entries(labels).map(([value, label]) => `
      <button type="button" class="btn btn-sm join-item threshold-button" role="radio" aria-checked="${state.threshold === value}" aria-label="${label}: ${descriptions[value]}" title="${descriptions[value]}${live ? ` · minimum ASR quality ${thresholdValues[value]}` : ""}" data-threshold="${value}">${label}${live ? ` · ${thresholdValues[value]}` : ""}</button>
    `).join("");
  }

  function renderFilters() {
    $("#classification-filters").innerHTML = ["All", "Alarm", "Review", "No alert"].map((filter) => `
      <button type="button" class="btn btn-xs filter-chip ${state.filter === filter ? "active" : ""}" data-filter="${filter}" aria-pressed="${state.filter === filter}">${filter}</button>
    `).join("");
  }

  function filteredCalls() {
    const query = state.query.toLowerCase();
    return calls.filter((call) => {
      const classification = classificationFor(call);
      const matchesFilter = state.filter === "All" || classification === state.filter;
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
        <span class="call-primary"><strong>${escapeHtml(call.id)}</strong><span>${escapeHtml(call.date)} · ${escapeHtml(call.time)}</span></span>
        <span>${statusMarkup(classification)}</span>
        <span class="confidence-cell" title="${live ? "ASR quality of the decisive passage (exp(avg_logprob)); a heuristic, not a probability" : "Illustrative mock confidence; not a verified probability"}"><span class="confidence-value">${call.confidence === null ? "n/a" : `${call.confidence}%`}</span><span class="confidence-bar" aria-hidden="true"><i style="width:${call.confidence || 0}%"></i></span></span>
        <span class="duration-cell">${formatTime(call.duration)}</span>
      </button>`;
    }).join("");
  }

  function selectedCall() { return calls.find((call) => call.id === state.selectedId); }

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
    return `<div class="condition"><span>${escapeHtml(condition.label)}</span><span class="status-label condition-state ${cls}">${escapeHtml(condition.state)}</span></div>`;
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

  function renderDetail(options = {}) {
    const call = selectedCall();
    const classification = classificationFor(call);
    const evidence = call.evidence[0];
    const keywordOccurrences = call.transcript.reduce((sum, line) => sum + activeKeywordMatches(line.text).length, 0);
    const token = ++state.selectionToken;
    stopReveal();
    stopAudio();
    const detail = $("#call-detail");
    detail.innerHTML = `<div class="detail-inner">
      <div class="mobile-detail-head"><button id="mobile-back" class="btn btn-sm btn-ghost" type="button">${icon("back")} Call queue</button><span class="badge badge-ghost badge-sm">${live ? "Pipeline result" : "Mock call"}</span></div>
      <header id="classification-section" class="detail-header guide-target">
        <div><div class="eyebrow">${escapeHtml(call.family)} compliance</div><div class="detail-title-line"><h2>${escapeHtml(call.id)}</h2>${statusMarkup(classification)}</div><p class="detail-meta">${escapeHtml(call.date)} · ${escapeHtml(call.time)} · ${formatTime(call.duration)}</p></div>
        <div class="detail-actions"><button id="replay-guide" class="btn btn-xs btn-ghost replay-guide" type="button">${icon("replay")} Replay guide</button><div class="mock-confidence"><strong>${call.confidence === null ? "n/a" : `${call.confidence}%`}</strong><span>${live ? "ASR quality<br>not a probability" : "Mock confidence<br>not a probability"}</span></div></div>
      </header>
      <div class="detail-grid">
        <div class="detail-column">
          <section id="assessment-section" class="detail-card guide-target">
            <div class="section-head"><div class="section-title"><span class="section-icon">${icon("reason")}</span><div><h3>Assessment reason</h3><p>${escapeHtml(call.family)} policy family</p></div></div><span class="badge badge-sm family-tag">${escapeHtml(call.family)}</span></div>
            <p class="assessment-copy">${escapeHtml(assessmentFor(call))}</p>
          </section>
          <section id="evidence-section" class="detail-card guide-target">
            <div class="section-head"><div class="section-title"><span class="section-icon">${icon("evidence")}</span><div><h3>Evidence preview</h3><p>Supporting passage in context <span class="badge badge-ghost badge-xs keyword-occurrences">${keywordOccurrences} keyword ${keywordOccurrences === 1 ? "match" : "matches"}</span></p></div></div></div>
            <div class="evidence-window"><div class="evidence-meta"><span><strong>${formatTime(evidence.time)}</strong> · ${escapeHtml(evidence.speaker)}</span><span class="badge badge-xs context-badge">≈10 sec before + after</span></div><p id="evidence-text" class="evidence-text" aria-hidden="true"></p><p class="sr-only">${escapeHtml(evidence.text)}</p></div>
            <div class="mark-legend"><span><i class="legend-swatch"></i>Policy support</span><span><i class="legend-swatch keyword"></i>Enabled keyword</span></div>
            <button id="show-full-text" type="button" class="btn btn-xs btn-ghost show-full">Show full text</button>
          </section>
          <details class="detail-card transcript-collapse">
            <summary><span class="section-title"><span class="section-icon">${icon("transcript")}</span><span><strong>Transcript</strong><small style="display:block;color:var(--muted);font-size:9.5px">Compact view · keyword highlights</small></span></span></summary>
            <div class="transcript-body">${call.transcript.map((line) => `<div class="transcript-line"><span class="transcript-time">${formatTime(line.time)}</span><span class="transcript-speaker">${escapeHtml(line.speaker)}</span><span>${markedText(line.text)}</span></div>`).join("")}</div>
          </details>
        </div>
        <div class="detail-column">
          <section id="audio-section" class="detail-card guide-target">
            <div class="section-head"><div class="section-title"><span class="section-icon">${icon("audio")}</span><div><h3>Audio replay</h3><p>Verify the passage with context</p></div></div><span class="badge badge-xs simulated-tag">${live ? "Original recording" : "Simulated player"}</span></div>
            ${live ? `<audio id="audio-element" preload="metadata" src="/audio/${encodeURIComponent(call.id)}.wav"></audio>` : ""}
            <div class="audio-player"><div class="audio-main"><button id="play-button" class="btn btn-primary btn-sm play-button" type="button" aria-label="Play ${live ? "" : "simulated "}audio"><svg viewBox="0 0 24 24" aria-hidden="true"><path id="play-icon-path" d="m9 7 8 5-8 5V7Z" fill="currentColor" stroke="none"/></svg></button><div class="audio-track"><input id="audio-range" class="range range-xs audio-range" type="range" min="0" max="${call.duration}" step="0.1" value="0" aria-label="Audio position"><div class="audio-times"><span id="elapsed-time">0:00</span><span>${formatTime(call.duration)}</span></div></div></div><div class="jump-row"><span>Jump to evidence${live ? " (−10 s)" : ""}</span>${call.evidence.map((item, index) => `<button class="btn btn-xs btn-outline jump-evidence" type="button" data-jump="${live ? Math.max(0, item.time - 10) : item.time}" title="${escapeHtml(item.text)}">${index + 1} · ${formatTime(item.time)}</button>`).join("")}</div>${live && call.evidence.some((item) => item.segments.length) ? `<div class="jump-row"><span>Download clip (±10 s)</span>${call.evidence.filter((item) => item.segments.length).map((item, index) => `<a class="btn btn-xs btn-ghost" href="/clip/${encodeURIComponent(call.id)}.wav?start=${item.time}&end=${item.end}" download>${index + 1} · ${formatTime(Math.max(0, item.time - 10))}–${formatTime(item.end + 10)}</a>`).join("")}</div>` : ""}</div>
          </section>
          <section id="policy-section" class="detail-card guide-target">
            <div class="section-head"><div class="section-title"><span class="section-icon">${icon("policy")}</span><div><h3>Policy conditions</h3><p>Facts used in this assessment</p></div></div></div>
            <div class="condition-list">${call.conditions.map(conditionMarkup).join("")}</div>
          </section>
          ${unresolvedMarkup(call, classification)}
        </div>
      </div>
    </div>`;

    bindDetailEvents(token);
    if (options.immediate || reducedMotion()) finishReveal(token, options.startGuide !== false);
    else startReveal(call, token, options.startGuide !== false);
  }

  function startReveal(call, token, startGuide) {
    const textElement = $("#evidence-text");
    const button = $("#show-full-text");
    const text = call.evidence[0].text;
    textElement.classList.add("reveal-cursor");
    let index = 0;
    const delay = Math.max(18, Math.min(38, Math.floor(2300 / text.length)));
    state.revealTimer = setInterval(() => {
      if (token !== state.selectionToken) return stopReveal();
      index += 2;
      textElement.textContent = text.slice(0, index);
      if (index >= text.length) finishReveal(token, startGuide);
    }, delay);
    button.hidden = false;
  }

  function finishReveal(token, startGuide) {
    if (token !== state.selectionToken) return;
    stopReveal();
    const call = selectedCall();
    const element = $("#evidence-text");
    if (!element) return;
    element.classList.remove("reveal-cursor");
    element.innerHTML = markedText(call.evidence[0].text, call.evidence[0].support);
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
    $("#show-full-text").addEventListener("click", () => finishReveal(token, true));
    $("#replay-guide").addEventListener("click", () => startCallGuide(false));
    $("#mobile-back").addEventListener("click", closeMobileDetail);
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
    $$(".jump-evidence").forEach((button) => button.addEventListener("click", () => updateAudio(Number(button.dataset.jump))));
    $$("#classification-section, #assessment-section, #evidence-section, #audio-section, #policy-section, #open-question-section, #threshold-note-section").forEach((section) => {
      if (section) section.addEventListener("pointerdown", pauseGuideTimer);
    });
  }

  function audioElement() { return live ? $("#audio-element") : null; }

  function toggleAudio() {
    stopGuide();
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
    if (el && !fromElement) {
      el.currentTime = state.audioElapsed;
      if (el.paused) el.play();
    }
    const range = $("#audio-range");
    const elapsed = $("#elapsed-time");
    if (range) range.value = state.audioElapsed;
    if (elapsed) elapsed.textContent = formatTime(state.audioElapsed);
  }

  function stopAudio() {
    const el = audioElement();
    if (el) el.pause();
    clearInterval(state.audioTimer);
    state.audioTimer = null;
    state.playing = false;
    state.audioElapsed = 0;
  }

  function renderKeywordPopover() {
    const query = state.keywordQuery.toLowerCase();
    const groups = Object.entries(keywordGroups).map(([family, keywords]) => {
      const visible = keywords.filter((keyword) => keyword.toLowerCase().includes(query) || family.toLowerCase().includes(query));
      if (!visible.length) return "";
      return `<div class="keyword-group"><h4>${escapeHtml(family)}</h4>${visible.map((keyword) => `<label class="keyword-option"><input class="checkbox checkbox-sm checkbox-primary" type="checkbox" value="${escapeHtml(keyword)}" ${state.enabledKeywords.has(keyword) ? "checked" : ""}><span>${escapeHtml(keyword)}</span></label>`).join("")}</div>`;
    }).join("");
    $("#keyword-popover").innerHTML = `<div class="keyword-head"><div><h3>Keyword highlights</h3><p>Highlights only · classifications stay unchanged</p></div><div class="keyword-actions"><button class="btn btn-xs btn-ghost" id="select-all-keywords" type="button">All</button><button class="btn btn-xs btn-ghost" id="clear-keywords" type="button">Clear</button></div></div><label class="input input-sm keyword-search"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg><input id="keyword-search" type="search" value="${escapeHtml(state.keywordQuery)}" placeholder="Find a keyword"></label>${groups || '<p class="empty-state">No keywords found.</p>'}`;
    $("#keyword-count").textContent = state.enabledKeywords.size;
    $("#keyword-search").addEventListener("input", (event) => { state.keywordQuery = event.target.value; renderKeywordPopover(); $("#keyword-search").focus(); });
    $("#select-all-keywords").addEventListener("click", () => { state.enabledKeywords = new Set(allKeywords); updateKeywords(); });
    $("#clear-keywords").addEventListener("click", () => { state.enabledKeywords.clear(); updateKeywords(); });
    $$(".keyword-option input", $("#keyword-popover")).forEach((checkbox) => checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.enabledKeywords.add(checkbox.value); else state.enabledKeywords.delete(checkbox.value);
      updateKeywords();
    }));
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
    state.audioElapsed = 0;
    renderQueue();
    renderDetail();
    if (innerWidth <= 760) openMobileDetail();
    $("#sr-status").textContent = `${id} selected. ${classificationFor(selectedCall())}.`;
  }

  function openMobileDetail() { $("#call-detail").classList.add("mobile-open"); $("#mobile-backdrop").hidden = false; }
  function closeMobileDetail() { stopGuide(); stopAudio(); $("#call-detail").classList.remove("mobile-open"); $("#mobile-backdrop").hidden = true; }

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
      if (!popover.hidden) $("#keyword-search").focus();
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest("#keyword-section")) { $("#keyword-popover").hidden = true; $("#keyword-button").setAttribute("aria-expanded", "false"); }
    });
    $("#page-guide-button").addEventListener("click", startPageGuide);
    $("#guide-next").addEventListener("click", () => { pauseGuideTimer(); advanceGuide(1); });
    $("#guide-back").addEventListener("click", () => { pauseGuideTimer(); advanceGuide(-1); });
    $("#guide-skip").addEventListener("click", stopGuide);
    $("#guide-hint").addEventListener("pointerenter", pauseGuideTimer);
    $("#mobile-backdrop").addEventListener("click", closeMobileDetail);
    addEventListener("resize", () => { if (state.guide) showGuideStep(); if (innerWidth > 760) closeMobileDetail(); });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape") { stopGuide(); closeMobileDetail(); $("#keyword-popover").hidden = true; } });
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
      ({ calls, keywordGroups } = data);
      thresholdValues = data.thresholds;
      $("#data-badge-text").textContent = `Live · ${data.calls.length} calls · ${data.meta.chat_model}` + (data.pending.length ? ` · ${data.pending.length} pending` : "");
      $("#threshold-badge").textContent = "ASR quality";
      $("#quality-column").textContent = "ASR quality";
    } else {
      ({ calls, keywordGroups } = window.CallGuardData);
    }
    allKeywords = Object.values(keywordGroups).flat();
    state = window.CallGuardState.createUiState(calls, allKeywords);
    if (live) state.threshold = data.defaultThreshold;
    init();
  }

  boot();
})();
