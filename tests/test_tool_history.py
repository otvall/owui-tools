"""Completed Tool history facts through the shared interpretation interface."""

import copy
import json
import unittest

from shared.tool_history import analyze_history
from test_handoff_history import assistant, call, marker, result
from test_previous_turn_context import registry_metadata


class ToolHistoryTests(unittest.TestCase):
    def test_grouped_result_order_drives_attribution_without_reordering_source_calls(self):
        registry = registry_metadata()["lite_agents"]
        messages = [
            {"role": "user", "content": "Question"},
            assistant(call("a", "lookup"), call("to-b", "lite_delegate"),
                      call("root", "lookup"), call("to-a", "lite_delegate"), call("b", "lookup")),
            result("root", "ROOT"), result("to-a", marker("route-a")), result("a", "A"),
            result("to-b", marker("agent-b")), result("b", "B"),
        ]
        original, original_registry = copy.deepcopy(messages), copy.deepcopy(registry)

        history = analyze_history(messages, registry=registry)

        self.assertEqual([messages[item.result_index]["tool_call_id"] for item in history.exchanges],
                         ["root", "to-a", "a", "to-b", "b"])
        self.assertEqual([(item.executor.kind, item.executor.agent_id) for item in history.exchanges],
                         [("orchestrator", None), ("orchestrator", None), ("subagent", "agent-a"),
                          ("subagent", "agent-a"), ("subagent", "agent-b")])
        self.assertEqual(history.current_handoff, "agent-b")
        self.assertEqual(messages, original)
        self.assertEqual(registry, original_registry)

    def test_standalone_keeps_model_attribution_but_preserves_unavailable_current_handoff(self):
        messages = [
            {"role": "user", "content": "Question"},
            assistant(call("delegate", "lite_delegate")), result("delegate", marker("removed-agent")),
            assistant(call("work", "lookup")), result("work", "COMPLETED"),
        ]

        standalone = analyze_history(messages)
        router = analyze_history(messages, registry={})

        self.assertEqual(standalone.current_handoff, "removed-agent")
        self.assertEqual([item.executor.kind for item in standalone.exchanges], ["model", "model"])
        self.assertEqual(router.current_handoff, "removed-agent")
        self.assertEqual(router.exchanges[-1].executor.kind, "unknown")
        self.assertEqual(router.exchanges[-1].executor.declared_agent_id, "removed-agent")

    def test_tool_images_do_not_start_a_request_and_new_user_resets_handoff(self):
        image = {"role": "user", "content": [
            {"type": "text", "text": "Here are the images from the tool results above. Please analyze them."},
            {"type": "image_url", "image_url": {"url": "https://example.test/image.png"}},
        ]}
        messages = [
            {"role": "user", "content": "First request"},
            assistant(call("delegate", "lite_delegate")), image, result("delegate", marker()),
            {"role": "user", "content": "Second request"},
            assistant(call("work", "lookup")), result("work", "ROOT"),
        ]

        history = analyze_history(messages, registry=registry_metadata()["lite_agents"])

        self.assertEqual(history.user_indices, (0, 4))
        self.assertIsNone(history.current_handoff)
        self.assertEqual(history.exchanges[-1].executor.kind, "orchestrator")

    def test_marker_shapes_and_non_delegate_results_do_not_invent_current_handoff(self):
        for name, value, expected in (
            ("lite_delegate", marker(), "agent-a"),
            ("lite_delegate", json.dumps(marker()), "agent-a"),
            ("lite_delegate", {"__lite_delegate__": "v2", "agent_id": " agent-a "}, "agent-a"),
            ("lookup", marker(), None),
            ("lite_delegate", {"__lite_delegate__": "v1", "skill_id": "agent-a"}, None),
            ("lite_delegate", {"__lite_delegate__": "v2", "skill_id": "agent-a"}, None),
            ("lite_delegate", "malformed", None),
        ):
            with self.subTest(name=name, value=value):
                history = analyze_history([
                    {"role": "user", "content": "Question"},
                    assistant(call("call", name)), result("call", value),
                ])
                self.assertEqual(history.current_handoff, expected)
