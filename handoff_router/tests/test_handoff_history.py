"""Regression tests for OWUI's reconstructed tool history, without an OWUI server.

Run from the repository root: python3 -m unittest discover -s handoff_router/tests -v
OWUI boundary adapters are stubbed; Pipe.pipe prepares children without attachments.
The fixture was generated with v0.11.1's convert_output_to_messages(raw=True).
"""

import copy
import importlib.util
import inspect
import json
import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from starlette.requests import Request


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


def load_plain_module(filename, name, modules=None):
    directory = (
        ROOT if filename in {"lite_handoff_router.py", "lite_delegate.py", "router_preparation.py"}
        else ROOT.parent / "optional_filters"
    )
    spec = importlib.util.spec_from_file_location(name, directory / filename)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module, **(modules or {})}):
        spec.loader.exec_module(module)
    return module


tool_filter_module = load_plain_module("tool_call_filter.py", "tool_call_filter_tests")
context_filter_module = load_plain_module("subagent_context.py", "subagent_context_tests")
skill_filter_module = load_plain_module(
    "skill_context.py", "skill_context_pipe_tests",
    {
        "open_webui.models.skills": types.SimpleNamespace(Skills=router.Skills),
        "open_webui.utils.tools": types.SimpleNamespace(get_builtin_tools=router.get_builtin_tools),
    },
)
registry_module = load_plain_module(
    "lite_subagent_registry.py", "registry_pipe_tests",
    {
        "open_webui.config": types.SimpleNamespace(BYPASS_ADMIN_ACCESS_CONTROL=False),
        "open_webui.env": types.SimpleNamespace(BYPASS_MODEL_ACCESS_CONTROL=False),
        "open_webui.models.models": types.SimpleNamespace(Models=router.Models),
        "open_webui.models.skills": types.SimpleNamespace(Skills=router.Skills),
        "open_webui.models.users": types.SimpleNamespace(Users=router.Users),
        "open_webui.utils.models": types.SimpleNamespace(check_model_access=AsyncMock()),
    },
)
previous_module = load_plain_module("previous_tool_context.py", "previous_pipe_tests")
cleanup_module = load_plain_module("history_cleanup.py", "cleanup_pipe_tests")


