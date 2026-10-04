"""
title: Lite Handoff Router
description: Stateless same-response subagent handoff router.
version: 0.18.0
required_open_webui_version: 0.11.1
"""

from __future__ import annotations

import copy
import html
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
"""Authoritative Skill preparation, embedded into independently uploaded Functions."""

import copy
import html
from dataclasses import dataclass
from typing import Any



def normalize_skill_ids(values) -> list[str]:
    result = []
    seen = set()
    for raw_value in values or []:
        value = str(raw_value or "").strip().lower()
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


@dataclass(frozen=True)
class PreparedSkills:
    ids: list[str]
    context: str
    loader: dict | None


class SkillPreparation:
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

DELEGATE_VERSION = "v2"
ACTIVE_HANDOFF_KEY = "lite_active_handoff"
CHILD_RUNTIME_KEY = "lite_active_tool_runtime"
BASE_RUNTIME_KEY = "lite_base_tool_runtime"
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
        if isinstance(value, dict):
            data = value
        elif isinstance(value, str) and value.strip():
            try:
                data = json.loads(value.strip())
                if isinstance(data, str):
                    data = json.loads(data)
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
        else:
            return None

        if not isinstance(data, dict) or data.get("__lite_delegate__") != DELEGATE_VERSION:
            return None
        agent_id = str(data.get("agent_id") or "").strip()
        if not agent_id:
            return None
        return cls(agent_id=agent_id)

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
        exchange = HandoffProtocol._current_exchange(messages)
        return exchange[0] if exchange is not None else None

    @staticmethod
    def _current_exchange(messages: list[dict]) -> tuple[HandoffMarker, int, int] | None:
        last_user_index = MessageHistory.last_user_index(messages)

        handoff = None
        delegate_call_ids = HandoffProtocol._delegate_call_ids(messages[last_user_index + 1:])
        for index, message in enumerate(messages[last_user_index + 1 :], last_user_index + 1):
            if message.get("role") == "tool" and message.get("tool_call_id") in delegate_call_ids:
                marker = HandoffMarker.parse(message.get("content"))
                if marker is not None:
                    handoff = (marker, index, last_user_index)
        return handoff

    @staticmethod
    def active(metadata: dict) -> HandoffMarker | None:
        return HandoffMarker.parse(metadata.get(ACTIVE_HANDOFF_KEY))

    @staticmethod
    def _delegate_call_ids(messages: list[dict]) -> set[str]:
        return {
            call.get("id")
            for message in messages
            if message.get("role") == "assistant"
            for call in message.get("tool_calls") or []
            if (call.get("function") or {}).get("name") == "lite_delegate"
            and call.get("id")
        }


