# Root-cause diagnostics (agent 2.0 + server)

What the monitor does **beyond** "is it plugged in", what evidence each conclusion
rests on, and - just as important - what it **cannot** know yet.

## 1. Why the earlier version was not enough

It answered one question: *does Windows list the device, and is its driver status OK?*
That cannot tell a working device from one that cannot earn revenue:

| Real situation | Old result |
|---|---|
| Scanner connected, RD service stopped | OK (device present) |
| Scanner connected, Windows reports "drivers not installed (Code 28)" | OK / unclear |
| Printer connected, paper out or jammed | OK (until the daily test page) |
| Device dropping off the USB bus 5 times a day | OK at each check |
| Device missing | "Problem - check it is plugged in" (no cause, no next step) |

Every red/green answer also lacked three things an operator needs: *why*, *what it
affects*, and *what to do*. And three earlier rules turned things the agent could not
prove (RD "NOTREADY", a silent GPS port, a generic USB entry) into false alarms on
working devices. Those are now notes or "unverified", never faults.

## 2. How a diagnosis is made

```
Windows / vendor evidence  ->  rules (each states a confidence)  ->  finding
        |                                                             |
   (real reads only)                              severity = confidence x workflow need x workaround
                                                                      |
   history (agent's own observations)  ------->  dashboard: cause, evidence, impact, steps, history
                                                                      |
                           next heartbeat: cause gone? -> RECOVERY VERIFIED (issue closed + event)
```

* **confirmed** - Windows or the device itself says so (an error code, a stopped
  service, the printer's own error state).
* **likely** - the evidence points there but does not prove it.
* **possible** - a lead only; never raises severity above *low*.

Only confirmed/likely findings can make a device look faulty.

### Severity (business impact, not device type)

| Severity | Rule |
|---|---|
| Critical | a confirmed blocking failure where operations has **confirmed** the workflow depends on the device (`impact_map.json`), or two+ confirmed blocking failures at one CSP |
| High | confirmed blocking failure, the workflow needs the device, **no workaround** known |
| Medium | likely failure, or a confirmed failure with a workaround (e.g. printer: receipts), or impact not established (GPS), or "not connected" after working recently |
| Low | possible lead, non-blocking condition (low paper), never-seen device |

### Hardware failure and replacement

Suspected only on repeated evidence: Windows code 43/41 (the device itself reported a
fault), 3+ disconnects in 24 h, or 3 failed test prints on 2+ days with driver,
spooler and printer status all normal. The advice is always sequenced -
different port -> cable -> another PC -> vendor inspection -> replacement - and says
replacement is justified only after those fail. One missed detection never suggests a
purchase.

## 3. Capability matrix

### Implemented (real evidence; unit-tested with realistic data, pipeline run on a real PC)

| Capability | Source |
|---|---|
| Device Manager error code **and Windows' own description** for every device, mapped to driver / software / suspected-hardware | `Get-PnpDevice` (`ConfigManagerErrorCode`, `ProblemDescription`) |
| Driver version, date, digital signature as evidence | `Win32_PnPSignedDriver` |
| Vendor service stopped although set to start automatically; Print Spooler stopped | `Win32_Service` (state, start mode, exit code) |
| Service crashing repeatedly (>= 2 in 24 h) | Windows System event log (Service Control Manager 7031/7034, start failures 7000/7001/7009/7011/7023/7024) |
| Scanner RD service: answering READY / NOT READY / running but silent / none found | local HTTP status request to ports 11100-11120 (never captures a fingerprint) |
| Windows' own services (e.g. Windows Hello biometric) excluded from "vendor software" | service path / name |
| Repeated disconnects; last time present; last time healthy | the agent's own observations (`diagnostic_history.json`) |
| Printer: out of paper / jam / door open / offline / no ribbon (physical, not a driver fault); "Use Printer Offline"; stuck print queue; repeated test-print failure | `Win32_Printer.DetectedErrorState`, `Win32_PrintJob`, daily test page |
| Micro-ATM: CSP application not running (when its process name is set) | process check |
| GPS: serial data check (fix / no fix) as **notes** | read-only NMEA read |
| Business-impact mapping with confirmed/assumed flag; revenue explicitly "not calculable" | `impact_map.py` / optional `impact_map.json` |
| Issue history, recurrence count, open/resolved, **recovery verified** | server `device_issues` + events |
| Operator-approved start of one stopped service, once, cooldown after failure, then verified | local agent page `/fix` |
| What was checked / what could not be checked, per device, on the dashboard | every diagnosis |

### Partially implemented

| Capability | Gap |
|---|---|
| Vendor software detection | by keyword (Mantra, Morpho, Idemia, SecuGen, Startek, Ingenico, Ezetap, ...). An unlisted vendor's service is not recognised - send `DIAGNOSE.bat` output to add it |
| Business impact | workflows are **assumptions** (labelled "assumed - not yet confirmed") until operations confirms them in `impact_map.json` |
| USB-level event log | collected, not yet turned into findings |
| Starting services | usually needs Administrator rights; the agent normally runs as the signed-in user, so it will say so and give manual steps |

### Not possible yet - what is needed

| Wanted | Needs |
|---|---|
| Micro-ATM transaction-level self-test, device error codes/logs | the vendor's SDK, diagnostic tool or log location (a live transaction is never used as a test) |
| "Detected but unavailable to the banking application" | the application's logs or an API to read them |
| Which transactions failed / revenue lost | a transaction feed (e.g. the micro-ATM transaction report already planned from the Calling Sheet) - then impact can be counted instead of described |
| Live biometric capture check | deliberately never done (no biometric data is collected); a vendor-supported health call could be added if the vendor supplies its spec |
| Network/backend dependency checks beyond reaching this monitor | the list of authorised endpoints to probe |
| Automatic driver reinstall / software repair | deliberately not automatic: needs the vendor-approved package and operator approval |
| How GPS is used by the CSP application | operations input (stays "impact not established" until given) |

## 4. Safety

* Read-only diagnostics only. No live financial transaction, no fingerprint or
  customer data is ever read or sent.
* The only change the agent can make is starting **one** Windows service that the
  current diagnosis names, **after the operator clicks and confirms**, once; a failed
  attempt is not repeated for 6 hours and the steps escalate (event log, repair).
* A rule bug or failed Windows query degrades to "could not be checked" - it never
  blocks the heartbeat or invents a fault.

## 5. Telling the agent about the business (impact_map.json)

Place `impact_map.json` next to the agent to override the defaults, e.g. after
operations confirms AePS depends on the scanner:

```json
{"biometric": {"workflows": [
  {"name": "AePS cash withdrawal / balance enquiry", "needs": "required", "confirmed": true}]}}
```
With `confirmed: true`, a confirmed failure of that device is rated **Critical** on its own.
