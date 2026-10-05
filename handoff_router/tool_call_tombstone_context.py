"""
title: Tool Call Tombstone Context
description: Trims completed turns while retaining minimal historical Tool Call IDs.
version: 0.1.0
required_open_webui_version: 0.11.1
"""

from __future__ import annotations

import copy
import logging

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

TOOL_IMAGE_TEXT = "Here are the images from the tool results above. Please analyze them."
TOMBSTONE_NOTICE = "Historical Tool Call IDs retained for continuity."
TOMBSTONE_RESULT = "[omitted]"
APPLIED_KEY = "tool_call_tombstone_context_applied"


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


def is_tombstone_message(message: dict) -> bool:
    return (
        message.get("role") == "assistant"
        and message.get("content") == TOMBSTONE_NOTICE
        and bool(message.get("tool_calls"))
    )


def user_indices(messages: list[dict]) -> list[int]:
    return [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "user" and not is_tool_image_message(message)
    ]


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=-20,
            description="Run before prompt/context filters that consume trimmed history.",
        )
        history_turns: int = Field(
            default=5,
            ge=0,
            description="Completed previous user/assistant turns to retain as text.",
        )
        debug: bool = Field(default=False, description="Enable debug logs.")

    def __init__(self):
        self.valves = self.Valves()

    def _debug(self, message: str, *args) -> None:
        if self.valves.debug:
            log.warning("[TOOL_CALL_TOMBSTONE_CONTEXT] " + message, *args)

    @staticmethod
    def _strip_existing_tombstones(messages: list[dict]) -> list[dict]:
        tombstone_ids = {
            call.get("id")
            for message in messages
            if is_tombstone_message(message)
            for call in message.get("tool_calls") or []
            if call.get("id")
        }
        return [
            message
            for message in messages
            if not is_tombstone_message(message)
            and not (
                message.get("role") == "tool"
                and message.get("tool_call_id") in tombstone_ids
                and message.get("content") == TOMBSTONE_RESULT
            )
        ]

    @staticmethod
    def _is_completed_segment(messages: list[dict], start: int, end: int) -> bool:
        return any(
            message.get("role") == "assistant"
            and message.get("content")
            and not message.get("tool_calls")
            for message in messages[start:end]
        )

    @staticmethod
    def _text_turn(messages: list[dict], start: int, end: int) -> list[dict]:
        segment = messages[start:end]
        if not segment:
            return []
        selected = [
            {
                "role": "user",
                "content": copy.deepcopy(segment[0].get("content")),
            }
        ]
        final_answer = next(
            (
                message
                for message in reversed(segment)
                if message.get("role") == "assistant"
                and message.get("content")
                and not message.get("tool_calls")
            ),
            None,
        )
        if final_answer is not None:
            selected.append(
                {
                    "role": "assistant",
                    "content": copy.deepcopy(final_answer.get("content")),
                }
            )
        return selected

    @staticmethod
    def _current_call_ids(messages: list[dict]) -> set[str]:
        return {
            call.get("id")
            for message in messages
            if message.get("role") == "assistant"
            for call in message.get("tool_calls") or []
            if call.get("id")
        }

    @staticmethod
    def _historical_calls(
        messages: list[dict], excluded_ids: set[str]
    ) -> list[dict]:
        result_ids = {
            message.get("tool_call_id")
            for message in messages
            if message.get("role") == "tool" and message.get("tool_call_id")
        }
        selected: list[dict] = []
        seen_ids: set[str] = set()
        for message in messages:
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or []:
                call_id = call.get("id")
                function = call.get("function")
                name = function.get("name") if isinstance(function, dict) else None
                if (
                    not call_id
                    or not name
                    or call_id not in result_ids
                    or call_id in excluded_ids
                    or call_id in seen_ids
                ):
                    continue
                seen_ids.add(call_id)
                selected.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": "{}"},
                    }
                )
        return selected

    @staticmethod
    def _tombstone_block(calls: list[dict]) -> list[dict]:
        if not calls:
            return []
        return [
            {
                "role": "assistant",
                "content": TOMBSTONE_NOTICE,
                "tool_calls": copy.deepcopy(calls),
            },
            *[
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": TOMBSTONE_RESULT,
                }
                for call in calls
            ],
        ]

    async def inlet(self, body: dict) -> dict:
        metadata = body.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise TypeError("Tool Call Tombstone Context metadata must be an object")
        raw_messages = body.get("messages")
        if not isinstance(raw_messages, list):
            raise TypeError("Tool Call Tombstone Context messages must be a list")
        if metadata.get(APPLIED_KEY):
            return body

        messages = self._strip_existing_tombstones(raw_messages)
        indices = user_indices(messages)
        if not indices:
            body["messages"] = copy.deepcopy(messages)
            metadata[APPLIED_KEY] = True
            return body

        current_user = indices[-1]
        historical_ranges = [
            (start, end)
            for start, end in zip(indices[:-1], indices[1:])
            if self._is_completed_segment(messages, start, end)
        ]
        retained_ranges = (
            historical_ranges[-self.valves.history_turns :]
            if self.valves.history_turns
            else []
        )

        system_messages = [
            copy.deepcopy(message)
            for message in messages[:current_user]
            if message.get("role") == "system"
        ]
        current = copy.deepcopy(messages[current_user:])
        calls = self._historical_calls(
            messages[:current_user], self._current_call_ids(current)
        )
        history = [
            item
            for start, end in retained_ranges
            for item in self._text_turn(messages, start, end)
        ]
        body["messages"] = [
            *system_messages,
            *self._tombstone_block(calls),
            *history,
            *current,
        ]
        metadata[APPLIED_KEY] = True
        self._debug(
            "turns=%s tombstones=%s before=%s after=%s",
            self.valves.history_turns,
            len(calls),
            len(raw_messages),
            len(body["messages"]),
        )
        return body