class MessageHistory:
    @staticmethod
    def is_tool_image_message(message: dict) -> bool:
        # OWUI 0.11.1 flattens tool images into this synthetic user message.
        content = message.get("content")
        return (
            message.get("role") == "user"
            and isinstance(content, list)
            and len(content) > 1
            and isinstance(content[0], dict)
            and content[0].get("type") == "text"
            and content[0].get("text") == TOOL_IMAGE_TEXT
            and all(isinstance(part, dict) and part.get("type") == "image_url" for part in content[1:])
        )

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

        return CapabilitySet(requested_ids, requested_skill_ids, tools)

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

    def __init__(self):
        self._capabilities = ModelCapabilityResolver(McpRuntime())
        self._filters = ChildFilterPipeline()

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
        body: dict,
        marker: HandoffMarker,
        registry: dict[str, AgentSpec],
        runtime: RequestRuntime,
        context: InvocationContext,
    ) -> tuple[dict, AgentSpec]:
        agent = registry.get(marker.agent_id)
        if agent is None:
            agent = next(
                (
                    candidate
                    for candidate in registry.values()
                    if candidate.routing_skill_id == marker.agent_id
                ),
                None,
            )
        if agent is None:
            raise ValueError(
                f'Agent ID "{marker.agent_id}" is not available in the current registry'
            )

        routed_body = runtime.routed_body(body)
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
        capability_messages = runtime.metadata.get("lite_child_messages")
        if not isinstance(capability_messages, list):
            capability_messages = []
            runtime.sync(lite_child_messages=capability_messages)
        runtime_model, child_skill_ids, child_tool_ids, capabilities = (
            await self._prepare_workspace_model(
                routed_body=routed_body,
                agent=agent,
                runtime=runtime,
                context=context,
                capability_messages=capability_messages,
            )
        )
        child_messages = copy.deepcopy(source_messages)
        child_messages.insert(
            0,
            {
                "role": "system",
                "content": self._system_prompt(
                    ModelCapabilityResolver.knowledge_context(
                        runtime_model,
                        runtime.metadata,
                    ),
                ),
            },
        )
        routed_body["messages"] = child_messages
        routed_body["model"] = agent.model_id
        runtime.bind_tools(capabilities.tools, child_tool_ids, replace=True)
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
        routed_body = await self._filters.run(
            body=routed_body,
            runtime_model=runtime_model,
            runtime=runtime,
            context=context,
        )
        capability_messages[:] = copy.deepcopy(routed_body["messages"])
        return routed_body, agent

    async def _prepare_workspace_model(
        self,
        *,
        routed_body: dict,
        agent: AgentSpec,
        runtime: RequestRuntime,
        context: InvocationContext,
        capability_messages: list[dict],
    ) -> tuple[dict, list[str], list[str], CapabilitySet]:
        """Prepare only the Workspace Model state needed by a nested handoff.

        Provider handlers still apply the child model's base_model_id, inference
        params, custom params, and system prompt. This method removes the outer
        model's already-expanded inference payload, then resolves the settings
        that OWUI normally prepares before provider dispatch: attached Tools,
        MCP servers and builtin Tools. The destination model's inlet filters
        prepare history and Skills after this step.
        """
        runtime_model = context.request.app.state.MODELS.get(agent.model_id)
        if runtime_model is None:
            raise ValueError(f'Agent "{agent.model_id}" is unavailable')
        child_skill_ids, child_tool_ids = await self._attachments(agent, runtime_model)
        self._clear_outer_inference_params(routed_body)
        capabilities = await self._model_capabilities(
            runtime_model=runtime_model,
            tool_ids=child_tool_ids,
            skill_ids=child_skill_ids,
            messages=capability_messages,
            runtime=runtime,
            context=context,
        )
        return runtime_model, child_skill_ids, child_tool_ids, capabilities

    async def _model_capabilities(
        self,
        *,
        runtime_model: dict,
        tool_ids: list[str],
        skill_ids: list[str],
        messages: list[dict],
        runtime: RequestRuntime,
        context: InvocationContext,
    ) -> CapabilitySet:
        model_id = str(runtime_model.get("id") or "").strip()
        cached = runtime.cached_capabilities(
            CHILD_RUNTIME_KEY, model_id, tool_ids, skill_ids,
        )
        if cached is not None:
            return cached

        model_info = await Models.get_model_by_id(model_id)
        if model_info is None:
            if tool_ids or skill_ids:
                raise ValueError("Child Model capability owner is unavailable")
            capabilities = CapabilitySet([], [], {})
        else:
            if not model_info.is_active:
                raise ValueError("Child Model capability owner is unavailable")
            capabilities = await self._capabilities.resolve(
                request=context.request,
                capability_owner_id=str(model_info.user_id or ""),
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
        runtime.cache_capabilities(CHILD_RUNTIME_KEY, model_id, capabilities)
        return capabilities

    @staticmethod
    async def _attachments(agent: AgentSpec, runtime_model) -> tuple[list[str], list[str]]:
        model_info = await Models.get_model_by_id(agent.model_id)
        if model_info is None:
            is_pipe = (
                isinstance(runtime_model, dict)
                and isinstance(runtime_model.get("pipe"), dict)
                and runtime_model["pipe"].get("type") == "pipe"
            )
            if not is_pipe:
                raise ValueError(
                    f'Agent "{agent.model_id}" must be a Workspace Model or Pipe'
                )
            return [], []
        if not model_info.is_active:
            raise ValueError(f'Agent "{agent.model_id}" is inactive')

        meta = model_info.meta
        if hasattr(meta, "model_dump"):
            meta = meta.model_dump()
        elif isinstance(meta, dict):
            meta = dict(meta)
        else:
            meta = {}
        return normalize_skill_ids(meta.get("skillIds")), normalize_ids(meta.get("toolIds"))

    @staticmethod
    def _system_prompt(workspace_context: str = "") -> str:
        prompt = (
            "You are the specialist selected by an orchestrator.\n"
            "Execute the delegated task directly and completely.\n"
            "Continue the current assistant response as if the user had asked you directly.\n"
            "Do not tell the user that the task was delegated.\n"
            "Do not discuss routing, handoff, orchestrators, or internal agents.\n"
            "Use your available tools whenever necessary.\n"
            "Follow the supplied Skill instructions. "
        )
        if workspace_context:
            prompt += "\n\n" + workspace_context
        return prompt

    @staticmethod
    def _clear_outer_inference_params(body: dict) -> None:
        for key in tuple(body):
            if key not in CHILD_REQUEST_ENVELOPE_KEYS:
                body.pop(key, None)


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
        debug: bool = Field(default=False, description="Enable debug logs.")

    def __init__(self):
        self.valves = self.Valves()
        self._protocol = HandoffProtocol()
        self._capabilities = ModelCapabilityResolver(McpRuntime())
        self._child_builder = ChildRequestBuilder()
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
        with runtime.preparation():
            runtime.require_router_chain()
            routed = runtime.routed_body(body)
            base_tool_ids = normalize_ids(runtime.metadata.get("lite_base_tool_ids"))
            skill_ids = normalize_skill_ids(runtime.metadata.get("lite_orchestrator_skill_ids"))
            router_model_id = str(runtime.metadata.get("lite_router_model_id") or "").strip()
            runtime_model = context.request.app.state.MODELS.get(router_model_id) or {"id": router_model_id}
            if base_tool_ids or skill_ids:
                capabilities = runtime.cached_capabilities(
                    BASE_RUNTIME_KEY,
                    router_model_id,
                    base_tool_ids,
                    skill_ids,
                )
                if capabilities is None:
                    capabilities = await self._capabilities.resolve(
                        request=context.request,
                        capability_owner_id=str(
                            runtime.metadata.get("lite_router_owner_id") or ""
                        ).strip(),
                        execution_user=context.user,
                        tool_ids=base_tool_ids,
                        skill_ids=skill_ids,
                        runtime_model=runtime_model,
                        metadata=runtime.metadata,
                        messages=routed.get("messages") or [],
                        event_emitter=context.event_emitter,
                        event_call=context.event_call,
                        oauth_token=context.oauth_token,
                        files=context.files,
                        connector=McpRuntime.connect,
                    )
                    runtime.cache_capabilities(
                        BASE_RUNTIME_KEY,
                        router_model_id,
                        capabilities,
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
            async def load_builtin(ids):
                owner = await Users.get_user_by_id(str(runtime.metadata.get("lite_router_owner_id") or "").strip())
                if owner is None:
                    raise ValueError("Model capability owner is unavailable")
                extra_params = self._capabilities._extra_params(
                    request=context.request, execution_user=context.user, runtime_model=runtime_model,
                    metadata=runtime.metadata, messages=messages, event_emitter=context.event_emitter,
                    event_call=context.event_call, oauth_token=context.oauth_token, files=context.files,
                )
                return await get_builtin_tools(
                    context.request,
                    {**extra_params, "__user__": owner.model_dump(), "__skill_ids__": ids},
                    model=runtime_model,
                )

            prepared = await SkillPreparation.prepare(
                skill_ids=skill_ids, runtime_model=runtime_model, metadata=runtime.metadata,
                lookup_skill=Skills.get_skill_by_id, load_builtin=load_builtin,
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

            model_id = self.valves.orchestrator_model_id.strip()
            if not model_id:
                raise ValueError("orchestrator_model_id is not configured")
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
        with runtime.preparation():
            runtime.require_router_chain()
            routed, agent = await self._child_builder.prepare(
                body=body,
                marker=marker,
                registry=registry,
                runtime=runtime,
                context=context,
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
        user_id = (__user__ or {}).get("id")
        if not user_id:
            raise ValueError("Missing user id")
        user = await Users.get_user_by_id(user_id)
        if user is None:
            raise ValueError("User not found")

        runtime = RequestRuntime(__request__, __metadata__)
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
