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
        "open_webui.utils.filter": {
            "get_filter_functions": AsyncMock(return_value=[]),
            "process_filter_functions": AsyncMock(side_effect=lambda **kwargs: (kwargs["form_data"], {})),
        },
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


def load_plain_module(filename, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool_filter_module = load_plain_module("tool_call_filter.py", "tool_call_filter_tests")
context_filter_module = load_plain_module("subagent_context.py", "subagent_context_tests")


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


class ProtocolTests(unittest.TestCase):
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
        self.tool_filter = tool_filter_module.Filter()
        self.context_filter = context_filter_module.Filter()

    async def apply_filters(self, *, body, runtime_model, runtime, context):
        metadata = runtime.metadata
        metadata["lite_subagent_filter_run"] = True
        metadata["lite_subagent_filter_pipeline"] = []
        await self.tool_filter.inlet(body)
        await self.context_filter.inlet(body)
        metadata["skill_context_applied"] = True
        metadata["lite_subagent_filter_pipeline"].append("skill_context")
        metadata.pop("lite_subagent_filter_run", None)
        return body

    async def prepare(self, messages):
        return (await self.builder.prepare(
            body={"model": "router", "messages": messages},
            marker=router.HandoffMarker("agent-a"), registry={"agent-a": self.agent},
            runtime=self.runtime, context=self.context, load_capabilities=self.loader,
            apply_filters=self.apply_filters,
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
        image_message = {
            "role": "user",
            "content": [
                {"type": "text", "text": tool_filter_module.TOOL_IMAGE_TEXT},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.test/image.png"},
                },
            ],
        }
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
            apply_filters=self.apply_filters,
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


class ChildFilterPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.metadata = {"filter_ids": ["enabled-toggle"]}
        self.request = types.SimpleNamespace(
            state=types.SimpleNamespace(metadata=self.metadata)
        )
        self.runtime = router.RequestRuntime(self.request, self.metadata)
        self.context = router.InvocationContext(
            request=self.request,
            user=types.SimpleNamespace(model_dump=lambda: {"id": "user"}),
        )
        self.model = {
            "id": "agent-a",
            "info": {"meta": {"filterIds": ["tool", "context", "skills"]}},
        }

    async def test_runs_destination_model_inlet_pipeline(self):
        filters = [types.SimpleNamespace(id=name) for name in ("tool", "context", "skills")]

        async def process(**kwargs):
            metadata = kwargs["form_data"]["metadata"]
            metadata.update(
                {
                    "tool_call_filter_applied": True,
                    "subagent_context_applied": True,
                    "skill_context_applied": True,
                    "lite_subagent_filter_pipeline": [
                        "tool_call_filter",
                        "subagent_context",
                        "skill_context",
                    ],
                }
            )
            return kwargs["form_data"], {}

        with (
            patch.object(router, "get_filter_functions", AsyncMock(return_value=filters)) as get_filters,
            patch.object(router, "process_filter_functions", AsyncMock(side_effect=process)) as run_filters,
        ):
            body = await router.ChildFilterPipeline().run(
                body={"model": "agent-a", "messages": [], "metadata": self.metadata},
                runtime_model=self.model,
                runtime=self.runtime,
                context=self.context,
            )

        self.assertIs(body["metadata"], self.metadata)
        get_filters.assert_awaited_once_with(
            self.request,
            self.model,
            ["enabled-toggle"],
        )
        self.assertEqual(run_filters.call_args.kwargs["filter_type"], "inlet")
        self.assertNotIn("lite_subagent_filter_run", self.metadata)

    async def test_reports_missing_required_destination_filters(self):
        with (
            patch.object(router, "get_filter_functions", AsyncMock(return_value=[])),
            patch.object(
                router,
                "process_filter_functions",
                AsyncMock(side_effect=lambda **kwargs: (kwargs["form_data"], {})),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "Required subagent filters"):
                await router.ChildFilterPipeline().run(
                    body={"model": "agent-a", "messages": [], "metadata": self.metadata},
                    runtime_model=self.model,
                    runtime=self.runtime,
                    context=self.context,
                )


if __name__ == "__main__":
    unittest.main()
