(function (root) {
  "use strict";

  const minimumSeverity = Object.freeze({ sensitive: 1, balanced: 2, conservative: 3 });

  function classificationFor(call, threshold) {
    if (call.classificationByThreshold) return call.classificationByThreshold[threshold];
    if (call.decisionType === "fixed-alarm") return "Alarm";
    if (call.decisionType === "excluded") return "No alert";
    if (call.decisionType === "missing-fact") return "Review";
    return call.severity >= minimumSeverity[threshold] ? "Alarm" : "Review";
  }

  function assessmentFor(call, threshold) {
    return call.assessmentByThreshold ? call.assessmentByThreshold[threshold] : call.assessment;
  }

  const api = Object.freeze({ classificationFor, assessmentFor });
  root.CallGuardDecision = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
