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
        if self.path == "standalone":
            self.metadata.clear()
            self.metadata["tools"] = {}
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
        self.metadata.update(
            chat_id="chat", features={"web_search": True},
            files=["attachment"],
        )
        await self.prepare()
        params = self.builtins.call_args.args[1]
        self.assertEqual(params["__user__"], {"id": "owner" if self.path == "orchestrator" else "user"})
        self.assertEqual(params["__skill_ids__"], ["alpha"])
        self.assertIs(self.builtins.call_args.args[0], self.request)
        self.assertIs(params["__metadata__"], self.metadata)
        self.assertIs(params["__model__"], self.runtime_model)
        self.assertEqual(params["__chat_id__"], "chat")
        self.assertIsNone(params["__message_id__"])
        self.assertIn("__event_emitter__", params)
        self.assertIn("__event_call__", params)
        self.assertIn("__oauth_token__", params)
        self.assertEqual(set(params), {
            "__user__", "__metadata__", "__model__", "__event_emitter__",
            "__event_call__", "__oauth_token__", "__chat_id__", "__message_id__", "__skill_ids__",
        } | (set() if self.path == "standalone" else {
            "__request__", "__session_id__", "__messages__", "__files__", "__features__",
        }))
        if self.path == "orchestrator":
            self.assertEqual(self.builtins.call_args.kwargs, {"model": self.runtime_model})
        else:
            self.assertEqual(self.builtins.call_args.kwargs, {
                "model": self.runtime_model, "features": self.metadata["features"],
            })
        if self.path != "standalone":
            history_key = "lite_base_messages" if self.path == "orchestrator" else "lite_child_messages"
            self.assertIs(params["__messages__"], self.metadata[history_key])
            self.assertIs(params["__files__"], self.metadata["files"])
            self.assertIs(params["__features__"], self.metadata["features"])

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
        if self.path == "child":
            self.assertFalse(any(m["role"] == "system" for m in self.body["messages"]))

    async def test_foreign_loader_schema_follows_preparation_boundary(self):
        foreign_schema = {"type": "function", "function": {"name": "view_skill", "description": "foreign"}}
        if self.path == "child":
            async def other_filter(body):
                body["tools"].append(foreign_schema)
                return body
            self.filters.append(types.SimpleNamespace(inlet=other_filter))
            await self.prepare()
            self.assertEqual(self.body["tools"][-1], foreign_schema)
            self.assertEqual(await self.metadata["tools"]["lookup"]["callable"](), self.body["messages"])
            return
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
        if self.path != "child":
            self.assertIn("Administrator prompt", self.prompt())
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
            self.runtime_model["info"]["meta"]["skillIds"] = ["alpha", "beta"]
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
            self.assertNotIn("beta", self.prompt())

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

    async def test_owner_lookup_is_deferred_until_skills_are_valid_and_loader_is_eligible(self):
        self.owner = None
        for unavailable in ("empty", "missing", "inactive", "session", "legacy", "capability"):
            with self.subTest(unavailable=unavailable):
                self.select(["alpha"])
                self.records["alpha"].is_active = True
                self.metadata.update(session_id="session", params={"function_calling": "native"})
                self.runtime_model["info"]["meta"]["capabilities"] = {"builtin_tools": True}
                if unavailable == "empty":
                    self.select([])
                elif unavailable == "missing":
                    self.select(["missing"])
                elif unavailable == "inactive":
                    self.records["alpha"].is_active = False
                elif unavailable == "session":
                    self.metadata["session_id"] = ""
                elif unavailable == "legacy":
                    self.metadata["params"]["function_calling"] = "legacy"
                else:
                    self.runtime_model["info"]["meta"]["capabilities"]["builtin_tools"] = False
                self.users.reset_mock()
                self.builtins.reset_mock()
                if unavailable in ("missing", "inactive"):
                    with self.assertRaisesRegex(ValueError, "Attached model Skills are unavailable"):
                        await self.prepare()
                else:
                    await self.prepare()
                self.assertEqual([item.args[0] for item in self.users.await_args_list], ["user"])
                self.builtins.assert_not_awaited()

        self.select(["alpha"])
        self.records["alpha"].is_active = True
        self.metadata.update(session_id="session", params={"function_calling": "native"})
        self.runtime_model["info"]["meta"]["capabilities"] = {"builtin_tools": True}
        self.users.reset_mock()
        with self.assertRaisesRegex(ValueError, "Model capability owner is unavailable"):
            await self.prepare()
        self.assertEqual([item.args[0] for item in self.users.await_args_list], ["user", "owner"])
        self.builtins.assert_not_awaited()

    def registry_filter(self):
        registry = load_plain_module("lite_subagent_registry.py", "registry_lifecycle_tests", {
            "open_webui.config": types.SimpleNamespace(BYPASS_ADMIN_ACCESS_CONTROL=False),
            "open_webui.env": types.SimpleNamespace(BYPASS_MODEL_ACCESS_CONTROL=False),
            "open_webui.models.models": types.SimpleNamespace(Models=router.Models),
            "open_webui.models.skills": types.SimpleNamespace(Skills=router.Skills),
            "open_webui.models.users": types.SimpleNamespace(Users=router.Users),
            "open_webui.utils.models": types.SimpleNamespace(check_model_access=AsyncMock()),
        })
        self.enterContext(patch.dict(registry.SUBAGENTS, {"agent-b": "beta"}, clear=True))
        self.user.id, self.user.role = "user", "user"
        self.models["router"] = types.SimpleNamespace(is_active=True, user_id="owner")
        filter = registry.Filter()
        filter.valves.base_skill_ids = ["alpha"]
        filter.valves.base_tool_ids = []
        return filter

    async def test_new_request_resets_successful_handoff_and_prepares_current_registry(self):
        self.metadata["params"]["function_calling"] = "native"
        self.models["agent-a"].meta["skillIds"] = ["alpha"]
        await self.invoke(grouped_history()[:4])
        shared = self.metadata["tools"]
        ordinary = shared["lookup"]
        schemas = self.routed["tools"]
        legacy_keys = ("lite_history_boundary", "lite_router_user_index", "lite_active_skill_id", "lite_orchestrator_skill_context")
        self.metadata.update({key: "old" for key in legacy_keys})
        self.metadata["platform"] = "keep authoritative"
        client = object()
        self.metadata["mcp_clients"] = {"existing": client}
        request_state = {**self.metadata, "platform": "keep request", "session_setting": "keep"}
        self.request.state.metadata = request_state
        body = {"model": "router", "metadata": self.metadata, "messages": self.routed["messages"] + [
            {"role": "user", "content": "new task"},
        ], "tools": schemas}
        registry = self.registry_filter()
        registry.valves.base_skill_ids = ["alpha"]
        self.begin_request(metadata=self.request.state.metadata)
        await registry.inlet(body, __request__=self.request, __user__={"id": "user"})
        for key in (*legacy_keys, "lite_active_handoff", "lite_active_model_id", "lite_active_tool_runtime",
                    "lite_base_tool_runtime", "lite_unfiltered_messages", "lite_child_messages", "lite_skill_loader",
                    "tool_call_filter_applied", "subagent_context_applied", "skill_context_applied",
                    "lite_subagent_filter_run", "lite_target_model_id"):
            self.assertNotIn(key, self.metadata)
            self.assertNotIn(key, request_state)
        self.assertIs(self.metadata["tools"], shared)
        self.assertIs(request_state["tools"], shared)
        self.assertIs(shared["lookup"], ordinary)
        self.assertNotIn("view_skill", shared)
        self.assertFalse(any(s["function"]["name"] == "view_skill" for s in body["tools"]))
        self.assertIs(self.metadata["mcp_clients"]["existing"], client)
        self.assertEqual(list(self.metadata["lite_agents"]), ["agent-b"])
        self.assertEqual(self.metadata["lite_orchestrator_skill_ids"], ["alpha", "beta"])
        previous = load_plain_module("previous_tool_context.py", "lifecycle_previous_tests").Filter()
        cleanup = load_plain_module("history_cleanup.py", "lifecycle_cleanup_tests").Filter()
        await previous.inlet(body, __request__=self.request)
        await cleanup.inlet(body, __request__=self.request)
        self.records["alpha"].description = "new request current Skill"
        await self.invoke_body(body)
        self.assertEqual(self.routed["model"], "base-model")
        self.assertIs(self.routed["metadata"], self.metadata)
        self.assertIn("new request current Skill", self.routed["messages"][0]["content"])
        self.assertIn("<id>beta</id>", self.routed["messages"][0]["content"])
        self.assertNotIn("lite_active_handoff", self.metadata)
        self.assertEqual(request_state["platform"], "keep request")
        self.assertEqual(request_state["session_setting"], "keep")
        self.assertEqual(self.metadata["platform"], "keep authoritative")
        for key in ("previous_tool_context_applied", "history_cleanup_applied", "lite_skill_loader", "lite_view_skill_available"):
            self.assertEqual(request_state[key], self.metadata[key])

    async def test_new_request_loader_cleanup_respects_callable_and_schema_ownership(self):
        for replacement, include_schemas in (("owned", True), ("absent", True), ("foreign", True), ("owned", False)):
            with self.subTest(replacement=replacement, include_schemas=include_schemas):
                await self.prepare()
                foreign = {"spec": {"name": "view_skill", "description": "foreign"}, "callable": AsyncMock()}
                if replacement == "absent":
                    self.metadata["tools"].pop("view_skill")
                elif replacement == "foreign":
                    self.metadata["tools"]["view_skill"] = foreign
                old_schema = next(s for s in self.body["tools"] if s["function"]["name"] == "view_skill")
                if not include_schemas:
                    self.body.pop("tools")
                self.body["model"] = "router"
                self.body["messages"] += [
                    {"role": "assistant", "content": "Done"},
                    {"role": "user", "content": "question"},
                ]
                registry = self.registry_filter()
                self.begin_request(metadata=self.request.state.metadata)
                await registry.inlet(self.body, __request__=self.request, __user__={"id": "user"})
                self.assertNotIn("lite_skill_loader", self.metadata)
                await self.context_inlets(self.body)
                if replacement == "foreign":
                    self.assertIs(self.metadata["tools"]["view_skill"], foreign)
                    self.assertIn(old_schema, self.body["tools"])
                    with self.assertRaisesRegex(ValueError, "conflicts with the builtin Skill loader"):
                        await self.prepare()
                    self.metadata["tools"].pop("view_skill")
                    self.body["tools"] = []
                else:
                    self.assertNotIn("view_skill", self.metadata["tools"])
                    self.assertNotIn(old_schema, self.body.get("tools", []))

    async def test_registry_validation_failure_does_not_reset_previous_request(self):
        await self.prepare()
        snapshot = dict(self.metadata)
        old_loader = self.metadata["tools"]["view_skill"]
        self.body["model"] = "router"
        registry = self.registry_filter()
        self.records.pop("alpha")
        with self.assertRaisesRegex(ValueError, "Configured model-bound Skills are unavailable"):
            self.begin_request(metadata=self.request.state.metadata)
            await registry.inlet(self.body, __request__=self.request, __user__={"id": "user"})
        self.assertEqual(self.metadata, snapshot)
        self.assertIs(self.metadata["tools"]["view_skill"], old_loader)

    async def test_registry_initializes_invalid_tool_registry_and_keeps_foreign_schema_conflict(self):
        self.metadata["tools"] = ["invalid registry"]
        schema = {"type": "function", "function": {"name": "view_skill", "description": "foreign"}}
        self.body["tools"] = [schema]
        registry = self.registry_filter()
        self.begin_request(metadata=self.request.state.metadata)
        await registry.inlet(self.body, __request__=self.request, __user__={"id": "user"})
        self.assertEqual(self.metadata["tools"], {})
        self.assertEqual(self.body["tools"], [schema])
        await self.context_inlets(self.body)
        with self.assertRaisesRegex(ValueError, "conflicts with the builtin Skill loader"):
            await self.prepare()
        self.completion.assert_not_awaited()

    async def test_orchestrator_provider_failure_retains_prepared_cache_and_loader(self):
        self.metadata["lite_base_tool_ids"] = ["toolkit"]
        self.completion.side_effect = RuntimeError("provider failed")
        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            await self.prepare()
        self.completion.assert_awaited_once()
        self.assertIn("lite_base_tool_runtime", self.metadata)
        self.assertIn("lookup", self.metadata["tools"])
        self.assertIn("view_skill", self.metadata["tools"])
        self.assertIs(self.metadata["lite_skill_loader"]["callable"], self.metadata["tools"]["view_skill"]["callable"])

    async def test_failed_orchestrator_skill_preparation_restores_tools_cache_and_attachments(self):
        await self.prepare()
        shared = self.metadata["tools"]
        old_loader = shared["view_skill"]
        ownership = self.metadata["lite_skill_loader"]
        self.metadata.update(lite_base_tool_ids=["toolkit"], tool_ids=["existing"], skill_ids=["old"])
        state = dict(self.metadata)
        self.records.pop("alpha")
        self.completion.reset_mock()
        with self.assertRaisesRegex(ValueError, "Skills are unavailable"):
            await self.prepare()
        self.completion.assert_not_awaited()
        self.assertEqual(self.metadata, state)
        self.assertIs(self.metadata["tools"], shared)
        self.assertIs(shared["view_skill"], old_loader)
        self.assertIs(self.metadata["lite_skill_loader"], ownership)
        self.assertNotIn("lookup", shared)
        self.assertIs(self.metadata["lite_base_tool_runtime"], state["lite_base_tool_runtime"])
        self.assertEqual(self.metadata["tool_ids"], ["existing"])
        self.loader.assert_awaited_once()

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
        self.begin_request(metadata=self.request.state.metadata)
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

    async def test_removing_skills_filters_previous_loader_exchange_on_the_next_continuation(self):
        self.runtime_model["info"]["meta"]["skillIds"] = ["alpha"]
        await self.prepare()
        self.body["messages"] += [
            assistant(call("loaded-alpha", "view_skill", id="alpha")),
            result("loaded-alpha", "Full alpha instructions <unchanged>"),
            assistant(call("lookup", "lookup")), result("lookup", "CURRENT_RESULT"),
        ]
        await self.prepare()
        self.assertEqual(
            [message for message in self.body["messages"] if message.get("tool_calls") or message["role"] == "tool"],
            [
                assistant(call("loaded-alpha", "view_skill", id="alpha")),
                result("loaded-alpha", "Full alpha instructions <unchanged>"),
                assistant(call("lookup", "lookup")), result("lookup", "CURRENT_RESULT"),
            ],
        )
        self.select([])

        await self.prepare()

        self.assertEqual(
            [message for message in self.body["messages"] if message.get("tool_calls") or message["role"] == "tool"],
            [assistant(call("lookup", "lookup")), result("lookup", "CURRENT_RESULT")],
        )
        self.assertNotIn("view_skill", self.metadata["tools"])
        self.assertFalse(any(schema["function"]["name"] == "view_skill" for schema in self.body["tools"]))

    async def test_loader_eligibility_changes_filter_the_exchange_without_detaching_skills(self):
        available_builtins = self.builtins.return_value
        for unavailable in ("legacy", "session", "capability", "builtin"):
            with self.subTest(unavailable=unavailable):
                self.metadata.update(session_id="session", params={"function_calling": "native"})
                self.runtime_model["info"]["meta"]["capabilities"] = {"builtin_tools": True}
                self.builtins.return_value = available_builtins
                self.body["messages"] = grouped_history()[:4]
                await self.prepare()
                self.body["messages"] += [
                    assistant(call("loaded-alpha", "view_skill", id="alpha")),
                    result("loaded-alpha", "Full alpha instructions <unchanged>"),
                    assistant(call("lookup", "lookup")), result("lookup", "CURRENT_RESULT"),
                ]
                if unavailable == "legacy":
                    self.metadata["params"] = {"function_calling": "legacy"}
                elif unavailable == "session":
                    self.metadata.pop("session_id")
                elif unavailable == "capability":
                    self.runtime_model["info"]["meta"]["capabilities"]["builtin_tools"] = False
                else:
                    self.builtins.return_value = {}

                await self.prepare()

                self.assertEqual(
                    [message for message in self.body["messages"] if message.get("tool_calls") or message["role"] == "tool"],
                    [assistant(call("lookup", "lookup")), result("lookup", "CURRENT_RESULT")],
                )
                self.assertIn("Full alpha instructions <unchanged>", self.prompt())
                self.assertNotIn("view_skill", self.metadata["tools"])

    async def test_database_removal_clears_skills_despite_stale_runtime_and_outer_selection(self):
        self.runtime_model["info"]["meta"]["skillIds"] = ["alpha"]
        request_state = {"platform": "keep request"}
        self.request.state.metadata = request_state
        for mode in ("native", "legacy"):
            with self.subTest(mode=mode):
                self.metadata["params"] = {"function_calling": mode}
                self.select(["alpha"])
                await self.prepare()
                self.assertIn("alpha", self.prompt())
                shared = self.metadata["tools"]
                history = self.metadata["lite_child_messages"]
                self.select([])
                self.body["skill_ids"] = ["alpha", "beta"]
                self.metadata["lite_orchestrator_skill_ids"] = ["beta"]
                outer_metadata = {"skill_ids": ["alpha"], "platform": "keep outer"}
                self.body["metadata"] = outer_metadata
                self.skills.reset_mock()
                self.builtins.reset_mock()

                await self.prepare()

                self.assertEqual(self.runtime_model["info"]["meta"]["skillIds"], ["alpha"])
                self.assertNotIn("alpha", self.prompt())
                self.assertNotIn("beta", self.prompt())
                self.assertNotIn("<available_skills>", self.prompt())
                self.assertNotIn("view_skill", shared)
                self.assertNotIn("lite_skill_loader", self.metadata)
                self.assertFalse(self.metadata["lite_view_skill_available"])
                self.assertFalse(any(s["function"]["name"] == "view_skill" for s in self.body["tools"]))
                self.assertEqual(self.metadata["skill_ids"], [])
                self.assertEqual(self.metadata["lite_target_skill_ids"], [])
                self.assertIs(self.body["metadata"]["tools"], shared)
                self.assertIs(self.metadata["lite_child_messages"], history)
                self.assertIs(request_state["tools"], shared)
                self.assertIs(request_state["lite_child_messages"], history)
                self.assertNotIn("lite_skill_loader", request_state)
                self.assertEqual(request_state["skill_ids"], [])
                self.assertEqual(request_state["platform"], "keep request")
                self.assertEqual(outer_metadata, {"skill_ids": ["alpha"], "platform": "keep outer"})
                self.skills.assert_not_awaited()
                self.assertTrue(all(args.args[1]["__skill_ids__"] == [] for args in self.builtins.call_args_list))

    async def test_database_replacement_updates_context_schema_and_loader_allowlist(self):
        self.runtime_model["info"]["meta"]["skillIds"] = ["alpha"]

        async def dispatch_with_stale_body_selection(**kwargs):
            kwargs["form_data"]["skill_ids"] = ["alpha"]
            return await self.process_filters(**kwargs)

        self.dispatch_filters.side_effect = dispatch_with_stale_body_selection
        self.metadata["lite_orchestrator_skill_ids"] = ["alpha"]
        for mode in ("native", "legacy"):
            with self.subTest(mode=mode):
                self.metadata["params"] = {"function_calling": mode}
                self.select(["alpha"])
                await self.prepare()
                previous_loader = self.metadata["tools"].get("view_skill")
                self.select([" BETA ", "beta", ""])
                self.skills.reset_mock()
                self.view_skill.reset_mock()
                self.builtins.return_value["view_skill"]["spec"] = {
                    "name": "view_skill", "description": "Current Skill loader",
                }

                await self.prepare()

                self.assertEqual(self.runtime_model["info"]["meta"]["skillIds"], ["alpha"])
                self.assertNotIn("alpha", self.prompt())
                self.assertIn("beta", self.prompt())
                self.skills.assert_awaited_once_with("beta")
                self.assertEqual(self.metadata["skill_ids"], ["beta"])
                schemas = [s["function"] for s in self.body["tools"] if s["function"]["name"] == "view_skill"]
                if mode == "native":
                    loader = self.metadata["tools"]["view_skill"]
                    self.assertIsNot(loader, previous_loader)
                    self.assertEqual(schemas, [{"name": "view_skill", "description": "Current Skill loader"}])
                    self.assertEqual(json.loads(await loader["callable"](id=" ALPHA ")), {
                        "error": "Skill is not available in the current model context",
                    })
                    self.view_skill.assert_not_awaited()
                    self.assertEqual(await loader["callable"](id=" BETA "), "builtin checked permissions")
                    self.view_skill.assert_awaited_once_with(id="beta")
                    params = self.builtins.call_args.args[1]
                    self.assertEqual(params["__skill_ids__"], ["beta"])
                    self.assertEqual(params["__user__"], {"id": "user"})
                else:
                    self.assertIn("Full beta instructions", self.prompt())
                    self.assertEqual(schemas, [])
                    self.assertNotIn("view_skill", self.metadata["tools"])
                    self.assertNotIn("lite_skill_loader", self.metadata)

    async def test_fresh_attachment_errors_do_not_lookup_detached_runtime_skills(self):
        self.runtime_model["info"]["meta"]["skillIds"] = ["alpha"]
        await self.prepare()
        self.records.pop("alpha")
        self.select([" BETA ", "beta"])
        for mode, inactive in itertools.product(("native", "legacy"), (False, True)):
            with self.subTest(mode=mode, inactive=inactive):
                self.metadata["params"] = {"function_calling": mode}
                if inactive:
                    self.records["beta"] = types.SimpleNamespace(is_active=False)
                else:
                    self.records.pop("beta", None)
                self.skills.reset_mock()
                self.completion.reset_mock()
                with self.assertRaisesRegex(ValueError, "Attached model Skills are unavailable: beta$"):
                    await self.prepare()
                self.skills.assert_awaited_once_with("beta")
                self.completion.assert_not_awaited()
                self.assertEqual(self.metadata["skill_ids"], ["alpha"])

    async def test_failure_after_fresh_skill_install_restores_local_state_and_keeps_mcp_resources(self):
        self.runtime_model["info"]["meta"]["skillIds"] = ["alpha"]
        request_state = {"platform": "keep request"}
        self.request.state.metadata = request_state
        await self.prepare()
        shared = self.metadata["tools"]
        old_tools = dict(shared)
        history = self.metadata["lite_child_messages"]
        old_history = list(history)
        state = dict(self.metadata)
        request_snapshot = dict(request_state)
        body = self.body
        outer_metadata = {"skill_ids": ["alpha"], "platform": "keep outer"}
        body["metadata"] = outer_metadata
        self.models["agent-a"].meta.update(skillIds=["beta"], toolIds=["Toolkit", "server:mcp:documents"])
        client = types.SimpleNamespace(call_tool=AsyncMock(return_value="MCP result"))
        self.connector.return_value = (client, [{"name": "fetch"}])

        async def fail_after_filters(**kwargs):
            filtered, _ = await self.process_filters(**kwargs)
            filtered["metadata"]["lite_child_messages"].append({"role": "user", "content": "partial"})
            raise RuntimeError("dispatch failed after Skill refresh")

        self.dispatch_filters.side_effect = fail_after_filters
        self.completion.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "dispatch failed after Skill refresh"):
            await self.prepare()
        self.completion.assert_not_awaited()
        self.assertIs(self.metadata["tools"], shared)
        self.assertEqual(shared, old_tools)
        for name, tool in old_tools.items():
            self.assertIs(shared[name], tool)
        self.assertIs(self.metadata["lite_child_messages"], history)
        self.assertEqual(history, old_history)
        self.assertEqual(await old_tools["lookup"]["callable"](), old_history)
        for key, value in state.items():
            self.assertEqual(self.metadata[key], value)
        self.assertEqual(request_state, request_snapshot)
        self.assertIs(request_state["tools"], shared)
        self.assertIs(request_state["lite_child_messages"], history)
        self.assertNotIn("lite_subagent_filter_run", request_state)
        self.assertEqual(request_state["platform"], "keep request")
        self.assertIs(self.body, body)
        self.assertEqual(outer_metadata, {"skill_ids": ["alpha"], "platform": "keep outer"})
        self.connector.assert_awaited_once()
        self.assertIs(self.metadata["mcp_clients"]["documents"], client)
        self.assertNotIn("beta", "\n".join(m["content"] for m in history if m["role"] == "system"))
        self.assertIn("error", json.loads(await old_tools["view_skill"]["callable"](id="beta")))

    async def test_child_skill_failure_restores_owned_loader_and_attachment_identifiers(self):
        await self.prepare()
        shared = self.metadata["tools"]
        old_tools = dict(shared)
        history = self.metadata["lite_child_messages"]
        old_history = list(history)
        self.models["agent-a"].meta.update(toolIds=["new-toolkit"], skillIds=["beta"])
        self.records.pop("beta")
        state = dict(self.metadata)
        self.completion.reset_mock()
        with self.assertRaisesRegex(ValueError, "Skills are unavailable: beta"):
            await self.prepare()
        self.completion.assert_not_awaited()
        self.assertEqual(self.metadata, state)
        self.assertIs(self.metadata["tools"], shared)
        for name, tool in old_tools.items():
            self.assertIs(shared[name], tool)
        self.assertEqual(self.metadata["tool_ids"], ["toolkit"])
        self.assertEqual(self.metadata["skill_ids"], ["alpha"])
        self.assertIs(self.metadata["lite_child_messages"], history)
        self.assertEqual(await old_tools["lookup"]["callable"](), old_history)
        self.records["beta"] = types.SimpleNamespace(is_active=True, name="beta", description="current", content="current beta")
        await self.prepare()
        self.assertEqual(self.metadata["skill_ids"], ["beta"])
        self.assertIn("<id>beta</id>", self.prompt())


