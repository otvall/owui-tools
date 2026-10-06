"""Integration tests for the split Router inlet filters."""

import copy
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_handoff_history import PipeTestCase, assistant, call, grouped_history, marker, result, router

ROOT = Path(__file__).resolve().parents[2] / "optional_filters"


def load_module(filename, name, modules=None):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module, **(modules or {})}):
        spec.loader.exec_module(module)
    return module


previous_filter = load_module(
    "previous_tool_context.py", "previous_tool_context_tests"
)
cleanup_filter = load_module("history_cleanup.py", "history_cleanup_tests")

skills_api = types.SimpleNamespace(get_skill_by_id=AsyncMock())
open_webui = types.ModuleType("open_webui")
open_webui.__path__ = []
open_webui_models = types.ModuleType("open_webui.models")
open_webui_models.__path__ = []
open_webui_skills = types.ModuleType("open_webui.models.skills")
open_webui_skills.Skills = skills_api
open_webui_utils = types.ModuleType("open_webui.utils")
open_webui_utils.__path__ = []
open_webui_tools = types.ModuleType("open_webui.utils.tools")
open_webui_tools.get_builtin_tools = AsyncMock(return_value={})
skills_filter = load_module(
    "skill_context.py",
    "skill_context_tests",
    {
        "open_webui": open_webui,
        "open_webui.models": open_webui_models,
        "open_webui.models.skills": open_webui_skills,
        "open_webui.utils": open_webui_utils,
        "open_webui.utils.tools": open_webui_tools,
    },
)
tool_call_filter = load_module("tool_call_filter.py", "tool_call_filter_integration_tests")
subagent_context_filter = load_module("subagent_context.py", "subagent_context_integration_tests")


def previous_turn():
    return grouped_history() + [
        {"role": "assistant", "content": "Found documents: first, second."}
    ]


def unpack_record(messages):
    records = [
        message
        for message in messages
        if isinstance(message.get("content"), str)
        and message["content"].startswith(previous_filter.CONTEXT_PREFIX)
    ]
    if not records:
        return None
    if len(records) != 1:
        raise AssertionError("Previous Tool context must appear exactly once")
    if records[0]["role"] != "assistant" or "tool_calls" in records[0]:
        raise AssertionError("Historical calls must be plain assistant text")
    return json.loads(
        records[0]["content"][len(previous_filter.CONTEXT_PREFIX) :]
    )


def registry_metadata():
    return {
        "tools": {},
        "lite_registry_applied": True,
        "lite_router_model_id": "router",
        "lite_router_owner_id": "owner",
        "lite_base_tool_ids": [],
        "lite_orchestrator_skill_ids": [],
        "lite_agents": {
            "agent-a": {
                "model_id": "agent-a",
                "name": "Catalog",
                "routing_skill_id": "route-a",
            },
            "agent-b": {
                "model_id": "agent-b",
                "name": "Reader",
                "routing_skill_id": "route-b",
            },
        },
    }


class PreviousToolContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = registry_metadata()["lite_agents"]

    async def record(self, messages):
        metadata = registry_metadata()
        metadata.update(
            lite_agents=self.registry,
            lite_router_filter_pipeline=["lite_registry"],
        )
        body = {
            "messages": [*messages, {"role": "user", "content": "Next request"}],
            "metadata": metadata,
        }
        await previous_filter.Filter().inlet(body)
        return unpack_record(body["messages"])

    def test_only_v2_agent_id_marker_is_supported(self):
        self.assertEqual(
            previous_filter.parse_handoff(
                {"__lite_delegate__": "v2", "agent_id": "agent-a"}
            ),
            "agent-a",
        )
        self.assertIsNone(
            previous_filter.parse_handoff(
                {"__lite_delegate__": "v1", "skill_id": "agent-a"}
            )
        )
        self.assertIsNone(
            previous_filter.parse_handoff(
                {"__lite_delegate__": "v2", "skill_id": "agent-a"}
            )
        )

    async def test_keeps_full_arguments_results_and_agent_identity(self):
        messages = previous_turn()
        large_result = "Результат\n" + "x" * 20000 + "\nitem42"
        messages[4]["content"] = large_result
        messages[1]["tool_calls"][2]["function"]["arguments"] = (
            '{ "query": "документы", "limit": 20 }'
        )
        original = copy.deepcopy(messages)
        record = await self.record(messages)
        exchanges = record["tool_exchanges"]
        self.assertEqual(
            [item["call"]["id"] for item in exchanges],
            ["route", "delegate", "lookup"],
        )
        self.assertEqual(exchanges[0]["executor"], {"kind": "orchestrator"})
        self.assertEqual(exchanges[1]["executor"], {"kind": "orchestrator"})
        self.assertEqual(
            exchanges[2]["executor"],
            {
                "kind": "subagent",
                "agent_id": "agent-a",
                "model_id": "agent-a",
                "name": "Catalog",
            },
        )
        self.assertEqual(exchanges[2]["call"], messages[1]["tool_calls"][2])
        self.assertEqual(exchanges[2]["result"], messages[4])
        self.assertEqual(messages, original)

    async def test_only_immediately_previous_request_is_considered(self):
        messages = previous_turn() + [
            {"role": "user", "content": "Say hello"},
            {"role": "assistant", "content": "Hello"},
        ]
        self.assertIsNone(await self.record(messages))

    async def test_first_request_has_no_record(self):
        self.assertIsNone(await self.record([]))

    async def test_tool_images_are_reference_data(self):
        messages = previous_turn()
        image_message = {
            "role": "user",
            "content": [
                {"type": "text", "text": previous_filter.TOOL_IMAGE_TEXT},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.test/result.png"},
                },
            ],
        }
        messages.insert(-1, image_message)
        record = await self.record(messages)
        self.assertEqual(record["tool_result_images"], [image_message["content"]])

    async def test_orphans_are_not_invented_as_executions(self):
        messages = previous_turn()
        messages.insert(-1, assistant(call("unfinished", "lookup")))
        messages.insert(-1, result("orphan", "no matching call"))
        self.assertEqual(len((await self.record(messages))["tool_exchanges"]), 3)

    async def test_pairing_is_batch_scoped_and_repeated_results_are_unambiguous(self):
        valid = [assistant(call("valid", "lookup")), result("valid", "VALID")]
        for disputed in (
            [assistant(call("same", "lookup")),
             {"role": "assistant", "content": "Another execution batch"},
             result("same", "LATE")],
            [assistant(call("same", "private_tool"), call("same", "lookup")),
             result("same", "AMBIGUOUS")],
            [assistant(call("same", "lookup")),
             result("same", "FIRST"), result("same", "CONFLICT")],
        ):
            with self.subTest(disputed=disputed):
                record = await self.record([
                    {"role": "user", "content": "Question"}, *disputed, *valid,
                    {"role": "assistant", "content": "Answer"},
                ])
                self.assertEqual([item["call"]["id"] for item in record["tool_exchanges"]], ["valid"])

        record = await self.record([
            {"role": "user", "content": "Question"},
            *valid, result("valid", "VALID"),
            {"role": "assistant", "content": "Answer"},
        ])
        self.assertEqual(len(record["tool_exchanges"]), 1)

    async def test_alias_uses_canonical_id_and_current_registry_enrichment(self):
        messages = previous_turn()
        messages[3]["content"] = marker("route-a")
        self.registry["agent-a"].update(model_id="current-model", name="Current catalog")

        record = await self.record(messages)

        self.assertEqual(record["tool_exchanges"][-1]["executor"], {
            "kind": "subagent", "agent_id": "agent-a",
            "model_id": "current-model", "name": "Current catalog",
        })
        self.assertEqual(record["tool_exchanges"][1]["result"], messages[3])

    async def test_ambiguous_alias_preserves_completed_work_with_unknown_executor(self):
        self.registry["agent-b"]["routing_skill_id"] = "route-a"
        messages = previous_turn()
        messages[3]["content"] = marker("route-a")

        record = await self.record(messages)

        self.assertEqual(record["tool_exchanges"][-1]["executor"], {
            "kind": "unknown", "declared_agent_id": "route-a",
        })
        self.assertEqual(record["tool_exchanges"][-1]["result"], messages[4])

    async def test_removed_destination_preserves_declared_id_without_enrichment(self):
        self.registry.pop("agent-a")

        record = await self.record(previous_turn())

        self.assertEqual(record["tool_exchanges"][-1]["executor"], {
            "kind": "unknown", "declared_agent_id": "agent-a",
        })

    async def test_uncertain_handoff_keeps_work_and_later_proven_handoff_restores_executor(self):
        record = await self.record([
            {"role": "user", "content": "Question"},
            assistant(call("to-a", "lite_delegate")), result("to-a", marker()),
            assistant(call("unfinished", "lite_delegate"), call("uncertain-work", "lookup")),
            result("uncertain-work", "COMPLETED_WITH_UNKNOWN_EXECUTOR"),
            assistant(call("to-b", "lite_delegate")), result("to-b", marker("agent-b")),
            assistant(call("confirmed-work", "lookup")), result("confirmed-work", "B_RESULT"),
            {"role": "assistant", "content": "Answer"},
        ])

        self.assertEqual([item["call"]["id"] for item in record["tool_exchanges"]],
                         ["to-a", "uncertain-work", "to-b", "confirmed-work"])
        self.assertEqual([item["executor"]["kind"] for item in record["tool_exchanges"]],
                         ["orchestrator", "unknown", "unknown", "subagent"])
        self.assertEqual(record["tool_exchanges"][-1]["executor"]["agent_id"], "agent-b")


class StandalonePreviousToolContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_handoff_does_not_change_model_executor_without_registry(self):
        body = {
            "messages": [*previous_turn(), {"role": "user", "content": "Next request"}],
            "metadata": {"lite_agents": registry_metadata()["lite_agents"]},
        }

        await previous_filter.Filter().inlet(body)

        record = unpack_record(body["messages"])
        self.assertEqual([item["executor"] for item in record["tool_exchanges"]],
                         [{"kind": "model"}] * 3)
        self.assertEqual(record["tool_exchanges"][1]["result"], previous_turn()[3])

    async def test_runs_without_registry_and_works_with_cleanup(self):
        body = {
            "messages": [
                {"role": "user", "content": "Find a document"},
                assistant(call("lookup", "search", query="report")),
                result("lookup", "DOCUMENT_42"),
                {"role": "assistant", "content": "Document found"},
                {"role": "user", "content": "Open it"},
            ]
        }

        await previous_filter.Filter().inlet(body)

        record = unpack_record(body["messages"])
        self.assertEqual(len(record["tool_exchanges"]), 1)
        self.assertEqual(record["tool_exchanges"][0]["executor"], {"kind": "model"})
        self.assertTrue(body["metadata"]["previous_tool_context_applied"])
        self.assertNotIn("lite_unfiltered_messages", body["metadata"])

        await cleanup_filter.Filter().inlet(body)
        self.assertIsNotNone(unpack_record(body["messages"]))
        self.assertFalse(
            any(message.get("role") == "tool" for message in body["messages"])
        )


class SkillPromptFilterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        skills_api.get_skill_by_id = AsyncMock(
            side_effect=lambda skill_id: types.SimpleNamespace(
                id=skill_id,
                name=f"Skill {skill_id}",
                description=f"Description {skill_id}",
                content=f"Instructions {skill_id}",
                is_active=True,
            )
        )
        skills_filter.get_builtin_tools = AsyncMock(
            return_value={
                "view_skill": {
                    "spec": {"name": "view_skill"},
                    "callable": AsyncMock(),
                }
            }
        )
        self.request = types.SimpleNamespace(
            app=types.SimpleNamespace(
                state=types.SimpleNamespace(
                    MODELS={
                        "router": {
                            "id": "router",
                            "info": {
                                "meta": {"capabilities": {"builtin_tools": True}}
                            },
                        }
                    }
                )
            )
        )

    async def test_lazy_manifest_and_full_context(self):
        for builtin_tools, expected in (
            (True, "<available_skills>"),
            (False, "Instructions route-a"),
        ):
            metadata = registry_metadata()
            metadata.update(
                {"session_id": "session", "params": {"function_calling": "native"}}
            )
            metadata["history_cleanup_applied"] = True
            metadata["lite_orchestrator_skill_ids"] = ["route-a"]
            self.request.app.state.MODELS["router"]["info"]["meta"][
                "capabilities"
            ]["builtin_tools"] = builtin_tools
            body = {"model": "router", "metadata": metadata, "messages": []}
            filtered = await skills_filter.Filter().inlet(
                body, __request__=self.request, __user__={"id": "user"}
            )
            self.assertIn(expected, filtered["messages"][0]["content"])
            self.assertTrue(metadata["skill_context_applied"])
            self.assertEqual(
                "view_skill" in metadata.get("tools", {}),
                builtin_tools,
            )
            self.assertEqual(
                any(
                    ((schema.get("function") or {}).get("name") == "view_skill")
                    for schema in body.get("tools", [])
                ),
                builtin_tools,
            )

    async def test_runs_for_regular_model_without_router_metadata(self):
        self.request.app.state.MODELS["regular"] = {
            "id": "regular",
            "info": {
                "meta": {
                    "skillIds": ["general-skill"],
                    "capabilities": {"builtin_tools": True},
                }
            },
        }
        body = {"model": "regular", "messages": [], "metadata": {}}

        await skills_filter.Filter().inlet(body, __request__=self.request)

        self.assertIn("general-skill", body["messages"][0]["content"])
        self.assertTrue(body["metadata"]["skill_context_applied"])

    async def test_filter_is_idempotent(self):
        metadata = registry_metadata()
        metadata["history_cleanup_applied"] = True
        metadata["lite_orchestrator_skill_ids"] = ["route-a"]
        body = {"model": "router", "metadata": metadata, "messages": []}
        filter_instance = skills_filter.Filter()
        await filter_instance.inlet(body, __request__=self.request)
        await filter_instance.inlet(body, __request__=self.request)
        prompts = [
            message
            for message in body["messages"]
            if message.get("content", "").startswith(skills_filter.PROMPT_PREFIX)
        ]
        self.assertEqual(len(prompts), 1)


class ToolCallFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_only_supported_complete_pairs_and_strips_router_trace(self):
        body = {
            "messages": grouped_history()
            + [
                assistant(call("blocked", "private_tool")),
                result("blocked", "private result"),
                result("orphan", "orphan result"),
            ],
            "tools": [
                {"type": "function", "function": {"name": "lookup"}}
            ],
            "metadata": {
                "tools": {"lookup": {}},
                "lite_target_agent_id": "agent-a",
                "lite_subagent_filter_run": True,
            },
        }
        original = copy.deepcopy(body["messages"])

        await tool_call_filter.Filter().inlet(body)

        calls = [
            call_item["id"]
            for message in body["messages"]
            for call_item in message.get("tool_calls", [])
        ]
        results = [
            message["tool_call_id"]
            for message in body["messages"]
            if message.get("role") == "tool"
        ]
        self.assertEqual(calls, ["lookup"])
        self.assertEqual(results, ["lookup"])
        self.assertNotIn("routing instructions", json.dumps(body["messages"]))
        self.assertNotIn("private result", json.dumps(body["messages"]))
        self.assertEqual(original, grouped_history() + [
            assistant(call("blocked", "private_tool")),
            result("blocked", "private result"),
            result("orphan", "orphan result"),
        ])

    async def test_keeps_view_skill_result_after_skill_filter_enabled_it(self):
        body = {
            "messages": [
                {"role": "user", "content": "question"},
                assistant(call("delegate", "lite_delegate", agent_id="agent-a")),
                result("delegate", marker("agent-a")),
                assistant(call("skill", "view_skill", id="child-skill")),
                result("skill", "skill instructions"),
            ],
            "metadata": {
                "tools": {},
                "lite_target_agent_id": "agent-a",
                "lite_target_model_id": "agent-a",
                "lite_view_skill_available": True,
                "lite_view_skill_model_id": "agent-a",
            },
        }

        await tool_call_filter.Filter().inlet(body)

        self.assertEqual(
            [
                call_item["id"]
                for message in body["messages"]
                for call_item in message.get("tool_calls", [])
            ],
            ["skill"],
        )
        self.assertEqual(
            [message.get("content") for message in body["messages"] if message.get("role") == "tool"],
            ["skill instructions"],
        )


class SubagentContextFilterTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def conversation():
        return [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "question one"},
            assistant(call("one-a", "lookup")),
            result("one-a", "result one a"),
            {"role": "assistant", "content": "answer one"},
            {"role": "user", "content": "question two"},
            assistant(call("two-a", "lookup"), call("two-b", "lookup")),
            result("two-a", "result two a"),
            result("two-b", "result two b"),
            {"role": "assistant", "content": "answer two"},
            {"role": "user", "content": "current question"},
            assistant(call("current", "lookup")),
            result("current", "current result"),
        ]

    async def apply(self, turns, tool_calls):
        instance = subagent_context_filter.Filter()
        instance.valves.history_turns = turns
        instance.valves.history_tool_calls = tool_calls
        body = {
            "messages": self.conversation(),
            "metadata": {
                "tool_call_filter_applied": True,
                "lite_subagent_filter_run": True,
            },
        }
        await instance.inlet(body)
        return body["messages"]

    async def test_tool_limit_is_additional_to_turn_limit(self):
        messages = await self.apply(turns=1, tool_calls=5)
        serialized = json.dumps(messages)
        self.assertNotIn("question one", serialized)
        self.assertIn("question two", serialized)
        self.assertIn("answer two", serialized)
        self.assertIn("result two a", serialized)
        self.assertIn("result two b", serialized)
        self.assertIn("current result", serialized)

    async def test_tool_limit_keeps_last_calls_but_all_selected_text_turns(self):
        messages = await self.apply(turns=2, tool_calls=1)
        serialized = json.dumps(messages)
        self.assertIn("question one", serialized)
        self.assertIn("answer one", serialized)
        self.assertIn("question two", serialized)
        self.assertIn("answer two", serialized)
        self.assertNotIn("result one a", serialized)
        self.assertNotIn("result two a", serialized)
        self.assertIn("result two b", serialized)
        self.assertIn("current result", serialized)

    async def test_zero_turns_removes_all_historical_calls(self):
        messages = await self.apply(turns=0, tool_calls=5)
        serialized = json.dumps(messages)
        self.assertNotIn("question one", serialized)
        self.assertNotIn("question two", serialized)
        self.assertNotIn("result two b", serialized)
        self.assertIn("current question", serialized)
        self.assertIn("current result", serialized)

    async def test_incomplete_turn_does_not_consume_text_pair_limit(self):
        instance = subagent_context_filter.Filter()
        instance.valves.history_turns = 1
        body = {
            "messages": [
                {"role": "user", "content": "completed question"},
                {"role": "assistant", "content": "completed answer"},
                {"role": "user", "content": "abandoned question"},
                {"role": "user", "content": "current question"},
            ],
            "metadata": {"tool_call_filter_applied": True},
        }

        await instance.inlet(body)

        serialized = json.dumps(body["messages"])
        self.assertIn("completed question", serialized)
        self.assertIn("completed answer", serialized)
        self.assertNotIn("abandoned question", serialized)


class PreviousToolContextMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_removes_legacy_lite_guidance(self):
        body = {
            "messages": [
                {
                    "role": "system",
                    "content": previous_filter.LEGACY_GUIDANCE_PREFIX + "old",
                },
                *previous_turn(),
                {"role": "user", "content": "Continue"},
            ]
        }

        await previous_filter.Filter().inlet(body)

        self.assertFalse(
            any(
                str(message.get("content", "")).startswith(
                    previous_filter.LEGACY_GUIDANCE_PREFIX
                )
                for message in body["messages"]
            )
        )


class StandaloneHistoryCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_without_other_router_filters(self):
        body = {
            "messages": previous_turn()
            + [
                {"role": "user", "content": "Current question"},
                assistant(call("current", "current_tool")),
                result("current", "CURRENT_RESULT"),
            ]
        }

        await cleanup_filter.Filter().inlet(body)

        self.assertTrue(body["metadata"]["history_cleanup_applied"])
        self.assertIn(
            "Found documents: first, second.",
            [message.get("content") for message in body["messages"]],
        )
        self.assertEqual(
            [
                message["tool_call_id"]
                for message in body["messages"]
                if message.get("role") == "tool"
            ],
            ["current"],
        )
        self.assertNotIn("TOOL_RESULT_42", json.dumps(body["messages"]))


