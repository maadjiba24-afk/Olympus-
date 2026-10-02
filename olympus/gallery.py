"""Exact-owner gallery surface, deliberately separate from shared file tools.

Legacy flat and lossy-owner directories are never gallery read authority. Only
an operator's reviewed, digest-bound claim can create an owned copy. Recovery
and deletion preserve image bytes; neither certifies complete erasure.
"""
from __future__ import annotations

import os
import contextlib
from pathlib import Path
import time

from .gallery_state import (Directory, EXTENSIONS, GalleryError, MAX_IMAGE_BYTES,
                            MAX_STATE_BYTES, OMITTED, Store, _decode, _digest,
                            _guard, _json, _native, _regular, capture_owner,
                            validate_name)

_IMAGE_EXTS = EXTENSIONS
_MAX_SERVE_BYTES = MAX_IMAGE_BYTES
_GALLERY_DIR = 'gallery-v2'
MAX_LEGACY_SCAN = 10000


def owner_root(user=OMITTED, *, create=False):
    store = Store(capture_owner(user))
    if create:
        try:
            with _guard(store.root, create=True):
                pass
        except OSError as exc:
            raise GalleryError('unavailable', 'Gallery directory unavailable') from exc
    return store.root


def _legacy_candidates(workspace):
    """Bounded handle-relative enumeration; errors never become empty success."""
    with contextlib.ExitStack() as stack:
        root = Directory(workspace)
        stack.callback(root.close)
        scanned = 0
        entries = root.entries()
        stack.callback(entries.close)
        for entry in entries:
            scanned += 1
            if scanned > MAX_LEGACY_SCAN:
                raise GalleryError('capacity', 'Legacy inventory scan bound reached; operator review required', 409)
            if Path(entry.name).suffix.lower() in EXTENSIONS:
                yield entry.name
        try:
            legacy = root.child('gallery')
        except FileNotFoundError:
            return
        stack.callback(legacy.close)
        directories = legacy.entries()
        stack.callback(directories.close)
        for entry in directories:
            scanned += 1
            if scanned > MAX_LEGACY_SCAN:
                raise GalleryError('capacity', 'Legacy inventory scan bound reached', 409)
            if not _regular(entry.stat(follow_symlinks=False), True):
                raise GalleryError('unavailable', 'Unsafe legacy owner directory preserved')
            child = legacy.child(entry.name)
            try:
                images = child.entries()
                try:
                    for image in images:
                        scanned += 1
                        if scanned > MAX_LEGACY_SCAN:
                            raise GalleryError('capacity', 'Legacy inventory scan bound reached', 409)
                        if Path(image.name).suffix.lower() in EXTENSIONS:
                            yield 'gallery/' + entry.name + '/' + image.name
                finally:
                    images.close()
            finally:
                child.close()


def _has_legacy(workspace):
    try:
        candidates = _legacy_candidates(workspace)
        try:
            return next(candidates, None) is not None
        finally:
            candidates.close()
    except (OSError, ValueError) as exc:
        raise GalleryError('unavailable', 'Legacy inventory is unavailable; originals preserved') from exc


def list_result(user=OMITTED):
    store = Store(capture_owner(user))
    images = store.list_images()
    unclaimed = _has_legacy(store.workspace)
    return {'status': 'ok' if images else ('unclaimed' if unclaimed else 'missing'),
            'images': images, 'unclaimed': unclaimed}


def list_images(user=OMITTED):
    return list_result(user)['images']


def read_image(name, user=OMITTED, *, expected_id=None, expected_revision=None):
    data, record = Store(capture_owner(user)).read(name, expected_id, expected_revision)
    return data, record['mime']


def delete_image(name, user=OMITTED, *, expected_id=None, expected_revision=None,
                 operation_id=None):
    return Store(capture_owner(user)).delete(name, expected_id, expected_revision, operation_id)


def operation_status(operation_id, user=OMITTED):
    return Store(capture_owner(user)).status(operation_id)


def recover_operation(operation_id, user=OMITTED):
    return Store(capture_owner(user)).recover(operation_id)


def _legacy_read(store, source):
    if not isinstance(source, str) or '\\' in source:
        raise GalleryError('invalid', 'Invalid reviewed legacy source', 400)
    parts = source.split('/')
    if len(parts) == 1:
        parents = ()
    elif (len(parts) == 3 and parts[0] == 'gallery'
          and parts[1] not in ('', '.', '..') and ':' not in parts[1]
          and all(ord(c) >= 32 for c in parts[1])):
        parents = ('gallery', parts[1])
    else:
        raise GalleryError('invalid', 'Legacy source is outside approved roots', 400)
    validate_name(parts[-1])
    with contextlib.ExitStack() as stack:
        parent = Directory(store.workspace)
        stack.callback(parent.close)
        for component in parents:
            parent = parent.child(component)
            stack.callback(parent.close)
        return parent.read_with_identity(parts[-1], MAX_IMAGE_BYTES)