class PipeTestCase(unittest.IsolatedAsyncioTestCase):
    """Run the real Pipe and filters with controlled OWUI boundary adapters."""

    async def asyncSetUp(self):
        self.user = types.SimpleNamespace(model_dump=lambda: {"id": "user"})
        self.owner: Any = types.SimpleNamespace(model_dump=lambda: {"id": "owner"})
        self.metadata: dict[str, Any] = {"tools": {}}
        self.models = {
            name: types.SimpleNamespace(is_active=True, user_id="owner", meta={"toolIds": ["toolkit"]})
            for name in ("agent-a", "agent-b")
        }
        self.request = types.SimpleNamespace(
            state=types.SimpleNamespace(metadata=self.metadata),
            app=types.SimpleNamespace(state=types.SimpleNamespace(MODELS={
                name: {"id": name} for name in ("router", "agent-a", "agent-b")
            })),
        )
        self.tool_names = ["lookup"]
        self.tool_filter = tool_filter_module.Filter()
        self.skill_filter = skill_filter_module.Filter()
        self.filters = []
        self.users = self.enterContext(patch.object(
            router.Users, "get_user_by_id", AsyncMock(side_effect=lambda id: self.user if id == "user" else self.owner), create=True,
        ))
        self.model_lookup = self.enterContext(patch.object(
            router.Models, "get_model_by_id", AsyncMock(side_effect=lambda id: self.models.get(id)), create=True,
        ))
        self.loader = self.enterContext(patch.object(router, "get_tools", AsyncMock(side_effect=self.load_tools)))
        self.builtins = self.enterContext(patch.object(router, "get_builtin_tools", AsyncMock(return_value={})))
        self.enterContext(patch.object(skill_filter_module, "get_builtin_tools", self.builtins))
        self.skills = self.enterContext(patch.object(
            router.Skills, "get_skill_by_id", AsyncMock(side_effect=lambda id: types.SimpleNamespace(
                is_active=True, name={"route-a": "Agent A", "route-b": "Agent B"}.get(id, id),
                description="Skill description", content="Skill instructions for " + id,
            )), create=True,
        ))
        self.connector = AsyncMock(return_value=None)
        self.enterContext(patch.dict(sys.modules, {
            "open_webui.utils.middleware": types.SimpleNamespace(connect_mcp_server=self.connector),
        }))
        self.get_filters = self.enterContext(patch.object(
            router, "get_filter_functions", AsyncMock(side_effect=lambda *args: self.filters),
        ))
        self.dispatch_filters = self.enterContext(patch.object(
            router, "process_filter_functions", AsyncMock(side_effect=self.process_filters),
        ))
        self.completion_result = {"choices": [{"message": {"content": "answer"}}]}
        self.completion = self.enterContext(patch.object(
            router, "generate_chat_completion", AsyncMock(return_value=self.completion_result),
        ))
        self.events = AsyncMock()
        self.pipe = router.Pipe()
        self.pipe.valves.orchestrator_model_id = "base-model"
        await self.router_inlets(
            {"model": "router", "metadata": self.metadata, "messages": []},
            registry=PipeTestCase.registry_filter(self),
        )
        self.users.reset_mock()
        self.model_lookup.reset_mock()
        self.skills.reset_mock()

    def begin_request(self, *, metadata=None):
        self.request = Request({
            "type": "http", "app": self.request.app,
            "state": {"metadata": self.metadata if metadata is None else metadata},
        })

    async def load_tools(self, request, ids, owner, extra_params):
        history = extra_params["__messages__"]

        async def read_history():
            return copy.deepcopy(history)

        return {
            name: {"tool_id": ids[0], "spec": {"name": name}, "callable": read_history}
            for name in self.tool_names
        }

    def registry_filter(self):
        self.enterContext(patch.dict(registry_module.SUBAGENTS, {
            "agent-a": "route-a", "agent-b": "route-b",
        }, clear=True))
        self.user.id, self.user.role = "user", "user"
        self.models["router"] = types.SimpleNamespace(is_active=True, user_id="owner")
        registry = registry_module.Filter()
        registry.valves.base_tool_ids = []
        registry.valves.base_skill_ids = []
        return registry

    async def router_inlets(self, body, *, registry=None):
        registry = registry or self.registry_filter()
        await registry.inlet(body, __request__=self.request, __user__={"id": "user"})
        return await self.context_inlets(body)

    async def context_inlets(self, body):
        await previous_module.Filter().inlet(body, __request__=self.request)
        await cleanup_module.Filter().inlet(body, __request__=self.request)
        return body

    async def process_filters(self, **kwargs):
        body = kwargs["form_data"]
        for filter in kwargs["filter_functions"]:
            supported = inspect.signature(filter.inlet).parameters
            body = await filter.inlet(body, **{
                key: value for key, value in kwargs["extra_params"].items() if key in supported
            })
        return body, {}

    async def invoke(self, messages, **fields):
        return await self.invoke_body({"model": "router", "messages": messages, **fields})

    async def invoke_body(self, body):
        return await self.pipe.pipe(
            body,
            __request__=self.request, __user__={"id": "user"}, __metadata__=self.metadata,
            __event_emitter__=self.events,
        )

    async def route_history(self, messages):
        body = {"model": "router", "metadata": self.metadata, "messages": copy.deepcopy(messages)}
        self.begin_request()
        await self.router_inlets(body)
        await self.invoke_body(body)
        return self.routed["messages"]

    @property
    def routed(self):
        return self.completion.call_args.args[1]


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


