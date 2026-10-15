"""Google OAuth connect flow — per-user, self-serve account linking.

Replaces "paste a token into an env var" with a real flow: the user clicks
Connect, authorizes Google, and Olympus stores the resulting tokens in the
encrypted vault, refreshing them automatically. This is what makes Gmail +
Calendar usable by someone who isn't running shell commands.

Setup (operator, one time): create an OAuth client in Google Cloud Console and
set OLYMPUS_GOOGLE_CLIENT_ID / OLYMPUS_GOOGLE_CLIENT_SECRET, plus a redirect URI
that points at this app's /oauth/google/callback.

Tokens are per user, encrypted at rest (vault.py). Reading them back yields a
live access token, refreshed on demand.
"""

from __future__ import annotations

import json
import math
import threading
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from . import native_cleanup, store, vault

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Gmail (read/send/modify) + Calendar (events). Least privilege for the MVP.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "openid", "email",
]


class OAuthError(RuntimeError):
    pass


def _client() -> tuple[str, str, str]:
    cid = os.environ.get("OLYMPUS_GOOGLE_CLIENT_ID")
    secret = os.environ.get("OLYMPUS_GOOGLE_CLIENT_SECRET")
    redirect = os.environ.get(
        "OLYMPUS_GOOGLE_REDIRECT",
        "http://localhost:8484/oauth/google/callback")
    if not cid or not secret:
        raise OAuthError(
            "Google OAuth not configured — set OLYMPUS_GOOGLE_CLIENT_ID and "
            "OLYMPUS_GOOGLE_CLIENT_SECRET (create an OAuth client in Google "
            "Cloud Console).")
    return cid, secret, redirect


def configured() -> bool:
    try:
        _client()
        return True
    except OAuthError:
        return False


