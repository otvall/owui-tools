"""Automatic child preparation through Pipe.pipe without preparation attachments."""

import copy
import types
from unittest.mock import AsyncMock

from test_handoff_history import PipeTestCase, assistant, call, grouped_history, marker, result


class InlineSubagentPreparationTests(PipeTestCase):
    async def test_handoff_and_continuation_prepare_skills_without_attached_filters(self):
        self.models["agent-a"].meta["skillIds"] = ["specialist"]
        initial = {"model": "router", "metadata": self.metadata, "messages": grouped_history()[:4]}
        self.begin_request()
        await self.router_inlets(initial)
        self.assertIs(await self.invoke_body(initial), self.completion_result)
        self.assertEqual(self.filters, [])
        self.assertIn("Skill instructions for specialist", self.routed["messages"][0]["content"])
        self.assertFalse(any(m.get("tool_calls") or m["role"] == "tool" for m in self.routed["messages"]))
        lookup = self.metadata["tools"]["lookup"]["callable"]

        continued = copy.deepcopy(self.routed["messages"]) + [
            assistant(call("child", "lookup")), result("child", "CURRENT_RESULT"),
        ]
        await self.invoke(continued)
        self.assertEqual([m for m in self.routed["messages"] if m["role"] == "tool"],
                         [result("child", "CURRENT_RESULT")])
        self.assertEqual(await lookup(), self.routed["messages"])
        self.assertIs(self.metadata["tools"]["lookup"]["callable"], lookup)
        self.loader.assert_awaited_once()

    async def test_pipe_applies_common_history_limits_to_two_destination_models(self):
        previous = [
            {"role": "user", "content": "Previous question"},
            assistant(call("delegate-a", "lite_delegate")), result("delegate-a", marker()),
            assistant(call("reused", "lookup")), result("reused", "A result"),
            assistant(call("delegate-b", "lite_delegate")), result("delegate-b", marker("agent-b")),
            assistant(call("reused", "lookup")), result("reused", "B result"),
            {"role": "assistant", "content": "Previous answer"},
        ]
        for turns, tools in ((0, 0), (1, 1), (1, 0), (0, 1)):
            self.pipe.valves.history_turns = turns
            self.pipe.valves.history_tool_calls = tools
            for target, latest in (("agent-a", "A result"), ("agent-b", "B result")):
                with self.subTest(turns=turns, tools=tools, target=target):
                    messages = await self.route_history([
                        *previous, {"role": "user", "content": "Current question"},
                        assistant(call("delegate", "lite_delegate")), result("delegate", marker(target)),
                        assistant(call("reused", "lookup")), result("reused", "CURRENT_RESULT"),
                    ])
                    self.assertEqual([m["content"] for m in messages if m["role"] == "tool"],
                                     ([latest] if turns and tools else []) + ["CURRENT_RESULT"])
                    self.assertEqual([m["content"] for m in messages
                                      if m["role"] in ("user", "assistant") and not m.get("tool_calls")],
                                     (["Previous question", "Previous answer"] if turns else []) + ["Current question"])

    async def test_additional_filters_receive_prepared_context_and_publish_final_tool_history(self):
        self.models["agent-a"].meta["skillIds"] = ["specialist"]
        self.metadata.update(session_id="session", params={"function_calling": "native"})
        builtin = AsyncMock(return_value="Loaded")
        self.builtins.return_value = {"view_skill": {"spec": {"name": "view_skill"}, "callable": builtin}}

        async def first(body):
            self.assertIn("<available_skills>", body["messages"][0]["content"])
            self.assertIn("view_skill", body["metadata"]["tools"])
            self.assertEqual([m["content"] for m in body["messages"] if m["role"] == "tool"],
                             ["TOOL_RESULT_42"])
            return {**body, "messages": [{"role": "system", "content": "Additional instructions"}, *body["messages"]]}

        async def last(body):
            return {**body, "messages": [*body["messages"], {"role": "system", "content": "Final context"}]}

        self.filters = [types.SimpleNamespace(inlet=first), types.SimpleNamespace(inlet=last)]
        await self.invoke(grouped_history())
        final = self.routed["messages"]
        self.assertEqual(final[0]["content"], "Additional instructions")
        self.assertEqual(final[-1]["content"], "Final context")
        self.assertEqual(await self.metadata["tools"]["lookup"]["callable"](), final)
        self.assertEqual(await self.metadata["tools"]["view_skill"]["callable"](id="not-attached"), "Loaded")
        builtin.assert_awaited_once_with(id="not-attached")
        self.dispatch_filters.assert_awaited_once()

    async def test_additional_filter_can_replace_view_skill_without_final_revalidation(self):
        self.models["agent-a"].meta["skillIds"] = ["specialist"]
        self.metadata.update(session_id="session", params={"function_calling": "native"})
        self.builtins.return_value = {
            "view_skill": {"spec": {"name": "view_skill"}, "callable": AsyncMock(return_value="Builtin")},
        }
        replacement = {"spec": {"name": "view_skill", "description": "Additional filter"},
                       "callable": AsyncMock(return_value="Replacement")}

        async def replace(body):
            body["metadata"]["tools"]["view_skill"] = replacement
            body["tools"] = [schema for schema in body["tools"] if schema["function"]["name"] != "view_skill"]
            body["tools"].append({"type": "function", "function": replacement["spec"]})
            return body

        self.filters = [types.SimpleNamespace(inlet=replace)]
        await self.invoke(grouped_history()[:4])
        self.completion.assert_awaited_once()
        self.assertIs(self.metadata["tools"]["view_skill"], replacement)
        self.assertEqual(await replacement["callable"](id="specialist"), "Replacement")
