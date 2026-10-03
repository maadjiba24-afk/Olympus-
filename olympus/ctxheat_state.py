"""M07 exact-owner state. One publication covers heat, pins and their receipts.

Uses the reviewed held-directory IO primitives, not the gallery schema. Reads
never create a lock, quarantine, repair, sync or initialize. POSIX writers use
flock; Windows is supported only with one process per state directory (M13 is
separate). File flush and atomic rename are available on Windows; directory
fsync/power-loss durability is not claimed there. Administrators remain trusted.
"""
from __future__ import annotations

import contextlib
from bisect import bisect_right
import copy
import hashlib
import math
import os
from pathlib import Path
import re
import uuid

from . import config, memory
from .gallery_state import Directory, GalleryError, _decode, _json, _guard

MAX_BYTES = 8 * 1024 * 1024
MAX_COUNT = 2**31 - 1
MAX_TIME = 253402300799
MAX_ENTRIES = 2000
MAX_EVENTS = 10000
MAX_OPERATIONS = 2000
MAX_SHADOW = 500
HEX = re.compile(r'[0-9a-f]{64}\Z')
ID = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
STATE = 'state.json'
MARKER = 'initialized.json'
LEGACY = ('context_heat.json', 'pins.json', 'ctxheat_shadow.jsonl')


class StateError(RuntimeError):
    def __init__(self, code='unavailable'):
        self.code = code
        super().__init__('Context heat evidence ' + code + '; state preserved')


def owner(user=None):
    value = memory.current_owner() if user is None else user
    if (not isinstance(value, str) or not value.strip() or len(value) > 8192
            or '\x00' in value):
        raise StateError('invalid_owner')
    try:
        raw = value.encode('utf-8', 'strict')
    except UnicodeError as exc:
        raise StateError('invalid_owner') from exc
    if len(raw) > 32768:
        raise StateError('invalid_owner')
    return value


def path(user=None):
    exact = owner(user)
    return config.MEMORY_DIR / 'owners' / memory.owner_key(exact) / 'ctxheat-v2'


def digest(value):
    return hashlib.sha256(_json(value)).hexdigest()


def integer(value, low=0, high=MAX_COUNT):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('invalid integer')


def number(value, high=MAX_TIME):
    if (type(value) not in (int, float) or not 0 <= value <= high
            or not math.isfinite(value)):
        raise ValueError('invalid finite number')


