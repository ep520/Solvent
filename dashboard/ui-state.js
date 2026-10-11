(function (root) {
  "use strict";

  function createUiState(calls, keywords) {
    return {
      selectedId: calls && calls.length > 0 ? calls[0].id : null,
      threshold: "0.6",
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
      settingsOpener: null,
      autoGuideUsed: sessionStorage.getItem("callguard-guide-seen") === "1"
    };
  }

  root.CallGuardState = Object.freeze({ createUiState });
})(window);
