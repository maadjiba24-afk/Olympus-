"""Schema introspection into the graph — databases and manifests are code too.

A live PostgreSQL schema and a Cargo workspace both describe structure that
the source files alone don't state: which tables reference which, which crate
depends on which. Both become ENTITY nodes and EXTRACTED edges in the same
per-project graph the code lives in, so `codegraph_impact "users"` can answer
"what breaks if I drop this table" right next to "what calls this function".

Postgres needs the optional psycopg extra (the store's own optional backend
dep — no new surface); Cargo parsing is stdlib `tomllib` with a line-regex
fallback for Python 3.10. Read-only by construction: one SELECT over the
information schema, no DDL, no writes.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import codegraph

_PG_TABLES = """
    SELECT table_schema, table_name, table_type
    FROM information_schema.tables
    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
    ORDER BY table_schema, table_name
"""

_PG_FKS = """
    SELECT tc.table_schema, tc.table_name,
           ccu.table_schema AS f_schema, ccu.table_name AS f_table
    FROM information_schema.table_constraints tc
    JOIN information_schema.constraint_column_usage ccu
      ON tc.constraint_name = ccu.constraint_name
     AND tc.table_schema = ccu.table_schema
    WHERE tc.constraint_type = 'FOREIGN KEY'
    ORDER BY 1, 2, 3, 4
"""


def introspect_postgres(project: str, dsn: str) -> dict:
    """Map a live PostgreSQL schema: tables/views as ENTITY nodes, foreign
    keys as `references` edges. Requires the `postgres` extra (psycopg)."""
    try:
        import psycopg
    except ImportError as err:
        raise RuntimeError(
            "psycopg is not installed — pip install 'olympus-council[postgres]'"
        ) from err
    tables = fks = 0
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(_PG_TABLES).fetchall()
        ids: dict[tuple[str, str], str] = {}
        for schema, name, ttype in rows:
            kind_note = "view" if "VIEW" in (ttype or "") else "table"
            n = codegraph.add_node(project, f"postgres/{schema}",
                                   f"{schema}.{name}", kind=codegraph.ENTITY)
            if n is None:                        # graph at the node cap
                continue
            ids[(schema, name)] = n["id"]
            codegraph.add_rationale(project, f"postgres/{schema}", n["id"],
                                    f"{kind_note} in schema {schema}")
            tables += 1
        for schema, name, f_schema, f_table in conn.execute(_PG_FKS).fetchall():
            a, b = ids.get((schema, name)), ids.get((f_schema, f_table))
            if a and b and codegraph.add_edge(project, a, "references", b):
                fks += 1
    return {"tables": tables, "foreign_keys": fks}


# --- cargo ----------------------------------------------------------------

def _parse_toml(text: str) -> dict:
    try:
        import tomllib
        return tomllib.loads(text)
    except ImportError:                     # Python 3.10 — minimal fallback
        data: dict = {}
        section: dict | None = None
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            m = re.match(r"^\[([^\]]+)\]$", line)
            if m:
                section = data
                for part in m.group(1).split("."):
                    section = section.setdefault(part.strip().strip('"'), {})
                continue
            m = re.match(r"""^([\w-]+)\s*=\s*(?:"([^"]*)"|(\{.*\})|(.+))$""",
                         line)
            if m and section is not None:
                section[m.group(1)] = m.group(2) or m.group(3) or m.group(4)
        return data
    except Exception:
        return {}


def introspect_cargo(project: str, root: str | Path) -> dict:
    """Workspace-internal crate dependencies from Cargo.toml files: one ENTITY
    node per crate, `depends_on` edges between workspace members."""
    root_p = Path(root).resolve()
    crates: dict[str, tuple[str, dict]] = {}
    for manifest in sorted(root_p.rglob("Cargo.toml")):
        rel = str(manifest.relative_to(root_p))
        if any(part in ("target", "vendor", ".git")
               for part in manifest.relative_to(root_p).parts):
            continue
        try:
            data = _parse_toml(manifest.read_text(encoding="utf-8",
                                                  errors="replace"))
        except OSError:
            continue
        name = (data.get("package") or {}).get("name")
        if isinstance(name, str) and name:
            crates[name] = (rel, data)

    edges = 0
    ids: dict[str, str] = {}
    for name, (rel, _data) in crates.items():
        n = codegraph.add_node(project, rel, name, kind=codegraph.ENTITY)
        if n is not None:                        # skip if at the node cap
            ids[name] = n["id"]
    for name, (_rel, data) in crates.items():
        deps = data.get("dependencies") or {}
        if isinstance(deps, dict):
            for dep in deps:
                tgt = ids.get(str(dep))
                if tgt and tgt != ids[name]:
                    if codegraph.add_edge(project, ids[name], "depends_on", tgt):
                        edges += 1
    return {"crates": len(crates), "depends_on": edges}
