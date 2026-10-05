"""Interpret completed Tool history before consumers select or format it."""

import json
from dataclasses import dataclass
from typing import Any

from shared.request_runtime import RequestRuntime


def _agent_registry(registry: dict | None) -> dict:
    if not isinstance(registry, dict):
        return {}
    return {
        str(agent_id or "").strip(): config
        for agent_id, config in registry.items()
        if str(agent_id or "").strip() and isinstance(config, dict)
        and str(config.get("model_id") or "").strip()
    }


def _resolve_agent_id(value: str | None, agents: dict) -> str | None:
    if value in agents:
        return value
    matches = [
        agent_id for agent_id, config in agents.items()
        if value and str(config.get("routing_skill_id") or "").strip() == value
    ]
    return matches[0] if len(matches) == 1 else None


def resolve_agent_id(value: str | None, registry: dict) -> str | None:
    """Resolve a direct ID or an unambiguous accepted routing Skill alias."""
    return _resolve_agent_id(value, _agent_registry(registry))


def parse_handoff(value: Any) -> str | None:
    """Read a v2 destination without deciding whether it is available."""
    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(value.strip())
            if isinstance(value, str):
                value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict) or value.get("__lite_delegate__") != "v2":
        return None
    return str(value.get("agent_id") or "").strip() or None


@dataclass(frozen=True)
class ToolExecutor:
    kind: str
    agent_id: str | None = None
    declared_agent_id: str | None = None
    model_id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class HandoffEvidence:
    declared_agent_id: str
    agent_id: str | None


@dataclass(frozen=True)
class ToolExchange:
    message_index: int
    call_index: int
    result_index: int
    executor: ToolExecutor
    handoff: HandoffEvidence | None = None


@dataclass(frozen=True)
class ToolHistory:
    """Immutable facts whose indices refer to the unmodified input history."""

    user_indices: tuple[int, ...]
    exchanges: tuple[ToolExchange, ...]

    @property
    def current_user_index(self) -> int:
        return self.user_indices[-1] if self.user_indices else -1

    @property
    def current_handoff(self) -> str | None:
        return next(
            (
                exchange.handoff.declared_agent_id
                for exchange in reversed(self.exchanges)
                if exchange.message_index > self.current_user_index
                and exchange.handoff is not None
            ),
            None,
        )


def _completed_exchanges(messages: list[dict]) -> list[tuple[int, int, int]]:
    """Match only within one assistant execution batch in one user request.

    OWUI may group several sequential executions into one assistant message.
    Unique IDs can still correlate those calls with their ordered results. Reused
    IDs within that batch are ambiguous; a later batch cannot complete an earlier
    one. Identical repeated results represent one exchange, conflicting ones none.
    """
    exchanges: list[tuple[int, int, int]] = []
    calls: dict[str, list[tuple[int, int]]] = {}
    results: dict[str, list[int]] = {}

    def finish_batch() -> None:
        for call_id, occurrences in calls.items():
            matching_results = results.get(call_id, [])
            if len(occurrences) != 1 or not matching_results:
                continue
            first_result = matching_results[0]
            if any(messages[index] != messages[first_result] for index in matching_results[1:]):
                continue
            message_index, call_index = occurrences[0]
            exchanges.append((message_index, call_index, first_result))
        calls.clear()
        results.clear()

    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant" or (role == "user" and not RequestRuntime.is_tool_image_message(message)):
            finish_batch()
        if role == "assistant":
            for call_index, call in enumerate(message.get("tool_calls") or []):
                call_id = call.get("id")
                if isinstance(call_id, str) and call_id:
                    calls.setdefault(call_id, []).append((index, call_index))
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id in calls:
                results.setdefault(call_id, []).append(index)
    finish_batch()
    return exchanges


def analyze_history(messages: list[dict], *, registry: dict | None = None) -> ToolHistory:
    """Pair and attribute exchanges in Tool Result order without changing input.

    None means standalone execution: every executor stays model. A supplied
    Registry, even empty, enables Router attribution. Missing or ambiguous
    destinations remain Handoff evidence, but cannot prove a subagent executor.
    """
    if not isinstance(messages, list):
        raise TypeError("Tool history messages must be a list")
    if registry is not None and not isinstance(registry, dict):
        raise TypeError("Tool history Registry must be an object")

    agents = _agent_registry(registry)
    pairs = _completed_exchanges(messages)
    by_result = {result_index: (message_index, call_index) for message_index, call_index, result_index in pairs}
    completed_calls = {(message_index, call_index) for message_index, call_index, _ in pairs}
    root_executor = ToolExecutor("model" if registry is None else "orchestrator")
    executor = root_executor
    uncertain_batch = False
    user_indices: list[int] = []
    exchanges: list[ToolExchange] = []

    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "user" and not RequestRuntime.is_tool_image_message(message):
            user_indices.append(index)
            executor = root_executor
            uncertain_batch = False
        if role == "assistant":
            uncertain_batch = any(
                (call.get("function") or {}).get("name") == "lite_delegate"
                and (index, call_index) not in completed_calls
                for call_index, call in enumerate(message.get("tool_calls") or [])
            )
            if registry is not None and uncertain_batch:
                # An unpaired delegate has no trustworthy transition position.
                executor = ToolExecutor("unknown")

        pair = by_result.get(index)
        if pair is None:
            continue
        message_index, call_index = pair
        call = messages[message_index]["tool_calls"][call_index]
        is_delegate = (call.get("function") or {}).get("name") == "lite_delegate"
        declared_id = parse_handoff(message.get("content")) if is_delegate else None
        agent_id = _resolve_agent_id(declared_id, agents) if declared_id is not None else None
        handoff = HandoffEvidence(declared_id, agent_id) if declared_id is not None else None
        exchanges.append(ToolExchange(message_index, call_index, index, executor, handoff))

        # The delegate exchange itself belongs to the preceding executor.
        if registry is not None and is_delegate:
            if uncertain_batch or agent_id is None:
                executor = ToolExecutor("unknown", declared_agent_id=declared_id)
            else:
                config = agents[agent_id]
                executor = ToolExecutor(
                    "subagent", agent_id=agent_id, declared_agent_id=declared_id,
                    model_id=str(config.get("model_id") or "").strip(),
                    name=str(config.get("name") or agent_id),
                )

    return ToolHistory(tuple(user_indices), tuple(exchanges))
