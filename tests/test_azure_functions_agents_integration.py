"""Hosted Skills integration: hook prompts, application auth, and scoped MCP callbacks."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
import typing
from dataclasses import make_dataclass

import pytest
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from app_service_extensions import auth, hooks, azure_functions_agents_integration
from app_service_extensions.azure_functions_agents_integration import _mcp_request_scope, _prompt, _tools
from app_service_extensions.rewrite_request import EditableRequest
from app_service_extensions.tools import ALL_TOOLS, call_web_app_wrapper, set_header
from app_service_extensions.tools import request as request_tools


class FunctionTool:
    def __init__(self, name, description="", func=None):
        self.name = name
        self.description = description
        self.func = func


# Same fields as the runtime's discovery results.
ProjectTools = make_dataclass(
    "ProjectTools",
    [("user_tools", list), ("workflow_tools", list), ("failed_loads", list)],
    frozen=True,
)
ToolDiscoveryResult = make_dataclass(
    "ToolDiscoveryResult", [("tools", list), ("failed_loads", list)]
)
MCPDiscoveryResult = make_dataclass(
    "MCPDiscoveryResult", [("servers", dict), ("failed_loads", list)]
)


def make_tool(func, *, name=None, description=None):
    return FunctionTool(name or func.__name__, description or "", func)


class FakeHttpClient:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class FakeAsyncClient(FakeHttpClient):
    """Stands in for the runtime's httpx client, including its event hooks."""

    def __init__(self):
        super().__init__()
        self.event_hooks = {"request": [], "response": []}


class FakeMcpTool:
    def __init__(self, name, failure=None):
        self.name = name
        self.failure = failure
        self.functions = []
        self.closed = 0
        self._httpx_client = FakeHttpClient()

    async def __aenter__(self):
        if self.failure is not None:
            raise self.failure
        self.functions = [FunctionTool(f"{self.name}_tool")]
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        self.closed += 1


class Recorder:
    def __init__(self):
        self.seen = None
        self.answer = None
        self.invocation = None
        self.built_with = None

    def make_http_agent_handler(self, resolved, capabilities):
        self.built_with = (resolved, capabilities)

        async def handler(req):
            self.invocation = hooks.hook_invocation()
            self.seen = await req.json()
            if self.answer is not None:
                return self.answer()
            return {"ok": True, "session": req.headers.get("x-ms-session-id")}

        handler.__name__ = "handler_expense_normalizer"
        handler.__annotations__ = {"req": "Request", "return": "Response"}
        return handler


