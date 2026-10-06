"""
title: Lite Handoff Router
description: Stateless same-response subagent handoff router.
version: 0.22.0
required_open_webui_version: 0.11.1
"""

from __future__ import annotations

import copy
import html
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, get_type_hints

from fastapi import HTTPException
from open_webui.models.models import Models
from open_webui.models.skills import Skills
from open_webui.models.users import Users
from open_webui.utils.chat import generate_chat_completion
from open_webui.utils.filter import get_filter_functions, process_filter_functions
from open_webui.utils.misc import remove_system_message
from open_webui.utils.tools import get_attached_knowledge, get_builtin_tools, get_tools
from pydantic import BaseModel, Field
from starlette.responses import Response, StreamingResponse

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
        "lite_subagent_filter_run",
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

    def select_model(self, model_id: str) -> None:
        """Give Tools the inference model ID, preserving Workspace dispatch."""
        self.metadata["model_id"] = model_id
        request_metadata = self.request_metadata
        if request_metadata is not None and request_metadata is not self.metadata:
            request_metadata["model_id"] = model_id

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
        missing = object()
        model_ids = [(self.metadata, self.metadata.get("model_id", missing))]
        request_metadata = self.request_metadata
        if request_metadata is not None and request_metadata is not self.metadata:
            model_ids.append((request_metadata, request_metadata.get("model_id", missing)))
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
            for metadata, model_id in model_ids:
                if model_id is missing:
                    metadata.pop("model_id", None)
                else:
                    metadata["model_id"] = model_id
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
            # Cached OWUI Tool wrappers can retain metadata from an earlier
            # dispatch. Publish the selected identity only after preparation.
            if "model_id" in self.metadata:
                for tool in self.shared_tools().values():
                    if not isinstance(tool, dict):
                        continue
                    injections = getattr(tool.get("callable"), "__extra_params__", None)
                    metadata = injections.get("__metadata__") if isinstance(injections, dict) else None
                    if isinstance(metadata, dict):
                        metadata["model_id"] = self.metadata["model_id"]

    @contextmanager
    def child_filters(self) -> Iterator[None]:
        """Keep destination inlets from changing the Router's preparation evidence."""
        self.sync(lite_subagent_filter_run=True)
        try:
            yield
        finally:
            self.discard("lite_subagent_filter_run")

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

    def finish_filter(self, name: str, **values) -> None:
        self.metadata.update(values)
        self.metadata[name + "_applied"] = True
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
"""Select and reconstruct Tool context for independently uploaded Functions."""

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

        Preparation supplies a valid body. Read its Tool schemas and
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

        Counts are nonnegative, as enforced by the caller's Valves. This operation
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
        loader = (await load_builtin(ids)).get("view_skill") if ids and eligible else None

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

DELEGATE_VERSION = "v2"
ACTIVE_HANDOFF_KEY = "lite_active_handoff"
ORCHESTRATOR_SKILL_PROMPT_PREFIX = "Lite orchestrator Skill context:\n"
GENERIC_SKILL_PROMPT_PREFIX = "Skill context:\n"
TOOL_IMAGE_TEXT = "Here are the images from the tool results above. Please analyze them."
CHILD_REQUEST_ENVELOPE_KEYS = frozenset(
    {
        "model",
        "messages",
        "stream",
        "stream_options",
        "metadata",
    }
)
BLOCKED_CHILD_BUILTIN_TOOLS = frozenset({"delegate_task", "timer"})


def normalize_ids(values) -> list[str]:
    result = []
    seen = set()
    for raw_value in values or []:
        value = str(raw_value or "").strip()
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


@dataclass(frozen=True)
class HandoffMarker:
    agent_id: str

    @classmethod
    def parse(cls, value: Any) -> HandoffMarker | None:
        agent_id = parse_handoff(value)
        return cls(agent_id=agent_id) if agent_id is not None else None

    def to_dict(self) -> dict:
        return {
            "__lite_delegate__": DELEGATE_VERSION,
            "agent_id": self.agent_id,
        }


@dataclass(frozen=True)
class AgentSpec:
    model_id: str
    name: str
    routing_skill_id: str = ""


@dataclass(frozen=True)
class InvocationContext:
    request: Any
    user: Any
    event_emitter: Any = None
    event_call: Any = None
    oauth_token: Any = None
    files: Any = None


