"""Tools for editing hook requests and calling the application's API."""

from __future__ import annotations

from .app import APP_TOOLS, AppCallError, call_web_app, call_web_app_wrapper
from .request import (
    passthrough,
    remove_header,
    remove_query,
    replace_body,
    respond,
    set_header,
    set_query,
)

#: Editing the in-flight request an intercept hook is deciding about.
REQUEST_TOOLS = [
    set_header,
    remove_header,
    set_query,
    remove_query,
    replace_body,
    respond,
    passthrough,
]

#: Everything an agent gets by default. ``APP_TOOLS`` is deliberately not here: calling any route
#: on the host application with its credentials must be enabled explicitly.
ALL_TOOLS = REQUEST_TOOLS.copy()

__all__ = [
    "ALL_TOOLS",
    "APP_TOOLS",
    "REQUEST_TOOLS",
    "AppCallError",
    "call_web_app",
    "call_web_app_wrapper",
    "passthrough",
    "remove_header",
    "remove_query",
    "replace_body",
    "respond",
    "set_header",
    "set_query",
]
