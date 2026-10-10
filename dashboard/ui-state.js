(function (root) {
  "use strict";

  function createUiState(calls, keywords) {
    return {
      selectedId: calls[0].id,
      threshold: "balanced",
      filter: "All",
      query: "",
      enabledKeywords: new Set(keywords),
      keywordQuery: "",
      selectionToken: 0,
      revealTimer: null,
      audioTimer: null,
      audioElapsed: 0,
      playing: false,
      guide: null,
      guideTimer: null,
      autoGuideUsed: sessionStorage.getItem("callguard-guide-seen") === "1"
    };
  }

  root.CallGuardState = Object.freeze({ createUiState });
})(window);