class HandoffProtocol:
    @staticmethod
    def find_current(messages: list[dict]) -> HandoffMarker | None:
        agent_id = analyze_history(messages).current_handoff
        return HandoffMarker(agent_id) if agent_id is not None else None

    @staticmethod
    def active(metadata: dict) -> HandoffMarker | None:
        return HandoffMarker.parse(metadata.get(ACTIVE_HANDOFF_KEY))

class MessageHistory:
    is_tool_image_message = staticmethod(RequestRuntime.is_tool_image_message)

    @classmethod
    def last_user_index(cls, messages: list[dict]) -> int:
        return next(
            (index for index in range(len(messages) - 1, -1, -1)
             if messages[index].get("role") == "user"
             and not cls.is_tool_image_message(messages[index])),
            -1,
        )


class McpRuntime:
    @staticmethod
    async def connect(request, server_id, user, metadata, extra_params):
        from open_webui.utils.middleware import connect_mcp_server

        return await connect_mcp_server(request, server_id, user, metadata, extra_params)

    @staticmethod
    def register_client(metadata: dict, server_id: str, client) -> None:
        clients = metadata.get("mcp_clients")
        if not isinstance(clients, dict):
            clients = {}
            metadata["mcp_clients"] = clients

        key = server_id
        suffix = 2
        while key in clients and clients[key] is not client:
            key = f"{server_id}#{suffix}"
            suffix += 1
        clients[key] = client

    @staticmethod
    def tool_callable(client, function_name: str):
        async def tool_function(**kwargs):
            return await client.call_tool(function_name, function_args=kwargs)

        return tool_function

    async def resolve(
        self,
        *,
        tool_ids: list[str],
        tools: dict,
        owner,
        metadata: dict,
        extra_params: dict,
        request,
        connector: Callable,
    ) -> dict:
        for tool_id in tool_ids:
            server_id = tool_id.removeprefix("server:mcp:").strip()
            if not server_id:
                continue
            try:
                result = await connector(request, server_id, owner, metadata, extra_params)
            except Exception as exc:
                raise RuntimeError(
                    f'Attached MCP Tool Server "{server_id}" could not connect: {exc}'
                ) from exc
            if result is None:
                continue

            client, tool_specs = result
            self.register_client(metadata, server_id, client)
            self._add_specs(tools, tool_id, server_id, client, tool_specs or [])
        return tools

    def _add_specs(self, tools, tool_id, server_id, client, tool_specs) -> None:
        for tool_spec in tool_specs:
            if not isinstance(tool_spec, dict):
                continue
            original_name = str(tool_spec.get("name") or "").strip()
            if not original_name:
                continue
            base_name = f"{server_id}_{original_name}"
            unique_name = base_name
            suffix = 2
            while unique_name in tools:
                unique_name = f"{base_name}_{suffix}"
                suffix += 1
            tools[unique_name] = {
                "tool_id": tool_id,
                "spec": {**tool_spec, "name": unique_name},
                "callable": self.tool_callable(client, original_name),
                "type": "mcp",
                "client": client,
                "direct": False,
            }


