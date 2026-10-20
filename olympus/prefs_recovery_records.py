"""Uninstalled bounded preference recovery records; paths supplied by prefs.

This module owns record parsing, immutable images and receipt/lineage checks.
It does not publish a live preference, retire a legacy source or choose owners.
All methods run inside the caller's one native preference transaction.
"""
from dataclasses import dataclass
import hashlib

from . import prefs_records as codec

MAX_OPERATIONS = 16
MAX_METADATA = 64 * 1024
MAX_RETAINED_BYTES = 100 * 1024 * 1024
IMAGE_NAMES = ('source', 'before', 'after')


class RecoveryEvidenceError(RuntimeError):
    """Recovery metadata is incomplete, changed or outside its bounded schema."""


def _refuse():
    raise RecoveryEvidenceError('preference recovery evidence requires explicit reconciliation')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def operation_id(value):
    if type(value) is not str or len(value) != 32 or any(ch not in '0123456789abcdef' for ch in value):
        _refuse()
    return value


def _sha(value):
    if type(value) is not str or len(value) != 64 or any(ch not in '0123456789abcdef' for ch in value):
        _refuse()
    return value


def _identity(value, *, absent=False):
    if value is None and absent:
        return None
    if (type(value) is not list or len(value) != 5
            or any(type(item) is not int for item in value[:4])
            or value[4] is not None and type(value[4]) is not int):
        _refuse()
    return tuple(value)


def authority(tx):
    binding = tx.binding
    return {'root': list(binding.root_identity), 'locks': list(binding.locks_identity),
            'lock': list(binding.lock_identity)}


def encode(value):
    raw = None
    try:
        raw = codec.encode(value)
    except codec.PreferenceRecordError:
        pass
    if raw is None or len(raw) > MAX_METADATA:
        _refuse()
    return raw


def decode(raw):
    value = None
    try:
        if type(raw) is bytes and len(raw) <= MAX_METADATA:
            value = codec.decode(raw)
    except codec.PreferenceRecordError:
        pass
    if value is None or encode(value) != raw:
        _refuse()
    return value


@dataclass(frozen=True, repr=False)
class Record:
    raw: object
    identity: object

    def value(self):
        return None if self.raw is None else decode(self.raw)

    def prior(self):
        return {'value': self.value(), 'identity': None if self.identity is None else list(self.identity)}


def read_record(tx, address):
    return Record(*tx.read(address, max_bytes=MAX_METADATA))


def prior_record(value):
    if type(value) is not dict or set(value) != {'value', 'identity'}:
        _refuse()
    if value['value'] is None:
        if value['identity'] is not None:
            _refuse()
        return Record(None, None)
    return Record(encode(value['value']), _identity(value['identity']))


def immutable(tx, address, raw, *, metadata=False):
    bound = MAX_METADATA if metadata else codec.MAX_BYTES
    if type(raw) is not bytes or len(raw) > bound:
        _refuse()
    observed = tx.read(address, max_bytes=bound)
    if observed[0] is None:
        tx.write_new(address, raw)
        observed = tx.read(address, max_bytes=bound)
    if observed[0] != raw:
        _refuse()
    tx.confirm(address, raw, observed[1])
    return Record(raw, observed[1])


def reject_unbound_staging(tx, addresses):
    """Observe each publication parent; retain and refuse unknown stages.

    Random staging names from a previous failed publication are not bound to
    this operation's immutable plan. Matching bytes would not establish that
    ownership. No retry creates another such file in an affected parent.
    """
    parents = tuple(sorted({address[:-1] for address in addresses}))
    tx.reserve(tuple(addresses) + tuple(parent + ('.inventory-probe',) for parent in parents))
    for parent in parents:
        for name, _kind, _identity_value in tx.inventory(parent, limit=64) or ():
            if name.casefold().startswith('pending-'):
                _refuse()


def checked_publish(tx, address, raw):
    if type(raw) is not bytes or len(raw) > codec.MAX_BYTES:
        _refuse()
    reject_unbound_staging(tx, (address,))
    tx.publish(address, raw)


