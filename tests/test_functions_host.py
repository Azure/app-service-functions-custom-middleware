"""Hook state across Functions invocations, decorator stacks and pooled worker threads."""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from app_service_extensions import configure, current_request, hooks

func = pytest.importorskip("azure.functions")


@pytest.fixture(autouse=True)
def isolate():
    reset = hooks._current.set(None)
    with hooks._use_hook_invocation(None):
        yield
    hooks._current.reset(reset)


def function_app():
    app = func.FunctionApp()
    configure(app)
    return app


def indexed(app):
    """The app's functions as Azure Functions loads them, by name."""
    return {function.get_function_name(): function for function in app.get_functions()}


class FakeHttpRequest:
    def __init__(self, chain=None, hook=False):
        self.headers = {"X-MS-Hook-Invocation": chain} if chain else {}
        if hook:
            self.headers["X-MS-Hook-Name"] = "test-hook"
        self.method = "POST"
        self.url = "http://localhost/api/x"

    def get_body(self):
        return b"{}"


class TestDecoratorStacks:
    def test_a_trigger_above_function_name_registers_one_function(self):
        app = function_app()

        @app.route(route="named")
        @app.function_name(name="named_hook")
        def handler(req):
            return hooks.hook_invocation()

        functions = indexed(app)
        assert list(functions) == ["named_hook"]
        user_function = functions["named_hook"].get_user_function()
        assert user_function(req=FakeHttpRequest("chain")) == "chain"

    def test_output_bindings_below_a_trigger_are_kept(self):
        # The order Microsoft's docs use for an HTTP function with a queue output.
        app = function_app()

        @app.route(route="queued")
        @app.queue_output(arg_name="msg", queue_name="out", connection="AzureWebJobsStorage")
        def queued(req, msg):
            return hooks.hook_invocation()

        functions = indexed(app)
        assert list(functions) == ["queued"]
        bindings = {binding["name"] for binding in functions["queued"].get_dict_repr()["bindings"]}
        assert {"req", "msg"} <= bindings
        user_function = functions["queued"].get_user_function()
        assert user_function(req=FakeHttpRequest("chain"), msg=object()) == "chain"

    def test_a_trigger_below_function_name_still_works(self):
        app = function_app()

        @app.function_name(name="outer_name")
        @app.route(route="outer")
        async def handler(req):
            return hooks.hook_invocation()

        user_function = indexed(app)["outer_name"].get_user_function()
        assert inspect.iscoroutinefunction(user_function)


class TestTheCallingConvention:
    @pytest.mark.parametrize("parameter", ["req", "request"])
    def test_a_request_parameter_is_found_by_value(self, parameter):
        app = function_app()

        @app.route(route="hook")
        def handler(**kwargs):
            return hooks.hook_invocation()

        assert handler(**{parameter: FakeHttpRequest("chain")}) == "chain"

    def test_extra_bindings_are_passed_through(self):
        app = function_app()

        @app.route(route="hook")
        def handler(request, msg):
            return hooks.hook_invocation(), msg

        assert handler(request=FakeHttpRequest("chain"), msg="hello") == ("chain", "hello")

    @pytest.mark.asyncio
    async def test_an_async_handler_is_prepared_too(self):
        app = function_app()

        @app.route(route="hook")
        async def handler(request):
            return hooks.hook_invocation()

        assert await handler(request=FakeHttpRequest("chain")) == "chain"

    def test_a_sync_handler_stays_sync(self):
        app = function_app()

        @app.route(route="hook")
        def handler(req):
            return "fine"

        assert not inspect.iscoroutinefunction(indexed(app)["handler"].get_user_function())


class TestStatePerInvocation:
    @pytest.mark.parametrize("kind", ["timer_trigger", "queue_trigger"])
    def test_non_http_triggers_clear_the_previous_hook_state(self, kind):
        app = function_app()

        @app.route(route="hook")
        def http_handler(req):
            return hooks.hook_invocation()

        trigger = (
            app.timer_trigger(schedule="0 */5 * * * *", arg_name="binding")
            if kind == "timer_trigger"
            else app.queue_trigger(
                arg_name="binding", queue_name="in", connection="AzureWebJobsStorage"
            )
        )

        @trigger
        def background(binding):
            assert hooks._current.get() is None
            return hooks.hook_invocation(), binding

        assert http_handler(req=FakeHttpRequest("chain", hook=True)) == "chain"
        assert background(binding="work") == (None, "work")

    def test_a_request_without_a_chain_clears_the_last_one(self):
        app = function_app()

        @app.route(route="hook")
        def handler(req):
            return hooks.hook_invocation()

        assert handler(req=FakeHttpRequest("chain")) == "chain"
        assert handler(req=FakeHttpRequest()) is None

    @pytest.mark.parametrize("asynchronous", [False, True])
    @pytest.mark.asyncio
    async def test_failed_invocations_clear_both_the_hook_and_the_chain(self, asynchronous):
        app = function_app()

        def fail(req):
            assert current_request().request.hook == "test-hook"
            assert hooks.hook_invocation() == "chain"
            raise RuntimeError("the handler failed")

        async def fail_async(req):
            fail(req)

        handler = app.route(route="boom")(fail_async if asynchronous else fail)
        with pytest.raises(RuntimeError, match="handler failed"):
            result = handler(FakeHttpRequest("chain", hook=True))
            if asynchronous:
                await result

        assert hooks.hook_invocation() is None
        with pytest.raises(RuntimeError, match="No hook"):
            current_request()

    @pytest.mark.parametrize("asynchronous", [False, True])
    @pytest.mark.asyncio
    async def test_successful_invocations_clear_both_the_hook_and_the_chain(
        self, asynchronous, monkeypatch
    ):
        app = function_app()
        monkeypatch.setattr(hooks, "_http_v2_enabled", lambda: False)

        def ok(req):
            request = current_request()
            assert hooks.hook_invocation() == "chain"
            request.set_header("X-Reviewed", "yes")
            return request

        async def ok_async(req):
            return ok(req)

        handler = app.route(route="ok")(ok_async if asynchronous else ok)
        response = handler(FakeHttpRequest("chain", hook=True))
        if asynchronous:
            response = await response

        assert json.loads(response.get_body())["edits"] == [
            {"op": "set", "target": "header", "name": "X-Reviewed", "value": "yes"}
        ]
        assert hooks.hook_invocation() is None
        with pytest.raises(hooks.NoHookError):
            current_request()

    @pytest.mark.asyncio
    async def test_cancelled_preparation_clears_both_the_hook_and_the_chain(self):
        app = function_app()

        class CancelledRequest(FakeHttpRequest):
            async def body(self):
                raise asyncio.CancelledError

        @app.route(route="cancelled")
        async def handler(req):
            pytest.fail("A cancelled request must not reach the handler")

        hooks.hold_request(hooks.from_functions(FakeHttpRequest("chain", hook=True)))
        with pytest.raises(asyncio.CancelledError):
            await handler(CancelledRequest("chain", hook=True))

        assert hooks.hook_invocation() is None
        with pytest.raises(hooks.NoHookError):
            current_request()
