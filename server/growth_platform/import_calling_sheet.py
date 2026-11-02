"""
Calling Sheet -> csps.json

Re-runnable ETL. Reads the operational Calling Sheet workbook and normalises the
one tab that matters ("Calling Sheet New") into a flat JSON record per CSP.

This is deliberately PURE extraction: it copies what the sheet says and cleans
types, but it does not decide who is active, who has a gap, or who is
contradicted. Those are business judgements and live in app/engine.py, so they
can be changed without re-importing, and so the rules are reviewable in one
place instead of buried in a data script.

Usage:
    python import_calling_sheet.py [path-to-xlsx] [-o out.json]
    python import_calling_sheet.py --live [-o out.json]
    python import_calling_sheet.py "<google-sheets-url>" [-o out.json]

The source can be a local .xlsx, a Google Sheets URL, or --live to fetch the
sheet this project was built against (LIVE_SHEET_URL below). A URL source is
downloaded via the sheet's own /export?format=xlsx endpoint - the SAME file
format as a manual File > Download > Microsoft Excel, so it lands in the exact
column layout the rest of this script already expects. No credentials needed:
this only works because the sheet is shared as "Anyone with the link can view".
If that sharing setting is ever tightened, live fetching stops working and the
error will say so - falling back to a manual download is always available.

Columns are located by matching the sheet's own header text (row 2; row 1
holds merged group labels) via resolve_columns() below, not by a fixed
letter - see its docstring for why. If the sheet's layout still manages to
defeat that (a field's header wording itself changes), resolve_columns()
raises instead of guessing, so a bad sheet fails the import loudly rather
than silently overwriting csps.json with garbage - which is what happened
on 2026-08-24, when the ops team reordered columns and the old
letter-position COLS dict kept reading whatever now sat in that slot. Fix
new/renamed headers by adjusting the patterns in resolve_columns(), not by
hunting for a column letter.
"""

import argparse
import json
import math
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, date, timezone
from typing import Optional

try:
    import pandas as pd
except ImportError:
    # ImportError, not sys.exit: inside the Device Monitor this module is imported by the background refresh, and
    # sys.exit there would take the whole dashboard down with it.
    raise ImportError("pandas is required:  pip install pandas openpyxl")


DEFAULT_XLSX = r"D:\Calling Sheet.xlsx"
SHEET = "Calling Sheet New"
HEADER_ROW = 1  # 0-based; real headers are the 2nd row

# The sheet this project is built against - what --live fetches. Update this
# if the CSP team ever moves the calling sheet to a different spreadsheet.
LIVE_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1KHy7ejiUWZg9EQA0x4lm0axnVS5Hpiw1vLruc-0jSOg/edit?gid=1807123739"
)

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_GID_RE = re.compile(r"[?&#]gid=(\d+)")


def is_google_sheets_source(source: str) -> bool:
    return "docs.google.com/spreadsheets" in source or source.startswith("live-sheet-id:")


def fetch_live_xlsx(url_or_id: str) -> str:
    """
    Downloads a Google Sheet as .xlsx via its own export endpoint - the same
    file a human gets from File > Download > Microsoft Excel - and returns the
    path to a temp copy. Works without any credentials because the sheet is
    shared as "Anyone with the link can view"; if that ever changes, the
    download will fail with an HTTP error instead of silently returning stale
    or partial data.

    Deliberately NOT the /gviz/tq CSV endpoint: that one was tried first and
    turned out to drop the header text for several columns (including the
    mATM transaction counts) while still including their data, which is a
    silent-misalignment risk not worth carrying into a pipeline. The xlsx
    export produces byte-identical structure to a manual download, verified
    against the column layout the rest of this script expects.
    """
    if url_or_id.startswith("live-sheet-id:"):
        sheet_id, gid = url_or_id.split(":", 1)[1], None
    else:
        match = _SHEET_ID_RE.search(url_or_id)
        if not match:
            raise ValueError(f"Could not find a spreadsheet ID in: {url_or_id}")
        sheet_id = match.group(1)
        gid_match = _GID_RE.search(url_or_id)
        gid = gid_match.group(1) if gid_match else None

    export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
    if gid:
        export_url += f"&gid={gid}"

    request = urllib.request.Request(export_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
    except urllib.error.HTTPError as err:
        raise RuntimeError(
            f"Could not download the live sheet (HTTP {err.code}). Most likely "
            "cause: sharing was changed away from \"Anyone with the link can "
            "view\". Fall back to a manual File > Download > Microsoft Excel "
            "and pass that file's path instead."
        ) from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"Could not reach Google Sheets: {err.reason}") from err

    if not data.startswith(b"PK"):   # .xlsx is a zip archive; this signature is cheap insurance
        raise RuntimeError(
            "The download did not look like a real .xlsx file - Google may have "
            "returned a sign-in page instead of the sheet. Check the sharing "
            "settings on the spreadsheet."
        )

    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx", prefix="calling_sheet_live_")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return tmp_path


