"""Regression tests for OWUI's reconstructed tool history, without an OWUI server.

Run: python3 -m unittest discover -s tests -v
OWUI imports are stubbed; the router and ChildRequestBuilder run unmodified.
The fixture was generated with v0.11.1's convert_output_to_messages(raw=True).
"""

import copy
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_router():
    modules = {}
    for name, attributes in {
        "open_webui.models.models": {"Models": types.SimpleNamespace()},
        "open_webui.models.skills": {"Skills": types.SimpleNamespace()},
        "open_webui.models.users": {"Users": types.SimpleNamespace()},
        "open_webui.utils.chat": {"generate_chat_completion": AsyncMock()},
        "open_webui.utils.misc": {
            "remove_system_message": lambda messages: [
                m for m in messages if m["role"] != "system"
            ],
        },
        "open_webui.utils.tools": {
            "get_attached_knowledge": lambda model, metadata: (
                (model.get("info", {}).get("meta", {}) or {}).get("knowledge", [])
            ),
            "get_builtin_tools": AsyncMock(), "get_tools": AsyncMock(),
        },
    }.items():
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        modules[name] = module
    spec = importlib.util.spec_from_file_location(
        "lite_router_history_tests", ROOT / "lite_handoff_router.py"
    )
    router = importlib.util.module_from_spec(spec)
    modules[spec.name] = router
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(router)
    return router


router = load_router()