class ChildContinuationTests(PipeTestCase):
    async def prepare(self, messages):
        await self.invoke(messages)
        return self.routed["messages"]

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
        shared_messages = await self.metadata["tools"]["lookup"]["callable"]()
        self.assertEqual(shared_messages, messages)
        self.loader.assert_awaited_once()

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
        self.models["agent-a"].meta["toolIds"] = ["toolkit", "server:mcp:first_server", "server:mcp:second_server"]
        self.tool_names = ["extra_tool"]
        clients = {}

        async def connect(request, server_id, owner, metadata, extra_params):
            client = types.SimpleNamespace(call_tool=AsyncMock(return_value="mcp result"))
            clients[server_id] = client
            return client, [{"name": "search" if server_id == "first_server" else "fetch"}]

        self.connector.side_effect = connect
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
        self.loader.assert_awaited_once()
        self.assertEqual(self.connector.await_count, 2)
        self.assertEqual(set(self.metadata["mcp_clients"]), {"first_server", "second_server"})
        returned = await self.metadata["tools"]["second_server_fetch"]["callable"](id="document")
        self.assertEqual(returned, "mcp result")
        clients["second_server"].call_tool.assert_awaited_once_with("fetch", function_args={"id": "document"})

    async def test_child_tool_returning_marker_does_not_move_history_boundary(self):
        await self.prepare(grouped_history()[:4])
        source = grouped_history()
        source[-1]["content"] = marker()
        messages = await self.prepare(source)
        self.assertEqual(
            [m["tool_call_id"] for m in messages if m["role"] == "tool"], ["lookup"],
        )

    async def test_workspace_preparation_removes_all_outer_inference_fields(self):
        child_model = self.request.app.state.MODELS["agent-a"]
        child_model.update(
            base_model_id="child-provider",
            info={"params": {"temperature": 0.2, "system": "Child system prompt"}},
        )
        child_settings = copy.deepcopy(child_model)
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
        await self.invoke_body(body)
        routed = self.routed
        self.assertEqual(
            set(routed),
            {"model", "messages", "metadata", "stream", "stream_options", "tools"},
        )
        self.assertEqual(routed["model"], "agent-a")
        self.assertTrue(routed["stream"])
        self.assertEqual(routed["stream_options"], {"include_usage": True})
        self.assertEqual(child_model, child_settings)
        self.assertIs(self.get_filters.call_args.args[1], child_model)