def legacy_review(user=OMITTED, *, offset=0, limit=1000):
    """Operator-only private review. The caller must review owner attribution."""
    from .image_validation import validate_image
    store = Store(capture_owner(user))
    if type(offset) is not int or not 0 <= offset <= 100000 or type(limit) is not int or not 1 <= limit <= 1000:
        raise GalleryError('invalid', 'Invalid legacy review page', 400)
    items = []
    more = False
    try:
        for index, source in enumerate(_legacy_candidates(store.workspace)):
            if index < offset:
                continue
            if len(items) == limit:
                more = True
                break
            try:
                data, identity = _legacy_read(store, source)
                info = validate_image(data)
                name = source.split('/')[-1]
                if EXTENSIONS[Path(name).suffix.lower()] != info['mime']:
                    raise GalleryError('unsupported_type', 'Legacy image extension mismatch', 415)
                items.append({'source': source, 'name': name, 'bytes': len(data),
                              'revision': _digest(data), 'mime': info['mime'], 'identity': identity})
            except (GalleryError, OSError) as exc:
                items.append({'source': source, 'error': str(exc)})
    except (OSError, ValueError) as exc:
        raise GalleryError('unavailable', 'Legacy review inventory unavailable') from exc
    review = {'version': 2, 'owner': store.owner, 'workspace': os.path.normcase(str(store.workspace)),
              'offset': offset, 'next_offset': offset + len(items) if more else None, 'items': items}
    review['digest'] = _digest(_json(review))
    return review