def call(call_id, name, **arguments):
    return {
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def result(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def assistant(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def marker(agent_id="agent-a"):
    return json.dumps({"__lite_delegate__": "v2", "agent_id": agent_id})


def grouped_history():
    fixture = json.loads((ROOT / "tests/fixtures/owui_grouped_tools.json").read_text())
    return fixture["messages"]


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.agent = router.AgentSpec("agent-a", "Agent A", "route-a")
        self.registry = {"agent-a": self.agent}

    def scope(self, messages, **options):
        params = dict(
            agent=self.agent, registry=self.registry,
            allowed_tools={"lookup", "view_skill"}, skill_ids=["child-skill"],
            user_index=0, marker_index=3, history_turns=0, history_tool_calls=0,
        )
        params.update(options)
        return router.HandoffProtocol.scoped_history(messages, **params)

    def assert_pairs(self, messages, expected_ids):
        calls = [c["id"] for m in messages for c in m.get("tool_calls", [])]
        results = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
        self.assertCountEqual(calls, expected_ids)
        self.assertCountEqual(results, expected_ids)

    def test_only_v2_agent_id_marker_is_supported(self):
        self.assertEqual(router.HandoffMarker.parse(marker()).agent_id, "agent-a")
        self.assertIsNone(
            router.HandoffMarker.parse(
                {"__lite_delegate__": "v1", "skill_id": "agent-a"}
            )
        )
        self.assertIsNone(
            router.HandoffMarker.parse(
                {"__lite_delegate__": "v2", "skill_id": "agent-a"}
            )
        )

    def test_owui_grouped_delegate_and_child_call_preserves_result(self):
        messages = grouped_history()
        original = copy.deepcopy(messages)
        scoped = self.scope(messages)
        self.assert_pairs(scoped, ["lookup"])
        self.assertEqual(scoped[-1]["content"], "TOOL_RESULT_42")
        self.assertNotIn("routing instructions", json.dumps(scoped))
        self.assertEqual(messages, original)

    def test_separate_current_exchange_ignores_historical_limits(self):
        messages = [
            {"role": "user", "content": "question"},
            assistant(call("delegate", "lite_delegate", agent_id="agent-a")),
            result("delegate", marker()),
            assistant(call("first", "lookup")), result("first", "first result"),
            assistant(call("second", "lookup")), result("second", "second result"),
        ]
        self.assert_pairs(self.scope(messages, marker_index=2), ["first", "second"])

    def test_result_from_another_tool_cannot_start_handoff(self):
        messages = [
            {"role": "user", "content": "question"},
            assistant(call("lookup", "lookup")), result("lookup", marker()),
        ]
        self.assertIsNone(router.HandoffProtocol.find_current(messages))
        self.assertEqual(
            router.HandoffProtocol.find_current(grouped_history()),
            router.HandoffMarker("agent-a"),
        )

    def test_grouped_calls_keep_child_skill_and_drop_router_skill_and_orphans(self):
        messages = grouped_history()
        messages[1]["tool_calls"] += [
            call("skill", "view_skill", id="child-skill"),
            call("private", "view_skill", id="route-a"),
            call("unknown", "other_tool"), call("unfinished", "lookup"),
        ]
        messages += [
            result("skill", "child instructions"), result("private", "private instructions"),
            result("unknown", "unknown output"), result("orphan", "orphan output"),
        ]
        scoped = self.scope(messages)
        self.assert_pairs(scoped, ["lookup", "skill"])
        self.assertNotIn("private instructions", json.dumps(scoped))
        self.assertNotIn("orphan output", json.dumps(scoped))

    def test_historical_grouped_calls_use_delegate_result_for_ownership(self):
        messages = grouped_history() + [
            {"role": "assistant", "content": "previous answer"},
            {"role": "user", "content": "next question"},
            assistant(call("delegate2", "lite_delegate", agent_id="agent-a")),
            result("delegate2", marker()),
        ]
        scoped = self.scope(messages, user_index=6, marker_index=8, history_tool_calls=1)
        self.assert_pairs(scoped, ["lookup"])
        self.assertEqual(scoped[-1]["content"], "next question")

    def test_historical_other_agent_and_unknown_owner_are_excluded(self):
        messages = grouped_history()
        messages[3]["content"] = marker("agent-b")
        self.registry["agent-b"] = router.AgentSpec("agent-b", "Agent B")
        messages += [
            {"role": "user", "content": "unowned turn"},
            assistant(call("unowned", "lookup")), result("unowned", "unowned result"),
            {"role": "user", "content": "next question"},
            assistant(call("delegate2", "lite_delegate", agent_id="agent-a")),
            result("delegate2", marker()),
        ]
        scoped = self.scope(messages, user_index=8, marker_index=10, history_tool_calls=10)
        self.assert_pairs(scoped, [])


class ChildContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.metadata = {"tools": {}}
        self.request = types.SimpleNamespace(
            state=types.SimpleNamespace(metadata=self.metadata),
            app=types.SimpleNamespace(state=types.SimpleNamespace(MODELS={"agent-a": {"id": "agent-a"}})),
        )
        self.runtime = router.RequestRuntime(self.request, self.metadata)
        self.context = router.InvocationContext(request=self.request, user=object())
        self.agent = router.AgentSpec("agent-a", "Agent A", "route-a")
        self.builder = router.ChildRequestBuilder()
        self.builder._attachments = AsyncMock(return_value=([], ["toolkit"]))
        self.capabilities = router.CapabilitySet(
            ["toolkit"], [], {"lookup": {"spec": {"name": "lookup"}, "callable": AsyncMock()}},
        )
        self.loader = AsyncMock(return_value=self.capabilities)

    async def prepare(self, messages):
        return (await self.builder.prepare(
            body={"model": "router", "messages": messages},
            marker=router.HandoffMarker("agent-a"), registry={"agent-a": self.agent},
            runtime=self.runtime, context=self.context, load_capabilities=self.loader,
        ))[0]["messages"]

    async def test_boundary_tracks_receipt_when_owui_regroups_messages(self):
        initial = [
            {"role": "user", "content": "Find the answer"},
            assistant(call("route", "view_skill", id="route-a")),
            result("route", "routing instructions"),
            assistant(call("delegate", "lite_delegate", agent_id="agent-a")),
            result("delegate", marker()),
        ]
        await self.prepare(initial)
        messages = await self.prepare(grouped_history())
        self.assertEqual([m["content"] for m in messages if m["role"] == "tool"], ["TOOL_RESULT_42"])
        # Cached tool callables must see the same updated history list.
        shared_messages = self.loader.call_args.kwargs["messages"]
        self.assertEqual(shared_messages, messages)

    async def test_tool_image_user_message_does_not_replace_original_request(self):
        initial = grouped_history()[:4]
        await self.prepare(initial)
        image_message = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.test/image.png"}}]}
        messages = await self.prepare(grouped_history() + [image_message])
        users = [m for m in messages if m["role"] == "user"]
        self.assertEqual(users, [initial[0], image_message])
        self.assertEqual([m["content"] for m in messages if m["role"] == "tool"], ["TOOL_RESULT_42"])

    async def test_three_continuations_with_two_mcp_servers_and_regular_tool(self):
        names = ["first_server_search", "second_server_fetch", "extra_tool"]
        self.capabilities.tools.clear()
        self.capabilities.tools.update({
            name: {"spec": {"name": name}, "callable": AsyncMock()}
            for name in names
        })
        # The first invocation contains only the router's completed handoff.
        source = grouped_history()[:4]
        source[1]["tool_calls"] = source[1]["tool_calls"][:2]
        await self.prepare(source)
        for index, name in enumerate(names):
            call_id = f"child-{index}"
            source[1]["tool_calls"].append(call(call_id, name))
            source.append(result(call_id, f"result-{index}"))
            messages = await self.prepare(source)
            self.assertEqual(
                [m["content"] for m in messages if m["role"] == "tool"],
                [f"result-{n}" for n in range(index + 1)],
            )
            self.assertEqual(
                [c["id"] for m in messages for c in m.get("tool_calls", [])],
                [f"child-{n}" for n in range(index + 1)],
            )
            self.assertEqual(set(self.metadata["tools"]), set(names))

    async def test_child_tool_returning_marker_does_not_move_history_boundary(self):
        await self.prepare(grouped_history()[:4])
        source = grouped_history()
        source[-1]["content"] = marker()
        messages = await self.prepare(source)
        self.assertEqual(
            [m["tool_call_id"] for m in messages if m["role"] == "tool"], ["lookup"],
        )

    async def test_workspace_preparation_removes_all_outer_inference_fields(self):
        body = {
            "model": "router",
            "messages": grouped_history()[:4],
            "metadata": self.metadata,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0.9,
            "top_k": 99,
            "provider_specific_option": "outer-value",
            "options": {"repeat_penalty": 1.5},
        }
        routed, _ = await self.builder.prepare(
            body=body,
            marker=router.HandoffMarker("agent-a"),
            registry={"agent-a": self.agent},
            runtime=self.runtime,
            context=self.context,
            load_capabilities=self.loader,
        )
        self.assertEqual(
            set(routed),
            {"model", "messages", "metadata", "stream", "stream_options", "tools"},
        )
        self.assertEqual(routed["model"], "agent-a")
        self.assertTrue(routed["stream"])
        self.assertEqual(routed["stream_options"], {"include_usage": True})


class WorkspaceCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.user = types.SimpleNamespace(model_dump=lambda: {"id": "user"})
        self.owner = types.SimpleNamespace(model_dump=lambda: {"id": "owner"})
        router.Users.get_user_by_id = AsyncMock(return_value=self.owner)
        router.get_builtin_tools = AsyncMock(
            return_value={
                "search_web": {
                    "spec": {"name": "search_web"},
                    "callable": AsyncMock(),
                },
                "delegate_task": {
                    "spec": {"name": "delegate_task"},
                    "callable": AsyncMock(),
                },
                "timer": {
                    "spec": {"name": "timer"},
                    "callable": AsyncMock(),
                },
            }
        )
        self.resolver = router.ModelCapabilityResolver(router.McpRuntime())

    async def test_child_gets_enabled_builtins_but_not_nested_subagents(self):
        capabilities = await self.resolver.resolve(
            request=types.SimpleNamespace(),
            capability_owner_id="owner",
            execution_user=self.user,
            tool_ids=[],
            skill_ids=[],
            runtime_model={
                "id": "child",
                "info": {
                    "meta": {
                        "capabilities": {
                            "builtin_tools": True,
                            "web_search": True,
                        }
                    }
                },
            },
            metadata={
                "session_id": "session",
                "params": {"function_calling": "native"},
                "features": {"web_search": True},
            },
            messages=[],
            event_emitter=None,
            event_call=None,
            oauth_token=None,
            files=[],
            connector=AsyncMock(),
            include_builtin_tools=True,
        )
        self.assertEqual(set(capabilities.tools), {"search_web"})
        self.assertEqual(
            router.get_builtin_tools.call_args.kwargs["features"],
            {"web_search": True},
        )

    async def test_builtin_tools_capability_can_disable_all_builtins(self):
        capabilities = await self.resolver.resolve(
            request=types.SimpleNamespace(),
            capability_owner_id="owner",
            execution_user=self.user,
            tool_ids=[],
            skill_ids=[],
            runtime_model={
                "id": "child",
                "info": {"meta": {"capabilities": {"builtin_tools": False}}},
            },
            metadata={
                "session_id": "session",
                "params": {"function_calling": "native"},
                "features": {"web_search": True},
            },
            messages=[],
            event_emitter=None,
            event_call=None,
            oauth_token=None,
            files=[],
            connector=AsyncMock(),
            include_builtin_tools=True,
        )
        self.assertEqual(capabilities.tools, {})
        router.get_builtin_tools.assert_not_awaited()

    async def test_legacy_function_calling_does_not_inject_native_builtins(self):
        capabilities = await self.resolver.resolve(
            request=types.SimpleNamespace(),
            capability_owner_id="owner",
            execution_user=self.user,
            tool_ids=[],
            skill_ids=[],
            runtime_model={
                "id": "child",
                "info": {"meta": {"capabilities": {"builtin_tools": True}}},
            },
            metadata={
                "session_id": "session",
                "params": {"function_calling": "legacy"},
                "features": {"web_search": True},
            },
            messages=[],
            event_emitter=None,
            event_call=None,
            oauth_token=None,
            files=[],
            connector=AsyncMock(),
            include_builtin_tools=True,
        )
        self.assertEqual(capabilities.tools, {})
        router.get_builtin_tools.assert_not_awaited()

    def test_attached_knowledge_is_rendered_for_child_prompt(self):
        context = self.resolver.knowledge_context(
            {
                "info": {
                    "meta": {
                        "knowledge": [
                            {
                                "type": "file",
                                "id": "file-1",
                                "name": 'Guide "A"',
                                "source": "model",
                            }
                        ]
                    }
                }
            },
            {"session_id": "session", "params": {"function_calling": "native"}},
        )
        self.assertIn('<knowledge type="file" id="file-1"', context)
        self.assertIn('name="Guide &quot;A&quot;"', context)


if __name__ == "__main__":
    unittest.main()
