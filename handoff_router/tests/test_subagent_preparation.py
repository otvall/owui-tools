"""Automatic child preparation through Router Preparation and public Pipe.pipe."""

import copy
import types
from unittest.mock import AsyncMock

import test_capability_context as capability_context
import test_executor_history as executor_history
import test_handoff_history as handoff_history
import test_router_chain as router_chain
import test_skill_preparation as skill_preparation
import test_tool_history_occurrences as tool_occurrences
import test_router_preparation as router_preparation

from test_handoff_history import (
    PipeTestCase, assistant, call, grouped_history, marker, result,
)

class PreparationOnly:
    async def router_inlets(self, body, *, registry=None):
        return await router_preparation.RouterPreparationTests.router_inlets(self, body, registry=registry)


class SubagentPreparationTests(PreparationOnly, PipeTestCase):
    async def test_pipe_history_limits_reject_negative_values(self):
        from pydantic import ValidationError

        for field in ("history_turns", "history_tool_calls"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.pipe.Valves(**{field: -1})

    async def test_no_attachments_complete_handoff_and_trimmed_continuation_with_skills(self):
        self.models["agent-a"].meta["skillIds"] = ["specialist"]
        initial = {"model": "router", "metadata": self.metadata, "messages": grouped_history()[:4]}
        self.begin_request()
        await self.router_inlets(initial)
        self.assertIs(await self.invoke_body(initial), self.completion_result)
        self.assertEqual(self.routed["model"], "agent-a")
        self.assertIn("Skill instructions for specialist", self.routed["messages"][0]["content"])
        self.assertEqual([m for m in self.routed["messages"] if m["role"] == "user"], [initial["messages"][0]])
        self.assertFalse(any(m.get("tool_calls") or m["role"] == "tool" for m in self.routed["messages"]))
        lookup = self.metadata["tools"]["lookup"]["callable"]

        continued = copy.deepcopy(self.routed["messages"]) + [
            assistant(call("child", "lookup")), result("child", "CURRENT_RESULT"),
        ]
        self.assertIs(await self.invoke(continued), self.completion_result)
        self.assertEqual([m for m in self.routed["messages"] if m["role"] == "tool"],
                         [result("child", "CURRENT_RESULT")])
        self.assertEqual(await lookup(), self.routed["messages"])
        self.assertIs(self.metadata["tools"]["lookup"]["callable"], lookup)
        self.loader.assert_awaited_once()

    async def test_same_function_applies_common_limits_to_two_destination_models(self):
        self.assertEqual(self.pipe.valves.model_dump(), {
            "orchestrator_model_id": "base-model", "emit_handoff_status": True,
            "history_turns": 0, "history_tool_calls": 0, "debug": False,
        })
        # Deliberately contrary runtime settings cannot override Function Valves.
        for model_id in ("agent-a", "agent-b"):
            self.request.app.state.MODELS[model_id]["info"] = {
                "meta": {"history_turns": 99, "history_tool_calls": 99},
            }
        old_history = [
            {"role": "user", "content": "Discarded question"},
            assistant(call("older-delegate", "lite_delegate")), result("older-delegate", marker()),
            assistant(call("reused", "lookup")), result("reused", "DISCARDED_RESULT"),
            {"role": "assistant", "content": "Discarded answer"},
            {"role": "user", "content": "Retained question"},
            assistant(call("a-delegate", "lite_delegate")), result("a-delegate", marker()),
            assistant(call("a-early", "lookup")), result("a-early", "A_EARLY"),
            assistant(call("reused", "lookup")), result("reused", "A_LATEST"),
            assistant(call("b-delegate", "lite_delegate")), result("b-delegate", marker("agent-b")),
            assistant(call("reused", "lookup")), result("reused", "B_LATEST"),
            {"role": "assistant", "content": "Retained answer"},
        ]
        for turns, tools in ((0, 0), (1, 1), (1, 0), (0, 1)):
            self.pipe.valves.history_turns = turns
            self.pipe.valves.history_tool_calls = tools
            for target, latest in (("agent-a", "A_LATEST"), ("agent-b", "B_LATEST")):
                with self.subTest(turns=turns, tools=tools, target=target):
                    messages = await self.route_history([
                        *old_history, {"role": "user", "content": "Current question"},
                        assistant(call("delegate", "lite_delegate")), result("delegate", marker(target)),
                        assistant(call("reused", "lookup")), result("reused", "CURRENT_RESULT"),
                    ])
                    self.assertEqual(self.routed["model"], target)
                    self.assertEqual([m["content"] for m in messages if m["role"] == "tool"],
                                     ([latest] if turns and tools else []) + ["CURRENT_RESULT"])
                    self.assertEqual([m["content"] for m in messages
                                      if m["role"] in ("user", "assistant") and not m.get("tool_calls")],
                                     (["Retained question", "Retained answer"] if turns else []) + ["Current question"])

    async def test_tools_see_skills_and_additional_filters_after_automatic_preparation(self):
        self.models["agent-a"].meta["skillIds"] = ["specialist"]
        self.metadata.update(session_id="session", params={"function_calling": "native"})
        self.builtins.return_value = {
            "view_skill": {"spec": {"name": "view_skill"}, "callable": AsyncMock(return_value="Loaded")},
        }

        async def first(body):
            self.assertIn("<available_skills>", body["messages"][0]["content"])
            self.assertEqual([m for m in body["messages"] if m["role"] == "tool"],
                             [result("lookup", "TOOL_RESULT_42")])
            return {**body, "messages": [{"role": "system", "content": "Additional instructions"}, *body["messages"]]}

        async def after(body):
            return {**body, "messages": [*body["messages"], {"role": "system", "content": "Final context"}]}

        self.filters = [types.SimpleNamespace(inlet=first), types.SimpleNamespace(inlet=after)]
        await self.invoke(grouped_history())
        final = self.routed["messages"]
        self.assertIn("Additional instructions", final[0]["content"])
        self.assertIn("<available_skills>", final[1]["content"])
        self.assertEqual(final[-1], {"role": "system", "content": "Final context"})
        self.assertEqual(await self.metadata["tools"]["lookup"]["callable"](), final)
        self.assertEqual(await self.metadata["tools"]["view_skill"]["callable"](id="specialist"), "Loaded")
        self.assertIn("error", await self.metadata["tools"]["view_skill"]["callable"](id="route-a"))


# Reuse behavior contracts with Router Preparation and no child attachments.
# OWUI/provider adapters stay external; project preparation runs unmodified.
class PreparationExecutorHistoryTests(PreparationOnly, executor_history.ExecutorHistoryTests):
    pass


class PreparationOccurrenceTests(PreparationOnly, tool_occurrences.PipeOccurrenceHistoryTests):
    pass


class PreparationSkillTests(PreparationOnly, skill_preparation.ChildSkillTests):
    pass


class PreparationCapabilityContextTests(PreparationOnly, capability_context.CapabilityContextTests):
    pass


class PreparationContinuationTests(PreparationOnly, handoff_history.ChildContinuationTests):
    pass


class PreparationCapabilityReuseTests(PreparationOnly, handoff_history.CapabilityReuseTests):
    pass


class PreparationRouterChainTests(PreparationOnly, router_chain.RouterChainTests):
    pass