class ModelCapabilityResolver:
    MCP_PREFIX = "server:mcp:"
    SKILL_TOOL_NAME = "view_skill"

    def __init__(self, mcp_runtime: McpRuntime):
        self.mcp_runtime = mcp_runtime

    @staticmethod
    def bind_history(tools: dict, messages: list[dict]) -> dict:
        """Keep Router history when OWUI refreshes callable extra parameters.

        OWUI's native Tool loop injects its outer request history, including on
        nested Router continuations. Bind only __messages__ at the original
        function; retain OWUI's wrapper convention so __files__ still refreshes.
        Custom closures already capture the live list supplied to their loader.
        """
        bound_tools = {}
        for name, tool in tools.items():
            loaded = tool["callable"]
            original = getattr(loaded, "__function__", None)
            extra_params = getattr(loaded, "__extra_params__", None)
            if (
                not callable(original) or not isinstance(extra_params, dict)
                or "__messages__" not in inspect.signature(original).parameters
            ):
                bound_tools[name] = tool
                continue

            # Each closure must bind its own original function and history.
            def bind(loaded, original, extra_params):
                @wraps(original, updated=())
                async def with_history(*args, **kwargs):
                    kwargs["__messages__"] = messages
                    result = original(*args, **kwargs)
                    return await result if inspect.isawaitable(result) else result

                with_history.__signature__ = inspect.signature(original)
                try:
                    with_history.__annotations__ = get_type_hints(original)
                except Exception:  # Match OWUI's fallback for unresolved Tool annotations.
                    pass

                @wraps(loaded, updated=())
                async def bound(*args, **kwargs):
                    result = loaded(*args, **{**kwargs, "__messages__": messages})
                    return await result if inspect.isawaitable(result) else result

                bound.__signature__ = inspect.signature(loaded)
                bound.__function__ = with_history
                bound.__extra_params__ = {**extra_params, "__messages__": messages}
                return bound

            bound_tools[name] = {**tool, "callable": bind(loaded, original, extra_params)}
        return bound_tools

    @staticmethod
    def builtin_tools_enabled(runtime_model: dict) -> bool:
        meta = (runtime_model or {}).get("info", {}).get("meta", {}) or {}
        return (meta.get("capabilities") or {}).get("builtin_tools", True) is not False

    @staticmethod
    def knowledge_context(runtime_model: dict, metadata: dict) -> str:
        meta = (runtime_model or {}).get("info", {}).get("meta", {}) or {}
        if (meta.get("capabilities") or {}).get("builtin_tools", True) is False:
            return ""
        if not isinstance(metadata, dict) or not metadata.get("session_id"):
            return ""
        if (metadata.get("params") or {}).get("function_calling") == "legacy":
            return ""
        if (meta.get("builtinTools") or {}).get("knowledge", True) is False:
            return ""
        entries = []
        for item in get_attached_knowledge(runtime_model, metadata) or []:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "").strip()
            item_type = str(item.get("type") or "").strip()
            if not item_id or not item_type:
                continue
            attrs = [
                f'type="{html.escape(item_type, quote=True)}"',
                f'id="{html.escape(item_id, quote=True)}"',
            ]
            for key in ("name", "source"):
                value = str(item.get(key) or "").strip()
                if value:
                    attrs.append(f'{key}="{html.escape(value, quote=True)}"')
            entries.append("<knowledge " + " ".join(attrs) + "/>")
        if not entries:
            return ""
        return "<attached_knowledge>\n" + "\n".join(entries) + "\n</attached_knowledge>"

    async def resolve_general_builtin_tools(
        self,
        *,
        request,
        execution_user,
        runtime_model,
        metadata,
        extra_params: dict,
    ) -> dict:
        if not self.builtin_tools_enabled(runtime_model):
            return {}
        if not isinstance(metadata, dict) or not metadata.get("session_id"):
            return {}
        if (metadata.get("params") or {}).get("function_calling") == "legacy":
            return {}
        features = metadata.get("features") if isinstance(metadata, dict) else None
        builtin_tools = await get_builtin_tools(
            request,
            {**extra_params, "__user__": execution_user.model_dump(), "__skill_ids__": []},
            features=features or {},
            model=runtime_model,
        )
        return {
            name: tool
            for name, tool in builtin_tools.items()
            if name not in BLOCKED_CHILD_BUILTIN_TOOLS and name != self.SKILL_TOOL_NAME
        }

    async def resolve(
        self,
        *,
        request,
        capability_owner_id: str,
        execution_user,
        tool_ids,
        skill_ids,
        runtime_model,
        metadata,
        messages,
        event_emitter,
        event_call,
        oauth_token,
        files,
        connector: Callable,
        include_builtin_tools: bool = False,
    ) -> CapabilitySet:
        requested_ids = normalize_ids(tool_ids)
        requested_skill_ids = normalize_skill_ids(skill_ids)
        if not requested_ids and not requested_skill_ids and not include_builtin_tools:
            return CapabilitySet([], [], {})

        owner = None
        if requested_ids:
            owner = await Users.get_user_by_id(capability_owner_id)
            if owner is None:
                raise ValueError("Model capability owner is unavailable")

        mcp_ids = [item for item in requested_ids if item.startswith(self.MCP_PREFIX)]
        regular_ids = [item for item in requested_ids if not item.startswith(self.MCP_PREFIX)]
        extra_params = self._extra_params(
            request=request,
            execution_user=execution_user,
            runtime_model=runtime_model,
            metadata=metadata,
            messages=messages,
            event_emitter=event_emitter,
            event_call=event_call,
            oauth_token=oauth_token,
            files=files,
        )
        tools = (
            await get_tools(request, regular_ids, owner, extra_params)
            if regular_ids
            else {}
        )
        tools = await self.mcp_runtime.resolve(
            tool_ids=mcp_ids,
            tools=tools,
            owner=owner,
            metadata=metadata,
            extra_params=extra_params,
            request=request,
            connector=connector,
        )
        resolved_ids = {str(tool.get("tool_id") or "") for tool in tools.values()}
        missing = [tool_id for tool_id in requested_ids if tool_id not in resolved_ids]
        if missing:
            raise ValueError(
                "Attached model-bound Tools are unavailable: " + ", ".join(missing)
            )

        if include_builtin_tools:
            builtin_tools = await self.resolve_general_builtin_tools(
                request=request,
                execution_user=execution_user,
                runtime_model=runtime_model,
                metadata=metadata,
                extra_params=extra_params,
            )
            for name, tool in builtin_tools.items():
                if name not in tools:
                    tools[name] = tool

        return CapabilitySet(requested_ids, requested_skill_ids, self.bind_history(tools, messages))

    @staticmethod
    def _extra_params(
        *,
        request,
        execution_user,
        runtime_model,
        metadata,
        messages,
        event_emitter,
        event_call,
        oauth_token,
        files,
    ) -> dict:
        return {
            "__event_emitter__": event_emitter,
            "__event_call__": event_call,
            "__user__": execution_user.model_dump(),
            "__metadata__": metadata,
            "__oauth_token__": oauth_token,
            "__request__": request,
            "__model__": runtime_model,
            "__chat_id__": metadata.get("chat_id"),
            "__session_id__": metadata.get("session_id"),
            "__message_id__": metadata.get("message_id"),
            "__messages__": messages,
            "__files__": files or metadata.get("files", []),
            "__features__": metadata.get("features", {}),
        }


