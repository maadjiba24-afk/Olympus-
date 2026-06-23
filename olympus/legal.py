"""Privacy policy and terms pages for a public Olympus instance.

Plain-language, honest descriptions of what Olympus actually does with data —
written to match the real behaviour (encrypted OAuth token vault, per-user
memory you can export/delete, opt-in anonymized cross-model contribution,
human-approved actions). The operator sets OLYMPUS_OPERATOR_NAME and
OLYMPUS_OPERATOR_CONTACT.

NOTE: these are sensible defaults, not legal advice. Review with counsel before
a commercial launch, and note Google requires a public privacy policy for a
production OAuth app that touches Gmail/Calendar.
"""

from __future__ import annotations

import os
import time


def _operator() -> tuple[str, str]:
    return (os.environ.get("OLYMPUS_OPERATOR_NAME", "the operator of this instance"),
            os.environ.get("OLYMPUS_OPERATOR_CONTACT",
                           "the operator (set OLYMPUS_OPERATOR_CONTACT)"))


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{title} · OLYMPUS</title><style>"
        "body{margin:0 auto;max-width:760px;padding:32px 20px;"
        "font-family:Georgia,serif;background:#0e1116;color:#e8e3d8;line-height:1.6}"
        "h1{color:#d9b44a;letter-spacing:2px}h2{color:#d9b44a;margin-top:28px;font-size:18px}"
        "a{color:#d9b44a}code{background:#11151d;padding:1px 5px;border-radius:4px}"
        "small{color:#6b7280}</style></head><body>"
        f"{body}<p style=\"margin-top:32px\"><a href=\"/\">← back to Olympus</a></p>"
        "</body></html>")


def privacy_html() -> str:
    name, contact = _operator()
    updated = time.strftime("%Y-%m-%d", time.gmtime())
    return _page("Privacy", f"""
<h1>Privacy Policy</h1>
<p><small>Last updated {updated} · operated by {name}</small></p>
<p>Olympus is an AI assistant. This policy explains what it stores and why, in
plain language.</p>

<h2>What is collected</h2>
<ul>
<li><b>Account</b> — if accounts are enabled, your username and a hashed
password (never stored in plain text).</li>
<li><b>Conversations &amp; memory</b> — your messages and the durable facts,
preferences, and notes Olympus derives from them, to give you continuity. This
is scoped privately to your account.</li>
<li><b>Connected accounts (optional)</b> — if you connect Google, Olympus stores
OAuth tokens to read your Gmail/Calendar. These tokens are <b>encrypted at
rest</b> and used only to perform what you ask.</li>
<li><b>Operational data</b> — basic usage counts and error logs to keep the
service running.</li>
</ul>

<h2>How it is used</h2>
<p>Only to provide the assistant: answer you, remember context you've shared,
and perform actions you approve. Olympus <b>prepares</b> sensitive or
irreversible actions (e.g. sending email) and never performs them without your
explicit approval.</p>

<h2>Third parties</h2>
<ul>
<li><b>AI model provider</b> — your messages are sent to the AI provider that
answers them (e.g. Anthropic, or whichever provider's key you bring). Their
handling is governed by their own terms.</li>
<li><b>Google</b> — only if you connect it, and only for the scopes you grant.</li>
</ul>

<h2>Cross-model learning (opt-in, off by default)</h2>
<p>Olympus can distill anonymized lessons across models, but <b>only if you
turn it on</b>. When enabled, emails, phone numbers, long ID numbers, and
key-shaped strings are stripped before any short snapshot is shared internally.
Raw conversations are never pooled.</p>

<h2>Your data rights</h2>
<p>Your memory is yours: you can <b>export</b> it and <b>delete</b> it on
demand (the operator can run <code>olympus memory export</code> /
<code>olympus memory delete</code> for your namespace). Disconnecting Google
removes its stored tokens.</p>

<h2>Security</h2>
<p>OAuth tokens are encrypted with a key the operator controls. Access can be
gated by login and an access token. No system is perfectly secure; share
sensitive information at your own discretion.</p>

<h2>Contact</h2>
<p>Questions or a data request? Contact {contact}, or use the <b>📣 report</b>
button in the app.</p>
""")


def terms_html() -> str:
    name, contact = _operator()
    updated = time.strftime("%Y-%m-%d", time.gmtime())
    return _page("Terms", f"""
<h1>Terms of Use</h1>
<p><small>Last updated {updated} · operated by {name}</small></p>
<p>By using this Olympus instance you agree to the following.</p>

<h2>The service</h2>
<p>Olympus is an AI assistant provided <b>as is</b>, without warranty of any
kind. AI output can be wrong or incomplete — verify anything important before
relying on it. Olympus flags uncertain claims, but you are responsible for how
you use its output.</p>

<h2>Actions on your behalf</h2>
<p>Olympus may <b>prepare</b> actions (emails, calendar invites, webhooks) but
performs sensitive or irreversible ones only after your explicit approval. You
are responsible for the actions you approve.</p>

<h2>Acceptable use</h2>
<p>Don't use Olympus for illegal activity, to harm others, to generate abusive
or deceptive content, or to attempt to break or overload the service. The
operator may rate-limit or remove access to protect the service.</p>

<h2>Your API key (BYOK)</h2>
<p>If you bring your own model API key, you are responsible for its usage and
costs. Keys you enter are used per-request and not stored server-side.</p>

<h2>Liability</h2>
<p>To the maximum extent permitted by law, {name} is not liable for any damages
arising from use of this service. The underlying software is provided under the
MIT license.</p>

<h2>Changes &amp; contact</h2>
<p>These terms may change; continued use means acceptance. Questions: {contact}.</p>
""")