@pytest.fixture
def fake_hosted_skills(monkeypatch):
    azure_functions_agents_integration._enabled = False
    chain_reset = hooks._hook_invocation.set(None)
    request_reset = hooks._current.set(None)
    scope_reset = _mcp_request_scope._request_scope.set(None)
    _mcp_request_scope._report_unconfigured_origin.cache_clear()
    monkeypatch.setattr(auth, "_base_url", None)
    monkeypatch.setenv("APP_BASE_URL", "https://app.example.invalid")

    # Like the runtime, discover_user_tools calls the module's discover_project_tools.
    tools = types.ModuleType("azure_functions_agents.discovery.tools")
    tools.app_tools = [FunctionTool("app_tool")]
    tools.discover_project_tools = lambda root: ProjectTools(list(tools.app_tools), [], [])
    tools.discover_user_tools = lambda root: ToolDiscoveryResult(
        tools.discover_project_tools(root).user_tools, []
    )
    app_module = types.ModuleType("azure_functions_agents.app")
    app_module.discover_project_tools = tools.discover_project_tools
    runner = types.ModuleType("azure_functions_agents.runner")
    runner.discover_user_tools = tools.discover_user_tools
    runner.get_app_root = lambda: "."

    recorder = Recorder()
    handlers = types.ModuleType("azure_functions_agents.registration._handlers")
    handlers.make_http_agent_handler = recorder.make_http_agent_handler
    triggers = types.ModuleType("azure_functions_agents.registration.triggers")
    triggers.make_http_agent_handler = handlers.make_http_agent_handler
    endpoints = types.ModuleType("azure_functions_agents.registration.endpoints")

    def register_chat(app, resolved, capabilities, *, route, function_name):
        @app.route(route=route, methods=["POST"])
        async def chat(req):
            return await resolved(req)

        app.function_name(name=function_name)(chat)

    endpoints._register_http_chat = register_chat
    endpoints._register_http_chat_stream = register_chat

    mcp = types.ModuleType("azure_functions_agents.discovery.mcp")
    mcp.seen_auth = []

    def build_header_provider(server):
        config = server.get("auth")
        static = dict(server.get("headers") or {})
        if not isinstance(config, dict):
            return (lambda ctx: dict(static)) if static else None
        method = config.get("type", "managed_identity")
        mcp.seen_auth.append(method)
        return lambda ctx: {**static, "Authorization": f"application:{method}"}

    mcp._build_header_provider = build_header_provider

    def build_http_client(provider):
        # The runtime builds a client only when a server has headers to inject.
        return None if provider is None else FakeAsyncClient()

    mcp._build_http_client = build_http_client
    state = types.SimpleNamespace(
        built=[], cache=None,
        servers={"portal": {
            "url": "https://app.example.invalid/.ai/mcp/portal",
            "auth": {"scope": "api://app/.default"},
        }},
    )

    mcp.MCPStreamableHTTPTool = FakeMcpTool

    def build_mcp_tool(name, server):
        # Like the runtime, look the tool class up on the module at build time.
        tool = mcp.MCPStreamableHTTPTool(name, server.get("failure"))
        state.built.append(tool)
        return tool, None

    def discover_mcp_servers(root):
        if state.cache is None:
            state.cache = {
                name: mcp._build_mcp_tool(name, config)[0]
                for name, config in state.servers.items()
            }
        return MCPDiscoveryResult(dict(state.cache), [])

    def clear_mcp_cache():
        state.cache = None

    mcp._build_mcp_tool = build_mcp_tool
    mcp.discover_mcp_servers = discover_mcp_servers
    mcp.clear_mcp_cache = clear_mcp_cache
    runner.discover_mcp_servers = discover_mcp_servers

    async def run_agent(*args, mcp_tools=None, **kwargs):
        return (
            list(mcp.discover_mcp_servers(".").servers.values())
            if mcp_tools is None else list(mcp_tools)
        )

    runner.run_agent = run_agent
    runner.run_agent_stream = run_agent
    capabilities = types.SimpleNamespace(
        filtered_mcp_tools=list(discover_mcp_servers(".").servers.values())
    )

    discovery = types.ModuleType("azure_functions_agents.discovery")
    discovery.tools = tools
    discovery.mcp = mcp
    registration = types.ModuleType("azure_functions_agents.registration")
    registration._handlers = handlers
    registration.triggers = triggers
    registration.endpoints = endpoints
    package = types.ModuleType("azure_functions_agents")
    package.tool = make_tool
    package.app = app_module
    package.runner = runner
    package.discovery = discovery
    package.registration = registration

    for name, module in {
        "azure_functions_agents": package,
        "azure_functions_agents.app": app_module,
        "azure_functions_agents.runner": runner,
        "azure_functions_agents.discovery": discovery,
        "azure_functions_agents.discovery.tools": tools,
        "azure_functions_agents.discovery.mcp": mcp,
        "azure_functions_agents.registration": registration,
        "azure_functions_agents.registration._handlers": handlers,
        "azure_functions_agents.registration.triggers": triggers,
        "azure_functions_agents.registration.endpoints": endpoints,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(azure_functions_agents_integration.importlib.util, "find_spec", lambda name: object())

    yield types.SimpleNamespace(
        tools=tools, app=app_module, runner=runner, handlers=handlers, triggers=triggers,
        endpoints=endpoints, mcp=mcp, recorder=recorder, state=state, capabilities=capabilities,
    )

    azure_functions_agents_integration._enabled = False
    hooks._hook_invocation.reset(chain_reset)
    hooks._current.reset(request_reset)
    _mcp_request_scope._request_scope.reset(scope_reset)


class FakeRequest:
    def __init__(self, body=b'{"amount":"16"}', headers=None):
        self.method = "POST"
        self.headers = headers or {}
        self.url = types.SimpleNamespace(query="debug=1")
        self._body = body

    async def body(self):
        return self._body

    async def json(self):
        return json.loads(self._body)


FORWARD = {
    "x-ms-hook-name": "expense-normalizer",
    "x-ms-hook-mode": "intercept",
    "x-ms-hook-path": "/api/expenses",
    "x-ms-hook-route": "/api/expenses/{id}",
    "x-ms-hook-invocation": "signed-chain",
    "x-service-token": "service-secret",
    "x-functions-key": "KEY",
    "content-type": "application/json",
}


class FakeFunctionApp:
    def __init__(self):
        self.registered = {}

    def route(self, route, **kwargs):
        def decorate(handler):
            self.registered[route] = handler
            return handler

        return decorate

    def function_name(self, name):
        return lambda handler: handler


def chat_route(runtime, inner, streaming=False):
    app = FakeFunctionApp()
    registrar = (
        runtime.endpoints._register_http_chat_stream
        if streaming else runtime.endpoints._register_http_chat
    )
    registrar(app, inner, None, route="agents/x/chat", function_name="agents_x_chat")
    return app.registered["agents/x/chat"]


class TestDetection:
    def runtime_state(self, runtime):
        return {
            module.__name__: vars(module).copy()
            for module in vars(runtime).values()
            if isinstance(module, types.ModuleType)
        }

    def test_absent_runtime_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(azure_functions_agents_integration, "_enabled", False)
        monkeypatch.setattr(azure_functions_agents_integration.importlib.util, "find_spec", lambda name: None)
        assert azure_functions_agents_integration.is_available() is False
        assert azure_functions_agents_integration.enable() is False

    def test_present_runtime_is_wired_once(self, fake_hosted_skills):
        assert azure_functions_agents_integration.is_available() is True
        assert azure_functions_agents_integration.enable() is True
        assert azure_functions_agents_integration.enable() is False

    @pytest.mark.asyncio
    async def test_configure_integrates_agents_and_preserves_regular_functions(self, fake_hosted_skills):
        app = pytest.importorskip("azure.functions").FunctionApp()
        auth.configure(app)
        assert azure_functions_agents_integration._enabled is True

        @app.route(route="regular")
        async def regular(req):
            return await req.json()

        handler = fake_hosted_skills.handlers.make_http_agent_handler(object(), object())
        agent = app.route(route="agent")(handler)

        assert await regular(FakeRequest(headers=FORWARD)) == {"amount": "16"}
        await agent(FakeRequest(headers=FORWARD))
        assert fake_hosted_skills.recorder.seen["path"] == "/api/expenses"
        assert fake_hosted_skills.recorder.seen["body"] == '{"amount":"16"}'

    @pytest.mark.parametrize(
        ("flag", "module_name", "function_name"),
        [
            ("callbacks", "azure_functions_agents.discovery.mcp", "_build_header_provider"),
            ("callbacks", "azure_functions_agents.discovery.mcp", "_build_http_client"),
            ("callbacks", "azure_functions_agents.discovery.mcp", "discover_mcp_servers"),
            ("callbacks", "azure_functions_agents.discovery.mcp", "clear_mcp_cache"),
            ("callbacks", "azure_functions_agents.runner", "run_agent"),
            ("callbacks", "azure_functions_agents.runner", "run_agent_stream"),
            ("callbacks", "azure_functions_agents.runner", "get_app_root"),
            ("tools", "azure_functions_agents.discovery.tools", "discover_project_tools"),
            ("tools", "azure_functions_agents", "tool"),
            ("prompt", "azure_functions_agents.registration._handlers", "make_http_agent_handler"),
        ],
    )
    @pytest.mark.parametrize("missing", [True, False], ids=["missing", "not-callable"])
    def test_an_invalid_required_function_refuses_to_start_before_patching(
        self, fake_hosted_skills, monkeypatch, flag, module_name, function_name, missing
    ):
        module = sys.modules[module_name]
        if missing:
            monkeypatch.delattr(module, function_name)
        else:
            monkeypatch.setattr(module, function_name, None)
        original = self.runtime_state(fake_hosted_skills)

        with pytest.raises(RuntimeError, match=function_name) as caught:
            azure_functions_agents_integration.enable()
        assert "Upgrade" in str(caught.value)
        assert f"{flag}=False" in str(caught.value)
        assert azure_functions_agents_integration._enabled is False
        assert self.runtime_state(fake_hosted_skills) == original

    def test_invalid_callbacks_prevent_all_installers(self, fake_hosted_skills, monkeypatch):
        monkeypatch.delattr(fake_hosted_skills.runner, "run_agent_stream")

        def unexpected_installation():
            pytest.fail("No installer should run before all enabled integrations are validated")

        monkeypatch.setattr(_mcp_request_scope, "patch_hook_forwarding", unexpected_installation)
        monkeypatch.setattr(_tools, "patch_tool_discovery", unexpected_installation)
        monkeypatch.setattr(_prompt, "patch_prompt", unexpected_installation)

        with pytest.raises(RuntimeError, match="run_agent_stream"):
            azure_functions_agents_integration.enable()
        assert azure_functions_agents_integration._enabled is False

    def test_descriptor_callbacks_are_validated_before_installation(
        self, fake_hosted_skills, monkeypatch
    ):
        maf_mcp = types.ModuleType("azure_functions_agents.harness.agent_framework._maf_mcp")
        maf_mcp.build_maf_mcp_tools = lambda servers: []
        maf_mcp.build_mcp_header_provider = lambda server: None
        maf_mcp._OwnedHTTPClientMCPStreamableHTTPTool = FakeMcpTool
        monkeypatch.setitem(sys.modules, maf_mcp.__name__, maf_mcp)
        monkeypatch.setattr(
            fake_hosted_skills.mcp, "MCPServerDescriptor", types.SimpleNamespace, raising=False
        )
        monkeypatch.setattr(
            fake_hosted_skills.runner, "get_agent_runner", lambda harness: None, raising=False
        )
        monkeypatch.delattr(fake_hosted_skills.mcp, "_build_header_provider")

        _mcp_request_scope.validate_hook_forwarding()
        monkeypatch.delattr(fake_hosted_skills.runner, "get_agent_runner")

        def unexpected_installation():
            pytest.fail("Descriptor callbacks must be validated before installing integrations")

        monkeypatch.setattr(_mcp_request_scope, "patch_hook_forwarding", unexpected_installation)
        monkeypatch.setattr(_tools, "patch_tool_discovery", unexpected_installation)
        monkeypatch.setattr(_prompt, "patch_prompt", unexpected_installation)

        with pytest.raises(RuntimeError, match="get_agent_runner"):
            azure_functions_agents_integration.enable()
        assert azure_functions_agents_integration._enabled is False

    def test_callback_installer_validates_before_replacing_functions(
        self, fake_hosted_skills, monkeypatch
    ):
        monkeypatch.delattr(fake_hosted_skills.runner, "run_agent_stream")
        original = self.runtime_state(fake_hosted_skills)

        with pytest.raises(RuntimeError, match="run_agent_stream"):
            _mcp_request_scope.patch_hook_forwarding()
        assert self.runtime_state(fake_hosted_skills) == original

    @pytest.mark.parametrize(
        ("installer", "module_name", "function_name"),
        [
            (_tools.patch_tool_discovery, "azure_functions_agents", "tool"),
            (
                _prompt.patch_prompt,
                "azure_functions_agents.registration.endpoints",
                "_register_http_chat_stream",
            ),
        ],
        ids=["tools", "prompt"],
    )
    def test_tool_and_prompt_installers_validate_before_replacing_functions(
        self, fake_hosted_skills, monkeypatch, installer, module_name, function_name
    ):
        monkeypatch.setattr(sys.modules[module_name], function_name, None)
        original = self.runtime_state(fake_hosted_skills)

        with pytest.raises(RuntimeError, match=function_name):
            installer()
        assert self.runtime_state(fake_hosted_skills) == original

    @pytest.mark.parametrize(
        "module_name",
        [
            "azure_functions_agents.discovery.mcp",
            "azure_functions_agents.discovery.tools",
            "azure_functions_agents.registration._handlers",
        ],
    )
    def test_a_missing_required_module_refuses_to_start_before_patching(
        self, fake_hosted_skills, monkeypatch, module_name
    ):
        parent, name = module_name.rsplit(".", 1)
        monkeypatch.delattr(sys.modules[parent], name)
        monkeypatch.setitem(sys.modules, module_name, None)
        original = self.runtime_state(fake_hosted_skills)

        with pytest.raises(RuntimeError, match=module_name):
            azure_functions_agents_integration.enable()
        assert azure_functions_agents_integration._enabled is False
        assert self.runtime_state(fake_hosted_skills) == original

    @pytest.mark.parametrize(
        ("flag", "module_name", "function_name"),
        [
            ("callbacks", "azure_functions_agents.discovery.mcp", "_build_header_provider"),
            ("tools", "azure_functions_agents.discovery.tools", "discover_project_tools"),
            ("tools", "azure_functions_agents", "tool"),
            ("prompt", "azure_functions_agents.registration._handlers", "make_http_agent_handler"),
        ],
    )
    def test_disabled_integrations_do_not_require_their_runtime_functions(
        self, fake_hosted_skills, monkeypatch, flag, module_name, function_name
    ):
        monkeypatch.delattr(sys.modules[module_name], function_name)
        assert azure_functions_agents_integration.enable(**{flag: False}) is True

    @pytest.mark.parametrize("registrar_name", ["_register_http_chat", "_register_http_chat_stream"])
    def test_a_noncallable_chat_registrar_refuses_to_start_before_patching(
        self, fake_hosted_skills, monkeypatch, registrar_name
    ):
        monkeypatch.setattr(fake_hosted_skills.endpoints, registrar_name, None)
        original = self.runtime_state(fake_hosted_skills)

        with pytest.raises(RuntimeError, match=registrar_name):
            azure_functions_agents_integration.enable()
        assert azure_functions_agents_integration._enabled is False
        assert self.runtime_state(fake_hosted_skills) == original
        assert azure_functions_agents_integration.enable(prompt=False) is True

    @pytest.mark.parametrize(
        "missing",
        [
            None,
            ("_register_http_chat",),
            ("_register_http_chat_stream",),
            ("_register_http_chat", "_register_http_chat_stream"),
        ],
        ids=["module", "chat", "stream", "both"],
    )
    @pytest.mark.asyncio
    async def test_absent_optional_chat_endpoints_do_not_block_agent_prompts(
        self, fake_hosted_skills, monkeypatch, missing
    ):
        if missing is None:
            monkeypatch.delattr(sys.modules["azure_functions_agents.registration"], "endpoints")
            monkeypatch.setitem(sys.modules, "azure_functions_agents.registration.endpoints", None)
        else:
            for name in missing:
                monkeypatch.delattr(fake_hosted_skills.endpoints, name)

        assert azure_functions_agents_integration.enable() is True
        handler = fake_hosted_skills.handlers.make_http_agent_handler(object(), object())
        await handler(FakeRequest(headers=FORWARD))
        assert fake_hosted_skills.recorder.seen["path"] == "/api/expenses"

    def test_installed_chat_registrars_match(self):
        endpoints = pytest.importorskip("azure_functions_agents.registration.endpoints")
        from app_service_extensions.azure_functions_agents_integration._prompt import _CHAT_REGISTRARS

        assert all(hasattr(endpoints, name) for name in _CHAT_REGISTRARS)

    def test_installed_runtime_supports_all_integrations(self):
        pytest.importorskip("azure_functions_agents.runner")
        from app_service_extensions.azure_functions_agents_integration._prompt import validate_prompt
        from app_service_extensions.azure_functions_agents_integration._tools import validate_tool_discovery

        _mcp_request_scope.validate_hook_forwarding()
        validate_tool_discovery()
        validate_prompt()

    def test_installed_mcp_api_matches(self):
        runner = pytest.importorskip("azure_functions_agents.runner")
        capabilities = pytest.importorskip("azure_functions_agents.registration.capabilities")
        assert hasattr(runner, "run_agent")
        assert hasattr(runner, "run_agent_stream")
        assert "filtered_mcp_tools" in capabilities.AgentCapabilities.__annotations__

    def test_installed_runtime_exports_match(self):
        mcp = pytest.importorskip("azure_functions_agents.discovery.mcp")
        tools = pytest.importorskip("azure_functions_agents.discovery.tools")
        handlers = pytest.importorskip("azure_functions_agents.registration._handlers")
        triggers = pytest.importorskip("azure_functions_agents.registration.triggers")
        if _mcp_request_scope._uses_mcp_descriptors():
            from azure_functions_agents.harness.agent_framework import _maf_mcp

            assert isinstance(_maf_mcp._OwnedHTTPClientMCPStreamableHTTPTool, type)
        else:
            assert isinstance(mcp.MCPStreamableHTTPTool, type)
        assert callable(tools.discover_project_tools)
        assert triggers.make_http_agent_handler is handlers.make_http_agent_handler


@pytest.fixture
def descriptor_runtime(monkeypatch):
    mcp = pytest.importorskip("azure_functions_agents.discovery.mcp")
    if not hasattr(mcp, "MCPServerDescriptor"):
        pytest.skip("requires the descriptor-based runtime")
    from azure_functions_agents import runner
    from azure_functions_agents.harness.agent_framework import _maf_mcp

    for module, names in (
        (runner, ("run_agent", "run_agent_stream", "get_agent_runner")),
        (_maf_mcp, ("build_maf_mcp_tools",)),
    ):
        for name in names:
            monkeypatch.setattr(module, name, getattr(module, name))
    return mcp, runner, _maf_mcp


async def test_descriptor_runtime_scopes_real_maf_transport(descriptor_runtime, monkeypatch):
    import httpx

    mcp, _, maf_mcp = descriptor_runtime
    monkeypatch.setattr(_mcp_request_scope, "app_base_url", lambda: "https://app.test")
    client_class = httpx.AsyncClient
    seen = []

    def handle(request):
        seen.append((request.url.host, request.headers.get(hooks.HOOK_INVOCATION_HEADER),
                     request.headers.get("authorization")))
        if request.url.path == "/redirect":
            return httpx.Response(307, headers={"location": "https://other.test/mcp"})
        return httpx.Response(200)

    class MockClient(client_class):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    _mcp_request_scope.patch_hook_forwarding()
    anonymous = mcp.MCPServerDescriptor.create(name="same", url="https://app.test/mcp", tools=["read"])
    authenticated = mcp.MCPServerDescriptor.create(
        name="auth", url="https://app.test/mcp", headers={"Authorization": "Bearer test-auth"},
    )
    external = mcp.MCPServerDescriptor.create(name="external", url="https://other.test/mcp")
    assert maf_mcp.build_maf_mcp_tools([anonymous])[0] is maf_mcp.build_maf_mcp_tools([anonymous])[0]
    ready = asyncio.Event()

    async def turn(token, server):
        scope = _mcp_request_scope.RequestScope(token)
        scope.adopt()
        try:
            tool, other = maf_mcp.build_maf_mcp_tools([server, external])
            assert other._header_provider is None
            async with tool.get_mcp_client():
                client = tool._httpx_client
                assert client is not None
                if token == "first":
                    await ready.wait()
                else:
                    ready.set()
                await client.get(server.url)
                with pytest.raises(httpx.RequestError, match="refused to follow"):
                    await client.get("https://app.test/redirect")
            assert client.is_closed
            return tool
        finally:
            await scope.close()

    first, second, repeat = await asyncio.gather(
        turn("first", anonymous), turn("second", authenticated), turn("repeat", anonymous),
    )
    assert first is not repeat
    assert first.allowed_tools == ["read"]
    assert second is not first
    assert sorted(seen) == [
        ("app.test", "first", None),
        ("app.test", "first", None),
        ("app.test", "repeat", None),
        ("app.test", "repeat", None),
        ("app.test", "second", "Bearer test-auth"),
        ("app.test", "second", "Bearer test-auth"),
    ]


async def test_descriptor_runtime_rejects_copilot_for_scoped_turns(descriptor_runtime):
    _, runner, _ = descriptor_runtime
    _mcp_request_scope.patch_hook_forwarding()
    scope = _mcp_request_scope.RequestScope("test")
    scope.adopt()
    try:
        with pytest.raises(RuntimeError, match="require the Agent Framework"):
            runner.get_agent_runner(types.SimpleNamespace(name="copilot"))
    finally:
        await scope.close()


async def test_installed_runtime_http_routes(monkeypatch, tmp_path):
    runner = pytest.importorskip("azure_functions_agents.runner")
    from azure_functions_agents import create_function_app
    from azure_functions_agents.discovery import mcp, tools
    from azure_functions_agents.registration import _handlers, endpoints, triggers
    from app_service_extensions.azure_functions_agents_integration._prompt import patch_prompt
    from app_service_extensions.azure_functions_agents_integration._tools import patch_tool_discovery

    if _mcp_request_scope._uses_mcp_descriptors():
        from azure_functions_agents.harness.agent_framework import _maf_mcp

        monkeypatch.setattr(_maf_mcp, "build_maf_mcp_tools", _maf_mcp.build_maf_mcp_tools)
        monkeypatch.setattr(runner, "get_agent_runner", runner.get_agent_runner)
    else:
        for name in ("_build_header_provider", "_build_http_client", "discover_mcp_servers",
                     "MCPStreamableHTTPTool"):
            monkeypatch.setattr(mcp, name, getattr(mcp, name))
        monkeypatch.setattr(runner, "discover_mcp_servers", runner.discover_mcp_servers)

    for module, name in (
        (_handlers, "make_http_agent_handler"),
        (triggers, "make_http_agent_handler"),
        (endpoints, "_register_http_chat"),
        (endpoints, "_register_http_chat_stream"),
        (tools, "discover_project_tools"),
        (sys.modules["azure_functions_agents.app"], "discover_project_tools"),
    ):
        monkeypatch.setattr(module, name, getattr(module, name))

    (tmp_path / "main.agent.md").write_text(
        "---\nname: main\ndescription: Test agent\ntrigger:\n  type: http_trigger\n"
        "  args:\n    route: main\n"
        "builtin_endpoints:\n  chat_api: true\n---\nReview the request.\n",
        encoding="utf-8",
    )
    prompts = []
    stream_failed = False

    async def run(prompt, **kwargs):
        assert hooks.hook_invocation() == "signed-chain"
        assert "set_header" in {tool.name for tool in kwargs["tools"]}
        prompts.append(str(prompt))
        request_tools.set_header("X-Reviewed", "yes")
        return types.SimpleNamespace(content="reviewed", model="test", tool_calls=[], session_id="test")

    async def stream(prompt, **kwargs):
        await run(prompt, **kwargs)
        if stream_failed:
            yield 'data: {"type":"error","content":"mock failure"}\n\n'
        else:
            yield 'data: {"type":"delta","content":"reviewed"}\n\n'

    monkeypatch.setattr(runner, "run_agent", run)
    monkeypatch.setattr(runner, "run_agent_stream", stream)
    _mcp_request_scope.patch_hook_forwarding()
    patch_tool_discovery()
    patch_prompt()
    app = create_function_app(tmp_path)
    routes = {
        binding.route: fn.get_user_function()
        for fn in app.get_functions()
        for binding in fn.get_bindings()
        if hasattr(binding, "route")
    }

    for route in ("main", "agents/main/chat", "agents/main/chatstream"):
        response = await routes[route](FakeRequest(headers=FORWARD))
        assert response.status_code == 200
        assert json.loads(response.body)["edits"][0]["name"] == "X-Reviewed"
        assert hooks.hook_invocation() is None

    stream_failed = True
    response = await routes["agents/main/chatstream"](FakeRequest(headers=FORWARD))
    assert response.status_code == 500
    assert "edits" not in json.loads(response.body)
    assert all("/api/expenses" in prompt for prompt in prompts)


class TestTools:
    def discovered(self, runtime):
        """The tools found on each runtime path: discovery, app start-up, and agent runs."""
        return {
            "source": runtime.tools.discover_project_tools("/app").user_tools,
            "app": runtime.app.discover_project_tools("/app").user_tools,
            "runner": runtime.runner.discover_user_tools("/app").tools,
        }

    def test_our_tools_join_the_apps_own_once_on_every_path(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        expected = ["app_tool", *(tool.__name__ for tool in ALL_TOOLS)]
        for path, found in self.discovered(fake_hosted_skills).items():
            assert sorted(tool.name for tool in found) == sorted(expected), path

    def test_the_app_calling_tool_stays_withheld(self, fake_hosted_skills, monkeypatch):
        from app_service_extensions.azure_functions_agents_integration import _tools

        monkeypatch.setattr(_tools, "ALL_TOOLS", [*ALL_TOOLS, call_web_app_wrapper])
        azure_functions_agents_integration.enable()
        names = {tool.name for tool in fake_hosted_skills.app.discover_project_tools("/app").user_tools}
        assert "call_web_app" not in names
        assert "call_web_app_wrapper" not in names
        assert "set_header" in names

    def test_docstrings_carry_over(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        found = {tool.name: tool for tool in self.discovered(fake_hosted_skills)["runner"]}
        assert found["set_header"].description == set_header.__doc__.strip()

    def test_an_app_tool_wins_a_name_clash(self, fake_hosted_skills):
        fake_hosted_skills.tools.app_tools = [FunctionTool("set_header", "app")]
        azure_functions_agents_integration.enable()
        found = [tool for tool in self.discovered(fake_hosted_skills)["runner"] if tool.name == "set_header"]
        assert len(found) == 1
        assert found[0].description == "app"

    def test_tools_can_be_skipped(self, fake_hosted_skills):
        azure_functions_agents_integration.enable(tools=False)
        assert [tool.name for tool in self.discovered(fake_hosted_skills)["runner"]] == ["app_tool"]


class TestPrompt:
    async def call(self, runtime, request):
        handler = runtime.handlers.make_http_agent_handler(object(), object())
        return await handler(request)

    @pytest.mark.asyncio
    async def test_the_agent_sees_the_whole_forward(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        await self.call(fake_hosted_skills, FakeRequest(headers=FORWARD))
        seen = fake_hosted_skills.recorder.seen
        assert {name: seen[name] for name in ("hook", "mode", "method", "path", "route")} == {
            "hook": "expense-normalizer", "mode": "intercept", "method": "POST",
            "path": "/api/expenses", "route": "/api/expenses/{id}",
        }
        assert seen["query"] == "debug=1"
        assert seen["body"] == '{"amount":"16"}'

    @pytest.mark.asyncio
    async def test_credentials_never_reach_the_agent(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        await self.call(fake_hosted_skills, FakeRequest(headers=FORWARD))
        rendered = json.dumps(fake_hosted_skills.recorder.seen)
        for credential in ("service-secret", "signed-chain", "KEY"):
            assert credential not in rendered

    @pytest.mark.asyncio
    async def test_a_bodyless_hook_still_describes_the_request(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        await self.call(fake_hosted_skills, FakeRequest(body=b"", headers=FORWARD))
        assert fake_hosted_skills.recorder.seen["path"] == "/api/expenses"

    @pytest.mark.asyncio
    async def test_a_direct_call_is_left_alone(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        await self.call(fake_hosted_skills, FakeRequest())
        assert fake_hosted_skills.recorder.seen == {"amount": "16"}

    @pytest.mark.asyncio
    async def test_other_request_attributes_are_delegated(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        answer = await self.call(
            fake_hosted_skills, FakeRequest(headers={**FORWARD, "x-ms-session-id": "session-9"})
        )
        assert answer["session"] == "session-9"

    @pytest.mark.asyncio
    async def test_the_callback_chain_is_scoped_to_the_turn(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        await self.call(fake_hosted_skills, FakeRequest(headers=FORWARD))
        assert fake_hosted_skills.recorder.invocation == "signed-chain"
        assert hooks.hook_invocation() is None

    @pytest.mark.asyncio
    async def test_prompt_patching_can_be_skipped(self, fake_hosted_skills):
        azure_functions_agents_integration.enable(prompt=False)
        await self.call(fake_hosted_skills, FakeRequest(headers=FORWARD))
        assert fake_hosted_skills.recorder.seen == {"amount": "16"}

    def test_worker_annotations_and_handler_names_survive(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        handler = fake_hosted_skills.handlers.make_http_agent_handler(object(), object())
        assert handler.__name__ == "handler_expense_normalizer"
        assert typing.get_type_hints(handler) == {"req": Request, "return": Response}
        assert fake_hosted_skills.triggers.make_http_agent_handler is (
            fake_hosted_skills.handlers.make_http_agent_handler
        )


class TestAnswers:
    @pytest.mark.asyncio
    async def test_edits_replace_prose_and_preserve_the_session_header(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        prose = Response(
            "I set a header.", media_type="text/plain", headers={"x-ms-session-id": "session-9"}
        )

        def agent():
            request_tools.set_header("X-Reviewed", "yes")
            return prose

        fake_hosted_skills.recorder.answer = agent
        handler = fake_hosted_skills.handlers.make_http_agent_handler(object(), object())
        answer = await handler(FakeRequest(headers=FORWARD))
        assert json.loads(answer.body) == {
            "action": "modify",
            "edits": [{"op": "set", "target": "header", "name": "X-Reviewed", "value": "yes"}],
        }
        assert answer.headers["content-type"] == "application/json"
        assert int(answer.headers["content-length"]) == len(answer.body)
        assert answer.headers["x-ms-session-id"] == "session-9"

    @pytest.mark.asyncio
    async def test_untouched_requests_keep_the_agents_answer(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        prose = Response("nothing to do", media_type="text/plain")
        fake_hosted_skills.recorder.answer = lambda: prose
        handler = fake_hosted_skills.handlers.make_http_agent_handler(object(), object())
        assert await handler(FakeRequest(headers=FORWARD)) is prose

    @pytest.mark.asyncio
    async def test_a_run_that_fails_after_an_edit_returns_the_failure(self, fake_hosted_skills):
        # Sending the half-finished patch would skip the hook's failurePolicy.
        async def failing_run(*args, **kwargs):
            request_tools.set_header("X-Reviewed", "partial")
            raise RuntimeError("model unavailable")

        fake_hosted_skills.runner.run_agent = failing_run
        azure_functions_agents_integration.enable()

        async def inner(req):
            try:
                await fake_hosted_skills.runner.run_agent(mcp_tools=None)
            except RuntimeError as exc:
                # How the runtime's own handlers report a failed run.
                return Response(json.dumps({"error": str(exc)}), status_code=500)
            return Response("Reviewed.")

        answer = await chat_route(fake_hosted_skills, inner)(FakeRequest(body=b"", headers=FORWARD))
        assert answer.status_code == 500
        assert json.loads(answer.body) == {"error": "model unavailable"}

    @pytest.mark.asyncio
    async def test_an_unsuccessful_status_after_an_edit_is_kept(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()

        def agent():
            request_tools.set_header("X-Reviewed", "partial")
            return Response('{"error":"schema validation failed"}', status_code=500)

        fake_hosted_skills.recorder.answer = agent
        handler = fake_hosted_skills.handlers.make_http_agent_handler(object(), object())
        answer = await handler(FakeRequest(headers=FORWARD))
        assert answer.status_code == 500
        assert b"edits" not in answer.body

    @pytest.mark.asyncio
    async def test_a_stream_that_reports_an_error_after_an_edit_becomes_a_failure(self, fake_hosted_skills):
        # A stream sends 200 before the agent runs, so the error comes as an event in the body.
        async def failing_stream(*args, **kwargs):
            request_tools.set_header("X-Reviewed", "partial")
            yield 'data: {"type": "content", "content": "Reviewing"}\n\n'
            yield f"data: {json.dumps({'type': 'error', 'content': 'Timeout after 30s'})}\n\n"

        fake_hosted_skills.runner.run_agent_stream = failing_stream
        azure_functions_agents_integration.enable()

        async def inner(req):
            return StreamingResponse(
                fake_hosted_skills.runner.run_agent_stream(mcp_tools=None), media_type="text/event-stream"
            )

        answer = await chat_route(fake_hosted_skills, inner, streaming=True)(
            FakeRequest(body=b"", headers=FORWARD)
        )
        assert answer.status_code == 500
        assert b"edits" not in answer.body

    @pytest.mark.asyncio
    async def test_a_completed_stream_commits_its_decision(self, fake_hosted_skills):
        async def stream(*args, **kwargs):
            request_tools.set_header("X-Reviewed", "yes")
            yield 'data: {"type": "done"}\n\n'

        fake_hosted_skills.runner.run_agent_stream = stream
        azure_functions_agents_integration.enable()

        async def inner(req):
            return StreamingResponse(fake_hosted_skills.runner.run_agent_stream(mcp_tools=None))

        answer = await chat_route(fake_hosted_skills, inner, streaming=True)(
            FakeRequest(body=b"", headers=FORWARD)
        )
        assert answer.status_code == 200
        assert json.loads(answer.body)["edits"][0]["name"] == "X-Reviewed"

    @pytest.mark.asyncio
    async def test_the_decision_is_logged_without_its_values(self, fake_hosted_skills, caplog):
        azure_functions_agents_integration.enable()

        def agent():
            request_tools.set_header("X-Reviewed", "SYNTHETIC-HEADER-VALUE")
            request_tools.replace_body('{"note": "SYNTHETIC-BODY-VALUE"}')
            return Response("Reviewed.")

        fake_hosted_skills.recorder.answer = agent
        handler = fake_hosted_skills.handlers.make_http_agent_handler(object(), object())
        with caplog.at_level(logging.INFO, logger="azure.functions.AppServiceExtensions"):
            answer = await handler(FakeRequest(headers=FORWARD))

        assert json.loads(answer.body)["edits"][1]["value"] == {"note": "SYNTHETIC-BODY-VALUE"}
        assert "SYNTHETIC" not in caplog.text
        assert '"name": "X-Reviewed"' in caplog.text
        assert '"target": "body"' in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error_type", [TypeError, ValueError, RuntimeError])
    async def test_response_serialization_failures_are_logged_without_values(
        self, fake_hosted_skills, monkeypatch, caplog, error_type
    ):
        azure_functions_agents_integration.enable()
        failure = error_type("SYNTHETIC-BODY-VALUE")

        def fail_to_json(self, reason=None):
            raise failure

        monkeypatch.setattr(EditableRequest, "to_json", fail_to_json)

        def agent():
            request_tools.set_header("X-Reviewed", "SYNTHETIC-HEADER-VALUE")
            return Response("Reviewed.")

        fake_hosted_skills.recorder.answer = agent
        handler = fake_hosted_skills.handlers.make_http_agent_handler(object(), object())
        with caplog.at_level(logging.INFO, logger="azure.functions.AppServiceExtensions"):
            with pytest.raises(error_type) as caught:
                await handler(FakeRequest(headers=FORWARD))

        assert caught.value is failure
        assert caplog.messages == [
            f"Could not build hook response ({error_type.__name__})"
        ]
        assert "SYNTHETIC" not in caplog.text
        assert hooks.hook_invocation() is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error_type", [TypeError, ValueError, RuntimeError, KeyError])
    async def test_header_conversion_failures_are_logged_without_values(
        self, fake_hosted_skills, caplog, error_type
    ):
        azure_functions_agents_integration.enable()
        failure = error_type("SYNTHETIC-HEADER-VALUE")

        class UnreadableHeaders:
            def __iter__(self):
                raise failure

        def agent():
            request_tools.set_header("X-Reviewed", "yes")
            return types.SimpleNamespace(headers=UnreadableHeaders())

        fake_hosted_skills.recorder.answer = agent
        handler = fake_hosted_skills.handlers.make_http_agent_handler(object(), object())
        with caplog.at_level(logging.ERROR, logger="azure.functions.AppServiceExtensions"):
            with pytest.raises(error_type) as caught:
                await handler(FakeRequest(headers=FORWARD))

        assert caught.value is failure
        assert caplog.messages == [
            f"Could not copy agent response headers ({error_type.__name__})"
        ]
        assert "SYNTHETIC" not in caplog.text
        assert hooks.hook_invocation() is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "header_fields", [{}, {"headers": None}, {"headers": {}}],
        ids=["absent", "none", "empty"],
    )
    async def test_missing_headers_do_not_log_a_failure(
        self, fake_hosted_skills, caplog, header_fields
    ):
        azure_functions_agents_integration.enable()

        def agent():
            request_tools.set_header("X-Reviewed", "yes")
            return types.SimpleNamespace(**header_fields)

        fake_hosted_skills.recorder.answer = agent
        handler = fake_hosted_skills.handlers.make_http_agent_handler(object(), object())
        with caplog.at_level(logging.ERROR, logger="azure.functions.AppServiceExtensions"):
            answer = await handler(FakeRequest(headers=FORWARD))

        assert answer.status_code == 200
        assert json.loads(answer.body)["edits"][0]["value"] == "yes"
        assert not caplog.records

    @pytest.mark.asyncio
    @pytest.mark.parametrize("streaming", [False, True])
    async def test_both_chat_routes_scope_the_chain(self, fake_hosted_skills, streaming):
        azure_functions_agents_integration.enable()
        seen = []

        async def inner(req):
            seen.append(hooks.hook_invocation())
            return "answered"

        handler = chat_route(fake_hosted_skills, inner, streaming)
        assert await handler(FakeRequest(headers=FORWARD)) == "answered"
        assert hooks._current.get() is None
        assert await handler(FakeRequest()) == "answered"
        assert seen == ["signed-chain", None]
        assert hooks.hook_invocation() is None

    @pytest.mark.asyncio
    async def test_chat_hooks_are_prompts_and_tool_edits_become_patches(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        seen = []

        async def inner(req):
            seen.append(await req.json())
            request_tools.set_header("X-Reviewed", "yes")
            return Response("Reviewed.", media_type="text/plain")

        answer = await chat_route(fake_hosted_skills, inner)(FakeRequest(body=b"", headers=FORWARD))
        assert "expense-normalizer" in seen[0]["prompt"]
        assert "/api/expenses" in seen[0]["prompt"]
        assert "KEY" not in seen[0]["prompt"]
        assert json.loads(answer.body)["edits"][0]["name"] == "X-Reviewed"
        assert hooks._current.get() is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("proxy", [False, True])
    async def test_ordinary_chat_and_proxy_bodies_are_not_rewritten(self, fake_hosted_skills, proxy):
        azure_functions_agents_integration.enable()
        seen = []

        async def inner(req):
            seen.append(await req.json())
            return Response('{"response":"hello"}', media_type="application/json")

        headers = {**FORWARD, "x-ms-hook-mode": "proxy"} if proxy else {}
        answer = await chat_route(fake_hosted_skills, inner)(
            FakeRequest(body=b'{"prompt":"hey"}', headers=headers)
        )
        assert seen == [{"prompt": "hey"}]
        assert json.loads(answer.body) == {"response": "hello"}

    @pytest.mark.asyncio
    async def test_a_proxy_answer_remains_a_stream(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()

        async def chunks():
            yield b"data: hey\n\n"
            yield b"data: there\n\n"

        async def inner(req):
            return StreamingResponse(chunks(), media_type="text/event-stream")

        answer = await chat_route(fake_hosted_skills, inner)(
            FakeRequest(headers={**FORWARD, "x-ms-hook-mode": "proxy"})
        )
        assert b"".join([chunk async for chunk in answer.body_iterator]) == (
            b"data: hey\n\ndata: there\n\n"
        )


class TestMcpSessions:
    def chat(self, runtime, seen, failure=False, streaming=False):
        async def body():
            seen.append(("stream", hooks.hook_invocation()))
            yield b"chunk"

        async def inner(req):
            tools = await runtime.runner.run_agent(
                mcp_tools=runtime.capabilities.filtered_mcp_tools
            )
            seen.append(tools)
            if failure:
                raise RuntimeError("the agent failed")
            return StreamingResponse(body()) if streaming else "answered"

        return chat_route(runtime, inner)

    @pytest.mark.asyncio
    async def test_each_turn_gets_fresh_sessions_and_closes_them(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        startup = fake_hosted_skills.capabilities.filtered_mcp_tools[0]
        seen = []
        handler = self.chat(fake_hosted_skills, seen)
        await handler(FakeRequest(headers=FORWARD))
        await handler(FakeRequest(headers={"x-ms-hook-invocation": "another-chain"}))
        assert seen[0][0] is not seen[1][0]
        assert all(tool is not startup for tools in seen for tool in tools)
        assert all(tool.closed == 1 for tools in seen for tool in tools)
        assert all(tool._httpx_client.closed for tools in seen for tool in tools)

    @pytest.mark.asyncio
    async def test_failed_turns_close_sessions_and_clear_the_chain(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        seen = []
        with pytest.raises(RuntimeError, match="agent failed"):
            await self.chat(fake_hosted_skills, seen, failure=True)(FakeRequest(headers=FORWARD))
        assert seen[0][0].closed == 1
        assert seen[0][0]._httpx_client.closed
        assert hooks.hook_invocation() is None
        assert hooks._current.get() is None

    @pytest.mark.asyncio
    async def test_streaming_preserves_the_chain_and_sessions_until_consumed(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        seen = []
        response = await self.chat(fake_hosted_skills, seen, streaming=True)(
            FakeRequest(headers={"x-ms-hook-invocation": "signed-chain"})
        )
        tool = seen[0][0]
        assert tool.closed == 0
        assert [chunk async for chunk in response.body_iterator] == [b"chunk"]
        assert seen[1] == ("stream", "signed-chain")
        assert tool.closed == 1
        assert tool._httpx_client.closed
        assert hooks.hook_invocation() is None

    @pytest.mark.asyncio
    async def test_a_turn_keeps_all_and_only_its_declared_servers(self, fake_hosted_skills):
        fake_hosted_skills.state.servers["connector"] = {
            "url": "https://connector.invalid/mcp", "auth": {"type": "managed_identity"},
        }
        fake_hosted_skills.state.cache = None
        declared = fake_hosted_skills.mcp.discover_mcp_servers(".").servers
        azure_functions_agents_integration.enable()

        async def inner(req):
            return await fake_hosted_skills.runner.run_agent(mcp_tools=list(declared.values()))

        tools = await chat_route(fake_hosted_skills, inner)(FakeRequest(headers=FORWARD))
        assert {tool.name for tool in tools} == {"portal", "connector"}

        async def only_portal(req):
            return await fake_hosted_skills.runner.run_agent(mcp_tools=[declared["portal"]])

        tools = await chat_route(fake_hosted_skills, only_portal)(FakeRequest(headers=FORWARD))
        assert [tool.name for tool in tools] == ["portal"]

    def test_startup_and_warmup_discovery_keeps_its_cache(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        first = fake_hosted_skills.runner.discover_mcp_servers(".").servers["portal"]
        assert fake_hosted_skills.runner.discover_mcp_servers(".").servers["portal"] is first
        assert first.closed == 0

    @pytest.mark.parametrize("name", ["portal", " Portal "])
    def test_a_turns_servers_are_rebuilt_by_name(self, fake_hosted_skills, name):
        startup, fresh = FakeMcpTool(name), FakeMcpTool(name)
        discovered = MCPDiscoveryResult({name: fresh}, [])
        assert _mcp_request_scope._rebuilt_for_this_turn(
            [startup], lambda root: discovered, fake_hosted_skills.runner
        ) == [fresh]

    @pytest.mark.parametrize(
        "tool",
        [
            pytest.param(types.SimpleNamespace(), id="missing"),
            pytest.param(types.SimpleNamespace(name=None), id="null"),
            pytest.param(types.SimpleNamespace(name=""), id="empty"),
            pytest.param(types.SimpleNamespace(name=" \t"), id="blank"),
            pytest.param(types.SimpleNamespace(name=123), id="non-string"),
        ],
    )
    def test_invalid_tool_names_are_rejected_before_discovery(self, fake_hosted_skills, tool):
        def unexpected_discovery(root):
            pytest.fail("Invalid tool names must be rejected before discovery")

        with pytest.raises(TypeError, match="nonblank string 'name'"):
            _mcp_request_scope._rebuilt_for_this_turn(
                [FakeMcpTool("portal"), tool], unexpected_discovery, fake_hosted_skills.runner
            )

    def test_failed_rediscovery_is_logged(self, fake_hosted_skills, caplog):
        def failed(root):
            raise RuntimeError("discovery unavailable")

        with caplog.at_level(logging.WARNING):
            assert _mcp_request_scope._rebuilt_for_this_turn(
                [FakeMcpTool("portal")], failed, fake_hosted_skills.runner
            ) == []
        assert "Could not rebuild" in caplog.text

    def test_servers_missing_from_rediscovery_are_left_out(self, fake_hosted_skills, caplog):
        portal, removed = FakeMcpTool("portal"), FakeMcpTool("removed")
        with caplog.at_level(logging.WARNING):
            rebuilt = _mcp_request_scope._rebuilt_for_this_turn(
                [portal, removed],
                lambda root: MCPDiscoveryResult({"portal": portal}, []),
                fake_hosted_skills.runner,
            )
        assert rebuilt == [portal]
        assert "removed" in caplog.text

    @pytest.mark.parametrize("tools", [None, []], ids=["unspecified", "disabled"])
    def test_unspecified_and_disabled_tools_are_preserved(self, fake_hosted_skills, tools):
        def unexpected_discovery(root):
            pytest.fail("Unspecified or disabled tools must not trigger rebuilding")

        assert _mcp_request_scope._rebuilt_for_this_turn(
            tools, unexpected_discovery, fake_hosted_skills.runner
        ) == tools

    @pytest.mark.asyncio
    async def test_a_turn_that_cannot_rebuild_its_servers_never_uses_the_startup_ones(
        self, fake_hosted_skills
    ):
        # Start-up sessions are shared and can carry another request's X-MS-Hook-Invocation.
        azure_functions_agents_integration.enable()
        startup = fake_hosted_skills.capabilities.filtered_mcp_tools
        fake_hosted_skills.state.servers = {}
        seen = []
        await self.chat(fake_hosted_skills, seen)(FakeRequest(headers=FORWARD))
        assert startup
        assert seen == [[]]

    @pytest.mark.asyncio
    async def test_teardown_failure_is_logged_and_does_not_fail_the_turn(self, caplog):
        class Broken(FakeMcpTool):
            async def close(self):
                raise RuntimeError("cannot close session")

        tool = Broken("portal")
        scope = _mcp_request_scope.RequestScope("chain")
        scope.adopt()
        scope.register([tool])
        with caplog.at_level(logging.WARNING):
            await scope.close()
        assert "could not be closed" in caplog.text
        assert tool._httpx_client.closed
        assert hooks.hook_invocation() is None


class TestMcpAuthentication:
    OURS = {"url": "https://app.example.invalid/.ai/mcp/portal", "auth": {"scope": "s"}}
    THEIRS = {"url": "https://external.invalid/mcp", "auth": {"scope": "s"}}

    @pytest.mark.parametrize("method", [None, "managed_identity", "custom"])
    def test_configured_transport_authentication_is_unchanged(self, fake_hosted_skills, method):
        azure_functions_agents_integration.enable()
        server = {"auth": {"scope": "s"}}
        if method is not None:
            server["auth"]["type"] = method
        original = json.loads(json.dumps(server))
        provider = fake_hosted_skills.mcp._build_header_provider(server)
        assert fake_hosted_skills.mcp.seen_auth == [method or "managed_identity"]
        assert provider({})["Authorization"] == f"application:{method or 'managed_identity'}"
        assert server == original

    def test_callback_chain_reaches_only_our_own_endpoint(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        ours = fake_hosted_skills.mcp._build_header_provider(self.OURS)
        theirs = fake_hosted_skills.mcp._build_header_provider(self.THEIRS)
        with hooks._use_hook_invocation("signed-chain"):
            assert ours({})["X-MS-Hook-Invocation"] == "signed-chain"
            assert "X-MS-Hook-Invocation" not in theirs({})
        assert ours({}) == {"Authorization": "application:managed_identity"}

    def test_an_anonymous_external_server_keeps_no_provider(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        assert fake_hosted_skills.mcp._build_header_provider({"url": "https://external.invalid/mcp"}) is None
        assert fake_hosted_skills.mcp.seen_auth == []

    def test_an_anonymous_same_site_server_only_gets_the_chain(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        provider = fake_hosted_skills.mcp._build_header_provider({"url": self.OURS["url"]})
        with hooks._use_hook_invocation("signed-chain"):
            assert provider({}) == {"X-MS-Hook-Invocation": "signed-chain"}
        assert provider({}) == {}
        assert fake_hosted_skills.mcp.seen_auth == []

    @pytest.mark.parametrize("url", [OURS["url"], THEIRS["url"]])
    def test_static_headers_are_not_mutated(self, fake_hosted_skills, url):
        static = {"X-Api-Key": "key"}
        fake_hosted_skills.mcp._build_header_provider = lambda server: lambda ctx: static
        azure_functions_agents_integration.enable()
        provider = fake_hosted_skills.mcp._build_header_provider({"url": url})
        with hooks._use_hook_invocation("chain"):
            assert provider({})["X-Api-Key"] == "key"
        assert static == {"X-Api-Key": "key"}

    def test_the_host_is_derived_from_the_mcp_base_url(self, fake_hosted_skills, monkeypatch):
        monkeypatch.delenv("APP_BASE_URL", raising=False)
        monkeypatch.setenv("MCP_BASE_URL", self.OURS["url"])
        azure_functions_agents_integration.enable()
        provider = fake_hosted_skills.mcp._build_header_provider(self.OURS)
        with hooks._use_hook_invocation("chain"):
            assert provider({})["X-MS-Hook-Invocation"] == "chain"

    @pytest.mark.parametrize("server", [OURS, THEIRS])
    def test_a_provider_is_built_once_per_server_configuration(self, fake_hosted_skills, server):
        azure_functions_agents_integration.enable()
        for _ in range(5):
            fake_hosted_skills.mcp._build_header_provider(server)
        assert fake_hosted_skills.mcp.seen_auth == ["managed_identity"]

    def test_different_servers_and_scopes_get_separate_providers(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        for server in (self.OURS, self.THEIRS, {**self.OURS, "auth": {"scope": "other"}}):
            fake_hosted_skills.mcp._build_header_provider(server)
        assert fake_hosted_skills.mcp.seen_auth == ["managed_identity"] * 3

    def test_the_shared_provider_keeps_the_chain_request_scoped(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        provider = fake_hosted_skills.mcp._build_header_provider(self.OURS)
        for chain in ("first", "second"):
            with hooks._use_hook_invocation(chain):
                assert provider({})["X-MS-Hook-Invocation"] == chain
        assert "X-MS-Hook-Invocation" not in provider({})
        assert fake_hosted_skills.mcp.seen_auth == ["managed_identity"]

    def test_repatching_does_not_reuse_an_old_builders_providers(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        fake_hosted_skills.mcp._build_header_provider(self.OURS)
        rebuilt = []

        def new_builder(server):
            rebuilt.append(server)
            return lambda ctx: {"Authorization": "new-application"}

        fake_hosted_skills.mcp._build_header_provider = new_builder
        _mcp_request_scope._patch_mcp_header_provider()
        provider = fake_hosted_skills.mcp._build_header_provider(self.OURS)
        assert rebuilt == [self.OURS]
        assert provider({})["Authorization"] == "new-application"

    @pytest.mark.parametrize(
        "url",
        [
            "http://app.example.invalid/.ai/mcp/portal",
            "https://app.example.invalid:8443/.ai/mcp/portal",
            "https://app.example.invalid.attacker.invalid/mcp",
            "not a url",
        ],
    )
    def test_only_the_configured_origin_receives_the_chain(self, fake_hosted_skills, url):
        azure_functions_agents_integration.enable()
        provider = fake_hosted_skills.mcp._build_header_provider({"url": url, "auth": {"scope": "s"}})
        with hooks._use_hook_invocation("signed-chain"):
            assert "X-MS-Hook-Invocation" not in provider({})

    def test_an_explicit_default_port_is_the_same_origin(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        server = {"url": "https://APP.example.invalid:443/.ai/mcp/portal"}
        provider = fake_hosted_skills.mcp._build_header_provider(server)
        with hooks._use_hook_invocation("signed-chain"):
            assert provider({}) == {"X-MS-Hook-Invocation": "signed-chain"}

    def test_a_server_without_a_url_receives_no_chain(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        provider = fake_hosted_skills.mcp._build_header_provider({"auth": {"scope": "s"}})
        with hooks._use_hook_invocation("signed-chain"):
            assert "X-MS-Hook-Invocation" not in provider({})

    def test_an_unconfigured_site_sends_the_chain_nowhere(self, fake_hosted_skills, monkeypatch, caplog):
        # With no base URL set, there's no safe way to tell this site from a third party.
        monkeypatch.delenv("APP_BASE_URL")
        monkeypatch.delenv("MCP_BASE_URL", raising=False)
        azure_functions_agents_integration.enable()
        with caplog.at_level(logging.WARNING):
            ours = fake_hosted_skills.mcp._build_header_provider(self.OURS)
            theirs = fake_hosted_skills.mcp._build_header_provider(self.THEIRS)
        with hooks._use_hook_invocation("signed-chain"):
            assert "X-MS-Hook-Invocation" not in ours({})
            assert "X-MS-Hook-Invocation" not in theirs({})
        assert caplog.text.count("APP_BASE_URL") == 1

    @pytest.mark.parametrize(
        "site",
        ["app.example.invalid", "ftp://app.example.invalid", "https://app.example.invalid:x"],
    )
    def test_an_invalid_site_origin_is_reported_rather_than_ignored(
        self, fake_hosted_skills, monkeypatch, caplog, site
    ):
        monkeypatch.setenv("APP_BASE_URL", site)
        azure_functions_agents_integration.enable()
        with caplog.at_level(logging.WARNING):
            provider = fake_hosted_skills.mcp._build_header_provider(self.OURS)
        with hooks._use_hook_invocation("signed-chain"):
            assert "X-MS-Hook-Invocation" not in provider({})
        assert "APP_BASE_URL" in caplog.text

    def test_a_unicode_host_matches_its_ascii_form(self, fake_hosted_skills, monkeypatch):
        monkeypatch.setenv("APP_BASE_URL", "https://täst.example")
        azure_functions_agents_integration.enable()
        provider = fake_hosted_skills.mcp._build_header_provider({"url": "https://xn--tst-qla.example/mcp"})
        with hooks._use_hook_invocation("signed-chain"):
            assert provider({}) == {"X-MS-Hook-Invocation": "signed-chain"}

    @pytest.mark.asyncio
    async def test_a_unicode_host_is_not_refused_its_own_requests(self, fake_hosted_skills, monkeypatch):
        httpx = pytest.importorskip("httpx")
        monkeypatch.setenv("APP_BASE_URL", "https://täst.example")
        azure_functions_agents_integration.enable()
        server = {"url": "https://täst.example/mcp"}
        client = fake_hosted_skills.mcp._build_http_client(fake_hosted_skills.mcp._build_header_provider(server))
        [stay_on_origin] = client.event_hooks["request"]
        await stay_on_origin(httpx.Request("GET", "https://täst.example/mcp"))

    @pytest.mark.asyncio
    async def test_a_same_site_client_refuses_to_leave_the_site(self, fake_hosted_skills):
        httpx = pytest.importorskip("httpx")
        azure_functions_agents_integration.enable()
        client = fake_hosted_skills.mcp._build_http_client(fake_hosted_skills.mcp._build_header_provider(self.OURS))
        [stay_on_origin] = client.event_hooks["request"]

        await stay_on_origin(httpx.Request("GET", "https://app.example.invalid/.ai/mcp/next"))
        with pytest.raises(httpx.RequestError, match="refused to follow a redirect"):
            await stay_on_origin(httpx.Request("GET", "https://external.invalid/collect"))

    def test_an_anonymous_same_site_server_still_gets_a_guarded_client(self, fake_hosted_skills):
        pytest.importorskip("httpx")
        azure_functions_agents_integration.enable()
        provider = fake_hosted_skills.mcp._build_header_provider({"url": self.OURS["url"]})
        client = fake_hosted_skills.mcp._build_http_client(provider)
        assert len(client.event_hooks["request"]) == 1

    def test_other_clients_are_left_as_the_runtime_built_them(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        theirs = fake_hosted_skills.mcp._build_http_client(fake_hosted_skills.mcp._build_header_provider(self.THEIRS))
        assert theirs.event_hooks["request"] == []
        anonymous = {"url": "https://external.invalid/mcp"}
        provider = fake_hosted_skills.mcp._build_header_provider(anonymous)
        assert fake_hosted_skills.mcp._build_http_client(provider) is None


class TestMcpConnectionGuards:
    def test_the_tools_the_runtime_builds_are_guarded(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        tool, error = fake_hosted_skills.mcp._build_mcp_tool("portal", {})
        assert error is None
        assert isinstance(tool, FakeMcpTool)
        assert type(tool).__name__ == "GuardedFakeMcpTool"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403, 500])
    async def test_an_unreachable_server_does_not_take_down_other_tools(
        self, fake_hosted_skills, caplog, status
    ):
        azure_functions_agents_integration.enable()
        failure = RuntimeError(
            "Failed to enter context manager.",
            ExceptionGroup("TaskGroup failed", [RuntimeError(f"HTTP {status}")]),
        )
        ours, _ = fake_hosted_skills.mcp._build_mcp_tool("portal", {"failure": failure})
        theirs, _ = fake_hosted_skills.mcp._build_mcp_tool("external", {})
        with caplog.at_level(logging.WARNING):
            async with asyncio.TaskGroup() as group:
                group.create_task(ours.__aenter__())
                group.create_task(theirs.__aenter__())
        assert ours.functions == []
        assert len(theirs.functions) == 1
        assert "portal" in caplog.text
        assert "without its tools" in caplog.text
        assert "external" not in caplog.text
        await ours.__aexit__(None, None, None)
        assert ours.closed == 0

    @pytest.mark.asyncio
    async def test_reachable_servers_connect_and_close_normally(self, fake_hosted_skills):
        azure_functions_agents_integration.enable()
        tool, _ = fake_hosted_skills.mcp._build_mcp_tool("external", {})
        async with tool as entered:
            assert entered is tool
            assert len(tool.functions) == 1
        assert tool.closed == 1