@dataclass(frozen=True)
class PreparedWorkspaceModel:
    runtime_model: dict
    capabilities: CapabilitySet


@dataclass(frozen=True)
class _WorkspaceModelSnapshot:
    owner_id: str
    tool_ids: tuple[str, ...]
    skill_ids: tuple[str, ...]


class WorkspaceModelPreparation:
    """Prepare fresh Workspace Model facts and request-local capabilities."""

    def __init__(
        self,
        *,
        lookup_model: Callable[[str], Awaitable[Any]],
        capability_resolver: ModelCapabilityResolver,
    ):
        self._lookup_model = lookup_model
        self._capabilities = capability_resolver

    async def prepare(
        self,
        model_id: str,
        *,
        prepared: ModelPreparation,
        runtime: RequestRuntime,
        context: InvocationContext,
    ) -> PreparedWorkspaceModel:
        """Validate fresh selection before consulting the existing capability cache.

        One preparation-local snapshot supplies attachments and their owner when
        configured and runtime IDs match. Distinct IDs retain the separate owner
        record lookup on a cache miss. No snapshot survives this operation.

        Remove outer inference fields; provider handlers still apply the child's
        base_model_id, inference/custom params and system prompt. The builder
        prepares history, Skills and destination inlet filters afterward.
        """
        runtime_model = context.request.app.state.MODELS.get(model_id)
        if runtime_model is None:
            raise ValueError(f'Agent "{model_id}" is unavailable')
        model_info = await self._lookup_model(model_id)
        snapshot = None
        if model_info is None:
            is_pipe = (
                isinstance(runtime_model, dict)
                and isinstance(runtime_model.get("pipe"), dict)
                and runtime_model["pipe"].get("type") == "pipe"
            )
            if not is_pipe:
                raise ValueError(f'Agent "{model_id}" must be a Workspace Model or Pipe')
        else:
            if not model_info.is_active:
                raise ValueError(f'Agent "{model_id}" is inactive')
            meta = model_info.meta
            if hasattr(meta, "model_dump"):
                meta = meta.model_dump()
            elif isinstance(meta, dict):
                meta = dict(meta)
            else:
                meta = {}
            snapshot = _WorkspaceModelSnapshot(
                owner_id=str(model_info.user_id or ""),
                tool_ids=tuple(normalize_ids(meta.get("toolIds"))),
                skill_ids=tuple(normalize_skill_ids(meta.get("skillIds"))),
            )
        tool_ids = list(snapshot.tool_ids) if snapshot is not None else []
        skill_ids = list(snapshot.skill_ids) if snapshot is not None else []
        runtime.select_model(model_id)
        self._clear_outer_inference_params(prepared.body)
        capability_model_id = str(runtime_model.get("id") or "").strip()

        async def load(messages):
            if capability_model_id == model_id:
                owner_id = snapshot.owner_id if snapshot is not None else None
            else:
                capability_record = await self._lookup_model(capability_model_id)
                if capability_record is not None and not capability_record.is_active:
                    raise ValueError("Child Model capability owner is unavailable")
                owner_id = str(capability_record.user_id or "") if capability_record is not None else None
            if owner_id is None:
                if tool_ids or skill_ids:
                    raise ValueError("Child Model capability owner is unavailable")
                return CapabilitySet([], [], {})
            return await self._capabilities.resolve(
                request=context.request,
                capability_owner_id=owner_id,
                execution_user=context.user,
                tool_ids=tool_ids,
                skill_ids=skill_ids,
                runtime_model=runtime_model,
                metadata=runtime.metadata,
                messages=messages,
                event_emitter=context.event_emitter,
                event_call=context.event_call,
                oauth_token=context.oauth_token,
                files=context.files,
                connector=McpRuntime.connect,
                include_builtin_tools=True,
            )

        capabilities = await prepared.capabilities(
            model_id=capability_model_id, tool_ids=tool_ids, skill_ids=skill_ids, load=load,
        )
        return PreparedWorkspaceModel(runtime_model, capabilities)

    @staticmethod
    def _clear_outer_inference_params(body: dict) -> None:
        for key in tuple(body):
            if key not in CHILD_REQUEST_ENVELOPE_KEYS:
                body.pop(key, None)


