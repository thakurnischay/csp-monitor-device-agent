"""Export an email campaign's audience to Excel, exactly as the campaign page shows it, plus the message template.

Run inside the dashboard container (it reads the live database):
    sudo docker exec -i csp-heartbeat-v2 python - <campaign id> [eligible|review|excluded] < export_campaign_audience.py
Writes /tmp/campaign_<id>_<bucket>.xlsx inside the container. Email addresses are masked, as on the page.
"""
import os
import sys

sys.path.insert(0, os.getcwd())                       # /app in the container: the dashboard's own modules

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

import routes_campaigns
from campaigns import operators as ops, service
from db import get_connection

cid = int(sys.argv[1]) if len(sys.argv) > 1 else 1
bucket = sys.argv[2] if len(sys.argv) > 2 else "eligible"
out_path = f"/tmp/campaign_{cid}_{bucket}.xlsx" if os.path.isdir("/tmp") else f"campaign_{cid}_{bucket}.xlsx"

with get_connection() as conn:
    c = service.get_campaign(conn, cid)
    rows, counts, _ = routes_campaigns._recipient_rows(conn, c, bucket, 1, per_page=100000)
    first = rows[0]["csp_code"] if rows else None
    example = service.preview(conn, cid, first) if first else None

HEAD = PatternFill("solid", fgColor="0E5A6E")
WHITE = Font(bold=True, color="FFFFFF")
BOLD = Font(bold=True)
WRAP = Alignment(wrap_text=True, vertical="top")

wb = Workbook()
ws = wb.active
ws.title = f"{bucket.title()} ({len(rows)})"
cols = (("CSP Code", 14), ("Operator", 28), ("Email", 24), ("Location", 32), ("RM", 16), ("Why", 60))
for i, (name, width) in enumerate(cols, 1):
    cell = ws.cell(row=1, column=i, value=name)
    cell.fill, cell.font = HEAD, WHITE
    ws.column_dimensions[cell.column_letter].width = width
for r in rows:
    ws.append([r["csp_code"], r["name"], r["email_masked"], r["location"], r["rm"], r["reason"]])
for row in ws.iter_rows(min_row=2):
    row[5].alignment = WRAP
ws.freeze_panes = "A2"
ws.auto_filter.ref = ws.dimensions

t = wb.create_sheet("Message template")
t.column_dimensions["A"].width = 24
t.column_dimensions["B"].width = 100
lines = [
    ("Campaign", c["name"]),
    ("Festival / occasion", c["occasion"]),
    ("Sent to operators who do not have", ", ".join(ops.DEVICE_LABELS.get(d, d) for d in c["devices"])),
    ("Status", c["status"]),
    ("Eligible / review / excluded", f"{counts.get('eligible', 0)} / {counts.get('review', 0)} / {counts.get('excluded', 0)}"),
    ("", ""),
    ("TEMPLATE (as written)", ""),
    ("Subject", c["subject"]),
    ("Message", c["body"]),
    ("Button text", c.get("link_label") or "(no button)"),
    ("Button link", c.get("link_url") or "(no button)"),
    ("Added automatically", "Footer: \"You are receiving this because you are an Eko CSP partner. Unsubscribe.\" "
                            "(every email gets its own unsubscribe link)"),
    ("", ""),
    ("PLACEHOLDERS", "Filled in automatically for each operator when the email is sent:"),
    ("{name}", "operator's full name"),
    ("{first_name}", "operator's first name"),
    ("{csp_code}", "operator's CSP code"),
    ("{rm_name}", "operator's RM"),
    ("{occasion}", "the festival / occasion above"),
    ("{missing_devices}", "only the devices that operator is confirmed not to have"),
]
if example:
    lines += [("", ""), (f"EXAMPLE (as sent to {example['sample']})", ""),
              ("Subject", example["subject"]), ("Message", example["text"])]
for label, value in lines:
    t.append([label, value])
for row in t.iter_rows():
    row[0].font = BOLD
    row[0].alignment = row[1].alignment = WRAP
    if row[0].value and str(row[0].value).isupper():
        row[0].fill, row[0].font = HEAD, WHITE
for i, (label, _) in enumerate(lines, 1):
    if label in ("Message",):
        t.row_dimensions[i].height = 15 * max(4, str(t.cell(row=i, column=2).value).count("\n") + 2)

wb.save(out_path)
print(f"saved {out_path}: {len(rows)} {bucket} operators, campaign '{c['name']}'")
