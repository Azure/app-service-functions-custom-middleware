"""Callback chains stay request-scoped across ASGI tasks and pooled worker threads."""

import asyncio
import inspect
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app_service_extensions import configure, hooks

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi import FastAPI, Request  # noqa: E402

CHAINS = [f"chain-{n}" for n in range(24)]


@pytest.fixture(autouse=True)
def isolate():
    with hooks._use_hook_invocation(None):
        yield


def build_app():
    app = FastAPI()
    configure(app)

    @app.get("/chain")
    async def chain(req: Request):
        first = hooks.hook_invocation()
        await asyncio.sleep(0.001)
        return {"first": first, "second": hooks.hook_invocation()}

    return app


async def call_all(app, chains):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        async def call(chain):
            headers = {"X-MS-Hook-Invocation": chain} if chain else {}
            return chain, (await client.get("/chain", headers=headers)).json()

        return await asyncio.gather(*(call(chain) for chain in chains))


class TestAsgiConcurrency:
    def test_interleaved_requests_keep_their_own_chain(self):
        for chain, seen in asyncio.run(call_all(build_app(), CHAINS)):
            assert seen == {"first": chain, "second": chain}

    def test_a_request_without_a_chain_does_not_inherit_one(self):
        mixed = [chain if index % 2 else None for index, chain in enumerate(CHAINS)]
        for chain, seen in asyncio.run(call_all(build_app(), mixed)):
            assert seen == {"first": chain, "second": chain}

    def test_the_chain_does_not_escape_to_the_caller(self):
        from fastapi.testclient import TestClient

        with TestClient(build_app()) as client:
            answer = client.get("/chain", headers={"X-MS-Hook-Invocation": "chain-a"}).json()
        assert answer["first"] == "chain-a"
        assert hooks.hook_invocation() is None


class FakeRequest:
    def __init__(self, chain=None):
        self.headers = {"X-MS-Hook-Invocation": chain} if chain else {}


def function_app():
    app = pytest.importorskip("azure.functions").FunctionApp()
    configure(app)
    return app


def user_function(app):
    [function] = app.get_functions()
    return function.get_user_function()


class TestThreadConcurrency:
    def build(self):
        app = function_app()

        @app.route(route="chain")
        def chain(req):
            first = hooks.hook_invocation()
            time.sleep(0.001)
            return first, hooks.hook_invocation()

        return app

    def test_pooled_threads_do_not_share_chains(self):
        handler = user_function(self.build())

        def call(chain):
            return chain, handler(FakeRequest(chain))

        with ThreadPoolExecutor(max_workers=4) as pool:
            for chain, seen in pool.map(call, CHAINS * 4):
                assert seen == (chain, chain)

    def test_a_reused_thread_does_not_leak_the_last_chain(self):
        handler = user_function(self.build())
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(handler, FakeRequest("chain-a")).result()
            assert pool.submit(hooks.hook_invocation).result() is None
            assert pool.submit(hooks._current.get).result() is None
            assert pool.submit(handler, FakeRequest()).result() == (None, None)

    def test_a_sync_handler_stays_sync(self):
        assert not inspect.iscoroutinefunction(user_function(self.build()))

    def test_an_async_handler_stays_async(self):
        app = function_app()

        @app.route(route="chain")
        async def chain(req):
            return hooks.hook_invocation()

        handler = user_function(app)
        assert inspect.iscoroutinefunction(handler)
        assert asyncio.run(handler(FakeRequest("chain-a"))) == "chain-a"
