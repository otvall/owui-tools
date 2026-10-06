"""Saved HTML workflow at the public Tool seam; OWUI boundaries are fixtures."""

import copy
import importlib.util
import json
import sys
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from sqlalchemy import JSON, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Base(DeclarativeBase):
    pass


class PlatformChat(Base):
    __tablename__ = "chat"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String)
    folder_id: Mapped[str | None] = mapped_column(String, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON)
    chat: Mapped[dict] = mapped_column(JSON)


SOURCE_HTML = (
    '<!doctype html>\r\n<html lang="ru"><style>.chart{color:#123}</style>\r\n'
    '<script>const values = [120, 145, 132]; const label = "Продажи \\\"А\\\"";</script>\r\n'
    '<div class="chart">42 → 57</div></html>\r\n'
)


class SavedHtmlWidgetTests(unittest.IsolatedAsyncioTestCase):
    metadata: dict[str, Any]
    messages: dict[str, Any]
    module: Any

    def setUp(self):
        self.user = types.SimpleNamespace(id="execution-user", role="user")
        self.metadata = {"chat_id": "chat-a", "user_message_id": "request", "message_id": "generating"}
        self.messages = {
            "question": {"id": "question", "parentId": None, "role": "user"},
            "chart": {
                "id": "chart", "parentId": "question", "role": "assistant", "done": True,
                "output": [
                    {"type": "function_call", "call_id": "plot-1", "name": "plot_sql_chart", "status": "completed"},
                    {"type": "function_call_output", "call_id": "plot-1", "status": "completed",
                     "output": [{"type": "input_text", "text": '{"status":"success"}'}], "embeds": [SOURCE_HTML]},
                ],
            },
            "request": {"id": "request", "parentId": "chart", "role": "user"},
        }
        self.chat = PlatformChat(
            id="chat-a", user_id="workspace-owner", folder_id=None, meta={},
            chat={"history": {"messages": {}, "currentId": "other-answer"}},
        )
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        self.database = Session(engine, expire_on_commit=False)
        self.addCleanup(self.database.close)
        self.database.add(self.chat)
        self.database.commit()
        self.allowed = True
        self.folder_allowed = False
        self.users = types.SimpleNamespace(get_user_by_id=AsyncMock(side_effect=lambda id: self.user if id == self.user.id else None))
        self.grants = types.SimpleNamespace(has_access=AsyncMock(side_effect=lambda **kwargs: self.allowed and kwargs["user_id"] == self.user.id))
        self.folders = types.SimpleNamespace(get_folder_by_id=AsyncMock(return_value=types.SimpleNamespace(id="folder-a")))
        self.folder_access = AsyncMock(side_effect=lambda user_id, folder, permission, db=None: self.folder_allowed and user_id == self.user.id)
        self.native_messages = types.SimpleNamespace(get_messages_map_by_chat_id=AsyncMock(side_effect=lambda id: copy.deepcopy(self.messages)))

        @asynccontextmanager
        async def database_context():
            yield types.SimpleNamespace(get=AsyncMock(side_effect=self.database.get))

        adapters = {}
        definitions: dict[str, dict[str, Any]] = {
            "open_webui.models.users": {"Users": self.users},
            "open_webui.models.chats": {"Chat": PlatformChat, "is_internal_chat": lambda meta: bool(meta and meta.get("internal") is True)},
            "open_webui.models.chat_messages": {"ChatMessages": self.native_messages},
            "open_webui.internal.db": {"get_async_db_context": database_context},
            "open_webui.env": {"ENABLE_ADMIN_CHAT_ACCESS": False},
            "open_webui.models.access_grants": {"AccessGrants": self.grants},
            "open_webui.models.folders": {"Folders": self.folders},
            "open_webui.utils.access_control.folders": {"has_folder_access": self.folder_access},
            "open_webui.utils.chat_id": {"is_saved_chat_id": lambda id: bool(id) and not id.startswith(("temporary:", "local:", "channel:"))},
        }
        for name, attrs in definitions.items():
            module = types.ModuleType(name)
            module.__dict__.update(attrs)
            adapters[name] = module
        spec = importlib.util.spec_from_file_location(
            "saved_html_widgets_contract", Path(__file__).resolve().parents[1] / "saved_html_widgets.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {**adapters, spec.name: module}):
            spec.loader.exec_module(module)
        self.tool = module.Tools()
        self.module = module

    async def listing(self, metadata=None, user=None, messages=None, continuation=None):
        return await self.tool.list_saved_html_widgets(
            continuation=continuation,
            __metadata__=self.metadata if metadata is None else metadata,
            __user__={"id": self.user.id} if user is None else user,
            __messages__=[] if messages is None else messages,
        )

    async def reading(self, widget_id, metadata=None, user=None):
        return await self.tool.read_saved_html_widget(
            widget_id, __metadata__=self.metadata if metadata is None else metadata,
            __user__={"id": self.user.id} if user is None else user, __messages__=[],
        )

    async def test_later_agent_lists_and_reads_original_html_without_producer_history(self):
        listing = await self.listing(messages=[{"role": "user", "content": "Use the saved chart in slides"}])
        self.assertEqual(listing["status"], "success")
        self.assertEqual(len(listing["widgets"]), 1)
        widget = listing["widgets"][0]
        self.assertEqual(widget["message_id"], "chart")
        self.assertEqual(widget["html_bytes"], 200)
        self.assertNotIn("html", widget)
        read = await self.reading(widget["widget_id"])
        self.assertEqual(read, {"status": "success", "widget_id": widget["widget_id"], "html": SOURCE_HTML})

    async def test_distinct_native_embed_locations_remain_individually_readable(self):
        self.messages["chart"]["output"][1]["embeds"] = [SOURCE_HTML, "<table><tr><td>7</td></tr></table>"]
        self.messages["chart"]["output"].append({
            "type": "function_call_output", "status": "completed", "call_id": "unknown-producer",
            "embeds": [SOURCE_HTML],
        })
        listing = await self.listing()
        widgets = listing["widgets"]
        self.assertEqual(len({widget["widget_id"] for widget in widgets}), 3)
        self.assertEqual([(await self.reading(widget["widget_id"]))["html"] for widget in widgets], [
            SOURCE_HTML, "<table><tr><td>7</td></tr></table>", SOURCE_HTML,
        ])

    async def test_catalog_preserves_branch_order_and_each_mixed_source_location(self):
        self.messages["chart"]["timestamp"] = 9000
        self.messages["chart"]["embeds"] = ["<div>older message</div>"]
        self.messages["middle"] = {"id": "middle", "parentId": "chart", "role": "user"}
        self.messages["recent"] = {
            "id": "recent", "parentId": "middle", "role": "assistant", "done": True,
            "timestamp": 1, "embeds": [SOURCE_HTML, "<svg>diagram</svg>"],
            "output": [
                {"type": "function_call_output", "embeds": [SOURCE_HTML, "<table>42</table>"]},
                {"type": "function_call_output", "embeds": ["<section>another output</section>"]},
            ],
        }
        self.messages["request"]["parentId"] = "recent"
        widgets = (await self.listing())["widgets"]
        self.assertEqual([widget["message_id"] for widget in widgets], ["recent"] * 5 + ["chart"] * 2)
        self.assertEqual(len({widget["widget_id"] for widget in widgets}), 7)
        self.assertEqual([(await self.reading(widget["widget_id"]))["html"] for widget in widgets], [
            SOURCE_HTML, "<svg>diagram</svg>", SOURCE_HTML, "<table>42</table>",
            "<section>another output</section>", "<div>older message</div>", SOURCE_HTML,
        ])

    async def test_optional_metadata_uses_html_titles_and_unambiguous_saved_calls(self):
        titled = '<!doctype html><title>Продажи &amp; прибыль</title><div>42</div>'
        self.messages["chart"]["embeds"] = [titled, '<section><h1>Not a saved title</h1></section>']
        self.messages["chart"]["output"] = [
            {"type": "function_call", "call_id": "known", "name": "plot_sql_chart"},
            {"type": "function_call_output", "call_id": "known", "embeds": [titled]},
            {"type": "function_call_output", "call_id": "missing", "name": "guessed", "embeds": [SOURCE_HTML]},
            {"type": "function_call", "call_id": "ambiguous", "name": "first"},
            {"type": "function_call", "call_id": "ambiguous", "name": "second"},
            {"type": "function_call_output", "call_id": "ambiguous", "embeds": [titled]},
            {"type": "function_call", "call_id": "unnamed", "name": ""},
            {"type": "function_call_output", "call_id": "unnamed", "embeds": [SOURCE_HTML]},
            {"type": "function_call", "name": "no_relationship"},
            {"type": "function_call_output", "embeds": [SOURCE_HTML]},
        ]
        widgets = (await self.listing())["widgets"]
        self.assertEqual([widget.get("title") for widget in widgets], [
            "Продажи & прибыль", None, "Продажи & прибыль", None, "Продажи & прибыль", None, None,
        ])
        self.assertEqual([widget.get("producer_operation") for widget in widgets], [
            None, None, "plot_sql_chart", None, None, None, None,
        ])
        self.assertNotIn("producer_operation", widgets[0])
        self.assertNotIn("title", widgets[1])
        self.assertEqual((await self.reading(widgets[0]["widget_id"]))["html"], titled)

    async def test_bounded_catalog_continues_to_every_widget_and_rejects_invalid_pages(self):
        htmls = ["<div>one</div>", "<table>two</table>", "<svg>three</svg>", "<p>four</p>", SOURCE_HTML]
        self.messages["chart"]["embeds"] = htmls[:-1]
        self.tool.valves.LIST_PAGE_SIZE = 2
        first = await self.listing()
        self.assertTrue(first["has_more"])
        self.assertEqual(len(first["widgets"]), 2)
        continuation = first["next_continuation"]
        widgets = list(first["widgets"])
        page = await self.listing(continuation=continuation)
        widgets.extend(page["widgets"])
        self.assertTrue(page["has_more"])
        last = await self.listing(continuation=page["next_continuation"])
        widgets.extend(last["widgets"])
        self.assertFalse(last["has_more"])
        self.assertIsNone(last["next_continuation"])
        self.assertEqual([(await self.reading(widget["widget_id"]))["html"] for widget in widgets], htmls)
        for invalid in ["", "arbitrary", continuation + "x", 1, True,
                        continuation.replace("page_2_", "page_1_"),
                        continuation.replace("page_2_", "page_99_")]:
            with self.subTest(continuation=invalid):
                result = await self.listing(continuation=invalid)
                self.assertEqual(result["code"], "INVALID_CONTINUATION")
                self.assertNotIn("widgets", result)
        other_request = {"id": "other-request", "parentId": "chart", "role": "user"}
        self.messages["other-request"] = other_request
        self.assertEqual((await self.listing(
            metadata={**self.metadata, "user_message_id": "other-request"}, continuation=continuation,
        ))["code"], "INVALID_CONTINUATION")
        self.messages["chart"]["embeds"].pop()
        self.assertEqual((await self.listing(continuation=continuation))["code"], "INVALID_CONTINUATION")

    async def test_read_byte_limit_returns_all_html_or_a_clear_error(self):
        widget_id = (await self.listing())["widgets"][0]["widget_id"]
        self.tool.valves.MAX_HTML_BYTES = 199
        rejected = await self.reading(widget_id)
        self.assertEqual(rejected["code"], "WIDGET_TOO_LARGE")
        self.assertIn("200", rejected["message"])
        self.assertIn("199", rejected["message"])
        self.assertNotIn("html", rejected)
        self.tool.valves.MAX_HTML_BYTES = 200
        self.assertEqual((await self.reading(widget_id))["html"], SOURCE_HTML)
        self.tool.valves.MAX_HTML_BYTES = 0
        self.assertEqual((await self.reading(widget_id))["html"], SOURCE_HTML)

    async def test_title_parsing_never_hides_unusual_saved_html_fragments(self):
        htmls = [
            '<![bogus]><div>literal saved markup</div>',
            '<script>const text = "<title>not document metadata</title>";</script><div>42</div>',
        ]
        self.messages["chart"]["embeds"] = htmls
        self.messages["chart"]["output"] = []
        listing = await self.listing()
        self.assertEqual(listing["status"], "success")
        self.assertEqual(len(listing["widgets"]), 2)
        self.assertTrue(all("title" not in widget for widget in listing["widgets"]))
        self.assertEqual([(await self.reading(widget["widget_id"]))["html"] for widget in listing["widgets"]], htmls)

    async def test_presentation_agent_reads_real_saved_chart_among_unrelated_widgets(self):
        fixture_path = Path(__file__).resolve().parents[2] / "deployment" / "sql-demo-chat-result.json"
        fixture = json.loads(fixture_path.read_text())
        history = fixture["chat"]["history"]
        chart_id = history["currentId"]
        self.messages = copy.deepcopy(history["messages"])
        original_html = self.messages[chart_id]["output"][3]["embeds"][0]
        self.messages[chart_id]["embeds"] = ["<title>Unrelated table</title><table>9</table>"]
        self.messages["another-question"] = {"id": "another-question", "parentId": chart_id, "role": "user"}
        self.messages["another-answer"] = {
            "id": "another-answer", "parentId": "another-question", "role": "assistant", "done": True,
            "embeds": ["<svg>unrelated recent diagram</svg>"],
            "output": [{"type": "function_call_output", "call_id": "other", "embeds": ["<p>unrelated fragment</p>"]}],
        }
        self.messages["request"] = {"id": "request", "parentId": "another-answer", "role": "user"}
        self.metadata["user_message"] = copy.deepcopy(self.messages["request"])
        self.chat.chat = copy.deepcopy(fixture["chat"])
        self.chat.chat["history"]["messages"] = copy.deepcopy(self.messages)
        self.database.commit()
        original_chat = copy.deepcopy(self.chat.chat)
        for native_storage in [True, False]:
            self.native_messages.get_messages_map_by_chat_id.side_effect = None
            self.native_messages.get_messages_map_by_chat_id.return_value = copy.deepcopy(self.messages) if native_storage else None
            for tool_context in [[], [{"role": "user", "content": "Use my earlier saved chart in HTML slides"}]]:
                with self.subTest(native_storage=native_storage, tool_context=tool_context):
                    listing = await self.listing(messages=tool_context)
                    self.assertEqual([widget["message_id"] for widget in listing["widgets"]], [
                        "another-answer", "another-answer", chart_id, chart_id,
                    ])
                    selected = [widget for widget in listing["widgets"] if widget.get("producer_operation") == "plot_sql_chart"]
                    self.assertEqual(len(selected), 1)
                    self.assertEqual(selected[0]["title"], "SQL → интерактивный график")
                    self.assertEqual(selected[0]["html_bytes"], 7068)
                    self.assertEqual((await self.reading(selected[0]["widget_id"]))["html"], original_html)
        self.database.expire_all()
        saved = self.database.get(PlatformChat, "chat-a")
        assert saved is not None
        self.assertEqual(saved.chat, original_chat)

    async def test_message_embed_reads_revalidate_access_branch_completion_and_saved_location(self):
        self.messages["chart"]["embeds"] = ["<div>message-level original</div>"]
        widget_id = (await self.listing())["widgets"][0]["widget_id"]
        original_messages = copy.deepcopy(self.messages)
        cases = ["access", "branch", "unfinished", "removed", "replacement", "url"]
        for case in cases:
            with self.subTest(change=case):
                self.messages = copy.deepcopy(original_messages)
                self.allowed = case != "access"
                if case == "branch":
                    self.messages["alternative"] = {
                        "id": "alternative", "parentId": "question", "role": "assistant", "done": True,
                        "embeds": ["<div>alternative result must not be revealed</div>"],
                    }
                    self.messages["request"]["parentId"] = "alternative"
                elif case == "unfinished":
                    self.messages["chart"]["done"] = False
                elif case == "removed":
                    del self.messages["chart"]["embeds"]
                elif case == "replacement":
                    self.messages["chart"]["embeds"][0] = "<div>replacement must not be revealed</div>"
                elif case == "url":
                    self.messages["chart"]["embeds"][0] = "https://example.com/replacement"
                result = await self.reading(widget_id)
                self.assertEqual(result["code"], "ACCESS_DENIED" if case == "access" else "WIDGET_UNAVAILABLE")
                self.assertNotIn("html", result)

    async def test_message_embeds_exclude_urls_and_keep_saved_resource_references_unchanged(self):
        original_html = '<title>Table</title>\r\n<link href="https://example.com/table.css"><table><td>42</td></table>'
        self.messages["chart"]["embeds"] = [
            "https://example.com/chart", "//example.com/chart", "data:text/html,<p>remote</p>", original_html,
        ]
        self.messages["chart"]["output"] = []
        with patch("urllib.request.urlopen", side_effect=AssertionError("URL fetching is forbidden")):
            listing = await self.listing()
            self.assertEqual(len(listing["widgets"]), 1)
            self.assertEqual((await self.reading(listing["widgets"][0]["widget_id"]))["html"], original_html)

    async def test_url_prefix_followed_by_markup_remains_readable_as_saved_html(self):
        htmls = [
            "https://example.org <b>Sales 42</b>",
            "Sales link https://example.org <b>Sales 42</b>",
        ]
        self.messages["chart"]["embeds"] = ["https://example.org", htmls[0]]
        self.messages["chart"]["output"][1]["embeds"] = ["https://example.org", *htmls]
        listing = await self.listing()
        self.assertEqual(len(listing["widgets"]), 3)
        self.assertEqual([(await self.reading(widget["widget_id"]))["html"] for widget in listing["widgets"]], [htmls[0], *htmls])

    async def test_url_only_embeds_are_not_downloadable_widgets(self):
        self.messages["chart"]["output"][1]["embeds"] = [
            "https://example.com/chart?x=42", "//example.com/chart", "ftp://example.com/chart.html",
            "mailto:user@example.com", "data:text/html,<div>URL content</div>", "custom:some-resource",
            "<div><img src=\"https://example.com/logo.png\">9</div>",
        ]
        with patch("urllib.request.urlopen", side_effect=AssertionError("URL fetching is forbidden")):
            listing = await self.listing()
            self.assertEqual(len(listing["widgets"]), 1)
            read = await self.reading(listing["widgets"][0]["widget_id"])
            self.assertEqual(read["html"], '<div><img src="https://example.com/logo.png">9</div>')

    async def test_request_anchor_confines_both_operations_despite_misleading_current_id(self):
        original_id = (await self.listing())["widgets"][0]["widget_id"]
        self.messages["alternative"] = {
            "id": "alternative", "parentId": "question", "role": "assistant", "done": True,
            "output": [{"type": "function_call_output", "status": "completed", "embeds": ["<div>other branch</div>"]}],
        }
        self.messages["other-request"] = {"id": "other-request", "parentId": "alternative", "role": "user"}
        alternative_meta = {**self.metadata, "user_message_id": "other-request"}
        alternative_id = (await self.listing(metadata=alternative_meta))["widgets"][0]["widget_id"]
        self.assertEqual([w["widget_id"] for w in (await self.listing())["widgets"]], [original_id])
        self.assertEqual((await self.reading(alternative_id))["code"], "WIDGET_UNAVAILABLE")

    async def test_saved_request_and_injected_request_must_agree(self):
        self.metadata["user_message"] = {"id": "request", "parentId": "question", "role": "user"}
        result = await self.listing()
        self.assertEqual(result["code"], "BRANCH_CONTEXT_INVALID")
        self.assertNotIn("html", result)

    async def test_only_completed_prior_assistant_responses_are_readable(self):
        widget_id = (await self.listing())["widgets"][0]["widget_id"]
        self.messages["chart"]["done"] = False
        self.messages["generating"] = {
            "id": "generating", "parentId": "request", "role": "assistant", "done": False,
            "output": [{"type": "function_call_output", "embeds": ["<div>partial</div>"]}],
        }
        self.assertEqual((await self.listing())["widgets"], [])
        self.assertEqual((await self.reading(widget_id))["code"], "WIDGET_UNAVAILABLE")

    async def test_missing_and_broken_branch_context_do_not_fall_back_to_current_id(self):
        for metadata, parent in [
            ({"chat_id": "chat-a"}, "chart"),
            ({**self.metadata, "user_message_id": "absent"}, "chart"),
            (self.metadata, "missing-parent"),
            (self.metadata, "request"),
        ]:
            with self.subTest(metadata=metadata, parent=parent):
                self.messages["request"]["parentId"] = parent
                result = await self.listing(metadata=metadata)
                self.assertEqual(result["status"], "error")
                self.assertIn(result["code"], {"BRANCH_CONTEXT_MISSING", "BRANCH_CONTEXT_INVALID"})

    async def test_an_assistant_root_is_broken_ancestry(self):
        del self.messages["chart"]["parentId"]
        self.assertEqual((await self.listing())["code"], "BRANCH_CONTEXT_INVALID")

    async def test_access_loss_between_list_and_read_never_uses_model_owner_identity(self):
        widget_id = (await self.listing())["widgets"][0]["widget_id"]
        self.allowed = False
        self.assertEqual((await self.listing())["code"], "ACCESS_DENIED")
        self.assertEqual((await self.reading(widget_id))["code"], "ACCESS_DENIED")

    async def test_platform_failure_is_an_explicit_error_without_html(self):
        self.native_messages.get_messages_map_by_chat_id.side_effect = RuntimeError("private storage diagnostic")
        with self.assertLogs(self.module.log, level="ERROR"):
            result = await self.listing()
        self.assertEqual(result["code"], "STORAGE_UNAVAILABLE")
        self.assertNotIn("private storage diagnostic", str(result))
        self.assertNotIn("html", result)

    async def test_removed_or_changed_widget_is_unavailable_after_listing(self):
        widget_id = (await self.listing())["widgets"][0]["widget_id"]
        self.messages["chart"]["output"][1]["embeds"] = ["<div>replacement result</div>"]
        self.assertEqual((await self.reading(widget_id))["code"], "WIDGET_UNAVAILABLE")
        self.messages["chart"]["output"][1]["embeds"] = []
        self.assertEqual((await self.reading(widget_id))["code"], "WIDGET_UNAVAILABLE")

    async def test_widget_identifier_does_not_grant_access_to_a_different_chat(self):
        widget_id = (await self.listing())["widgets"][0]["widget_id"]
        other_chat = PlatformChat(id="chat-b", user_id=self.user.id, meta={}, chat={})
        self.database.add(other_chat)
        self.database.commit()
        result = await self.reading(widget_id, metadata={**self.metadata, "chat_id": "chat-b"})
        self.assertEqual(result["code"], "WIDGET_UNAVAILABLE")
        self.assertNotIn("html", result)

    async def test_owner_configured_admin_internal_shared_and_folder_policy_paths(self):
        cases = [
            ("execution-user", "user", False, {}, False, None, False, True),
            ("owner", "admin", True, {}, False, None, False, True),
            ("owner", "admin", False, {}, False, None, False, False),
            ("owner", "admin", False, {"internal": True}, False, None, False, True),
            ("owner", "user", False, {"internal": True}, False, None, False, False),
            ("owner", "user", False, {}, True, None, False, True),
            ("owner", "user", False, {}, False, "folder-a", True, True),
            ("owner", "user", False, {}, False, "folder-a", False, False),
        ]
        for owner, role, admin_access, meta, shared, folder, folder_read, allowed in cases:
            with self.subTest(owner=owner, role=role, admin_access=admin_access, meta=meta, shared=shared, folder=folder, folder_read=folder_read):
                self.chat.user_id, self.chat.meta, self.chat.folder_id = owner, meta, folder
                self.user.role = role
                self.module.ENABLE_ADMIN_CHAT_ACCESS = admin_access
                self.allowed, self.folder_allowed = shared, folder_read
                result = await self.listing()
                self.assertEqual(result["status"], "success" if allowed else "error")
                if not allowed:
                    self.assertEqual(result["code"], "ACCESS_DENIED")

    async def test_missing_saved_chat_context_is_distinct_from_empty_discovery(self):
        self.messages["chart"]["output"] = []
        self.assertEqual(await self.listing(), {
            "status": "success", "widgets": [], "has_more": False, "next_continuation": None,
        })
        self.assertEqual((await self.listing(metadata={}))["code"], "CHAT_CONTEXT_MISSING")
        self.assertEqual((await self.listing(metadata={**self.metadata, "chat_id": "absent"}))["code"], "ACCESS_DENIED")

    async def test_legacy_storage_preserves_html_and_chat_without_read_repairs(self):
        original_html = '<div>nul:\u0000</div>\r\n<script>const n=77;</script>'
        self.messages["chart"]["output"][1]["embeds"] = [original_html]
        self.chat.chat = {"history": {"messages": copy.deepcopy(self.messages), "currentId": "missing-global-id"}}
        self.database.commit()
        original_chat = copy.deepcopy(self.chat.chat)
        self.native_messages.get_messages_map_by_chat_id.return_value = None
        self.native_messages.get_messages_map_by_chat_id.side_effect = None
        listing = await self.listing()
        self.assertEqual((await self.reading(listing["widgets"][0]["widget_id"]))["html"], original_html)
        self.database.expire_all()
        saved = self.database.get(PlatformChat, "chat-a")
        assert saved is not None
        self.assertEqual(saved.chat, original_chat)

    async def test_unsupported_embed_container_is_not_invented_as_separate_html_characters(self):
        self.messages["chart"]["output"][1]["embeds"] = SOURCE_HTML
        self.assertEqual((await self.listing())["widgets"], [])

    async def test_inconsistent_request_role_or_saved_current_response_fails_explicitly(self):
        self.metadata["user_message"] = {"id": "request", "role": "assistant", "parentId": "chart"}
        self.assertEqual((await self.listing())["code"], "BRANCH_CONTEXT_INVALID")
        del self.metadata["user_message"]
        self.messages["generating"] = {"id": "generating", "role": "assistant", "parentId": "question", "done": False}
        self.assertEqual((await self.listing())["code"], "BRANCH_CONTEXT_INVALID")

    async def test_temporary_and_channel_context_is_not_a_saved_chat(self):
        for chat_id in ["temporary:session-a", "local:session-a", "channel:room-a"]:
            with self.subTest(chat_id=chat_id):
                self.assertEqual((await self.listing(metadata={**self.metadata, "chat_id": chat_id}))["code"], "CHAT_CONTEXT_MISSING")

    async def test_unknown_identifier_and_missing_execution_user_return_no_html(self):
        self.assertEqual((await self.reading("unavailable-widget"))["code"], "WIDGET_UNAVAILABLE")
        for operation in [self.listing(user={}), self.reading("unavailable-widget", user={})]:
            result = await operation
            self.assertEqual(result["code"], "USER_CONTEXT_MISSING")
            self.assertNotIn("html", result)


if __name__ == "__main__":
    unittest.main()
