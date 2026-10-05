"""Both preparation Functions together through the public inlets and Pipe."""

from unittest.mock import AsyncMock

import test_router_preparation as router_preparation

from test_handoff_history import assistant, call, marker, result
from test_previous_turn_context import unpack_record
from test_subagent_preparation import preparation_module


class CombinedPreparationTests(router_preparation.RouterPreparationTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.child_preparation = preparation_module.Filter()
        self.context_filter = self.child_preparation
        self.filters = [self.child_preparation]

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
        self.assertIn("error", await loader(id="route-a"))
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
