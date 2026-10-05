"""Workspace Model preparation through its interface and public Pipe continuations."""

import types
from unittest.mock import AsyncMock

from test_handoff_history import PipeTestCase, grouped_history, router


class WorkspaceModelPreparationTests(PipeTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.workspace = router.WorkspaceModelPreparation(
            lookup_model=self.model_lookup,
            capability_resolver=router.ModelCapabilityResolver(router.McpRuntime()),
        )

    async def prepare_workspace(self, body=None):
        runtime = router.RequestRuntime(self.request, self.metadata)
        context = router.InvocationContext(request=self.request, user=self.user)
        with runtime.prepare_model("child", body if body is not None else {"messages": []}) as prepared:
            child = await self.workspace.prepare(
                "agent-a", prepared=prepared, runtime=runtime, context=context,
            )
        return child, prepared.body

    async def test_matching_ids_use_one_record_for_attachments_and_owner(self):
        self.model_lookup.side_effect = [
            self.models["agent-a"],
            types.SimpleNamespace(is_active=True, user_id="different-owner", meta={"toolIds": ["other"]}),
        ]
        child, body = await self.prepare_workspace({"messages": [], "temperature": 0.9})

        self.model_lookup.assert_awaited_once_with("agent-a")
        self.users.assert_awaited_once_with("owner")
        self.assertEqual(child.capabilities.tool_ids, ["toolkit"])
        self.assertEqual(child.capabilities.skill_ids, [])
        self.assertIs(child.runtime_model, self.request.app.state.MODELS["agent-a"])
        self.assertNotIn("temperature", body)

    async def test_each_preparation_reads_fresh_attachments_and_owner(self):
        self.model_lookup.side_effect = [
            self.models["agent-a"],
            types.SimpleNamespace(is_active=True, user_id="new-owner", meta={"toolIds": ["reader"]}),
        ]
        first, _ = await self.prepare_workspace()
        second, _ = await self.prepare_workspace()

        self.assertEqual(first.capabilities.tool_ids, ["toolkit"])
        self.assertEqual(second.capabilities.tool_ids, ["reader"])
        self.assertEqual([item.args[0] for item in self.users.await_args_list], ["owner", "new-owner"])
        self.assertEqual(self.loader.await_count, 2)
        self.assertEqual(self.model_lookup.await_count, 2)
        self.assertIsNot(first.capabilities.tools, second.capabilities.tools)

    async def test_inactive_model_rejects_continuation_before_reusing_tools(self):
        await self.invoke(grouped_history()[:4])
        lookup = self.metadata["tools"]["lookup"]["callable"]
        self.models["agent-a"].is_active = False
        self.completion.reset_mock()

        with self.assertRaisesRegex(ValueError, 'Agent "agent-a" is inactive'):
            await self.invoke(grouped_history())

        self.completion.assert_not_awaited()
        self.loader.assert_awaited_once()
        self.assertIs(self.metadata["tools"]["lookup"]["callable"], lookup)
        self.assertEqual(await lookup(), self.metadata["lite_child_messages"])

    async def test_owner_change_keeps_matching_continuation_cache(self):
        await self.invoke(grouped_history()[:4])
        lookup = self.metadata["tools"]["lookup"]["callable"]
        self.models["agent-a"].user_id = "missing-owner"
        self.owner = None

        await self.invoke(grouped_history())

        self.assertEqual([item.args[0] for item in self.users.await_args_list if item.args[0] != "user"], ["owner"])
        self.loader.assert_awaited_once()
        self.assertIs(self.metadata["tools"]["lookup"]["callable"], lookup)
        self.assertEqual(await lookup(), self.routed["messages"])

    async def test_distinct_ids_read_capability_owner_only_on_cache_miss(self):
        self.request.app.state.MODELS["agent-a"]["id"] = " capability-model "
        capability_record = types.SimpleNamespace(is_active=True, user_id="capability-owner")
        self.models["capability-model"] = capability_record

        first, _ = await self.prepare_workspace()
        capability_record.is_active = False
        second, _ = await self.prepare_workspace()

        self.assertEqual([item.args[0] for item in self.model_lookup.await_args_list], ["agent-a", "capability-model", "agent-a"])
        self.users.assert_awaited_once_with("capability-owner")
        self.assertEqual(first.capabilities.tool_ids, ["toolkit"])
        self.assertEqual(self.metadata["lite_active_tool_runtime"]["model_id"], "capability-model")
        self.assertIs(first.capabilities.tools, second.capabilities.tools)
        self.loader.assert_awaited_once()

    async def test_distinct_unavailable_capability_record_preserves_error(self):
        self.request.app.state.MODELS["agent-a"]["id"] = "capability-model"
        for record in (None, types.SimpleNamespace(is_active=False, user_id="owner")):
            with self.subTest(record=record):
                self.models["capability-model"] = record
                with self.assertRaisesRegex(ValueError, "Child Model capability owner is unavailable"):
                    await self.prepare_workspace()

        self.loader.assert_not_awaited()
        self.users.assert_not_awaited()

    async def test_missing_workspace_record_requires_a_pipe(self):
        del self.models["agent-a"]

        with self.assertRaisesRegex(ValueError, 'Agent "agent-a" must be a Workspace Model or Pipe'):
            await self.prepare_workspace()

        self.loader.assert_not_awaited()
        self.builtins.assert_not_awaited()

    async def test_pipe_without_workspace_record_loads_empty_capabilities(self):
        del self.models["agent-a"]
        self.request.app.state.MODELS["agent-a"]["pipe"] = {"type": "pipe"}
        self.metadata.update(session_id="session", params={"function_calling": "native"})

        child, _ = await self.prepare_workspace()

        self.model_lookup.assert_awaited_once_with("agent-a")
        self.assertEqual(child.capabilities.tool_ids, [])
        self.assertEqual(child.capabilities.skill_ids, [])
        self.assertEqual(child.capabilities.tools, {})
        self.loader.assert_not_awaited()
        self.builtins.assert_not_awaited()
        self.users.assert_not_awaited()

    async def test_pipe_with_distinct_capability_record_receives_builtins(self):
        del self.models["agent-a"]
        self.request.app.state.MODELS["agent-a"].update(id="capability-model", pipe={"type": "pipe"})
        self.models["capability-model"] = types.SimpleNamespace(is_active=True, user_id="owner")
        self.metadata.update(session_id="session", params={"function_calling": "native"})
        self.builtins.return_value = {"search_web": {"spec": {"name": "search_web"}, "callable": AsyncMock()}}

        child, _ = await self.prepare_workspace()

        self.assertEqual(
            (child.capabilities.tool_ids, child.capabilities.skill_ids, set(child.capabilities.tools)),
            ([], [], {"search_web"}),
        )

    async def test_pipe_transition_keeps_matching_empty_selection_cache(self):
        self.models["agent-a"].meta = {}
        self.metadata.update(session_id="session", params={"function_calling": "native"})
        self.builtins.return_value = {"search_web": {"spec": {"name": "search_web"}, "callable": AsyncMock()}}
        first, _ = await self.prepare_workspace()
        del self.models["agent-a"]
        self.request.app.state.MODELS["agent-a"]["pipe"] = {"type": "pipe"}

        second, _ = await self.prepare_workspace()

        self.assertEqual(set(second.capabilities.tools), {"search_web"})
        self.assertIs(first.capabilities.tools, second.capabilities.tools)
        self.builtins.assert_awaited_once()
        self.users.assert_not_awaited()

    async def test_metadata_shapes_preserve_attachment_normalization(self):
        meta = {"toolIds": [" Toolkit ", "Toolkit", ""], "skillIds": [" Skill-A ", "skill-a", ""]}
        for value, tools, skills in (
            (meta, ["Toolkit"], ["skill-a"]),
            (types.SimpleNamespace(model_dump=lambda: meta), ["Toolkit"], ["skill-a"]),
            (object(), [], []),
        ):
            with self.subTest(meta=value):
                self.models["agent-a"].meta = value
                child, _ = await self.prepare_workspace()
                self.assertEqual(child.capabilities.tool_ids, tools)
                self.assertEqual(child.capabilities.skill_ids, skills)

        self.assertEqual(meta, {"toolIds": [" Toolkit ", "Toolkit", ""], "skillIds": [" Skill-A ", "skill-a", ""]})
