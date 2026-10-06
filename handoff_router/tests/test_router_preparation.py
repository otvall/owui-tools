"""Router Preparation followed by the real Pipe and destination Filter chain."""

import copy
import types
from unittest.mock import AsyncMock, patch

from test_handoff_history import PipeTestCase, assistant, call, load_plain_module, marker, result, router
from test_previous_turn_context import unpack_record


preparation_module = load_plain_module(
    "router_preparation.py", "router_preparation_tests",
    {
        "open_webui.config": types.SimpleNamespace(BYPASS_ADMIN_ACCESS_CONTROL=False),
        "open_webui.env": types.SimpleNamespace(BYPASS_MODEL_ACCESS_CONTROL=False),
        "open_webui.models.models": types.SimpleNamespace(Models=router.Models),
        "open_webui.models.skills": types.SimpleNamespace(Skills=router.Skills),
        "open_webui.models.users": types.SimpleNamespace(Users=router.Users),
        "open_webui.utils.models": types.SimpleNamespace(check_model_access=AsyncMock()),
    },
)


class RouterPreparationTests(PipeTestCase):
    async def router_inlets(self, body, *, registry=None):
        self.enterContext(patch.dict(preparation_module.SUBAGENTS, {
            "agent-a": "route-a", "agent-b": "route-b",
        }, clear=True))
        self.preparation = preparation_module.Filter()
        self.preparation.valves.base_tool_ids = registry.valves.base_tool_ids if registry is not None else []
        self.preparation.valves.base_skill_ids = registry.valves.base_skill_ids if registry is not None else []
        return await self.preparation.inlet(body, __request__=self.request, __user__={"id": "user"})

    async def test_only_router_preparation_reaches_orchestrator_with_base_capabilities(self):
        self.begin_request()
        body = {"model": "router", "metadata": self.metadata, "messages": [
            {"role": "user", "content": "Plan a task"},
        ]}
        await self.router_inlets(body)
        self.preparation.valves.base_tool_ids = ["base-tool"]
        self.preparation.valves.base_skill_ids = ["base-skill"]
        # Start a fresh request with the owner's configured capabilities.
        self.begin_request()
        await self.preparation.inlet(body, __request__=self.request, __user__={"id": "user"})
        await self.invoke_body(body)

        self.assertEqual(self.routed["model"], "base-model")
        self.assertEqual(self.loader.call_args.args[1], ["base-tool"])
        self.assertEqual(self.loader.call_args.args[2], self.owner)
        self.assertEqual([tool["function"]["name"] for tool in self.routed["tools"]], ["lookup"])
        prompt = "\n".join(m["content"] for m in self.routed["messages"] if m["role"] == "system")
        for skill in ("base-skill", "route-a", "route-b"):
            self.assertIn("Skill instructions for " + skill, prompt)
        self.assertEqual(await self.metadata["tools"]["lookup"]["callable"](), self.routed["messages"])

    async def test_missing_current_preparation_blocks_dispatch_with_attachment_guidance(self):
        self.begin_request()
        body = {"model": "router", "metadata": self.metadata, "messages": [
            {"role": "user", "content": "A new request"},
        ]}
        with self.assertRaisesRegex(ValueError, "Router Preparation"):
            await self.invoke_body(body)
        self.completion.assert_not_awaited()

    async def test_record_toggle_preserves_source_for_handoff_and_current_tool_chain(self):
        previous = [
            {"role": "user", "content": "Previous question"},
            assistant(call("previous-handoff", "lite_delegate", agent_id="route-a")),
            result("previous-handoff", marker("route-a")),
            assistant(call("previous-lookup", "lookup")), result("previous-lookup", "previous result"),
            {"role": "assistant", "content": "Previous answer"},
        ]
        older = [
            {"role": "user", "content": "Older question"},
            assistant(call("older", "lookup")), result("older", "older result"),
            {"role": "assistant", "content": "Older answer"},
        ]
        current = [
            {"role": "user", "content": "Follow up"},
            assistant(call("handoff", "lite_delegate", agent_id="route-a")),
            result("handoff", marker("route-a")),
            assistant(call("current", "lookup")), result("current", "current result"),
        ]
        self.pipe.valves.history_turns = 1
        self.pipe.valves.history_tool_calls = 1
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.begin_request()
                self.preparation.valves.enabled = enabled
                source = older + previous + current
                body = {"model": "router", "metadata": self.metadata, "messages": copy.deepcopy(source)}
                await self.preparation.inlet(body, __request__=self.request, __user__={"id": "user"})
                record = unpack_record(body["messages"])
                if enabled:
                    self.assertEqual([e["call"]["id"] for e in record["tool_exchanges"]], [
                        "previous-handoff", "previous-lookup",
                    ])
                    self.assertEqual(record["tool_exchanges"][1]["executor"], {
                        "kind": "subagent", "agent_id": "agent-a", "model_id": "agent-a", "name": "Agent A",
                    })
                else:
                    self.assertIsNone(record)
                self.assertEqual(body["messages"][-len(current):], current)
                self.assertFalse(any(m.get("tool_calls") or m["role"] == "tool" for m in body["messages"][:-len(current)]))

                await self.invoke_body(body)
                self.assertEqual(self.routed["model"], "agent-a")
                self.assertEqual([m["content"] for m in self.routed["messages"] if m["role"] == "tool"], [
                    "previous result", "current result",
                ])
                self.assertEqual(await self.metadata["tools"]["lookup"]["callable"](), self.routed["messages"])

    async def test_child_continuations_reuse_evidence_and_capabilities_with_fresh_context(self):
        self.begin_request()
        body = {"model": "router", "metadata": self.metadata, "messages": [
            {"role": "user", "content": "Find the answer"},
            assistant(call("handoff", "lite_delegate", agent_id="agent-a")), result("handoff", marker()),
        ]}
        await self.preparation.inlet(body, __request__=self.request, __user__={"id": "user"})
        await self.invoke_body(body)
        tool = self.metadata["tools"]["lookup"]["callable"]
        body["messages"] += [assistant(call("lookup", "lookup")), result("lookup", "fresh result")]
        await self.invoke_body(body)

        self.assertEqual(self.routed["model"], "agent-a")
        self.assertEqual([m["content"] for m in self.routed["messages"] if m["role"] == "tool"], ["fresh result"])
        self.assertIs(self.metadata["tools"]["lookup"]["callable"], tool)
        self.assertEqual(await tool(), self.routed["messages"])
        self.loader.assert_awaited_once()
        self.assertEqual(self.dispatch_filters.await_count, 2)
        self.assertEqual(self.completion.await_count, 2)

    async def test_new_request_requires_preparation_and_resets_active_child_across_metadata(self):
        for platform_ids in (False, True):
            with self.subTest(platform_ids=platform_ids):
                self.begin_request()
                if platform_ids:
                    self.metadata.update(chat_id="chat", message_id="first")
                first = {"model": "router", "metadata": self.metadata, "messages": [
                    {"role": "user", "content": "Repeat"},
                    assistant(call("handoff", "lite_delegate", agent_id="agent-a")), result("handoff", marker()),
                ]}
                await self.preparation.inlet(first, __request__=self.request, __user__={"id": "user"})
                await self.invoke_body(first)
                shared = self.metadata["tools"]
                self.completion.reset_mock()
                if platform_ids:
                    self.metadata["message_id"] = "second"
                request_metadata = {**self.metadata, "platform": "preserved"}
                self.begin_request(metadata=request_metadata)
                second = {"model": "router", "metadata": self.metadata, "messages": [
                    {"role": "user", "content": "Repeat"},
                ]}
                with self.assertRaisesRegex(ValueError, "Router Preparation"):
                    await self.invoke_body(second)
                self.completion.assert_not_awaited()
                await self.preparation.inlet(second, __request__=self.request, __user__={"id": "user"})
                await self.invoke_body(second)
                self.assertEqual(self.routed["model"], "base-model")
                self.assertEqual([m["content"] for m in self.routed["messages"] if m["role"] == "user"], ["Repeat"])
                self.assertNotIn("lite_active_handoff", self.metadata)
                self.assertNotIn("lite_active_handoff", request_metadata)
                self.assertIs(self.metadata["tools"], shared)
                self.assertIs(request_metadata["tools"], shared)
                self.assertEqual(request_metadata["platform"], "preserved")
                self.metadata.pop("chat_id", None)
                self.metadata.pop("message_id", None)

    async def test_registry_validation_and_incomplete_history_preparation_never_dispatch(self):
        for failure in ("skill", "messages"):
            with self.subTest(failure=failure):
                self.begin_request()
                body = {"model": "router", "metadata": self.metadata, "messages": []}
                lookup = self.skills.side_effect
                if failure == "skill":
                    self.preparation.valves.base_skill_ids = ["missing"]
                    self.skills.side_effect = lambda skill_id: None if skill_id == "missing" else lookup(skill_id)
                    expected = "Skills are unavailable"
                else:
                    body["messages"] = "invalid history"
                    expected = "messages must be a list"
                with self.assertRaisesRegex((ValueError, TypeError), expected):
                    await self.preparation.inlet(body, __request__=self.request, __user__={"id": "user"})
                body["messages"] = []
                with self.assertRaisesRegex(ValueError, "Router Preparation"):
                    await self.invoke_body(body)
                self.completion.assert_not_awaited()
                self.skills.side_effect = lookup
                self.preparation.valves.base_skill_ids = []

    async def test_unavailable_agent_is_not_advertised_and_cannot_receive_handoff(self):
        async def access(user, runtime_model, *, model_info):
            if runtime_model["id"] == "agent-b":
                raise ValueError("Model not found")

        self.enterContext(patch.object(preparation_module, "check_model_access", AsyncMock(side_effect=access)))
        self.begin_request()
        body = {"model": "router", "metadata": self.metadata, "messages": [{"role": "user", "content": "Plan"}]}
        await self.preparation.inlet(body, __request__=self.request, __user__={"id": "user"})
        await self.invoke_body(body)
        prompt = "\n".join(m["content"] for m in self.routed["messages"] if m["role"] == "system")
        self.assertIn("Skill instructions for route-a", prompt)
        self.assertNotIn("Skill instructions for route-b", prompt)
        self.completion.reset_mock()
        body["messages"] += [
            assistant(call("handoff", "lite_delegate", agent_id="agent-b")), result("handoff", marker("agent-b")),
        ]
        with self.assertRaisesRegex(ValueError, 'Agent ID "agent-b" is not available'):
            await self.invoke_body(body)
        self.completion.assert_not_awaited()

    async def test_failed_child_preparation_restores_managed_state_and_tool_identities(self):
        self.begin_request()
        body = {"model": "router", "metadata": self.metadata, "messages": [
            {"role": "user", "content": "Find"},
            assistant(call("handoff", "lite_delegate", agent_id="agent-a")), result("handoff", marker()),
        ]}
        await self.preparation.inlet(body, __request__=self.request, __user__={"id": "user"})
        await self.invoke_body(body)
        shared, history = self.metadata["tools"], self.metadata["lite_child_messages"]
        tool = shared["lookup"]["callable"]
        original = copy.deepcopy(history)
        request_metadata = {**self.metadata, "platform": "preserved"}
        self.request.state.metadata = request_metadata
        self.completion.reset_mock()
        async def fail(body):
            raise RuntimeError("additional filter failed")

        self.filters = [types.SimpleNamespace(inlet=fail)]
        body["messages"] += [assistant(call("lookup", "lookup")), result("lookup", "unpublished")]
        with self.assertRaisesRegex(RuntimeError, "additional filter failed"):
            await self.invoke_body(body)
        self.completion.assert_not_awaited()
        self.assertIs(self.metadata["tools"], shared)
        self.assertIs(request_metadata["tools"], shared)
        self.assertIs(self.metadata["lite_child_messages"], history)
        self.assertIs(request_metadata["lite_child_messages"], history)
        self.assertEqual(await tool(), original)
        self.assertEqual(request_metadata["platform"], "preserved")
