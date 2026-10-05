"""
title: Subagent Preparation
description: Projects eligible Tool history, applies common history limits and installs prepared Skills for Router destinations.
version: 0.21.0
required_open_webui_version: 0.11.1
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

# BEGIN GENERATED REQUEST RUNTIME
# Edit handoff_router/shared/request_runtime.py; run python3 handoff_router/tools/generate_skill_preparation.py
"""Authoritative local request lifecycle for independently uploaded Functions."""


import copy
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class CapabilitySet:
    tool_ids: list[str]
    skill_ids: list[str]
    tools: dict


class ModelPreparation:
    """A model draft whose cached Tools share one request-local history."""

    def __init__(self, runtime, branch: str, body: dict):
        try:
            self._cache_key, messages_key = runtime.CAPABILITY_FIELDS[branch]
        except KeyError:
            raise ValueError(f"Unknown model preparation branch: {branch}") from None
        self._runtime = runtime
        self.body = runtime.routed_body(body)
        messages = runtime.metadata.get(messages_key)
        self._messages = messages if isinstance(messages, list) else []
        runtime.sync(**{messages_key: self._messages})

    @property
    def messages(self) -> list[dict]:
        """Pass this stable list to loaders; preparation owns its contents."""
        return self._messages

    async def capabilities(
        self,
        *,
        model_id: str,
        tool_ids: list[str],
        skill_ids: list[str],
        load: Callable[[list[dict]], Awaitable[CapabilitySet]],
    ) -> CapabilitySet:
        cached = self._runtime._cached_capabilities(
            self._cache_key, model_id, tool_ids, skill_ids,
        )
        if cached is not None:
            return cached
        capabilities = await load(self.messages)
        self._runtime._cache_capabilities(self._cache_key, model_id, capabilities)
        return capabilities


class SkillLoaderOwnership:
    """Inspect ownership before removing either the loader or its evidence."""

    def __init__(self, metadata: dict):
        self.record = metadata.get("lite_skill_loader")
        tools = metadata.get("tools")
        self.current = tools.get("view_skill") if isinstance(tools, dict) else None
        self.owns_current = (
            isinstance(self.record, dict) and isinstance(self.current, dict)
            and self.current.get("callable") is self.record.get("callable")
            and self.current.get("spec") == self.record.get("spec")
        )

    def owns_schema(self, schema: dict) -> bool:
        return (
            isinstance(self.record, dict) and (self.current is None or self.owns_current)
            and schema.get("function") == self.record.get("spec")
        )

    def remove(self, body: dict) -> None:
        metadata = body["metadata"]
        if self.owns_current:
            metadata["tools"].pop("view_skill", None)
        if "tools" in body:
            body["tools"] = [schema for schema in body["tools"] or [] if not self.owns_schema(schema)]
        metadata.pop("lite_skill_loader", None)


class RequestRuntime:
    """Own the shared, request-scoped metadata and live Tool registries."""

    RESET_FIELDS = (
        "lite_history_boundary", "lite_child_messages", "lite_base_messages", "lite_router_user_index",
        "lite_active_handoff", "lite_active_agent_id", "lite_active_skill_id",
        "lite_active_model_id", "lite_active_tool_runtime", "lite_base_tool_runtime",
        "lite_orchestrator_skill_context", "lite_unfiltered_messages",
        "lite_router_filter_pipeline", "lite_router_request_key", "lite_context_filter_request_key",
        "previous_tool_context_applied", "history_cleanup_applied",
        "tool_call_filter_applied", "subagent_context_applied", "skill_context_applied",
        "lite_subagent_filter_pipeline", "lite_subagent_filter_run",
        "lite_target_agent_id", "lite_target_model_id", "lite_target_skill_ids",
        "lite_view_skill_available", "lite_view_skill_model_id", "lite_skill_loader",
    )
    CONFIG_FIELDS = (
        "lite_agents", "lite_router_model_id", "lite_router_owner_id",
        "lite_base_tool_ids", "lite_orchestrator_skill_ids", "lite_registry_applied",
    )
    MANAGED_FIELDS = RESET_FIELDS + CONFIG_FIELDS + ("tools", "tool_ids", "skill_ids")
    CAPABILITY_FIELDS = {
        "child": ("lite_active_tool_runtime", "lite_child_messages"),
        "orchestrator": ("lite_base_tool_runtime", "lite_base_messages"),
    }
    ROUTER_FILTERS = {
        "lite_registry": "Lite Subagent Registry",
        "previous_tool_context": "Previous Tool Context",
        "history_cleanup": "History Cleanup",
    }
    CHILD_FILTERS = {
        "tool_call_filter": "Tool Call Filter",
        "subagent_context": "Subagent Context",
        "skill_context": "Skill Context",
    }

    def __init__(self, request, metadata: dict):
        if not isinstance(metadata, dict):
            raise TypeError("Lite Router metadata must be an object")
        self.request = request
        self.metadata = metadata

    @staticmethod
    def copy_body(body: dict) -> dict:
        metadata = body.get("metadata") if isinstance(body, dict) else None
        memo = {id(metadata): metadata} if isinstance(metadata, dict) else {}
        return copy.deepcopy(body, memo)

    def routed_body(self, body: dict) -> dict:
        routed = self.copy_body(body)
        routed["metadata"] = self.metadata
        return routed

    @property
    def request_metadata(self) -> dict | None:
        state = getattr(self.request, "state", None)
        metadata = getattr(state, "metadata", None)
        return metadata if isinstance(metadata, dict) else None

    def sync(self, **values) -> None:
        self.metadata.update(values)
        self.publish()

    def publish(self) -> None:
        """Mirror only project-managed fields, including their absence."""
        request_metadata = self.request_metadata
        if request_metadata is not None and request_metadata is not self.metadata:
            for key in self.MANAGED_FIELDS:
                if key in self.metadata:
                    request_metadata[key] = self.metadata[key]
                else:
                    request_metadata.pop(key, None)

    def discard(self, *keys) -> None:
        for key in keys:
            self.metadata.pop(key, None)
        self.publish()

    def start_request(self, body: dict, **configuration) -> None:
        self.bind_context_request()
        request_key = self.router_request_key()
        pipeline = self.metadata.get("lite_router_filter_pipeline")
        pipeline = pipeline if isinstance(pipeline, list) else []
        preceding = [
            label for name, label in self.ROUTER_FILTERS.items()
            if name != "lite_registry" and (name in pipeline or self.metadata.get(name + "_applied"))
        ]
        context_request_key = self.metadata.get(
            "lite_context_filter_request_key", self.metadata.get("lite_router_request_key"),
        )
        if preceding and (context_request_key is None or context_request_key == request_key):
            raise ValueError("Lite Subagent Registry must run before " + " and ".join(preceding))
        body["metadata"] = self.metadata
        SkillLoaderOwnership(self.metadata).remove(body)
        self.discard(*self.RESET_FIELDS)
        self.shared_tools()
        self.sync(**configuration, lite_router_filter_pipeline=["lite_registry"], lite_router_request_key=request_key)

    def router_request_key(self) -> dict:
        """Recognize Registry re-entry without deriving identity from message text."""
        if self.metadata.get("message_id"):
            return {"chat_id": self.metadata.get("chat_id"), "message_id": self.metadata["message_id"]}
        scope = getattr(self.request, "scope", None)
        if isinstance(scope, dict):
            # Request wrappers over the same ASGI scope share one transport identity.
            request_id = scope.setdefault("lite_router_request_id", uuid4().hex)
        else:
            # Lightweight request adapters need not implement the ASGI scope.
            request_id = getattr(self.request, "_lite_router_request_id", None)
            if request_id is None:
                request_id = uuid4().hex
                self.request._lite_router_request_id = request_id
        return {"request_id": request_id}

    @staticmethod
    def is_tool_image_message(message: dict) -> bool:
        content = message.get("content")
        return (
            message.get("role") == "user"
            and isinstance(content, list) and len(content) > 1
            and isinstance(content[0], dict) and content[0].get("type") == "text"
            and content[0].get("text") == "Here are the images from the tool results above. Please analyze them."
            and all(isinstance(part, dict) and part.get("type") == "image_url" for part in content[1:])
        )

    def bind_context_request(self) -> None:
        """Bind observed inlet evidence before the reversible preparation starts."""
        if (
            "lite_context_filter_request_key" in self.metadata
            and self.metadata["lite_context_filter_request_key"] is None
        ):
            self.sync(lite_context_filter_request_key=self.router_request_key())

    def require_router_chain(self) -> None:
        guidance = "; attach Router Preparation to the Router Workspace Model"
        pipeline = self.metadata.get("lite_router_filter_pipeline")
        pipeline = pipeline if isinstance(pipeline, list) else []
        missing = [label for name, label in self.ROUTER_FILTERS.items() if name not in pipeline]
        if missing:
            raise ValueError("Required Router filters are missing or out of order: " + ", ".join(missing) + guidance)
        if pipeline != list(self.ROUTER_FILTERS):
            raise ValueError(
                "Router filters ran in the wrong order; required: " + " -> ".join(self.ROUTER_FILTERS.values()) + guidance
            )
        if self.metadata.get("lite_router_request_key") != self.router_request_key():
            raise ValueError("Lite Subagent Registry must run for the current request before Router dispatch" + guidance)

    @contextmanager
    def preparation(self) -> Iterator[RequestRuntime]:
        """Commit on success; restore bounded local state on any preparation error.

        Keep live registry/history identities and callable/client references. External
        resources (notably mcp_clients) are deliberately outside this checkpoint.
        """
        saved = {key: self.metadata[key] for key in self.MANAGED_FIELDS if key in self.metadata}
        contents: dict[str, Any] = {}
        for key, value in saved.items():
            if isinstance(value, list):
                contents[key] = copy.deepcopy(value)
            elif isinstance(value, dict):
                contents[key] = dict(value)
        try:
            yield self
        except BaseException:
            for key in self.MANAGED_FIELDS:
                if key not in saved:
                    self.metadata.pop(key, None)
                    continue
                value = saved[key]
                if isinstance(value, list):
                    value[:] = contents[key]
                elif isinstance(value, dict):
                    value.clear()
                    value.update(contents[key])
                self.metadata[key] = value
            self.publish()
            raise
        else:
            self.publish()

    @contextmanager
    def prepare_model(self, branch: str, body: dict) -> Iterator[ModelPreparation]:
        """Publish final Tool context only when the whole model draft succeeds."""
        with self.preparation():
            prepared = ModelPreparation(self, branch, body)
            yield prepared
            if not isinstance(prepared.body, dict) or not isinstance(prepared.body.get("messages"), list):
                raise TypeError("Model preparation returned an invalid request body")
            prepared.body["metadata"] = self.metadata
            prepared.messages[:] = copy.deepcopy(prepared.body["messages"])

    @contextmanager
    def child_filters(self) -> Iterator[None]:
        self.discard(*(name + "_applied" for name in self.CHILD_FILTERS))
        self.sync(lite_subagent_filter_pipeline=[], lite_subagent_filter_run=True)
        try:
            yield
        finally:
            self.discard("lite_subagent_filter_run")
        missing = [label for name, label in self.CHILD_FILTERS.items() if not self.metadata.get(name + "_applied")]
        if missing:
            raise ValueError(
                "Required subagent filters are not attached to the destination model: " + ", ".join(missing)
            )
        actual_order = self.metadata.get("lite_subagent_filter_pipeline")
        if actual_order != list(self.CHILD_FILTERS):
            raise ValueError(
                "Subagent filters ran in the wrong order: " + " -> ".join(str(item) for item in actual_order or [])
            )

    def before_filter(self, name: str) -> None:
        if name in self.ROUTER_FILTERS:
            if self.metadata.get("lite_subagent_filter_run"):
                return
            # Standalone filtering also belongs to a request, even without Registry.
            context_request_key = (
                self.router_request_key()
                if self.request is not None or self.metadata.get("message_id") else None
            )
            self.sync(lite_context_filter_request_key=context_request_key)
            if not (
                self.metadata.get("lite_registry_applied") or "lite_router_filter_pipeline" in self.metadata
            ):
                return
            sequence = list(self.ROUTER_FILTERS)
            prior = sequence[:sequence.index(name)]
            pipeline = self.metadata.get("lite_router_filter_pipeline")
            pipeline = pipeline if isinstance(pipeline, list) else []
            if pipeline != prior:
                # Keep the rejected order invalid even if a caller continues after the error.
                self.sync(lite_router_filter_pipeline=[*pipeline, name])
                required = " and ".join(self.ROUTER_FILTERS[item] for item in prior)
                raise ValueError(f"{required} must run before {self.ROUTER_FILTERS[name]} in the Router inlet chain")
            return
        if not self.metadata.get("lite_subagent_filter_run"):
            return
        sequence = list(self.CHILD_FILTERS)
        prior = sequence[:sequence.index(name)]
        pipeline = self.metadata.get("lite_subagent_filter_pipeline") or []
        if prior and pipeline[-len(prior):] != prior:
            required = " and ".join(self.CHILD_FILTERS[item] for item in prior)
            raise ValueError(f"{required} must run before {self.CHILD_FILTERS[name]}")

    def finish_filter(self, name: str, **values) -> None:
        self.metadata.update(values)
        self.metadata[name + "_applied"] = True
        if name in self.CHILD_FILTERS and self.metadata.get("lite_subagent_filter_run"):
            self.metadata.setdefault("lite_subagent_filter_pipeline", []).append(name)
        if (
            name in self.ROUTER_FILTERS and self.metadata.get("lite_registry_applied")
            and not self.metadata.get("lite_subagent_filter_run")
        ):
            self.metadata.setdefault("lite_router_filter_pipeline", []).append(name)
        self.publish()

    def shared_tools(self) -> dict:
        tools = self.metadata.get("tools")
        if not isinstance(tools, dict):
            tools = {}
            self.sync(tools=tools)
        return tools

    def bind_tools(self, tools: dict, tool_ids: list[str], *, replace: bool) -> dict:
        shared = self.shared_tools()
        if replace:
            shared.clear()
        shared.update(tools)
        self.sync(tools=shared, tool_ids=list(tool_ids))
        return shared

    def _cached_capabilities(
        self,
        cache_key: str,
        model_id: str,
        tool_ids: list[str],
        skill_ids: list[str],
    ) -> CapabilitySet | None:
        cache = self.metadata.get(cache_key)
        if (
            isinstance(cache, dict)
            and cache.get("model_id") == model_id
            and cache.get("tool_ids") == tool_ids
            and cache.get("skill_ids") == skill_ids
            and isinstance(cache.get("tools"), dict)
        ):
            return CapabilitySet(
                tool_ids=list(tool_ids),
                skill_ids=list(skill_ids),
                tools=cache["tools"],
            )
        return None

    def _cache_capabilities(
        self,
        cache_key: str,
        model_id: str,
        capabilities: CapabilitySet,
    ) -> None:
        self.sync(
            **{
                cache_key: {
                    "model_id": model_id,
                    "tool_ids": list(capabilities.tool_ids),
                    "skill_ids": list(capabilities.skill_ids),
                    "tools": capabilities.tools,
                }
            }
        )

    def activate(self, marker: Any, agent_id: str, model_id: str) -> None:
        self.sync(
            lite_active_agent_id=agent_id,
            lite_active_model_id=model_id,
            lite_active_handoff=marker.to_dict(),
        )
# END GENERATED REQUEST RUNTIME

# BEGIN GENERATED TOOL HISTORY
# Edit handoff_router/shared/tool_history.py; run python3 handoff_router/tools/generate_skill_preparation.py
"""Interpret completed Tool history before consumers select or format it."""

import json
from dataclasses import dataclass
from typing import Any



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
# END GENERATED TOOL HISTORY

# BEGIN GENERATED TOOL CONTEXT
# Edit handoff_router/shared/tool_context.py; run python3 handoff_router/tools/generate_skill_preparation.py
"""Select and reconstruct Tool context for independently uploaded Filters."""

import copy
from dataclasses import dataclass



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
# END GENERATED TOOL CONTEXT

# BEGIN GENERATED SKILL PREPARATION
# Edit handoff_router/shared/skill_preparation.py; run python3 handoff_router/tools/generate_skill_preparation.py
"""Authoritative Skill preparation, embedded into independently uploaded Functions."""

import copy
import html
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal



def normalize_skill_ids(values) -> list[str]:
    result = []
    seen = set()
    for raw_value in values or []:
        value = str(raw_value or "").strip().lower()
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


@dataclass(frozen=True, kw_only=True)
class SkillBuiltinInvocation:
    profile: Literal["orchestrator", "child", "standalone"]
    request: Any
    runtime_model: dict
    metadata: dict
    event_emitter: Any = None
    event_call: Any = None
    oauth_token: Any = None
    messages: list[dict] | None = None
    files: Any = None


class BuiltinSkillLoader:
    """Load fresh OWUI builtins behind SkillPreparation's load_builtin seam.

    The caller selects the user; resolve_user runs only when load is called.
    Router profiles require their stable history list and history adapter.
    Standalone retains its smaller injection set and OWUI's native binding.
    This module neither checks Skill eligibility nor caches loaded Tools.
    """

    def __init__(
        self,
        *,
        invocation: SkillBuiltinInvocation,
        get_builtin_tools: Callable[..., Awaitable[dict]],
        resolve_user: Callable[[], Awaitable[dict]],
        bind_history: Callable[[dict, list[dict]], dict] | None = None,
    ):
        if invocation.profile not in ("orchestrator", "child", "standalone"):
            raise ValueError(f"Unknown builtin Skill profile: {invocation.profile}")
        if invocation.profile == "standalone":
            if bind_history is not None:
                raise ValueError("Standalone Skill loading cannot bind Router history")
        elif invocation.messages is None or bind_history is None:
            raise ValueError("Router Skill loading requires Tool history and its adapter")
        self._invocation = invocation
        self._get_builtin_tools = get_builtin_tools
        self._resolve_user = resolve_user
        self._bind_history = bind_history

    async def load(self, skill_ids: list[str]) -> dict:
        invocation = self._invocation
        metadata = invocation.metadata
        user = await self._resolve_user()
        extra_params = {
            "__user__": user,
            "__metadata__": metadata,
            "__model__": invocation.runtime_model,
            "__event_emitter__": invocation.event_emitter,
            "__event_call__": invocation.event_call,
            "__oauth_token__": invocation.oauth_token,
            "__chat_id__": metadata.get("chat_id"),
            "__message_id__": metadata.get("message_id"),
            "__skill_ids__": skill_ids,
        }
        options = {"model": invocation.runtime_model}
        if invocation.profile != "orchestrator":
            options["features"] = metadata.get("features", {})
        if invocation.profile != "standalone":
            extra_params.update({
                "__request__": invocation.request,
                "__session_id__": metadata.get("session_id"),
                "__messages__": invocation.messages,
                "__files__": invocation.files or metadata.get("files", []),
                "__features__": metadata.get("features", {}),
            })
        tools = await self._get_builtin_tools(invocation.request, extra_params, **options)
        if self._bind_history is not None:
            # Constructor validation guarantees history for the Router profiles.
            assert invocation.messages is not None
            tools = self._bind_history(tools, invocation.messages)
        return tools


@dataclass(frozen=True)
class PreparedSkills:
    ids: list[str]
    context: str
    loader: dict | None


class SkillPreparation:
    CONTEXT_PREFIX = "Skill context:\n"
    LEGACY_CONTEXT_PREFIX = "Lite orchestrator Skill context:\n"

    @staticmethod
    def install_context(prepared: PreparedSkills, body: dict, runtime_model: dict) -> None:
        """Replace the managed Skill context while preserving administrator instructions."""
        messages = SkillPreparation._remove_previous_context(body["messages"])
        SkillPreparation.install_loader(prepared, body, runtime_model)
        metadata = body["metadata"]
        metadata.setdefault("lite_view_skill_available", False)
        metadata.setdefault("lite_view_skill_model_id", None)
        prompt = prepared.context
        if prepared.loader is not None:
            prompt = (
                "The following Skills are available on demand. Inspect their descriptions "
                "and call view_skill for any Skill that may apply before following its full "
                "instructions.\n\n" + prompt
            )
        body["messages"] = SkillPreparation._append_system_context(messages, prompt)

    @staticmethod
    def _remove_previous_context(messages: list[dict]) -> list[dict]:
        cleaned = []
        for original in messages:
            content = original.get("content")
            if original.get("role") != "system" or not isinstance(content, str):
                cleaned.append(original)
                continue
            before = None
            for prefix in (SkillPreparation.CONTEXT_PREFIX, SkillPreparation.LEGACY_CONTEXT_PREFIX):
                if content.startswith(prefix):
                    before = ""
                    break
                separator = "\n\n" + prefix
                if separator in content:
                    before = content.rsplit(separator, 1)[0].rstrip()
                    break
            if before is None:
                cleaned.append(original)
                continue
            if before:
                message = dict(original)
                message["content"] = before
                cleaned.append(message)
        return cleaned

    @staticmethod
    def _append_system_context(messages: list[dict], prompt: str) -> list[dict]:
        if not prompt:
            return messages
        block = SkillPreparation.CONTEXT_PREFIX + prompt
        for index, original in enumerate(messages):
            if original.get("role") == "system":
                message = dict(original)
                content = str(message.get("content") or "").rstrip()
                message["content"] = f"{content}\n\n{block}" if content else block
                messages[index] = message
                return messages
        messages.insert(0, {"role": "system", "content": block})
        return messages

    @staticmethod
    def install_loader(prepared: PreparedSkills, body: dict, runtime_model: dict) -> None:
        metadata = body["metadata"]
        tools = metadata.get("tools")
        if not isinstance(tools, dict):
            tools = {}
            metadata["tools"] = tools
        ownership = SkillLoaderOwnership(metadata)
        schemas = list(body.get("tools") or [])

        if prepared.loader is not None and (
            (ownership.current is not None and not ownership.owns_current)
            or any(
                (schema.get("function") or {}).get("name") == "view_skill"
                and not ownership.owns_schema(schema)
                for schema in schemas
            )
        ):
            raise ValueError('Attached Tool name "view_skill" conflicts with the builtin Skill loader')
        ownership.remove(body)
        schemas = list(body.get("tools") or [])
        if prepared.loader is not None:
            tools["view_skill"] = prepared.loader
            schemas.append({"type": "function", "function": prepared.loader["spec"]})
            metadata["lite_skill_loader"] = {
                "callable": prepared.loader["callable"], "spec": copy.deepcopy(prepared.loader["spec"]),
            }
        if schemas or "tools" in body:
            body["tools"] = schemas
        if prepared.ids or ownership.record is not None or "lite_view_skill_available" in metadata:
            metadata["lite_view_skill_available"] = prepared.loader is not None
            metadata["lite_view_skill_model_id"] = runtime_model.get("id") if prepared.loader is not None else None

    @staticmethod
    async def prepare(
        *, skill_ids, runtime_model: dict, metadata: dict, lookup_skill, load_builtin,
    ) -> PreparedSkills:
        ids = normalize_skill_ids(skill_ids)
        skills: list[tuple[str, Any]] = []
        missing = []
        for skill_id in ids:
            skill = await lookup_skill(skill_id)
            if skill is None or not skill.is_active:
                missing.append(skill_id)
            else:
                skills.append((skill_id, skill))
        if missing:
            raise ValueError("Attached model Skills are unavailable: " + ", ".join(missing))

        meta = (runtime_model.get("info") or {}).get("meta") or {}
        eligible = (
            bool(metadata.get("session_id"))
            and (metadata.get("params") or {}).get("function_calling") != "legacy"
            and (meta.get("capabilities") or {}).get("builtin_tools", True) is not False
        )
        builtin = (await load_builtin(ids)).get("view_skill") if ids and eligible else None
        loader = None
        if builtin is not None:
            allowed = frozenset(ids)
            builtin_callable = builtin["callable"]

            async def allowlisted_view_skill(id: str):
                requested_id = next(iter(normalize_skill_ids([id])), "")
                if requested_id not in allowed:
                    return '{"error":"Skill is not available in the current model context"}'
                return await builtin_callable(id=requested_id)

            loader = {**builtin, "callable": allowlisted_view_skill}

        entries = []
        for skill_id, skill in skills:
            name = str(skill.name or skill_id)
            if loader is not None:
                entries.append(
                    "<skill>\n"
                    f"<id>{html.escape(skill_id)}</id>\n"
                    f"<name>{html.escape(name)}</name>\n"
                    f"<description>{html.escape(str(skill.description or ''))}</description>\n"
                    "</skill>"
                )
            else:
                entries.append(
                    f'<skill id="{html.escape(skill_id, quote=True)}" '
                    f'name="{html.escape(name, quote=True)}">\n'
                    f'{skill.content}\n</skill>'
                )
        context = "\n\n".join(entries)
        if loader is not None:
            context = "<available_skills>\n" + "\n".join(entries) + "\n</available_skills>"
        return PreparedSkills(ids, context, loader)
# END GENERATED SKILL PREPARATION

log = logging.getLogger(__name__)


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=-30, description="Order relative to additional destination inlet filters.")
        history_turns: int = Field(
            default=0, ge=0,
            description="Completed previous user/assistant turns to retain for every attached subagent.",
        )
        history_tool_calls: int = Field(
            default=0, ge=0,
            description="Maximum completed Tool call occurrences inside retained previous turns; repeated IDs count separately.",
        )
        debug: bool = Field(default=False, description="Enable debug logs.")

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(
        self, body: dict, __request__=None, __model__=None,
        __prepared_skills__: PreparedSkills | None = None,
    ) -> dict:
        metadata = body.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise TypeError("Subagent Preparation metadata must be an object")
        messages = body.get("messages")
        if not isinstance(messages, list):
            raise TypeError("Subagent Preparation messages must be a list")
        if __request__ is None or not metadata.get("lite_subagent_filter_run") or __prepared_skills__ is None:
            raise ValueError("Subagent Preparation requires Router destination inlet preparation")
        runtime = RequestRuntime(__request__, metadata)

        runtime.before_filter("tool_call_filter")
        projection = ToolContextProjection.available_tools(body)
        body["messages"] = projection.messages
        runtime.finish_filter("tool_call_filter")

        runtime.before_filter("subagent_context")
        body["messages"] = ToolContextProjection.completed_history(
            body["messages"], history_turns=self.valves.history_turns,
            history_tool_calls=self.valves.history_tool_calls,
        )
        runtime.finish_filter("subagent_context")

        runtime.before_filter("skill_context")
        model_id = str(body.get("model") or "").strip()
        runtime_model = (
            __model__ if isinstance(__model__, dict)
            else __request__.app.state.MODELS.get(model_id) or {"id": model_id}
        )
        SkillPreparation.install_context(__prepared_skills__, body, runtime_model)
        runtime.finish_filter("skill_context")
        if self.valves.debug:
            log.warning(
                "[SUBAGENT_PREPARATION] model=%s allowed=%s turns=%s tools=%s messages before=%s after=%s",
                model_id, sorted(projection.allowed_tool_names), self.valves.history_turns,
                self.valves.history_tool_calls, len(messages), len(body["messages"]),
            )
        return body
