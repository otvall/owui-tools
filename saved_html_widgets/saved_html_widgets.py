"""
title: Saved HTML Widgets
description: List and read original HTML from completed earlier responses in the current request branch.
version: 1.1.0
required_open_webui_version: 0.11.1
"""

import hashlib
import json
import logging
import re
from html.parser import HTMLParser
from typing import Any, NoReturn

from pydantic import BaseModel, Field

from open_webui.env import ENABLE_ADMIN_CHAT_ACCESS
from open_webui.internal.db import get_async_db_context
from open_webui.models.access_grants import AccessGrants
from open_webui.models.chat_messages import ChatMessages
from open_webui.models.chats import Chat, is_internal_chat
from open_webui.models.folders import Folders
from open_webui.models.users import Users
from open_webui.utils.access_control.folders import has_folder_access
from open_webui.utils.chat_id import is_saved_chat_id


log = logging.getLogger(__name__)


class WidgetError(Exception):
    def __init__(self, code: str, message: str):
        self.result = {"status": "error", "code": code, "message": message}


def fail(code: str, message: str) -> NoReturn:
    raise WidgetError(code, message)


async def accessible_saved_chat(chat_id: str, user: Any) -> Any:
    """Read-only counterpart of v0.11.1 Chats.get_chat_by_id_for_user.

    That getter repairs currentId/null bytes and may commit on read. Use the
    same owner/admin/share/folder policy with a raw snapshot to preserve HTML
    and avoid persistent repairs. The platform grant/folder APIs remain the
    source of their own access decisions.
    """
    async with get_async_db_context() as db:
        chat = await db.get(Chat, chat_id)
        if chat is None:
            return None
        if chat.user_id == user.id:
            return chat
        if user.role == "admin" and (ENABLE_ADMIN_CHAT_ACCESS or is_internal_chat(chat.meta)):
            return chat
        if await AccessGrants.has_access(
            user_id=user.id, resource_type="shared_chat", resource_id=chat_id, permission="read", db=db,
        ):
            return chat
        if chat.folder_id:
            folder = await Folders.get_folder_by_id(chat.folder_id, db=db)
            if folder and await has_folder_access(user.id, folder, "read", db):
                return chat
        return None


