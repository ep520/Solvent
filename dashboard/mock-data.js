(function () {
  "use strict";

  const calls = [
    {
      id: "CG-1048", date: "10 Oct 2026", time: "10:42", duration: 252, family: "Trade",
      confidence: 91, decisionType: "fixed-alarm", baseClassification: "Alarm",
      assessment: "The customer asks to buy shares before a confidential acquisition announcement, and the adviser acknowledges the timing.",
      evidence: [{ time: 96, end: 116, speaker: "Customer", text: "Before the acquisition is announced on Monday, place the order for 4,000 Helvetia Nova shares today.", support: "before the acquisition is announced on Monday|place the order for 4,000 Helvetia Nova shares" }],
      transcript: [
        { time: 82, speaker: "Adviser", text: "You mentioned a time-sensitive investment. What would you like to do?" },
        { time: 96, speaker: "Customer", text: "Before the acquisition is announced on Monday, place the order for 4,000 Helvetia Nova shares today." },
        { time: 116, speaker: "Adviser", text: "Understood. I will prepare that trade now." }
      ],
      conditions: [{ label: "Specific security and instruction", state: "Supported" }, { label: "Non-public event and timing", state: "Supported" }, { label: "Publicly available information", state: "Excluded" }]
    },
    {
      id: "CG-1047", date: "10 Oct 2026", time: "10:18", duration: 189, family: "Access",
      confidence: 36, decisionType: "excluded", baseClassification: "No alert",
      assessment: "Access-related words appear, but the customer is resetting a password through the approved process and shares no credential.",
      evidence: [{ time: 44, end: 64, speaker: "Adviser", text: "Please do not tell me your password or PIN. I can send the secure reset link to your registered device.", support: "do not tell me your password or PIN|secure reset link" }],
      transcript: [
        { time: 31, speaker: "Customer", text: "I forgot my password and my login is locked." },
        { time: 44, speaker: "Adviser", text: "Please do not tell me your password or PIN. I can send the secure reset link to your registered device." },
        { time: 64, speaker: "Customer", text: "Yes, please use my registered phone." }
      ],
      conditions: [{ label: "Credential requested or disclosed", state: "Excluded" }, { label: "Approved recovery channel", state: "Supported" }, { label: "Account access transferred", state: "Excluded" }]
    },
    {
      id: "CG-1046", date: "10 Oct 2026", time: "09:57", duration: 307, family: "Disclosure",
      confidence: 64, decisionType: "missing-fact", baseClassification: "Review",
      assessment: "The adviser discusses account holdings with another person, but the recording does not establish whether valid authority is on file.",
      missingFact: "Does the caller have valid authority to receive information about this account?",
      evidence: [{ time: 141, end: 161, speaker: "Adviser", text: "I can confirm the account holds two bond funds. I cannot see here whether your power of attorney is active.", support: "confirm the account holds two bond funds|cannot see here whether your power of attorney is active" }],
      transcript: [
        { time: 128, speaker: "Caller", text: "I manage the finances for my mother. Can you confirm what she holds?" },
        { time: 141, speaker: "Adviser", text: "I can confirm the account holds two bond funds. I cannot see here whether your power of attorney is active." },
        { time: 161, speaker: "Caller", text: "It should have been submitted last month." }
      ],
      conditions: [{ label: "Account information disclosed", state: "Supported" }, { label: "Recipient authority confirmed", state: "Unknown" }, { label: "Public information only", state: "Excluded" }]
    },
    {
      id: "CG-1045", date: "10 Oct 2026", time: "09:31", duration: 224, family: "Documentation",
      confidence: 77, decisionType: "threshold", severity: 2, baseClassification: "Alarm",
      assessmentByThreshold: {
        sensitive: "A supported request to keep suitability concerns outside the call record meets the sensitive escalation threshold.",
        balanced: "A supported request to keep suitability concerns outside the call record meets the balanced escalation threshold.",
        conservative: "The documentation-avoidance event is supported, but the conservative threshold routes it to review rather than an alarm."
      },
      evidence: [{ time: 72, end: 92, speaker: "Adviser", text: "We can discuss the suitability concern, but leave that part out of the written note so the file stays simple.", support: "leave that part out of the written note" }],
      transcript: [
        { time: 60, speaker: "Customer", text: "The product seems riskier than what we discussed before." },
        { time: 72, speaker: "Adviser", text: "We can discuss the suitability concern, but leave that part out of the written note so the file stays simple." },
        { time: 92, speaker: "Customer", text: "I would still like my concern recorded." }
      ],
      conditions: [{ label: "Material concern raised", state: "Supported" }, { label: "Request to omit record", state: "Supported" }, { label: "Administrative correction only", state: "Excluded" }]
    },
    {
      id: "CG-1044", date: "10 Oct 2026", time: "09:06", duration: 278, family: "Splitting",
      confidence: 71, decisionType: "threshold", severity: 1, baseClassification: "Review",
      assessmentByThreshold: {
        sensitive: "Repeated transfers just below the reporting level are supported and meet the sensitive escalation threshold.",
        balanced: "The transfer pattern is supported, but the balanced threshold routes this lower-severity event to review.",
        conservative: "The transfer pattern is supported, but the conservative threshold routes it to review rather than an alarm."
      },
      evidence: [{ time: 119, end: 139, speaker: "Customer", text: "Send 9,500 today and another 9,500 tomorrow, so neither transfer reaches the reporting level.", support: "9,500 today and another 9,500 tomorrow|neither transfer reaches the reporting level" }],
      transcript: [
        { time: 106, speaker: "Adviser", text: "You asked for a transfer of 19,000. Shall I prepare it as one payment?" },
        { time: 119, speaker: "Customer", text: "Send 9,500 today and another 9,500 tomorrow, so neither transfer reaches the reporting level." },
        { time: 139, speaker: "Adviser", text: "I need to review that instruction before proceeding." }
      ],
      conditions: [{ label: "Related transactions", state: "Supported" }, { label: "Intent to avoid a threshold", state: "Supported" }, { label: "Independent business purpose", state: "Unknown" }]
    },
    {
      id: "CG-1043", date: "10 Oct 2026", time: "08:42", duration: 156, family: "Trade",
      confidence: 22, decisionType: "excluded", baseClassification: "No alert",
      assessment: "The customer asks about a published market announcement and gives no instruction to trade on confidential information.",
      evidence: [{ time: 38, end: 58, speaker: "Customer", text: "I saw the acquisition announcement in this morning's newspaper. Has the share price already moved?", support: "this morning's newspaper" }],
      transcript: [
        { time: 28, speaker: "Adviser", text: "How can I help with your portfolio today?" },
        { time: 38, speaker: "Customer", text: "I saw the acquisition announcement in this morning's newspaper. Has the share price already moved?" },
        { time: 58, speaker: "Adviser", text: "Yes. The public market price reflects that news." }
      ],
      conditions: [{ label: "Corporate event mentioned", state: "Supported" }, { label: "Information is non-public", state: "Excluded" }, { label: "Trade instruction", state: "Excluded" }]
    },
    {
      id: "CG-1042", date: "9 Oct 2026", time: "16:25", duration: 341, family: "Access",
      confidence: 83, decisionType: "fixed-alarm", baseClassification: "Alarm",
      assessment: "The customer gives a one-time code after the adviser asks for it, creating a supported credential-disclosure event.",
      evidence: [{ time: 203, end: 223, speaker: "Customer", text: "The one-time code is 481 992. You can use it to enter the account now.", support: "one-time code is 481 992|use it to enter the account" }],
      transcript: [
        { time: 190, speaker: "Adviser", text: "Read me the code you just received so I can complete the login." },
        { time: 203, speaker: "Customer", text: "The one-time code is 481 992. You can use it to enter the account now." },
        { time: 223, speaker: "Adviser", text: "Thank you. I am in the account." }
      ],
      conditions: [{ label: "Authentication secret disclosed", state: "Supported" }, { label: "Adviser requested credential", state: "Supported" }, { label: "Approved recovery flow", state: "Excluded" }]
    }
  ];

  const keywordGroups = {
    Trade: ["acquisition", "announcement", "shares", "trade"],
    Access: ["password", "PIN", "one-time code", "login"],
    Disclosure: ["account", "holdings", "power of attorney"],
    Documentation: ["written note", "record", "suitability"],
    Splitting: ["reporting level", "transfer", "9,500"]
  };

  window.CallGuardData = Object.freeze({ calls, keywordGroups });
})();
