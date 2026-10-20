"""Restricted PostgreSQL selection for a vault snapshot spanning provider I/O.

General short vault transactions retain the store's ordinary DSN support. A
long-lived snapshot requires an explicit single host/port/database/user, no
libpq environment or service selectors, and explicit TCP password custody or
a local socket connection using no password/GSS exchange. This does not prove
server-side peer authentication, freeze DNS/server identity, or tolerate
arbitrary concurrent mutation of process configuration during libpq startup.

Primary contracts: PostgreSQL16 libpq-connect and libpq-envars;
psycopg3 ConnectionInfo and libpq PGconn.used_password/used_gssapi.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from types import SimpleNamespace

from . import store, native_cleanup


class SelectionError(RuntimeError):
    pass


def _environment():
    if any(name.startswith('PG') for name in os.environ):
        raise SelectionError('vault snapshots require explicit PostgreSQL selection without PG environment selectors')


def _parameters(dsn):
    _environment()
    if type(dsn) is not str or not 1 <= len(dsn) <= 32768:
        raise SelectionError('invalid PostgreSQL snapshot selection')
    from psycopg.conninfo import conninfo_to_dict
    opts = conninfo_to_dict(dsn)
    if 'service' in opts or 'passfile' in opts:
        raise SelectionError('service and password-file selection are unsupported for vault snapshots')
    for name in ('host', 'port', 'dbname', 'user'):
        if not opts.get(name):
            raise SelectionError('vault snapshots require explicit host, port, database and user')
    if any(',' in opts.get(name, '') for name in ('host', 'hostaddr', 'port')):
        raise SelectionError('multi-host PostgreSQL selection is unsupported for vault snapshots')
    if not opts['port'].isascii() or not opts['port'].isdigit() or not 1 <= int(opts['port']) <= 65535:
        raise SelectionError('invalid PostgreSQL snapshot port')
    local = Path(opts['host']).is_absolute()
    if local:
        if opts.get('hostaddr') or opts.get('password'):
            raise SelectionError('local vault snapshots require socket selection without password exchange')
    else:
        if opts['host'].startswith('@') or not opts.get('password'):
            raise SelectionError('TCP vault snapshots require explicit password custody')
        if opts.get('hostaddr'):
            ipaddress.ip_address(opts['hostaddr'])
    return opts, local


def capture(dsn, *, connect=None, initialize=False):
    """Validate before any lazy backend connection or constructor DDL."""
    opts, local = _parameters(dsn)
    import psycopg
    if connect is None:
        connect = psycopg.connect
    original_connect = connect

    def captured_connect(requested_dsn, *args, **kwargs):
        _environment()
        if requested_dsn != dsn or args:
            raise SelectionError('PostgreSQL snapshot selection changed')
        # Only the transaction's non-routing settings may be added later.
        if set(kwargs) - {'autocommit', 'connect_timeout'}:
            raise SelectionError('PostgreSQL snapshot connection overrides are unsupported')
        connection = original_connect(dsn, **kwargs)
        try:
            _environment()
            info = connection.info
            if ((info.host, info.port, info.dbname, info.user)
                    != (opts['host'], int(opts['port']), opts['dbname'], opts['user'])):
                raise SelectionError('actual PostgreSQL connection differs from captured selection')
            if opts.get('hostaddr') and info.hostaddr != opts['hostaddr']:
                raise SelectionError('actual PostgreSQL address differs from captured selection')
            if local and info.hostaddr:
                raise SelectionError('local PostgreSQL snapshot became a TCP connection')
            if connection.pgconn.used_gssapi or bool(connection.pgconn.used_password) == local:
                raise SelectionError('PostgreSQL authentication differs from supported snapshot custody')
            return connection
        except BaseException as primary:
            native_cleanup.drain([connection.close], primary)
            raise

    backend = object.__new__(store.PostgresStore)
    backend._dsn = dsn
    backend._psycopg = SimpleNamespace(connect=captured_connect)
    if initialize:
        with backend._conn() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS olympus_kv ('
                         'ns TEXT NOT NULL, k TEXT NOT NULL, v BYTEA NOT NULL,'
                         'PRIMARY KEY (ns, k))')
    return backend