async def saved_branch(metadata: dict | None, execution_user: dict | None) -> tuple[str, list[dict]]:
    if not isinstance(metadata, dict) or not isinstance(metadata.get("chat_id"), str) or not is_saved_chat_id(metadata["chat_id"]):
        fail("CHAT_CONTEXT_MISSING", "Open WebUI did not supply a saved chat for this request.")
    if not isinstance(execution_user, dict) or not execution_user.get("id"):
        fail("USER_CONTEXT_MISSING", "Open WebUI did not supply the Execution user.")
    user = await Users.get_user_by_id(execution_user["id"])
    if user is None:
        fail("ACCESS_DENIED", "The current Execution user cannot read this chat.")
    chat_id = metadata["chat_id"]
    chat = await accessible_saved_chat(chat_id, user)
    if chat is None:
        fail("ACCESS_DENIED", "The saved chat is missing or unavailable to the current Execution user.")
    messages = await ChatMessages.get_messages_map_by_chat_id(chat_id)
    if messages is None:
        messages = (chat.chat.get("history") or {}).get("messages")
    if not isinstance(messages, dict):
        fail("BRANCH_CONTEXT_INVALID", "The saved chat has no readable message ancestry.")
    request_id = metadata.get("user_message_id")
    if not isinstance(request_id, str) or not request_id:
        fail("BRANCH_CONTEXT_MISSING", "Open WebUI did not supply the originating user message identifier.")
    request = messages.get(request_id)
    if not isinstance(request, dict) or request.get("id") != request_id or request.get("role") != "user":
        fail("BRANCH_CONTEXT_INVALID", "The originating user request is not present in this saved chat.")
    injected_request = metadata.get("user_message")
    if injected_request is not None and (
        not isinstance(injected_request, dict)
        or injected_request.get("id") != request_id
        or injected_request.get("role") != "user"
        or injected_request.get("parentId") != request.get("parentId")
    ):
        fail("BRANCH_CONTEXT_INVALID", "The saved and injected originating user requests disagree.")
    current_id = metadata.get("message_id")
    if current_id is not None:
        if not isinstance(current_id, str) or not current_id or current_id == request_id:
            fail("BRANCH_CONTEXT_INVALID", "The current assistant response identifier is inconsistent with this request.")
        current = messages.get(current_id)
        if current is not None and (
            not isinstance(current, dict) or current.get("id") != current_id
            or current.get("role") != "assistant" or current.get("parentId") != request_id
        ):
            fail("BRANCH_CONTEXT_INVALID", "The saved current response is not a child of the originating user request.")
    seen = {request_id}
    branch = []
    parent_id = request.get("parentId")
    while parent_id is not None:
        if not isinstance(parent_id, str) or not parent_id or parent_id in seen:
            fail("BRANCH_CONTEXT_INVALID", "The originating request has inconsistent saved ancestry.")
        message = messages.get(parent_id)
        if not isinstance(message, dict) or message.get("id") != parent_id:
            fail("BRANCH_CONTEXT_INVALID", "A parent of the originating request is missing from this saved chat.")
        seen.add(parent_id)
        branch.append(message)
        parent_id = message.get("parentId")
    if branch and branch[-1].get("role") != "user":
        fail("BRANCH_CONTEXT_INVALID", "Saved message ancestry does not end at a root user message.")
    return chat_id, branch


