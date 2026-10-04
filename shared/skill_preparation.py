"""Authoritative Skill preparation, embedded into independently uploaded Functions."""

import copy
import html
from dataclasses import dataclass
from typing import Any

from shared.request_runtime import SkillLoaderOwnership


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
