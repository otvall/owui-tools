"""
title: Skill Context
description: Builds Skill context and adds the allowlisted view_skill builtin when available.
version: 0.18.0
required_open_webui_version: 0.11.1
"""

from __future__ import annotations

import html
import logging

from open_webui.models.skills import Skills
from open_webui.utils.tools import get_builtin_tools
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

PROMPT_PREFIX = "Skill context:\n"
LEGACY_PROMPT_PREFIX = "Lite orchestrator Skill context:\n"
APPLIED_KEY = "skill_context_applied"
PIPELINE_KEY = "lite_subagent_filter_pipeline"


def normalize_ids(values) -> list[str]:
    result = []
    seen = set()
    for raw_value in values or []:
        value = str(raw_value or "").strip()
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def lazy_skills(runtime_model: dict, metadata: dict) -> bool:
    meta = (runtime_model or {}).get("info", {}).get("meta", {}) or {}
    return (
        bool(metadata.get("session_id"))
        and (metadata.get("params") or {}).get("function_calling") != "legacy"
        and (meta.get("capabilities") or {}).get("builtin_tools", True) is not False
    )


async def skill_context(skill_ids: list[str], *, lazy: bool) -> str:
    entries = []
    missing = []
    for skill_id in normalize_ids(skill_ids):
        skill = await Skills.get_skill_by_id(skill_id)
        if skill is None or not skill.is_active:
            missing.append(skill_id)
            continue
        if lazy:
            entries.append(
                "<skill>\n"
                f"<id>{html.escape(skill_id)}</id>\n"
                f"<name>{html.escape(str(skill.name or skill_id))}</name>\n"
                f"<description>{html.escape(str(skill.description or ''))}</description>\n"
                "</skill>"
            )
        else:
            entries.append(
                f'<skill id="{html.escape(skill_id, quote=True)}" '
                f'name="{html.escape(str(skill.name or skill_id), quote=True)}">\n'
                f"{skill.content}\n</skill>"
            )
    if missing:
        raise ValueError(
            "Attached model Skills are unavailable: " + ", ".join(missing)
        )
    if not entries:
        return ""
    if lazy:
        manifest = "<available_skills>\n" + "\n".join(entries) + "\n</available_skills>"
        return (
            "The following Skills are available on demand. Inspect their descriptions "
            "and call view_skill for any Skill that may apply before following its full "
            "instructions."
            "\n\n"
            + manifest
        )
    return "\n\n".join(entries)


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
    def _merge_tool_schema(body: dict, name: str, tool: dict) -> None:
        schemas = list(body.get("tools") or [])
        schemas = [
            schema
            for schema in schemas
            if str(((schema or {}).get("function") or {}).get("name") or "") != name
        ]
        schemas.append({"type": "function", "function": tool["spec"]})
        body["tools"] = schemas

    @staticmethod
    def _remove_previous_view_skill(body: dict, metadata: dict) -> None:
        if not metadata.get("lite_view_skill_available"):
            return
        tools = metadata.get("tools")
        if isinstance(tools, dict):
            tools.pop("view_skill", None)
        if isinstance(body.get("tools"), list):
            body["tools"] = [
                schema
                for schema in body["tools"]
                if str(((schema or {}).get("function") or {}).get("name") or "")
                != "view_skill"
            ]

    @staticmethod
    async def _view_skill_tool(
        *,
        request,
        runtime_model: dict,
        metadata: dict,
        user: dict,
        skill_ids: list[str],
        event_emitter,
        event_call,
        oauth_token,
    ) -> dict | None:
        builtin_tools = await get_builtin_tools(
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
        builtin = builtin_tools.get("view_skill")
        if builtin is None:
            return None
        allowed_ids = frozenset(item.lower() for item in skill_ids)
        builtin_callable = builtin["callable"]

        async def allowlisted_view_skill(id: str):
            requested_id = str(id or "").strip().lower()
            if requested_id not in allowed_ids:
                return '{"error":"Skill is not available in the current model context"}'
            return await builtin_callable(id=requested_id)

        return {**builtin, "callable": allowlisted_view_skill}

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

        if metadata.get("lite_subagent_filter_run"):
            pipeline = metadata.get(PIPELINE_KEY) or []
            if pipeline[-2:] != ["tool_call_filter", "subagent_context"]:
                raise ValueError(
                    "Tool Call Filter and Subagent Context must run before Skill Context"
                )

        self._remove_previous_view_skill(body, metadata)
        messages = self._remove_previous_context(messages)
        model_id = str(body.get("model") or "").strip()
        runtime_model = (
            __model__
            if isinstance(__model__, dict)
            else __request__.app.state.MODELS.get(model_id) or {"id": model_id}
        )
        model_meta = (runtime_model.get("info") or {}).get("meta") or {}
        contextual_skill_ids = (
            metadata.get("lite_target_skill_ids") or []
            if metadata.get("lite_subagent_filter_run")
            else metadata.get("lite_orchestrator_skill_ids") or []
        )
        skill_ids = normalize_ids(
            [
                *(body.get("skill_ids") or []),
                *(model_meta.get("skillIds") or []),
                *contextual_skill_ids,
            ]
        )
        use_view_skill = lazy_skills(runtime_model, metadata)
        view_skill = None
        if skill_ids and use_view_skill:
            view_skill = await self._view_skill_tool(
                request=__request__,
                runtime_model=runtime_model,
                metadata=metadata,
                user=__user__ if isinstance(__user__, dict) else {},
                skill_ids=skill_ids,
                event_emitter=__event_emitter__,
                event_call=__event_call__,
                oauth_token=__oauth_token__,
            )
            use_view_skill = view_skill is not None

        prompt = await skill_context(skill_ids, lazy=use_view_skill)
        body["messages"] = self._append_system_context(messages, prompt)
        if view_skill is not None:
            tools = metadata.get("tools")
            if not isinstance(tools, dict):
                tools = {}
                metadata["tools"] = tools
            tools["view_skill"] = view_skill
            self._merge_tool_schema(body, "view_skill", view_skill)
        metadata["lite_view_skill_available"] = view_skill is not None
        metadata["lite_view_skill_model_id"] = model_id if view_skill is not None else None
        metadata[APPLIED_KEY] = True
        if metadata.get("lite_subagent_filter_run"):
            metadata.setdefault(PIPELINE_KEY, []).append("skill_context")
        self._debug("model=%s Skill count=%s", model_id, len(skill_ids))
        return body
