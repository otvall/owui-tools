"""Read-only projection contracts at the shared module's interface."""

import copy
import unittest

from shared.tool_context import ToolContextProjection


def tool_call(call_id, name):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


class ToolContextProjectionTests(unittest.TestCase):
    def test_available_tools_reads_context_without_mutation_or_cross_call_state(self):
        lookup = tool_call("lookup", "lookup")
        private = tool_call("private", "private")
        question = {"role": "user", "content": "Current question"}
        lookup_result = {"role": "tool", "tool_call_id": "lookup", "content": "LOOKUP"}
        private_result = {"role": "tool", "tool_call_id": "private", "content": "PRIVATE"}
        body = {
            "messages": [question, {"role": "assistant", "tool_calls": [lookup, private]}, lookup_result, private_result],
            "tools": [{"function": {"name": " schema_tool "}}],
            "metadata": {
                "tools": {" lookup ": {}},
                "lite_view_skill_available": True,
                "lite_view_skill_model_id": "child",
                "lite_target_model_id": "child",
            },
        }
        original = copy.deepcopy(body)

        first = ToolContextProjection.available_tools(body)

        self.assertEqual(body, original)
        self.assertEqual(first.allowed_tool_names, {"lookup", "schema_tool", "view_skill"})
        self.assertEqual(first.messages, [question, {"role": "assistant", "tool_calls": [lookup]}, lookup_result])
        body["metadata"]["tools"] = {"private": {}}
        body["metadata"]["lite_view_skill_model_id"] = "other-model"
        changed = copy.deepcopy(body)

        second = ToolContextProjection.available_tools(body)

        self.assertEqual(body, changed)
        self.assertEqual(second.allowed_tool_names, {"private", "schema_tool"})
        self.assertEqual(second.messages, [question, {"role": "assistant", "tool_calls": [private]}, private_result])
        self.assertEqual(first.messages, [question, {"role": "assistant", "tool_calls": [lookup]}, lookup_result])

    def test_history_limits_are_independent_and_copy_the_current_continuation(self):
        system = {"role": "system", "content": "System"}
        question = {"role": "user", "content": "Old question"}
        lookup = tool_call("old", "lookup")
        call_message = {"role": "assistant", "content": "Looking up", "tool_calls": [lookup]}
        result = {"role": "tool", "tool_call_id": "old", "content": "OLD_RESULT"}
        answer = {"role": "assistant", "content": "Old answer", "reasoning_content": "Old reasoning"}
        current = [
            {"role": "user", "content": "Current question"},
            {"role": "assistant", "tool_calls": [tool_call("current", "lookup")]},
            {"role": "tool", "tool_call_id": "current", "content": {"items": ["CURRENT_RESULT"]}},
        ]
        messages = [system, question, call_message, result, answer, *current]
        original = copy.deepcopy(messages)

        retained = ToolContextProjection.completed_history(messages, history_turns=1, history_tool_calls=1)

        self.assertEqual(messages, original)
        self.assertEqual(retained, [
            system, question, {"role": "assistant", "content": "", "tool_calls": [lookup]}, result,
            {"role": "assistant", "content": "Old answer"}, *current,
        ])
        retained[-1]["content"]["items"].append("changed")
        self.assertEqual(messages, original)

        limited = ToolContextProjection.completed_history(messages, history_turns=0, history_tool_calls=0)

        self.assertEqual(limited, [system, *current])
        self.assertEqual(messages, original)

    def test_history_without_a_user_preserves_the_existing_shallow_list_contract(self):
        messages = [{"role": "system", "content": "System"}, {"role": "assistant", "content": "Answer"}]

        projected = ToolContextProjection.completed_history(messages, history_turns=0, history_tool_calls=0)

        self.assertIsNot(projected, messages)
        self.assertEqual(projected, messages)
        self.assertIs(projected[0], messages[0])
        self.assertIs(projected[1], messages[1])
