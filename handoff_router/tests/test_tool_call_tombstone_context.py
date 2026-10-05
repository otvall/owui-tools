"""Tests for the standalone Tool Call Tombstone Context filter."""

import copy
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "tool_call_tombstone_context_tests",
    ROOT / "tool_call_tombstone_context.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def call(call_id: str, name: str = "lookup", arguments: str = '{"full":true}'):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
        "index": 7,
    }


def assistant_call(*calls: dict):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": list(calls),
        "usage": {"input_tokens": 9000},
        "model": "test-model",
    }


def tool_result(call_id: str, content: str):
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": content,
        "name": "lookup",
        "files": [{"id": "secret-file"}],
    }


def completed_turn(number: int, *, call_id: str | None = None):
    messages = [{"role": "user", "content": f"question {number}"}]
    if call_id:
        messages.extend(
            [
                assistant_call(call(call_id, arguments=f'{{"turn":{number}}}')),
                tool_result(call_id, f"large result {number}"),
            ]
        )
    messages.append(
        {
            "role": "assistant",
            "content": f"answer {number}",
            "usage": {"output_tokens": 100},
            "output": [{"private": True}],
            "reasoning_content": "private reasoning",
        }
    )
    return messages


def tombstone_calls(messages: list[dict]) -> list[dict]:
    blocks = [
        message
        for message in messages
        if message.get("content") == MODULE.TOMBSTONE_NOTICE
    ]
    if not blocks:
        return []
    if len(blocks) != 1:
        raise AssertionError("Expected exactly one tombstone assistant message")
    return blocks[0]["tool_calls"]


class ToolCallTombstoneContextTests(unittest.IsolatedAsyncioTestCase):
    async def apply(self, messages, *, turns=5):
        instance = MODULE.Filter()
        instance.valves.history_turns = turns
        body = {"messages": copy.deepcopy(messages), "metadata": {}}
        await instance.inlet(body)
        return body

    async def test_keeps_five_completed_text_turns(self):
        messages = [{"role": "system", "content": "system"}]
        for number in range(1, 8):
            messages.extend(completed_turn(number, call_id=f"call_{number}"))
        messages.append({"role": "user", "content": "current"})

        body = await self.apply(messages)
        serialized = repr(body["messages"])

        self.assertNotIn("question 1", serialized)
        self.assertNotIn("question 2", serialized)
        for number in range(3, 8):
            self.assertIn(f"question {number}", serialized)
            self.assertIn(f"answer {number}", serialized)
        self.assertIn("current", serialized)

    async def test_tombstones_are_minimal_valid_pairs_after_system(self):
        messages = [
            {"role": "system", "content": "system one"},
            {"role": "system", "content": "system two"},
            *completed_turn(1, call_id="call_0"),
            *completed_turn(2, call_id="call_1"),
            {"role": "user", "content": "current"},
        ]

        body = await self.apply(messages)
        output = body["messages"]
        calls = tombstone_calls(output)

        self.assertEqual(
            [message["role"] for message in output[:3]],
            ["system", "system", "assistant"],
        )
        self.assertEqual([item["id"] for item in calls], ["call_0", "call_1"])
        self.assertEqual(
            calls,
            [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                },
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                },
            ],
        )
        self.assertEqual(
            output[3:5],
            [
                {
                    "role": "tool",
                    "tool_call_id": "call_0",
                    "content": "[omitted]",
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "[omitted]",
                },
            ],
        )

    async def test_current_tool_chain_is_unchanged(self):
        current = [
            {"role": "user", "content": "current"},
            assistant_call(
                call("current_0", "query_database", '{"sql":"select 1"}')
            ),
            tool_result("current_0", "row one"),
        ]
        messages = [*completed_turn(1, call_id="old_0"), *current]

        body = await self.apply(messages)

        self.assertEqual(body["messages"][-len(current) :], current)

    async def test_drops_raw_historical_tool_payload_and_owui_fields(self):
        messages = [
            *completed_turn(1, call_id="call_0"),
            {"role": "user", "content": "current"},
        ]

        body = await self.apply(messages)
        serialized = repr(body["messages"])

        self.assertNotIn("large result 1", serialized)
        self.assertNotIn("private reasoning", serialized)
        self.assertNotIn("secret-file", serialized)
        self.assertNotIn("9000", serialized)
        self.assertNotIn('"turn":1', serialized)

    async def test_deduplicates_ids_and_excludes_ids_in_current_chain(self):
        messages = [
            *completed_turn(1, call_id="call_0"),
            *completed_turn(2, call_id="call_0"),
            *completed_turn(3, call_id="call_1"),
            {"role": "user", "content": "current"},
            assistant_call(call("call_1", "lookup")),
            tool_result("call_1", "current result"),
        ]

        body = await self.apply(messages)

        self.assertEqual(
            [item["id"] for item in tombstone_calls(body["messages"])],
            ["call_0"],
        )

    async def test_skips_orphan_calls_and_results(self):
        messages = [
            {"role": "user", "content": "old"},
            assistant_call(call("missing-result")),
            tool_result("missing-call", "orphan"),
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "current"},
        ]

        body = await self.apply(messages)

        self.assertEqual(tombstone_calls(body["messages"]), [])
        self.assertFalse(
            any(message.get("role") == "tool" for message in body["messages"])
        )

    async def test_incomplete_turn_does_not_consume_turn_limit(self):
        messages = [
            *completed_turn(1),
            {"role": "user", "content": "abandoned"},
            {"role": "user", "content": "current"},
        ]

        body = await self.apply(messages, turns=1)
        serialized = repr(body["messages"])

        self.assertIn("question 1", serialized)
        self.assertIn("answer 1", serialized)
        self.assertNotIn("abandoned", serialized)

    async def test_tool_image_message_does_not_start_a_new_turn(self):
        tool_image = {
            "role": "user",
            "content": [
                {"type": "text", "text": MODULE.TOOL_IMAGE_TEXT},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,x"},
                },
            ],
        }
        current = [
            {"role": "user", "content": "current"},
            assistant_call(call("current")),
            tool_result("current", "image result"),
            tool_image,
        ]
        messages = [*completed_turn(1, call_id="old"), *current]

        body = await self.apply(messages)

        self.assertEqual(body["messages"][-len(current) :], current)

    async def test_inserts_tombstones_at_start_without_system_message(self):
        messages = [
            *completed_turn(1, call_id="call_0"),
            {"role": "user", "content": "current"},
        ]

        body = await self.apply(messages)

        self.assertEqual(body["messages"][0]["content"], MODULE.TOMBSTONE_NOTICE)

    async def test_second_application_is_idempotent(self):
        instance = MODULE.Filter()
        body = {
            "messages": [
                *completed_turn(1, call_id="call_0"),
                {"role": "user", "content": "current"},
            ],
            "metadata": {},
        }
        await instance.inlet(body)
        first = copy.deepcopy(body)

        await instance.inlet(body)

        self.assertEqual(body, first)
        self.assertTrue(body["metadata"][MODULE.APPLIED_KEY])


if __name__ == "__main__":
    unittest.main()
