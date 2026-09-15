"""
title: Tool Call Filter
description: Removes Tool calls that the destination model cannot execute.
version: 0.18.0
required_open_webui_version: 0.11.1
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

DELEGATE_VERSION = "v2"
TOOL_IMAGE_TEXT = "Here are the images from the tool results above. Please analyze them."
APPLIED_KEY = "tool_call_filter_applied"
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


def last_user_index(messages: list[dict]) -> int:
    return next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].get("role") == "user"
            and not is_tool_image_message(messages[index])
        ),
        -1,
    )


def parse_delegate_marker(value) -> str | None:
    if isinstance(value, dict):
        data = value
    elif isinstance(value, str) and value.strip():
        try:
            data = json.loads(value)
            if isinstance(data, str):
                data = json.loads(data)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    else:
        return None
    if not isinstance(data, dict) or data.get("__lite_delegate__") != DELEGATE_VERSION:
        return None
    agent_id = str(data.get("agent_id") or "").strip()
    return agent_id or None


def visible_assistant(message: dict) -> bool:
    content = message.get("content")
    return bool(
        (content.strip() if isinstance(content, str) else content)
        or message.get("tool_calls")
        or message.get("reasoning_content")
        or message.get("thinking")
    )


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=-30,
            description="Run before Subagent Context and Skill Context.",
        )
        debug: bool = Field(default=False, description="Enable debug logs.")

    def __init__(self):
        self.valves = self.Valves()

    def _debug(self, message: str, *args) -> None:
        if self.valves.debug:
            log.warning("[TOOL_CALL_FILTER] " + message, *args)

    @staticmethod
    def _allowed_tool_names(body: dict, metadata: dict) -> set[str]:
        names = {
            str(((schema or {}).get("function") or {}).get("name") or "").strip()
            for schema in body.get("tools") or []
        }
        names.update(str(name or "").strip() for name in (metadata.get("tools") or {}))

        if (
            metadata.get("lite_view_skill_available")
            and metadata.get("lite_view_skill_model_id")
            == metadata.get("lite_target_model_id")
        ):
            names.add("view_skill")
        return {name for name in names if name}

    @staticmethod
    def _strip_router_exchange(messages: list[dict], target_agent_id: str) -> list[dict]:
        current_user = last_user_index(messages)
        if current_user < 0 or not target_agent_id:
            return list(messages)
        delegate_ids = {
            call.get("id")
            for message in messages[current_user + 1 :]
            if message.get("role") == "assistant"
            for call in message.get("tool_calls") or []
            if (call.get("function") or {}).get("name") == "lite_delegate"
            and call.get("id")
        }
        marker_index = next(
            (
                index
                for index in range(len(messages) - 1, current_user, -1)
                if messages[index].get("role") == "tool"
                and messages[index].get("tool_call_id") in delegate_ids
                and parse_delegate_marker(messages[index].get("content"))
                == target_agent_id
            ),
            -1,
        )
        if marker_index < 0:
            return list(messages)
        after_marker = messages[marker_index + 1 :]
        completed_after_marker = {
            message.get("tool_call_id")
            for message in after_marker
            if message.get("role") == "tool" and message.get("tool_call_id")
        }
        recovered_calls = [
            call
            for message in messages[current_user + 1 : marker_index]
            if message.get("role") == "assistant"
            for call in message.get("tool_calls") or []
            if call.get("id") in completed_after_marker
        ]
        recovered = (
            [{"role": "assistant", "content": "", "tool_calls": recovered_calls}]
            if recovered_calls
            else []
        )
        return [*messages[: current_user + 1], *recovered, *after_marker]

    @staticmethod
    def _keep_supported_pairs(messages: list[dict], allowed_names: set[str]) -> list[dict]:
        results = {
            message.get("tool_call_id")
            for message in messages
            if message.get("role") == "tool" and message.get("tool_call_id")
        }
        accepted = {
            call.get("id")
            for message in messages
            if message.get("role") == "assistant"
            for call in message.get("tool_calls") or []
            if call.get("id") in results
            and str((call.get("function") or {}).get("name") or "").strip()
            in allowed_names
        }
        cleaned = []
        for original in messages:
            if original.get("role") == "assistant" and original.get("tool_calls"):
                message = dict(original)
                message["tool_calls"] = [
                    call
                    for call in original.get("tool_calls") or []
                    if call.get("id") in accepted
                ]
                if not message["tool_calls"]:
                    message.pop("tool_calls", None)
                    message.pop("reasoning_items", None)
                if visible_assistant(message):
                    cleaned.append(message)
            elif original.get("role") != "tool" or original.get("tool_call_id") in accepted:
                cleaned.append(original)
        return cleaned

    async def inlet(self, body: dict) -> dict:
        metadata = body.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise TypeError("Tool Call Filter metadata must be an object")
        messages = body.get("messages")
        if not isinstance(messages, list):
            raise TypeError("Tool Call Filter messages must be a list")
        allowed_names = self._allowed_tool_names(body, metadata)
        messages = self._strip_router_exchange(
            messages,
            str(metadata.get("lite_target_agent_id") or "").strip(),
        )
        body["messages"] = self._keep_supported_pairs(messages, allowed_names)
        metadata[APPLIED_KEY] = True
        if metadata.get("lite_subagent_filter_run"):
            metadata.setdefault(PIPELINE_KEY, []).append("tool_call_filter")
        self._debug(
            "allowed=%s messages before=%s after=%s",
            sorted(allowed_names),
            len(messages),
            len(body["messages"]),
        )
        return body