def fields(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise ValueError('invalid fields')


def token(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError('invalid reference')


def revision(value, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError('invalid revision')


def collection(value, kind, cap):
    if type(value) is not kind or len(value) > cap:
        raise ValueError('invalid collection')


def empty(exact, nonce):
    return {'version': 2, 'owner': exact, 'nonce': nonce, 'serial': 0,
            'entries': {}, 'bindings': {}, 'retired': [], 'events': {},
            'pins': [], 'gate': None, 'operations': {}, 'shadow': []}


def _pin(row):
    fields(row, 'id kind pinned_at est_tokens score verifier_ok revision')
    from . import ctxheat
    if not ctxheat._clean_id(row['id']) or not ctxheat._clean_kind(row['kind']) or ctxheat._clean_id(row['id']) != row['id'] or ctxheat._clean_kind(row['kind']) != row['kind']:
        raise ValueError('invalid pin identity')
    number(row['pinned_at']); number(row['score'], 1e12)
    integer(row['est_tokens'], 1, 1000000); integer(row['verifier_ok'], 1)
    revision(row['revision'])


def latest_pin_operation(data):
    candidates = [(op['revision'], oid) for oid, op in data['operations'].items()
                  if op['status'] in ('applied', 'rolled_back')]
    return max(candidates)[1] if candidates else None


def validate(data, exact, nonce):
    fields(data, 'version owner nonce serial entries bindings retired events pins gate operations shadow')
    if data['version'] != 2 or type(data['version']) is not int or data['owner'] != exact or data['nonce'] != nonce:
        raise ValueError('wrong owner/version/initialization')
    token(nonce); integer(data['serial'])
    from . import ctxheat
    collection(data['entries'], dict, MAX_ENTRIES)
    collection(data['bindings'], dict, MAX_ENTRIES)
    if set(data['entries']) != set(data['bindings']):
        raise ValueError('missing source attribution')
    for key, entry in data['entries'].items():
        if not ctxheat._entry_ok(entry) or key != ctxheat.key(entry['kind'], entry['id']):
            raise ValueError('invalid heat entry')
        revision(data['bindings'][key], nullable=True)
    collection(data['retired'], list, MAX_ENTRIES)
    for row in data['retired']:
        fields(row, 'entry revision serial')
        integer(row['serial'], 0, data['serial'])
        if not ctxheat._entry_ok(row['entry']):
            raise ValueError('invalid retired entry')
        revision(row['revision'], nullable=True)
    collection(data['events'], dict, MAX_EVENTS)
    counts = {}
    event_serials = set()
    for eid, event in data['events'].items():
        token(eid)
        fields(event, 'owner item kind revision source run_id event_id accepted observed_at recorded_serial evidence_digest')
        if event['owner'] != exact or event['event_id'] != eid or type(event['accepted']) is not bool:
            raise ValueError('invalid verifier attribution')
        if event['source'] not in ctxheat.TRUSTED_VERIFIER_SOURCES:
            raise ValueError('unqualified verifier')
        token(event['run_id']); revision(event['revision']); revision(event['evidence_digest'])
        if not ctxheat._clean_id(event['item']) or not ctxheat._clean_kind(event['kind']) or ctxheat._clean_id(event['item']) != event['item'] or ctxheat._clean_kind(event['kind']) != event['kind']:
            raise ValueError('invalid verifier identity')
        number(event['observed_at'])
        integer(event['recorded_serial'], 1, data['serial'])
        if event['recorded_serial'] in event_serials:
            raise ValueError('duplicate verifier publication')
        event_serials.add(event['recorded_serial'])
        if event['evidence_digest'] != digest({k:v for k,v in event.items() if k != 'evidence_digest'}):
            raise ValueError('verifier receipt digest mismatch')
        key = ctxheat.key(event['kind'], event['item'])
        pair = counts.setdefault((key, event['revision']), ([], []))
        pair[0 if event['accepted'] else 1].append(event['recorded_serial'])
    for accepted, rejected in counts.values():
        accepted.sort(); rejected.sort()
    def reconcile(entry, rev, cutoff):
        accepted, rejected = counts.get((ctxheat.key(entry['kind'], entry['id']), rev), ([], []))
        if (entry['verifier_ok'] != bisect_right(accepted, cutoff)
                or entry['corrections'] < bisect_right(rejected, cutoff)):
            raise ValueError('unattributed verifier counters')
    for key, entry in data['entries'].items():
        reconcile(entry, data['bindings'][key], data['serial'])
    for row in data['retired']:
        reconcile(row['entry'], row['revision'], row['serial'])
    collection(data['pins'], list, 1000)
    seen = set()
    for pin in data['pins']:
        _pin(pin)
        key = ctxheat.key(pin['kind'], pin['id'])
        if key in seen:
            raise ValueError('duplicate pin')
        seen.add(key)
    collection(data['operations'], dict, MAX_OPERATIONS)
    operation_revisions = set()
    for oid, op in data['operations'].items():
        token(oid)
        fields(op, 'kind request status created revision binding proposals result')
        if op['kind'] not in ('gate', 'rollback') or op['status'] not in ('evaluating', 'qualified', 'applied', 'refused', 'rolled_back'):
            raise ValueError('invalid operation')
        revision(op['request']); number(op['created']); integer(op['revision'])
        if op['result'] not in ('pending', 'benchmark_passed', 'benchmark_failed', 'gate_error', 'stale', 'applied', 'rolled_back'):
            raise ValueError('invalid operation result')
        transitions = {
            ('gate','evaluating','pending'), ('gate','qualified','benchmark_passed'),
            ('gate','applied','applied'), ('gate','refused','benchmark_failed'),
            ('gate','refused','gate_error'), ('gate','refused','stale'),
            ('rollback','rolled_back','rolled_back'),
        }
        if (op['kind'],op['status'],op['result']) not in transitions or not 1 <= op['revision'] <= data['serial']:
            raise ValueError('invalid operation transition')
        if op['revision'] in operation_revisions:
            raise ValueError('duplicate operation revision')
        operation_revisions.add(op['revision'])
        ctxheat._validate_binding(op['binding'], exact)
        ctxheat._validate_proposals(op['proposals'])
        binding = op['binding']
        if binding['nonce'] != nonce or not binding['serial'] < op['revision']:
            raise ValueError('invalid operation ancestry')
        if op['kind'] == 'gate':
            if binding['policy']['mode'] != 'on' or binding['policy']['qualified'] is not True:
                raise ValueError('unqualified gate policy')
            pins = [p for p in op['proposals'] if p['action'] != 'drop']
            if (len(pins) > binding['policy']['max_pins']
                    or sum(p['est_tokens'] for p in pins) > binding['policy']['budget']):
                raise ValueError('gate policy bounds exceeded')
            for pin in pins:
                key = ctxheat.key(pin['kind'],pin['id'])
                if (key not in binding['sources'] or pin['est_tokens'] < binding['estimates'][key]
                        or pin['verifier_ok'] < binding['policy']['min_verified']):
                    raise ValueError('unqualified gate item')
        if digest({'binding':op['binding'], 'proposals':op['proposals'], 'kind':op['kind']}) != op['request']:
            raise ValueError('operation digest mismatch')
    gate = data['gate']
    if gate is not None:
        fields(gate, 'operation_id pins_digest')
        token(gate['operation_id']); revision(gate['pins_digest'])
        op = data['operations'].get(gate['operation_id'])
        if not op or op['status'] != 'applied' or gate['pins_digest'] != digest(data['pins']):
            raise ValueError('unproven pin publication')
        proposed = [p for p in op['proposals'] if p['action'] != 'drop']
        if [(p['id'],p['kind'],p['est_tokens'],p['score'],p['verifier_ok']) for p in proposed] != [
                (p['id'],p['kind'],p['est_tokens'],p['score'],p['verifier_ok']) for p in data['pins']]:
            raise ValueError('pins differ from gate')
        if any(op['binding']['sources'].get(ctxheat.key(p['kind'],p['id'])) != p['revision'] for p in data['pins']):
            raise ValueError('pin revision differs from gate')
        if any(p['pinned_at'] != op['created'] for p in data['pins']):
            raise ValueError('pin timestamp differs from gate')
    elif data['pins']:
        raise ValueError('pins without gate')
    latest = latest_pin_operation(data)
    if latest is not None:
        if data['operations'][latest]['status'] == 'rolled_back':
            if gate is not None or data['pins']:
                raise ValueError('rolled-back pins restored without a new gate')
        elif gate is None or gate['operation_id'] != latest:
            raise ValueError('active pins do not match latest application')
    collection(data['shadow'], list, MAX_SHADOW)
    last = -1
    for row in data['shadow']:
        fields(row, 'serial owner at reason proposal_digest')
        integer(row['serial']); number(row['at']); revision(row['proposal_digest'])
        if row['owner'] != exact or row['serial'] <= last or row['serial'] > data['serial']:
            raise ValueError('shadow attribution mismatch')
        if row['reason'] not in ('shadow','no_benchmark_gate','refused','qualified','applied','rolled_back'):
            raise ValueError('invalid shadow label')
        last = row['serial']


def _legacy(exact):
    """Presence is unclaimed, never authority; no legacy bytes are opened."""
    old_scope = '' if exact == 'shared' else 'users/' + memory.safe_id(exact)
    directory = None
    try:
        directory = Directory(config.MEMORY_DIR / old_scope)
        for name in LEGACY:
            try:
                fd = directory._open(name, os.O_RDONLY)
            except FileNotFoundError:
                continue
            os.close(fd)
            raise StateError('unclaimed')
    except FileNotFoundError:
        pass
    finally:
        if directory is not None:
            directory.close()


def _load(directory, exact):
    marker = _decode(directory.read(MARKER, 65536))
    fields(marker, 'version owner nonce')
    if marker['version'] != 2 or type(marker['version']) is not int or marker['owner'] != exact:
        raise ValueError('invalid initialization')
    token(marker['nonce'])
    data = _decode(directory.read(STATE, MAX_BYTES))
    validate(data, exact, marker['nonce'])
    return data


def read(user=None):
    exact = owner(user)
    directory = None
    try:
        _legacy(exact)
        try:
            directory = Directory(path(exact))
        except FileNotFoundError:
            return None
        return _load(directory, exact)
    except StateError:
        raise
    except (OSError, ValueError, TypeError, GalleryError, RecursionError, OverflowError) as exc:
        raise StateError() from exc
    finally:
        if directory is not None:
            directory.close()


@contextlib.contextmanager
def transaction(user=None):
    exact = owner(user)
    try:
        before = read(exact)
        with _guard(path(exact), create=before is None) as directory:
            _legacy(exact)
            # Interrupted staging files are preserved. Capacity refusal bounds
            # recovery inventory without cleanup or replaying any operation.
            retained = 0
            for index, entry in enumerate(directory.entries()):
                if index >= 64:
                    raise StateError('capacity')
                if entry.name.startswith('pending-'):
                    info = entry.stat(follow_symlinks=False)
                    retained += info.st_size
                    if retained > 32 * 1024 * 1024:
                        raise StateError('capacity')
            try:
                data = _load(directory, exact)
            except FileNotFoundError:
                # Only the empty directory created for this transaction is new.
                # A marker, pending file or prior state forbids a history reset.
                if before is not None or {e.name for e in directory.entries()} - {'store.lock'}:
                    raise StateError()
                nonce = uuid.uuid4().hex
                directory.write_new(MARKER, _json({'version':2,'owner':exact,'nonce':nonce}))
                data = empty(exact, nonce)
                directory.write_new(STATE, _json(data))
            # Mutation/recovery retries establish barriers before acknowledging
            # visible receipts after a previous post-replace lost acknowledgement.
            directory.confirm(MARKER)
            directory.confirm(STATE)
            yield directory, copy.deepcopy(data)
    except StateError:
        raise
    except (OSError, ValueError, TypeError, GalleryError, RecursionError, OverflowError) as exc:
        raise StateError() from exc


def publish(directory, data):
    data['serial'] += 1
    validate(data, data['owner'], data['nonce'])
    raw = _json(data)
    if len(raw) > MAX_BYTES:
        raise StateError('capacity')
    try:
        directory.publish(STATE, raw)
    except (OSError, GalleryError) as exc:
        raise StateError('publication_unconfirmed') from exc


def shadow(data, reason, proposal, at):
    data['shadow'].append({'serial':data['serial']+1,'owner':data['owner'],'at':at,
                           'reason':reason,'proposal_digest':digest(proposal)})
    data['shadow'] = data['shadow'][-MAX_SHADOW:]
