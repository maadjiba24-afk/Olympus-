"""Bounded backup tar/manifest validation with no filesystem mutation.

All byte images are validated before the caller publishes any restored file.
This is not a multi-file restore transaction or an operational recovery proof.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile

from . import archive_io

SCHEMA = 'olympus-backup/1'
MANIFEST = 'BACKUP_MANIFEST.json'
MAX_FILES = 8192
MAX_ENTRIES = 32768
MAX_FILE = 64 * 1024 * 1024
MAX_SOURCE = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_MANIFEST = 4 * 1024 * 1024
MAX_SIGNATURE = 64 * 1024
_HASH = re.compile(r'[0-9a-f]{64}\Z')


class BackupFormatError(ValueError):
    pass


def relative(value):
    if type(value) is not str or not value or len(value) > 2048:
        raise BackupFormatError('invalid bounded backup path')
    try:
        if len(value.encode('utf-8')) > 4096:
            raise ValueError
        parts = value.split('/')
        for part in parts:
            archive_io._filename(part)
    except (ValueError, UnicodeError):
        raise BackupFormatError('unsafe backup path (traversal or nonportable component)') from None
    return parts


def strict_json(raw, cap):
    if type(raw) is not bytes or len(raw) > cap:
        raise BackupFormatError('backup JSON bound exceeded')
    def pairs(items):
        result = {}
        for key,value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    def forbidden(value):
        raise ValueError('unsupported numeric value')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=forbidden, parse_float=forbidden)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise BackupFormatError('backup JSON is unavailable or malformed') from None


def validate_paths(paths):
    """Every component has one spelling and one file/directory role."""
    nodes = {}
    for name in paths:
        parts = relative(name)
        for index in range(1,len(parts)+1):
            spelling = '/'.join(parts[:index])
            key = spelling.casefold()
            role = index == len(parts)
            if key in nodes and nodes[key] != (spelling,role):
                raise BackupFormatError('backup integrity component case or file/directory collision')
            nodes[key] = (spelling,role)


def validate_inventory(manifest, payloads):
    required = {'schema','created','olympus_version','host','full','file_count','files'}
    if (type(manifest) is not dict or set(manifest) != required or manifest['schema'] != SCHEMA
            or type(manifest['full']) is not bool
            or type(manifest['file_count']) is not int
            or type(manifest['files']) is not list
            or not 0 <= manifest['file_count'] == len(manifest['files']) <= MAX_FILES):
        raise BackupFormatError('backup integrity manifest schema or count mismatch')
    for name,cap in [('created',64),('olympus_version',256),('host',255)]:
        if type(manifest[name]) is not str or len(manifest[name]) > cap:
            raise BackupFormatError('backup integrity manifest metadata is invalid')
    changes, folded, total = {}, set(), 0
    for row in manifest['files']:
        if type(row) is not dict or set(row) != {'path','sha256','bytes'}:
            raise BackupFormatError('backup integrity file declaration is invalid')
        rel = row['path']; relative(rel)
        if rel.casefold() in folded:
            raise BackupFormatError('backup integrity duplicate or case-colliding path')
        folded.add(rel.casefold())
        data = payloads.get('data/' + rel)
        if (type(data) is not bytes or type(row['bytes']) is not int
                or not 0 <= row['bytes'] == len(data) <= MAX_FILE
                or type(row['sha256']) is not str or not _HASH.fullmatch(row['sha256'])
                or hashlib.sha256(data).hexdigest() != row['sha256']):
            raise BackupFormatError('backup integrity checksum, size or payload mismatch')
        total += len(data)
        if total > MAX_SOURCE:
            raise BackupFormatError('backup integrity aggregate bound exceeded')
        changes[rel] = data
    validate_paths(changes)
    if set(payloads) != {'data/' + name for name in changes}:
        raise BackupFormatError('backup integrity payload and manifest inventories differ')
    return changes


def decode(raw):
    if type(raw) is not bytes or len(raw) > archive_io.MAX_PLAINTEXT:
        raise BackupFormatError('compressed backup bound exceeded')
    try:
        # Bound PAX/long-name metadata before tarfile interprets a single header.
        with gzip.GzipFile(fileobj=io.BytesIO(raw),mode='rb') as stream:
            expanded = stream.read(MAX_EXPANDED + 1)
        if len(expanded) > MAX_EXPANDED:
            raise BackupFormatError('expanded backup bound exceeded')
        payloads, folded, total = {}, set(), 0
        with tarfile.open(fileobj=io.BytesIO(expanded),mode='r:') as archive:
            for item in archive:
                name = item.name
                relative(name)
                cap = MAX_MANIFEST if name == MANIFEST else MAX_FILE
                if (len(payloads) >= MAX_FILES + 1 or not item.isfile() or item.sparse is not None
                        or name.casefold() in folded or not 0 <= item.size <= cap):
                    raise BackupFormatError('invalid, duplicate or oversized backup archive member')
                if name != MANIFEST and not name.startswith('data/'):
                    raise BackupFormatError('undeclared backup archive member')
                total += item.size
                if total > MAX_SOURCE + MAX_MANIFEST:
                    raise BackupFormatError('backup payload aggregate bound exceeded')
                stream = archive.extractfile(item)
                if stream is None:
                    raise BackupFormatError('missing backup archive payload')
                data = stream.read(item.size + 1)
                if len(data) != item.size:
                    raise BackupFormatError('truncated backup archive member')
                payloads[name] = data
                folded.add(name.casefold())
    except (tarfile.TarError, EOFError, OSError, OverflowError, RecursionError):
        raise BackupFormatError('backup archive could not be decoded') from None
    if MANIFEST not in payloads:
        raise BackupFormatError('backup integrity manifest is missing')
    manifest = strict_json(payloads.pop(MANIFEST), MAX_MANIFEST)
    return manifest, validate_inventory(manifest, payloads)
