"""Email spam triage — a read-only classifier over the inbox.

Angelos already reads and summarizes mail; this sorts it. Each message is scored
by dependency-free heuristics over its sender, subject, and snippet into one of
four buckets — **important**, **promotions**, **spam**, **other** — with a short
reason, so the user (or Angelos) can act on the important few and skip the rest.

Heuristics only: no model call is required, so triage works even when the
sandbox egress can't reach a model, and it never *acts* — it classifies. Message
content is untrusted (it comes from anyone who can email you), so the rendered
output is wrapped as untrusted like the other inbox reads; classification is
derived data, not instructions to follow.
"""

from __future__ import annotations

import re

CATEGORIES = ("important", "promotions", "spam", "other")

# Strong spam signals — scams, account-phishing, lottery/crypto pumps.
_SPAM = [
    r"\bviagra\b", r"\bcialis\b", r"\blottery\b", r"\byou('?ve| have) won\b",
    r"\bwinner\b", r"\bclaim your (prize|reward)\b", r"\bnigerian? prince\b",
    r"\bverify your (account|identity)\b", r"\baccount (has been )?suspend",
    r"\bunusual (sign|log)-?in\b", r"\bconfirm your password\b",
    r"\bwire transfer\b", r"\bgift card\b", r"\bcrypto(currency)? (giveaway|doubl)",
    r"\bact now\b", r"\burgent(ly)? (respond|reply|action)\b", r"\brisk-?free\b",
    r"\b100% free\b", r"\bcongratulations\b.*\b(selected|won)\b",
]
# Bulk / marketing signals — legitimate but low-priority.
_PROMO = [
    r"\bunsubscribe\b", r"\bnewsletter\b", r"\b\d{1,3}% off\b", r"\bsale\b",
    r"\bdeal(s)?\b", r"\blimited time\b", r"\bshop now\b", r"\bpromo(tion)?\b",
    r"\bcoupon\b", r"\boffer\b", r"\bblack friday\b", r"\bcyber monday\b",
]
_PROMO_SENDERS = [r"no-?reply", r"newsletter", r"marketing", r"notifications?@",
                  r"updates?@", r"info@", r"deals?@", r"promo@"]
# Priority signals — direct, actionable, human.
_IMPORTANT = [
    r"^re:", r"^fwd?:", r"\binvoice\b", r"\bpayment (due|received)\b",
    r"\bmeeting\b", r"\bcalendar invite\b", r"\bcontract\b", r"\bdeadline\b",
    r"\bcan you\b", r"\bplease review\b", r"\baction required\b",
    r"\bquestion\b", r"\bfollow(ing)? up\b",
]


def _score(text: str, patterns: list[str]) -> int:
    return sum(1 for p in patterns if re.search(p, text, re.IGNORECASE))


def classify(sender: str, subject: str, snippet: str) -> dict:
    """Return {category, reason} for one message."""
    subj = subject or ""
    blob = f"{subject}\n{snippet}"
    sender_l = (sender or "").lower()

    spam = _score(blob, _SPAM)
    promo = _score(blob, _PROMO) + _score(sender_l, _PROMO_SENDERS)
    important = _score(subj, _IMPORTANT[:2]) * 2 + _score(blob, _IMPORTANT[2:])

    # A shouty ALL-CAPS subject with punctuation is a spam tell.
    if subj and len(subj) > 8 and subj.upper() == subj and re.search(r"[!?$]", subj):
        spam += 1

    if spam >= 2 or (spam >= 1 and promo == 0 and important == 0):
        return {"category": "spam", "reason": f"{spam} spam signal(s)"}
    if important >= 1 and important >= promo:
        why = "reply/forward thread" if re.match(r"(?i)^(re|fwd?):", subj) \
            else "direct/actionable language"
        return {"category": "important", "reason": why}
    if promo >= 1:
        return {"category": "promotions", "reason": "bulk/marketing signals"}
    return {"category": "other", "reason": "no strong signal"}


def triage(query: str = "in:inbox", max_results: int = 20) -> dict:
    """Fetch recent messages and bucket them. Returns
    {buckets: {cat: [msg,...]}, counts: {cat: n}, total}. Degrades to an empty
    triage (with an `error`) when Gmail isn't connected or is unreachable."""
    from . import gmail
    try:
        msgs = gmail.messages_meta(query, max_results)
    except Exception as err:
        return {"buckets": {c: [] for c in CATEGORIES}, "counts": {},
                "total": 0, "error": str(err)[:200]}
    buckets: dict[str, list] = {c: [] for c in CATEGORIES}
    for m in msgs:
        verdict = classify(m.get("from", ""), m.get("subject", ""),
                           m.get("snippet", ""))
        row = {**m, "reason": verdict["reason"]}
        buckets[verdict["category"]].append(row)
    counts = {c: len(buckets[c]) for c in CATEGORIES}
    return {"buckets": buckets, "counts": counts, "total": len(msgs)}


def render(query: str = "in:inbox", max_results: int = 20) -> str:
    t = triage(query, max_results)
    if t.get("error"):
        return f"Couldn't triage the inbox: {t['error']}"
    if not t["total"]:
        return "No messages to triage."
    order = ["important", "spam", "promotions", "other"]
    label = {"important": "⭐ Important", "spam": "🚫 Likely spam",
             "promotions": "🏷 Promotions", "other": "• Other"}
    out = [f"Triaged {t['total']} message(s): "
           + ", ".join(f"{t['counts'][c]} {c}" for c in order) + "\n"]
    for c in order:
        rows = t["buckets"][c]
        if not rows:
            continue
        out.append(f"{label[c]} ({len(rows)}):")
        for m in rows:
            out.append(f"  [{m['id']}] {m['from']} — {m['subject']}  "
                       f"({m['reason']})")
    return "\n".join(out)