class ChildFilterPipeline:
    async def run(
        self,
        *,
        body: dict,
        runtime_model: dict,
        runtime: RequestRuntime,
        context: InvocationContext,
    ) -> dict:
        metadata = runtime.metadata
        user_data = (
            context.user.model_dump()
            if hasattr(context.user, "model_dump")
            else {}
        )
        extra_params = {
            "__event_emitter__": context.event_emitter,
            "__event_call__": context.event_call,
            "__user__": user_data,
            "__metadata__": metadata,
            "__oauth_token__": context.oauth_token,
            "__request__": context.request,
            "__model__": runtime_model,
            "__chat_id__": metadata.get("chat_id"),
            "__message_id__": metadata.get("message_id"),
        }
        with runtime.child_filters():
            filter_functions = await get_filter_functions(
                context.request,
                runtime_model,
                metadata.get("filter_ids", []),
            )
            body, _flags = await process_filter_functions(
                request=context.request,
                filter_context=None,
                filter_functions=filter_functions,
                filter_type="inlet",
                form_data=body,
                extra_params=extra_params,
            )
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            raise TypeError("Subagent filter pipeline returned an invalid request body")
        body["metadata"] = metadata
        return body


class ChildRequestBuilder:
    """Prepare a destination request, including capabilities, reuse and filters."""

    def __init__(self, debug: Callable[..., None]):
        self._capabilities = ModelCapabilityResolver(McpRuntime())
        self._workspace_models = WorkspaceModelPreparation(
            lookup_model=lambda model_id: Models.get_model_by_id(model_id),
            capability_resolver=self._capabilities,
        )
        self._filters = ChildFilterPipeline()
        self._debug = debug

    @staticmethod
    def agent_registry(metadata: dict) -> dict[str, AgentSpec]:
        raw_registry = metadata.get("lite_agents") or {}
        if not isinstance(raw_registry, dict):
            return {}
        registry = {}
        for raw_agent_id, config in raw_registry.items():
            if not isinstance(config, dict):
                continue
            agent_id = str(raw_agent_id or "").strip()
            model_id = str(config.get("model_id") or "").strip()
            if agent_id and model_id:
                registry[agent_id] = AgentSpec(
                    model_id=model_id,
                    name=str(config.get("name") or agent_id),
                    routing_skill_id=str(config.get("routing_skill_id") or "").strip(),
                )
        return registry

    async def prepare(
        self,
        *,
        prepared: ModelPreparation,
        marker: HandoffMarker,
        registry: dict[str, AgentSpec],
        runtime: RequestRuntime,
        context: InvocationContext,
        history_turns: int,
        history_tool_calls: int,
    ) -> tuple[dict, AgentSpec]:
        agent_id = resolve_agent_id(marker.agent_id, runtime.metadata.get("lite_agents") or {})
        agent = registry.get(agent_id) if agent_id is not None else None
        if agent is None:
            raise ValueError(
                f'Agent ID "{marker.agent_id}" is not available in the current registry'
            )

        routed_body = prepared.body
        source_messages = remove_system_message(routed_body.get("messages") or [])
        raw_messages = runtime.metadata.get("lite_unfiltered_messages")
        if isinstance(raw_messages, list):
            raw_messages = remove_system_message(raw_messages)
            raw_user_index = MessageHistory.last_user_index(raw_messages)
            current_user_index = MessageHistory.last_user_index(source_messages)
            if raw_user_index >= 0 and current_user_index >= 0:
                source_messages = [
                    *raw_messages[:raw_user_index],
                    *source_messages[current_user_index:],
                ]
        child = await self._workspace_models.prepare(
            agent.model_id, prepared=prepared, runtime=runtime, context=context,
        )
        runtime_model = child.runtime_model
        capabilities = child.capabilities
        child_skill_ids = capabilities.skill_ids
        child_messages = copy.deepcopy(source_messages)
        workspace_context = ModelCapabilityResolver.knowledge_context(
            runtime_model, runtime.metadata,
        )
        if workspace_context:
            child_messages.insert(0, {"role": "system", "content": workspace_context})
        routed_body["messages"] = child_messages
        routed_body["model"] = agent.model_id
        runtime.bind_tools(capabilities.tools, capabilities.tool_ids, replace=True)
        runtime.sync(
            skill_ids=child_skill_ids,
            lite_target_agent_id=marker.agent_id,
            lite_target_model_id=agent.model_id,
            lite_target_skill_ids=child_skill_ids,
        )
        runtime.activate(marker, marker.agent_id, agent.model_id)
        routed_body["tools"] = [
            {"type": "function", "function": tool["spec"]}
            for tool in capabilities.tools.values()
        ]
        routed_body.pop("tool_choice", None)

        async def resolve_skill_user():
            return context.user.model_dump()

        skill_loader = BuiltinSkillLoader(
            invocation=SkillBuiltinInvocation(
                profile="child", request=context.request, runtime_model=runtime_model,
                metadata=runtime.metadata, messages=prepared.messages, files=context.files,
                event_emitter=context.event_emitter, event_call=context.event_call,
                oauth_token=context.oauth_token,
            ),
            get_builtin_tools=get_builtin_tools, resolve_user=resolve_skill_user,
            bind_history=self._capabilities.bind_history,
        )

        prepared_skills = await SkillPreparation.prepare(
            skill_ids=child_skill_ids, runtime_model=runtime_model, metadata=runtime.metadata,
            lookup_skill=Skills.get_skill_by_id, load_builtin=skill_loader.load,
        )
        # Tool history must use this preparation's loader eligibility, not the previous run's.
        runtime.sync(
            lite_view_skill_available=prepared_skills.loader is not None,
            lite_view_skill_model_id=agent.model_id if prepared_skills.loader is not None else None,
        )
        before_count = len(routed_body["messages"])
        projection = ToolContextProjection.available_tools(routed_body)
        routed_body["messages"] = ToolContextProjection.completed_history(
            projection.messages, history_turns=history_turns,
            history_tool_calls=history_tool_calls,
        )
        SkillPreparation.install_context(prepared_skills, routed_body, runtime_model)
        runtime.publish()
        self._debug(
            "child preparation model=%s allowed=%s turns=%s tools=%s messages before=%s after=%s",
            agent.model_id, sorted(projection.allowed_tool_names), history_turns,
            history_tool_calls, before_count, len(routed_body["messages"]),
        )
        routed_body = await self._filters.run(
            body=routed_body,
            runtime_model=runtime_model,
            runtime=runtime,
            context=context,
        )
        prepared.body = routed_body
        return routed_body, agent

