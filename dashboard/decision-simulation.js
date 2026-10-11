(function (root) {
  "use strict";

  const minimumSeverity = Object.freeze({ "0.6": 1, "0.7": 2, "0.8": 3 });

  function classificationFor(call, threshold) {
    // A technical failure (model unreachable, extraction incomplete) is operationally distinct from a
    // genuine Review: it needs a retry, not a compliance read. It outranks the threshold-based label.
    if (call.runStatus === "failed") return "Failed";
    if (call.classificationByThreshold) return call.classificationByThreshold[threshold];
    if (call.decisionType === "fixed-alarm") return "Alarm";
    if (call.decisionType === "excluded") return "No alert";
    if (call.decisionType === "missing-fact") return "Review";
    return call.severity >= minimumSeverity[threshold] ? "Alarm" : "Review";
  }

  function assessmentFor(call, threshold) {
    return call.assessmentByThreshold?.[threshold] || call.assessment;
  }

  const api = Object.freeze({ classificationFor, assessmentFor });
  root.CallGuardDecision = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
