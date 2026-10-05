"""Tool context through Pipe.pipe, including OWUI's native callable refresh."""

import copy
import inspect
from functools import partial, update_wrapper
import types
from typing import get_type_hints

from test_handoff_history import PipeTestCase, assistant, call, grouped_history, result


def owui_callable(function, extra_params):
    """Local stand-in for OWUI 0.11.1's Tool binding convention.

    The external wrapper freezes declared injections, hides them from its
    signature, coerces model arguments, and records the original function so
    native execution can rebind injections. This is independent of Router.
    """
    signature = inspect.signature(function)
    injections = {key: value for key, value in extra_params.items() if key in signature.parameters}
    bound = partial(function, **injections)
    hints = get_type_hints(function)

    async def wrapped(*args, **kwargs):
        for key, value in kwargs.items():
            if hints.get(key) is int and isinstance(value, str):
                kwargs[key] = int(value)
            elif hints.get(key) is str and isinstance(value, (int, float)):
                kwargs[key] = str(value)
        output = bound(*args, **kwargs)
        return await output if inspect.iscoroutinefunction(function) else output

    update_wrapper(wrapped, function)
    wrapped.__signature__ = signature.replace(parameters=[
        parameter for name, parameter in signature.parameters.items() if name not in injections
    ])
    wrapped.__function__ = function
    wrapped.__extra_params__ = injections
    return wrapped


def owui_refresh(function, extra_params):
    """Native OWUI merges new injections over the initially captured values."""
    original = getattr(function, "__function__", None)
    captured = getattr(function, "__extra_params__", None)
    if original is not None and captured is not None:
        return owui_callable(original, {**captured, **extra_params})
    return function