class StandaloneSkillTests(SkillBehavior, PipeTestCase):
    path = "standalone"

    async def test_standalone_selection_keeps_body_runtime_and_orchestrator_sources(self):
        self.records["gamma"] = types.SimpleNamespace(
            is_active=True, name="Gamma", description="Gamma description", content="Full gamma instructions",
        )
        self.body["skill_ids"] = [" BETA "]
        self.metadata.update(lite_orchestrator_skill_ids=["gamma"], lite_target_skill_ids=["missing-child-skill"])
        for mode in ("native", "legacy"):
            with self.subTest(mode=mode):
                self.metadata["params"] = {"function_calling": mode}
                self.skills.reset_mock()
                await self.prepare()
                self.assertEqual([args.args[0] for args in self.skills.call_args_list], ["beta", "alpha", "gamma"])
                self.assertNotIn("missing-child-skill", self.prompt())
                if mode == "native":
                    self.assertLess(self.prompt().index("<id>beta</id>"), self.prompt().index("<id>alpha</id>"))
                    self.assertLess(self.prompt().index("<id>alpha</id>"), self.prompt().index("<id>gamma</id>"))
                    loader = self.metadata["tools"]["view_skill"]["callable"]
                    self.assertEqual(await loader(id="gamma"), "builtin checked permissions")
                else:
                    for skill_id in ("alpha", "beta", "gamma"):
                        self.assertIn("Full " + skill_id + " instructions", self.prompt())

    async def test_standalone_filter_uses_body_metadata_and_mirrors_managed_fields_only(self):
        body_metadata = self.metadata
        state = {"session_id": "platform session", "platform": "retained", "lite_active_agent_id": "stale"}
        self.request.state.metadata = state
        await self.prepare()
        self.assertIs(self.body["metadata"], body_metadata)
        self.assertIs(state["tools"], body_metadata["tools"])
        self.assertTrue(state["skill_context_applied"])
        self.assertEqual(state["lite_skill_loader"], body_metadata["lite_skill_loader"])
        self.assertNotIn("lite_active_agent_id", state)
        self.assertEqual(state["session_id"], "platform session")
        self.assertEqual(state["platform"], "retained")