def _header_text(h) -> str:
    """Collapses a raw pandas column header (which may carry stray
    whitespace, e.g. "CSP Code " or "Relationship Manager ") down to
    something safe to pattern-match on."""
    return re.sub(r"\s+", " ", str(h)).strip()


def _month_pattern(d: date) -> str:
    """A regex alternation matching this month written either full
    ("July'26") or abbreviated ("Jul'26") - the sheet uses both spellings
    depending on which metric the column is (verified against the live
    header row 2026-08-24: transaction-count columns spell the month out,
    everything else abbreviates it)."""
    full, abbr, yy = d.strftime("%B"), d.strftime("%b"), d.strftime("%y")
    return rf"(?:{re.escape(full)}|{re.escape(abbr)})'{yy}"


def _shift_months(d: date, n: int) -> date:
    """First-of-month date shifted by n calendar months (n may be negative) -
    used to build month-column labels without a fixed-days approximation,
    which would skip or repeat a short month."""
    month0 = d.month - 1 + n
    return date(d.year + month0 // 12, month0 % 12 + 1, 1)


def _find_unique(headers: list, pattern: str, field: str) -> int:
    """Returns the 0-based index of the ONE header matching `pattern`
    (case-insensitive, whole-header match). Raises if zero or more than one
    column matches, rather than guessing - a silent guess here is exactly
    how the 2026-08-24 column-reorder corrupted the whole CSP base without
    anyone noticing until the dashboard's coverage went to 0/0. Failing the
    import instead leaves run.py serving yesterday's good csps.json, which
    is always safer than writing fresh garbage over it."""
    rx = re.compile(pattern, re.IGNORECASE)
    matches = [i for i, h in enumerate(headers) if rx.fullmatch(_header_text(h))]
    if len(matches) != 1:
        raise RuntimeError(
            f"Could not uniquely locate the '{field}' column in the live sheet "
            f"(pattern {pattern!r} matched {len(matches)} header(s): "
            f"{[_header_text(headers[i]) for i in matches]}). The sheet's "
            f"layout or wording has likely changed - check it by hand (and "
            f"update the pattern in resolve_columns()) before trusting an "
            f"import built from it."
        )
    return matches[0]


# The only columns this platform reads. Everything else in the sheet (targets,
# scheme achievements, per-day call logs) belongs to the RM workflow, not to
# device adoption, so it is deliberately left alone.
def resolve_columns(headers: list, *, today: Optional[date] = None) -> dict:
    """Maps each field this platform reads to a column index by matching the
    SHEET'S OWN HEADER TEXT for that row, instead of a fixed letter position.

    Column letters broke on 2026-08-24: the ops team reordered/inserted
    columns upstream, which silently shifted every fixed-letter mapping and
    sent the entire 535-CSP base to "inactive" with no error anywhere. Header
    text survives a reorder as long as the wording itself doesn't change,
    which is the much rarer failure - and if it ever does, _find_unique()
    above raises instead of reading the wrong column.

    Most headers are static ("District", "State"...) and matched by exact
    text. A handful are the sheet's own count-for-THIS-month /
    count-for-last-month columns, so their header text changes every month
    (e.g. "Total Transaction Count July'26" -> "...August'26" a month later) -
    those are matched by a pattern built from `today` (defaults to real
    today; a caller can pass a fixed date to test month-rollover)."""
    today = today or date.today()
    this_month = today.replace(day=1)
    prev_month = _shift_months(this_month, -1)

    patterns = {
        "cspCode":        r"CSP Code",
        "name":           r"CSP Name",
        "email":          r"CSP Mail ID",
        "mobile":         r"CSP Mobile [Nn]umber",
        "category":       r"CSP Category",
        "rm":             r"Relationship Manager",
        "district":       r"District",
        "state":          r"State",
        "population":     r"Population",
        "terminalStatus": r"Terminal Status",
        "inactivityText": rf"Inactivity Days {_month_pattern(this_month)}",
        "txnPrevMonth":   rf"Total Transaction Count {_month_pattern(prev_month)}",
        "txnThisMonth":   rf"Total Transaction Count {_month_pattern(this_month)}",
        "matmTxnPrev":    rf"mATM Transaction Count {_month_pattern(prev_month)}",
        "matmTxnThis":    rf"mATM Transaction Count {_month_pattern(this_month)}",
        # "No mATM" / "N Days inactive" / "No mATM Txn"
        "matmInactText":  rf"mATM Inactivity Days {_month_pattern(this_month)}",
        "pbPrintsPrev":   rf"MTD Passbook printing count {_month_pattern(prev_month)}",
        "pbPrintsThis":   rf"MTD Passbook printing count {_month_pattern(this_month)}",
        "loginDays":      rf"Total Login Day \({_month_pattern(this_month)}\)",
        # Commission columns are bare month labels ("Jul'26") with no other
        # words to anchor on - the sheet also reuses that exact text
        # elsewhere (a target/achievement column), which is why this must be
        # a FULL match, not a substring: pandas already disambiguates a
        # duplicate header by suffixing every repeat after the first with
        # ".2", ".3", ... so the clean, unsuffixed name is always the FIRST
        # occurrence in sheet order, which is this commission block.
        "commissionM3":   _month_pattern(_shift_months(prev_month, -3)),
        "commissionM2":   _month_pattern(_shift_months(prev_month, -2)),
        "commissionM1":   _month_pattern(_shift_months(prev_month, -1)),
        "commission":     _month_pattern(prev_month),   # latest full month
        "matmStatus":     r"mATM Status \(Yes/No\)",
        "printerStatus":  r"Passbook Printer Status",
        "gpsStatus":      r"GPS Dongle Status",
        "totalAccounts":  r"Total Accounts",
        "avgBalance":     r"Avg Balance",
    }
    return {field: _find_unique(headers, pattern, field) for field, pattern in patterns.items()}


def clean_text(v):
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "nat", "none") else s