class CompletionGateway:
    @staticmethod
    def response_error_text(response: Response) -> str:
        raw = getattr(response, "body", b"")
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")
        if not text:
            return f"Model provider returned HTTP {response.status_code}"
        try:
            data = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return text
        if not isinstance(data, dict):
            return text
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or error)
        return str(error or data.get("detail") or data.get("message") or text)

    async def generate(self, *, request, body, user):
        try:
            response = await generate_chat_completion(request, body, user)
        except HTTPException as exc:
            detail = exc.detail
            if isinstance(detail, (dict, list)):
                try:
                    detail = json.dumps(detail, ensure_ascii=False)
                except (TypeError, ValueError):
                    detail = str(detail)
            raise RuntimeError(str(detail or "Model request failed")) from exc

        if isinstance(response, StreamingResponse):
            if response.status_code >= 400:
                raise RuntimeError(f"Model provider returned HTTP {response.status_code}")
            return response
        if isinstance(response, Response):
            if response.status_code >= 400:
                raise RuntimeError(self.response_error_text(response))
            raw = getattr(response, "body", b"")
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")
            if not text:
                raise RuntimeError("Model returned empty HTTP response")
            try:
                return json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                return text
        if isinstance(response, dict):
            error = response.get("error")
            if error:
                if isinstance(error, dict):
                    error = error.get("detail") or error.get("message") or error
                raise RuntimeError(str(error))
            return response
        if response is None:
            raise RuntimeError("Model returned no response")
        return response


