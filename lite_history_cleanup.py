"""
title: Lite History Cleanup
description: Keeps conversation text and removes historical native Tool messages from model context.
version: 0.16.1
required_open_webui_version: 0.11.1
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

TOOL_IMAGE_TEXT = "Here are the images from the tool results above. Please analyze them."
APPLIED_KEY = "lite_history_cleanup_applied"


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


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=-80,
            description="In the Router chain, run after Lite Previous Tool Context.",
        )
        debug: bool = Field(default=False, description="Enable debug logs.")

    def __init__(self):
        self.valves = self.Valves()

    def _debug(self, message: str, *args) -> None:
        if self.valves.debug:
            log.warning("[LITE_HISTORY_CLEANUP] " + message, *args)

    async def inlet(self, body: dict) -> dict:
        metadata = body.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise TypeError("Lite History Cleanup metadata must be an object")
        messages = body.get("messages")
        if not isinstance(messages, list):
            raise TypeError("Lite History Cleanup messages must be a list")

        current_user = last_user_index(messages)
        current_start = max(current_user, 0)
        historical_text = [
            {"role": message["role"], "content": message.get("content", "")}
            for message in messages[:current_start]
            if message.get("role") in {"system", "user", "assistant"}
            and message.get("content")
            and not is_tool_image_message(message)
        ]
        body["messages"] = [*historical_text, *messages[current_start:]]
        metadata[APPLIED_KEY] = True
        self._debug(
            "messages before=%s after=%s current_user=%s",
            len(messages),
            len(body["messages"]),
            current_user,
        )
        return body
