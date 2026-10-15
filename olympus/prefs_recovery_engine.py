"""Uninstalled preference recovery state machine; all addresses come from prefs.

The caller holds one native transaction. No user transform, private input,
vault access or network operation is performed here. This draft is not wired
into public callers and has no execution evidence.
"""
from . import prefs_recovery_records as records


def _refuse():
    records._refuse()


def _same_pointer(left, right):
    return left is None and right is None or left is not None and right is not None and records.encode(left) == records.encode(right)


class View:
    """Validate head history and complete claim chains under one held mutex."""
    def __init__(self, journal):
        self.journal, self.tx = journal, journal.tx
        self.lineage = journal.lineage()
        self.plans = {entry['plan']['operation']: (entry['record'], entry['plan'], entry['selected'], entry['images'])
                      for entry in self.lineage}
        self.phases = {entry['plan']['operation']: entry['head']['state'] for entry in self.lineage}
        self.receipts = {entry['plan']['operation']: entry['completion']
                         for entry in self.lineage if entry['head']['state'] == 'complete'}
        self.claim_records = {}
        self.publication_addresses = {journal.layout['head']}

    def operation(self, identifier, expected_digest=None):
        if identifier not in self.plans:
            self.plans[identifier] = self.journal.read_plan(identifier, expected_digest)
        selected = self.plans[identifier]
        if expected_digest is not None and records.digest(selected[0].raw) != expected_digest:
            _refuse()
        return selected

    def claim_state(self, role, address):
        if address is None:
            return records.Record(None, None), False
        if (role, address) in self.claim_records:
            return self.claim_records[role, address]
        expected = [records.pointer(entry['plan']['operation'], records.digest(entry['record'].raw))
                    for entry in self.lineage if entry['selected'][role] == address]
        record = records.read_record(self.tx, address)
        current = records.claim(record)
        chain, visited = [], set()
        while current is not None:
            identifier = current['operation']
            if identifier in visited or len(visited) >= records.MAX_OPERATIONS:
                _refuse()
            visited.add(identifier)
            _plan_record, plan, selected, _images = self.operation(identifier, current['plan_sha256'])
            if selected[role] != address:
                _refuse()
            chain.append(current)
            previous = records.prior_record(plan['previous'][role])
            current = records.claim(previous)
        pending = False
        if chain != expected:
            # One claim may have been published before its pending head. It
            # may extend only the complete claim history and exact prior head.
            if len(chain) != len(expected) + 1 or chain[1:] != expected:
                _refuse()
            identifier = chain[0]['operation']
            if identifier in self.phases:
                _refuse()
            _raw, plan, _selected, _images = self.operation(identifier, chain[0]['plan_sha256'])
            if records.prior_record(plan['previous']['head']) != self.journal.head:
                _refuse()
            pending = True
        elif chain:
            pending = self.phases.get(chain[0]['operation']) != 'complete'
        result = (record, pending)
        self.claim_records[role, address] = result
        return result

    def snapshot(self, owner_claim, legacy_claim):
        owner, owner_pending = self.claim_state('owner_claim', owner_claim)
        legacy, legacy_pending = self.claim_state('legacy_claim', legacy_claim)
        head = self.journal.head.value()
        pending = (self.journal.interrupted_initialization or owner_pending or legacy_pending
                   or head is not None and head['state'] == 'pending')
        observed = (self.journal.namespace, self.journal.head, owner, legacy)
        stamp = tuple((record.raw, record.identity) for record in observed)
        return stamp, bool(pending)

    def all_claims(self):
        """Validate every retained plan's referenced claims before new recovery."""
        found = {}
        pending_ids = set()
        for identifier in self.journal.operations:
            location = self.journal.locate(identifier)['plan']
            if location not in self.journal.listed:
                continue  # Retained pre-plan preparation, never active authority.
            _record, plan, selected, _images = self.operation(identifier)
            self.publication_addresses.update(_publication_addresses(self.journal, selected))
            for role in ('owner_claim', 'legacy_claim'):
                address = selected[role]
                if address is None:
                    continue
                record, pending = self.claim_state(role, address)
                found[role, address] = record
                if pending:
                    pending_ids.add(records.claim(record)['operation'])
        return found, pending_ids


def _confirm_plan(journal, loaded):
    record, plan, _selected, images = loaded
    locations = journal.locate(plan['operation'])
    for name in records.IMAGE_NAMES:
        if images[name] is not None:
            raw, identity = journal.tx.read(locations[name])
            if raw != images[name]:
                _refuse()
            journal.tx.confirm(locations[name], raw, identity)
    journal.tx.confirm(locations['plan'], record.raw, record.identity)


