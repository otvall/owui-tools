"""Integration tests for the split Router inlet filters."""

import copy
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_handoff_history import assistant, call, grouped_history, marker, result, router

ROOT = Path(__file__).resolve().parents[1]


def load_module(filename, name, modules=None):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module, **(modules or {})}):
        spec.loader.exec_module(module)
    return module


previous_filter = load_module(
    "lite_previous_tool_context.py", "lite_previous_tool_context_tests"
)
cleanup_filter = load_module("lite_history_cleanup.py", "lite_history_cleanup_tests")

skills_api = types.SimpleNamespace(get_skill_by_id=AsyncMock())
open_webui = types.ModuleType("open_webui")
open_webui.__path__ = []
open_webui_models = types.ModuleType("open_webui.models")
open_webui_models.__path__ = []
open_webui_skills = types.ModuleType("open_webui.models.skills")
open_webui_skills.Skills = skills_api
skills_filter = load_module(
    "lite_orchestrator_skills.py",
    "lite_orchestrator_skills_tests",
    {
        "open_webui": open_webui,
        "open_webui.models": open_webui_models,
        "open_webui.models.skills": open_webui_skills,
    },
)


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


class PreviousToolContextTests(unittest.TestCase):
    def setUp(self):
        self.registry = previous_filter.agent_registry(registry_metadata())

    def record(self, messages):
        message = previous_filter.context_message(
            messages,
            user_index=len(messages),
            registry=self.registry,
        )
        return unpack_record([message]) if message else None

    def test_keeps_full_arguments_results_and_agent_identity(self):
        messages = previous_turn()
        large_result = "Результат\n" + "x" * 20000 + "\nitem42"
        messages[4]["content"] = large_result
        messages[1]["tool_calls"][2]["function"]["arguments"] = (
            '{ "query": "документы", "limit": 20 }'
        )
        original = copy.deepcopy(messages)
        record = self.record(messages)
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

    def test_only_immediately_previous_request_is_considered(self):
        messages = previous_turn() + [
            {"role": "user", "content": "Say hello"},
            {"role": "assistant", "content": "Hello"},
        ]
        self.assertIsNone(self.record(messages))

    def test_first_request_has_no_record(self):
        self.assertIsNone(self.record([]))

    def test_tool_images_are_reference_data(self):
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
        record = self.record(messages)
        self.assertEqual(record["tool_result_images"], [image_message["content"]])

    def test_orphans_are_not_invented_as_executions(self):
        messages = previous_turn()
        messages.insert(-1, assistant(call("unfinished", "lookup")))
        messages.insert(-1, result("orphan", "no matching call"))
        self.assertEqual(len(self.record(messages)["tool_exchanges"]), 3)


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
            metadata["lite_history_cleanup_applied"] = True
            metadata["lite_orchestrator_skill_ids"] = ["route-a"]
            self.request.app.state.MODELS["router"]["info"]["meta"][
                "capabilities"
            ]["builtin_tools"] = builtin_tools
            body = {"model": "router", "metadata": metadata, "messages": []}
            filtered = await skills_filter.Filter().inlet(
                body, __request__=self.request
            )
            self.assertIn(expected, filtered["messages"][0]["content"])
            self.assertTrue(metadata["lite_orchestrator_skills_applied"])

    async def test_filter_is_idempotent(self):
        metadata = registry_metadata()
        metadata["lite_history_cleanup_applied"] = True
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


class SplitFilterPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.metadata = registry_metadata()
        self.request = types.SimpleNamespace(
            state=types.SimpleNamespace(metadata=self.metadata),
            app=types.SimpleNamespace(
                state=types.SimpleNamespace(
                    MODELS={
                        "router": {"id": "router"},
                        "agent-a": {"id": "agent-a"},
                        "agent-b": {"id": "agent-b"},
                    }
                )
            ),
        )
        self.runtime = router.RequestRuntime(self.request, self.metadata)
        self.context = router.InvocationContext(request=self.request, user=object())
        self.previous = previous_filter.Filter()
        self.cleanup = cleanup_filter.Filter()
        self.skills = skills_filter.Filter()
        self.pipe = router.Pipe()
        self.pipe.valves.orchestrator_model_id = "base-model"
        self.pipe._generate = AsyncMock()

    async def apply_filters(self, messages):
        body = {
            "model": "router",
            "metadata": self.metadata,
            "messages": copy.deepcopy(messages),
            "tools": [
                {"type": "function", "function": {"name": "lite_delegate"}}
            ],
        }
        await self.previous.inlet(body)
        await self.cleanup.inlet(body)
        await self.skills.inlet(body, __request__=self.request)
        return body

    async def route(self, messages):
        body = await self.apply_filters(messages)
        filtered = copy.deepcopy(body)
        await self.pipe._orchestrator_branch(body, self.runtime, self.context)
        self.assertEqual(body, filtered)
        return self.pipe._generate.call_args.kwargs["body"]

    async def test_priorities_define_required_order(self):
        self.assertEqual(
            [
                self.previous.valves.priority,
                self.cleanup.valves.priority,
                self.skills.valves.priority,
            ],
            [-90, -80, -70],
        )

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
        self.runtime = router.RequestRuntime(self.request, self.metadata)
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
            await router.Pipe()._orchestrator_branch(
                {"messages": []},
                router.RequestRuntime(
                    self.request,
                    {"tools": {}, "lite_registry_applied": True},
                ),
                self.context,
            )

    async def test_other_child_excludes_orchestrator_reference_context(self):
        self.pipe._child_builder._attachments = AsyncMock(
            return_value=([], ["reader-tools"])
        )
        self.pipe._get_or_create_child_capabilities = AsyncMock(
            return_value=router.CapabilitySet(
                ["reader-tools"],
                [],
                {
                    "open_document": {
                        "spec": {"name": "open_document"},
                        "callable": AsyncMock(),
                    }
                },
            )
        )
        raw = previous_turn() + [
            {"role": "user", "content": "Open the second document"},
            assistant(call("next-delegate", "lite_delegate", agent_id="agent-b")),
            result("next-delegate", marker("agent-b")),
        ]
        filtered = await self.apply_filters(raw)
        source = filtered["messages"]
        registry = router.ChildRequestBuilder.agent_registry(self.metadata)
        for step in range(2):
            await self.pipe._child_branch(
                {"model": "router", "messages": source},
                router.HandoffMarker("agent-b"),
                registry,
                self.runtime,
                self.context,
            )
            routed = self.pipe._generate.call_args.kwargs["body"]
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
        self.pipe._child_builder._attachments = AsyncMock(
            return_value=([], ["catalog-tools"])
        )
        self.pipe._get_or_create_child_capabilities = AsyncMock(
            return_value=router.CapabilitySet(
                ["catalog-tools"],
                [],
                {"lookup": {"spec": {"name": "lookup"}, "callable": AsyncMock()}},
            )
        )
        raw = previous_turn()
        raw[3]["content"] = marker("agent-b")
        raw += [
            {"role": "user", "content": "Use the previous result"},
            assistant(call("next-delegate", "lite_delegate", agent_id="agent-b")),
            result("next-delegate", marker("agent-b")),
        ]
        filtered = await self.apply_filters(raw)
        await self.pipe._child_branch(
            {"model": "router", "messages": filtered["messages"]},
            router.HandoffMarker("agent-b"),
            router.ChildRequestBuilder.agent_registry(self.metadata),
            self.runtime,
            self.context,
        )
        messages = self.pipe._generate.call_args.kwargs["body"]["messages"]
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
