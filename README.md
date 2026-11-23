# App Service Functions Custom Middleware

Python tools for applications and AI agents that handle App Service Hooks.
The library reads a forwarded hook request and helps a handler pass it through,
modify it, or respond directly. It supports FastAPI, Starlette, Azure Functions,
and integration with the Azure Functions agents runtime.

App Service Hooks must be configured separately to forward requests to your
handler. Protect the handler endpoint so that only authorized callers can
submit hook requests.

## Project status

This project is under active development for preview. APIs and runtime
integrations may change before general availability.

## Installation

Python 3.11 or later is required. Install the base package:

```bash
pip install app-service-functions-custom-middleware
```

The Python import is `app_service_extensions`:

```python
import app_service_extensions as ase
```

Choose the extras needed by your application:

```bash
# For regular Azure Functions handlers
pip install "app-service-functions-custom-middleware[functions]"

# For integration with the Azure Functions agents runtime
pip install "app-service-functions-custom-middleware[hosted-skills]"

# For managed identity with ase.call_web_app()
pip install "app-service-functions-custom-middleware[identity]"
```

Install FastAPI or Starlette separately if your application uses either one.
The `hosted-skills` extra installs the official
[`azurefunctions-agents-runtime`](https://github.com/Azure/azure-functions-agents-runtime)
package. The runtime is in public preview; this integration uses a narrow
runtime version range because it relies on APIs that are not yet public.

## Handle an intercept hook

Call `ase.configure(app)` to attach the request adapter. Inside a forwarded
hook request, `ase.current_request()` returns an editable view of the original
request. Return its serialized patch from an intercept handler.

### FastAPI

Install FastAPI in addition to the base package:

```bash
pip install fastapi
```

```python
from fastapi import FastAPI
import app_service_extensions as ase

app = FastAPI()
ase.configure(app)

@app.post("/hooks/review")
async def review():
    request = ase.current_request()
    request.set_header("X-Reviewed", "true")
    return request.to_dict()
```

Configure an intercept hook to forward requests to `/hooks/review`. Calling this
endpoint directly without the hook headers raises `ase.NoHookError`; ordinary
application routes continue to receive their original requests. Starlette
applications can use the same `ase.configure(app)` and request API.

### Azure Functions

Install the `[functions]` extra, then configure the Function app **before**
registering the handler:

```python
import azure.functions as func
import app_service_extensions as ase

app = func.FunctionApp()
ase.configure(app)

@app.route(route="hooks/review", methods=["POST"])
def review(req: func.HttpRequest) -> func.HttpResponse:
    request = ase.current_request()
    request.set_header("X-Reviewed", "true")
    return func.HttpResponse(request.to_json(), mimetype="application/json")
```

Configure the hook to reach the deployed route, taking the Function app's route
prefix and authentication settings into account.

### Editing and answering

An editable request provides the original method, path, filtered headers, query,
and body, as well as methods to set or remove headers and query parameters, replace
the body, `respond()`, or `passthrough()`. Return `to_dict()` or `to_json()` as
appropriate for your framework.

Hooks do not allow edits to the request path or protected headers, including
credentials and platform-controlled headers. The extension also treats all
`x-ms-*` and `mwh-*` headers as reserved: it hides them from
`current_request().headers` and agent prompts, and rejects edits to them.
Hook metadata and the callback chain are still read from the raw incoming headers.

Attempting to edit a protected header raises `ValueError`. After `respond()`,
call `passthrough()` before editing again; it clears the response and any earlier
edits.

## Azure Functions agents integration

The [Azure Functions agents runtime](https://github.com/Azure/azure-functions-agents-runtime)
supports agents defined in `.agent.md` files, including HTTP-triggered agents
and hosted skills. Follow its [documentation](https://azure.github.io/azure-functions-agents-runtime/)
to configure an agent, model, and Function app.

With the `[hosted-skills]` extra installed, configure this library before the
runtime creates the Function app, then attach it to the generated app:

```python
import app_service_extensions as ase
from azure_functions_agents import create_function_app

ase.configure()
app = create_function_app()
ase.configure(app)
```

Alternatively, the convenience factory performs the same sequence:

```python
import app_service_extensions as ase

app = ase.create_function_app()
```

Use one initialization style, not both. The integration is enabled
automatically when the runtime is installed; ordinary Function handlers remain
ordinary handlers. The `[hosted-skills]` extra supports
`azurefunctions-agents-runtime` `0.1.0b7` through `0.1.0b17` with the Agent
Framework backend, for agent HTTP triggers and the built-in chat and streaming
chat routes. The Copilot SDK backend and the `@app.hosted_skill` binding are
not supported as hook handlers by this integration.

For an intercept hook, the integration supplies the
forwarded request to the agent and provides tools to edit it, pass it through,
or respond. The built-in tools do not automatically expose `ase.call_web_app()` to
the agent.

Mirror hooks supply a `Mirrored HTTP request` prompt to the agent without converting
its reply into a request patch. Mirrors observe the original request; their replies
cannot modify or replace the application's response.

For an agent that handles intercept hooks, add this guidance to the Markdown
body of its `.agent.md` file, below the YAML front matter:

> For intercept requests, use the hook tools to record your decision. If the
> request should continue unchanged, call `passthrough()`. If it needs changes,
> use the edit tools. If it needs a direct response, call `respond()`. A prose
> reply alone does not count as a decision.

The extension does not add these instructions automatically. If the intercept
handler does not return a valid patch, App Service Hooks applies the hook's
configured `failurePolicy`.

## Authentication and callbacks

App Service Authentication is not required by the library's hook-handling API.
Authentication for the app, the hook endpoint, and outbound calls are separate
concerns: configure access controls appropriate for each.

For Hosted Skills MCP connections, configure authentication per server in
`mcp.json`. The integration preserves the runtime's authentication behavior;
an anonymous connection remains anonymous.

`ase.call_web_app()` is the Python API for calling the application's own API.
It returns response text and raises exceptions on failure. It requires an
application base URL and an application credential. Provide a
zero-argument token provider through `ase.configure(presenter=...)`, or install
the `[identity]` extra and set `MCP_AUTH_SCOPE` for managed identity. Set
`MCP_AUTH_CLIENT_ID` as well when using a user-assigned identity. Calls use
application credentials, not the signed-in user's credentials; restrict the
application's permissions and accessible APIs accordingly.

`app_service_extensions.tools.call_web_app_wrapper()` wraps this API for explicit
registration as an agent tool, returning readable success or error messages.
It is not included in the default agent tools.

Set the application base URL using `ase.configure(base_url=...)`,
`APP_BASE_URL`, or `MCP_BASE_URL`, in that order of precedence. When using
`MCP_BASE_URL`, the library derives the application origin from it.

For callbacks, the library preserves the `X-MS-Hook-Invocation` recursion
chain when one is present. `ase.call_web_app()` sends it to the configured app;
the agents integration sends it to MCP servers only when their scheme, host,
and port match the configured app origin. Configure the base URL when MCP
tools call back into the app so the recursion chain can be forwarded.

`ase.current_request().request.origin` exposes the client-facing site origin
supplied by App Service Hooks when available.

## Third-party dependencies

This repository does not vendor third-party packages. Optional integrations use
`azure-functions`, `azure-identity`, `azurefunctions-agents-runtime`, and
`starlette`, installed separately through the extras described above. Development
and build dependencies include `pytest`, `pytest-asyncio`, and `poetry-core`.
See [pyproject.toml](pyproject.toml) for dependency declarations and version ranges.
These dependencies remain subject to their own licenses.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution requirements and development
instructions. This project has adopted the
[Microsoft Open Source Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Please follow [SECURITY.md](SECURITY.md) to report security vulnerabilities
privately. Do not report security vulnerabilities through public GitHub issues.

## License

This project is licensed under the [MIT License](LICENSE.txt).

## Trademarks

This project may contain trademarks or logos for projects, products, or services.
Authorized use of Microsoft trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not
cause confusion or imply Microsoft sponsorship. Any use of third-party trademarks
or logos are subject to those third-party's policies.

## Repository setup for maintainers

Use the website link in this repository's About section to complete setup and
configure access controls.