def replace_pointer(tx, address, before, value):
    """Settle a pointer only after the caller retained its immutable operation."""
    raw = encode(value)
    observed = read_record(tx, address)
    if observed.raw == raw:
        tx.confirm(address, raw, observed.identity)
        return observed
    if observed != before:
        _refuse()
    checked_publish(tx, address, raw)
    observed = read_record(tx, address)
    if observed.raw != raw:
        _refuse()
    tx.confirm(address, raw, observed.identity)
    return observed


def image_descriptor(raw):
    if raw is None:
        return None
    if type(raw) is not bytes or len(raw) > codec.MAX_BYTES:
        _refuse()
    return {'sha256': digest(raw), 'size': len(raw)}


def _image_descriptor(value):
    if value is None:
        return
    if (type(value) is not dict or set(value) != {'sha256', 'size'}
            or type(value['size']) is not int or not 0 <= value['size'] <= codec.MAX_BYTES):
        _refuse()
    _sha(value['sha256'])


def _head(value):
    if type(value) is not dict or value.get('version') != 1 or type(value.get('version')) is not int:
        _refuse()
    if value.get('state') == 'idle':
        if set(value) != {'version', 'state', 'authority'}:
            _refuse()
        return value
    if (set(value) != {'version', 'state', 'operation', 'plan_sha256', 'completion_sha256'}
            or value['state'] not in ('pending', 'complete')):
        _refuse()
    operation_id(value['operation']); _sha(value['plan_sha256'])
    if value['state'] == 'pending':
        if value['completion_sha256'] is not None:
            _refuse()
    else:
        _sha(value['completion_sha256'])
    return value


def pointer(identifier, plan_sha256):
    return {'version': 1, 'operation': operation_id(identifier), 'plan_sha256': _sha(plan_sha256)}


def claim(record):
    value = record.value()
    if value is None:
        return None
    if (set(value) != {'version', 'operation', 'plan_sha256'}
            or value['version'] != 1 or type(value['version']) is not int):
        _refuse()
    operation_id(value['operation']); _sha(value['plan_sha256'])
    return value


