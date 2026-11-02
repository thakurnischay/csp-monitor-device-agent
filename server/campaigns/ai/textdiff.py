"""Edit distance between what the AI wrote and what the person finally used (the blueprint's "correction delta").

`levenshtein` is the exact character-level Levenshtein distance (insertions, deletions, substitutions each cost 1), computed with
the bit-parallel algorithm (Myers 1999 / Hyyro 2003) on Python's arbitrary-size integers, so an 8,000-character message takes
milliseconds instead of the minute a plain dynamic-programming table would need.
"""
import difflib
from html import escape


def levenshtein(a, b):
    a, b = str(a or ""), str(b or "")
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    m = len(a)
    mask = (1 << m) - 1
    last = 1 << (m - 1)
    peq = {}
    for i, ch in enumerate(a):
        peq[ch] = peq.get(ch, 0) | (1 << i)
    pv, mv, score = mask, 0, m
    for ch in b:
        eq = peq.get(ch, 0)
        xv = eq | mv
        xh = ((((eq & pv) + pv) ^ pv) | eq) & mask
        ph = (mv | ~(xh | pv)) & mask
        mh = pv & xh
        if ph & last:
            score += 1
        elif mh & last:
            score -= 1
        ph = ((ph << 1) | 1) & mask
        mh = (mh << 1) & mask
        pv = (mh | ~(xv | ph)) & mask
        mv = ph & xv
    return score


def edit_ratio(generated_subject, generated_body, final_subject, final_body):
    """(subject_distance, body_distance, ratio). ratio = total distance / the longer total length: 0 = untouched, 1 = rewritten."""
    ds = levenshtein(generated_subject, final_subject)
    db = levenshtein(generated_body, final_body)
    longest = max(len(generated_subject or "") + len(generated_body or ""), len(final_subject or "") + len(final_body or ""), 1)
    return ds, db, round(min(1.0, (ds + db) / longest), 4)


REWRITTEN_AT = 0.6          # more than this share changed = "completely rewritten"


def outcome_for(distance_total, ratio):
    if distance_total == 0:
        return "unchanged"
    return "rewritten" if ratio > REWRITTEN_AT else "edited"


def inline_diff_html(old, new):
    """Word-level diff as safe HTML (everything escaped): removed text in <del>, added text in <ins>."""
    a, b = str(old or "").split(), str(new or "").split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(escape(" ".join(a[i1:i2])))
        else:
            if i2 > i1:
                out.append("<del>" + escape(" ".join(a[i1:i2])) + "</del>")
            if j2 > j1:
                out.append("<ins>" + escape(" ".join(b[j1:j2])) + "</ins>")
    return " ".join(x for x in out if x)
