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
        owned = metadata.get("lite_skill_loader")
        current = tools.get("view_skill")
        owns_current = (
            isinstance(owned, dict) and isinstance(current, dict)
            and current.get("callable") is owned.get("callable")
            and current.get("spec") == owned.get("spec")
        )
        schemas = list(body.get("tools") or [])

        def owned_schema(schema):
            return (
                isinstance(owned, dict) and (current is None or owns_current)
                and schema.get("function") == owned.get("spec")
            )

        if prepared.loader is not None and (
            (current is not None and not owns_current)
            or any(
                (schema.get("function") or {}).get("name") == "view_skill"
                and not owned_schema(schema)
                for schema in schemas
            )
        ):
            raise ValueError('Attached Tool name "view_skill" conflicts with the builtin Skill loader')
        if owns_current:
            tools.pop("view_skill", None)
        schemas = [schema for schema in schemas if not owned_schema(schema)]
        metadata.pop("lite_skill_loader", None)
        if prepared.loader is not None:
            tools["view_skill"] = prepared.loader
            schemas.append({"type": "function", "function": prepared.loader["spec"]})
            metadata["lite_skill_loader"] = {
                "callable": prepared.loader["callable"], "spec": copy.deepcopy(prepared.loader["spec"]),
            }
        if schemas or "tools" in body:
            body["tools"] = schemas
        if prepared.ids or owned is not None or "lite_view_skill_available" in metadata:
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

        if metadata.get("lite_subagent_filter_run"):
            pipeline = metadata.get(PIPELINE_KEY) or []
            if pipeline[-2:] != ["tool_call_filter", "subagent_context"]:
                raise ValueError(
                    "Tool Call Filter and Subagent Context must run before Skill Context"
                )

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
        skill_ids = [
            *(body.get("skill_ids") or []),
            *(model_meta.get("skillIds") or []),
            *contextual_skill_ids,
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
        metadata[APPLIED_KEY] = True
        if metadata.get("lite_subagent_filter_run"):
            metadata.setdefault(PIPELINE_KEY, []).append("skill_context")
        self._debug("model=%s Skill count=%s", model_id, len(prepared.ids))
        return body
