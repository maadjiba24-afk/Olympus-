#!/usr/bin/env python3
"""Source-similarity audit: Olympus-native code against an external repository.

    git clone --depth 1 https://github.com/shiyu-coder/Kronos /tmp/kronos
    python scripts/independence_similarity.py /tmp/kronos olympus/trading/native

`tests/test_trading_independence.py` proves that Olympus does not *import*,
*name* or *reference* the external project. That is a statement about the
dependency graph. It says nothing about whether the code was copied and renamed,
which is a different accusation and needs a different instrument.

Four probes, in increasing order of how hard they are to evade:

1. **Identifier overlap.** Shared class and function names. Trivially defeated
   by renaming, which is why it is first and not last.
2. **Comment and docstring overlap.** Copied prose is the most common tell in
   practice, because renaming identifiers is easy and rewriting explanations is
   tedious.
3. **Identical code lines.** Normalised for whitespace.
4. **AST shape collision.** Every function and class is reduced to the sequence
   of its AST *node types* — every identifier, constant and string discarded.
   A renamed copy, a reformatted copy and a re-commented copy all produce the
   same hash as the original. **This is the probe that catches cosmetic
   renaming**, and a collision here on a body of any size is close to
   conclusive.

The output is evidence either way. A run that found collisions would be a
finding about Olympus, and this script prints them rather than exiting quietly.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import re
import sys
from collections import defaultdict
from pathlib import Path

#: Below this many AST nodes, a "shape" is a getter or a two-line helper and
#: collisions are meaningless — `def to_dict(self): return {...}` collides with
#: itself across every codebase ever written.
MIN_SHAPE_NODES = 25

#: Short lines collide by accident (`import json`, `return None`). Long ones
#: do not.
MIN_LINE_CHARS = 40


def python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py")
                  if "__pycache__" not in p.parts)


def parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (SyntaxError, ValueError):
        return None


# -- probe 1: identifiers ---------------------------------------------------

def identifiers(paths) -> tuple[set[str], set[str]]:
    classes, functions = set(), set()
    for path in paths:
        tree = parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes.add(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.add(node.name)
    return classes, functions


#: Names every Python codebase shares. Reported separately rather than filtered
#: out, so the reader sees what was excused and can disagree.
UNIVERSAL = frozenset({
    "__init__", "__len__", "__iter__", "__getitem__", "__contains__",
    "__repr__", "__str__", "__post_init__", "__call__", "__enter__",
    "__exit__", "forward", "encode", "decode", "predict", "fit", "get",
    "run", "main", "to_dict", "from_dict", "generate", "name", "close",
})


# -- probe 2: prose ---------------------------------------------------------

def prose(paths) -> set[str]:
    out: set[str] = set()
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") and len(stripped) > 25:
                out.add(re.sub(r"\s+", " ", stripped.lstrip("#").strip().lower()))
        tree = parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                continue
            doc = ast.get_docstring(node)
            if not doc:
                continue
            for paragraph in doc.split("\n\n"):
                text = re.sub(r"\s+", " ", paragraph.strip().lower())
                if len(text) > 40:
                    out.add(text)
    return out


# -- probe 3: lines ---------------------------------------------------------

def code_lines(paths) -> set[str]:
    out: set[str] = set()
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if (stripped.startswith(("#", '"', "'"))
                    or len(stripped) < MIN_LINE_CHARS):
                continue
            out.add(re.sub(r"\s+", " ", stripped))
    return out


# -- probe 4: AST shapes ----------------------------------------------------

def shapes(paths) -> dict[str, list[tuple[str, str, int]]]:
    """`shape hash -> [(file, name, node count)]`.

    The hash covers node *types* only. Two functions collide here when they
    have the same control flow and the same expression structure, whatever
    their identifiers, literals, formatting or comments say.
    """
    out: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for path in paths:
        tree = parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                continue
            sequence = [type(child).__name__ for child in ast.walk(node)]
            if len(sequence) < MIN_SHAPE_NODES:
                continue
            digest = hashlib.sha256(
                "|".join(sequence).encode("utf-8")).hexdigest()[:16]
            out[digest].append((path.name, node.name, len(sequence)))
    return out


# -- file-pair similarity ---------------------------------------------------

def normalised(path: Path) -> str:
    return "\n".join(
        re.sub(r"\s+", " ", line.strip())
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip() and not line.strip().startswith("#"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("external", type=Path, help="the repository to compare against")
    parser.add_argument("ours", type=Path, help="the Olympus tree to audit")
    parser.add_argument("--top", type=int, default=12)
    args = parser.parse_args()

    theirs = python_files(args.external)
    ours = python_files(args.ours)
    if not theirs or not ours:
        print(f"nothing to compare: {len(theirs)} external, {len(ours)} ours")
        return 2

    print(f"external : {args.external}  ({len(theirs)} files)")
    print(f"ours     : {args.ours}  ({len(ours)} files)")
    findings = 0

    print("\n=== 1. identifier overlap ===")
    their_classes, their_functions = identifiers(theirs)
    our_classes, our_functions = identifiers(ours)
    shared_classes = sorted(their_classes & our_classes)
    shared_functions = sorted((their_functions & our_functions) - UNIVERSAL)
    print(f"  external: {len(their_classes)} classes, {len(their_functions)} functions")
    print(f"  ours:     {len(our_classes)} classes, {len(our_functions)} functions")
    print(f"  shared class names ({len(shared_classes)}): {shared_classes or '-'}")
    print(f"  shared function names, excluding universal "
          f"({len(shared_functions)}): {shared_functions or '-'}")
    print(f"  universal names excused: "
          f"{sorted((their_functions & our_functions) & UNIVERSAL) or '-'}")
    findings += len(shared_classes) + len(shared_functions)

    print("\n=== 2. comment and docstring overlap ===")
    their_prose, our_prose = prose(theirs), prose(ours)
    shared_prose = sorted(their_prose & our_prose)
    print(f"  external fragments: {len(their_prose)}   ours: {len(our_prose)}")
    print(f"  identical ({len(shared_prose)}):")
    for text in shared_prose[:args.top]:
        print(f"     {text[:100]}")
    if not shared_prose:
        print("     -")
    findings += len(shared_prose)

    print("\n=== 3. identical code lines ===")
    their_lines, our_lines = code_lines(theirs), code_lines(ours)
    shared_lines = sorted(their_lines & our_lines)
    print(f"  external lines: {len(their_lines)}   ours: {len(our_lines)}")
    print(f"  identical ({len(shared_lines)}):")
    for line in shared_lines[:args.top]:
        print(f"     {line[:100]}")
    if not shared_lines:
        print("     -")
    # Import lines collide across every codebase; count only the rest.
    substantive = [l for l in shared_lines if not l.startswith(("import ", "from "))]
    findings += len(substantive)

    print("\n=== 4. AST shape collision (survives renaming) ===")
    their_shapes, our_shapes = shapes(theirs), shapes(ours)
    collisions = sorted(set(their_shapes) & set(our_shapes),
                        key=lambda h: -max(x[2] for x in their_shapes[h]))
    print(f"  external shapes: {len(their_shapes)}   ours: {len(our_shapes)}")
    print(f"  IDENTICAL STRUCTURES ({len(collisions)}):")
    for digest in collisions[:args.top]:
        left, right = their_shapes[digest][0], our_shapes[digest][0]
        print(f"     {left[2]:>4} nodes  external {left[0]}:{left[1]}"
              f"   <->   ours {right[0]}:{right[1]}")
    if not collisions:
        print("     -")
    findings += len(collisions) * 10          # weighted: this is the strong probe

    print("\n=== 5. closest file pairs ===")
    their_text = {path: normalised(path) for path in theirs}
    ranked = []
    for path in ours:
        text = normalised(path)
        if len(text) < 200:
            continue
        best, best_path = 0.0, None
        for other, other_text in their_text.items():
            ratio = difflib.SequenceMatcher(None, text, other_text).quick_ratio()
            if ratio > best:
                best, best_path = ratio, other
        if best_path is not None:
            exact = difflib.SequenceMatcher(
                None, text, their_text[best_path]).ratio()
            ranked.append((exact, path.name, best_path.name))
    ranked.sort(reverse=True)
    for ratio, ourname, theirname in ranked[:args.top]:
        print(f"     {ratio:.3f}  ours/{ourname:<30} vs external/{theirname}")

    print("\n=== verdict ===")
    if findings == 0:
        print("  No evidence of copying. Zero shared classes, zero shared prose,")
        print("  zero identical substantive lines, zero identical structures.")
        print()
        print("  This shows the absence of textual and structural copying. It")
        print("  cannot show the absence of conceptual influence — two honest")
        print("  implementations of the same idea share no text and no shapes.")
        return 0
    print(f"  {findings} weighted finding(s) above. Read them; a collision in")
    print("  probe 4 on a body of any size is close to conclusive.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
