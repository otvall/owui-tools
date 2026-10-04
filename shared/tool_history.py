"""Pair concrete Tool exchanges without treating correlation IDs as global keys."""

from collections.abc import Callable
from dataclasses import dataclass


def resolve_agent_id(value: str | None, registry: dict) -> str | None:
    """Resolve a direct ID or an unambiguous accepted routing Skill alias."""
    if not isinstance(registry, dict):
        return None
    agents = {
        str(agent_id or "").strip(): config
        for agent_id, config in registry.items()
        if str(agent_id or "").strip() and isinstance(config, dict)
        and str(config.get("model_id") or "").strip()
    }
    if value in agents:
        return value
    matches = [
        agent_id for agent_id, config in agents.items()
        if value and str(config.get("routing_skill_id") or "").strip() == value
    ]
    return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class ToolExchange:
    message_index: int
    call_index: int
    result_index: int


def completed_tool_exchanges(
    messages: list[dict], *, is_tool_image_message: Callable[[dict], bool],
) -> list[ToolExchange]:
    """Match only within one assistant execution batch in one user request.

    OWUI may group several sequential executions into one assistant message.
    Unique IDs can still correlate those calls with their ordered results. Reused
    IDs within that batch are ambiguous; a later batch cannot complete an earlier
    one. Identical repeated results represent one exchange, conflicting ones none.
    """
    exchanges: list[ToolExchange] = []
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
            exchanges.append(ToolExchange(message_index, call_index, first_result))
        calls.clear()
        results.clear()

    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant" or (role == "user" and not is_tool_image_message(message)):
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
