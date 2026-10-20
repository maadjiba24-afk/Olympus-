"""What the interactive app does *around* a model turn — kept out of the model
loop and behind an injectable IO so it is fully testable without a terminal.

Two jobs, both consumer-facing:
  1. Secure credential capture: if the turn asked to remember a site sign-in, a
     private prompt (the model never sees it) collects username/password and
     stores them in the vault.
  2. Plain-English approval: any action the spine is holding for approval is
     shown and confirmed with a simple yes/no, instead of `olympus approve <id>`.

See docs/DESIGN_OPERATOR_UX.md.
"""

from __future__ import annotations

from . import approvals, securecapture

_YES = {"y", "yes", "yeah", "yep", "sure", "ok", "okay", "do it", "confirm",
        "go", "go ahead", "approve"}


def is_yes(answer: str) -> bool:
    return (answer or "").strip().lower() in _YES


class IO:
    """Default terminal IO. Tests pass a fake with the same three methods."""

    def line(self, prompt: str) -> str:
        return input(prompt)

    def secret(self, prompt: str) -> str:
        import getpass
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            return getpass.getpass(prompt)

    def out(self, text: str) -> None:
        print(text)


def _login_message(facts):
    """Render only bounded known facts from the private local workflow."""
    identity = facts['workflow_id']
    if facts['state'] == 'no_request':
        return ''
    if facts['state'] == 'complete' and facts['complete'] and not facts['cleanup_unconfirmed']:
        return 'Credentials stored and site authorization confirmed.'
    if facts['state'] == 'canceled' and facts['complete'] and not facts['cleanup_unconfirmed']:
        return 'Private sign-in canceled; no credentials were stored.'
    if facts['state'] == 'ready' and not facts['cleanup_unconfirmed']:
        return ('Private sign-in is ready. Use /login resume ' + identity + ' to open its private prompt.')
    if facts['vault'] == 'confirmed':
        message = ('Credential storage confirmed. Site authorization '
                   + ('confirmed.' if facts['authorization'] == 'confirmed' else 'unconfirmed.'))
    else:
        message = ('Credential storage was not attempted.' if facts['vault'] == 'not_attempted'
                   else 'Credential storage is unconfirmed.')
    if facts['cleanup_unconfirmed']:
        message += ' Cleanup needs reconciliation; further work is blocked.'
    return message + ' Workflow ' + identity + '. Use /login status ' + identity + ' to inspect it.'


def _cancel_input_failure(user, facts, primary):
    from . import native_cleanup
    cleanup = False
    try:
        result = securecapture.cancel(user, facts['workflow_id'])
        cleanup = result['cleanup_unconfirmed']
    except BaseException as error:
        cleanup = securecapture._cleanup_unknown(error)
        try:
            result = securecapture.status(user, facts['workflow_id'])
        except BaseException:
            result = {**facts, 'state': 'unconfirmed', 'complete': False,
                      'cancellation': 'unconfirmed', 'cleanup_unconfirmed': cleanup}
    if cleanup:
        native_cleanup.mark(primary)
    attributes = BaseException.__getattribute__(primary, '__dict__')
    if type(attributes) is dict:
        attributes['_secure_capture_workflow'] = {key: value for key, value in result.items() if key != 'domain'}
    return result


def _prompt_login(user, facts, io):
    admitted = securecapture.start_prompt(user, facts['workflow_id'])
    if admitted['state'] != 'prompting' or admitted['cleanup_unconfirmed'] or not admitted['prompt_dispatched']:
        return admitted
    username = password = None
    try:
        try:
            io.out('\nPrivate sign-in for ' + admitted['domain'] + '. Workflow ' + admitted['workflow_id'] + '. Input is kept outside the conversation.')
            username = io.line('   username: ').strip()
            if username:
                password = io.secret('   password: ')
        except BaseException as primary:
            result = _cancel_input_failure(user, admitted, primary)
            if not isinstance(primary, Exception):
                raise
            return result
        if not username or not password:
            return securecapture.cancel(user, admitted['workflow_id'])
        return securecapture.fulfill(user, admitted['domain'], username, password,
                                     workflow_id=admitted['workflow_id'])
    finally:
        username = password = None


