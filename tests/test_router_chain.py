"""Router lifecycle guarantees through actual public Function entry points."""

import copy

from starlette.requests import Request

from test_handoff_history import (
    PipeTestCase, assistant, call, cleanup_module, grouped_history, marker, previous_module, result,
)
from test_previous_turn_context import previous_turn, unpack_record


class RouterChainTests(PipeTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.metadata = {"tools": {}}
        self.request.state.metadata = self.metadata
        self.registry = self.registry_filter()
        self.previous = previous_module.Filter()
        self.cleanup = cleanup_module.Filter()
        self.body = {
            "model": "router", "metadata": self.metadata,
            "messages": previous_turn() + [{"role": "user", "content": "Next question"}],
        }

    async def test_repeated_trimmed_new_request_without_message_id_resets_router_chain(self):
        self.begin_request()
        first = {
            "model": "router", "metadata": self.metadata,
            "messages": [{"role": "user", "content": "Repeat this"}],
        }
        await self.router_inlets(first, registry=self.registry)
        await self.invoke_body(first)
        shared = self.metadata["tools"]
        self.completion.reset_mock()

        # A new HTTP request can reuse metadata and present identical trimmed text.
        request_metadata = {**self.metadata, "platform": "keep request state"}
        self.begin_request(metadata=request_metadata)
        second = {
            "model": "router", "metadata": self.metadata,
            "messages": [{"role": "user", "content": "Repeat this"}],
        }
        await self.registry.inlet(second, __request__=self.request, __user__={"id": "user"})
        with self.assertRaisesRegex(ValueError, "Previous Tool Context"):
            await self.invoke_body(second)
        self.completion.assert_not_awaited()

        await self.context_inlets(second)
        await self.invoke_body(second)
        self.completion.assert_awaited_once()
        self.assertEqual(self.routed["model"], "base-model")
        self.assertEqual([m["content"] for m in self.routed["messages"] if m["role"] == "user"], ["Repeat this"])
        self.assertIs(self.metadata["tools"], shared)
        self.assertIs(request_metadata["tools"], shared)
        self.assertEqual(request_metadata["platform"], "keep request state")

        # A fresh Request wrapper over the same HTTP scope is still the same request.
        same_request = Request(self.request.scope)
        with self.assertRaisesRegex(ValueError, "Registry.*before.*Previous Tool Context"):
            await self.registry.inlet(copy.deepcopy(second), __request__=same_request, __user__={"id": "user"})
        self.completion.assert_awaited_once()

    async def test_standalone_context_flags_cannot_replace_registry(self):
        await self.previous.inlet(self.body, __request__=self.request)
        await self.cleanup.inlet(self.body, __request__=self.request)
        original = copy.deepcopy(self.metadata)

        for messages in (self.body["messages"], self.body["messages"] + [
            assistant(call("next-delegate", "lite_delegate", agent_id="agent-a")),
            result("next-delegate", marker()),
        ]):
            with self.subTest(child=len(messages) > len(self.body["messages"])):
                with self.assertRaisesRegex(ValueError, "Registry"):
                    await self.invoke_body({**self.body, "messages": messages})
                self.completion.assert_not_awaited()
                self.assertEqual(self.metadata, original)

    async def test_missing_context_component_blocks_both_completion_routes(self):
        for missing, label in ((self.previous, "Previous Tool Context"), (self.cleanup, "History Cleanup")):
            for messages in ([{"role": "user", "content": "question"}], grouped_history()[:4]):
                with self.subTest(component=label, child=len(messages) > 1):
                    self.completion.reset_mock()
                    body = {"model": "router", "metadata": self.metadata, "messages": copy.deepcopy(messages)}
                    self.begin_request()
                    await self.registry.inlet(body, __request__=self.request, __user__={"id": "user"})
                    for context_filter in (self.previous, self.cleanup):
                        if context_filter is missing:
                            break
                        await context_filter.inlet(body, __request__=self.request)
                    with self.assertRaisesRegex(ValueError, label):
                        await self.invoke_body(body)
                    self.completion.assert_not_awaited()

    async def test_cleanup_rejects_bad_order_before_destroying_history(self):
        await self.registry.inlet(self.body, __request__=self.request, __user__={"id": "user"})
        original_messages = copy.deepcopy(self.body["messages"])

        with self.assertRaisesRegex(ValueError, "Previous Tool Context.*before.*History Cleanup"):
            await self.cleanup.inlet(self.body, __request__=self.request)

        self.assertEqual(self.body["messages"], original_messages)
        self.assertNotIn("history_cleanup_applied", self.metadata)
        # Even later applied flags cannot certify the rejected sequence.
        self.metadata.update(previous_tool_context_applied=True, history_cleanup_applied=True)
        with self.assertRaisesRegex(ValueError, "Previous Tool Context"):
            await self.invoke_body(self.body)
        self.completion.assert_not_awaited()
        with self.assertRaisesRegex(ValueError, "Previous Tool Context"):
            await self.previous.inlet(self.body, __request__=self.request)
        self.assertEqual(self.body["messages"], original_messages)

    async def test_registry_cannot_follow_context_filtering_on_the_same_request(self):
        for context_filter, label in ((self.previous, "Previous Tool Context"), (self.cleanup, "History Cleanup")):
            with self.subTest(component=label):
                self.metadata.clear()
                self.metadata["tools"] = {}
                body = {"model": "router", "metadata": self.metadata, "messages": previous_turn() + [
                    {"role": "user", "content": "Next question"},
                ]}
                await context_filter.inlet(body, __request__=self.request)
                original_messages = copy.deepcopy(body["messages"])
                original_metadata = dict(self.metadata)
                with self.assertRaisesRegex(ValueError, "Registry.*before.*" + label):
                    await self.registry.inlet(body, __request__=self.request, __user__={"id": "user"})
                self.assertEqual(body["messages"], original_messages)
                self.assertEqual(self.metadata, original_metadata)
                with self.assertRaisesRegex(ValueError, "Registry"):
                    await self.invoke_body(body)
                self.completion.assert_not_awaited()

    async def test_registry_reentry_cannot_recertify_the_same_cleaned_request(self):
        await self.router_inlets(self.body, registry=self.registry)
        original_metadata = dict(self.metadata)
        original_raw_history = copy.deepcopy(self.metadata["lite_unfiltered_messages"])
        for body in (self.body, copy.deepcopy(self.body)):
            with self.subTest(cloned=body is not self.body):
                # Registry receives the body-authoritative metadata, including cloned evidence.
                metadata = body["metadata"]
                with self.assertRaisesRegex(ValueError, "Registry.*before.*Previous Tool Context"):
                    await self.registry.inlet(body, __request__=self.request, __user__={"id": "user"})
                self.assertEqual(metadata, original_metadata)
                self.assertEqual(metadata["lite_unfiltered_messages"], original_raw_history)
        self.completion.assert_not_awaited()

    async def test_registry_cannot_erase_rejected_order_until_a_new_user_message(self):
        await self.registry.inlet(self.body, __request__=self.request, __user__={"id": "user"})
        with self.assertRaisesRegex(ValueError, "Previous Tool Context"):
            await self.cleanup.inlet(self.body, __request__=self.request)
        original_metadata = dict(self.metadata)
        with self.assertRaisesRegex(ValueError, "Registry.*before.*History Cleanup"):
            await self.registry.inlet(copy.deepcopy(self.body), __request__=self.request, __user__={"id": "user"})
        self.assertEqual(self.metadata, original_metadata)
        # A repeated question is still a new Execution user message.
        new_body = {**self.body, "messages": self.body["messages"] + [
            {"role": "assistant", "content": "Configuration fixed"},
            copy.deepcopy(self.body["messages"][-1]),
        ]}
        self.begin_request()
        await self.router_inlets(new_body, registry=self.registry)
        await self.invoke_body(new_body)
        self.assertEqual(self.routed["model"], "base-model")

    async def test_registry_boundary_survives_tool_images_and_uses_platform_request_ids(self):
        self.metadata.update(chat_id="chat", message_id="first-response")
        await self.router_inlets(self.body, registry=self.registry)
        image = {
            "role": "user", "content": [
                {"type": "text", "text": previous_module.TOOL_IMAGE_TEXT},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            ],
        }
        continued = {**self.body, "messages": self.body["messages"] + [image]}
        with self.assertRaisesRegex(ValueError, "Registry.*before.*Previous Tool Context"):
            await self.registry.inlet(copy.deepcopy(continued), __request__=self.request, __user__={"id": "user"})
        # The next platform request may present the same text and a trimmed history.
        self.metadata["message_id"] = "second-response"
        new_body = {"model": "router", "metadata": self.metadata, "messages": [
            copy.deepcopy(self.body["messages"][-1]),
        ]}
        self.begin_request()
        await self.router_inlets(new_body, registry=self.registry)
        await self.invoke_body(new_body)
        self.assertEqual(self.routed["model"], "base-model")

        # Without platform IDs, image service messages still do not create a boundary.
        self.metadata.pop("message_id")
        new_body["messages"] += [
            {"role": "assistant", "content": "Done"},
            copy.deepcopy(new_body["messages"][-1]),
        ]
        self.begin_request()
        await self.router_inlets(new_body, registry=self.registry)
        continued = {**new_body, "messages": new_body["messages"] + [image]}
        with self.assertRaisesRegex(ValueError, "Registry.*before.*Previous Tool Context"):
            await self.registry.inlet(copy.deepcopy(continued), __request__=self.request, __user__={"id": "user"})

    async def test_pipe_rejects_foreign_body_evidence_and_deletes_stale_request_evidence(self):
        await self.router_inlets(self.body, registry=self.registry)
        request_metadata = {**self.metadata, "platform": "keep request state"}
        self.request.state.metadata = request_metadata
        foreign_metadata = dict(self.metadata)
        self.metadata.pop("lite_router_filter_pipeline")
        self.metadata.pop("lite_router_request_key")
        self.metadata["platform"] = "keep Pipe state"
        shared = self.metadata["tools"]
        body = {**self.body, "metadata": foreign_metadata}

        with self.assertRaisesRegex(ValueError, "Registry"):
            await self.invoke_body(body)

        self.completion.assert_not_awaited()
        self.assertNotIn("lite_router_filter_pipeline", request_metadata)
        self.assertNotIn("lite_router_request_key", request_metadata)
        self.assertIn("lite_router_filter_pipeline", foreign_metadata)
        self.assertIn("lite_router_request_key", foreign_metadata)
        self.assertIs(request_metadata["tools"], shared)
        self.assertEqual(request_metadata["platform"], "keep request state")
        self.assertEqual(self.metadata["platform"], "keep Pipe state")

    async def test_valid_chain_captures_history_and_survives_child_continuations(self):
        self.context_filter.valves.history_turns = 1
        self.context_filter.valves.history_tool_calls = 1
        await self.router_inlets(self.body, registry=self.registry)
        pipeline = self.metadata["lite_router_filter_pipeline"]
        raw_history = self.metadata["lite_unfiltered_messages"]
        shared = self.metadata["tools"]
        await self.invoke_body(self.body)
        self.assertEqual(self.routed["model"], "base-model")
        record = unpack_record(self.routed["messages"])
        self.assertEqual(record["tool_exchanges"][-1]["result"]["content"], "TOOL_RESULT_42")
        self.assertFalse(any(message.get("role") == "tool" for message in self.routed["messages"]))

        self.body["messages"] += [
            assistant(call("next-delegate", "lite_delegate", agent_id="agent-a")),
            result("next-delegate", marker()),
        ]
        await self.invoke_body(self.body)
        self.assertEqual(self.routed["model"], "agent-a")
        self.assertEqual([m["content"] for m in self.routed["messages"] if m["role"] == "tool"], ["TOOL_RESULT_42"])
        self.assertIsNone(unpack_record(self.routed["messages"]))
        self.assertEqual([schema["function"]["name"] for schema in self.routed["tools"]], ["lookup"])
        child_history = self.metadata["lite_child_messages"]
        lookup = shared["lookup"]["callable"]

        self.body["messages"] += [assistant(call("current", "lookup")), result("current", "CURRENT_RESULT")]
        await self.invoke_body(self.body)
        self.assertEqual(self.routed["model"], "agent-a")
        self.assertEqual([m["content"] for m in self.routed["messages"] if m["role"] == "tool"], [
            "TOOL_RESULT_42", "CURRENT_RESULT",
        ])
        self.assertIs(self.metadata["lite_router_filter_pipeline"], pipeline)
        self.assertEqual(pipeline, ["lite_registry", "previous_tool_context", "history_cleanup"])
        self.assertIs(self.metadata["lite_unfiltered_messages"], raw_history)
        self.assertIs(self.metadata["tools"], shared)
        self.assertIs(self.metadata["lite_child_messages"], child_history)
        self.assertIs(shared["lookup"]["callable"], lookup)
        self.assertEqual(await lookup(), self.routed["messages"])
        self.loader.assert_awaited_once()

    async def test_new_request_resets_evidence_and_cannot_reuse_previous_success(self):
        self.body["messages"] += [
            assistant(call("next-delegate", "lite_delegate", agent_id="agent-a")),
            result("next-delegate", marker()),
        ]
        await self.router_inlets(self.body, registry=self.registry)
        await self.invoke_body(self.body)
        shared = self.metadata["tools"]
        client = object()
        clients = {"existing": client}
        self.metadata["mcp_clients"] = clients
        request_metadata = {**self.metadata, "platform": "keep request state"}
        self.request.state.metadata = request_metadata
        self.metadata["platform"] = "keep Pipe state"
        self.completion.reset_mock()
        new_body = {"model": "router", "metadata": self.metadata, "messages": self.body["messages"] + [
            {"role": "assistant", "content": "Done"},
            {"role": "user", "content": "New request"},
        ]}

        self.begin_request(metadata=request_metadata)
        await self.registry.inlet(new_body, __request__=self.request, __user__={"id": "user"})
        self.assertEqual(self.metadata["lite_router_filter_pipeline"], ["lite_registry"])
        self.assertIs(request_metadata["lite_router_filter_pipeline"], self.metadata["lite_router_filter_pipeline"])
        for key in ("lite_active_handoff", "lite_child_messages", "lite_unfiltered_messages",
                    "previous_tool_context_applied", "history_cleanup_applied"):
            self.assertNotIn(key, self.metadata)
            self.assertNotIn(key, request_metadata)
        # Old boolean evidence alone cannot complete the new request.
        self.metadata.update(previous_tool_context_applied=True, history_cleanup_applied=True)
        with self.assertRaisesRegex(ValueError, "Previous Tool Context"):
            await self.invoke_body(new_body)
        self.completion.assert_not_awaited()
        await self.previous.inlet(new_body, __request__=self.request)
        with self.assertRaisesRegex(ValueError, "History Cleanup"):
            await self.invoke_body(new_body)
        self.completion.assert_not_awaited()
        await self.cleanup.inlet(new_body, __request__=self.request)
        await self.invoke_body(new_body)
        self.assertEqual(self.routed["model"], "base-model")
        self.assertNotIn("lite_active_handoff", self.metadata)
        self.assertIs(self.metadata["tools"], shared)
        self.assertIs(self.metadata["mcp_clients"], clients)
        self.assertIs(clients["existing"], client)
        self.assertEqual(request_metadata["platform"], "keep request state")
        self.assertEqual(self.metadata["platform"], "keep Pipe state")

    async def test_failed_child_preparation_restores_evidence_across_distinct_metadata(self):
        self.metadata.update(session_id="session", params={"function_calling": "native"})
        self.models["agent-a"].meta["skillIds"] = ["child-skill"]
        self.body["messages"] += [
            assistant(call("next-delegate", "lite_delegate", agent_id="agent-a")),
            result("next-delegate", marker()),
        ]
        await self.router_inlets(self.body, registry=self.registry)
        await self.invoke_body(self.body)
        pipeline = self.metadata["lite_router_filter_pipeline"]
        request_key = self.metadata["lite_router_request_key"]
        saved_key = dict(request_key)
        history = self.metadata["lite_child_messages"]
        saved_history = copy.deepcopy(history)
        shared = self.metadata["tools"]
        lookup = shared["lookup"]["callable"]
        clients = {}
        self.metadata["mcp_clients"] = clients
        client = object()
        snapshot = dict(self.metadata)
        request_metadata = {"lite_router_filter_pipeline": ["stale"], "platform": "keep request state"}
        self.request.state.metadata = request_metadata
        foreign_metadata = {"lite_router_filter_pipeline": ["foreign"], "platform": "keep body state"}
        self.completion.reset_mock()

        async def fail(request, params, **kwargs):
            metadata = params["__metadata__"]
            metadata["lite_router_filter_pipeline"].append("history_cleanup")
            metadata["lite_router_request_key"]["message_id"] = "partial"
            metadata["mcp_clients"]["new"] = client
            raise RuntimeError("builtin preparation failed")

        self.builtins.side_effect = fail
        body = {**self.body, "metadata": foreign_metadata, "messages": self.body["messages"] + [
            assistant(call("current", "lookup")), result("current", "PARTIAL_RESULT"),
        ]}
        with self.assertRaisesRegex(RuntimeError, "builtin preparation failed"):
            await self.invoke_body(body)

        self.completion.assert_not_awaited()
        self.assertEqual(self.metadata, snapshot)
        self.assertIs(self.metadata["lite_router_filter_pipeline"], pipeline)
        self.assertEqual(pipeline, ["lite_registry", "previous_tool_context", "history_cleanup"])
        self.assertIs(request_metadata["lite_router_filter_pipeline"], pipeline)
        self.assertIs(self.metadata["lite_router_request_key"], request_key)
        self.assertEqual(request_key, saved_key)
        self.assertIs(request_metadata["lite_router_request_key"], request_key)
        self.assertIs(self.metadata["tools"], shared)
        self.assertIs(self.metadata["lite_child_messages"], history)
        self.assertEqual(history, saved_history)
        self.assertEqual(await lookup(), saved_history)
        self.assertIs(clients["new"], client)
        self.assertEqual(request_metadata["platform"], "keep request state")
        self.assertEqual(foreign_metadata, {"lite_router_filter_pipeline": ["foreign"], "platform": "keep body state"})