class CapabilityContextTests(PipeTestCase):
    def enable_orchestrator_tools(self, *, skills=False):
        self.metadata["lite_base_tool_ids"] = ["toolkit"]
        if skills:
            self.metadata.update(
                lite_orchestrator_skill_ids=["route-a"], session_id="session",
                params={"function_calling": "native"},
            )

    async def load_owui_tools(self, request, ids, owner, extra_params):
        async def read_async(amount: int, __messages__: list, __files__: list):
            return "async", amount, copy.deepcopy(__messages__), copy.deepcopy(__files__)

        def read_sync(amount: int, __messages__: list, __files__: list):
            return "sync", amount, copy.deepcopy(__messages__), copy.deepcopy(__files__)

        def echo(amount: int):
            return amount

        self.loaded_tools = {
            name: {
                "tool_id": ids[0],
                "spec": {"name": name, "parameters": {"properties": {"amount": {"type": "integer"}}}},
                "callable": owui_callable(function, extra_params),
            }
            for name, function in (("lookup", read_async), ("sync_lookup", read_sync), ("echo", echo))
        }
        return self.loaded_tools

    async def assert_native_context(self, expected, outer_messages):
        for name, kind in (("lookup", "async"), ("sync_lookup", "sync")):
            with self.subTest(tool=name):
                saved = self.metadata["tools"][name]["callable"]
                native = owui_refresh(saved, {"__messages__": outer_messages, "__files__": ["current file"]})
                self.assertEqual(await native(amount="7"), (kind, 7, expected, ["current file"]))
                self.assertEqual(inspect.signature(native), inspect.signature(self.loaded_tools[name]["callable"]))
                self.assertEqual(inspect.signature(saved), inspect.signature(native))
                self.assertEqual(self.metadata["tools"][name]["spec"], self.loaded_tools[name]["spec"])
        self.assertIs(self.metadata["tools"]["echo"], self.loaded_tools["echo"])
        self.assertEqual(await owui_refresh(self.metadata["tools"]["echo"]["callable"], {
            "__messages__": outer_messages, "__files__": ["current file"],
        })(amount="7"), 7)

    async def test_orchestrator_first_preparation_and_continuation_publish_final_history(self):
        self.enable_orchestrator_tools(skills=True)
        request_metadata = {"platform": "keep"}
        self.request.state.metadata = request_metadata
        incoming = [{"role": "user", "content": "Question"}]
        original = copy.deepcopy(incoming)
        shared = self.metadata["tools"]
        await self.invoke(incoming)
        lookup = shared["lookup"]["callable"]
        history = self.metadata["lite_base_messages"]
        self.assertEqual(await lookup(), self.routed["messages"])
        self.assertIn("Lite orchestrator Skill context", history[0]["content"])
        self.assertEqual(incoming, original)
        self.assertIsNot(history, self.routed["messages"])

        await self.invoke(incoming + [assistant(call("lookup-1", "lookup")), result("lookup-1", "fresh result")])
        self.loader.assert_awaited_once()
        self.assertIs(shared["lookup"]["callable"], lookup)
        self.assertIs(self.metadata["lite_base_messages"], history)
        self.assertEqual(await lookup(), self.routed["messages"])
        self.assertEqual([m["content"] for m in history if m["role"] == "tool"], ["fresh result"])
        self.assertIs(request_metadata["lite_base_messages"], history)
        self.assertIs(request_metadata["tools"], shared)
        self.assertEqual(request_metadata["platform"], "keep")

    async def test_native_orchestrator_refresh_keeps_prepared_history_and_updates_files(self):
        self.enable_orchestrator_tools(skills=True)
        self.loader.side_effect = self.load_owui_tools
        self.metadata["files"] = ["initial file"]
        outer = [{"role": "user", "content": "Question"}]
        await self.invoke(outer)
        lookup = self.metadata["tools"]["lookup"]["callable"]
        self.assertEqual((await lookup(amount="3"))[1:], (3, self.routed["messages"], ["initial file"]))
        await self.assert_native_context(self.routed["messages"], outer)

        await self.invoke(outer + [assistant(call("lookup-1", "lookup")), result("lookup-1", "fresh result")])
        self.loader.assert_awaited_once()
        self.assertIs(self.metadata["tools"]["lookup"]["callable"], lookup)
        # OWUI's native loop still injects the first outer snapshot here.
        await self.assert_native_context(self.routed["messages"], outer)

    async def test_native_child_refresh_sees_final_filters_and_reused_history(self):
        self.loader.side_effect = self.load_owui_tools
        self.request.state.metadata = {"platform": "keep"}

        async def replace_body(body):
            return {**body, "messages": [*body["messages"], {"role": "system", "content": "Final inlet context"}]}

        self.filters.append(types.SimpleNamespace(inlet=replace_body))
        outer = grouped_history()[:4]
        await self.invoke(outer)
        lookup = self.metadata["tools"]["lookup"]["callable"]
        await self.assert_native_context(self.routed["messages"], outer)
        self.assertEqual(self.routed["messages"][-1]["content"], "Final inlet context")
        await self.invoke(grouped_history())
        self.loader.assert_awaited_once()
        self.assertIs(self.metadata["tools"]["lookup"]["callable"], lookup)
        await self.assert_native_context(self.routed["messages"], outer)
        self.assertIs(self.request.state.metadata["lite_child_messages"], self.metadata["lite_child_messages"])

    async def test_preparation_exposes_previous_snapshot_until_success(self):
        self.enable_orchestrator_tools(skills=True)
        observed = []

        async def inspect_preparation(request, extra_params, **kwargs):
            observed.append((copy.deepcopy(extra_params["__messages__"]), await self.metadata["tools"]["lookup"]["callable"]()))
            return {}

        self.builtins.side_effect = inspect_preparation
        incoming = [{"role": "user", "content": "Question"}]
        await self.invoke(incoming)
        previous = copy.deepcopy(self.routed["messages"])
        await self.invoke(incoming + [assistant(call("lookup-1", "lookup")), result("lookup-1", "fresh result")])
        self.assertEqual(observed, [([], []), (previous, previous)])
        self.assertEqual(await self.metadata["tools"]["lookup"]["callable"](), self.routed["messages"])

    async def test_child_history_is_published_before_status_and_completion(self):
        observed = []

        async def status(event):
            self.completion.assert_not_awaited()
            observed.append(await self.metadata["tools"]["lookup"]["callable"]())
            self.assertEqual(observed[-1], self.metadata["lite_child_messages"])

        self.events.side_effect = status
        await self.invoke(grouped_history())
        self.assertEqual(observed, [self.routed["messages"]])

    async def test_failed_orchestrator_continuation_restores_history_and_distinct_metadata(self):
        self.enable_orchestrator_tools(skills=True)
        self.request.state.metadata = {"platform": "keep"}
        incoming = [{"role": "user", "content": "Question"}]
        await self.invoke(incoming)
        shared = self.metadata["tools"]
        lookup = shared["lookup"]["callable"]
        history = self.metadata["lite_base_messages"]
        previous = copy.deepcopy(history)
        cache = self.metadata["lite_base_tool_runtime"]
        state = dict(self.metadata)
        self.completion.reset_mock()

        async def fail(request, extra_params, **kwargs):
            self.assertEqual(await lookup(), previous)
            extra_params["__messages__"].append({"role": "user", "content": "partial"})
            raise RuntimeError("Skill loader failed")

        self.builtins.side_effect = fail
        continuation = incoming + [assistant(call("lookup-1", "lookup")), result("lookup-1", "fresh result")]
        with self.assertRaisesRegex(RuntimeError, "Skill loader failed"):
            await self.invoke(continuation)
        self.completion.assert_not_awaited()
        self.assertEqual(self.metadata, state)
        self.assertIs(self.metadata["tools"], shared)
        self.assertIs(shared["lookup"]["callable"], lookup)
        self.assertIs(self.metadata["lite_base_tool_runtime"], cache)
        self.assertIs(self.metadata["lite_base_messages"], history)
        self.assertEqual(await lookup(), previous)
        self.assertIs(self.request.state.metadata["lite_base_messages"], history)
        self.assertIs(self.request.state.metadata["tools"], shared)
        self.assertEqual(self.request.state.metadata["platform"], "keep")
        self.builtins.side_effect = None
        await self.invoke(continuation)
        self.loader.assert_awaited_once()
        self.assertEqual(await lookup(), self.routed["messages"])

    async def test_failed_orchestrator_rebuild_restores_previous_callable_and_history(self):
        self.enable_orchestrator_tools(skills=True)
        await self.invoke([{"role": "user", "content": "Question"}])
        lookup = self.metadata["tools"]["lookup"]["callable"]
        history = self.metadata["lite_base_messages"]
        previous = copy.deepcopy(history)
        cache = self.metadata["lite_base_tool_runtime"]
        self.metadata["lite_router_model_id"] = "other-router"
        self.builtins.side_effect = RuntimeError("Skill loader failed")
        self.completion.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "Skill loader failed"):
            await self.invoke([{"role": "user", "content": "Question"}])
        self.completion.assert_not_awaited()
        self.assertIs(self.metadata["tools"]["lookup"]["callable"], lookup)
        self.assertIs(self.metadata["lite_base_tool_runtime"], cache)
        self.assertEqual(await lookup(), previous)
        self.builtins.side_effect = None
        await self.invoke([{"role": "user", "content": "Question"}])
        self.assertEqual(self.loader.await_count, 3)
        self.assertIsNot(self.metadata["tools"]["lookup"]["callable"], lookup)
        self.assertIs(self.metadata["lite_base_messages"], history)
        self.assertEqual(await self.metadata["tools"]["lookup"]["callable"](), self.routed["messages"])

    async def test_normalized_orchestrator_attachments_reuse_then_rebuild_with_stable_history(self):
        self.metadata.update(lite_base_tool_ids=[" toolkit ", "toolkit", ""], lite_orchestrator_skill_ids=[" Route-A ", "route-a"])
        incoming = [{"role": "user", "content": "Question"}]
        await self.invoke(incoming)
        lookup = self.metadata["tools"]["lookup"]["callable"]
        history = self.metadata["lite_base_messages"]
        self.metadata.update(lite_base_tool_ids=["toolkit"], lite_orchestrator_skill_ids=["route-a"])
        await self.invoke(incoming)
        self.loader.assert_awaited_once()
        self.assertIs(self.metadata["tools"]["lookup"]["callable"], lookup)
        self.metadata["lite_orchestrator_skill_ids"] = ["route-b"]
        await self.invoke(incoming)
        self.assertEqual(self.loader.await_count, 2)
        self.assertIsNot(self.metadata["tools"]["lookup"]["callable"], lookup)
        self.assertIs(self.metadata["lite_base_messages"], history)
        self.metadata["lite_base_tool_ids"] = ["other-toolkit"]
        await self.invoke(incoming)
        self.assertEqual(self.loader.await_count, 3)
        self.assertIs(self.metadata["lite_base_messages"], history)
        self.assertEqual(await self.metadata["tools"]["lookup"]["callable"](), self.routed["messages"])

    async def test_orchestrator_provider_failure_keeps_new_native_tool_context(self):
        self.enable_orchestrator_tools()
        self.loader.side_effect = self.load_owui_tools
        outer = [{"role": "user", "content": "Question"}]
        await self.invoke(outer)
        lookup = self.metadata["tools"]["lookup"]["callable"]
        history = self.metadata["lite_base_messages"]
        cache = self.metadata["lite_base_tool_runtime"]
        self.completion.side_effect = RuntimeError("provider failed")
        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            await self.invoke(outer + [assistant(call("lookup-1", "lookup")), result("lookup-1", "fresh result")])
        self.assertIs(self.metadata["lite_base_tool_runtime"], cache)
        self.assertIs(self.metadata["tools"]["lookup"]["callable"], lookup)
        self.assertIs(self.metadata["lite_base_messages"], history)
        await self.assert_native_context(self.routed["messages"], outer)

    async def test_branch_histories_are_independent_and_new_request_resets_both(self):
        self.enable_orchestrator_tools()
        await self.invoke([{"role": "user", "content": "Question"}])
        base_history = self.metadata["lite_base_messages"]
        base_lookup = self.metadata["tools"]["lookup"]["callable"]
        base_snapshot = copy.deepcopy(base_history)
        await self.invoke(grouped_history())
        child_history = self.metadata["lite_child_messages"]
        self.assertIsNot(base_history, child_history)
        self.assertEqual(await base_lookup(), base_snapshot)
        self.assertEqual(await self.metadata["tools"]["lookup"]["callable"](), self.routed["messages"])
        shared = self.metadata["tools"]
        self.request.state.metadata = {"platform": "keep"}
        new_body = {"model": "router", "metadata": self.metadata, "messages": [
            *grouped_history(), {"role": "assistant", "content": "Done"}, {"role": "user", "content": "New request"},
        ]}
        registry = self.registry_filter()
        registry.valves.base_tool_ids = ["toolkit"]
        self.begin_request(metadata=self.request.state.metadata)
        await self.router_inlets(new_body, registry=registry)
        for key in ("lite_base_messages", "lite_child_messages", "lite_base_tool_runtime", "lite_active_tool_runtime"):
            self.assertNotIn(key, self.metadata)
            self.assertNotIn(key, self.request.state.metadata)
        await self.invoke_body(new_body)
        self.assertEqual(self.loader.await_count, 3)
        self.assertIs(self.metadata["tools"], shared)
        self.assertIsNot(self.metadata["lite_base_messages"], base_history)
        self.assertIsNot(shared["lookup"]["callable"], base_lookup)
        self.assertNotIn("lite_child_messages", self.metadata)
        self.assertEqual(await shared["lookup"]["callable"](), self.routed["messages"])