def _publication_addresses(journal, selected):
    return tuple(address for address in (selected['target'], selected['owner_claim'], selected['legacy_claim'],
        journal.layout['head']) if address is not None)


def _source_observation(tx, plan, selected, images, *, completed=False):
    raw, identity = tx.read(selected['source'])
    retained, retained_identity = tx.read(selected['retained'])
    expected_identity = records._identity(plan['source_identity'])
    if plan['kind'] == 'repair':
        # Repair retains a content-addressed copy; legacy retirement retains
        # the actual original inode. They have different identity contracts.
        if retained is not None and retained != images['source']:
            _refuse()
        if completed:
            if retained != images['source']:
                _refuse()
        elif (raw, identity) != (images['before'], records._identity(plan['before_identity'], absent=True)) and raw != images['after']:
            _refuse()
    elif raw is None:
        if retained != images['source'] or retained_identity[:2] != expected_identity[:2]:
            _refuse()
    elif completed or (raw, identity) != (images['source'], expected_identity) or retained is not None:
        _refuse()
    return (raw, identity), (retained, retained_identity)


def _confirmed_effects(tx, plan, selected, images):
    """Settle fixed planned images; never recompute a merge during resume."""
    original, retained = _source_observation(tx, plan, selected, images)
    before_identity = records._identity(plan['before_identity'], absent=True)
    target = (None, None) if selected['target'] is None else tx.read(selected['target'])
    if selected['target'] is not None:
        if target != (images['before'], before_identity) and target[0] != images['after']:
            _refuse()
        if images['before'] == images['after'] and target != (images['before'], before_identity):
            _refuse()  # Same-byte replacement is not an unchanged before image.
    if plan['kind'] == 'repair':
        if retained[0] is None and target[0] == images['after']:
            _refuse()  # An acknowledged target cannot legitimize a missing archive.
        records.immutable(tx, selected['retained'], images['source'])
    if selected['target'] is not None:
        if target[0] != images['after']:
            records.checked_publish(tx, selected['target'], images['after'])
            target = tx.read(selected['target'])
        if target[0] != images['after']:
            _refuse()
        tx.confirm(selected['target'], target[0], target[1])
    if plan['kind'] in ('migrate', 'discard'):
        tx.retire(selected['source'], selected['retained'], images['source'], records._identity(plan['source_identity']))
    _source_observation(tx, plan, selected, images, completed=True)
    retained = tx.read(selected['retained'])
    if retained[0] != images['source']:
        _refuse()
    tx.confirm(selected['retained'], retained[0], retained[1])
    if selected['target'] is not None:
        if tx.read(selected['target']) != target:
            _refuse()
        tx.confirm(selected['target'], target[0], target[1])
    return target[1], retained[1]


def _reconfirm_receipt(tx, plan, selected, images, receipt, *, historical):
    value = receipt.value()
    _source_observation(tx, plan, selected, images, completed=True)
    retained = tx.read(selected['retained'])
    retained_identity = records._identity(value['retained_identity'])
    if retained != (images['source'], retained_identity):
        _refuse()
    tx.confirm(selected['retained'], retained[0], retained[1])
    if selected['target'] is None:
        if value['target_identity'] is not None:
            _refuse()
    else:
        target = tx.read(selected['target'])
        exact = (images['after'], records._identity(value['target_identity']))
        if target != exact:
            if not historical:
                _refuse()
            # A subsequent ordinary change is legitimate. An old operation
            # reports historical completion without restoring old grants.
        else:
            tx.confirm(selected['target'], target[0], target[1])


def prepare_new(journal, identifier, selected):
    """Admit one new UUID after pure source/merge validation, before its plan.

    The caller builds its fixed plan from these exact previous observations.
    No live target change is authorized by this initialization/preparation.
    """
    records.operation_id(identifier)
    if identifier in journal.operations or len(journal.operations) >= records.MAX_OPERATIONS:
        _refuse()
    view = View(journal)
    _claims, pending_ids = view.all_claims()
    head = journal.head.value()
    if pending_ids or head is not None and head['state'] == 'pending':
        _refuse()
    owner, owner_pending = view.claim_state('owner_claim', selected['owner_claim'])
    legacy, legacy_pending = view.claim_state('legacy_claim', selected['legacy_claim'])
    if owner_pending or legacy_pending or legacy.raw is not None:
        _refuse()  # A legacy source group can never silently acquire a second claimant.
    addresses = tuple(address for address in selected.values() if address is not None)
    locations = journal.locate(identifier)
    journal.tx.reserve(addresses + tuple(locations.values()) + (locations['plan'][:-1] + ('.inventory-probe',),))
    records.reject_unbound_staging(journal.tx, tuple(view.publication_addresses | set(_publication_addresses(journal, selected))))
    journal.initialize()
    return {'head': journal.head.prior(), 'owner_claim': owner.prior(), 'legacy_claim': legacy.prior()}


