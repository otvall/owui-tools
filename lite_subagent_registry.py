"""
title: Lite Subagent Registry
description: Dynamically exposes accessible subagents to Lite Handoff Router.
version: 0.18.0
required_open_webui_version: 0.11.1
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from open_webui.config import BYPASS_ADMIN_ACCESS_CONTROL
from open_webui.env import BYPASS_MODEL_ACCESS_CONTROL
from open_webui.models.models import Models
from open_webui.models.skills import Skills
from open_webui.models.users import Users
from open_webui.utils.models import check_model_access
from pydantic import BaseModel, Field

# BEGIN GENERATED REQUEST RUNTIME
# Edit shared/request_runtime.py; run python3 tools/generate_skill_preparation.py
"""Authoritative local request lifecycle for independently uploaded Functions."""


import copy
import json
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
        request_key = self.router_request_key(body)
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

    def router_request_key(self, body: dict) -> dict:
        """Recognize Registry re-entry; Pipe continuations never infer a new request."""
        if self.metadata.get("message_id"):
            return {"chat_id": self.metadata.get("chat_id"), "message_id": self.metadata["message_id"]}
        users = [
            message for message in body.get("messages") or []
            if message.get("role") == "user" and not self.is_tool_image_message(message)
        ]
        return {
            "user_count": len(users),
            "user_content": json.dumps(users[-1].get("content") if users else None, sort_keys=True),
        }

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

    def finish_filter(self, name: str, *, body: dict | None = None, **values) -> None:
        self.metadata.update(values)
        self.metadata[name + "_applied"] = True
        if name in self.CHILD_FILTERS and self.metadata.get("lite_subagent_filter_run"):
            self.metadata.setdefault("lite_subagent_filter_pipeline", []).append(name)
        if (
            name in self.ROUTER_FILTERS and self.metadata.get("lite_registry_applied")
            and not self.metadata.get("lite_subagent_filter_run")
        ):
            self.metadata.setdefault("lite_router_filter_pipeline", []).append(name)
            if body is not None:
                self.metadata["lite_router_request_key"] = self.router_request_key(body)
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

# BEGIN GENERATED SKILL PREPARATION
# Edit shared/skill_preparation.py; run python3 tools/generate_skill_preparation.py
def normalize_skill_ids(values) -> list[str]:
    result = []
    seen = set()
    for raw_value in values or []:
        value = str(raw_value or "").strip().lower()
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result
# END GENERATED SKILL PREPARATION

log = logging.getLogger(__name__)

# Workspace Model ID -> Routing Skill ID.
SUBAGENTS: dict[str, str] = {
    "lite_test_pipe_echo": "route-lite-pipe-echo",
    "lite_test_pipe_json": "route-lite-pipe-json",
    "lite-test-workspace-math": "route-lite-workspace-math",
    "lite-test-workspace-text": "route-lite-workspace-text",
}


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
class AgentSpec:
    model_id: str
    routing_skill_id: str
    name: str


@dataclass(frozen=True)
class RegistrySnapshot:
    agents: tuple[AgentSpec, ...]

    @property
    def registry(self) -> dict[str, dict]:
        return {
            agent.model_id: {
                "model_id": agent.model_id,
                "routing_skill_id": agent.routing_skill_id,
                "name": agent.name,
            }
            for agent in self.agents
        }

    @property
    def skill_ids(self) -> list[str]:
        return [agent.routing_skill_id for agent in self.agents]


class RegistryMap(dict):
    """Dictionary wire format plus already-validated routing Skill IDs."""

    def __init__(self, snapshot: RegistrySnapshot):
        super().__init__(snapshot.registry)
        self.skill_ids = snapshot.skill_ids


class ModelAccessPolicy:
    def __init__(self, debug):
        self._debug = debug

    async def can_access(self, *, request, user, model_id: str) -> bool:
        runtime_model = request.app.state.MODELS.get(model_id)
        if runtime_model is None:
            self._debug("runtime model unavailable: %s", model_id)
            return False

        model_info = await Models.get_model_by_id(model_id)
        if model_info is None:
            is_pipe = (
                isinstance(runtime_model, dict)
                and isinstance(runtime_model.get("pipe"), dict)
                and runtime_model["pipe"].get("type") == "pipe"
            )
            if not is_pipe:
                self._debug("workspace model or pipe not found: %s", model_id)
                return False
            return BYPASS_MODEL_ACCESS_CONTROL or user.role == "admin"

        if not model_info.is_active:
            self._debug("workspace model inactive: %s", model_id)
            return False
        if BYPASS_MODEL_ACCESS_CONTROL:
            return True
        if user.role == "admin" and BYPASS_ADMIN_ACCESS_CONTROL:
            return True

        try:
            await check_model_access(user, runtime_model, model_info=model_info)
        except Exception as exc:
            if str(exc) != "Model not found":
                raise
            self._debug("user=%s cannot access model=%s", user.id, model_id)
            return False
        return True


class SkillAvailabilityValidator:
    @staticmethod
    async def validate(skill_ids: list[str], *, strict: bool = True) -> list[str]:
        ids = normalize_skill_ids(skill_ids)
        skills = await asyncio.gather(*(Skills.get_skill_by_id(skill_id) for skill_id in ids))
        missing = [
            skill_id
            for skill_id, skill in zip(ids, skills, strict=True)
            if skill is None or not skill.is_active
        ]
        if strict and missing:
            raise ValueError(
                "Configured model-bound Skills are unavailable: " + ", ".join(missing)
            )
        return [
            skill_id
            for skill_id, skill in zip(ids, skills, strict=True)
            if skill is not None and skill.is_active
        ]


class SubagentCatalog:
    def __init__(self, entries: dict[str, str], access_policy: ModelAccessPolicy, debug):
        self._entries = entries
        self._access_policy = access_policy
        self._debug = debug

    async def build(self, *, request, user) -> RegistrySnapshot:
        candidates = await asyncio.gather(
            *(
                self._load_agent(
                    request=request,
                    user=user,
                    model_id=str(model_id or "").strip(),
                    routing_skill_id=next(iter(normalize_skill_ids([skill_id])), ""),
                )
                for model_id, skill_id in self._entries.items()
            )
        )
        by_model_id = {}
        for agent in candidates:
            if agent is not None:
                by_model_id[agent.model_id] = agent
        return RegistrySnapshot(tuple(by_model_id.values()))

    async def _load_agent(
        self,
        *,
        request,
        user,
        model_id: str,
        routing_skill_id: str,
    ) -> AgentSpec | None:
        if not model_id or not routing_skill_id:
            return None
        if not await self._access_policy.can_access(
            request=request,
            user=user,
            model_id=model_id,
        ):
            return None

        skill = await Skills.get_skill_by_id(routing_skill_id)
        if skill is None or not skill.is_active:
            self._debug("routing skill unavailable: %s", routing_skill_id)
            return None
        return AgentSpec(
            model_id=model_id,
            routing_skill_id=routing_skill_id,
            name=skill.name or routing_skill_id,
        )


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=-100, description="Run this filter early.")
        base_tool_ids: list[str] = Field(
            default_factory=lambda: ["lite_delegate"],
            description="Base orchestrator tools.",
        )
        base_skill_ids: list[str] = Field(
            default_factory=lambda: [
                "orchestrator-capability-guide",
                "describe-available-agents",
            ],
            description="Base orchestrator skills.",
        )
        debug: bool = Field(default=False, description="Enable debug logs.")

    def __init__(self):
        self.valves = self.Valves()
        self._skill_validator = SkillAvailabilityValidator()
        self._access_policy = ModelAccessPolicy(self._debug)
        self._catalog = SubagentCatalog(SUBAGENTS, self._access_policy, self._debug)

    def _debug(self, message: str, *args) -> None:
        if self.valves.debug:
            log.warning("[LITE_REGISTRY] " + message, *args)

    # Compatibility adapters for installed-runtime characterization.
    async def _can_access_model(self, *, request, user, model_id: str) -> bool:
        return await self._access_policy.can_access(
            request=request,
            user=user,
            model_id=model_id,
        )

    async def _validate_skill_ids(
        self,
        skill_ids: list[str],
        *,
        strict: bool = True,
    ) -> list[str]:
        return await self._skill_validator.validate(skill_ids, strict=strict)

    async def _build_registry(self, *, request, user) -> dict[str, dict]:
        return RegistryMap(await self._catalog.build(request=request, user=user))

    async def inlet(
        self,
        body: dict,
        __user__: dict | None = None,
        __request__=None,
    ) -> dict:
        if __request__ is None:
            raise ValueError("Lite Subagent Registry requires __request__")
        user_id = (__user__ or {}).get("id")
        if not user_id:
            raise ValueError("Missing user id")
        user = await Users.get_user_by_id(user_id)
        if user is None:
            raise ValueError("User not found")

        registry = await self._build_registry(request=__request__, user=user)
        base_skill_ids = normalize_skill_ids(self.valves.base_skill_ids)
        if isinstance(registry, RegistryMap):
            await self._validate_skill_ids(base_skill_ids, strict=True)
            routing_skill_ids = registry.skill_ids
        else:
            # Preserve test/custom subclass compatibility without reloading in production.
            routing_skill_ids = normalize_skill_ids(registry)
            await self._validate_skill_ids(
                [*base_skill_ids, *registry],
                strict=True,
            )
        orchestrator_skill_ids = normalize_skill_ids([*base_skill_ids, *routing_skill_ids])

        router_model_id = str(body.get("model") or "").strip()
        router_model = await Models.get_model_by_id(router_model_id)
        if router_model is None or not router_model.is_active:
            raise ValueError("Router Model is unavailable")
        router_owner_id = str(router_model.user_id or "").strip()
        if not router_owner_id:
            raise ValueError("Router Model owner is unavailable")

        metadata = body.get("metadata")
        if metadata is None:
            metadata = {}
            body["metadata"] = metadata
        if not isinstance(metadata, dict):
            raise TypeError("Lite Subagent Registry metadata must be an object")
        RequestRuntime(__request__, metadata).start_request(
            body,
            lite_agents=dict(registry),
            lite_router_model_id=router_model_id,
            lite_router_owner_id=router_owner_id,
            lite_base_tool_ids=normalize_ids(self.valves.base_tool_ids),
            lite_orchestrator_skill_ids=orchestrator_skill_ids,
            lite_registry_applied=True,
        )
        self._debug("registry=%s", json.dumps(dict(registry), ensure_ascii=False))
        self._debug("model-bound base tools=%s", metadata["lite_base_tool_ids"])
        self._debug("model-bound skill count=%s", len(orchestrator_skill_ids))
        return body
