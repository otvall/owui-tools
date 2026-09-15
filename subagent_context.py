"""
title: Subagent Context
description: Limits previous user/assistant turns and Tool calls for a model.
version: 0.18.0
required_open_webui_version: 0.11.1
"""

from __future__ import annotations

import copy
import logging

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

TOOL_IMAGE_TEXT = "Here are the images from the tool results above. Please analyze them."
APPLIED_KEY = "subagent_context_applied"
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
            description="Run after Tool Call Filter and before Skill Context.",
        )
        history_turns: int = Field(
            default=0,
            ge=0,
            description="Completed previous user/assistant turns to retain.",
        )
        history_tool_calls: int = Field(
            default=0,
            ge=0,
            description="Maximum completed Tool calls inside retained previous turns.",
        )
        debug: bool = Field(default=False, description="Enable debug logs.")

    def __init__(self):
        self.valves = self.Valves()

    def _debug(self, message: str, *args) -> None:
        if self.valves.debug:
            log.warning("[SUBAGENT_CONTEXT] " + message, *args)

    @staticmethod
    def _historical_segment(messages: list[dict], start: int, end: int) -> list[dict]:
        return messages[start:end]

    @staticmethod
    def _is_completed_segment(messages: list[dict], start: int, end: int) -> bool:
        return any(
            message.get("role") == "assistant"
            and message.get("content")
            and not message.get("tool_calls")
            for message in messages[start:end]
        )

    @staticmethod
    def _select_segment(segment: list[dict], tool_limit_ids: set[str]) -> list[dict]:
        selected: dict[int, dict] = {}
        if segment and segment[0].get("role") == "user":
            selected[0] = copy.deepcopy(segment[0])

        final_answer = next(
            (
                index
                for index in range(len(segment) - 1, -1, -1)
                if segment[index].get("role") == "assistant"
                and segment[index].get("content")
                and not segment[index].get("tool_calls")
            ),
            -1,
        )
        if final_answer >= 0:
            selected[final_answer] = {
                "role": "assistant",
                "content": copy.deepcopy(segment[final_answer].get("content")),
            }

        for index, message in enumerate(segment):
            if message.get("role") == "assistant" and message.get("tool_calls"):
                kept = [
                    copy.deepcopy(call)
                    for call in message.get("tool_calls") or []
                    if call.get("id") in tool_limit_ids
                ]
                if kept:
                    selected[index] = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": kept,
                    }
            elif (
                message.get("role") == "tool"
                and message.get("tool_call_id") in tool_limit_ids
            ):
                selected[index] = copy.deepcopy(message)
        return [selected[index] for index in sorted(selected)]

    async def inlet(self, body: dict) -> dict:
        metadata = body.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise TypeError("Subagent Context metadata must be an object")
        messages = body.get("messages")
        if not isinstance(messages, list):
            raise TypeError("Subagent Context messages must be a list")
        if metadata.get("lite_subagent_filter_run") and not metadata.get(
            "tool_call_filter_applied"
        ):
            raise ValueError("Tool Call Filter must run before Subagent Context")

        indices = user_indices(messages)
        if not indices:
            body["messages"] = list(messages)
        else:
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
            historical_calls = [
                call.get("id")
                for start, end in retained_ranges
                for message in messages[start:end]
                if message.get("role") == "assistant"
                for call in message.get("tool_calls") or []
                if call.get("id")
            ]
            retained_call_ids = set(
                historical_calls[-self.valves.history_tool_calls :]
                if self.valves.history_tool_calls
                else []
            )
            system_messages = [
                copy.deepcopy(message)
                for message in messages[:current_user]
                if message.get("role") == "system"
            ]
            history = [
                item
                for start, end in retained_ranges
                for item in self._select_segment(
                    self._historical_segment(messages, start, end),
                    retained_call_ids,
                )
            ]
            current = copy.deepcopy(messages[current_user:])
            body["messages"] = [*system_messages, *history, *current]

        metadata[APPLIED_KEY] = True
        if metadata.get("lite_subagent_filter_run"):
            metadata.setdefault(PIPELINE_KEY, []).append("subagent_context")
        self._debug(
            "turns=%s tools=%s before=%s after=%s",
            self.valves.history_turns,
            self.valves.history_tool_calls,
            len(messages),
            len(body["messages"]),
        )
        return body
