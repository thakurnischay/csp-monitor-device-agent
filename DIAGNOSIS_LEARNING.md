# Diagnosis learning (human-approved)

The agent does not change its own rules. It records what happens, the server collects it, and a person decides what to change.

**Stage 1 - agent (2.3.0+)** `agent/outcomes.py`: for every diagnosed problem the agent keeps one record from first sighting to
clearing: how long, whether a fix ran and succeeded, whether the problem came back within 72 hours. It stores codes, device names,
times and action results only (no evidence text, no customer data), is bounded (90 days / 500 problems), never changes a
diagnosis and never raises. File: `diagnostic_outcomes.json` next to the diagnostic history. `python outcomes.py` prints a PC's summary.

**Stage 2 - server** `server/learning.py`:
- Each heartbeat carries the problems closed in the last 7 days (max 40 rows). Stored idempotently in `diagnosis_outcomes`
  (keyed by PC, device, code, first-seen), validated and clipped; a malformed list can never fail a heartbeat.
- On a PC's page, each open issue has **Was this right? Correct / Wrong / Not sure**, stored in `diagnosis_feedback`. It changes nothing on the PC.
- **Diagnosis Outcomes** page (sidebar): per problem type - finished cases, PCs affected, open now, typical time to clear, fixes tried
  and how many held, came-back rate, administrator verdicts, and an evidence label ("Not enough yet" below 5 finished cases).
  "Signals" are plain observations (e.g. "mostly clears by itself", "fix does not hold"); they appear only with enough evidence
  and never change a rule.

**Stage 3 (not built)**: turn signals into reviewed rule-change proposals, approved by a person and shipped as a signed update.