class SavedTitleParser(HTMLParser):
    """Extract optional document metadata without changing its source HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.parts: list[str] = []
        self.title: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title" and self.title is None:
            self.in_title = True
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self.in_title:
            self.title = "".join(self.parts).strip() or None
            self.in_title = False


def saved_widgets(chat_id: str, branch: list[dict]) -> list[dict]:
    widgets = []
    for message in branch:
        if message.get("role") != "assistant" or message.get("done") is not True:
            continue
        sources: list[tuple[str, int | None, Any, str | None]] = [("message", None, message.get("embeds"), None)]
        outputs = message.get("output")
        if isinstance(outputs, list):
            for output_index, output in enumerate(outputs):
                if not isinstance(output, dict) or output.get("type") != "function_call_output":
                    continue
                call_id = output.get("call_id")
                calls = [
                    item for item in outputs if isinstance(item, dict)
                    and item.get("type") == "function_call" and item.get("call_id") == call_id
                ] if isinstance(call_id, str) and call_id else []
                producer = calls[0].get("name") if len(calls) == 1 else None
                if not isinstance(producer, str) or not producer.strip():
                    producer = None
                sources.append(("output", output_index, output.get("embeds"), producer))
        for source_kind, source_index, embeds, producer in sources:
            if not isinstance(embeds, list):
                continue
            for embed_index, html in enumerate(embeds):
                if not isinstance(html, str) or not html:
                    continue
                if re.match(
                    r"^(?:[a-z][a-z0-9+.-]*://|//|(?:data|mailto|javascript|blob|tel|file|about|urn):)"
                    r"|^[a-z][a-z0-9+.-]*:[^\s<>]*$", html.strip(), re.IGNORECASE,
                ):
                    continue
                location = [chat_id, message["id"], source_kind, source_index, embed_index, html]
                widget_id = "html_" + hashlib.sha256(json.dumps(location, ensure_ascii=False).encode("utf-8")).hexdigest()
                widget = {"widget_id": widget_id, "message_id": message["id"],
                          "html_bytes": len(html.encode("utf-8")), "html": html}
                title_parser = SavedTitleParser()
                try:
                    title_parser.feed(html)
                except Exception:
                    # Parser behavior varies by Python version for malformed
                    # declarations. Optional metadata must not hide saved HTML.
                    pass
                else:
                    if title_parser.title is not None:
                        widget["title"] = title_parser.title
                if producer is not None:
                    widget["producer_operation"] = producer
                widgets.append(widget)
    return widgets


class Tools:
    class Valves(BaseModel):
        LIST_PAGE_SIZE: int = Field(default=50, ge=1, description="Maximum widgets returned per discovery page.")
        MAX_HTML_BYTES: int = Field(default=0, ge=0, description="Maximum full HTML size in UTF-8 bytes; 0 means unlimited.")

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def list_saved_html_widgets(self, continuation: str | None = None,
                                      __metadata__: dict | None = None, __user__: dict | None = None,
                                      __messages__: list | None = None) -> dict:
        """List saved HTML metadata newest first. Continue while has_more is true.

        :param continuation: Copy next_continuation from the preceding page; omit for the first page.
        """
        try:
            chat_id, branch = await saved_branch(__metadata__, __user__)
            widgets = [
                {key: value for key, value in widget.items() if key != "html"}
                for widget in saved_widgets(chat_id, branch)
            ]
            page_size = self.valves.LIST_PAGE_SIZE
            scope = [chat_id, (__metadata__ or {}).get("user_message_id"), page_size, widgets]
            snapshot = hashlib.sha256(json.dumps(scope, ensure_ascii=False).encode("utf-8")).hexdigest()
            offset = 0
            if continuation is not None:
                match = re.fullmatch(r"page_([1-9][0-9]{0,19})_([0-9a-f]{64})", continuation) if isinstance(continuation, str) else None
                if match is None:
                    fail("INVALID_CONTINUATION", "Use next_continuation from this request's preceding page, or restart discovery.")
                offset = int(match[1])
                if match[2] != snapshot or offset >= len(widgets) or offset % page_size:
                    fail("INVALID_CONTINUATION", "The catalog or request changed, or the page is invalid. Restart discovery.")
            end = offset + page_size
            has_more = end < len(widgets)
            return {"status": "success", "widgets": widgets[offset:end], "has_more": has_more,
                    "next_continuation": f"page_{end}_{snapshot}" if has_more else None}
        except WidgetError as exc:
            return exc.result
        except Exception:
            log.exception("Saved HTML discovery failed")
            return {"status": "error", "code": "STORAGE_UNAVAILABLE",
                    "message": "Saved HTML could not be read from Open WebUI storage. Retry after checking the server."}

    async def read_saved_html_widget(self, widget_id: str, __metadata__: dict | None = None,
                                     __user__: dict | None = None, __messages__: list | None = None) -> dict:
        """Return the complete original HTML of a widget selected from list_saved_html_widgets.

        :param widget_id: Exact identifier returned by list_saved_html_widgets in this chat and request branch.
        """
        try:
            chat_id, branch = await saved_branch(__metadata__, __user__)
            for widget in saved_widgets(chat_id, branch):
                if widget["widget_id"] == widget_id:
                    limit = self.valves.MAX_HTML_BYTES
                    if limit and widget["html_bytes"] > limit:
                        fail("WIDGET_TOO_LARGE", f"The saved HTML is {widget['html_bytes']} UTF-8 bytes, exceeding the configured {limit}-byte limit. Ask the administrator to raise MAX_HTML_BYTES; no HTML was returned.")
                    return {"status": "success", "widget_id": widget_id, "html": widget["html"]}
            fail("WIDGET_UNAVAILABLE", "The selected widget is absent from the accessible completed ancestor responses.")
        except WidgetError as exc:
            return exc.result
        except Exception:
            log.exception("Saved HTML reading failed")
            return {"status": "error", "code": "STORAGE_UNAVAILABLE",
                    "message": "Saved HTML could not be read from Open WebUI storage. Retry after checking the server."}
