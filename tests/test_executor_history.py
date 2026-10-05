"""Executor-specific child history through the public Pipe and Filter seams."""

import copy
import json
import unittest
from types import SimpleNamespace

from test_handoff_history import (
    PipeTestCase, assistant, call, context_filter_module, grouped_history, marker,
    result, tool_filter_module,
)
from test_previous_turn_context import unpack_record


class ExecutorHistoryTests(PipeTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.context_filter.valves.history_turns = 2
        self.context_filter.valves.history_tool_calls = 5

    async def test_reference_record_keeps_unknown_work_while_child_history_excludes_it(self):
        source = [
            {"role": "user", "content": "Old question"},
            assistant(call("to-a", "lite_delegate")), result("to-a", marker()),
            assistant(call("known", "lookup")), result("known", "KNOWN_A_RESULT"),
            assistant(call("unfinished", "lite_delegate"), call("unknown", "lookup")),
            result("unknown", "UNKNOWN_EXECUTOR_RESULT"),
            {"role": "assistant", "content": "Old answer"},
            {"role": "user", "content": "Current question"},
            assistant(call("delegate", "lite_delegate")), result("delegate", marker()),
        ]
        self.begin_request()
        body = {"model": "router", "metadata": self.metadata, "messages": source}
        await self.router_inlets(body)
        record = unpack_record(body["messages"])

        await self.invoke_body(body)

        self.assertEqual(record["tool_exchanges"][-1]["result"], result("unknown", "UNKNOWN_EXECUTOR_RESULT"))
        self.assertEqual(record["tool_exchanges"][-1]["executor"], {"kind": "unknown"})
        self.assertEqual([m for m in self.routed["messages"] if m["role"] == "tool"],
                         [result("known", "KNOWN_A_RESULT")])

    async def test_selected_agent_gets_only_its_own_permitted_history_and_retained_text(self):
        history = [
            {"role": "user", "content": "old question"},
            assistant(call("orchestrator", "lookup")), result("orchestrator", "ORCHESTRATOR_RESULT"),
            assistant(call("old-delegate", "lite_delegate")), result("old-delegate", marker()),
            assistant(call("child", "lookup")), result("child", "A_RESULT"),
            assistant(call("blocked", "private_tool")), result("blocked", "A_PRIVATE_RESULT"),
            {"role": "assistant", "content": "old answer"},
        ]
        for target, expected in (("agent-b", []), ("agent-a", [result("child", "A_RESULT")])):
            with self.subTest(target=target):
                messages = await self.route_history([
                    *history, {"role": "user", "content": "current question"},
                    assistant(call("current-delegate", "lite_delegate")), result("current-delegate", marker(target)),
                ])

                self.assertEqual([m for m in messages if m["role"] == "tool"], expected)
                self.assertEqual([c for m in messages for c in m.get("tool_calls", [])],
                                 [call("child", "lookup")] if expected else [])
                self.assertEqual([m["content"] for m in messages if m["role"] in ("user", "assistant") and not m.get("tool_calls")],
                                 ["old question", "old answer", "current question"])

    async def test_direct_ids_and_routing_skill_aliases_select_the_same_history(self):
        for historical_id in ("agent-a", "route-a"):
            for selected_id in ("agent-a", "route-a"):
                with self.subTest(historical_id=historical_id, selected_id=selected_id):
                    messages = await self.route_history([
                        {"role": "user", "content": "old question"},
                        assistant(call("old-delegate", "lite_delegate")), result("old-delegate", marker(historical_id)),
                        assistant(call("old-child", "lookup")), result("old-child", "OLD_CHILD_RESULT"),
                        {"role": "assistant", "content": "old answer"},
                        {"role": "user", "content": "current question"},
                        assistant(call("private", "lookup")), result("private", "PRIVATE_RESULT"),
                        assistant(call("delegate", "lite_delegate")), result("delegate", marker(selected_id)),
                        assistant(call("child", "lookup")), result("child", "CHILD_RESULT"),
                    ])

                    self.assertEqual(self.routed["model"], "agent-a")
                    self.assertEqual([m for m in messages if m["role"] == "tool"],
                                     [result("old-child", "OLD_CHILD_RESULT"), result("child", "CHILD_RESULT")])

    async def test_grouped_calls_follow_delegate_result_order_and_latest_eligible_limit(self):
        history = [
            {"role": "user", "content": "old question"},
            assistant(call("a-late", "lookup"), call("to-b", "lite_delegate"),
                      call("orchestrator", "lookup"), call("to-a", "lite_delegate"),
                      call("b", "lookup"), call("a-early", "lookup")),
            result("orchestrator", "ORCHESTRATOR_RESULT"), result("to-a", marker("route-a")),
            result("a-early", "A_EARLY_RESULT"), result("a-late", "A_LATE_RESULT"),
            result("to-b", marker("agent-b")), result("b", "B_RESULT"),
            {"role": "assistant", "content": "old answer"},
        ]
        for target, limit, expected_ids in (("agent-a", 5, ["a-early", "a-late"]),
                                            ("agent-b", 5, ["b"]),
                                            ("agent-a", 1, ["a-late"])):
            with self.subTest(target=target, limit=limit):
                self.context_filter.valves.history_tool_calls = limit
                messages = await self.route_history([
                    *history, {"role": "user", "content": "current question"},
                    assistant(call("delegate", "lite_delegate")), result("delegate", marker(target)),
                ])

                self.assertEqual([m["tool_call_id"] for m in messages if m["role"] == "tool"], expected_ids)
                self.assertCountEqual([c["id"] for m in messages for c in m.get("tool_calls", [])], expected_ids)

    async def test_uncertain_delegate_exchanges_cannot_carry_forward_an_executor(self):
        uncertain_segments = (
            [assistant(call("uncertain", "lite_delegate"), call("work", "lookup")), result("work", "UNCERTAIN_RESULT")],
            [assistant(call("uncertain", "lite_delegate"), call("uncertain", "lite_delegate"), call("work", "lookup")),
             result("uncertain", marker()), result("work", "UNCERTAIN_RESULT")],
            [assistant(call("uncertain", "lite_delegate")), result("uncertain", marker()), result("uncertain", marker("agent-b")),
             assistant(call("work", "lookup")), result("work", "UNCERTAIN_RESULT")],
            [assistant(call("uncertain", "lite_delegate")),
             assistant(call("uncertain", "lookup")), result("uncertain", marker()),
             assistant(call("work", "lookup")), result("work", "UNCERTAIN_RESULT")],
        )
        for segment in uncertain_segments:
            with self.subTest(segment=segment):
                messages = await self.route_history([
                    {"role": "user", "content": "old question"},
                    assistant(call("to-a", "lite_delegate")), result("to-a", marker()),
                    *segment, {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "current question"},
                    assistant(call("delegate", "lite_delegate")), result("delegate", marker()),
                ])

                self.assertEqual([m for m in messages if m["role"] == "tool"], [])

    async def test_only_supported_actual_handoffs_to_available_agents_establish_history(self):
        for name, receipt in (
            ("lookup", marker()),
            ("lite_delegate", json.dumps({"__lite_delegate__": "v1", "skill_id": "agent-a"})),
            ("lite_delegate", json.dumps({"__lite_delegate__": "v2", "skill_id": "agent-a"})),
            ("lite_delegate", marker("removed-agent")),
        ):
            with self.subTest(name=name, receipt=receipt):
                messages = await self.route_history([
                    {"role": "user", "content": "old question"},
                    assistant(call("fake-delegate", name)), result("fake-delegate", receipt),
                    assistant(call("child", "lookup")), result("child", "UNATTRIBUTED_RESULT"),
                    {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "current question"},
                    assistant(call("delegate", "lite_delegate")), result("delegate", marker()),
                ])

                self.assertEqual([m for m in messages if m["role"] == "tool"], [])

    async def test_reused_ids_cannot_transfer_provenance_between_requests_or_batches(self):
        messages = await self.route_history([
            {"role": "user", "content": "A question"},
            assistant(call("shared-delegate", "lite_delegate")), result("shared-delegate", marker()),
            assistant(call("shared-work", "lookup")), result("shared-work", "A_RESULT"),
            {"role": "assistant", "content": "A answer"},
            {"role": "user", "content": "other question"},
            assistant(call("shared-delegate", "lite_delegate")),
            assistant(call("shared-delegate", "lookup")), result("shared-delegate", marker()),
            assistant(call("shared-work", "lookup")), result("shared-work", "OTHER_RESULT"),
            {"role": "assistant", "content": "other answer"},
            {"role": "user", "content": "current question"},
            assistant(call("delegate", "lite_delegate")), result("delegate", marker()),
        ])

        self.assertEqual([m for m in messages if m["role"] == "tool"], [result("shared-work", "A_RESULT")])

    async def test_grouped_fixture_and_current_chain_survive_images_repeated_results_and_continuations(self):
        image = {"role": "user", "content": [
            {"type": "text", "text": tool_filter_module.TOOL_IMAGE_TEXT},
            {"type": "image_url", "image_url": {"url": "https://example.test/tool.png"}},
        ]}
        source = [
            *grouped_history(), image, result("lookup", "TOOL_RESULT_42"),
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "current question"},
            assistant(call("current-delegate", "lite_delegate")), image,
            result("current-delegate", marker("route-a")),
            assistant(call("first", "lookup")), image, result("first", "FIRST_RESULT"), result("first", "FIRST_RESULT"),
        ]
        await self.route_history(source)
        shared_tools = self.metadata["tools"]
        shared_history = self.metadata["lite_child_messages"]
        # Continue from the already stripped child request, without a delegate.
        continuation = [*copy.deepcopy(self.routed["messages"]),
                        assistant(call("second", "lookup")), result("second", "SECOND_RESULT")]
        await self.invoke(continuation)

        self.assertEqual([m for m in self.routed["messages"] if m["role"] == "tool"], [
            result("lookup", "TOOL_RESULT_42"), result("first", "FIRST_RESULT"), result("second", "SECOND_RESULT"),
        ])
        self.assertEqual([m["content"] for m in self.routed["messages"] if m["role"] == "user" and isinstance(m["content"], str)],
                         ["Find the answer", "current question"])
        self.assertIn(image, self.routed["messages"])
        self.assertIs(self.metadata["tools"], shared_tools)
        self.assertIs(self.metadata["lite_child_messages"], shared_history)
        self.assertEqual(await shared_tools["lookup"]["callable"](), self.routed["messages"])

    async def test_standalone_filter_resolves_history_before_stripping_delegate_evidence(self):
        body = {"metadata": {
            "tools": {"lookup": {}}, "lite_target_agent_id": "route-a", "lite_agents": self.metadata["lite_agents"],
        }, "messages": [
            *grouped_history(), {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "current question"},
            assistant(call("delegate", "lite_delegate")), result("delegate", marker()),
        ]}

        await tool_filter_module.Filter().inlet(body)

        self.assertEqual([m for m in body["messages"] if m["role"] == "tool"], [result("lookup", "TOOL_RESULT_42")])
        self.assertEqual([c for m in body["messages"] for c in m.get("tool_calls", [])], [call("lookup", "lookup")])

    async def test_ambiguous_historical_routing_alias_cannot_identify_the_selected_agent(self):
        registry = copy.deepcopy(self.metadata["lite_agents"])
        registry["agent-b"]["routing_skill_id"] = "route-a"
        body = {"metadata": {"tools": {"lookup": {}}, "lite_target_agent_id": "agent-a", "lite_agents": registry},
                "messages": [
                    {"role": "user", "content": "old question"},
                    assistant(call("ambiguous", "lite_delegate")), result("ambiguous", marker("route-a")),
                    assistant(call("work", "lookup")), result("work", "UNKNOWN_RESULT"),
                    {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "current question"},
                ]}

        await tool_filter_module.Filter().inlet(body)

        self.assertEqual(body["messages"], [
            {"role": "user", "content": "old question"}, {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "current question"},
        ])

    async def test_executor_history_limits_cannot_expand_retained_text_turns(self):
        source = [
            {"role": "user", "content": "A question"},
            assistant(call("to-a", "lite_delegate")), result("to-a", marker()),
            assistant(call("a", "lookup")), result("a", "A_RESULT"),
            {"role": "assistant", "content": "A answer"},
            {"role": "user", "content": "B question"},
            assistant(call("to-b", "lite_delegate")), result("to-b", marker("agent-b")),
            assistant(call("b", "lookup")), result("b", "B_RESULT"),
            {"role": "assistant", "content": "B answer"},
            {"role": "user", "content": "current question"},
            assistant(call("delegate", "lite_delegate")), result("delegate", marker()),
            assistant(call("current", "lookup")), result("current", "CURRENT_RESULT"),
        ]
        for turns, calls, expected in ((1, 5, ["CURRENT_RESULT"]), (2, 1, ["A_RESULT", "CURRENT_RESULT"]),
                                       (0, 5, ["CURRENT_RESULT"]), (2, 0, ["CURRENT_RESULT"])):
            with self.subTest(turns=turns, calls=calls):
                self.context_filter.valves.history_turns = turns
                self.context_filter.valves.history_tool_calls = calls
                messages = await self.route_history(source)

                self.assertEqual([m["content"] for m in messages if m["role"] == "tool"], expected)
                self.assertEqual([m["content"] for m in messages if m["role"] == "user"],
                                 ["A question", "B question", "current question"] if turns == 2 else
                                 ["B question", "current question"] if turns == 1 else ["current question"])

    async def test_filtered_tool_narration_does_not_replace_a_completed_text_turn(self):
        self.context_filter.valves.history_turns = 1
        narration = assistant(call("unfinished-lookup", "lookup"))
        narration["content"] = "I will look this up"
        messages = await self.route_history([
            {"role": "user", "content": "completed question"},
            assistant(call("old-delegate", "lite_delegate")), result("old-delegate", marker()),
            assistant(call("old-lookup", "lookup")), result("old-lookup", "COMPLETED_CHILD_RESULT"),
            {"role": "assistant", "content": "completed answer"},
            {"role": "user", "content": "unfinished question"},
            narration, result("unfinished-lookup", "PENDING_ORCHESTRATOR_RESULT"),
            {"role": "user", "content": "current question"},
            assistant(call("delegate", "lite_delegate")), result("delegate", marker()),
        ])

        self.assertEqual([m["content"] for m in messages if m["role"] in ("user", "assistant") and not m.get("tool_calls")],
                         ["completed question", "completed answer", "current question"])
        self.assertEqual([m for m in messages if m["role"] == "tool"], [result("old-lookup", "COMPLETED_CHILD_RESULT")])

    async def test_standalone_filters_preserve_final_answers_and_current_tool_narration(self):
        old_narration = assistant(call("private", "private_tool"))
        old_narration["content"] = "I will look this up"
        current_narration = assistant(call("current", "lookup"))
        current_narration["content"] = "Checking the current question"
        current = [
            {"role": "user", "content": "current question"},
            current_narration, result("current", "CURRENT_RESULT"),
        ]
        body = {"metadata": {"tools": {"lookup": {}}}, "messages": [
            {"role": "user", "content": "completed question"},
            {"role": "assistant", "content": "completed answer"},
            {"role": "user", "content": "unfinished question"},
            old_narration, result("private", "PRIVATE_RESULT"), *current,
        ]}
        # Standalone capability filtering must preserve the same text boundary.
        await tool_filter_module.Filter().inlet(body)
        context = context_filter_module.Filter()
        context.valves.history_turns = 1
        await context.inlet(body)

        self.assertEqual(body["messages"][:2], [
            {"role": "user", "content": "completed question"},
            {"role": "assistant", "content": "completed answer"},
        ])
        self.assertEqual(body["messages"][2:], current)

    async def test_failed_preparation_restores_history_and_tools_before_a_successful_retry(self):
        source = [*grouped_history(), {"role": "assistant", "content": "old answer"},
                  {"role": "user", "content": "current question"},
                  assistant(call("delegate", "lite_delegate")), result("delegate", marker())]
        await self.route_history(source)
        history = self.metadata["lite_child_messages"]
        saved_history = copy.deepcopy(history)
        tools = self.metadata["tools"]
        saved_tools = dict(tools)
        saved_metadata = dict(self.metadata)
        self.completion.reset_mock()

        async def fail(body):
            body["metadata"]["lite_child_messages"].clear()
            raise RuntimeError("preparation failed")

        # An additional installed filter may fail after the real history filters.
        self.filters.insert(2, SimpleNamespace(inlet=fail))
        with self.assertRaisesRegex(RuntimeError, "preparation failed"):
            await self.invoke(source)

        self.completion.assert_not_awaited()
        self.assertEqual(self.metadata, saved_metadata)
        self.assertIs(self.metadata["lite_child_messages"], history)
        self.assertEqual(history, saved_history)
        self.assertIs(self.metadata["tools"], tools)
        self.assertEqual(tools, saved_tools)
        self.filters.pop(2)
        await self.invoke(source)
        self.assertEqual(self.routed["messages"], saved_history)


if __name__ == "__main__":
    unittest.main()
