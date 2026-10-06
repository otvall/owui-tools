"""Both preparation Functions together through the public inlets and Pipe."""

import types
from unittest.mock import AsyncMock

import test_router_preparation as router_preparation

from test_capability_context import owui_callable, owui_refresh
from test_handoff_history import assistant, call, marker, result, router
from test_previous_turn_context import unpack_record
from test_subagent_preparation import preparation_module


class CombinedPreparationTests(router_preparation.RouterPreparationTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.child_preparation = preparation_module.Filter()
        self.context_filter = self.child_preparation
        self.filters = [self.child_preparation]

    async def load_nested_model_tool(self, request, ids, owner, extra_params):
        self.loaded_model_ids.append(extra_params["__metadata__"].get("model_id"))

        async def nested_model_call(__metadata__: dict, __request__, __user__: dict):
            user = await router.Users.get_user_by_id(__user__["id"])
            return await router.generate_chat_completion(__request__, {
                "model": str(__metadata__.get("model_id")),
                "metadata": __metadata__,
                "messages": [{"role": "user", "content": "Nested Tool inference"}],
                "stream": False,
            }, user)

        return {"lookup": {
            "tool_id": ids[0], "spec": {"name": "lookup"},
            "callable": owui_callable(nested_model_call, extra_params),
        }}

    async def run_nested_model_tool(self, expected):
        tool = self.metadata["tools"]["lookup"]["callable"]
        native = owui_refresh(tool, {"__messages__": [], "__files__": ["current file"]})
        await native()
        request, payload, user = self.completion.call_args.args
        self.assertEqual(payload["model"], expected)
        self.assertIs(request, self.request)
        self.assertIs(user, self.user)
        self.assertEqual(payload["metadata"]["model_id"], expected)
        # OWUI merges request-state metadata over the caller's payload metadata.
        effective_metadata = {**payload["metadata"], **request.state.metadata}
        self.assertEqual(effective_metadata["model_id"], expected)

    async def test_nested_tool_inference_uses_workspace_id_on_handoff_and_cached_continuation(self):
        self.loaded_model_ids = []
        self.loader.side_effect = self.load_nested_model_tool
        self.request.app.state.MODELS["agent-a"]["info"] = {"base_model_id": "physical-child"}
        for separate_metadata in (False, True):
            with self.subTest(separate_metadata=separate_metadata):
                self.metadata["model_id"] = "lite_handoff_router"
                request_metadata = (
                    {"model_id": "lite_handoff_router", "platform": "keep"}
                    if separate_metadata else self.metadata
                )
                self.begin_request(metadata=request_metadata)
                body = {"model": "router", "metadata": self.metadata, "messages": [
                    {"role": "user", "content": "Find curves"},
                    assistant(call("handoff", "lite_delegate", agent_id="route-a")),
                    result("handoff", marker("route-a")),
                ]}
                await self.preparation.inlet(body, __request__=self.request, __user__={"id": "user"})
                self.loader.reset_mock()
                await self.invoke_body(body)
                self.assertEqual(self.routed["model"], "agent-a")
                saved_tool = self.metadata["tools"]["lookup"]["callable"]
                await self.run_nested_model_tool("agent-a")
                self.assertEqual(self.loaded_model_ids[-1], "agent-a")

                # A continuation can carry the outer Pipe ID again; reused Tools
                # must observe the newly selected Workspace identity every time.
                self.metadata["model_id"] = "lite_handoff_router"
                request_metadata["model_id"] = "lite_handoff_router"
                body["messages"] += [assistant(call("lookup", "lookup")), result("lookup", "Curves")]
                await self.invoke_body(body)
                self.assertIs(self.metadata["tools"]["lookup"]["callable"], saved_tool)
                self.loader.assert_awaited_once()
                await self.run_nested_model_tool("agent-a")
                if separate_metadata:
                    self.assertEqual(request_metadata["platform"], "keep")

    async def test_nested_tool_model_id_follows_orchestrator_child_and_new_request(self):
        self.loaded_model_ids = []
        self.loader.side_effect = self.load_nested_model_tool
        self.preparation.valves.base_tool_ids = ["base-tool"]
        request_metadata = {"model_id": "lite_handoff_router", "platform": "keep"}
        for target in (None, "agent-a", "agent-b", None):
            with self.subTest(target=target):
                self.metadata["model_id"] = "lite_handoff_router"
                request_metadata["model_id"] = "lite_handoff_router"
                self.begin_request(metadata=request_metadata)
                messages = [{"role": "user", "content": "New question"}]
                if target is not None:
                    messages += [
                        assistant(call("handoff", "lite_delegate", agent_id=target)),
                        result("handoff", marker(target)),
                    ]
                body = {"model": "router", "metadata": self.metadata, "messages": messages}
                await self.preparation.inlet(body, __request__=self.request, __user__={"id": "user"})
                await self.invoke_body(body)
                expected = target or "base-model"
                self.assertEqual(self.routed["model"], expected)
                await self.run_nested_model_tool(expected)
                self.assertEqual(self.loaded_model_ids[-1], expected)
                self.assertEqual(request_metadata["platform"], "keep")
                if target is None:
                    saved_tool = self.metadata["tools"]["lookup"]["callable"]
                    self.metadata["model_id"] = "lite_handoff_router"
                    request_metadata["model_id"] = "lite_handoff_router"
                    body["messages"] += [assistant(call("lookup", "lookup")), result("lookup", "Answer")]
                    await self.invoke_body(body)
                    self.assertIs(self.metadata["tools"]["lookup"]["callable"], saved_tool)
                    await self.run_nested_model_tool(expected)

    async def test_failed_initial_child_preparation_restores_model_id_value_or_absence(self):
        self.loaded_model_ids = []
        self.loader.side_effect = self.load_nested_model_tool

        async def fail(body):
            raise RuntimeError("destination preparation failed")

        for initial, request_initial in (
            ({}, {}),
            ({"model_id": "lite_handoff_router"}, {"model_id": "request-router"}),
            ({}, {"model_id": "request-router"}),
        ):
            with self.subTest(initial=initial, request_initial=request_initial):
                self.metadata.pop("model_id", None)
                self.metadata.update(initial)
                request_metadata = {**request_initial, "platform": "keep"}
                self.begin_request(metadata=request_metadata)
                body = {"model": "router", "metadata": self.metadata, "messages": [
                    {"role": "user", "content": "Find curves"},
                    assistant(call("handoff", "lite_delegate", agent_id="agent-a")),
                    result("handoff", marker()),
                ]}
                await self.preparation.inlet(body, __request__=self.request, __user__={"id": "user"})
                self.filters.append(types.SimpleNamespace(inlet=fail))
                self.completion.reset_mock()
                try:
                    with self.assertRaisesRegex(RuntimeError, "destination preparation failed"):
                        await self.invoke_body(body)
                finally:
                    self.filters.pop()
                self.completion.assert_not_awaited()
                self.assertEqual("model_id" in self.metadata, "model_id" in initial)
                self.assertEqual("model_id" in request_metadata, "model_id" in request_initial)
                self.assertEqual(self.metadata.get("model_id"), initial.get("model_id"))
                self.assertEqual(request_metadata.get("model_id"), request_initial.get("model_id"))
                self.assertEqual(request_metadata["platform"], "keep")

    async def test_cached_tool_with_copied_metadata_uses_current_orchestrator_selection(self):
        self.loaded_model_ids = []
        self.loader.side_effect = self.load_nested_model_tool
        self.preparation.valves.base_tool_ids = ["base-tool"]
        self.metadata["model_id"] = "lite_handoff_router"
        self.begin_request(metadata={"model_id": "lite_handoff_router"})
        body = {"model": "router", "metadata": self.metadata, "messages": [
            {"role": "user", "content": "Find curves"},
        ]}
        await self.preparation.inlet(body, __request__=self.request, __user__={"id": "user"})
        await self.invoke_body(body)
        tool = self.metadata["tools"]["lookup"]["callable"]
        captured_metadata = tool.__extra_params__["__metadata__"]

        # Function dispatch can pass a new metadata dict while retaining the
        # cached capability set and its original OWUI-bound Tool injections.
        self.metadata = {**self.metadata, "model_id": "lite_handoff_router"}
        self.pipe.valves.orchestrator_model_id = "second-base-model"
        body["messages"] += [assistant(call("lookup", "lookup")), result("lookup", "Curves")]
        await self.invoke_body(body)
        self.loader.assert_awaited_once()
        self.assertIs(self.metadata["tools"]["lookup"]["callable"], tool)
        self.assertIs(tool.__extra_params__["__metadata__"], captured_metadata)
        await self.run_nested_model_tool("second-base-model")

    async def test_failed_child_switch_restores_identity_and_provider_error_keeps_commit(self):
        self.loaded_model_ids = []
        self.loader.side_effect = self.load_nested_model_tool
        self.metadata["model_id"] = "lite_handoff_router"
        self.begin_request(metadata={"model_id": "lite_handoff_router"})
        body = {"model": "router", "metadata": self.metadata, "messages": [
            {"role": "user", "content": "Find curves"},
            assistant(call("handoff", "lite_delegate", agent_id="agent-a")),
            result("handoff", marker()),
        ]}
        await self.preparation.inlet(body, __request__=self.request, __user__={"id": "user"})
        await self.invoke_body(body)
        saved_tool = self.metadata["tools"]["lookup"]["callable"]
        self.metadata["lite_active_handoff"] = marker("agent-b")

        async def fail(body):
            raise RuntimeError("destination preparation failed")

        self.filters.append(types.SimpleNamespace(inlet=fail))
        self.completion.reset_mock()
        try:
            with self.assertRaisesRegex(RuntimeError, "destination preparation failed"):
                await self.invoke_body(body)
        finally:
            self.filters.pop()
        self.completion.assert_not_awaited()
        self.assertEqual(self.metadata["model_id"], "agent-a")
        self.assertEqual(self.request.state.metadata["model_id"], "agent-a")
        self.assertIs(self.metadata["tools"]["lookup"]["callable"], saved_tool)
        await self.run_nested_model_tool("agent-a")

        self.completion.side_effect = RuntimeError("provider failed")
        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            await self.invoke_body(body)
        self.assertEqual(self.routed["model"], "agent-b")
        self.assertEqual(self.metadata["model_id"], "agent-b")
        self.assertEqual(self.request.state.metadata["model_id"], "agent-b")

    async def test_both_roles_apply_common_history_limits_to_two_children_with_record_disabled(self):
        self.preparation.valves.enabled = False
        self.child_preparation.valves.history_turns = 1
        self.child_preparation.valves.history_tool_calls = 1
        previous = [
            {"role": "user", "content": "Previous question"},
            assistant(call("delegate-a", "lite_delegate", agent_id="agent-a")),
            result("delegate-a", marker()),
            assistant(call("lookup-a", "lookup")), result("lookup-a", "A result"),
            assistant(call("delegate-b", "lite_delegate", agent_id="agent-b")),
            result("delegate-b", marker("agent-b")),
            assistant(call("lookup-b", "lookup")), result("lookup-b", "B result"),
            {"role": "assistant", "content": "Previous answer"},
        ]
        for agent_id, expected in (("agent-a", "A result"), ("agent-b", "B result")):
            with self.subTest(agent_id=agent_id):
                self.begin_request()
                body = {"model": "router", "metadata": self.metadata, "messages": [
                    *previous, {"role": "user", "content": "Follow up"},
                    assistant(call("delegate", "lite_delegate", agent_id=agent_id)),
                    result("delegate", marker(agent_id)),
                ]}
                await self.preparation.inlet(body, __request__=self.request, __user__={"id": "user"})
                await self.invoke_body(body)
                self.assertEqual(self.routed["model"], agent_id)
                self.assertEqual([m["content"] for m in self.routed["messages"] if m["role"] == "tool"], [expected])
                self.assertEqual(await self.metadata["tools"]["lookup"]["callable"](), self.routed["messages"])

    async def test_orchestrator_handoff_and_child_skill_removal_use_one_router_preparation(self):
        self.metadata.update(session_id="session", params={"function_calling": "native"})
        self.preparation.valves.base_tool_ids = ["base-tool"]
        self.models["agent-a"].meta["skillIds"] = ["specialist"]
        self.builtins.return_value = {
            "view_skill": {"spec": {"name": "view_skill"}, "callable": AsyncMock(return_value="Loaded")},
        }
        self.begin_request()
        body = {"model": "router", "metadata": self.metadata, "messages": [
            {"role": "user", "content": "Previous question"},
            assistant(call("previous", "lookup")), result("previous", "Previous result"),
            {"role": "assistant", "content": "Previous answer"},
            {"role": "user", "content": "Follow up"},
        ]}
        await self.preparation.inlet(body, __request__=self.request, __user__={"id": "user"})
        await self.invoke_body(body)
        self.assertEqual(self.routed["model"], "base-model")
        self.assertEqual(unpack_record(self.routed["messages"])["tool_exchanges"][0]["result"]["content"], "Previous result")

        body["messages"] += [
            assistant(call("handoff", "lite_delegate", agent_id="route-a")), result("handoff", marker("route-a")),
        ]
        await self.invoke_body(body)
        self.assertEqual(self.routed["model"], "agent-a")
        loader = self.metadata["tools"]["view_skill"]["callable"]
        self.assertEqual(await loader(id="specialist"), "Loaded")
        self.assertNotIn("<id>route-a</id>", self.routed["messages"][0]["content"])
        self.assertEqual(await loader(id="route-a"), "Loaded")
        self.builtins.return_value["view_skill"]["callable"].assert_any_await(id="route-a")
        lookup = self.metadata["tools"]["lookup"]["callable"]
        self.assertEqual(self.loader.await_count, 2)

        # Fresh database selection wins over cached runtime metadata on continuation.
        self.request.app.state.MODELS["agent-a"]["info"] = {"meta": {"skillIds": ["specialist"]}}
        self.models["agent-a"].meta["skillIds"] = []
        body["messages"] += [assistant(call("child", "lookup")), result("child", "Current result")]
        await self.invoke_body(body)
        self.assertNotIn("view_skill", self.metadata["tools"])
        self.assertEqual([m["content"] for m in self.routed["messages"] if m["role"] == "tool"], ["Current result"])
        self.assertEqual(await lookup(), self.routed["messages"])
        self.assertEqual(await self.metadata["tools"]["lookup"]["callable"](), self.routed["messages"])
        self.assertEqual(self.loader.await_count, 3)  # Changed Skill IDs invalidate capabilities.
