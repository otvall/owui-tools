"""Authoritative Skill preparation, embedded into independently uploaded Functions."""

import copy
import html
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

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


@dataclass(frozen=True, kw_only=True)
class SkillBuiltinInvocation:
    profile: Literal["orchestrator", "child", "standalone"]
    request: Any
    runtime_model: dict
    metadata: dict
    event_emitter: Any = None
    event_call: Any = None
    oauth_token: Any = None
    messages: list[dict] | None = None
    files: Any = None


class BuiltinSkillLoader:
    """Load fresh OWUI builtins behind SkillPreparation's load_builtin seam.

    The caller selects the user; resolve_user runs only when load is called.
    Router profiles require their stable history list and history adapter.
    Standalone retains its smaller injection set and OWUI's native binding.
    This module neither checks Skill eligibility nor caches loaded Tools.
    """

    def __init__(
        self,
        *,
        invocation: SkillBuiltinInvocation,
        get_builtin_tools: Callable[..., Awaitable[dict]],
        resolve_user: Callable[[], Awaitable[dict]],
        bind_history: Callable[[dict, list[dict]], dict] | None = None,
    ):
        if invocation.profile not in ("orchestrator", "child", "standalone"):
            raise ValueError(f"Unknown builtin Skill profile: {invocation.profile}")
        if invocation.profile == "standalone":
            if bind_history is not None:
                raise ValueError("Standalone Skill loading cannot bind Router history")
        elif invocation.messages is None or bind_history is None:
            raise ValueError("Router Skill loading requires Tool history and its adapter")
        self._invocation = invocation
        self._get_builtin_tools = get_builtin_tools
        self._resolve_user = resolve_user
        self._bind_history = bind_history

    async def load(self, skill_ids: list[str]) -> dict:
        invocation = self._invocation
        metadata = invocation.metadata
        user = await self._resolve_user()
        extra_params = {
            "__user__": user,
            "__metadata__": metadata,
            "__model__": invocation.runtime_model,
            "__event_emitter__": invocation.event_emitter,
            "__event_call__": invocation.event_call,
            "__oauth_token__": invocation.oauth_token,
            "__chat_id__": metadata.get("chat_id"),
            "__message_id__": metadata.get("message_id"),
            "__skill_ids__": skill_ids,
        }
        options = {"model": invocation.runtime_model}
        if invocation.profile != "orchestrator":
            options["features"] = metadata.get("features", {})
        if invocation.profile != "standalone":
            extra_params.update({
                "__request__": invocation.request,
                "__session_id__": metadata.get("session_id"),
                "__messages__": invocation.messages,
                "__files__": invocation.files or metadata.get("files", []),
                "__features__": metadata.get("features", {}),
            })
        tools = await self._get_builtin_tools(invocation.request, extra_params, **options)
        if self._bind_history is not None:
            # Constructor validation guarantees history for the Router profiles.
            assert invocation.messages is not None
            tools = self._bind_history(tools, invocation.messages)
        return tools


@dataclass(frozen=True)
class PreparedSkills:
    ids: list[str]
    context: str
    loader: dict | None


class SkillPreparation:
    CONTEXT_PREFIX = "Skill context:\n"
    LEGACY_CONTEXT_PREFIX = "Lite orchestrator Skill context:\n"

    @staticmethod
    def install_context(prepared: PreparedSkills, body: dict, runtime_model: dict) -> None:
        """Replace the managed Skill context while preserving administrator instructions."""
        messages = SkillPreparation._remove_previous_context(body["messages"])
        SkillPreparation.install_loader(prepared, body, runtime_model)
        metadata = body["metadata"]
        metadata.setdefault("lite_view_skill_available", False)
        metadata.setdefault("lite_view_skill_model_id", None)
        prompt = prepared.context
        if prepared.loader is not None:
            prompt = (
                "The following Skills are available on demand. Inspect their descriptions "
                "and call view_skill for any Skill that may apply before following its full "
                "instructions.\n\n" + prompt
            )
        body["messages"] = SkillPreparation._append_system_context(messages, prompt)

    @staticmethod
    def _remove_previous_context(messages: list[dict]) -> list[dict]:
        cleaned = []
        for original in messages:
            content = original.get("content")
            if original.get("role") != "system" or not isinstance(content, str):
                cleaned.append(original)
                continue
            before = None
            for prefix in (SkillPreparation.CONTEXT_PREFIX, SkillPreparation.LEGACY_CONTEXT_PREFIX):
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
        block = SkillPreparation.CONTEXT_PREFIX + prompt
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
