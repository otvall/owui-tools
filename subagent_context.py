"""
title: Subagent Context
description: Limits previous user/assistant turns and Tool calls for a model.
version: 0.18.0
required_open_webui_version: 0.11.1
"""

from __future__ import annotations

import copy
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
        body["metadata"] = self.metadata
        SkillLoaderOwnership(self.metadata).remove(body)
        self.discard(*self.RESET_FIELDS)
        self.shared_tools()
        self.sync(**configuration)

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

log = logging.getLogger(__name__)

TOOL_IMAGE_TEXT = "Here are the images from the tool results above. Please analyze them."
APPLIED_KEY = "subagent_context_applied"
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


def user_indices(messages: list[dict]) -> list[int]:
    return [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "user" and not is_tool_image_message(message)
    ]


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=-20,
            description="Run after Tool Call Filter and before Skill Context.",
        )
        history_turns: int = Field(
            default=0,
            ge=0,
            description="Completed previous user/assistant turns to retain.",
        )
        history_tool_calls: int = Field(
            default=0,
            ge=0,
            description="Maximum completed Tool calls inside retained previous turns.",
        )
        debug: bool = Field(default=False, description="Enable debug logs.")

    def __init__(self):
        self.valves = self.Valves()

    def _debug(self, message: str, *args) -> None:
        if self.valves.debug:
            log.warning("[SUBAGENT_CONTEXT] " + message, *args)

    @staticmethod
    def _historical_segment(messages: list[dict], start: int, end: int) -> list[dict]:
        return messages[start:end]

    @staticmethod
    def _is_completed_segment(messages: list[dict], start: int, end: int) -> bool:
        return any(
            message.get("role") == "assistant"
            and message.get("content")
            and not message.get("tool_calls")
            for message in messages[start:end]
        )

    @staticmethod
    def _select_segment(segment: list[dict], tool_limit_ids: set[str]) -> list[dict]:
        selected: dict[int, dict] = {}
        if segment and segment[0].get("role") == "user":
            selected[0] = copy.deepcopy(segment[0])

        final_answer = next(
            (
                index
                for index in range(len(segment) - 1, -1, -1)
                if segment[index].get("role") == "assistant"
                and segment[index].get("content")
                and not segment[index].get("tool_calls")
            ),
            -1,
        )
        if final_answer >= 0:
            selected[final_answer] = {
                "role": "assistant",
                "content": copy.deepcopy(segment[final_answer].get("content")),
            }

        for index, message in enumerate(segment):
            if message.get("role") == "assistant" and message.get("tool_calls"):
                kept = [
                    copy.deepcopy(call)
                    for call in message.get("tool_calls") or []
                    if call.get("id") in tool_limit_ids
                ]
                if kept:
                    selected[index] = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": kept,
                    }
            elif (
                message.get("role") == "tool"
                and message.get("tool_call_id") in tool_limit_ids
            ):
                selected[index] = copy.deepcopy(message)
        return [selected[index] for index in sorted(selected)]

    async def inlet(self, body: dict, __request__=None) -> dict:
        metadata = body.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise TypeError("Subagent Context metadata must be an object")
        messages = body.get("messages")
        if not isinstance(messages, list):
            raise TypeError("Subagent Context messages must be a list")
        RequestRuntime(__request__, metadata).before_filter("subagent_context")

        indices = user_indices(messages)
        if not indices:
            body["messages"] = list(messages)
        else:
            current_user = indices[-1]
            historical_ranges = [
                (start, end)
                for start, end in zip(indices[:-1], indices[1:])
                if self._is_completed_segment(messages, start, end)
            ]
            retained_ranges = (
                historical_ranges[-self.valves.history_turns :]
                if self.valves.history_turns
                else []
            )
            historical_calls = [
                call.get("id")
                for start, end in retained_ranges
                for message in messages[start:end]
                if message.get("role") == "assistant"
                for call in message.get("tool_calls") or []
                if call.get("id")
            ]
            retained_call_ids = set(
                historical_calls[-self.valves.history_tool_calls :]
                if self.valves.history_tool_calls
                else []
            )
            system_messages = [
                copy.deepcopy(message)
                for message in messages[:current_user]
                if message.get("role") == "system"
            ]
            history = [
                item
                for start, end in retained_ranges
                for item in self._select_segment(
                    self._historical_segment(messages, start, end),
                    retained_call_ids,
                )
            ]
            current = copy.deepcopy(messages[current_user:])
            body["messages"] = [*system_messages, *history, *current]

        RequestRuntime(__request__, metadata).finish_filter("subagent_context")
        self._debug(
            "turns=%s tools=%s before=%s after=%s",
            self.valves.history_turns,
            self.valves.history_tool_calls,
            len(messages),
            len(body["messages"]),
        )
        return body