class Pipe:
    class Valves(BaseModel):
        orchestrator_model_id: str = Field(
            default="",
            description="Real model used for normal orchestrator inference.",
        )
        emit_handoff_status: bool = Field(
            default=True,
            description="Emit visible status on subagent handoff.",
        )
        history_turns: int = Field(
            default=0, ge=0,
            description="Completed previous user/assistant turns to retain for every subagent.",
        )
        history_tool_calls: int = Field(
            default=0, ge=0,
            description="Maximum completed Tool call occurrences inside retained previous turns; repeated IDs count separately.",
        )
        debug: bool = Field(default=False, description="Enable debug logs.")

    def __init__(self):
        self.valves = self.Valves()
        self._protocol = HandoffProtocol()
        self._capabilities = ModelCapabilityResolver(McpRuntime())
        self._child_builder = ChildRequestBuilder(self._debug)
        self._gateway = CompletionGateway()

    def _debug(self, message: str, *args) -> None:
        if self.valves.debug:
            log.warning("[LITE_ROUTER] " + message, *args)

    @staticmethod
    def _merge_tool_schemas(body: dict, tools: dict) -> None:
        schemas = list(body.get("tools") or [])
        by_name = {}
        order = []
        for schema in schemas:
            name = str(((schema or {}).get("function") or {}).get("name") or "")
            if name and name not in by_name:
                order.append(name)
            if name:
                by_name[name] = schema
        for name, tool in tools.items():
            if name not in by_name:
                order.append(name)
            by_name[name] = {"type": "function", "function": tool["spec"]}
        body["tools"] = [by_name[name] for name in order]

    async def _orchestrator_branch(
        self,
        body: dict,
        runtime: RequestRuntime,
        context: InvocationContext,
    ):
        with runtime.prepare_model("orchestrator", body) as preparation:
            runtime.require_router_chain()
            model_id = self.valves.orchestrator_model_id.strip()
            if not model_id:
                raise ValueError("orchestrator_model_id is not configured")
            runtime.select_model(model_id)
            routed = preparation.body
            base_tool_ids = normalize_ids(runtime.metadata.get("lite_base_tool_ids"))
            skill_ids = normalize_skill_ids(runtime.metadata.get("lite_orchestrator_skill_ids"))
            router_model_id = str(runtime.metadata.get("lite_router_model_id") or "").strip()
            runtime_model = context.request.app.state.MODELS.get(router_model_id) or {"id": router_model_id}
            if base_tool_ids or skill_ids:
                async def load(messages):
                    return await self._capabilities.resolve(
                        request=context.request,
                        capability_owner_id=str(
                            runtime.metadata.get("lite_router_owner_id") or ""
                        ).strip(),
                        execution_user=context.user,
                        tool_ids=base_tool_ids,
                        skill_ids=skill_ids,
                        runtime_model=runtime_model,
                        metadata=runtime.metadata,
                        messages=messages,
                        event_emitter=context.event_emitter,
                        event_call=context.event_call,
                        oauth_token=context.oauth_token,
                        files=context.files,
                        connector=McpRuntime.connect,
                    )
                capabilities = await preparation.capabilities(
                    model_id=router_model_id, tool_ids=base_tool_ids, skill_ids=skill_ids,
                    load=load,
                )
                tool_ids = normalize_ids(
                    [*(runtime.metadata.get("tool_ids") or []), *base_tool_ids]
                )
                runtime.bind_tools(capabilities.tools, tool_ids, replace=False)
                self._merge_tool_schemas(routed, capabilities.tools)

            messages = [
                message
                for message in routed.get("messages") or []
                if not (
                    isinstance(message.get("content"), str)
                    and message["content"].startswith(
                        (ORCHESTRATOR_SKILL_PROMPT_PREFIX, GENERIC_SKILL_PROMPT_PREFIX)
                    )
                )
            ]
            async def resolve_skill_user():
                owner = await Users.get_user_by_id(str(runtime.metadata.get("lite_router_owner_id") or "").strip())
                if owner is None:
                    raise ValueError("Model capability owner is unavailable")
                return owner.model_dump()

            skill_loader = BuiltinSkillLoader(
                invocation=SkillBuiltinInvocation(
                    profile="orchestrator", request=context.request, runtime_model=runtime_model,
                    metadata=runtime.metadata, messages=preparation.messages, files=context.files,
                    event_emitter=context.event_emitter, event_call=context.event_call,
                    oauth_token=context.oauth_token,
                ),
                get_builtin_tools=get_builtin_tools, resolve_user=resolve_skill_user,
                bind_history=self._capabilities.bind_history,
            )

            prepared = await SkillPreparation.prepare(
                skill_ids=skill_ids, runtime_model=runtime_model, metadata=runtime.metadata,
                lookup_skill=Skills.get_skill_by_id, load_builtin=skill_loader.load,
            )
            SkillPreparation.install_loader(prepared, routed, runtime_model)
            runtime.sync(tools=runtime.shared_tools())
            skill_prompt = prepared.context
            if prepared.loader is not None:
                skill_prompt = (
                    "The following Skills are available on demand. Inspect their descriptions "
                    "and call view_skill for any Skill that may apply before following its full "
                    "instructions. Load a relevant routing Skill before calling lite_delegate."
                    "\n\n" + skill_prompt
                )
            if skill_prompt:
                messages.insert(
                    0,
                    {
                        "role": "system",
                        "content": ORCHESTRATOR_SKILL_PROMPT_PREFIX + skill_prompt,
                    },
                )
            routed["messages"] = messages

            routed["model"] = model_id
        self._debug("-> orchestrator %s", model_id)
        return await self._gateway.generate(request=context.request, body=routed, user=context.user)

    async def _child_branch(
        self,
        body: dict,
        marker: HandoffMarker,
        registry: dict[str, AgentSpec],
        runtime: RequestRuntime,
        context: InvocationContext,
    ):
        with runtime.prepare_model("child", body) as preparation:
            runtime.require_router_chain()
            routed, agent = await self._child_builder.prepare(
                prepared=preparation,
                marker=marker,
                registry=registry,
                runtime=runtime,
                context=context,
                history_turns=self.valves.history_turns,
                history_tool_calls=self.valves.history_tool_calls,
            )
        if self.valves.emit_handoff_status and context.event_emitter:
            try:
                await context.event_emitter(
                    {
                        "type": "status",
                        "data": {
                            "action": "lite_delegate",
                            "description": f"delegate to {agent.name}",
                            "done": True,
                        },
                    }
                )
            except Exception as exc:  # noqa: BLE001 - status events are best effort
                self._debug("status emit failed: %s", exc)
        self._debug("-> child %s", agent.model_id)
        self._debug(
            "child tool results: incoming=%s outgoing=%s",
            [m.get("tool_call_id") for m in body.get("messages", []) if m.get("role") == "tool"],
            [m.get("tool_call_id") for m in routed["messages"] if m.get("role") == "tool"],
        )
        return await self._gateway.generate(request=context.request, body=routed, user=context.user)

    async def pipe(
        self,
        body: dict,
        __request__,
        __user__: dict,
        __metadata__: dict,
        __event_emitter__=None,
        __event_call__=None,
        __oauth_token__=None,
        __files__=None,
    ):
        runtime = RequestRuntime(__request__, __metadata__)
        runtime.bind_context_request()
        user_id = (__user__ or {}).get("id")
        if not user_id:
            raise ValueError("Missing user id")
        user = await Users.get_user_by_id(user_id)
        if user is None:
            raise ValueError("User not found")

        context = InvocationContext(
            request=__request__,
            user=user,
            event_emitter=__event_emitter__,
            event_call=__event_call__,
            oauth_token=__oauth_token__,
            files=__files__,
        )
        messages = body.get("messages") or []
        marker = self._protocol.active(__metadata__) or self._protocol.find_current(messages)
        registry = self._child_builder.agent_registry(__metadata__)
        self._debug(
            "model=%s registry=%s handoff_skill=%s",
            body.get("model"),
            list(registry),
            marker.agent_id if marker else None,
        )
        if marker is None:
            return await self._orchestrator_branch(body, runtime, context)
        return await self._child_branch(body, marker, registry, runtime, context)
