#!/usr/bin/env python3
"""Import every module in the package and in scripts/, on THIS interpreter.

Why this exists, concretely: `scripts/release_pipeline.py` imported `tomllib`
at module scope. That is stdlib only on Python 3.11+, and this project supports
3.10. On the 3.10 CI leg the import raised at collection time, pytest exited 2
having run NOTHING, and the leg failed in ~45s while the others took ~6m — a
signature that looks like a fast test failure and is actually a suite that
never started. Nothing caught it earlier because:

  * `python -m compileall` proves a file PARSES, not that it IMPORTS. A missing
    stdlib module is a perfectly valid parse.
  * the existing Windows/macOS import smoke imports the `olympus` package as a
    whole on 3.12 — it never touches `scripts/`, and 3.12 has `tomllib`.
  * the full test suite catches it, but only after ~6 minutes of matrix time,
    and only if the module happens to be imported by a test.

So: import everything, on the OLDEST supported interpreter, in seconds. Run it
before the suite and a whole class of "works on my 3.13" breakage stops
reaching CI.

    python scripts/import_smoke.py            # olympus/ + scripts/
    python scripts/import_smoke.py --quiet    # only report failures

Exit status is 0 when every module imported, 1 otherwise, with each failure
printed as `module: ExceptionType: message`.

Modules are imported in a SUBPROCESS-free, in-process loop on purpose: the
point is to observe real import side effects on this interpreter. Anything that
must not run at import time should be behind `if __name__ == "__main__":`,
which this honors by never executing a module as `__main__`.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Modules deliberately not imported here, each for a stated reason. Keep this
#: list SHORT and justified — every entry is a hole in the check.
SKIP: dict[str, str] = {
    # Executing the package's __main__ runs the CLI argument parser and exits.
    "olympus.__main__": "running it is the CLI entry point, not an import",
    # This module, imported by itself, would recurse.
    "scripts.import_smoke": "self",
}


def _module_names() -> list[str]:
    """Every importable module under olympus/ and scripts/, dotted."""
    names: list[str] = []
    for path in sorted((ROOT / "olympus").rglob("*.py")):
        rel = path.relative_to(ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts.pop()
        if parts:
            names.append(".".join(parts))
    for path in sorted((ROOT / "scripts").glob("*.py")):
        names.append(f"scripts.{path.stem}")
    return names


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true",
                    help="print only failures and the summary")
    args = ap.parse_args(argv)

    # `scripts/` is not a package; put the repo root on the path so both
    # `olympus.*` and a synthesized `scripts.*` resolve.
    sys.path.insert(0, str(ROOT))

    names = _module_names()
    failures: list[tuple[str, str]] = []
    imported = 0

    for name in names:
        if name in SKIP:
            continue
        try:
            if name.startswith("scripts."):
                # scripts/ has no __init__.py and should not gain one (it is a
                # directory of standalone tools, not a package). Load by path.
                stem = name.split(".", 1)[1]
                spec = importlib.util.spec_from_file_location(
                    f"_smoke_{stem}", ROOT / "scripts" / f"{stem}.py")
                if spec is None or spec.loader is None:
                    raise ImportError(f"no loader for {name}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
            else:
                importlib.import_module(name)
        except BaseException as err:                        # noqa: BLE001
            failures.append((name, f"{type(err).__name__}: {err}"))
            if not args.quiet:
                traceback.print_exc(limit=3)
        else:
            imported += 1

    print(f"import smoke on Python {sys.version.split()[0]}: "
          f"{imported} imported, {len(failures)} failed, "
          f"{len(SKIP)} skipped")
    for name, err in failures:
        print(f"  FAIL {name}: {err}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