class Journal:
    """All path selection is supplied by the preference owner/path authority.

    layout contains root/operations/namespace/head tuples. locate(UUID) returns
    the fixed five file addresses. resolve(plan) validates its complete schema
    and derived source/target/retained/claim addresses without filesystem IO.
    """
    def __init__(self, tx, layout, locate, resolve, validate_images):
        self.tx, self.layout, self.locate, self.resolve = tx, layout, locate, resolve
        self.validate_images = validate_images
        self.operations = ()
        self.listed = {}
        self.retained_bytes = 0
        self.operation_container_present = False
        self.scan()
        # One fresh native observation proves these descendants absent
        # together. Never retain this result across Journal instances or skip
        # claim reads, later observations, or initialization's real barriers.
        self.namespace = Record(None, None) if self.root_absent else read_record(tx, layout['namespace'])
        self.head = Record(None, None) if self.root_absent else read_record(tx, layout['head'])
        for key, record in (('namespace', self.namespace), ('head', self.head)):
            if record.identity != self.root_listed.get(layout[key]):
                _refuse()
        self.initialization_confirmed = False
        self.initialized = False
        self.interrupted_initialization = False
        namespace, head = self.namespace.value(), self.head.value()
        expected = {'version': 1, 'authority': authority(tx)}
        idle = {'version': 1, 'state': 'idle', 'authority': authority(tx)}
        if namespace is None:
            if self.operation_container_present or head is not None and self.head.raw != encode(idle):
                _refuse()
            self.interrupted_initialization = head is not None
        else:
            if self.namespace.raw != encode(expected) or head is None:
                _refuse()
            _head(head)
            if head['state'] == 'idle' and self.head.raw != encode(idle):
                _refuse()
            self.initialized = True

    def scan(self):
        self.operations, self.listed, self.retained_bytes = (), {}, 0
        self.operation_container_present = False
        self.root_listed = {}
        self.root_absent = False
        root_entries = self.tx.inventory(self.layout['root'], limit=3)
        if root_entries is None:
            self.root_absent = True
            return
        allowed = {self.layout['namespace'][-1]: 'file', self.layout['head'][-1]: 'file',
                   self.layout['operations'][-1]: 'directory'}
        self.root_listed = {}
        for name, kind, identity in root_entries or ():
            if allowed.get(name) != kind:
                _refuse()
            if kind == 'file' and identity[2] > MAX_METADATA:
                _refuse()
            if kind == 'file':
                self.root_listed[self.layout['root'] + (name,)] = identity
        operations = self.tx.inventory(self.layout['operations'], limit=MAX_OPERATIONS)
        self.operation_container_present = operations is not None
        identifiers = []
        total = 0
        listed = {}
        for identifier, kind, _identity_value in operations or ():
            operation_id(identifier)
            if kind != 'directory':
                _refuse()
            identifiers.append(identifier)
            locations = self.locate(identifier)
            directory = locations['plan'][:-1]
            allowed_files = {address[-1]: key for key, address in locations.items()}
            if len(allowed_files) != 5 or set(locations) != {'plan', 'completion', *IMAGE_NAMES}:
                _refuse()
            entries = self.tx.inventory(directory, limit=5)
            if entries is None:
                _refuse()  # It was already observed in the parent listing.
            for name, file_kind, identity in entries:
                key = allowed_files.get(name)
                if key is None or file_kind != 'file':
                    _refuse()
                maximum = codec.MAX_BYTES if key in IMAGE_NAMES else MAX_METADATA
                if not 0 <= identity[2] <= maximum:
                    _refuse()
                total += identity[2]
                if total > MAX_RETAINED_BYTES:
                    _refuse()
                listed[locations[key]] = identity
        self.operations, self.listed, self.retained_bytes = tuple(identifiers), listed, total

    def initialize(self):
        if self.initialized:
            # A fresh process cannot infer confirmation from byte visibility.
            # Reconfirm both exact existing images before any new effect.
            self.tx.confirm(self.layout['head'], self.head.raw, self.head.identity)
            self.tx.confirm(self.layout['namespace'], self.namespace.raw, self.namespace.identity)
            self.initialization_confirmed = True
            return
        idle = {'version': 1, 'state': 'idle', 'authority': authority(self.tx)}
        self.head = immutable(self.tx, self.layout['head'], encode(idle), metadata=True)
        self.namespace = immutable(self.tx, self.layout['namespace'],
            encode({'version': 1, 'authority': authority(self.tx)}), metadata=True)
        self.initialized = True
        self.initialization_confirmed = True
        self.interrupted_initialization = False

    def read_plan(self, identifier, expected_digest=None):
        operation_id(identifier)
        if not self.initialized or identifier not in self.operations:
            _refuse()
        locations = self.locate(identifier)
        record = read_record(self.tx, locations['plan'])
        if record.identity != self.listed.get(locations['plan']):
            _refuse()
        if record.raw is None or expected_digest is not None and digest(record.raw) != _sha(expected_digest):
            _refuse()
        plan = record.value()
        if plan.get('operation') != identifier or encode(plan.get('authority')) != encode(authority(self.tx)):
            _refuse()
        selected = self.resolve(plan)  # Exact schema and centralized path validation.
        descriptors = plan.get('images')
        if type(descriptors) is not dict or set(descriptors) != set(IMAGE_NAMES):
            _refuse()
        images = {}
        for name in IMAGE_NAMES:
            _image_descriptor(descriptors[name])
            raw, identity = self.tx.read(locations[name])
            if identity != self.listed.get(locations[name]):
                _refuse()
            descriptor = descriptors[name]
            if descriptor is None:
                if raw is not None:
                    _refuse()
            elif raw is None or image_descriptor(raw) != descriptor:
                _refuse()
            images[name] = raw
        self.validate_images(plan, images)
        return record, plan, selected, images

    def completion(self, identifier, plan_digest, expected_digest=None):
        record = read_record(self.tx, self.locate(operation_id(identifier))['completion'])
        if record.raw is None:
            return None
        value = record.value()
        if (set(value) != {'version', 'operation', 'plan_sha256', 'target_identity', 'retained_identity'}
                or value['version'] != 1 or type(value['version']) is not int
                or value['operation'] != identifier or value['plan_sha256'] != _sha(plan_digest)):
            _refuse()
        _identity(value['target_identity'], absent=True)
        _identity(value['retained_identity'], absent=True)
        if expected_digest is not None and digest(record.raw) != _sha(expected_digest):
            _refuse()
        return record

    def lineage(self):
        """Validate every referenced predecessor through the bound idle head."""
        current = self.head.value()
        if current is None:
            return ()
        visited, entries = set(), []
        while _head(current)['state'] != 'idle':
            selected_id = current['operation']
            if selected_id in visited or len(visited) >= MAX_OPERATIONS:
                _refuse()
            visited.add(selected_id)
            record, plan, selected, images = self.read_plan(selected_id, current['plan_sha256'])
            receipt = None
            if current['state'] == 'complete':
                receipt = self.completion(selected_id, current['plan_sha256'], current['completion_sha256'])
                if receipt is None:
                    _refuse()
            entries.append({'head': current, 'record': record, 'plan': plan,
                'selected': selected, 'images': images, 'completion': receipt})
            previous = prior_record(plan['previous']['head']).value()
            if previous is None or _head(previous)['state'] not in ('idle', 'complete'):
                _refuse()
            current = previous
        if encode(current) != encode({'version': 1, 'state': 'idle', 'authority': authority(self.tx)}):
            _refuse()
        return tuple(entries)

    def completed_in_history(self, identifier, plan_digest):
        """Visible pending receipts and incomplete predecessor chains refuse."""
        operation_id(identifier); _sha(plan_digest)
        result = False
        for entry in self.lineage():
            current = entry['head']
            if current['operation'] == identifier:
                if current['plan_sha256'] != plan_digest:
                    _refuse()
                result = current['state'] == 'complete'
        return result

    def reserve_new(self, identifier, plan, images, extra_addresses):
        """Validate all new-operation resource admission before any publication."""
        operation_id(identifier)
        if not self.initialization_confirmed or identifier in self.operations or len(self.operations) >= MAX_OPERATIONS:
            _refuse()
        if type(images) is not dict or set(images) != set(IMAGE_NAMES):
            _refuse()
        if (type(plan) is not dict or plan.get('operation') != identifier
                or encode(plan.get('authority')) != encode(authority(self.tx))):
            _refuse()
        raw_plan = encode(plan)
        if {name: image_descriptor(raw) for name, raw in images.items()} != plan.get('images'):
            _refuse()
        self.resolve(plan)
        self.validate_images(plan, images)
        if prior_record(plan['previous']['head']) != self.head:
            _refuse()
        locations = self.locate(identifier)
        intended = len(raw_plan) + sum(len(raw) for raw in images.values() if raw is not None)
        # Budget a full completion plus both claims/head and a retained source
        # for every admitted operation, including each partial preparation.
        conservative = (len(self.operations) + 1) * (5 * codec.MAX_BYTES + 4 * MAX_METADATA) + 2 * MAX_METADATA + codec.MAX_BYTES
        if conservative > MAX_RETAINED_BYTES or self.retained_bytes + intended > MAX_RETAINED_BYTES:
            _refuse()
        self.tx.reserve(tuple(locations.values()) + tuple(extra_addresses))
        return raw_plan

    def retain_plan(self, identifier, plan, images, extra_addresses):
        raw_plan = self.reserve_new(identifier, plan, images, extra_addresses)
        locations = self.locate(identifier)
        for name in IMAGE_NAMES:
            if images[name] is not None:
                immutable(self.tx, locations[name], images[name])
        immutable(self.tx, locations['plan'], raw_plan, metadata=True)
        self.scan()
        loaded = self.read_plan(identifier, digest(raw_plan))
        return loaded