def settle(journal, identifier, request):
    """Resume one existing immutable plan; no plan construction or callback."""
    view = View(journal)
    loaded = view.operation(identifier)
    record, plan, selected, images = loaded
    if any(plan[key] != request[key] for key in ('kind', 'owner', 'legacy_id', 'overwrite')):
        _refuse()
    # Every historical/dangling claim is validated, including those whose
    # files are absent; a missing pointer never becomes first use.
    _claims, pending_ids = view.all_claims()
    if pending_ids - {identifier}:
        _refuse()
    journal.initialize()  # Reconfirm visible namespace/head before any effects.
    _confirm_plan(journal, loaded)
    plan_digest = records.digest(record.raw)
    historical = view.phases.get(identifier) == 'complete'
    if historical:
        # Keep the exact record whose digest was authenticated by lineage.
        # A new same-schema receipt must not replace that authority on reread.
        receipt = view.receipts[identifier]
        current_receipt = journal.completion(identifier, plan_digest, records.digest(receipt.raw))
        if current_receipt != receipt:
            _refuse()
        _reconfirm_receipt(journal.tx, plan, selected, images, receipt, historical=True)
        journal.tx.confirm(journal.locate(identifier)['completion'], receipt.raw, receipt.identity)
        return {'operation_id': identifier, 'completed': True, 'historical': True, 'count': plan['result_count']}
    receipt = journal.completion(identifier, plan_digest)
    before_head = records.prior_record(plan['previous']['head'])
    pending_head = {'version': 1, 'state': 'pending', 'operation': identifier,
                    'plan_sha256': plan_digest, 'completion_sha256': None}
    if journal.head != before_head and journal.head.raw != records.encode(pending_head):
        _refuse()
    wanted_claim = records.pointer(identifier, plan_digest)
    for role in ('owner_claim', 'legacy_claim'):
        if selected[role] is None:
            continue
        current = records.read_record(journal.tx, selected[role])
        if current != records.prior_record(plan['previous'][role]) and current.raw != records.encode(wanted_claim):
            _refuse()
    # A retained but unarmed plan may have become stale while ordinary writes
    # were still allowed. Refuse before installing its first claim/head.
    armed = journal.head.raw == records.encode(pending_head) or any(
        selected[role] is not None and records.read_record(journal.tx, selected[role]).raw == records.encode(wanted_claim)
        for role in ('owner_claim', 'legacy_claim'))
    if not armed:
        if receipt is not None:
            _refuse()  # Effects and their receipt cannot precede all authority.
        if plan['kind'] in ('migrate', 'discard'):
            if journal.tx.read(selected['source']) != (
                    images['source'], records._identity(plan['source_identity'])):
                _refuse()
            if journal.tx.read(selected['retained']) != (None, None):
                _refuse()
        else:
            _source_observation(journal.tx, plan, selected, images)
        if selected['target'] is not None and journal.tx.read(selected['target']) != (
                images['before'], records._identity(plan['before_identity'], absent=True)):
            _refuse()
    records.reject_unbound_staging(journal.tx, tuple(view.publication_addresses | set(_publication_addresses(journal, selected))))
    for role in ('owner_claim', 'legacy_claim'):
        if selected[role] is not None:
            records.replace_pointer(journal.tx, selected[role], records.prior_record(plan['previous'][role]), wanted_claim)
    journal.head = records.replace_pointer(journal.tx, journal.layout['head'], before_head, pending_head)
    if receipt is None:
        target_identity, retained_identity = _confirmed_effects(journal.tx, plan, selected, images)
        value = {'version': 1, 'operation': identifier, 'plan_sha256': plan_digest,
            'target_identity': None if target_identity is None else list(target_identity),
            'retained_identity': list(retained_identity)}
        receipt = records.immutable(journal.tx, journal.locate(identifier)['completion'], records.encode(value), metadata=True)
    else:
        # Completion exists under a still-pending head: effects cannot repeat.
        _reconfirm_receipt(journal.tx, plan, selected, images, receipt, historical=False)
        journal.tx.confirm(journal.locate(identifier)['completion'], receipt.raw, receipt.identity)
    complete = {**pending_head, 'state': 'complete', 'completion_sha256': records.digest(receipt.raw)}
    journal.head = records.replace_pointer(journal.tx, journal.layout['head'], journal.head, complete)
    return {'operation_id': identifier, 'completed': True, 'historical': False, 'count': plan['result_count']}
