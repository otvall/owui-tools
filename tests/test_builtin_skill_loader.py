"""OWUI loading and native callable refresh through the shared loader interface."""

import copy
import unittest
from unittest.mock import AsyncMock

from shared.skill_preparation import BuiltinSkillLoader, SkillBuiltinInvocation
from test_capability_context import owui_callable, owui_refresh
from test_handoff_history import router


class BuiltinSkillLoaderTests(unittest.IsolatedAsyncioTestCase):
    def invocation(self, profile, **fields):
        return SkillBuiltinInvocation(
            profile=profile, request=object(), runtime_model={"id": "model"},
            metadata={"session_id": "session", "features": {"web_search": True}}, **fields,
        )

    async def test_loading_resolves_fresh_user_and_metadata_without_caching(self):
        invocation = self.invocation("standalone")
        first_tool, second_tool = {"callable": AsyncMock()}, {"callable": AsyncMock()}
        builtins = AsyncMock(side_effect=[{"view_skill": first_tool}, {"view_skill": second_tool}])
        users = [{"id": "first-user"}, {"id": "second-user"}]
        resolve_user = AsyncMock(side_effect=users)
        loader = BuiltinSkillLoader(
            invocation=invocation, get_builtin_tools=builtins, resolve_user=resolve_user,
        )
        resolve_user.assert_not_awaited()
        builtins.assert_not_awaited()
        self.assertIs((await loader.load(["alpha"]))["view_skill"], first_tool)
        invocation.metadata["features"] = {"web_search": False}
        self.assertIs((await loader.load(["beta"]))["view_skill"], second_tool)
        self.assertEqual(resolve_user.await_count, 2)
        self.assertEqual([item.args[1]["__user__"] for item in builtins.await_args_list], users)
        self.assertEqual([item.args[1]["__skill_ids__"] for item in builtins.await_args_list], [["alpha"], ["beta"]])
        self.assertEqual(builtins.call_args.kwargs["features"], {"web_search": False})

    async def test_native_refresh_preserves_router_history_and_standalone_binding(self):
        for profile in ("orchestrator", "child", "standalone"):
            with self.subTest(profile=profile):
                history = [{"role": "system", "content": "Prepared Skill context"}]
                outer = [{"role": "user", "content": "Outer request"}]
                invocation = self.invocation(
                    profile, messages=history if profile != "standalone" else None,
                    files=["initial file"] if profile != "standalone" else None,
                )
                spec = {"name": "view_skill"}

                async def view_skill(id: str, __messages__: list = None, __files__: list = None):
                    return id, copy.deepcopy(__messages__), copy.deepcopy(__files__)

                async def get_builtin_tools(request, extra_params, **options):
                    return {"view_skill": {"spec": spec, "callable": owui_callable(view_skill, extra_params)}}

                loader = BuiltinSkillLoader(
                    invocation=invocation, get_builtin_tools=get_builtin_tools,
                    resolve_user=AsyncMock(return_value={"id": "user"}),
                    bind_history=router.ModelCapabilityResolver.bind_history if profile != "standalone" else None,
                )
                tool = (await loader.load(["alpha"]))["view_skill"]
                self.assertIs(tool["spec"], spec)
                expected_history = history if profile != "standalone" else outer
                for content in ("First continuation", "Next continuation"):
                    history.append({"role": "assistant", "content": content})
                    native = owui_refresh(tool["callable"], {
                        "__messages__": outer, "__files__": ["current file"],
                    })
                    self.assertEqual(await native(id="alpha"), ("alpha", expected_history, ["current file"]))

    async def test_dependency_errors_propagate(self):
        builtins = AsyncMock(side_effect=RuntimeError("OWUI load failed"))
        resolve_user = AsyncMock(side_effect=ValueError("Owner is unavailable"))
        loader = BuiltinSkillLoader(
            invocation=self.invocation("standalone"), get_builtin_tools=builtins,
            resolve_user=resolve_user,
        )
        with self.assertRaisesRegex(ValueError, "Owner is unavailable"):
            await loader.load(["alpha"])
        builtins.assert_not_awaited()
        resolve_user.side_effect = None
        resolve_user.return_value = {"id": "user"}
        with self.assertRaisesRegex(RuntimeError, "OWUI load failed"):
            await loader.load(["alpha"])

    async def test_router_requires_history_adapter_and_standalone_rejects_it(self):
        for profile in ("orchestrator", "child"):
            for history, adapter in ((None, router.ModelCapabilityResolver.bind_history), ([], None)):
                with self.subTest(profile=profile, history=history, adapter=adapter):
                    with self.assertRaisesRegex(ValueError, "requires Tool history"):
                        BuiltinSkillLoader(
                            invocation=self.invocation(profile, messages=history),
                            get_builtin_tools=AsyncMock(), resolve_user=AsyncMock(), bind_history=adapter,
                        )
        with self.assertRaisesRegex(ValueError, "cannot bind Router history"):
            BuiltinSkillLoader(
                invocation=self.invocation("standalone"), get_builtin_tools=AsyncMock(),
                resolve_user=AsyncMock(), bind_history=router.ModelCapabilityResolver.bind_history,
            )
