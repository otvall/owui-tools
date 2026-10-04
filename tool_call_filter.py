"""
title: Tool Call Filter
description: Keeps unambiguous completed Tool call occurrences permitted for the destination model.
version: 0.18.0
required_open_webui_version: 0.11.1
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

# BEGIN GENERATED REQUEST RUNTIME
# Edit shared/request_runtime.py; run python3 tools/generate_skill_preparation.py
"""Authoritative local request lifecycle for independently uploaded Functions."""


import copy
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class CapabilitySet:
    tool_ids: list[str]
    skill_ids: list[str]
    tools: dict


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
        "lite_history_boundary", "lite_child_messages", "lite_router_user_index",
        "lite_active_handoff", "lite_active_agent_id", "lite_active_skill_id",
        "lite_active_model_id", "lite_active_tool_runtime", "lite_base_tool_runtime",
        "lite_orchestrator_skill_context", "lite_unfiltered_messages",
        "lite_router_filter_pipeline", "lite_router_request_key",
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
        request_key = self.router_request_key()
        pipeline = self.metadata.get("lite_router_filter_pipeline")
        pipeline = pipeline if isinstance(pipeline, list) else []
        preceding = [
            label for name, label in self.ROUTER_FILTERS.items()
            if name != "lite_registry" and (name in pipeline or self.metadata.get(name + "_applied"))
        ]
        if preceding and (
            not self.metadata.get("lite_registry_applied")
            or self.metadata.get("lite_router_request_key") == request_key
        ):
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

    def require_router_chain(self) -> None:
        pipeline = self.metadata.get("lite_router_filter_pipeline")
        pipeline = pipeline if isinstance(pipeline, list) else []
        missing = [label for name, label in self.ROUTER_FILTERS.items() if name not in pipeline]
        if missing:
            raise ValueError("Required Router filters are missing or out of order: " + ", ".join(missing))
        if pipeline != list(self.ROUTER_FILTERS):
            raise ValueError(
                "Router filters ran in the wrong order; required: " + " -> ".join(self.ROUTER_FILTERS.values())
            )
        if self.metadata.get("lite_router_request_key") != self.router_request_key():
            raise ValueError("Lite Subagent Registry must run for the current request before Router dispatch")

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
            if self.metadata.get("lite_subagent_filter_run") or not (
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

    def cached_capabilities(
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

    def cache_capabilities(
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
# Edit shared/tool_history.py; run python3 tools/generate_skill_preparation.py
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
# END GENERATED TOOL HISTORY

log = logging.getLogger(__name__)

DELEGATE_VERSION = "v2"
TOOL_IMAGE_TEXT = "Here are the images from the tool results above. Please analyze them."
APPLIED_KEY = "tool_call_filter_applied"
PIPELINE_KEY = "lite_subagent_filter_pipeline"


def is_tool_image_message(message: dict) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "user"
        and isinstance(content, list)
        and len(content) > 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
        and content[0].get("text") == TOOL_IMAGE_TEXT
        and all(
            isinstance(part, dict) and part.get("type") == "image_url"
            for part in content[1:]
        )
    )


def last_user_index(messages: list[dict]) -> int:
    return next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].get("role") == "user"
            and not is_tool_image_message(messages[index])
        ),
        -1,
    )


def parse_delegate_marker(value) -> str | None:
    if isinstance(value, dict):
        data = value
    elif isinstance(value, str) and value.strip():
        try:
            data = json.loads(value)
            if isinstance(data, str):
                data = json.loads(data)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    else:
        return None
    if not isinstance(data, dict) or data.get("__lite_delegate__") != DELEGATE_VERSION:
        return None
    agent_id = str(data.get("agent_id") or "").strip()
    return agent_id or None


def visible_assistant(message: dict) -> bool:
    content = message.get("content")
    return bool(
        (content.strip() if isinstance(content, str) else content)
        or message.get("tool_calls")
        or message.get("reasoning_content")
        or message.get("thinking")
    )


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=-30,
            description="Run before Subagent Context and Skill Context.",
        )
        debug: bool = Field(default=False, description="Enable debug logs.")

    def __init__(self):
        self.valves = self.Valves()

    def _debug(self, message: str, *args) -> None:
        if self.valves.debug:
            log.warning("[TOOL_CALL_FILTER] " + message, *args)

    @staticmethod
    def _allowed_tool_names(body: dict, metadata: dict) -> set[str]:
        names = {
            str(((schema or {}).get("function") or {}).get("name") or "").strip()
            for schema in body.get("tools") or []
        }
        names.update(str(name or "").strip() for name in (metadata.get("tools") or {}))

        if (
            metadata.get("lite_view_skill_available")
            and metadata.get("lite_view_skill_model_id")
            == metadata.get("lite_target_model_id")
        ):
            names.add("view_skill")
        return {name for name in names if name}

    @staticmethod
    def _historical_exchanges(
        messages: list[dict], exchanges: list[ToolExchange],
        selected_agent: str | None, registry: dict,
    ) -> set[ToolExchange]:
        """Attribute grouped calls at their results, before removing Handoffs."""
        selected: set[ToolExchange] = set()
        if selected_agent is None:
            return selected
        by_result = {exchange.result_index: exchange for exchange in exchanges}
        completed_calls = {(exchange.message_index, exchange.call_index) for exchange in exchanges}
        executor: str | None = None
        uncertain_batch = False
        for index, message in enumerate(messages):
            if message.get("role") == "user" and not is_tool_image_message(message):
                executor = None
            if message.get("role") == "assistant":
                uncertain_batch = any(
                    (call.get("function") or {}).get("name") == "lite_delegate"
                    and (index, call_index) not in completed_calls
                    for call_index, call in enumerate(message.get("tool_calls") or [])
                )
                if uncertain_batch:
                    # An unpaired delegate has no trustworthy transition position.
                    executor = None
            exchange = by_result.get(index)
            if exchange is None:
                continue
            call = messages[exchange.message_index]["tool_calls"][exchange.call_index]
            if (call.get("function") or {}).get("name") == "lite_delegate":
                agent_id = parse_delegate_marker(message.get("content"))
                executor = resolve_agent_id(agent_id, registry) if not uncertain_batch else None
            elif executor == selected_agent:
                selected.add(exchange)
        return selected

    @classmethod
    def _keep_supported_pairs(
        cls, messages: list[dict], allowed_names: set[str], target_agent_id: str,
        registry: dict,
    ) -> list[dict]:
        exchanges = completed_tool_exchanges(messages, is_tool_image_message=is_tool_image_message)
        current_user = last_user_index(messages)
        selected_agent = resolve_agent_id(target_agent_id, registry)
        historical = cls._historical_exchanges(
            messages[:max(current_user, 0)], exchanges, selected_agent, registry,
        )
        marker_index = -1
        if current_user >= 0 and target_agent_id:
            for exchange in exchanges:
                call = messages[exchange.message_index]["tool_calls"][exchange.call_index]
                if exchange.message_index <= current_user or (call.get("function") or {}).get("name") != "lite_delegate":
                    continue
                destination = parse_delegate_marker(messages[exchange.result_index].get("content"))
                if registry:
                    destination = resolve_agent_id(destination, registry)
                if destination and destination == (selected_agent or target_agent_id):
                    marker_index = max(marker_index, exchange.result_index)

        accepted = []
        for exchange in exchanges:
            call = messages[exchange.message_index]["tool_calls"][exchange.call_index]
            name = str((call.get("function") or {}).get("name") or "").strip()
            if name not in allowed_names:
                continue
            if exchange.message_index < current_user:
                if target_agent_id and exchange not in historical:
                    continue
            elif exchange.result_index <= marker_index:
                continue
            accepted.append(exchange)
        # Grouped child calls occur before the delegate result in OWUI's payload.
        # Recover only their concrete pairs, after matching the original batches.
        recovered_calls = [
            messages[exchange.message_index]["tool_calls"][exchange.call_index]
            for exchange in accepted
            if current_user < exchange.message_index < marker_index
        ]
        accepted_calls = {(exchange.message_index, exchange.call_index) for exchange in accepted}
        accepted_results = {exchange.result_index for exchange in accepted}
        cleaned = []
        for index, original in enumerate(messages):
            if current_user < index <= marker_index:
                if index == marker_index and recovered_calls:
                    cleaned.append({"role": "assistant", "content": "", "tool_calls": recovered_calls})
                continue
            if original.get("role") == "assistant" and original.get("tool_calls"):
                message = dict(original)
                message["tool_calls"] = [
                    call
                    for call_index, call in enumerate(original.get("tool_calls") or [])
                    if (index, call_index) in accepted_calls
                ]
                if not message["tool_calls"]:
                    message.pop("tool_calls", None)
                    message.pop("reasoning_items", None)
                if visible_assistant(message):
                    cleaned.append(message)
            elif original.get("role") != "tool" or index in accepted_results:
                cleaned.append(original)
        return cleaned

    async def inlet(self, body: dict, __request__=None) -> dict:
        metadata = body.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise TypeError("Tool Call Filter metadata must be an object")
        messages = body.get("messages")
        if not isinstance(messages, list):
            raise TypeError("Tool Call Filter messages must be a list")
        allowed_names = self._allowed_tool_names(body, metadata)
        registry = metadata.get("lite_agents")
        registry = registry if isinstance(registry, dict) else {}
        body["messages"] = self._keep_supported_pairs(
            messages, allowed_names,
            str(metadata.get("lite_target_agent_id") or "").strip(),
            registry,
        )
        RequestRuntime(__request__, metadata).finish_filter("tool_call_filter")
        self._debug(
            "allowed=%s messages before=%s after=%s",
            sorted(allowed_names),
            len(messages),
            len(body["messages"]),
        )
        return body
