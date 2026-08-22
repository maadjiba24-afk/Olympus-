"""Built-in action types registered on the Action spine.

These prove the spine end-to-end and are the first real "things Olympus can do":
  - save_note   : trivial + reversible (demonstrates auto-execute + undo)
  - send_email  : irreversible, scope-gated, always needs explicit approval
  - call_webhook: irreversible, scope-gated

Sending an email or hitting a webhook reuses the existing allowlist-gated
handlers in tools.py, so the operating-assistant layer inherits the same
safety as before. New capabilities (calendar, files, payments-prep) are just
new ActionTypes registered here.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import actions, calendar, config, gmail, memory, sandbox, tools


# --- save_note: trivial, reversible -------------------------------------

def _notes_dir(user: str) -> Path:
    d = config.MEMORY_DIR / "notes" / memory.safe_id(user)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _note_preview(p: dict) -> str:
    return f"Save a note titled '{p.get('title', 'note')}':\n{p.get('body', '')[:500]}"


def _note_execute(p: dict) -> dict:
    user = p.get("_user", "shared")
    fname = f"{int(time.time())}-{memory.safe_id(p.get('title', 'note'))}.md"
    path = _notes_dir(user) / fname
    path.write_text(f"# {p.get('title', 'note')}\n\n{p.get('body', '')}\n",
                    encoding="utf-8")
    return {"path": str(path)}


def _note_undo(result: dict) -> str:
    path = Path(result.get("path", ""))
    if path.exists():
        path.unlink()
    return "note deleted"


# --- send_email: irreversible, scope-gated ------------------------------

def _email_preview(p: dict) -> str:
    return (f"Send email\n  To: {p.get('to', '?')}\n"
            f"  Subject: {p.get('subject', '?')}\n\n{p.get('body', '')}")


def _email_execute(p: dict) -> dict:
    # _approved=True: this runs only after explicit approval on the actions
    # spine, so the egress guard must NOT re-fire here (it would re-hold the
    # send and loop forever). Approval IS the gate clearing.
    msg = tools._send_email(p.get("to", ""), p.get("subject", ""),
                            p.get("body", ""), _approved=True)
    if msg.lower().startswith("error"):
        raise RuntimeError(msg)
    return {"status": msg}


# --- call_webhook: irreversible, scope-gated ----------------------------

def _webhook_preview(p: dict) -> str:
    return f"POST to webhook '{p.get('name', '?')}' with payload:\n{p.get('payload', {})}"


def _webhook_execute(p: dict) -> dict:
    # _approved=True: approved-action path; the egress guard must not re-fire
    # here (see _email_execute).
    msg = tools._call_webhook(p.get("name", ""), p.get("payload", {}),
                              _approved=True)
    if msg.lower().startswith("error"):
        raise RuntimeError(msg)
    return {"status": msg}


# --- Gmail actions (the operating-assistant MVP) ------------------------

def _gmail_send_preview(p: dict) -> str:
    return (f"Send via Gmail\n  To: {p.get('to', '?')}\n"
            f"  Subject: {p.get('subject', '?')}\n\n{p.get('body', '')}")


def _gmail_send_execute(p: dict) -> dict:
    r = gmail.send(p.get("to", ""), p.get("subject", ""), p.get("body", ""))
    return {"id": r.get("id"), "sent": True}


def _gmail_draft_preview(p: dict) -> str:
    return (f"Save a Gmail draft\n  To: {p.get('to', '?')}\n"
            f"  Subject: {p.get('subject', '?')}\n\n{p.get('body', '')[:1000]}")


def _gmail_draft_execute(p: dict) -> dict:
    r = gmail.create_draft(p.get("to", ""), p.get("subject", ""),
                           p.get("body", ""))
    return {"draft_id": r.get("id")}


def _gmail_draft_undo(result: dict) -> str:
    if result.get("draft_id"):
        gmail.delete_draft(result["draft_id"])
    return "draft deleted"


def _gmail_archive_preview(p: dict) -> str:
    return f"Archive message {p.get('message_id', '?')} (remove from inbox)"


def _gmail_archive_execute(p: dict) -> dict:
    gmail.archive(p.get("message_id", ""))
    return {"message_id": p.get("message_id", "")}


def _gmail_archive_undo(result: dict) -> str:
    gmail.unarchive(result.get("message_id", ""))
    return "moved back to inbox"


# --- Calendar actions ----------------------------------------------------

def _cal_create_preview(p: dict) -> str:
    who = ", ".join(p.get("attendees", []) or []) or "(no attendees — personal hold)"
    return (f"Create calendar event\n  '{p.get('summary', '?')}'\n"
            f"  {p.get('start', '?')} → {p.get('end', '?')}\n"
            f"  Invite: {who}\n  {p.get('description', '')}")


def _cal_create_execute(p: dict) -> dict:
    r = calendar.create_event(
        p.get("summary", ""), p.get("start", ""), p.get("end", ""),
        p.get("attendees"), p.get("description", ""))
    return {"event_id": r.get("id")}


def _cal_create_undo(result: dict) -> str:
    if result.get("event_id"):
        calendar.delete_event(result["event_id"])
    return "event cancelled"


# --- sandbox execution: run a command / write a file --------------------
# These are the operator surface. They are IRREVERSIBLE/NOTABLE and scope-gated,
# so an agent can only PREPARE them — a human (or explicit policy) approves
# before anything runs on the host.

def _run_command_preview(p: dict) -> str:
    from . import cmdguard
    cmd = p.get("command", "")
    verdict = cmdguard.scan(cmd)
    risk = ("" if verdict.level == cmdguard.SAFE
            else f"\n  {verdict.render()}")
    return (f"Run command in the workspace ({sandbox.backend()} backend):\n"
            f"  $ {cmd}{risk}")


def _run_command_execute(p: dict) -> dict:
    res = sandbox.run(p.get("command", ""), timeout=p.get("timeout"),
                      watch=p.get("watch"), root=p.get("_pinned_root"))
    out = {"code": res.code, "ok": res.ok, "output": res.output}
    if res.watched:
        out["watched"] = list(res.watched)
    return out


def _run_python_preview(p: dict) -> str:
    code = p.get("code", "")
    # cmdguard is shell-only and cannot inspect Python semantics, so the preview
    # shows the whole snippet for a human to read before it runs, and names the
    # backend — the real isolation for untrusted code is the docker backend.
    note = ("" if sandbox.backend() == "docker" else
            "\n  (local backend: runs with your privileges — approve only code "
            "you trust; set OLYMPUS_EXEC_BACKEND=docker to isolate)")
    return (f"Run Python in the workspace ({sandbox.backend()} backend):"
            f"{note}\n\n{code}")


def _run_python_execute(p: dict) -> dict:
    res = sandbox.run_python(p.get("code", ""), timeout=p.get("timeout"),
                             watch=p.get("watch"), root=p.get("_pinned_root"))
    out = {"code": res.code, "ok": res.ok, "output": res.output}
    if res.watched:
        out["watched"] = list(res.watched)
    return out


def _write_file_preview(p: dict) -> str:
    try:
        sandbox._guard_write_target(
            p.get("path", ""), root=p.get("_pinned_root"))
    except ValueError as err:
        return f"Write blocked: {err}"
    body = p.get("content", "")
    return (f"Write file '{p.get('path', '?')}' "
            f"({len(body.encode('utf-8'))} bytes) in the workspace:\n"
            f"{body[:500]}")


def _write_file_execute(p: dict) -> dict:
    return sandbox.write_file(p.get("path", ""), p.get("content", ""),
                              root=p.get("_pinned_root"))


def _write_document_preview(p: dict) -> str:
    body = p.get("content", "")
    return (f"Save document '{p.get('name', '?')}' "
            f"({len(body.encode('utf-8'))} bytes) to your workspace:\n"
            f"{body[:500]}")


def _write_document_execute(p: dict) -> dict:
    from . import documents, memory
    user = p.get("_user") or memory.current_user()
    return documents.save(user, p.get("name", ""), p.get("content", ""))


def _write_document_undo(result: dict) -> str:
    from . import documents
    return documents.undo_save(result)


def _edit_file_preview(p: dict) -> str:
    """The approval preview IS the diff — the human sees exactly the hunk that
    will land, not a description of it."""
    diff = sandbox.edit_file_diff(
        p.get("path", ""), p.get("old_string", ""), p.get("new_string", ""),
        bool(p.get("replace_all")), root=p.get("_pinned_root"))
    return f"Edit file '{p.get('path', '?')}' in the workspace:\n{diff}"


def _edit_file_execute(p: dict) -> dict:
    return sandbox.edit_file(
        p.get("path", ""), p.get("old_string", ""), p.get("new_string", ""),
        bool(p.get("replace_all")), root=p.get("_pinned_root"))


# --- AP2 payment-mandate authorization (ADR 0004) — NO live rail ----------

def _fmt_money(minor, currency: str) -> str:
    try:
        return f"{int(minor) / 100:.2f} {str(currency).upper()}"
    except (TypeError, ValueError):
        return f"{minor} {currency}"


def _authorize_payment_preview(p: dict) -> str:
    """The plain-language summary the human sees BEFORE signing — the exact
    bounded authorization (construction-injection backstop, threat-model C2.4)."""
    items = ", ".join(str(i) for i in (p.get("items") or [])) or "(none)"
    return (
        f"Authorize payment of {_fmt_money(p.get('amount'), p.get('currency'))} "
        f"to '{p.get('merchant', '?')}' for \"{p.get('item', '?')}\".\n"
        f"  Cap: {_fmt_money(p.get('amount_cap'), p.get('currency'))} · "
        f"allowed merchants: {', '.join(p.get('merchants') or []) or '(none)'} · "
        f"expires in {int(p.get('expires_in', 3600)) // 60} min.\n"
        f"  Items: {items}\n"
        "  This RECORDS a signed authorization mandate — it does NOT move money "
        "(no payment rail).")


def _authorize_payment_execute(p: dict) -> dict:
    """The approval IS the signing event. Build + sign the intent and cart, run
    the payment.mandate ABC contract (raises to FAIL the action on any
    violation), then record the verified mandate. Moves NO money."""
    from . import mandate, mandate_store
    user = p.get("_user", "shared")
    intent = mandate.create_intent(
        user, amount_cap=int(p["amount_cap"]), currency=str(p["currency"]),
        merchants=list(p.get("merchants") or []), item=str(p["item"]),
        expires_in=float(p.get("expires_in", 3600)), trusted=True)
    cart = mandate.create_cart(
        intent, amount=int(p["amount"]), currency=str(p["currency"]),
        merchant=str(p["merchant"]), items=list(p.get("items") or []))
    signed_intent = mandate.sign(intent)
    # The human approval IS the user co-signature (dual-signature, M4): the
    # system signs, then the approving user co-signs the SAME payload.
    signed_cart = mandate.co_sign(mandate.sign(cart))
    # Verify + govern BEFORE recording; a spoofed/over-cap/expired/replayed/
    # un-co-signed mandate raises ContractViolation → the action fails closed.
    seen = mandate_store.consumed_nonces(user)
    mandate.enforce_commit(signed_cart, intent, seen_nonces=seen)
    rec = mandate_store.record(user, signed_intent, signed_cart)
    return {"mandate_id": rec["id"], "recorded": True, "moved_money": False,
            "note": "Authorization recorded; NO payment rail — no money moved."}


# --- authorize_assessment: the signed scope grant (the surveyed agent absorption, ADR 0011) -
# The surveyed agent's system prompt orders the model to "never ask permission." Olympus does
# the inverse: an assessment can only run against a target the operator
# authorized HERE, on the approval spine — a human-signed, ledger-recorded fact.
# The grant is what makes assess.require_scope() pass; without it every
# assessment tool fails closed. IRREVERSIBLE so it never auto-runs; undo revokes.

def _authorize_assessment_preview(p: dict) -> str:
    targets = ", ".join(p.get("targets") or []) or "(none)"
    hours = int(p.get("expires_in", 24 * 3600)) // 3600
    return (
        "Authorize a SECURITY ASSESSMENT of your own asset(s):\n"
        f"  Targets: {targets}\n"
        f"  Valid for: {hours}h\n"
        f"  Note: {p.get('note', '') or '(none)'}\n"
        "  This records a signed, revocable authorization that lets Aegis run "
        "recon / security-header audit / SAST / secret + dependency scans "
        "against ONLY these targets. Scope is then enforced in code — nothing "
        "outside this list can be assessed. Only authorize assets you own or "
        "have explicit written permission to test.")


def _authorize_assessment_execute(p: dict) -> dict:
    from . import assess
    rec = assess.grant(
        p.get("targets") or [],
        expires_in=float(p.get("expires_in", 24 * 3600)),
        note=str(p.get("note", "")),
        approved_by=str(p.get("_user", "operator")),
        user=p.get("_user"))
    return {"authorization_id": rec["id"], "targets": rec["targets"],
            "expires": rec["expires"], "_user": p.get("_user")}


def _authorize_assessment_undo(result: dict) -> str:
    from . import assess
    ok = assess.revoke(result.get("authorization_id", ""),
                       user=result.get("_user"))
    return "authorization revoked" if ok else "authorization already gone"


def register_builtins() -> None:
    actions.register(actions.ActionType(
        name="save_note", risk_class=actions.TRIVIAL, scope="notes",
        preview=_note_preview, execute=_note_execute, undo=_note_undo,
        description="Save a note to the user's notes (reversible)."))
    # AP2 payment-mandate authorization — FINANCIAL_LEGAL so it can NEVER
    # auto-run; the human approval signs the mandate. Records only; no rail.
    actions.register(actions.ActionType(
        name="authorize_payment", risk_class=actions.FINANCIAL_LEGAL,
        scope="payment.authorize", preview=_authorize_payment_preview,
        execute=_authorize_payment_execute,
        description="Authorize a bounded payment by signing an AP2 mandate "
                    "(records a verified authorization — moves NO money)."))
    actions.register(actions.ActionType(
        name="send_email", risk_class=actions.IRREVERSIBLE, scope="email",
        preview=_email_preview, execute=_email_execute,
        description="Send an email via the configured SMTP account."))
    actions.register(actions.ActionType(
        name="call_webhook", risk_class=actions.IRREVERSIBLE, scope="webhook",
        preview=_webhook_preview, execute=_webhook_execute,
        description="POST a payload to an operator-configured webhook."))
    # Egress-gateway HOLD targets: when egress.guard() classifies an outbound
    # email/webhook SENSITIVE (C2), it routes the send here for explicit
    # approval. They reuse the existing executors/previews; IRREVERSIBLE so they
    # never auto-run. (Boundary layer, Phase A — docs/DESIGN_BOUNDARY_LAYER.md.)
    actions.register(actions.ActionType(
        name="email_egress_held", risk_class=actions.IRREVERSIBLE, scope="email",
        preview=_email_preview, execute=_email_execute,
        description="An email whose content was classified SENSITIVE and held "
                    "for explicit approval before sending."))
    actions.register(actions.ActionType(
        name="webhook_egress_held", risk_class=actions.IRREVERSIBLE,
        scope="webhook", preview=_webhook_preview, execute=_webhook_execute,
        description="A webhook POST whose payload was classified SENSITIVE and "
                    "held for explicit approval before sending."))
    # Gmail
    actions.register(actions.ActionType(
        name="gmail_send", risk_class=actions.IRREVERSIBLE, scope="gmail.send",
        preview=_gmail_send_preview, execute=_gmail_send_execute,
        description="Send an email via the connected Gmail account."))
    actions.register(actions.ActionType(
        name="gmail_draft", risk_class=actions.NOTABLE, scope="gmail.compose",
        preview=_gmail_draft_preview, execute=_gmail_draft_execute,
        undo=_gmail_draft_undo,
        description="Save a Gmail draft (reversible: undo deletes it)."))
    actions.register(actions.ActionType(
        name="gmail_archive", risk_class=actions.NOTABLE, scope="gmail.modify",
        preview=_gmail_archive_preview, execute=_gmail_archive_execute,
        undo=_gmail_archive_undo,
        description="Archive a message (reversible: undo restores it)."))
    # Calendar — creating an event with attendees emails them an invitation,
    # so it's irreversible (always needs approval); undo cancels the event.
    actions.register(actions.ActionType(
        name="calendar_create", risk_class=actions.IRREVERSIBLE,
        scope="calendar.events", preview=_cal_create_preview,
        execute=_cal_create_execute, undo=_cal_create_undo,
        description="Create a calendar event / send an invitation."))
    # Sandbox execution — the operator surface.
    actions.register(actions.ActionType(
        name="run_command", risk_class=actions.IRREVERSIBLE, scope="exec",
        preview=_run_command_preview, execute=_run_command_execute,
        pins_root=True,
        description="Run a shell command in the confined workspace."))
    # Python execution — the same exec scope + irreversible/never-auto posture as
    # run_command, routed through sandbox.run_python (gate + confinement + caps).
    actions.register(actions.ActionType(
        name="run_python", risk_class=actions.IRREVERSIBLE, scope="exec",
        preview=_run_python_preview, execute=_run_python_execute,
        pins_root=True,
        description="Run a Python snippet in the confined workspace."))
    actions.register(actions.ActionType(
        name="write_file", risk_class=actions.NOTABLE, scope="exec",
        preview=_write_file_preview, execute=_write_file_execute,
        undo=sandbox.undo_write, pins_root=True,
        description="Create/overwrite a file in the workspace (reversible)."))
    actions.register(actions.ActionType(
        name="edit_file", risk_class=actions.NOTABLE, scope="exec",
        preview=_edit_file_preview, execute=_edit_file_execute,
        undo=sandbox.undo_write, pins_root=True,
        description="Exact-string edit of a workspace file, previewed as a "
                    "unified diff (reversible)."))
    # User documents — the workspace. No scope gate (it's the user's own
    # content, confined to their document dir), but always human-approved
    # (staged via prepare_action, never auto-executed) and reversible.
    actions.register(actions.ActionType(
        name="write_document", risk_class=actions.NOTABLE, scope="",
        preview=_write_document_preview, execute=_write_document_execute,
        undo=_write_document_undo,
        description="Create or overwrite a document in the user's workspace "
                    "(reversible)."))
    # Authorized-assessment scope grant (the surveyed agent absorption, ADR 0011).
    # IRREVERSIBLE so it always needs explicit human approval and never
    # auto-runs; undo revokes the grant. This signed, ledger-recorded action is
    # the ONLY way an assessment becomes in-scope — the code-enforced inversion
    # of the surveyed agent's prompt-level "you are already authorized".
    actions.register(actions.ActionType(
        name="authorize_assessment", risk_class=actions.IRREVERSIBLE,
        scope="assess.authorize", preview=_authorize_assessment_preview,
        execute=_authorize_assessment_execute, undo=_authorize_assessment_undo,
        description="Authorize a bounded, revocable security assessment of your "
                    "own asset(s) — the signed scope grant Aegis's assessment "
                    "tools enforce in code."))
    # Operator (HERMES) credentialed browser actions — see olympus/operator.py.
    from . import operator
    operator.register_operator_actions()
    # OS-level computer use — registers its 6 IRREVERSIBLE ActionTypes on the
    # spine so `prepare_action("computer_click", …)` can reach them. Importing
    # the module runs its `register_actions()`. Execution stays gated: disabled
    # by default, requires an installed actuator, and each action still needs an
    # explicit approval (see olympus/computeruse.py).
    from . import computeruse  # noqa: F401


register_builtins()
