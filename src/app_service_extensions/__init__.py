"""Building blocks for agents hosted behind an EasyAuth-fronted App Service."""

from __future__ import annotations

from ._function_app import create_function_app
from .auth import (
    AuthError,
    configure,
)
from .hooks import (
    FUNCTION_KEY_HEADER,
    HOOK_HEADER_PREFIX,
    HOOK_INVOCATION_HEADER,
    HOOK_MODE_HEADER,
    HOOK_NAME_HEADER,
    HOOK_ORIGIN_HEADER,
    HOOK_PATH_HEADER,
    HOOK_REQUEST_METHOD_HEADER,
    HOOK_ROUTE_HEADER,
    HookRequest,
    NoHookError,
    current_request,
    from_asgi,
    from_functions,
)
from .rewrite_request import (
    ACTION_MODIFY,
    ACTION_PASSTHROUGH,
    ACTION_RESPOND,
    EditableRequest,
    HookResponse,
)
from .tools.app import AppCallError, call_web_app

__version__ = "1.0.0"

__all__ = [
    "ACTION_MODIFY",
    "ACTION_PASSTHROUGH",
    "ACTION_RESPOND",
    "FUNCTION_KEY_HEADER",
    "HOOK_HEADER_PREFIX",
    "HOOK_INVOCATION_HEADER",
    "HOOK_MODE_HEADER",
    "HOOK_NAME_HEADER",
    "HOOK_ORIGIN_HEADER",
    "HOOK_PATH_HEADER",
    "HOOK_REQUEST_METHOD_HEADER",
    "HOOK_ROUTE_HEADER",
    "AppCallError",
    "AuthError",
    "EditableRequest",
    "HookRequest",
    "HookResponse",
    "NoHookError",
    "__version__",
    "call_web_app",
    "configure",
    "create_function_app",
    "current_request",
    "from_asgi",
    "from_functions",
]
