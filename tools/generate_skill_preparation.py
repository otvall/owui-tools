#!/usr/bin/env python3
"""Embed shared preparation, lifecycle and Tool context in standalone Functions."""

import argparse
import ast
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("lite_handoff_router.py", "skill_context.py", "lite_subagent_registry.py", "router_preparation.py")
NORMALIZATION_TARGETS = ("lite_subagent_registry.py", "router_preparation.py")
RUNTIME_TARGETS = TARGETS + (
    "previous_tool_context.py", "history_cleanup.py", "tool_call_filter.py", "subagent_context.py",
)
HISTORY_TARGETS = ("tool_call_filter.py", "subagent_context.py", "lite_handoff_router.py", "previous_tool_context.py", "router_preparation.py")
CONTEXT_TARGETS = ("tool_call_filter.py", "subagent_context.py")
STAGE_TARGETS = {
    "REGISTRY PREPARATION": ("shared/registry_preparation.py", ("lite_subagent_registry.py", "router_preparation.py")),
    "PREVIOUS TOOL CONTEXT": ("shared/previous_tool_context.py", ("previous_tool_context.py", "router_preparation.py")),
    "HISTORY CLEANUP": ("shared/history_cleanup.py", ("history_cleanup.py", "router_preparation.py")),
}


def deployment_source(path: str) -> str:
    """Local imports are satisfied by earlier embedded regions."""
    return "\n".join(
        line for line in (ROOT / path).read_text().splitlines()
        if not line.startswith("from shared.") and line != "from __future__ import annotations"
    ) + "\n"


def embed(original: str, source: str, region: str, source_name: str) -> str:
    begin = f"# BEGIN GENERATED {region}\n"
    end = f"# END GENERATED {region}\n"
    if original.count(begin) != 1 or original.count(end) != 1:
        raise ValueError(f"expected exactly one generated {region} region")
    before, remainder = original.split(begin)
    _old_block, after = remainder.split(end)
    block = begin + f"# Edit {source_name}; run python3 tools/generate_skill_preparation.py\n" + source.rstrip() + "\n" + end
    return before + block + after


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report stale output without writing files")
    args = parser.parse_args()
    source = deployment_source("shared/skill_preparation.py")
    runtime_source = deployment_source("shared/request_runtime.py")
    history_source = deployment_source("shared/tool_history.py")
    context_source = deployment_source("shared/tool_context.py")
    normalizer = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "normalize_skill_ids"
    )
    normalization_source = "\n".join(source.splitlines()[normalizer.lineno - 1:normalizer.end_lineno])
    outputs = []
    for name in RUNTIME_TARGETS:
        path = ROOT / name
        original = path.read_text()
        try:
            generated = embed(original, runtime_source, "REQUEST RUNTIME", "shared/request_runtime.py")
            if name in HISTORY_TARGETS:
                generated = embed(generated, history_source, "TOOL HISTORY", "shared/tool_history.py")
            if name in CONTEXT_TARGETS:
                generated = embed(generated, context_source, "TOOL CONTEXT", "shared/tool_context.py")
            if name in TARGETS:
                embedded = normalization_source if name in NORMALIZATION_TARGETS else source
                generated = embed(generated, embedded, "SKILL PREPARATION", "shared/skill_preparation.py")
            for region, (stage_source, targets) in STAGE_TARGETS.items():
                if name in targets:
                    generated = embed(generated, deployment_source(stage_source), region, stage_source)
        except ValueError as exc:
            parser.error(f"{name}: {exc}")
        if original != generated:
            outputs.append((path, generated))
    if args.check:
        for path, _output in outputs:
            print(f"Stale generated preparation: {path.name}", file=sys.stderr)
        return int(bool(outputs))
    for path, output in outputs:
        path.write_text(output)
        print(f"Updated {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
