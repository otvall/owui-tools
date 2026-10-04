#!/usr/bin/env python3
"""Embed the authoritative Skill preparation in the standalone Functions."""

import argparse
import ast
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("lite_handoff_router.py", "skill_context.py", "lite_subagent_registry.py")
BEGIN = "# BEGIN GENERATED SKILL PREPARATION\n"
END = "# END GENERATED SKILL PREPARATION\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report stale output without writing files")
    args = parser.parse_args()
    source = (ROOT / "shared/skill_preparation.py").read_text()
    normalizer = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "normalize_skill_ids"
    )
    normalization_source = "\n".join(source.splitlines()[normalizer.lineno - 1:normalizer.end_lineno])
    outputs = []
    for name in TARGETS:
        path = ROOT / name
        embedded = normalization_source if name == "lite_subagent_registry.py" else source
        block = BEGIN + "# Edit shared/skill_preparation.py; run python3 tools/generate_skill_preparation.py\n" + embedded.rstrip() + "\n" + END
        original = path.read_text()
        if original.count(BEGIN) != 1 or original.count(END) != 1:
            parser.error(f"{name}: expected exactly one generated region")
        before, remainder = original.split(BEGIN)
        _old_block, after = remainder.split(END)
        generated = before + block + after
        if original != generated:
            outputs.append((path, generated))
    if args.check:
        for path, _output in outputs:
            print(f"Stale generated Skill preparation: {path.name}", file=sys.stderr)
        return int(bool(outputs))
    for path, output in outputs:
        path.write_text(output)
        print(f"Updated {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
