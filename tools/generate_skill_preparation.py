#!/usr/bin/env python3
"""Embed shared preparation, lifecycle and Tool context in standalone Functions."""

import argparse
import ast
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("lite_handoff_router.py", "skill_context.py", "lite_subagent_registry.py")
RUNTIME_TARGETS = TARGETS + (
    "previous_tool_context.py", "history_cleanup.py", "tool_call_filter.py", "subagent_context.py",
)
HISTORY_TARGETS = ("tool_call_filter.py", "subagent_context.py", "lite_handoff_router.py", "previous_tool_context.py")
CONTEXT_TARGETS = ("tool_call_filter.py", "subagent_context.py")


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
    source = (ROOT / "shared/skill_preparation.py").read_text()
    source = source.replace("from shared.request_runtime import SkillLoaderOwnership\n", "")
    runtime_source = (ROOT / "shared/request_runtime.py").read_text()
    # Future imports belong at the top of deployment files, which already use them.
    runtime_source = runtime_source.replace("from __future__ import annotations\n", "")
    history_source = (ROOT / "shared/tool_history.py").read_text()
    history_source = history_source.replace("from shared.request_runtime import RequestRuntime\n", "")
    context_source = (ROOT / "shared/tool_context.py").read_text()
    context_source = context_source.replace("from shared.tool_history import ToolExchange, analyze_history, resolve_agent_id\n", "")
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
                embedded = normalization_source if name == "lite_subagent_registry.py" else source
                generated = embed(generated, embedded, "SKILL PREPARATION", "shared/skill_preparation.py")
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