def clean_num(v):
    """Numeric or None. Strips currency symbols and separators."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and math.isnan(v)) else float(v)
    s = re.sub(r"[^\d.\-]", "", str(v))
    if s in ("", "-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def num0(v):
    """Numeric defaulting to 0 - for counts, where blank genuinely means none."""
    n = clean_num(v)
    return 0.0 if n is None else n


def normalise_mobile(v):
    """
    Indian mobile -> 91XXXXXXXXXX, or None when it cannot be one.

    The sheet stores these as floats ("9800000000.0"), so the trailing ".0" has
    to go before length checks - otherwise every number looks 12 digits long.
    """
    s = clean_text(v)
    if not s:
        return None
    s = re.sub(r"\.0+$", "", s)
    d = re.sub(r"\D", "", s)
    if len(d) == 12 and d.startswith("91"):
        return d
    if len(d) == 11 and d.startswith("0"):
        d = d[1:]
    if len(d) == 10 and d[0] in "6789":   # Indian mobiles start 6-9
        return "91" + d
    return None


def normalise_email(v):
    s = clean_text(v).lower()
    # The sheet has multi-address cells ("a@x.com, b@y.com") - take the first.
    s = s.split(",")[0].strip()
    return s if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s) else None


def parse_inactivity_days(text):
    """'9 Days inactive' -> 9. '0 Days inactive' -> 0. Anything else -> None."""
    m = re.search(r"(\d+)\s*days?\s*inactive", clean_text(text), re.I)
    return int(m.group(1)) if m else None


def yes_no(v):
    s = clean_text(v).lower()
    if s in ("yes", "y", "true", "1"):
        return True
    if s in ("no", "n", "false", "0"):
        return False
    return None          # genuinely unknown - not the same as "No"


def jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def load(path):
    df = pd.read_excel(path, sheet_name=SHEET, header=HEADER_ROW, engine="openpyxl")
    code_idx = resolve_columns(list(df.columns))["cspCode"]
    df = df[df.iloc[:, code_idx].notna()].reset_index(drop=True)
    return df


def build_records(df):
    idx = resolve_columns(list(df.columns))

    def cell(row, key):
        i = idx[key]
        return row.iloc[i] if i < len(row) else None

    records, seen = [], {}
    duplicates = []

    for _, row in df.iterrows():
        code = clean_text(cell(row, "cspCode"))
        if not code:
            continue

        matm_inact_text = clean_text(cell(row, "matmInactText"))
        mobile = normalise_mobile(cell(row, "mobile"))
        email = normalise_email(cell(row, "email"))

        rec = {
            "cspCode": code,
            "name": clean_text(cell(row, "name")) or code,
            "mobile": mobile,
            "email": email,
            "contactable": bool(mobile or email),
            "category": clean_text(cell(row, "category")),
            "rm": clean_text(cell(row, "rm")) or "Unassigned",
            "district": clean_text(cell(row, "district")),
            "state": clean_text(cell(row, "state")),
            "population": clean_text(cell(row, "population")),

            "terminalStatus": clean_text(cell(row, "terminalStatus")),
            "inactivityDays": parse_inactivity_days(cell(row, "inactivityText")),
            "loginDays": num0(cell(row, "loginDays")),

            # Activity. "prev" is the last full month, "this" is month-to-date,
            # so the two are summed wherever a volume signal is wanted rather
            # than compared against each other.
            "txnPrev": num0(cell(row, "txnPrevMonth")),
            "txnThis": num0(cell(row, "txnThisMonth")),
            "matmTxnPrev": num0(cell(row, "matmTxnPrev")),
            "matmTxnThis": num0(cell(row, "matmTxnThis")),
            "pbPrintsPrev": num0(cell(row, "pbPrintsPrev")),
            "pbPrintsThis": num0(cell(row, "pbPrintsThis")),

            "commission": clean_num(cell(row, "commission")),
            "commissionHistory": [
                clean_num(cell(row, "commissionM3")),
                clean_num(cell(row, "commissionM2")),
                clean_num(cell(row, "commissionM1")),
            ],

            # Claimed ownership, exactly as the sheet states it. The engine
            # decides whether to believe it.
            "claimsMatm": yes_no(cell(row, "matmStatus")),
            "claimsPrinter": yes_no(cell(row, "printerStatus")),
            "claimsGps": yes_no(cell(row, "gpsStatus")),
            "matmInactivityText": matm_inact_text,
            # The ops team writes "No mATM" for no device but "No mATM Txn" for
            # a device that is idle - a distinction worth keeping, because it is
            # a second independent opinion on ownership.
            "matmInactSaysNoDevice": matm_inact_text.lower().startswith("no matm")
                                     and "txn" not in matm_inact_text.lower(),

            "totalAccounts": num0(cell(row, "totalAccounts")),
            "avgBalance": clean_num(cell(row, "avgBalance")),
        }
        rec = {k: jsonable(v) for k, v in rec.items()}

        if code in seen:
            # Keep whichever row carries more information rather than letting
            # sheet order decide - a duplicate is usually a stale first entry.
            duplicates.append(code)
            prev = records[seen[code]]
            if _filled(rec) > _filled(prev):
                records[seen[code]] = rec
            continue

        seen[code] = len(records)
        records.append(rec)

    return records, duplicates


def _filled(rec):
    return sum(1 for v in rec.values() if v not in (None, "", 0, 0.0, False))


def _default_out_path() -> str:
    """csps.json in GROWTH_DATA_DIR - the same place core/store.py reads it from."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.environ.get("GROWTH_DATA_DIR") or os.path.join(here, "data"), "csps.json")