def claim_legacy(user=OMITTED, *, review=None, review_digest=None):
    """Copy explicitly reviewed attribution, with a global conflict ledger.

    The ledger commits before owned publication. A crash cannot let a different
    owner silently take the same source. Originals are never moved or deleted.
    """
    store = Store(capture_owner(user))
    if not isinstance(review, dict) or not isinstance(review_digest, str):
        raise GalleryError('review_required', 'A reviewed legacy manifest and digest are required', 400)
    body = dict(review)
    supplied = body.pop('digest', None)
    if (set(body) != {'version', 'owner', 'workspace', 'offset', 'next_offset', 'items'}
            or type(body['version']) is not int or body['version'] != 2 or body['owner'] != store.owner
            or body['workspace'] != os.path.normcase(str(store.workspace))
            or supplied != review_digest or _digest(_json(body)) != review_digest
            or not isinstance(body['items'], list) or not 0 < len(body['items']) <= 1000
            or len(_json(body)) > MAX_STATE_BYTES):
        raise GalleryError('review_conflict', 'Reviewed manifest identity does not match', 409)
    seen_sources = set()
    seen_identities = set()
    for item in body['items']:
        if isinstance(item, dict) and 'identity' in item:
            identity = item['identity']
            if not isinstance(identity, dict) or set(identity) != {'device', 'inode', 'mtime_ns'} or any(type(v) is not int or v < 0 for v in identity.values()):
                raise GalleryError('invalid', 'Invalid reviewed source identity', 400)
            if not isinstance(item.get('source'), str):
                raise GalleryError('invalid', 'Invalid reviewed source path', 400)
            source_key = os.path.normcase(item['source'])
            identity_key = (identity['device'], identity['inode'])
            if source_key in seen_sources or identity_key in seen_identities:
                raise GalleryError('review_conflict', 'Duplicate reviewed source identity', 409)
            seen_sources.add(source_key)
            seen_identities.add(identity_key)
    results = []
    with _guard(store.workspace / 'gallery-v2', create=True) as global_directory:
        try:
            ledger = _decode(global_directory.read('claims.json', MAX_STATE_BYTES))
            try:
                sentinel = _decode(global_directory.read('claims.initialized', 65536))
            except FileNotFoundError as exc:
                raise GalleryError('unavailable', 'Claim initialization evidence is missing') from exc
            if sentinel != {'version': 2, 'workspace': body['workspace']} or type(sentinel.get('version')) is not int:
                raise GalleryError('unavailable', 'Claim initialization identity mismatch')
            global_directory.confirm('claims.initialized')
        except FileNotFoundError:
            try:
                global_directory.read('claims.initialized', 65536)
            except FileNotFoundError:
                pass
            else:
                raise GalleryError('unavailable', 'Initialized claim ledger is missing; attribution refused')
            ledger = {'version': 2, 'workspace': body['workspace'], 'claims': {}}
        if (not isinstance(ledger, dict) or set(ledger) != {'version', 'workspace', 'claims'}
                or type(ledger['version']) is not int or ledger['version'] != 2 or ledger['workspace'] != body['workspace']
                or not isinstance(ledger['claims'], dict) or len(ledger['claims']) > 10000):
            raise GalleryError('unavailable', 'Legacy attribution ledger is invalid; preserved')
        for ledger_key, attribution in ledger['claims'].items():
            try:
                if (not isinstance(attribution, dict) or set(attribution) != {'owner', 'source', 'name', 'revision', 'identity'}
                        or not isinstance(attribution['revision'], str) or len(attribution['revision']) != 64
                        or any(c not in '0123456789abcdef' for c in attribution['revision'])
                        or not isinstance(attribution['source'], str)
                        or not isinstance(attribution['identity'], dict)
                        or set(attribution['identity']) != {'device', 'inode', 'mtime_ns'}
                        or any(type(v) is not int or v < 0 for v in attribution['identity'].values())):
                    raise ValueError('bad attribution')
                capture_owner(attribution['owner'])
                validate_name(attribution['name'])
                identity = attribution['identity']
                expected_key = _digest(_json({'workspace': body['workspace'], 'device': identity['device'], 'inode': identity['inode']}))
                if ledger_key != expected_key:
                    raise ValueError('bad attribution identity')
            except (ValueError, TypeError, KeyError, GalleryError) as exc:
                raise GalleryError('unavailable', 'Legacy attribution entries are malformed; preserved') from exc
        if ledger['claims']:
            global_directory.confirm('claims.json')
        for item in body['items']:
            operation_id = None
            try:
                if not isinstance(item, dict) or set(item) != {'source', 'name', 'bytes', 'revision', 'mime', 'identity'}:
                    raise GalleryError('invalid', 'Reviewed candidate has an unresolved error', 400)
                validate_name(item['name'])
                data, identity = _legacy_read(store, item['source'])
                if identity != item['identity'] or len(data) != item['bytes'] or _digest(data) != item['revision']:
                    raise GalleryError('stale', 'Legacy source changed since review', 409)
                key = _digest(_json({'workspace': body['workspace'], 'device': identity['device'], 'inode': identity['inode']}))
                attribution = {'owner': store.owner, 'source': item['source'], 'name': item['name'],
                               'revision': item['revision'], 'identity': item['identity']}
                operation_id = 'claim-' + _digest(_json(attribution))
                previous = ledger['claims'].get(key)
                for prior in ledger['claims'].values():
                    if os.path.normcase(prior['source']) == os.path.normcase(item['source']) and prior != attribution:
                        raise GalleryError('legacy_conflict', 'Legacy path has conflicting attribution history', 409)
                if previous is not None and previous != attribution:
                    raise GalleryError('legacy_conflict', 'Legacy source has conflicting attribution; originals preserved', 409)
                if previous is None:
                    if len(ledger['claims']) >= 10000:
                        raise GalleryError('capacity', 'Legacy attribution ledger capacity reached', 409)
                    candidate = {**ledger, 'claims': {**ledger['claims'], key: attribution}}
                    encoded = _json(candidate)
                    if len(encoded) > MAX_STATE_BYTES:
                        raise GalleryError('capacity', 'Legacy attribution metadata capacity reached', 409)
                    try:
                        try:
                            global_directory.write_new('claims.initialized', _json({'version': 2, 'workspace': body['workspace']}))
                        except FileExistsError:
                            if _decode(global_directory.read('claims.initialized', 65536)) != {'version': 2, 'workspace': body['workspace']}:
                                raise GalleryError('unavailable', 'Claim initialization identity mismatch')
                            global_directory.confirm('claims.initialized')
                        global_directory.publish('claims.json', encoded)
                    except (GalleryError, OSError) as exc:
                        raise GalleryError('ledger_unavailable', 'Attribution publication unconfirmed; claim stopped') from exc
                    ledger = candidate
                reservation = store.reserve(operation_id, 'claim', attribution, item['name'])
                if reservation['execute'] or reservation['status'] == 'reserved':
                    store.start(operation_id)
                elif reservation['status'] in ('complete', 'failed'):
                    results.append(reservation)
                    continue
                # Claim bytes are an owned local copy; retrying their persistence
                # is allowed and never performs a billable provider operation.
                result = store.finalize(operation_id, data, {'legacy_source': item['source'],
                                                             'review_digest': review_digest})
                results.append(result)
            except (GalleryError, OSError) as exc:
                error = exc if isinstance(exc, GalleryError) else GalleryError('unavailable', 'Legacy claim storage unavailable')
                if error.code == 'ledger_unavailable':
                    raise error
                error.operation_id = operation_id
                results.append(error.to_dict())
    return {'status': 'complete' if all(r['status'] == 'complete' for r in results) else 'partial',
            'results': results, 'originals_preserved': True}


def render_list(user=OMITTED):
    result = list_result(user)
    if not result['images']:
        if result['unclaimed']:
            return 'Unclaimed legacy images are preserved. An operator must review attribution before claiming them.'
        return 'No owned images yet.'
    lines = []
    for image in result['images']:
        when = time.strftime('%Y-%m-%d %H:%M', time.localtime(image['updated']))
        lines.append(f"- {image['name']} ({image['bytes'] // 1024}KB, {when}) id={image['id']} revision={image['revision']}")
    return '\n'.join(lines)
