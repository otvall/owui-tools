"""
title: Skill Context
description: Builds Skill context and adds the allowlisted view_skill builtin when available.
version: 0.18.0
required_open_webui_version: 0.11.1
"""

from __future__ import annotations

import logging

from open_webui.models.skills import Skills
from open_webui.utils.tools import get_builtin_tools
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

PROMPT_PREFIX = "Skill context:\n"
LEGACY_PROMPT_PREFIX = "Lite orchestrator Skill context:\n"
APPLIED_KEY = "skill_context_applied"
PIPELINE_KEY = "lite_subagent_filter_pipeline"


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

class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=-10,
            description="Run after Tool Call Filter and Subagent Context.",
        )
        debug: bool = Field(default=False, description="Enable debug logs.")

    def __init__(self):
        self.valves = self.Valves()

    def _debug(self, message: str, *args) -> None:
        if self.valves.debug:
            log.warning("[SKILL_CONTEXT] " + message, *args)

    @staticmethod
    def _remove_previous_context(messages: list[dict]) -> list[dict]:
        cleaned = []
        for original in messages:
            content = original.get("content")
            if original.get("role") != "system" or not isinstance(content, str):
                cleaned.append(original)
                continue
            before = None
            for prefix in (PROMPT_PREFIX, LEGACY_PROMPT_PREFIX):
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
        block = PROMPT_PREFIX + prompt
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
    async def _load_skill_builtins(
        *,
        request,
        runtime_model: dict,
        metadata: dict,
        user: dict,
        skill_ids: list[str],
        event_emitter,
        event_call,
        oauth_token,
    ) -> dict:
        return await get_builtin_tools(
            request,
            {
                "__user__": user,
                "__metadata__": metadata,
                "__model__": runtime_model,
                "__event_emitter__": event_emitter,
                "__event_call__": event_call,
                "__oauth_token__": oauth_token,
                "__chat_id__": metadata.get("chat_id"),
                "__message_id__": metadata.get("message_id"),
                "__skill_ids__": skill_ids,
            },
            features=metadata.get("features", {}),
            model=runtime_model,
        )
    async def inlet(
        self,
        body: dict,
        __request__=None,
        __user__=None,
        __model__=None,
        __event_emitter__=None,
        __event_call__=None,
        __oauth_token__=None,
    ) -> dict:
        if __request__ is None:
            raise ValueError("Skill Context requires __request__")
        metadata = body.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise TypeError("Skill Context metadata must be an object")
        messages = body.get("messages")
        if not isinstance(messages, list):
            raise TypeError("Skill Context messages must be a list")

        RequestRuntime(__request__, metadata).before_filter("skill_context")

        messages = self._remove_previous_context(messages)
        model_id = str(body.get("model") or "").strip()
        runtime_model = (
            __model__
            if isinstance(__model__, dict)
            else __request__.app.state.MODELS.get(model_id) or {"id": model_id}
        )
        if metadata.get("lite_subagent_filter_run"):
            # Router has already read the child's current database attachments.
            # An empty selection is authoritative even when runtime metadata is stale.
            skill_ids = metadata.get("lite_target_skill_ids") or []
        else:
            model_meta = (runtime_model.get("info") or {}).get("meta") or {}
            skill_ids = [
                *(body.get("skill_ids") or []),
                *(model_meta.get("skillIds") or []),
                *(metadata.get("lite_orchestrator_skill_ids") or []),
            ]

        async def load_builtin(ids):
            return await self._load_skill_builtins(
                request=__request__, runtime_model=runtime_model, metadata=metadata,
                user=__user__ if isinstance(__user__, dict) else {}, skill_ids=ids,
                event_emitter=__event_emitter__, event_call=__event_call__, oauth_token=__oauth_token__,
            )

        prepared = await SkillPreparation.prepare(
            skill_ids=skill_ids, runtime_model=runtime_model, metadata=metadata,
            lookup_skill=Skills.get_skill_by_id, load_builtin=load_builtin,
        )
        SkillPreparation.install_loader(prepared, body, runtime_model)
        metadata.setdefault("lite_view_skill_available", False)
        metadata.setdefault("lite_view_skill_model_id", None)
        prompt = prepared.context
        if prepared.loader is not None:
            prompt = (
                "The following Skills are available on demand. Inspect their descriptions "
                "and call view_skill for any Skill that may apply before following its full "
                "instructions.\n\n" + prompt
            )
        body["messages"] = self._append_system_context(messages, prompt)
        RequestRuntime(__request__, metadata).finish_filter("skill_context")
        self._debug("model=%s Skill count=%s", model_id, len(prepared.ids))
        return body
