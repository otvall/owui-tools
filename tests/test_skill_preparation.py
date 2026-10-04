"""The same Skill policy through both deployed Functions and child dispatch."""

import types
import itertools
import json
from unittest.mock import AsyncMock, patch

from test_handoff_history import PipeTestCase, assistant, call, grouped_history, load_plain_module, result, router


class SkillBehavior:
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.records = {
            "alpha": types.SimpleNamespace(
                is_active=True, name='Alpha <guide> "A"', description="Alpha & details",
                content="Full alpha instructions <unchanged>",
            ),
            "beta": types.SimpleNamespace(
                is_active=True, name="Beta", description="Beta description", content="Full beta instructions",
            ),
        }
        self.skills.side_effect = self.records.get
        self.metadata.update(session_id="session", params={"function_calling": "native"})
        self.view_skill = AsyncMock(return_value="builtin checked permissions")
        self.builtins.return_value = {
            "view_skill": {"spec": {"name": "view_skill"}, "callable": self.view_skill},
        }
        self.model_id = {"orchestrator": "router", "child": "agent-a", "standalone": "regular"}[self.path]
        self.runtime_model = {"id": self.model_id, "info": {"meta": {}}}
        self.request.app.state.MODELS[self.model_id] = self.runtime_model
        self.metadata.update(lite_router_model_id="router", lite_router_owner_id="owner")
        messages = grouped_history()[:4] if self.path == "child" else [
            {"role": "system", "content": "Administrator prompt"},
            {"role": "user", "content": "question"},
        ]
        self.body = {"model": self.model_id, "messages": messages, "metadata": self.metadata}
        self.select(["alpha"])

    def select(self, ids):
        if self.path == "orchestrator":
            self.metadata["lite_orchestrator_skill_ids"] = ids
        elif self.path == "child":
            self.models["agent-a"].meta["skillIds"] = ids
        else:
            self.runtime_model["info"]["meta"]["skillIds"] = ids

    async def prepare(self):
        if self.path == "standalone":
            self.body = await self.skill_filter.inlet(
                self.body, __request__=self.request, __user__={"id": "user"},
                __model__=self.runtime_model,
            )
        else:
            await self.invoke_body(self.body)
            self.body = self.routed
        return self.body

    def prompt(self):
        return "\n".join(m["content"] for m in self.body["messages"] if m["role"] == "system")

    async def test_without_session_uses_full_instructions(self):
        self.metadata.pop("session_id")
        await self.prepare()
        self.assertIn("Full alpha instructions <unchanged>", self.prompt())
        self.assertNotIn("<available_skills>", self.prompt())
        self.assertNotIn("view_skill", self.metadata["tools"])
        self.builtins.assert_not_awaited()

    async def test_foreign_replacement_of_earlier_loader_is_rejected(self):
        await self.prepare()
        foreign = {"spec": {"name": "view_skill"}, "callable": AsyncMock()}
        if self.path == "child":
            self.models["agent-a"].meta["toolIds"] = ["foreign-toolkit"]
            self.tool_names = ["view_skill"]
        else:
            self.metadata["tools"]["view_skill"] = foreign
        with self.assertRaisesRegex(ValueError, "conflicts with the builtin Skill loader"):
            await self.prepare()
        if self.path != "child":
            self.assertIs(self.metadata["tools"]["view_skill"], foreign)

    async def test_lazy_full_eligibility_matrix_is_reassessed(self):
        builtin = self.builtins.return_value
        for session, mode, capability, loader in itertools.product(
            (True, False), (None, "native", "legacy"), (None, True, False), (True, False),
        ):
            with self.subTest(session=session, mode=mode, capability=capability, loader=loader):
                self.metadata["session_id"] = "session" if session else ""
                self.metadata["params"] = {} if mode is None else {"function_calling": mode}
                meta = self.runtime_model["info"]["meta"]
                meta["capabilities"] = {} if capability is None else {"builtin_tools": capability}
                self.builtins.return_value = builtin if loader else {}
                await self.prepare()
                lazy = session and mode != "legacy" and capability is not False and loader
                self.assertEqual("<available_skills>" in self.prompt(), lazy)
                self.assertEqual("Full alpha instructions <unchanged>" in self.prompt(), not lazy)
                self.assertEqual("view_skill" in self.metadata["tools"], lazy)
                self.assertEqual(sum(
                    (schema.get("function") or {}).get("name") == "view_skill"
                    for schema in self.body.get("tools", [])
                ), int(lazy))

    async def test_canonical_selection_lookup_order_rendering_and_allowlist(self):
        self.select([" BETA ", "Alpha", " beta ", "", None, "ALPHA"])
        await self.prepare()
        self.assertEqual([call.args[0] for call in self.skills.call_args_list], ["beta", "alpha"])
        prompt = self.prompt()
        self.assertLess(prompt.index("<id>beta</id>"), prompt.index("<id>alpha</id>"))
        self.assertIn("Alpha &lt;guide&gt; &quot;A&quot;", prompt)
        self.assertIn("Alpha &amp; details", prompt)
        self.assertNotIn("Full alpha instructions", prompt)
        tool = self.metadata["tools"]["view_skill"]["callable"]
        self.assertEqual(await tool(id=" ALPHA "), "builtin checked permissions")
        self.assertIn("error", json.loads(await tool(id="not-selected")))
        self.view_skill.assert_awaited_once_with(id="alpha")
        self.metadata["params"]["function_calling"] = "legacy"
        await self.prepare()
        self.assertLess(self.prompt().index('id="beta"'), self.prompt().index('id="alpha"'))
        self.assertIn('name="Alpha &lt;guide&gt; &quot;A&quot;"', self.prompt())
        self.assertIn("Full alpha instructions <unchanged>", self.prompt())

    async def test_loader_receives_role_specific_user_and_context(self):
        await self.prepare()
        params = self.builtins.call_args.args[1]
        self.assertEqual(params["__user__"], {"id": "owner" if self.path == "orchestrator" else "user"})
        self.assertEqual(params["__skill_ids__"], ["alpha"])
        self.assertIs(self.builtins.call_args.args[0], self.request)
        self.assertIs(params["__metadata__"], self.metadata)
        self.assertIs(params["__model__"], self.runtime_model)

    async def test_missing_and_inactive_skills_fail_in_both_modes(self):
        for mode, inactive in itertools.product(("native", "legacy"), (False, True)):
            with self.subTest(mode=mode, inactive=inactive):
                self.metadata["params"] = {"function_calling": mode}
                self.select(["alpha", "invalid"])
                if inactive:
                    self.records["invalid"] = types.SimpleNamespace(is_active=False)
                else:
                    self.records.pop("invalid", None)
                with self.assertRaisesRegex(ValueError, "Attached model Skills are unavailable: invalid"):
                    await self.prepare()
        self.completion.assert_not_awaited()

    async def test_empty_selection_exposes_no_context_or_loader(self):
        self.select([])
        await self.prepare()
        self.assertNotIn("alpha", self.prompt())
        self.assertNotIn("view_skill", self.metadata["tools"])
        self.skills.assert_not_awaited()

    async def test_foreign_loader_schema_is_rejected(self):
        foreign_schema = {"type": "function", "function": {"name": "view_skill", "description": "foreign"}}
        if self.path == "child":
            async def other_filter(body):
                body["tools"].append(foreign_schema)
                return body
            self.filters.insert(2, types.SimpleNamespace(inlet=other_filter))
        else:
            self.body["tools"] = [foreign_schema]
        with self.assertRaisesRegex(ValueError, "conflicts with the builtin Skill loader"):
            await self.prepare()
        self.completion.assert_not_awaited()

    async def test_changed_tool_schema_cannot_be_authorized_by_earlier_loader(self):
        await self.prepare()
        if self.path == "child":
            self.models["agent-a"].meta["toolIds"] = ["foreign-toolkit"]
            self.tool_names = ["view_skill"]
        else:
            self.metadata["tools"]["view_skill"]["spec"] = {
                "name": "view_skill", "description": "Foreign replacement schema",
            }
        with self.assertRaisesRegex(ValueError, "conflicts with the builtin Skill loader"):
            await self.prepare()

    async def test_repeated_preparation_replaces_owned_loader_and_preserves_prompt(self):
        await self.prepare()
        earlier = self.metadata["tools"]["view_skill"]["callable"]
        await self.prepare()
        self.assertIsNot(self.metadata["tools"]["view_skill"]["callable"], earlier)
        self.assertEqual(self.prompt().count("<available_skills>"), 1)
        self.assertIn("specialist selected" if self.path == "child" else "Administrator prompt", self.prompt())
        if self.path == "orchestrator":
            self.assertIn("Load a relevant routing Skill before calling lite_delegate", self.prompt())

    async def test_skill_edits_and_status_are_refreshed_without_selection_change(self):
        await self.prepare()
        self.records["alpha"].name = "Current name"
        self.records["alpha"].description = "Current description"
        self.records["alpha"].content = "Current full instructions"
        await self.prepare()
        self.assertIn("Current name", self.prompt())
        self.assertIn("Current description", self.prompt())
        self.assertNotIn("Alpha &amp; details", self.prompt())
        self.metadata["params"] = {"function_calling": "legacy"}
        await self.prepare()
        self.assertIn("Current full instructions", self.prompt())
        self.records["alpha"].is_active = False
        with self.assertRaisesRegex(ValueError, "Attached model Skills are unavailable: alpha"):
            await self.prepare()

    async def test_selection_changes_and_removal_replace_context_and_loader(self):
        await self.prepare()
        self.select(["beta"])
        await self.prepare()
        self.assertNotIn("<id>alpha</id>", self.prompt())
        self.assertIn("<id>beta</id>", self.prompt())
        tool = self.metadata["tools"]["view_skill"]["callable"]
        self.assertIn("error", json.loads(await tool(id="alpha")))
        self.select([])
        await self.prepare()
        self.assertNotIn("<available_skills>", self.prompt())
        self.assertNotIn("view_skill", self.metadata["tools"])
        self.assertFalse(any((s.get("function") or {}).get("name") == "view_skill" for s in self.body.get("tools", [])))

    async def test_builtin_exceptions_do_not_fall_back_to_full_context(self):
        await self.prepare()
        self.builtins.side_effect = RuntimeError("builtin dependency failed")
        with self.assertRaisesRegex(RuntimeError, "builtin dependency failed"):
            await self.prepare()

    async def test_skill_refresh_preserves_ordinary_tools_mcp_and_shared_references(self):
        client = types.SimpleNamespace(call_tool=AsyncMock(return_value="MCP result"))
        self.connector.return_value = (client, [{"name": "fetch"}])
        if self.path == "orchestrator":
            self.metadata["lite_base_tool_ids"] = ["Toolkit", "server:mcp:documents"]
        elif self.path == "child":
            self.models["agent-a"].meta["toolIds"] = ["Toolkit", "server:mcp:documents"]
        else:
            self.metadata["tools"]["lookup"] = {
                "spec": {"name": "lookup"}, "callable": AsyncMock(),
            }
            self.body["tools"] = [{"type": "function", "function": {"name": "lookup"}}]
        shared = self.metadata["tools"]
        await self.prepare()
        lookup = shared["lookup"]["callable"]
        mcp = shared.get("documents_fetch")
        self.records["alpha"].content = "Edited full instructions"
        self.metadata["params"]["function_calling"] = "legacy"
        self.body["messages"] += [assistant(call("continued", "lookup")), result("continued", "continued result")]
        await self.prepare()
        self.assertIn("Edited full instructions", self.prompt())
        self.assertIs(self.metadata["tools"], shared)
        self.assertIs(self.body["metadata"], self.metadata)
        self.assertIs(shared["lookup"]["callable"], lookup)
        self.assertNotIn("view_skill", shared)
        if self.path == "standalone":
            self.loader.assert_not_awaited()
            self.connector.assert_not_awaited()
        else:
            self.loader.assert_awaited_once()
            self.connector.assert_awaited_once()
            self.assertEqual(self.loader.call_args.args[1], ["Toolkit"])
            self.assertIs(shared["documents_fetch"], mcp)
            self.assertEqual(await mcp["callable"](id="item"), "MCP result")
        if self.path == "child":
            self.assertEqual(await lookup(), self.body["messages"])

    async def test_newly_missing_skill_is_detected_on_next_preparation(self):
        await self.prepare()
        del self.records["alpha"]
        with self.assertRaisesRegex(ValueError, "Attached model Skills are unavailable: alpha"):
            await self.prepare()

    async def test_allowed_loader_calls_keep_builtin_permission_errors(self):
        await self.prepare()
        self.view_skill.side_effect = PermissionError("Skill permission denied")
        with self.assertRaisesRegex(PermissionError, "Skill permission denied"):
            await self.metadata["tools"]["view_skill"]["callable"](id="alpha")

    async def test_full_fallback_preserves_foreign_tool_after_owned_loader_replacement(self):
        await self.prepare()
        foreign = {"spec": {"name": "view_skill"}, "callable": AsyncMock()}
        if self.path == "child":
            self.models["agent-a"].meta["toolIds"] = ["foreign-toolkit"]
            self.tool_names = ["view_skill"]
        else:
            self.metadata["tools"]["view_skill"] = foreign
        self.metadata["params"]["function_calling"] = "legacy"
        await self.prepare()
        self.assertIn("Full alpha instructions", self.prompt())
        self.assertIn("view_skill", self.metadata["tools"])
        if self.path != "child":
            self.assertIs(self.metadata["tools"]["view_skill"], foreign)