def authorize_url(state: str) -> str:
    """The Google consent URL to send the user to. `state` is an opaque,
    single-use nonce minted by issue_state() and validated on the callback —
    it is NOT trusted as identity on return."""
    cid, _, redirect = _client()
    params = {
        "client_id": cid,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",      # request a refresh token
        "prompt": "consent",
        "state": state,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


# --- CSRF state (single-use, server-side, short-lived) -------------------

_STATE_NS = "oauth_state"
_STATE_TTL = 600          # seconds; a consent round-trip is quick


def issue_state(user: str, sid: str) -> str:
    """Mint an unguessable, single-use `state` and bind it server-side to the
    user/browser that started the flow. The returned `state` carries no
    identity itself — the callback resolves identity from this record, so a
    forged or replayed `state` cannot bind a connection to the wrong user."""
    token = secrets.token_urlsafe(32)
    store.backend().put(_STATE_NS, token, json.dumps(
        {"user": user, "sid": sid, "created_at": time.time()}).encode())
    return token


def consume_state(state: str) -> dict | None:
    """Validate a returned `state`; return its {user, sid} record or None if
    it's unknown, already used, or expired. Single-use: a valid state is
    deleted on consumption."""
    if not state:
        return None
    blob = store.backend().get(_STATE_NS, state)
    if not blob:
        return None
    store.backend().delete(_STATE_NS, state)      # single-use, even if expired
    try:
        rec = json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        return None
    if time.time() - rec.get("created_at", 0) > _STATE_TTL:
        return None
    return rec


MAX_TOKEN_RESPONSE = 128 * 1024
MAX_TOKEN_BYTES = 16 * 1024


def _facts(*, response=False, available=False, confirmed=False,
           response_cleanup=False, storage_cleanup=False):
    """Only bounded nonsecret facts may be surfaced or logged."""
    return {'provider_may_have_completed': True,
            'provider_response_validated': bool(response),
            'local_settlement_available': bool(available),
            'local_publication_confirmed': bool(confirmed),
            'response_cleanup_unconfirmed': bool(response_cleanup),
            'storage_cleanup_unconfirmed': bool(storage_cleanup)}


def _attach(error, facts, *, handle=None, response=None):
    # Preserve actual interrupt identity; never replace it with an ordinary error.
    attributes = BaseException.__getattribute__(error, '__dict__')
    attributes['_oauth_outcome'] = dict(facts)
    if handle is not None:
        attributes['_oauth_settlement'] = handle
    if response is not None:
        attributes['_oauth_completed_response'] = response


class OAuthOutcomeUnknown(OAuthError):
    """Provider work must not be automatically repeated after dispatch."""
    provider_may_have_completed = True

    def __init__(self, response=None, *, response_cleanup_unconfirmed=False):
        super().__init__('Google token request outcome requires account reconciliation; do not repeat it automatically')
        self._completed_response = response
        self._response_cleanup_unconfirmed = response_cleanup_unconfirmed

    def status(self):
        return _facts(response=self._completed_response is not None,
                      response_cleanup=self._response_cleanup_unconfirmed)


def _token_response(value):
    if type(value) is not dict:
        raise ValueError('invalid token response')
    result = {}
    for key in ('access_token', 'refresh_token'):
        token = value.get(key, '')
        if (type(token) is not str or len(token) > MAX_TOKEN_BYTES
                or len(token.encode('utf-8')) > MAX_TOKEN_BYTES
                or key == 'access_token' and not token):
            raise ValueError('invalid token response')
        if key == 'access_token' or token:
            result[key] = token
    expires = value.get('expires_in', 3600)
    if type(expires) is not int or not 1 <= expires <= 7 * 86400:
        raise ValueError('invalid token expiry')
    kind = value.get('token_type', 'Bearer')
    if type(kind) is not str or kind.lower() != 'bearer':
        raise ValueError('invalid token type')
    result['expires_in'] = expires
    return result


def _post_token(data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(TOKEN_URL, data=body)
    response = completed = failure = None
    owners = []
    response_cleanup = False

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate token field')
            result[key] = value
        return result

    try:
        response = urllib.request.urlopen(request, timeout=30)
        owners.append(response)
        raw = response.read(MAX_TOKEN_RESPONSE + 1)
        if len(raw) > MAX_TOKEN_RESPONSE:
            raise ValueError('oversized token response')
        completed = _token_response(json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid token response'))))
    except BaseException as error:
        failure = error
        if isinstance(error, urllib.error.HTTPError) and all(error is not owner for owner in owners):
            # Acquisition errors and later body errors can own distinct bodies.
            # At most the returned response and one raised error are observed.
            owners.append(error)
    response = None
    while owners:
        owned = owners.pop()
        try:
            owned.close()
        except BaseException as error:
            response_cleanup = True
            if failure is None:
                failure = error
    if failure is not None:
        if isinstance(failure, Exception):
            # Raised outside the handler: no private source exception context.
            raise OAuthOutcomeUnknown(completed,
                response_cleanup_unconfirmed=response_cleanup) from None
        _attach(failure, _facts(response=completed is not None,
                response_cleanup=response_cleanup), response=completed)
        raise failure
    return completed


class OAuthSettlementError(OAuthOutcomeUnknown):
    """Original-executor local settlement handle; never stringify its fields.

    One bounded response/bundle and one PendingUpdate are retained. No global
    registry, provider replay or recovery after process death is implied.
    settle() confirms local publication only. status() separately retains an
    unresolved response-close fact, even after successful local publication.
    """
    def __init__(self, selected, bundle, result_kind, pending=None, *,
                 response_cleanup_unconfirmed=False):
        super().__init__(response_cleanup_unconfirmed=response_cleanup_unconfirmed)
        self._selected, self._bundle = selected, bundle
        self._pending, self._result_kind = pending, result_kind
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self._storage_cleanup_unconfirmed = False
        self._confirmed, self._result = False, None
        self._busy = False

    def _check_executor(self):
        if self._pid != os.getpid() or self._thread != threading.get_ident():
            raise OAuthError('OAuth settlement belongs to its original executor')

    def status(self):
        self._check_executor()
        return _facts(response=True,
            available=not self._busy and not self._confirmed and not self._storage_cleanup_unconfirmed,
            confirmed=self._confirmed,
            response_cleanup=self._response_cleanup_unconfirmed,
            storage_cleanup=self._storage_cleanup_unconfirmed)

    def _remember(self, error):
        if native_cleanup.unknown(error):
            self._storage_cleanup_unconfirmed = True
            native_cleanup.mark(self)
        _attach(error, self.status(), handle=self)

    def _settle_bundle(self):
        self._check_executor()
        if self._busy:
            raise OAuthError('OAuth local settlement is already in progress')
        if self._storage_cleanup_unconfirmed:
            error = OAuthError('OAuth storage cleanup is unconfirmed; local retry requires reconciliation')
            native_cleanup.mark(error)
            _attach(error, self.status(), handle=self)
            raise error
        self._busy = True
        failure = None
        try:
            if self._pending is None:
                self._pending = vault.prepare_snapshot_update(self._selected, self._bundle)
            bundle = vault.settle_snapshot_update(self._pending)
            self._result = (bundle['access_token'] if self._result_kind == 'token'
                            else 'Google account connected.')
            self._confirmed = True
            self._selected = self._bundle = self._pending = None
        except BaseException as error:
            failure = error
        finally:
            self._busy = False
        if failure is not None:
            self._remember(failure)
            raise failure
        return bundle

    def settle(self):
        self._check_executor()
        if self._busy:
            raise OAuthError('OAuth local settlement is already in progress')
        failed = False
        try:
            if not self._confirmed:
                self._settle_bundle()
            return self._result
        except (vault.VaultError, OAuthError):
            raise
        except Exception:
            failed = True
        if failed:
            raise self from None


def _settle(selected, bundle, result_kind):
    handle = OAuthSettlementError(selected, bundle, result_kind)
    failed = False
    try:
        return handle._settle_bundle()
    except Exception:
        failed = True
    if failed:
        raise handle from None


def _request_and_settle(selected, request, result_kind):
    # Start time is captured before dispatch. Delayed settlement must not extend
    # a provider token's lifetime by recomputing its expiration on each retry.
    started = time.time()
    response = primary = None
    response_cleanup = False
    try:
        response = _post_token(request)
    except BaseException as error:
        primary = error
        if isinstance(error, OAuthOutcomeUnknown):
            response = error._completed_response
            response_cleanup = error._response_cleanup_unconfirmed
        else:
            attributes = BaseException.__getattribute__(error, '__dict__')
            response = attributes.get('_oauth_completed_response')
            facts = attributes.get('_oauth_outcome')
            response_cleanup = (type(facts) is dict
                                and facts.get('response_cleanup_unconfirmed') is True)
    if response is None:
        if primary is not None and not isinstance(primary, Exception):
            _attach(primary, _facts(response_cleanup=response_cleanup))
            raise primary
        raise OAuthOutcomeUnknown(response_cleanup_unconfirmed=response_cleanup) from None
    malformed = False
    try:
        tok = _token_response(response)
        previous = selected.value if type(selected.value) is dict else {}
        bundle = {
            'access_token': tok['access_token'],
            'refresh_token': tok.get('refresh_token', previous.get('refresh_token', '')
                                     if result_kind == 'token' else ''),
            'expires_at': started + tok['expires_in'],
        }
    except Exception:
        malformed = True
    if malformed:
        if primary is not None and not isinstance(primary, Exception):
            _attach(primary, _facts(response_cleanup=response_cleanup))
            raise primary
        raise OAuthOutcomeUnknown(response_cleanup_unconfirmed=response_cleanup) from None
    if primary is not None:
        handle = OAuthSettlementError(selected, bundle, result_kind,
            response_cleanup_unconfirmed=response_cleanup)
        if not isinstance(primary, Exception):
            _attach(primary, handle.status(), handle=handle)
            raise primary
        raise handle from None
    return _settle(selected, bundle, result_kind)


def exchange_code(user: str, code: str) -> str:
    """Capture writable vault authority before consuming the authorization code."""
    selected = vault.snapshot(user, 'google', for_update=True)
    cid, secret, redirect = _client()
    _request_and_settle(selected, {
        'code': code, 'client_id': cid, 'client_secret': secret,
        'redirect_uri': redirect, 'grant_type': 'authorization_code',
    }, 'connect')
    return 'Google account connected.'


def connected(user: str) -> bool:
    """False means inspected absence; malformed present state is not absence."""
    value = vault.get(user, 'google')
    if value is None:
        return False
    _stored_bundle(value)
    return True


def disconnect(user: str) -> str:
    vault.delete(user, 'google')
    return 'Google account disconnected.'


def _stored_bundle(value):
    if type(value) is not dict:
        raise OAuthError('stored Google credential bundle is invalid')
    invalid = False
    for name in ('access_token', 'refresh_token'):
        token = value.get(name, '')
        if type(token) is not str or len(token) > MAX_TOKEN_BYTES or name == 'access_token' and not token:
            invalid = True
            break
        try:
            if len(token.encode('utf-8')) > MAX_TOKEN_BYTES:
                invalid = True
        except UnicodeError:
            invalid = True
        if invalid:
            break
    expiry = value.get('expires_at', 0)
    if type(expiry) not in (int, float):
        invalid = True
    else:
        try:
            if not math.isfinite(expiry):
                invalid = True
        except (OverflowError, ValueError):
            invalid = True
    if invalid:
        # Outside any exception handler: invalid token bytes never become a
        # public UnicodeEncodeError payload or chained private exception.
        raise OAuthError('stored Google credential bundle is invalid') from None
    return value


def access_token_if_connected(user: str) -> str | None:
    """One captured lookup; None only for true absence, never failed custody."""
    selected = vault.snapshot(user, 'google')
    if selected.value is None:
        return None
    bundle = _stored_bundle(selected.value)
    if time.time() < bundle.get('expires_at', 0) - 60:
        return bundle['access_token']
    vault.require_snapshot_writable(selected)
    refresh = bundle.get('refresh_token')
    if not refresh:
        raise OAuthError('access token expired and no refresh token - reconnect')
    cid, secret, _ = _client()
    return _request_and_settle(selected, {
        'refresh_token': refresh, 'client_id': cid, 'client_secret': secret,
        'grant_type': 'refresh_token',
    }, 'token')['access_token']


def access_token(user: str) -> str:
    token = access_token_if_connected(user)
    if token is None:
        raise OAuthError('no Google account connected for this user')
    return token
