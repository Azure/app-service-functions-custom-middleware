"""Tests for library configuration and request-scoped callback chains."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from app_service_extensions import auth, configure, create_function_app, hooks


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(auth, "_base_url", None)
    monkeypatch.setattr(auth, "_presenter", None)
    monkeypatch.setattr(auth, "_timeout", auth._DEFAULT_TIMEOUT)
    with hooks._use_hook_invocation(None):
        yield


def function_app():
    return pytest.importorskip("azure.functions").FunctionApp()


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}


class TestSettings:
    def test_only_application_and_hook_settings_are_supported(self):
        assert set(inspect.signature(configure).parameters) == {
            "app", "base_url", "presenter", "timeout",
        }
        assert set(inspect.signature(create_function_app).parameters) == {"app_root"}

    def test_runtime_selection_parameter_is_not_supported(self):
        with pytest.raises(TypeError, match="unexpected keyword argument 'hosted_skills'"):
            configure(hosted_skills=False)

    def test_base_url(self):
        configure(base_url="https://webapp.example/")
        assert auth.app_base_url() == "https://webapp.example"

    def test_presenter(self):
        configure(presenter=lambda: "MI")
        assert auth.presenter_token() == "MI"

    def test_timeout(self):
        configure(timeout=3)
        assert auth._timeout == 3

    def test_app_is_optional(self):
        configure()


class TestRuntimeDetection:
    @pytest.mark.parametrize("available", [False, True])
    def test_runtime_is_enabled_only_when_available(self, monkeypatch, available):
        from app_service_extensions import azure_functions_agents_integration

        enabled = []
        monkeypatch.setattr(azure_functions_agents_integration, "is_available", lambda: available)
        monkeypatch.setattr(azure_functions_agents_integration, "enable", lambda: enabled.append(True))

        configure()

        assert enabled == ([True] if available else [])


class TestFunctionsApp:
    @pytest.mark.parametrize("header", ["X-MS-Hook-Invocation", "x-ms-hook-invocation"])
    def test_the_callback_chain_is_available_before_the_handler_runs(self, header):
        app = function_app()
        configure(app)

        @app.route(route="hook")
        def handler(req):
            return hooks.hook_invocation()

        assert handler(FakeRequest({header: "signed-chain"})) == "signed-chain"
        assert handler(FakeRequest()) is None

    def test_async_handlers_work(self):
        app = function_app()
        configure(app)

        @app.route(route="hook")
        async def handler(req):
            return hooks.hook_invocation()

        request = FakeRequest({"X-MS-Hook-Invocation": "signed-chain"})
        assert asyncio.run(handler(request)) == "signed-chain"

    def test_the_handler_keeps_its_name(self):
        app = function_app()
        configure(app)

        @app.route(route="hook")
        def my_handler(req):
            return None

        [function] = app.get_functions()
        assert function.get_function_name() == "my_handler"
        assert function.get_user_function().__name__ == "my_handler"

    def test_configuring_twice_does_not_double_wrap(self):
        app = function_app()
        configure(app)
        wrapped_once = app._validate_type
        configure(app)
        assert app._validate_type is wrapped_once

    def test_an_app_without_the_functions_decorator_api_is_rejected(self):
        class RouteOnly:
            def route(self, *args, **kwargs):
                return lambda fn: fn

        with pytest.raises(TypeError, match="don't know how to listen"):
            configure(RouteOnly())


class TestAsgiApp:
    class App:
        def __init__(self):
            self.middleware = []
            self.seen = None

        def add_middleware(self, cls):
            self.middleware.append(cls)

        async def __call__(self, scope, receive, send):
            self.seen = hooks.hook_invocation()

    def run(self, chain=None, kind="http"):
        app = self.App()
        configure(app)
        headers = [(b"x-ms-hook-invocation", chain.encode())] if chain else []
        asyncio.run(app.middleware[0](app)({"type": kind, "headers": headers}, None, None))
        return app.seen

    def test_middleware_is_installed(self):
        app = self.App()
        configure(app)
        assert app.middleware == [hooks._Middleware]

    def test_the_callback_chain_is_read_from_the_scope(self):
        assert self.run("signed-chain") == "signed-chain"

    def test_no_header_clears_the_previous_chain(self):
        with hooks._use_hook_invocation("previous"):
            assert self.run() is None

    def test_non_http_scopes_are_left_alone(self):
        with hooks._use_hook_invocation("kept"):
            assert self.run("ignored", "lifespan") == "kept"


def test_an_unrecognized_app_is_rejected():
    with pytest.raises(TypeError, match="don't know how to listen"):
        configure(object())