def _report_login(io, facts):
    """Preserve known workflow facts if terminal reporting itself fails."""
    message = _login_message(facts)
    if not message:
        return
    failed = False
    try:
        io.out(message)
    except BaseException as error:
        if not isinstance(error, Exception):
            attributes = BaseException.__getattribute__(error, '__dict__')
            if type(attributes) is dict:
                attributes['_secure_capture_workflow'] = {key: value for key, value in facts.items() if key != 'domain'}
                attributes['_secure_capture_reporting_unconfirmed'] = True
            raise
        failed = True
    if failed:
        error = securecapture.SecureCaptureError('private sign-in reporting is unavailable; use /login status')
        error._secure_capture_workflow = {key: value for key, value in facts.items() if key != 'domain'}
        error._secure_capture_reporting_unconfirmed = True
        raise error from None


def _secure_login(user, io):
    facts = securecapture.begin(user)
    if facts['state'] == 'ready' and not facts['cleanup_unconfirmed']:
        facts = _prompt_login(user, facts, io)
    _report_login(io, facts)
    if facts['state'] == 'complete' and facts['complete'] and not facts['cleanup_unconfirmed']:
        return ['saved-login:' + facts['domain']]
    if facts['state'] in ('no_request', 'canceled') and facts['complete'] and not facts['cleanup_unconfirmed']:
        return []
    return ['login-unconfirmed:' + facts['workflow_id']]


def login_command(user, argument, io=None):
    """Local same-process command; no model/provider invocation or private arguments."""
    if type(argument) is not str or len(argument) > 128:
        return 'Usage: /login status [workflow-id] or /login settle|cancel|resume <workflow-id>'
    parts = argument.split()
    if parts == ['status']:
        try:
            facts = securecapture.active_status(user)
            return 'No private sign-in workflow is retained in this process.' if facts is None else _login_message(facts)
        except securecapture.SecureCaptureError:
            return 'Private sign-in workflow is unavailable in this executor.'
    if len(parts) != 2 or parts[0] not in ('status', 'settle', 'cancel', 'resume'):
        return 'Usage: /login status [workflow-id] or /login settle|cancel|resume <workflow-id>'
    operation, identity = parts
    try:
        if operation == 'resume':
            facts = securecapture.status(user, identity)
            if facts['state'] != 'ready' or facts['cleanup_unconfirmed']:
                return _login_message(facts)
            facts = _prompt_login(user, facts, io or IO())
        else:
            function = {'status': securecapture.status, 'settle': securecapture.settle,
                        'cancel': securecapture.cancel}[operation]
            facts = function(user, identity)
        return _login_message(facts)
    except securecapture.SecureCaptureError:
        return 'Private sign-in workflow is unavailable or needs reconciliation in its original process.'



def after_turn(user: str, io: IO | None = None) -> list[str]:
    """Run the post-turn interactions for `user`. Returns a list of short event
    tags (for logs/tests). Actual interrupts retain their identity and known workflow facts."""
    io = io or IO()
    events: list[str] = []

    events.extend(_secure_login(user, io))

    # 2) Plain-English approval of held actions.
    for action in approvals.pending(user):
        io.out("")
        io.out(action.preview or action.title)
        if is_yes(io.line("Approve this? [y/N] ")):
            done = approvals.approve(user, action.id)
            ok = getattr(done, "status", "") == "executed"
            io.out("   ✓ Done." if ok else f"   {done.status}: {done.error or ''}")
            events.append(f"approved:{action.id}")
        else:
            approvals.reject(user, action.id, "declined at prompt")
            io.out("   Skipped.")
            events.append(f"declined:{action.id}")

    return events
