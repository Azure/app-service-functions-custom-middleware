"""Tests for current_request(): the same call on every host.

The point is that a handler names no framework type and parses nothing, and that the body arrives
intact for the application behind the middleware.
"""

import asyncio
import inspect
import json

import pytest

from app_service_extensions import auth, configure, current_request, hooks

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402

FORWARD = {
    "X-MS-Hook-Name": "expense-normalizer",
    "X-MS-Hook-Mode": "intercept",
    "X-MS-Hook-Path": "/api/expenses",
    "X-MS-Hook-Invocation": "signed-chain",
    "Content-Type": "application/json",
}

#: A callback carries the chain even when it is not another hook forward.
PLAIN = {"X-MS-Hook-Invocation": "signed-chain", "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def isolate():
    with hooks._use_hook_invocation(None):
        hooks._current.set(None)
        yield
    auth._base_url = None
    auth._presenter = None


class TestAsgi:
    def build(self):
        app = FastAPI()
        configure(app)

        @app.post("/hooks/version")
        async def version():
            request = current_request()
            request.set_query("version", "2")
            return request.to_dict("moved to v2")

        return app

    async def post(self, app, path="/hooks/version", payload=None, headers=None):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.post(
                path, json=payload if payload is not None else {"amount": "16"},
                headers=headers if headers is not None else FORWARD,
            )

    def test_a_handler_names_no_framework_type(self):
        answer = asyncio.run(self.post(self.build())).json()
        assert answer["action"] == "modify"
        assert answer["edits"] == [
            {"op": "set", "target": "query", "name": "version", "value": "2"}
        ]

    def test_the_forward_is_parsed(self):
        app = FastAPI()
        configure(app)

        @app.post("/echo")
        async def echo():
            request = current_request()
            return {
                "hook": request.request.hook,
                "mode": request.request.mode,
                "path": request.path,
                "body": json.loads(request.body),
                "query": request.query,
            }

        answer = asyncio.run(self.post(app, "/echo?debug=1")).json()
        assert answer["hook"] == "expense-normalizer"
        assert answer["mode"] == "intercept"
        assert answer["path"] == "/api/expenses"
        assert answer["body"] == {"amount": "16"}
        assert answer["query"] == {"debug": "1"}

    def test_the_callback_chain_is_adopted_too(self):
        app = FastAPI()
        configure(app)

        @app.post("/who")
        async def who():
            return {"invocation": hooks.hook_invocation()}

        assert asyncio.run(self.post(app, "/who")).json()["invocation"] == "signed-chain"

    def test_the_application_still_receives_its_body(self):
        # Buffering the stream would starve the app unless what was read is put back.
        app = FastAPI()
        configure(app)

        @app.post("/passthrough")
        async def passthrough(payload: dict):
            return {"seen": payload}

        answer = asyncio.run(self.post(app, "/passthrough", payload={"a": 1})).json()
        assert answer["seen"] == {"a": 1}

    def test_ordinary_traffic_is_not_a_forward(self):
        # No X-MS-Hook-Name means nothing was intercepted, so there is no forward to edit -- and
        # the stream is left alone rather than buffered for a route that will not ask for it.
        with pytest.raises(RuntimeError, match="hook"):
            asyncio.run(self.post(self.build(), headers=PLAIN))

    def test_ordinary_traffic_still_preserves_its_callback_chain(self):
        app = FastAPI()
        configure(app)

        @app.post("/who")
        async def who(payload: dict):
            return {"invocation": hooks.hook_invocation(), "seen": payload}

        answer = asyncio.run(self.post(app, "/who", payload={"a": 1}, headers=PLAIN)).json()
        assert answer["invocation"] == "signed-chain"
        assert answer["seen"] == {"a": 1}

    def test_concurrent_requests_do_not_share_a_request(self):
        app = FastAPI()
        configure(app)

        @app.post("/echo")
        async def echo():
            request = current_request()
            await asyncio.sleep(0.005)
            return {"body": json.loads(request.body)}

        async def exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:

                async def call(n):
                    response = await client.post("/echo", json={"n": n}, headers=FORWARD)
                    return n, response.json()["body"]["n"]

                return await asyncio.gather(*(call(n) for n in range(24)))

        for sent, seen in asyncio.run(exercise()):
            assert sent == seen

    @pytest.mark.parametrize("headers", [FORWARD, PLAIN], ids=["hook", "callback"])
    @pytest.mark.parametrize("failed", [False, True])
    @pytest.mark.asyncio
    async def test_completed_calls_clear_state_in_the_calling_context(self, headers, failed):
        app = FastAPI()
        configure(app)

        @app.post("/complete")
        async def complete():
            assert hooks.hook_invocation() == "signed-chain"
            if headers is FORWARD:
                assert current_request().request.hook == "expense-normalizer"
            if failed:
                raise RuntimeError("handler failed")
            return {"complete": True}

        if failed:
            with pytest.raises(RuntimeError, match="handler failed"):
                await self.post(app, "/complete", headers=headers)
        else:
            assert (await self.post(app, "/complete", headers=headers)).json() == {
                "complete": True
            }

        assert (hooks._current.get(), hooks.hook_invocation()) == (None, None)

    @pytest.mark.parametrize("cancelled", [False, True])
    @pytest.mark.asyncio
    async def test_stream_state_lasts_through_the_response_and_clears_on_exit(self, cancelled):
        from starlette.background import BackgroundTask
        from starlette.responses import StreamingResponse

        app = FastAPI()
        configure(app)
        started = asyncio.Event()
        release = asyncio.Event()
        seen = []
        after = []

        def record(stage):
            seen.append((stage, current_request().request.hook, hooks.hook_invocation()))

        async def chunks():
            try:
                record("stream")
                yield b"first"
                started.set()
                await release.wait()
                yield b"last"
            finally:
                record("closed")

        async def background():
            record("background")

        @app.post("/stream")
        async def stream():
            return StreamingResponse(chunks(), background=BackgroundTask(background))

        async def invoke():
            try:
                return await self.post(app, "/stream")
            finally:
                after.append((hooks._current.get(), hooks.hook_invocation()))

        task = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            if cancelled:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                assert (await task).content == b"firstlast"
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        stages = ["stream", "closed"] if cancelled else ["stream", "closed", "background"]
        assert seen == [
            (stage, "expense-normalizer", "signed-chain") for stage in stages
        ]
        assert after == [(None, None)]


def function_app():
    app = pytest.importorskip("azure.functions").FunctionApp()
    configure(app)
    return app


class FakeRequest:
    def __init__(self, body=b"{}", headers=None, url="https://f.example/api/hook"):
        self.method = "POST"
        self.url = url
        self.headers = headers or dict(FORWARD)
        self._body = body

    def get_body(self):
        return self._body


class TestFunctions:
    def build(self):
        app = function_app()

        @app.route(route="version")
        def version(req):
            request = current_request()
            request.set_query("version", "2")
            return request.to_json("moved to v2")

        return version

    def test_the_same_handler_shape_works(self):
        answer = json.loads(self.build()(FakeRequest(b'{"amount":"16"}')))
        assert answer["action"] == "modify"
        assert answer["edits"][0]["value"] == "2"

    def test_the_body_is_free_here(self):
        # No buffering needed: the Functions API is synchronous and already holds the body.
        app = function_app()

        @app.route(route="echo")
        def echo(req):
            return current_request().body

        assert echo(FakeRequest(b'{"a":1}')) == '{"a":1}'

    def test_the_query_comes_from_the_url(self):
        app = function_app()

        @app.route(route="echo")
        def echo(req):
            return current_request().query

        request = FakeRequest(url="https://f.example/api/hook?tag=a&debug=1")
        assert echo(request) == {"tag": "a", "debug": "1"}

    def test_ordinary_traffic_is_not_a_forward(self):
        with pytest.raises(RuntimeError, match="hook"):
            self.build()(FakeRequest(headers=PLAIN))

    def test_ordinary_traffic_still_preserves_its_callback_chain(self):
        app = function_app()

        @app.route(route="who")
        def who(req):
            return hooks.hook_invocation()

        assert who(FakeRequest(headers=dict(PLAIN))) == "signed-chain"

    def test_a_sync_handler_stays_sync(self):
        app = function_app()

        @app.route(route="sync")
        def sync_handler(req):
            return "fine"

        assert not inspect.iscoroutinefunction(app.get_functions()[0].get_user_function())


class TestUnconfigured:
    def test_calling_it_outside_a_request_says_what_to_do(self):
        with pytest.raises(RuntimeError, match="hook"):
            current_request()
