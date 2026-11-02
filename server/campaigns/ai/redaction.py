"""Personal data NEVER goes to the model.

Two layers: (1) the prompt is built only from non-personal facts (occasion, device types, counts, house style) so there is
nothing personal to send; (2) anything a person TYPES (the optional brief) or that comes from stored text (earlier campaign
messages used as examples) is scrubbed first. This module does the scrubbing and can also check the model's reply.

What it removes: email addresses, phone numbers, long digit strings (account numbers), links (they can carry tokens), and - using
the operator data already in the dashboard - exact CSP codes, mobile numbers, email addresses and full names of real operators.
Reports contain counts and kinds only, never the removed values.
"""
import re

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
PHONE = re.compile(r"(?<![\dA-Za-z])(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")
LONG_NUMBER = re.compile(r"(?<!\d)\d[\d\s-]{7,}\d(?!\d)")
URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_NOT_NAME_CHAR = re.compile(r"[^a-z\s'-]")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{4,}")
MIN_NAME_CHARS = 6                      # shorter names ("Ram") would match ordinary words


class Known:
    """Real operator identifiers, from the dashboard's own operator data."""
    def __init__(self, codes=(), names=(), emails=(), mobiles=()):
        self.codes = {str(c).upper() for c in codes if len(str(c)) >= 4}
        norm = [" ".join(_NOT_NAME_CHAR.sub(" ", str(n).lower()).split()) for n in names]     # "Md. Akhtar" -> "md akhtar"
        self.names = {n for n in norm if len(n) >= MIN_NAME_CHARS}
        self.emails = {str(e).strip().lower() for e in emails if e}
        self.mobiles = {re.sub(r"\D", "", str(m))[-10:] for m in mobiles if len(re.sub(r"\D", "", str(m))) >= 10}
        self.max_words = max([len(n.split()) for n in self.names] + [1])


def load_known(conn):
    rows = conn.execute("SELECT csp_code, name, email, mobile FROM campaign_operators").fetchall()
    return Known([r["csp_code"] for r in rows], [r["name"] for r in rows], [r["email"] for r in rows], [r["mobile"] for r in rows])


def _name_spans(text, known):
    words = [(m.start(), m.end(), m.group(0).lower()) for m in _WORD.finditer(text)]
    spans, i = [], 0
    while i < len(words):
        hit = 0
        for n in range(min(known.max_words, len(words) - i), 0, -1):
            if " ".join(w[2] for w in words[i:i + n]) in known.names:
                hit = n
                break
        if hit:
            spans.append((words[i][0], words[i + hit - 1][1]))
            i += hit
        else:
            i += 1
    return spans


def redact(text, known=None):
    """-> (clean_text, report). The report is {"emails": n, "phones": n, ...} with only the kinds that occurred."""
    text = str(text or "")
    known = known or Known()
    report = {}

    def bump(kind, n=1):
        report[kind] = report.get(kind, 0) + n

    text = EMAIL.sub(lambda m: (bump("emails"), "[email]")[1], text)
    text = URL.sub(lambda m: (bump("links"), "[link]")[1], text)
    text = PHONE.sub(lambda m: (bump("phones"), "[phone]")[1], text)
    text = LONG_NUMBER.sub(lambda m: (bump("long_numbers"), "[number]")[1], text)
    if known.codes:
        text = _TOKEN.sub(lambda m: (bump("csp_codes"), "[code]")[1] if m.group(0).upper() in known.codes else m.group(0), text)
    if known.names:
        spans = _name_spans(text, known)
        for s, e in reversed(spans):
            text = text[:s] + "[name]" + text[e:]
        if spans:
            bump("names", len(spans))
    return text, report


def leaks(text, known=None):
    """What is still personal in `text` (used to check the model's reply and the final prompt). -> list of kinds."""
    text = str(text or "")
    known = known or Known()
    found = []
    if EMAIL.search(text):
        found.append("email")
    if PHONE.search(text):
        found.append("phone")
    if LONG_NUMBER.search(text):
        found.append("long_number")
    if known.codes and any(t.upper() in known.codes for t in _TOKEN.findall(text)):
        found.append("csp_code")
    if known.names and _name_spans(text, known):
        found.append("name")
    return found
