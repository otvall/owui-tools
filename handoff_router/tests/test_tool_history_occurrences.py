"""Occurrence-safe history contracts at public Function entry points."""

import copy
import unittest

from test_handoff_history import (
    PipeTestCase, assistant, call, context_filter_module, marker, result,
    tool_filter_module,
)


class ToolOccurrenceFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_pairs_cannot_borrow_results_from_other_requests_or_batches(self):
        question = {"role": "user", "content": "question"}
        next_question = {"role": "user", "content": "next question"}
        for source, expected in (
            ([assistant(call("shared", "lookup")), next_question, result("shared", "ORPHAN")], [next_question]),
            ([result("shared", "ORPHAN"), assistant(call("shared", "lookup"))], []),
            ([assistant(call("shared", "lookup")), assistant(call("other", "private_tool")), result("shared", "ORPHAN")], []),
            ([assistant(call("shared", "lookup")), {"role": "assistant", "content": "answer"}, result("shared", "ORPHAN")], [{"role": "assistant", "content": "answer"}]),
            ([assistant(call("shared", "private_tool")), assistant(call("shared", "lookup")), result("shared", "ALLOWED")], [assistant(call("shared", "lookup")), result("shared", "ALLOWED")]),
            ([assistant(call("shared", "private_tool"), call("shared", "lookup"), call("valid", "lookup")), result("shared", "PRIVATE"), result("shared", "ALLOWED"), result("valid", "VALID")], [assistant(call("valid", "lookup")), result("valid", "VALID")]),
            ([assistant(call("shared", "lookup")), result("shared", "FIRST"), result("shared", "CONFLICT")], []),
        ):
            with self.subTest(source=source):
                body = {"messages": [question, *source], "metadata": {"tools": {"lookup": {}}}}

                await tool_filter_module.Filter().inlet(body)

                self.assertEqual(body["messages"], [question, *expected])

    async def test_handoff_does_not_complete_pre_router_call_with_orphan_result(self):
        question = {"role": "user", "content": "question"}
        body = {"messages": [
            question, assistant(call("shared", "lookup")),
            assistant(call("delegate", "lite_delegate")), result("delegate", marker()),
            result("shared", "BORROWED_RESULT"),
        ], "metadata": {"tools": {"lookup": {}}, "lite_target_agent_id": "agent-a"}}

        await tool_filter_module.Filter().inlet(body)

        self.assertEqual(body["messages"], [question])

    async def test_tool_images_and_identical_repeated_results_preserve_one_pair(self):
        question = {"role": "user", "content": "question"}
        image = {"role": "user", "content": [
            {"type": "text", "text": tool_filter_module.TOOL_IMAGE_TEXT},
            {"type": "image_url", "image_url": {"url": "https://example.test/tool.png"}},
        ]}
        body = {"messages": [
            question, assistant(call("shared", "lookup")), image,
            result("shared", "RESULT"), result("shared", "RESULT"),
        ], "metadata": {"tools": {"lookup": {}}}}

        await tool_filter_module.Filter().inlet(body)
        await tool_filter_module.Filter().inlet(body)

        self.assertEqual(body["messages"], [question, assistant(call("shared", "lookup")), image, result("shared", "RESULT")])

    async def test_allowed_occurrence_does_not_authorize_private_reused_id(self):
        for same_request in (False, True):
            with self.subTest(same_request=same_request):
                question = {"role": "user", "content": "old question"}
                answer = {"role": "assistant", "content": "old answer"}
                next_question = {"role": "user", "content": "next question"}
                messages = [
                    question, assistant(call("shared", "private_tool")),
                    result("shared", "PRIVATE_RESULT"),
                    *([] if same_request else [answer, next_question]),
                    assistant(call("shared", "lookup")), result("shared", "ALLOWED_RESULT"),
                ]
                original = copy.deepcopy(messages)
                body = {"messages": messages, "metadata": {"tools": {"lookup": {}}}}

                await tool_filter_module.Filter().inlet(body)

                self.assertEqual(body["messages"], [
                    question, *([] if same_request else [answer, next_question]),
                    assistant(call("shared", "lookup")), result("shared", "ALLOWED_RESULT"),
                ])
                self.assertEqual(messages, original)


class HistoricalOccurrenceLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_and_orphan_history_does_not_consume_limit(self):
        current = {"role": "user", "content": "current question"}
        question = {"role": "user", "content": "question"}
        answer = {"role": "assistant", "content": "answer"}
        body = {"messages": [
            question, assistant(call("shared", "lookup")), result("shared", "VALID_RESULT"),
            assistant(call("shared", "lookup"), call("shared", "lookup")),
            result("shared", "AMBIGUOUS_RESULT"),
            result("orphan", "ORPHAN_RESULT"), answer, current,
        ]}
        instance = context_filter_module.Filter()
        instance.valves.history_turns = 1
        instance.valves.history_tool_calls = 1

        await instance.inlet(body)

        self.assertEqual(body["messages"], [question, assistant(call("shared", "lookup")), result("shared", "VALID_RESULT"), answer, current])

    async def test_limit_selects_latest_complete_occurrence_within_retained_turns(self):
        old_question = {"role": "user", "content": "old question"}
        old_answer = {"role": "assistant", "content": "old answer"}
        recent_question = {"role": "user", "content": "recent question"}
        recent_answer = {"role": "assistant", "content": "recent answer"}
        current = [
            {"role": "user", "content": "current question"},
            assistant(call("shared", "lookup")), result("shared", "CURRENT_RESULT"),
        ]
        for turns, limit, expected in (
            (2, 1, [old_question, old_answer, recent_question, assistant(call("shared", "lookup")), result("shared", "RECENT_RESULT"), recent_answer]),
            (2, 0, [old_question, old_answer, recent_question, recent_answer]),
            (1, 5, [recent_question, assistant(call("shared", "lookup")), result("shared", "RECENT_RESULT"), recent_answer]),
            (0, 5, []),
        ):
            with self.subTest(turns=turns, limit=limit):
                body = {"messages": [
                    old_question, assistant(call("shared", "lookup")), result("shared", "OLD_RESULT"), old_answer,
                    recent_question, assistant(call("shared", "lookup")), result("shared", "RECENT_RESULT"),
                    assistant(call("unfinished", "lookup")), recent_answer, *current,
                ]}
                instance = context_filter_module.Filter()
                instance.valves.history_turns = turns
                instance.valves.history_tool_calls = limit

                await instance.inlet(body)

                self.assertEqual(body["messages"], [*expected, *current])


