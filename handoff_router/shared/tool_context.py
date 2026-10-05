"""Select and reconstruct Tool context for independently uploaded Filters."""

import copy
from dataclasses import dataclass

from handoff_router.shared.tool_history import ToolExchange, analyze_history, resolve_agent_id


@dataclass(frozen=True)
class AvailableToolContext:
    messages: list[dict]
    allowed_tool_names: frozenset[str]


@dataclass(frozen=True)
class _CompletedTurn:
    start: int
    end: int
    final_answer: int


@dataclass(frozen=True)
class _ContextSelection:
    exchanges: tuple[ToolExchange, ...]
    current_user: int
    handoff_end: int = -1
    # None selects available Tools; a tuple selects completed historical turns.
    turns: tuple[_CompletedTurn, ...] | None = None


def _visible_assistant(message: dict) -> bool:
    content = message.get("content")
    return bool(
        (content.strip() if isinstance(content, str) else content)
        or message.get("tool_calls")
        or message.get("reasoning_content")
        or message.get("thinking")
    )


def _render_context(messages: list[dict], selection: _ContextSelection) -> list[dict]:
    """Emit selected occurrences in source order with two fixed text profiles.

    Available-Tools selection preserves ordinary messages and assistant fields.
    Historical selection emits only chosen questions, final answers and minimal
    Tool messages, then copies the whole current continuation without filtering.
    """
    selected_calls = {(item.message_index, item.call_index) for item in selection.exchanges}
    selected_results = {item.result_index for item in selection.exchanges}
    historical = selection.turns is not None
    current_user = selection.current_user
    marker_index = selection.handoff_end
    questions = {turn.start for turn in selection.turns or ()}
    answers = {turn.final_answer for turn in selection.turns or ()}
    message_indices = (
        (index for turn in selection.turns or () for index in range(turn.start, turn.end))
        if historical else range(len(messages))
    )
    recovered_calls = [
        messages[item.message_index]["tool_calls"][item.call_index]
        for item in sorted(selection.exchanges, key=lambda item: (item.message_index, item.call_index))
        if current_user < item.message_index < marker_index
    ]
    rendered = (
        [copy.deepcopy(message) for message in messages[:current_user] if message.get("role") == "system"]
        if historical else []
    )

    for index in message_indices:
        original = messages[index]
        if not historical and current_user < index <= marker_index:
            # OWUI may group child calls before the selected Handoff result.
            if index == marker_index and recovered_calls:
                rendered.append({"role": "assistant", "content": "", "tool_calls": recovered_calls})
            continue

        role = original.get("role")
        if role == "assistant" and original.get("tool_calls"):
            kept = [
                call for call_index, call in enumerate(original["tool_calls"])
                if (index, call_index) in selected_calls
            ]
            if historical:
                if kept:
                    rendered.append({
                        "role": "assistant", "content": "",
                        "tool_calls": [copy.deepcopy(call) for call in kept],
                    })
            else:
                message = dict(original)
                message["tool_calls"] = kept
                if not kept:
                    if index < current_user:
                        # Excluded Tool narration must not become a final answer.
                        continue
                    message.pop("tool_calls", None)
                    message.pop("reasoning_items", None)
                if _visible_assistant(message):
                    rendered.append(message)
        elif role == "tool":
            if index in selected_results:
                rendered.append(copy.deepcopy(original) if historical else original)
        elif historical:
            if index in questions and role == "user":
                rendered.append(copy.deepcopy(original))
            elif index in answers:
                rendered.append({"role": "assistant", "content": copy.deepcopy(original.get("content"))})
        else:
            rendered.append(original)

    if historical:
        rendered.extend(copy.deepcopy(messages[current_user:]))
    return rendered


class ToolContextProjection:
    """Two pure transformations; neither retains state or changes its input.

    Each operation analyzes its own input. Occurrence indices never escape this
    module or survive a transformation of the messages they refer to.
    """

    @staticmethod
    def available_tools(body: dict) -> AvailableToolContext:
        """Select completed exchanges allowed by the current model context.

        The inlet adapter validates the body first. Read its Tool schemas and
        metadata without changing either; retain current copy/field semantics.
        """
        metadata = body.get("metadata", {})
        messages = body["messages"]
        allowed_names = {
            str(((schema or {}).get("function") or {}).get("name") or "").strip()
            for schema in body.get("tools") or []
        }
        allowed_names.update(str(name or "").strip() for name in (metadata.get("tools") or {}))
        if (
            metadata.get("lite_view_skill_available")
            and metadata.get("lite_view_skill_model_id") == metadata.get("lite_target_model_id")
        ):
            allowed_names.add("view_skill")
        allowed_names.discard("")
        registry = metadata.get("lite_agents")
        registry = registry if isinstance(registry, dict) else {}
        target_agent = str(metadata.get("lite_target_agent_id") or "").strip()
        history = analyze_history(messages, registry=registry)
        current_user = history.current_user_index
        selected_agent = resolve_agent_id(target_agent, registry)
        marker_index = -1
        if current_user >= 0 and target_agent:
            for exchange in history.exchanges:
                if exchange.message_index <= current_user or exchange.handoff is None:
                    continue
                destination = exchange.handoff.agent_id if registry else exchange.handoff.declared_agent_id
                if destination and destination == (selected_agent or target_agent):
                    marker_index = max(marker_index, exchange.result_index)

        accepted = []
        for exchange in history.exchanges:
            call = messages[exchange.message_index]["tool_calls"][exchange.call_index]
            name = str((call.get("function") or {}).get("name") or "").strip()
            if name not in allowed_names:
                continue
            if registry and target_agent and exchange.executor.kind == "unknown":
                continue
            if exchange.message_index < current_user:
                if target_agent and (
                    name == "lite_delegate" or exchange.executor.kind != "subagent"
                    or exchange.executor.agent_id != selected_agent
                ):
                    continue
            elif exchange.result_index <= marker_index:
                continue
            accepted.append(exchange)

        selection = _ContextSelection(tuple(accepted), current_user, marker_index)
        return AvailableToolContext(_render_context(messages, selection), frozenset(allowed_names))

    @staticmethod
    def completed_history(
        messages: list[dict], *, history_turns: int, history_tool_calls: int,
    ) -> list[dict]:
        """Limit past completed turns and exchanges without limiting the current request.

        Counts are nonnegative, as enforced by the inlet Valves. This operation
        also works without available-Tools selection having run beforehand.
        """
        history = analyze_history(messages)
        indices = history.user_indices
        if not indices:
            return list(messages)

        completed = []
        for start, end in zip(indices[:-1], indices[1:]):
            final_answer = next(
                (
                    index for index in range(end - 1, start - 1, -1)
                    if messages[index].get("role") == "assistant"
                    and messages[index].get("content")
                    and not messages[index].get("tool_calls")
                ),
                -1,
            )
            if final_answer >= 0:
                completed.append(_CompletedTurn(start, end, final_answer))
        turns = tuple(completed[-history_turns:]) if history_turns else ()
        exchanges = [
            exchange for exchange in history.exchanges
            if any(turn.start <= exchange.message_index < exchange.result_index < turn.end for turn in turns)
        ]
        exchanges = exchanges[-history_tool_calls:] if history_tool_calls else []
        selection = _ContextSelection(tuple(exchanges), indices[-1], turns=turns)
        return _render_context(messages, selection)