def run_import(source: str, out_path: str = None) -> dict:
    """
    Does the actual fetch -> parse -> write work for one source, and returns
    the payload written (so a caller - main() below, or run.py's background
    refresh - can report on it without re-reading the file). Factored out of
    main() so the live-refresh scheduler can call this in-process instead of
    shelling out to this script as a subprocess.
    """
    source_label = source   # what gets recorded in csps.json's "source" field
    xlsx_path = source
    temp_path = None
    if is_google_sheets_source(source):
        temp_path = fetch_live_xlsx(source)
        xlsx_path = temp_path

    if not os.path.exists(xlsx_path):
        raise FileNotFoundError(f"Workbook not found: {xlsx_path}")

    out_path = os.path.abspath(out_path or _default_out_path())

    try:
        df = load(xlsx_path)
        records, duplicates = build_records(df)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)   # never leave a copy of real CSP data in a temp dir

    payload = {
        "source": source_label,
        "sheet": SHEET,
        # Timezone-aware UTC, matching store.now_iso() and every other
        # timestamp this platform writes. It used to be a naive datetime.now()
        # (local wall-clock), which run.py's staleness check then read as if it
        # were UTC - so in IST the import always looked 5.5 hours younger than
        # it was, and the "daily" refresh silently ran every 29.5 hours.
        "importedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rowCount": int(len(df)),
        "cspCount": len(records),
        "duplicateCodes": sorted(set(duplicates)),
        "csps": records,
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    return payload


def import_live(out_path: str = None) -> dict:
    """What run.py's daily background refresh calls - always the live sheet,
    never a local file, since a scheduled job has no human standing by to
    point it at one."""
    return run_import(LIVE_SHEET_URL, out_path)


def main():
    ap = argparse.ArgumentParser(description="Import the Calling Sheet into csps.json")
    ap.add_argument("xlsx", nargs="?", default=None,
                     help="Local .xlsx path, or a Google Sheets URL. "
                          f"Defaults to the local file at {DEFAULT_XLSX!r} unless --live is given.")
    ap.add_argument("--live", action="store_true",
                     help="Fetch the live Calling Sheet instead of a local file.")
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()

    source = args.xlsx
    if args.live:
        if source:
            sys.exit("Pass either a source or --live, not both.")
        source = LIVE_SHEET_URL
    elif source is None:
        source = DEFAULT_XLSX

    if is_google_sheets_source(source):
        print(f"Fetching live sheet from {source} ...")

    try:
        payload = run_import(source, args.out)
    except (ValueError, RuntimeError, FileNotFoundError) as err:
        sys.exit(str(err))

    out_path = os.path.abspath(args.out or _default_out_path())
    records = payload["csps"]
    reachable = sum(1 for r in records if r["contactable"])
    print(f"Source                 : {payload['source']}")
    print(f"Rows read              : {payload['rowCount']}")
    print(f"CSPs written           : {payload['cspCount']}")
    if payload["duplicateCodes"]:
        print(f"Duplicate codes merged : {len(payload['duplicateCodes'])} -> {payload['duplicateCodes']}")
    print(f"Contactable (mob/email): {reachable} ({reachable / max(len(records),1) * 100:.1f}%)")
    print(f"Output                 : {out_path}")


if __name__ == "__main__":
    main()