class PipeOccurrenceHistoryTests(PipeTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.pipe.valves.history_turns = 2
        self.pipe.valves.history_tool_calls = 5

    async def test_grouped_current_calls_keep_source_order_when_results_arrive_in_another_order(self):
        messages = await self.route_history([
            {"role": "user", "content": "Question"},
            assistant(call("second", "lookup"), call("delegate", "lite_delegate"), call("first", "lookup")),
            result("delegate", marker()), result("first", "FIRST"), result("second", "SECOND"),
        ])

        self.assertEqual([c["id"] for m in messages for c in m.get("tool_calls", [])], ["second", "first"])
        self.assertEqual([m["tool_call_id"] for m in messages if m["role"] == "tool"], ["first", "second"])

    async def test_private_reused_id_never_reaches_child_completion(self):
        messages = await self.route_history([
            {"role": "user", "content": "private question"},
            assistant(call("shared", "private_tool")), result("shared", "PRIVATE_RESULT"),
            {"role": "assistant", "content": "private answer"},
            {"role": "user", "content": "allowed question"},
            assistant(call("allowed-delegate", "lite_delegate")), result("allowed-delegate", marker()),
            assistant(call("shared", "lookup")), result("shared", "ALLOWED_RESULT"),
            {"role": "assistant", "content": "allowed answer"},
            {"role": "user", "content": "current question"},
            assistant(call("delegate", "lite_delegate")), result("delegate", marker()),
        ])

        self.assertEqual([c for m in messages for c in m.get("tool_calls", [])], [call("shared", "lookup")])
        self.assertEqual([m for m in messages if m["role"] == "tool"], [result("shared", "ALLOWED_RESULT")])
        self.assertEqual([m["content"] for m in messages if m["role"] != "system" and "tool_calls" not in m and m["role"] != "tool"], [
            "private question", "private answer", "allowed question", "allowed answer", "current question",
        ])

    async def test_limit_one_keeps_latest_history_and_all_current_continuations(self):
        self.pipe.valves.history_tool_calls = 1
        source = [
            {"role": "user", "content": "old question"},
            assistant(call("old-delegate", "lite_delegate")), result("old-delegate", marker()),
            assistant(call("shared", "lookup")), result("shared", "OLD_RESULT"),
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "recent question"},
            assistant(call("recent-delegate", "lite_delegate")), result("recent-delegate", marker()),
            assistant(call("shared", "lookup")), result("shared", "RECENT_RESULT"),
            {"role": "assistant", "content": "recent answer"},
            {"role": "user", "content": "current question"},
            assistant(call("delegate", "lite_delegate")), result("delegate", marker()),
            assistant(call("shared", "lookup")), result("shared", "CURRENT_RESULT"),
        ]
        messages = await self.route_history(source)
        self.assertEqual([m["content"] for m in messages if m["role"] == "tool"], ["RECENT_RESULT", "CURRENT_RESULT"])
        self.assertEqual([c for m in messages for c in m.get("tool_calls", [])], [call("shared", "lookup"), call("shared", "lookup")])

        source += [assistant(call("shared", "lookup")), result("shared", "CONTINUATION_RESULT")]
        await self.invoke(source)
        messages = self.routed["messages"]
        self.assertEqual([m["content"] for m in messages if m["role"] == "tool"], ["RECENT_RESULT", "CURRENT_RESULT", "CONTINUATION_RESULT"])
        self.assertEqual([c for m in messages for c in m.get("tool_calls", [])], [call("shared", "lookup"), call("shared", "lookup"), call("shared", "lookup")])

    async def test_marker_result_from_reused_non_delegate_call_keeps_orchestrator(self):
        await self.route_history([
            {"role": "user", "content": "question"},
            assistant(call("shared", "lite_delegate")),
            assistant(call("shared", "lookup")), result("shared", marker()),
        ])

        self.assertEqual(self.routed["model"], "base-model")
        self.assertNotIn("lite_active_agent_id", self.metadata)

    async def test_malformed_history_and_mixed_calls_leave_only_eligible_pairs(self):
        messages = await self.route_history([
            {"role": "user", "content": "first question"},
            assistant(call("shared", "lookup")),
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second question"},
            result("shared", "OTHER_REQUEST_RESULT"),
            assistant(call("shared", "lookup")),
            assistant(call("private", "private_tool")), result("shared", "OTHER_BATCH_RESULT"),
            assistant(call("same-batch", "private_tool"), call("same-batch", "lookup")),
            result("same-batch", "AMBIGUOUS_RESULT"),
            assistant(call("valid-delegate", "lite_delegate")), result("valid-delegate", marker()),
            assistant(call("valid", "lookup"), call("blocked", "private_tool")),
            result("blocked", "PRIVATE_RESULT"), result("valid", "VALID_RESULT"),
            {"role": "assistant", "content": "second answer"},
            {"role": "user", "content": "current question"},
            assistant(call("delegate", "lite_delegate")), result("delegate", marker()),
        ])

        self.assertEqual([c for m in messages for c in m.get("tool_calls", [])], [call("valid", "lookup")])
        self.assertEqual([m for m in messages if m["role"] == "tool"], [result("valid", "VALID_RESULT")])

    async def test_zero_history_limits_preserve_grouped_current_pairs_and_images(self):
        from test_handoff_history import grouped_history

        image = {"role": "user", "content": [
            {"type": "text", "text": tool_filter_module.TOOL_IMAGE_TEXT},
            {"type": "image_url", "image_url": {"url": "https://example.test/tool.png"}},
        ]}
        for turns, limit in ((0, 5), (2, 0)):
            with self.subTest(turns=turns, limit=limit):
                self.pipe.valves.history_turns = turns
                self.pipe.valves.history_tool_calls = limit
                source = [
                    {"role": "user", "content": "old question"},
                    assistant(call("lookup", "lookup")), result("lookup", "OLD_RESULT"),
                    {"role": "assistant", "content": "old answer"},
                    *grouped_history(),
                ]
                source.insert(-1, image)
                source.append(result("lookup", "TOOL_RESULT_42"))
                messages = await self.route_history(source)

                self.assertEqual([c for m in messages for c in m.get("tool_calls", [])], [call("lookup", "lookup")])
                self.assertEqual([m for m in messages if m["role"] == "tool"], [result("lookup", "TOOL_RESULT_42")])
                self.assertIn(image, messages)
                self.assertEqual([m["content"] for m in messages if m["role"] == "user" and isinstance(m["content"], str)], ["Find the answer"] if turns == 0 else ["old question", "Find the answer"])

                await self.invoke(source)
                self.assertEqual(self.routed["messages"], messages)