class CapabilityReuseTests(PipeTestCase):
    async def test_matching_continuation_reuses_live_tools_with_updated_history(self):
        shared_tools = self.metadata["tools"]
        # OWUI can also keep a distinct metadata dictionary on request.state.
        self.request.state.metadata = {}
        await self.invoke(grouped_history()[:4])
        lookup = shared_tools["lookup"]["callable"]
        await self.invoke(grouped_history())
        self.loader.assert_awaited_once()
        self.assertIs(shared_tools["lookup"]["callable"], lookup)
        self.assertEqual(await lookup(), self.routed["messages"])
        self.assertIs(self.routed["metadata"], self.metadata)
        self.assertIs(self.request.state.metadata["tools"], shared_tools)
        self.assertEqual(self.loader.call_args.args[2], self.owner)
        params = self.loader.call_args.args[3]
        self.assertEqual(params["__user__"], {"id": "user"})
        self.assertIs(params["__metadata__"], self.metadata)
        self.assertIs(params["__request__"], self.request)

    async def test_changed_destination_reloads_capabilities(self):
        await self.invoke(grouped_history()[:4])
        lookup = self.metadata["tools"]["lookup"]["callable"]
        self.metadata["lite_agents"]["agent-a"]["model_id"] = "agent-b"
        await self.invoke(grouped_history())
        self.assertEqual(self.loader.await_count, 2)
        self.assertEqual(self.routed["model"], "agent-b")
        self.assertIsNot(self.metadata["tools"]["lookup"]["callable"], lookup)

    async def test_changed_tool_selection_replaces_capabilities(self):
        await self.invoke(grouped_history()[:4])
        self.models["agent-a"].meta["toolIds"] = ["reader-tools"]
        self.tool_names = ["open_document"]
        await self.invoke(grouped_history())
        self.assertEqual(self.loader.await_count, 2)
        self.assertEqual(set(self.metadata["tools"]), {"open_document"})
        self.assertEqual(self.loader.call_args.args[1], ["reader-tools"])
        self.assertNotIn("TOOL_RESULT_42", json.dumps(self.routed["messages"]))

    async def test_changed_skill_selection_reloads_capabilities(self):
        await self.invoke(grouped_history()[:4])
        self.models["agent-a"].meta["skillIds"] = ["specialist-skill"]
        await self.invoke(grouped_history())
        self.assertEqual(self.loader.await_count, 2)
        self.assertIn("Skill instructions for specialist-skill", self.routed["messages"][0]["content"])

    async def test_equivalent_normalized_selections_reuse_capabilities(self):
        self.models["agent-a"].meta.update(toolIds=[" toolkit ", "toolkit", ""], skillIds=[" skill ", "skill"])
        await self.invoke(grouped_history()[:4])
        self.models["agent-a"].meta.update(toolIds=["toolkit"], skillIds=["skill"])
        await self.invoke(grouped_history())
        self.loader.assert_awaited_once()
        self.assertEqual(self.loader.call_args.args[1], ["toolkit"])


class PublicRoutingTests(PipeTestCase):
    async def test_only_v2_agent_id_marker_starts_handoff(self):
        for receipt in (
            {"__lite_delegate__": "v1", "skill_id": "agent-a"},
            {"__lite_delegate__": "v2", "skill_id": "agent-a"},
        ):
            with self.subTest(receipt=receipt):
                await self.invoke([
                    {"role": "user", "content": "question"},
                    assistant(call("delegate", "lite_delegate")), result("delegate", json.dumps(receipt)),
                ])
                self.assertEqual(self.routed["model"], "base-model")
        await self.invoke(grouped_history())
        self.assertEqual(self.routed["model"], "agent-a")

    async def test_unrelated_tool_marker_keeps_configured_orchestrator(self):
        returned = await self.invoke([
            {"role": "user", "content": "question"},
            assistant(call("lookup", "lookup")), result("lookup", marker()),
        ])
        self.assertEqual(self.routed["model"], "base-model")
        self.assertIs(returned, self.completion_result)
        self.events.assert_not_awaited()

    async def test_routing_skill_alias_selects_agent_and_emits_status(self):
        await self.invoke([
            {"role": "user", "content": "question"},
            assistant(call("delegate", "lite_delegate")), result("delegate", marker("route-a")),
        ])
        self.assertEqual(self.routed["model"], "agent-a")
        self.assertEqual(self.events.call_args.args[0], {
            "type": "status", "data": {"action": "lite_delegate", "description": "delegate to Agent A", "done": True},
        })

    async def test_active_agent_continues_without_delegate_exchange(self):
        await self.invoke(grouped_history()[:4])
        current = copy.deepcopy(self.routed["messages"])
        current += [assistant(call("next", "lookup")), result("next", "second result")]
        await self.invoke(current)
        self.assertEqual(self.routed["model"], "agent-a")
        self.assertEqual([m["tool_call_id"] for m in self.routed["messages"] if m["role"] == "tool"], ["next"])
        self.loader.assert_awaited_once()

    async def test_unregistered_agent_fails_before_completion(self):
        with self.assertRaisesRegex(ValueError, 'Agent ID "unknown" is not available'):
            await self.invoke([
                {"role": "user", "content": "question"},
                assistant(call("delegate", "lite_delegate")), result("delegate", marker("unknown")),
            ])
        self.completion.assert_not_awaited()

    async def test_unavailable_destination_fails_before_completion(self):
        del self.request.app.state.MODELS["agent-a"]
        with self.assertRaisesRegex(ValueError, 'Agent "agent-a" is unavailable'):
            await self.invoke(grouped_history()[:4])
        self.completion.assert_not_awaited()

    async def test_inactive_destination_fails_before_completion(self):
        self.models["agent-a"].is_active = False
        with self.assertRaisesRegex(ValueError, 'Agent "agent-a" is inactive'):
            await self.invoke(grouped_history()[:4])
        self.completion.assert_not_awaited()

    async def test_unavailable_tool_fails_before_completion(self):
        self.loader.return_value = {}
        self.loader.side_effect = None
        with self.assertRaisesRegex(ValueError, "Attached model-bound Tools are unavailable: toolkit"):
            await self.invoke(grouped_history()[:4])
        self.completion.assert_not_awaited()

    async def test_unavailable_skill_fails_before_completion(self):
        self.models["agent-a"].meta["skillIds"] = ["missing"]
        self.skills.side_effect = None
        self.skills.return_value = None
        with self.assertRaisesRegex(ValueError, "Attached model Skills are unavailable: missing"):
            await self.invoke(grouped_history()[:4])
        self.completion.assert_not_awaited()

    async def test_unavailable_owner_fails_before_completion(self):
        self.owner = None
        with self.assertRaisesRegex(ValueError, "Model capability owner is unavailable"):
            await self.invoke(grouped_history()[:4])
        self.completion.assert_not_awaited()


