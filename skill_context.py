"""
title: Skill Context
description: Injects a dynamic Skill prompt for any model.
version: 0.16.5
required_open_webui_version: 0.11.1
"""

from __future__ import annotations

import html
import logging

from open_webui.models.skills import Skills
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

PROMPT_PREFIX = "Skill context:\n"
LEGACY_PROMPT_PREFIX = "Lite orchestrator Skill context:\n"
APPLIED_KEY = "skill_context_applied"


def normalize_ids(values) -> list[str]:
    result = []
    seen = set()
    for raw_value in values or []:
        value = str(raw_value or "").strip()
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def lazy_skills(runtime_model: dict) -> bool:
    meta = (runtime_model or {}).get("info", {}).get("meta", {}) or {}
    return (meta.get("capabilities") or {}).get("builtin_tools", True) is not False


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
            "Attached orchestrator Skills are unavailable: " + ", ".join(missing)
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
            default=-70,
            description="Filter execution order; lower values run first.",
        )
        debug: bool = Field(default=False, description="Enable debug logs.")

    def __init__(self):
        self.valves = self.Valves()

    def _debug(self, message: str, *args) -> None:
        if self.valves.debug:
            log.warning("[SKILL_CONTEXT] " + message, *args)

    async def inlet(self, body: dict, __request__=None) -> dict:
        if __request__ is None:
            raise ValueError("Skill Context requires __request__")
        metadata = body.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise TypeError("Skill Context metadata must be an object")
        messages = body.get("messages")
        if not isinstance(messages, list):
            raise TypeError("Skill Context messages must be a list")

        messages = [
            message
            for message in messages
            if not (
                isinstance(message.get("content"), str)
                and message["content"].startswith(
                    (PROMPT_PREFIX, LEGACY_PROMPT_PREFIX)
                )
            )
        ]
        model_id = str(body.get("model") or "").strip()
        runtime_model = __request__.app.state.MODELS.get(model_id) or {
            "id": model_id
        }
        model_meta = (runtime_model.get("info") or {}).get("meta") or {}
        skill_ids = normalize_ids(
            [
                *(body.get("skill_ids") or []),
                *(model_meta.get("skillIds") or []),
                *(metadata.get("lite_orchestrator_skill_ids") or []),
            ]
        )
        prompt = await skill_context(skill_ids, lazy=lazy_skills(runtime_model))
        if prompt:
            messages.insert(
                0,
                {"role": "system", "content": PROMPT_PREFIX + prompt},
            )
        body["messages"] = messages
        metadata[APPLIED_KEY] = True
        self._debug("model=%s Skill count=%s", model_id, len(skill_ids))
        return body