class OrchestratorSkillTests(SkillBehavior, PipeTestCase):
    path = "orchestrator"

    async def test_registry_canonicalizes_configured_skills_before_pipe(self):
        registry = load_plain_module("lite_subagent_registry.py", "registry_skill_tests", {
            "open_webui.config": types.SimpleNamespace(BYPASS_ADMIN_ACCESS_CONTROL=False),
            "open_webui.env": types.SimpleNamespace(BYPASS_MODEL_ACCESS_CONTROL=False),
            "open_webui.models.models": types.SimpleNamespace(Models=router.Models),
            "open_webui.models.skills": types.SimpleNamespace(Skills=router.Skills),
            "open_webui.models.users": types.SimpleNamespace(Users=router.Users),
            "open_webui.utils.models": types.SimpleNamespace(check_model_access=AsyncMock()),
        })
        self.enterContext(patch.dict(registry.SUBAGENTS, {"AgentCase": " BETA "}, clear=True))
        self.user.id, self.user.role = "user", "user"
        self.request.app.state.MODELS["AgentCase"] = {"id": "AgentCase"}
        self.models["AgentCase"] = self.models["agent-a"]
        self.runtime_model["id"] = "RouterCase"
        self.request.app.state.MODELS["RouterCase"] = self.runtime_model
        self.models["RouterCase"] = types.SimpleNamespace(is_active=True, user_id="owner")
        self.body["model"] = "RouterCase"
        filter = registry.Filter()
        filter.valves.base_skill_ids = [" ALPHA ", "alpha", ""]
        filter.valves.base_tool_ids = ["Toolkit"]
        await filter.inlet(self.body, __request__=self.request, __user__={"id": "user"})
        previous = load_plain_module("previous_tool_context.py", "skill_registry_previous_tests").Filter()
        cleanup = load_plain_module("history_cleanup.py", "skill_registry_cleanup_tests").Filter()
        await previous.inlet(self.body)
        await cleanup.inlet(self.body)
        await self.prepare()
        self.assertLess(self.prompt().index("<id>alpha</id>"), self.prompt().index("<id>beta</id>"))
        self.assertEqual({call.args[0] for call in self.skills.call_args_list}, {"alpha", "beta"})
        self.assertEqual(self.metadata["lite_agents"]["AgentCase"]["model_id"], "AgentCase")
        self.assertEqual(self.metadata["lite_agents"]["AgentCase"]["routing_skill_id"], "beta")
        self.assertEqual(self.loader.call_args.args[1], ["Toolkit"])


class ChildSkillTests(SkillBehavior, PipeTestCase):
    path = "child"


class StandaloneSkillTests(SkillBehavior, PipeTestCase):
    path = "standalone"