class SplitFilterPipelineTests(PipeTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.registry = self.registry_filter()
        self.previous = previous_filter.Filter()
        self.cleanup = cleanup_filter.Filter()

    async def apply_filters(self, messages):
        self.begin_request()
        body = {
            "model": "router",
            "metadata": self.metadata,
            "messages": copy.deepcopy(messages),
            "tools": [
                {"type": "function", "function": {"name": "lite_delegate"}}
            ],
        }
        self.registry.valves.base_skill_ids = self.metadata.get("lite_orchestrator_skill_ids") or []
        await self.registry.inlet(body, __request__=self.request, __user__={"id": "user"})
        await self.previous.inlet(body, __request__=self.request)
        await self.cleanup.inlet(body, __request__=self.request)
        return body

    async def route(self, messages):
        body = await self.apply_filters(messages)
        filtered = copy.deepcopy({key: value for key, value in body.items() if key != "metadata"})
        await self.invoke_body(body)
        self.assertEqual({key: value for key, value in body.items() if key != "metadata"}, filtered)
        return self.routed

    async def test_standalone_router_history_priorities_define_required_order(self):
        self.assertEqual(
            [
                self.previous.valves.priority,
                self.cleanup.valves.priority,
            ],
            [-90, -80],
        )

    async def test_router_builds_skill_prompt_without_skill_filter(self):
        self.metadata["lite_orchestrator_skill_ids"] = ["route-a"]
        self.builtins.return_value = {
            "view_skill": {"spec": {"name": "view_skill"}, "callable": AsyncMock()},
        }

        body = await self.apply_filters(
            [{"role": "user", "content": "Use an agent"}]
        )
        await self.invoke_body(body)
        routed = self.routed

        prompts = [
            message["content"]
            for message in routed["messages"]
            if isinstance(message.get("content"), str)
            and message["content"].startswith(
                router.ORCHESTRATOR_SKILL_PROMPT_PREFIX
            )
        ]
        self.assertEqual(len(prompts), 1)
        self.assertIn("route-a", prompts[0])
        self.assertNotIn("skill_context_applied", self.metadata)

    async def test_orchestrator_gets_text_history_record_and_current_native_calls(self):
        messages = [
            {"role": "user", "content": "Old question"},
            {"role": "assistant", "content": "Old answer"},
            *previous_turn(),
            {"role": "user", "content": "Open the second document"},
            assistant(call("current-skill", "view_skill", id="route-b")),
            result("current-skill", "Current routing instructions"),
        ]
        messages[3]["content"] = "Searching the catalog."
        routed = await self.route(messages)
        record = unpack_record(routed["messages"])
        self.assertEqual(
            record["tool_exchanges"][-1]["result"]["content"],
            "TOOL_RESULT_42",
        )
        self.assertIn("Old answer", [m.get("content") for m in routed["messages"]])
        self.assertIn(
            "Searching the catalog.",
            [m.get("content") for m in routed["messages"]],
        )
        self.assertEqual(
            [m["tool_call_id"] for m in routed["messages"] if m["role"] == "tool"],
            ["current-skill"],
        )
        serialized = json.dumps(routed["messages"])
        self.assertEqual(serialized.count("Find the answer"), 1)
        self.assertEqual(serialized.count("Found documents: first, second."), 1)
        self.assertNotIn("lookup", self.metadata["tools"])

    async def test_new_request_replaces_record_instead_of_accumulating(self):
        first = await self.route(
            previous_turn() + [{"role": "user", "content": "Say hello"}]
        )
        self.assertEqual(len(unpack_record(first["messages"])["tool_exchanges"]), 3)

        self.metadata = registry_metadata()
        self.request.state.metadata = self.metadata
        second = await self.route(
            previous_turn()
            + [
                {"role": "user", "content": "Say hello"},
                {"role": "assistant", "content": "Hello"},
                {"role": "user", "content": "Say it again"},
            ]
        )
        self.assertIsNone(unpack_record(second["messages"]))
        self.assertNotIn("TOOL_RESULT_42", json.dumps(second["messages"]))

    async def test_disabled_context_still_captures_child_history(self):
        self.previous.valves.enabled = False
        body = await self.apply_filters(
            previous_turn() + [{"role": "user", "content": "Next question"}]
        )
        self.assertIsNone(unpack_record(body["messages"]))
        self.assertIn("lite_unfiltered_messages", self.metadata)

    async def test_router_reports_missing_filters(self):
        with self.assertRaisesRegex(ValueError, "Required Router filters"):
            self.metadata = {"tools": {}, "lite_registry_applied": True}
            await self.invoke([])

    async def test_other_child_excludes_orchestrator_reference_context(self):
        self.models["agent-b"].meta["toolIds"] = ["reader-tools"]
        self.tool_names = ["open_document"]
        raw = previous_turn() + [
            {"role": "user", "content": "Open the second document"},
            assistant(call("next-delegate", "lite_delegate", agent_id="agent-b")),
            result("next-delegate", marker("agent-b")),
        ]
        filtered = await self.apply_filters(raw)
        source = filtered["messages"]
        for step in range(2):
            await self.invoke(source)
            routed = self.routed
            self.assertIsNone(unpack_record(routed["messages"]))
            self.assertNotIn("TOOL_RESULT_42", json.dumps(routed["messages"]))
            self.assertEqual(
                [m["tool_call_id"] for m in routed["messages"] if m["role"] == "tool"],
                [] if step == 0 else ["open-second"],
            )
            source += [
                assistant(call("open-second", "open_document", id="item42")),
                result("open-second", "Document body"),
            ]

    async def test_same_child_receives_native_history_after_cleanup(self):
        self.pipe.valves.history_turns = 1
        self.pipe.valves.history_tool_calls = 1
        self.models["agent-b"].meta["toolIds"] = ["catalog-tools"]
        raw = previous_turn()
        raw[3]["content"] = marker("agent-b")
        raw += [
            {"role": "user", "content": "Use the previous result"},
            assistant(call("next-delegate", "lite_delegate", agent_id="agent-b")),
            result("next-delegate", marker("agent-b")),
        ]
        filtered = await self.apply_filters(raw)
        await self.invoke(filtered["messages"])
        messages = self.routed["messages"]
        self.assertIsNone(unpack_record(messages))
        self.assertIn(
            "Found documents: first, second.",
            [m.get("content") for m in messages],
        )
        self.assertEqual(
            [c["id"] for m in messages for c in m.get("tool_calls", [])],
            ["lookup"],
        )
        self.assertEqual(
            [m["content"] for m in messages if m["role"] == "tool"],
            ["TOOL_RESULT_42"],
        )


if __name__ == "__main__":
    unittest.main()
