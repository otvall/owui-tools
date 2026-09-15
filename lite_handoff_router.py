"""
title: Lite Handoff Router
description: Stateless same-response subagent handoff router.
version: 0.16.0
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
from open_webui.utils.misc import remove_system_message
from open_webui.utils.tools import get_builtin_tools, get_tools
from pydantic import BaseModel, Field
from starlette.responses import Response, StreamingResponse

log = logging.getLogger(__name__)

DELEGATE_VERSION = "v2"
LEGACY_DELEGATE_VERSION = "v1"
ACTIVE_HANDOFF_KEY = "lite_active_handoff"
CHILD_RUNTIME_KEY = "lite_active_tool_runtime"
BASE_RUNTIME_KEY = "lite_base_tool_runtime"
PREVIOUS_TOOL_CONTEXT_PREFIX = "Previous request execution record (reference data):\n"
TOOL_IMAGE_TEXT = "Here are the images from the tool results above. Please analyze them."
OUTER_INFERENCE_PARAMS = (
    "temperature",
    "top_p",
    "min_p",
    "max_tokens",
    "max_completion_tokens",
    "frequency_penalty",
    "presence_penalty",
    "reasoning_effort",
    "seed",
    "stop",
    "logit_bias",
    "response_format",
    "options",
    "think",
    "keep_alive",
    "previous_response_id",
)


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

        if not isinstance(data, dict) or data.get("__lite_delegate__") not in {
            DELEGATE_VERSION,
            LEGACY_DELEGATE_VERSION,
        }:
            return None
        agent_id = str(data.get("agent_id") or data.get("skill_id") or "").strip()
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
class CapabilitySet:
    tool_ids: list[str]
    skill_ids: list[str]
    tools: dict[str, dict]
    skill_manifest: str = ""


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
    def model_for_marker(marker: HandoffMarker | None, registry: dict[str, AgentSpec]) -> str | None:
        if marker is None:
            return None
        spec = registry.get(marker.agent_id) or next(
            (spec for spec in registry.values() if spec.routing_skill_id == marker.agent_id), None,
        )
        return spec.model_id if spec else None

    @staticmethod
    def scoped_history(
        messages: list[dict], *, agent: AgentSpec, registry: dict[str, AgentSpec],
        allowed_tools: set[str], skill_ids: list[str], user_index: int,
        marker_index: int, history_turns: int, history_tool_calls: int,
    ) -> list[dict]:
        """Select independent text/tool memory; keep the current execution intact.

        Persisted delegate receipts establish tool ownership. Names alone are
        insufficient: different agents can expose identically named tools.
        Unknown ownership is excluded from historical tool memory.
        """
        selected: dict[int, dict] = {}
        past = messages[:user_index]
        pairs = []
        pending_user = None
        final_answer = None
        owner = None
        calls = {}
        exchanges = []
        allowed_skills = {item.lower() for item in skill_ids}

        def allowed(call):
            function = call.get("function") or {}
            name = function.get("name")
            if name not in allowed_tools:
                return False
            if name == "view_skill":
                try:
                    args = function.get("arguments") or {}
                    if isinstance(args, str):
                        args = json.loads(args)
                    return str(args.get("id") or "").lower() in allowed_skills
                except (ValueError, TypeError, AttributeError):
                    return False
            return True

        for index, message in enumerate(past):
            role = message.get("role")
            if role == "user":
                owner = None
                if pending_user is not None and final_answer is not None:
                    pairs.append((pending_user, final_answer))
                pending_user, final_answer = index, None
            elif role == "assistant":
                if message.get("tool_calls"):
                    for position, call in enumerate(message["tool_calls"]):
                        if call.get("id"):
                            calls[call["id"]] = (index, position, call)
                elif message.get("content") and not MessageHistory.is_previous_tool_context_message(message):
                    final_answer = index
            elif role == "tool":
                entry = calls.get(message.get("tool_call_id"))
                marker = HandoffMarker.parse(message.get("content"))
                if marker is not None and entry is not None and (
                    entry[2].get("function") or {}
                ).get("name") == "lite_delegate":
                    owner = HandoffProtocol.model_for_marker(marker, registry)
                    continue
                if entry is not None:
                    call_index, position, call = entry
                    # OWUI may batch calls from both sides of a handoff into
                    # one assistant message. Receipt order establishes the
                    # owner; the assistant message's position does not.
                    if owner == agent.model_id and allowed(call):
                        exchanges.append((call_index, position, call, index))
        if pending_user is not None and final_answer is not None:
            pairs.append((pending_user, final_answer))
        for pair in pairs[-history_turns:] if history_turns else []:
            for index in pair:
                selected[index] = {"role": past[index]["role"], "content": past[index]["content"]}
        exchanges.sort(key=lambda item: (item[0], item[1]))
        for call_index, _position, call, result_index in exchanges[-history_tool_calls:] if history_tool_calls else []:
            assistant = selected.setdefault(call_index, {"role": "assistant", "content": ""})
            assistant.setdefault("tool_calls", []).append(call)
            selected[result_index] = past[result_index]

        current_start = max(marker_index, user_index) + 1
        current = copy.deepcopy(messages[current_start:])
        completed = {m.get("tool_call_id") for m in current if m.get("role") == "tool"}

        # convert_output_to_messages in OWUI 0.11.1 can put lite_delegate
        # and child calls in the SAME assistant.tool_calls array, before the
        # delegate receipt. Recover only calls whose results follow that
        # receipt; never copy the router's text, reasoning or Skill results.
        recovered_calls = [
            copy.deepcopy(call)
            for message in messages[max(user_index + 1, 0):current_start]
            if message.get("role") == "assistant"
            for call in message.get("tool_calls") or []
            if call.get("id") and call["id"] in completed and allowed(call)
        ]
        if recovered_calls:
            current.insert(0, {"role": "assistant", "content": "", "tool_calls": recovered_calls})

        # Keep complete, allowlisted pairs, including recovered calls.
        accepted = set()
        for message in current:
            for call in message.get("tool_calls") or []:
                if allowed(call) and call.get("id") and call["id"] in completed:
                    accepted.add(call["id"])
            if message.get("tool_calls"):
                message["tool_calls"] = [c for c in message["tool_calls"] if c.get("id") in accepted]
                if not message["tool_calls"]:
                    message.pop("tool_calls")
        current = [m for m in current
                   if not (m.get("role") == "tool" and m.get("tool_call_id") not in accepted)
                   and not (m.get("role") == "assistant" and not HandoffProtocol._has_visible_assistant_content(m))]
        result = [selected[index] for index in sorted(selected)]
        if 0 <= user_index < len(messages):
            result.append(messages[user_index])
        return result + current

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
    def strip_exchange(messages: list[dict]) -> list[dict]:
        delegate_call_ids = HandoffProtocol._delegate_call_ids(messages)
        cleaned = []
        for original in messages:
            if HandoffProtocol._is_delegate_result(original, delegate_call_ids):
                continue
            message = HandoffProtocol._clean_assistant_message(original)
            if message is not None:
                cleaned.append(message)
        return cleaned

    @staticmethod
    def child_history(messages: list[dict], allowed_tool_names: set[str]) -> list[dict]:
        """Remove the Router turn and retain only the selected child's Tools.

        A current handoff marker divides the Router's private execution trace
        from child continuation messages. The marker itself and every message
        after the latest user input but before that marker are omitted.
        """

        exchange = HandoffProtocol._current_exchange(messages)
        if exchange is None:
            candidate_messages = messages
        else:
            _marker, marker_index, last_user_index = exchange
            candidate_messages = [
                *messages[: last_user_index + 1],
                *messages[marker_index + 1 :],
            ]
        return HandoffProtocol._filter_tool_history(
            candidate_messages,
            allowed_tool_names,
        )

    @staticmethod
    def _filter_tool_history(messages: list[dict], allowed_tool_names: set[str]) -> list[dict]:
        allowed_names = {str(name or "").strip() for name in allowed_tool_names}
        removed_call_ids = set()
        cleaned = []
        for original in messages:
            if original.get("role") == "assistant" and original.get("tool_calls"):
                message = dict(original)
                kept_calls = []
                for call in original["tool_calls"]:
                    name = str((call.get("function") or {}).get("name") or "").strip()
                    if name in allowed_names:
                        kept_calls.append(call)
                    elif call.get("id"):
                        removed_call_ids.add(call["id"])
                if kept_calls:
                    message["tool_calls"] = kept_calls
                else:
                    message.pop("tool_calls", None)
                if HandoffProtocol._has_visible_assistant_content(message):
                    cleaned.append(message)
                continue

            if (
                original.get("role") == "tool"
                and original.get("tool_call_id") in removed_call_ids
            ):
                continue
            cleaned.append(original)
        return cleaned

    @staticmethod
    def _has_visible_assistant_content(message: dict) -> bool:
        content = message.get("content")
        has_content = bool(content.strip()) if isinstance(content, str) else bool(content)
        return bool(
            has_content
            or message.get("tool_calls")
            or message.get("reasoning_content")
            or message.get("thinking")
        )

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

    @staticmethod
    def _is_delegate_result(message: dict, delegate_call_ids: set[str]) -> bool:
        return message.get("role") == "tool" and (
            HandoffMarker.parse(message.get("content")) is not None
            or message.get("tool_call_id") in delegate_call_ids
        )

    @staticmethod
    def _clean_assistant_message(message: dict) -> dict | None:
        cleaned = dict(message)
        if cleaned.get("role") != "assistant" or not cleaned.get("tool_calls"):
            return cleaned
        kept = [
            call
            for call in cleaned["tool_calls"]
            if (call.get("function") or {}).get("name") != "lite_delegate"
        ]
        if kept:
            cleaned["tool_calls"] = kept
        else:
            cleaned.pop("tool_calls", None)
        if HandoffProtocol._has_visible_assistant_content(cleaned):
            return cleaned
        return None


class MessageHistory:
    @staticmethod
    def is_previous_tool_context_message(message: dict) -> bool:
        content = message.get("content")
        return isinstance(content, str) and content.startswith(PREVIOUS_TOOL_CONTEXT_PREFIX)

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


class RequestRuntime:
    """Own the shared, request-scoped metadata and live Tool registries."""

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
        request_metadata = self.request_metadata
        if request_metadata is not None and request_metadata is not self.metadata:
            request_metadata.update(values)

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
            and isinstance(cache.get("skill_manifest"), str)
        ):
            return CapabilitySet(
                tool_ids=list(tool_ids),
                skill_ids=list(skill_ids),
                tools=cache["tools"],
                skill_manifest=cache["skill_manifest"],
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
                    "skill_manifest": capabilities.skill_manifest,
                }
            }
        )

    # Compatibility adapters used by installed-runtime characterization.
    def cached_tools(
        self,
        cache_key: str,
        model_id: str,
        tool_ids: list[str],
        skill_ids: list[str] | None = None,
    ) -> dict | None:
        capabilities = self.cached_capabilities(
            cache_key,
            model_id,
            tool_ids,
            normalize_ids(skill_ids),
        )
        return capabilities.tools if capabilities else None

    def cache_tools(
        self,
        cache_key: str,
        model_id: str,
        tool_ids: list[str],
        tools: dict,
        skill_ids: list[str] | None = None,
        skill_manifest: str = "",
    ) -> None:
        self.cache_capabilities(
            cache_key,
            model_id,
            CapabilitySet(
                tool_ids=list(tool_ids),
                skill_ids=normalize_ids(skill_ids),
                tools=tools,
                skill_manifest=skill_manifest,
            ),
        )

    def activate(self, marker: HandoffMarker, agent_id: str, model_id: str) -> None:
        self.sync(
            lite_active_agent_id=agent_id,
            lite_active_model_id=model_id,
            lite_active_handoff=marker.to_dict(),
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
    def lazy_skills(runtime_model: dict) -> bool:
        meta = (runtime_model or {}).get("info", {}).get("meta", {}) or {}
        return (meta.get("capabilities") or {}).get("builtin_tools", True) is not False

    @staticmethod
    async def full_skill_context(skill_ids: list[str]) -> str:
        blocks = []
        for skill_id in normalize_ids(skill_ids):
            skill = await Skills.get_skill_by_id(skill_id)
            if skill is None or not skill.is_active:
                raise ValueError(f"Attached model-bound Skill is unavailable: {skill_id}")
            blocks.append(
                f'<skill id="{html.escape(skill_id, quote=True)}" '
                f'name="{html.escape(str(skill.name or skill_id), quote=True)}">\n'
                f'{skill.content}\n</skill>'
            )
        return "\n\n".join(blocks)

    @staticmethod
    async def skill_manifest(skill_ids: list[str]) -> str:
        entries = []
        missing = []
        for skill_id in normalize_ids(skill_ids):
            skill = await Skills.get_skill_by_id(skill_id)
            if skill is None or not skill.is_active:
                missing.append(skill_id)
            else:
                entries.append(
                    "<skill>\n"
                    f"<id>{html.escape(skill_id)}</id>\n"
                    f"<name>{html.escape(str(skill.name or skill_id))}</name>\n"
                    f"<description>{html.escape(str(skill.description or ''))}</description>\n"
                    "</skill>"
                )
        if missing:
            raise ValueError(
                "Attached model-bound Skills are unavailable: " + ", ".join(missing)
            )
        if not entries:
            return ""
        return "<available_skills>\n" + "\n".join(entries) + "\n</available_skills>"

    @classmethod
    async def resolve_builtin_skill_tool(
        cls,
        *,
        request,
        owner,
        skill_ids: list[str],
        runtime_model,
        extra_params: dict,
    ) -> dict | None:
        allowed_ids = frozenset(skill_id.lower() for skill_id in normalize_ids(skill_ids))
        if not allowed_ids:
            return None

        owner_params = {
            **extra_params,
            "__user__": owner.model_dump(),
            "__skill_ids__": list(skill_ids),
        }
        builtin_tools = await get_builtin_tools(
            request,
            owner_params,
            model=runtime_model,
        )
        builtin = builtin_tools.get(cls.SKILL_TOOL_NAME)
        if builtin is None:
            raise RuntimeError("Open WebUI builtin view_skill is unavailable")

        builtin_callable = builtin["callable"]

        async def allowlisted_view_skill(id: str):
            requested_id = str(id or "").strip().lower()
            if requested_id not in allowed_ids:
                return json.dumps(
                    {"error": "Skill is not available in the current model context"},
                    ensure_ascii=False,
                )
            return await builtin_callable(id=requested_id)

        return {**builtin, "callable": allowlisted_view_skill}

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
    ) -> CapabilitySet:
        requested_ids = normalize_ids(tool_ids)
        requested_skill_ids = normalize_ids(skill_ids)
        if not requested_ids and not requested_skill_ids:
            return CapabilitySet([], [], {})

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

        lazy_skills = self.lazy_skills(runtime_model)
        skill_manifest = await (
            self.skill_manifest(requested_skill_ids)
            if lazy_skills else self.full_skill_context(requested_skill_ids)
        )
        if requested_skill_ids and lazy_skills:
            if self.SKILL_TOOL_NAME in tools:
                raise ValueError(
                    'Attached Tool name "view_skill" conflicts with the builtin Skill loader'
                )
            skill_tool = await self.resolve_builtin_skill_tool(
                request=request,
                owner=owner,
                skill_ids=requested_skill_ids,
                runtime_model=runtime_model,
                extra_params=extra_params,
            )
            if skill_tool is not None:
                tools[self.SKILL_TOOL_NAME] = skill_tool
        return CapabilitySet(
            requested_ids,
            requested_skill_ids,
            tools,
            skill_manifest,
        )

    async def resolve_tools(self, **kwargs) -> CapabilitySet:
        return await self.resolve(**kwargs, skill_ids=[])

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
        }


class ChildRequestBuilder:
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
        load_capabilities: Callable,
        history_turns: int = 0,
        history_tool_calls: int = 0,
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

        runtime_model = context.request.app.state.MODELS.get(agent.model_id)
        if runtime_model is None:
            raise ValueError(f'Agent "{agent.model_id}" is unavailable')
        child_skill_ids, child_tool_ids = await self._attachments(agent, runtime_model)

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
        capabilities = await load_capabilities(
            request=context.request,
            user=context.user,
            runtime_model=runtime_model,
            metadata=runtime.metadata,
            tool_ids=child_tool_ids,
            skill_ids=child_skill_ids,
            messages=capability_messages,
            event_emitter=context.event_emitter,
            event_call=context.event_call,
            oauth_token=context.oauth_token,
            files=context.files,
        )
        # OWUI rebuilds and regroups the trace on each tool continuation.
        # Resolve the receipt again: cached message offsets are not stable.
        delegate_call_ids = HandoffProtocol._delegate_call_ids(source_messages)
        marker_index = next(
            (index for index in range(len(source_messages) - 1, -1, -1)
             if source_messages[index].get("role") == "tool"
             and source_messages[index].get("tool_call_id") in delegate_call_ids
             and HandoffProtocol.model_for_marker(
                 HandoffMarker.parse(source_messages[index].get("content")), registry,
             ) == agent.model_id),
            -1,
        )
        before = marker_index if marker_index >= 0 else len(source_messages)
        user_index = MessageHistory.last_user_index(source_messages[:before])
        runtime.sync(lite_history_boundary={
            "model_id": agent.model_id, "user_index": user_index, "marker_index": marker_index,
        })
        child_messages = HandoffProtocol.scoped_history(
            source_messages, agent=agent, registry=registry,
            allowed_tools=set(capabilities.tools), skill_ids=child_skill_ids,
            user_index=user_index, marker_index=marker_index,
            history_turns=history_turns, history_tool_calls=history_tool_calls,
        )
        child_messages.insert(
            0,
            {
                "role": "system",
                "content": self._system_prompt(marker, capabilities.skill_manifest),
            },
        )
        capability_messages[:] = copy.deepcopy(child_messages)

        routed_body["messages"] = child_messages
        routed_body["model"] = agent.model_id
        self.clear_outer_inference_params(routed_body)
        runtime.bind_tools(capabilities.tools, child_tool_ids, replace=True)
        runtime.sync(skill_ids=child_skill_ids)
        runtime.activate(marker, marker.agent_id, agent.model_id)
        routed_body["tools"] = [
            {"type": "function", "function": tool["spec"]}
            for tool in capabilities.tools.values()
        ]
        routed_body.pop("tool_choice", None)
        return routed_body, agent

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
        return normalize_ids(meta.get("skillIds")), normalize_ids(meta.get("toolIds"))

    @staticmethod
    def _system_prompt(_marker: HandoffMarker, skill_manifest: str) -> str:
        prompt = (
            "You are the specialist selected by an orchestrator.\n"
            "Execute the delegated task directly and completely.\n"
            "Continue the current assistant response as if the user had asked you directly.\n"
            "Do not tell the user that the task was delegated.\n"
            "Do not discuss routing, handoff, orchestrators, or internal agents.\n"
            "Use your available tools whenever necessary.\n"
            "Follow the supplied Skill instructions. "
        )
        if "<available_skills>" in skill_manifest:
            prompt += (
                "When an available Skill may apply, call view_skill to load its full "
                "instructions before completing the task."
            )
        if skill_manifest:
            prompt += "\n\n" + skill_manifest
        return prompt

    @staticmethod
    def clear_outer_inference_params(body: dict) -> None:
        for key in OUTER_INFERENCE_PARAMS:
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


class PipeAdapters:
    class Valves(BaseModel):
        history_turns: int = Field(
            default=0, ge=0,
            description="Previous user/assistant pairs supplied at handoff; 0 means none.",
        )
        history_tool_calls: int = Field(
            default=0, ge=0,
            description="Previous calls and results from the selected agent; 0 means none. Current execution is always retained.",
        )
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
        self._mcp = McpRuntime()
        self._capabilities = ModelCapabilityResolver(self._mcp)
        self._child_builder = ChildRequestBuilder()
        self._gateway = CompletionGateway()

    def _debug(self, message: str, *args) -> None:
        if self.valves.debug:
            log.warning("[LITE_ROUTER] " + message, *args)

    # Compatibility adapters for installed-runtime characterization.
    @staticmethod
    def _json_object(value: Any) -> dict | None:
        marker = HandoffMarker.parse(value)
        return marker.to_dict() if marker else None

    @staticmethod
    def _agent_registry(metadata: dict) -> dict[str, dict]:
        return {
            skill_id: {"model_id": spec.model_id, "name": spec.name}
            for skill_id, spec in ChildRequestBuilder.agent_registry(metadata).items()
        }

    def _find_current_handoff(self, messages: list[dict]) -> dict | None:
        marker = self._protocol.find_current(messages)
        return marker.to_dict() if marker else None

    @staticmethod
    def _active_handoff(metadata: dict) -> dict | None:
        marker = HandoffProtocol.active(metadata)
        return marker.to_dict() if marker else None

    def _strip_delegate_exchange(self, messages: list[dict]) -> list[dict]:
        return self._protocol.strip_exchange(messages)

    async def _load_child_skill_manifest(self, *, user=None, skill_ids: list[str]) -> str:
        return await self._capabilities.skill_manifest(skill_ids)

    @staticmethod
    def _clear_outer_inference_params(body: dict) -> None:
        ChildRequestBuilder.clear_outer_inference_params(body)

    @staticmethod
    def _copy_request_body(body: dict) -> dict:
        return RequestRuntime.copy_body(body)

    @staticmethod
    async def _connect_mcp_server(request, server_id, user, metadata, extra_params):
        return await McpRuntime.connect(request, server_id, user, metadata, extra_params)

    @staticmethod
    def _register_mcp_client(metadata: dict, server_id: str, client) -> None:
        McpRuntime.register_client(metadata, server_id, client)

    @staticmethod
    def _mcp_tool_callable(client, function_name: str):
        return McpRuntime.tool_callable(client, function_name)

    async def _get_model_bound_tools(self, **kwargs) -> dict:
        capabilities = await self._capabilities.resolve_tools(
            **kwargs,
            connector=self._connect_mcp_server,
        )
        return capabilities.tools

    async def _get_model_bound_capabilities(self, **kwargs) -> CapabilitySet:
        return await self._capabilities.resolve(
            **kwargs,
            connector=self._connect_mcp_server,
        )

    async def _resolve_child_tools(self, **kwargs) -> dict:
        if not normalize_ids(kwargs["tool_ids"]):
            return {}
        runtime_model = kwargs["runtime_model"]
        target_model_id = str((runtime_model or {}).get("id") or "").strip()
        model_info = await Models.get_model_by_id(target_model_id)
        if model_info is None or not model_info.is_active:
            raise ValueError("Child Model capability owner is unavailable")
        return await self._get_model_bound_tools(
            request=kwargs["request"],
            capability_owner_id=str(model_info.user_id or ""),
            execution_user=kwargs["user"],
            tool_ids=kwargs["tool_ids"],
            runtime_model=runtime_model,
            metadata=kwargs["metadata"],
            messages=kwargs["messages"],
            event_emitter=kwargs["event_emitter"],
            event_call=kwargs["event_call"],
            oauth_token=kwargs["oauth_token"],
            files=kwargs["files"],
        )

    async def _resolve_child_capabilities(self, **kwargs) -> CapabilitySet:
        if not normalize_ids(kwargs["tool_ids"]) and not normalize_ids(kwargs["skill_ids"]):
            return CapabilitySet([], [], {})
        runtime_model = kwargs["runtime_model"]
        target_model_id = str((runtime_model or {}).get("id") or "").strip()
        model_info = await Models.get_model_by_id(target_model_id)
        if model_info is None or not model_info.is_active:
            raise ValueError("Child Model capability owner is unavailable")
        return await self._get_model_bound_capabilities(
            request=kwargs["request"],
            capability_owner_id=str(model_info.user_id or ""),
            execution_user=kwargs["user"],
            tool_ids=kwargs["tool_ids"],
            skill_ids=kwargs["skill_ids"],
            runtime_model=runtime_model,
            metadata=kwargs["metadata"],
            messages=kwargs["messages"],
            event_emitter=kwargs["event_emitter"],
            event_call=kwargs["event_call"],
            oauth_token=kwargs["oauth_token"],
            files=kwargs["files"],
        )

    async def _get_or_create_child_capabilities(self, **kwargs) -> CapabilitySet:
        tool_ids = normalize_ids(kwargs["tool_ids"])
        skill_ids = normalize_ids(kwargs["skill_ids"])
        model_id = str((kwargs["runtime_model"] or {}).get("id") or "").strip()
        runtime = RequestRuntime(kwargs["request"], kwargs["metadata"])
        cached = runtime.cached_capabilities(
            CHILD_RUNTIME_KEY,
            model_id,
            tool_ids,
            skill_ids,
        )
        if cached is not None:
            return cached
        capabilities = await self._resolve_child_capabilities(
            **{
                **kwargs,
                "tool_ids": tool_ids,
                "skill_ids": skill_ids,
            }
        )
        runtime.cache_capabilities(CHILD_RUNTIME_KEY, model_id, capabilities)
        return capabilities

    async def _get_or_create_child_tools(self, **kwargs) -> dict:
        tool_ids = normalize_ids(kwargs["tool_ids"])
        model_id = str((kwargs["runtime_model"] or {}).get("id") or "").strip()
        runtime = RequestRuntime(kwargs["request"], kwargs["metadata"])
        cached = runtime.cached_tools(CHILD_RUNTIME_KEY, model_id, tool_ids)
        if cached is not None:
            return cached
        tools = await self._resolve_child_tools(**{**kwargs, "tool_ids": tool_ids})
        runtime.cache_tools(CHILD_RUNTIME_KEY, model_id, tool_ids, tools)
        return tools

    async def _prepare_child(self, **kwargs):
        marker = HandoffMarker.parse(kwargs["handoff"])
        if marker is None:
            raise ValueError("Invalid handoff marker")
        registry = {
            agent_id: AgentSpec(
                config["model_id"],
                config["name"],
                str(config.get("routing_skill_id") or "").strip(),
            )
            for agent_id, config in kwargs["registry"].items()
        }
        context = InvocationContext(
            request=kwargs["request"],
            user=kwargs["user"],
            event_emitter=kwargs["event_emitter"],
            event_call=kwargs["event_call"],
            oauth_token=kwargs["oauth_token"],
            files=kwargs["files"],
        )
        body, agent = await self._child_builder.prepare(
            body=kwargs["body"],
            marker=marker,
            registry=registry,
            runtime=RequestRuntime(kwargs["request"], kwargs["metadata"]),
            context=context,
            load_capabilities=self._get_or_create_child_capabilities,
            history_turns=self.valves.history_turns,
            history_tool_calls=self.valves.history_tool_calls,
        )
        return body, agent.name, agent.model_id

    @staticmethod
    def _response_error_text(response: Response) -> str:
        return CompletionGateway.response_error_text(response)

    async def _generate(self, **kwargs):
        return await self._gateway.generate(**kwargs)

    @staticmethod
    def _append_unique(current, additions) -> list[str]:
        return normalize_ids([*(current or []), *(additions or [])])

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


class Pipe(PipeAdapters):
    async def _orchestrator_branch(
        self,
        body: dict,
        runtime: RequestRuntime,
        context: InvocationContext,
    ):
        routed = runtime.routed_body(body)
        missing_filters = [
            name
            for name, key in (
                ("Previous Tool Context", "previous_tool_context_applied"),
                ("History Cleanup", "history_cleanup_applied"),
                ("Lite Orchestrator Skills", "lite_orchestrator_skills_applied"),
            )
            if not runtime.metadata.get(key)
        ]
        if missing_filters:
            raise ValueError(
                "Required Router filters are missing or out of order: "
                + ", ".join(missing_filters)
            )
        base_tool_ids = normalize_ids(runtime.metadata.get("lite_base_tool_ids"))
        skill_ids = normalize_ids(runtime.metadata.get("lite_orchestrator_skill_ids"))
        if base_tool_ids or skill_ids:
            router_model_id = str(runtime.metadata.get("lite_router_model_id") or "").strip()
            capabilities = runtime.cached_capabilities(
                BASE_RUNTIME_KEY,
                router_model_id,
                base_tool_ids,
                skill_ids,
            )
            if capabilities is None:
                runtime_model = context.request.app.state.MODELS.get(router_model_id) or {
                    "id": router_model_id
                }
                capabilities = await self._get_model_bound_capabilities(
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

        model_id = self.valves.orchestrator_model_id.strip()
        if not model_id:
            raise ValueError("orchestrator_model_id is not configured")
        routed["model"] = model_id
        self._debug("-> orchestrator %s", model_id)
        return await self._generate(request=context.request, body=routed, user=context.user)

    async def _child_branch(
        self,
        body: dict,
        marker: HandoffMarker,
        registry: dict[str, AgentSpec],
        runtime: RequestRuntime,
        context: InvocationContext,
    ):
        routed, agent_name, model_id = await self._prepare_child(
            body=body,
            handoff=marker.to_dict(),
            registry={
                agent_id: {
                    "model_id": spec.model_id,
                    "name": spec.name,
                    "routing_skill_id": spec.routing_skill_id,
                }
                for agent_id, spec in registry.items()
            },
            request=context.request,
            user=context.user,
            metadata=runtime.metadata,
            event_emitter=context.event_emitter,
            event_call=context.event_call,
            oauth_token=context.oauth_token,
            files=context.files,
        )
        if self.valves.emit_handoff_status and context.event_emitter:
            try:
                await context.event_emitter(
                    {
                        "type": "status",
                        "data": {
                            "action": "lite_delegate",
                            "description": f"delegate to {agent_name}",
                            "done": True,
                        },
                    }
                )
            except Exception as exc:  # noqa: BLE001 - status events are best effort
                self._debug("status emit failed: %s", exc)
        self._debug("-> child %s", model_id)
        self._debug(
            "child tool results: incoming=%s outgoing=%s",
            [m.get("tool_call_id") for m in body.get("messages", []) if m.get("role") == "tool"],
            [m.get("tool_call_id") for m in routed["messages"] if m.get("role") == "tool"],
        )
        return await self._generate(request=context.request, body=routed, user=context.user)

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