class WorkspaceCapabilityTests(PipeTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.metadata.update(
            session_id="session", params={"function_calling": "native"}, features={"web_search": True},
        )
        self.models["agent-a"].meta["toolIds"] = []
        self.builtins.return_value = {
            name: {"spec": {"name": name}, "callable": AsyncMock()}
            for name in ("search_web", "delegate_task", "timer")
        }

    async def test_child_gets_enabled_builtins_but_not_nested_subagents(self):
        await self.invoke(grouped_history()[:4])
        self.assertEqual(set(self.metadata["tools"]), {"search_web"})
        self.assertEqual(self.builtins.call_args.kwargs["features"], {"web_search": True})
        self.assertEqual(self.builtins.call_args.args[1]["__user__"], {"id": "user"})

    async def test_builtin_tools_capability_can_disable_all_builtins(self):
        self.request.app.state.MODELS["agent-a"]["info"] = {
            "meta": {"capabilities": {"builtin_tools": False}},
        }
        await self.invoke(grouped_history()[:4])
        self.assertEqual(self.metadata["tools"], {})
        self.builtins.assert_not_awaited()

    async def test_legacy_function_calling_does_not_inject_native_builtins(self):
        self.metadata["params"]["function_calling"] = "legacy"
        await self.invoke(grouped_history()[:4])
        self.assertEqual(self.metadata["tools"], {})
        self.builtins.assert_not_awaited()

    async def test_attached_knowledge_is_rendered_for_child_prompt(self):
        self.request.app.state.MODELS["agent-a"]["info"] = {"meta": {"knowledge": [{
            "type": "file", "id": "file-1", "name": 'Guide "A"', "source": "model",
        }]}}
        await self.invoke(grouped_history()[:4])
        prompt = self.routed["messages"][0]["content"]
        self.assertIn('<knowledge type="file" id="file-1"', prompt)
        self.assertIn('name="Guide &quot;A&quot;"', prompt)

    async def test_child_skill_manifest_and_loader_use_only_attached_skills(self):
        self.models["agent-a"].meta["skillIds"] = ["specialist-skill"]
        view_skill = AsyncMock(return_value="Loaded specialist Skill")
        self.builtins.return_value["view_skill"] = {
            "spec": {"name": "view_skill"}, "callable": view_skill,
        }
        await self.invoke(grouped_history()[:4])
        prompt = self.routed["messages"][0]["content"]
        self.assertIn("<available_skills>", prompt)
        self.assertIn("specialist-skill", prompt)
        self.assertNotIn("Skill instructions for specialist-skill", prompt)
        tool = self.metadata["tools"]["view_skill"]["callable"]
        self.assertEqual(await tool(id="specialist-skill"), "Loaded specialist Skill")
        self.assertIn("error", json.loads(await tool(id="routing-skill")))
        view_skill.assert_awaited_once_with(id="specialist-skill")
        self.assertEqual(self.builtins.call_args.args[1]["__user__"], {"id": "user"})

    async def test_disabled_builtins_deliver_full_attached_skills(self):
        self.models["agent-a"].meta["skillIds"] = ["specialist-skill"]
        self.request.app.state.MODELS["agent-a"]["info"] = {
            "meta": {"capabilities": {"builtin_tools": False}},
        }
        await self.invoke(grouped_history()[:4])
        self.assertIn("Skill instructions for specialist-skill", self.routed["messages"][0]["content"])
        self.assertNotIn("view_skill", self.metadata["tools"])
        self.builtins.assert_not_awaited()

    async def test_pipe_destination_without_workspace_attachments_remains_supported(self):
        del self.models["agent-a"]
        self.request.app.state.MODELS["agent-a"]["pipe"] = {"type": "pipe"}
        await self.invoke(grouped_history()[:4])
        self.assertEqual(self.routed["model"], "agent-a")
        self.assertEqual(self.metadata["tools"], {})
        self.loader.assert_not_awaited()
        self.builtins.assert_not_awaited()


class CompletionTests(PipeTestCase):
    async def test_provider_failure_keeps_committed_preparation_without_retry(self):
        self.completion.side_effect = RuntimeError("provider failed")
        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            await self.invoke(grouped_history()[:4])
        self.completion.assert_awaited_once()
        self.assertEqual(self.metadata["lite_active_model_id"], "agent-a")
        self.assertEqual(self.metadata["lite_target_model_id"], "agent-a")
        self.assertEqual(self.routed["messages"], [grouped_history()[0]])
        self.assertEqual(await self.metadata["tools"]["lookup"]["callable"](), self.routed["messages"])
        self.assertEqual(self.metadata["lite_child_messages"], self.routed["messages"])

    async def test_provider_http_error_retains_message_on_both_routes(self):
        self.completion.return_value = router.Response(
            content='{"error":{"message":"provider refused request"}}', status_code=403,
        )
        for messages in ([{"role": "user", "content": "question"}], grouped_history()[:4]):
            with self.subTest(messages=messages):
                with self.assertRaisesRegex(RuntimeError, "provider refused request"):
                    await self.invoke(messages)

    async def test_streaming_child_response_keeps_identity(self):
        response = router.StreamingResponse(iter(["chunk"]))
        self.completion.return_value = response
        self.assertIs(await self.invoke(grouped_history()[:4]), response)

    async def test_status_failure_does_not_interrupt_child_completion(self):
        self.events.side_effect = RuntimeError("status channel closed")
        self.assertIs(await self.invoke(grouped_history()[:4]), self.completion_result)

    async def test_handoff_status_can_be_disabled(self):
        self.pipe.valves.emit_handoff_status = False
        await self.invoke(grouped_history()[:4])
        self.events.assert_not_awaited()


class ChildFilterPipelineTests(PipeTestCase):
    async def test_additional_history_filters_do_not_change_router_preparation_evidence(self):
        evidence = copy.deepcopy(self.metadata["lite_router_filter_pipeline"])
        previous = load_plain_module("previous_tool_context.py", "additional_previous_tests").Filter()
        cleanup = load_plain_module("history_cleanup.py", "additional_cleanup_tests").Filter()
        self.filters.extend([previous, cleanup])
        await self.invoke(grouped_history()[:4])
        self.assertEqual(self.metadata["lite_router_filter_pipeline"], evidence)

    async def test_pipe_metadata_is_authoritative_and_managed_state_is_mirrored(self):
        foreign_body_metadata = {"lite_active_handoff": {"agent_id": "agent-b", "__lite_delegate__": "v2"}}
        request_state = {
            "lite_active_model_id": "obsolete", "lite_subagent_filter_run": True,
            "lite_active_skill_id": "legacy", "platform": "keep request state",
        }
        self.request.state.metadata = request_state
        self.metadata["platform"] = "keep supplied state"
        shared = self.metadata["tools"]
        await self.invoke_body({"metadata": foreign_body_metadata, "messages": grouped_history()[:4]})
        self.assertEqual(self.routed["model"], "agent-a")
        self.assertIs(self.routed["metadata"], self.metadata)
        self.assertIs(request_state["tools"], shared)
        for key in ("lite_active_model_id", "lite_active_handoff", "lite_target_skill_ids", "skill_ids",
                    "tool_ids", "lite_view_skill_available"):
            self.assertEqual(request_state[key], self.metadata[key])
        self.assertNotIn("lite_subagent_filter_run", request_state)
        self.assertNotIn("lite_active_skill_id", request_state)
        self.assertEqual(request_state["platform"], "keep request state")
        self.assertEqual(self.metadata["platform"], "keep supplied state")
        self.assertEqual(foreign_body_metadata["lite_active_handoff"]["agent_id"], "agent-b")
        snapshot = dict(self.metadata)
        self.completion.reset_mock()

        async def fail(body):
            raise RuntimeError("failed continuation")

        self.filters.append(types.SimpleNamespace(inlet=fail))
        with self.assertRaisesRegex(RuntimeError, "failed continuation"):
            await self.invoke_body({"metadata": foreign_body_metadata, "messages": grouped_history()})
        self.completion.assert_not_awaited()
        self.assertEqual(self.metadata, snapshot)
        for key in snapshot:
            if key != "platform":
                self.assertEqual(request_state[key], self.metadata[key])
        self.assertNotIn("lite_subagent_filter_run", request_state)
        self.assertIs(request_state["tools"], shared)

    async def test_failed_switch_restores_live_history_tools_and_capability_cache(self):
        await self.invoke(grouped_history()[:4])
        shared = self.metadata["tools"]
        lookup = shared["lookup"]["callable"]
        history = self.metadata["lite_child_messages"]
        previous_history = copy.deepcopy(history)
        cache = self.metadata["lite_active_tool_runtime"]
        self.metadata["lite_agents"]["agent-a"]["model_id"] = "agent-b"
        state = dict(self.metadata)
        self.completion.reset_mock()

        async def fail(body):
            body["metadata"]["lite_child_messages"].append({"role": "user", "content": "partial"})
            raise RuntimeError("second filter failed")

        self.filters.append(types.SimpleNamespace(inlet=fail))
        with self.assertRaisesRegex(RuntimeError, "second filter failed"):
            await self.invoke(grouped_history())
        self.completion.assert_not_awaited()
        self.assertEqual(self.metadata, state)
        self.assertIs(self.metadata["tools"], shared)
        self.assertIs(shared["lookup"]["callable"], lookup)
        self.assertIs(self.metadata["lite_child_messages"], history)
        self.assertEqual(await lookup(), previous_history)
        self.assertIs(self.metadata["lite_active_tool_runtime"], cache)
        self.assertEqual(self.metadata["lite_active_model_id"], "agent-a")
        self.filters.pop()
        await self.invoke(grouped_history())
        self.assertEqual(self.routed["model"], "agent-b")
        self.assertIs(self.metadata["lite_child_messages"], history)

    async def test_invalid_body_restores_state_without_completion(self):
        original = dict(self.metadata)

        async def invalid(body):
            return {**body, "messages": None}

        self.filters.append(types.SimpleNamespace(inlet=invalid))
        with self.assertRaisesRegex(TypeError, "invalid request body"):
            await self.invoke(grouped_history()[:4])
        self.completion.assert_not_awaited()
        self.assertEqual(self.metadata, original)

    async def test_failed_preparation_keeps_new_mcp_client_registered(self):
        client = types.SimpleNamespace(call_tool=AsyncMock(return_value="available"))
        earlier_client = types.SimpleNamespace(call_tool=AsyncMock())
        clients = {"old": earlier_client}
        self.metadata["mcp_clients"] = clients
        self.models["agent-a"].meta["toolIds"] = ["toolkit", "server:mcp:documents"]
        self.connector.return_value = (client, [{"name": "fetch"}])
        self.models["agent-a"].meta["skillIds"] = ["missing"]
        self.skills.return_value = None
        self.skills.side_effect = None
        shared = self.metadata["tools"]
        with self.assertRaisesRegex(ValueError, "Skills are unavailable"):
            await self.invoke(grouped_history()[:4])
        self.completion.assert_not_awaited()
        self.connector.assert_awaited_once()
        self.assertIs(self.metadata["mcp_clients"], clients)
        self.assertIs(clients["documents"], client)
        self.assertIs(clients["old"], earlier_client)
        self.assertIs(self.metadata["tools"], shared)
        self.assertEqual(shared, {})
        self.assertNotIn("lite_active_handoff", self.metadata)
        self.assertNotIn("lite_active_tool_runtime", self.metadata)

    async def test_failed_initial_preparation_restores_state_and_allows_next_invocation(self):
        shared = self.metadata["tools"]
        original = dict(self.metadata)

        async def fail(body):
            raise RuntimeError("partial filter failure")

        self.filters.append(types.SimpleNamespace(inlet=fail))
        with self.assertRaisesRegex(RuntimeError, "partial filter failure"):
            await self.invoke(grouped_history()[:4])
        self.completion.assert_not_awaited()
        self.assertEqual(self.metadata, original)
        self.assertIs(self.metadata["tools"], shared)
        self.assertEqual(shared, {})
        self.filters.pop()
        await self.invoke(grouped_history()[:4])
        self.assertEqual(self.routed["model"], "agent-a")

    async def test_runs_destination_model_inlet_pipeline(self):
        self.metadata["filter_ids"] = ["enabled-toggle"]
        await self.invoke(grouped_history()[:4])
        self.assertIs(self.routed["metadata"], self.metadata)
        self.get_filters.assert_awaited_once_with(
            self.request, self.request.app.state.MODELS["agent-a"], ["enabled-toggle"],
        )
        self.assertEqual(self.dispatch_filters.call_args.kwargs["filter_type"], "inlet")
        self.assertNotIn("lite_subagent_filter_run", self.metadata)

    async def test_handoff_dispatches_without_destination_filters(self):
        self.filters = []
        await self.invoke(grouped_history()[:4])
        self.assertEqual(self.routed["model"], "agent-a")
        self.assertEqual(self.routed["messages"], [grouped_history()[0]])

    async def test_additional_filters_keep_platform_order(self):
        async def first(body):
            return {**body, "messages": [*body["messages"], {"role": "assistant", "content": "First filter"}]}

        async def second(body):
            self.assertEqual(body["messages"][-1]["content"], "First filter")
            return {**body, "messages": [*body["messages"], {"role": "assistant", "content": "Second filter"}]}

        self.filters = [types.SimpleNamespace(inlet=first), types.SimpleNamespace(inlet=second)]
        await self.invoke(grouped_history()[:4])
        self.assertEqual([m["content"] for m in self.routed["messages"][-2:]], ["First filter", "Second filter"])
        self.dispatch_filters.assert_awaited_once()
        self.assertNotIn("lite_subagent_filter_run", self.metadata)


if __name__ == "__main__":
    unittest.main()
