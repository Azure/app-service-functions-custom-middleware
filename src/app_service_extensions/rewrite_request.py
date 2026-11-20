"""The outbound side: editing the in-flight request, deciding, and serializing the patch."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .hooks import (
    HOOK_HEADER_PREFIX,
    HookRequest,
    _is_sensitive,
    _normalized,
    _without_credentials,
)

# What the middleware will do with the request.
ACTION_PASSTHROUGH = "passthrough"
ACTION_MODIFY = "modify"
ACTION_RESPOND = "respond"

# Edit operations. Hooks allows set/remove on headers and query values, but only set on the body.
OP_SET = "set"
OP_REMOVE = "remove"

# Edit targets. No path target: Hooks never allows path edits.
TARGET_QUERY = "query"
TARGET_HEADER = "header"
TARGET_BODY = "body"

#: Connection and message framing (ImmutableRequestHeaders).
_IMMUTABLE_REQUEST_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "forwarded",
        "host",
        "keep-alive",
        "set-cookie",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

#: Other routing and platform headers that cannot be edited.
_PLATFORM_HEADERS = frozenset(
    {
        "client-ip",
        "disguised-host",
        "scm-deployment-id",
        "was-default-hostname",
        "x-appservice-proto",
        "x-azure-fdid",
        "x-azure-ja4-fingerprint",
        "x-client-ip",
        "x-client-port",
        "x-fd-healthprobe",
        "x-site-deployment-id",
        "x-waws-unencoded-url",
    }
)

_PLATFORM_HEADER_PREFIXES = (
    HOOK_HEADER_PREFIX.lower(),
    "x-arr-",
    "x-forwarded-",
    "x-original-",
)


def _is_protected_request_header(name: str) -> bool:
    """True if the extension rejects an edit to this request header."""
    normalized = _normalized(name)
    return (
        normalized in _IMMUTABLE_REQUEST_HEADERS
        or normalized in _PLATFORM_HEADERS
        or normalized.startswith(_PLATFORM_HEADER_PREFIXES)
        or _is_sensitive(normalized)
    )


@dataclass
class HookResponse:
    """The response an intercept handler authors when it takes over the request.

    Attributes:
        status: HTTP status. Outside 100-599 the middleware falls back to 200.
        headers: Response headers. Transport headers are dropped by the writer.
        body: A string is used verbatim; anything else is serialized as JSON.
        content_type: Stamped on the body. Defaults to JSON when the body is not a verbatim string.
    """

    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    content_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Render as the ``response`` object the middleware expects, omitting anything unset."""
        spec: dict[str, Any] = {"status": self.status}
        if self.headers:
            spec["headers"] = dict(self.headers)
        if self.body is not None:
            spec["body"] = self.body
        if self.content_type:
            spec["contentType"] = self.content_type
        return spec


class EditableRequest:
    """A request in flight, and the answer a handler gives about it."""

    def __init__(self, request: HookRequest) -> None:
        self._request = request
        self._edits: list[dict[str, Any]] = []
        self._response: HookResponse | None = None
        self._declined = False

    # getters

    @property
    def request(self) -> HookRequest:
        """The request as it arrived."""
        return self._request

    @property
    def path(self) -> str | None:
        """The path the client requested."""
        return self._request.path

    @property
    def method(self) -> str | None:
        """The client's HTTP method."""
        return self._request.method

    @property
    def content_type(self) -> str | None:
        """The body's content type."""
        return self._request.content_type

    @property
    def body(self) -> str | None:
        """The body exactly as sent."""
        return self._request.body

    @property
    def headers(self) -> dict[str, str]:
        """The filtered request headers. A copy -- change one with :meth:`set_header`."""
        return {
            name: value
            for name, value in _without_credentials(self._request.headers).items()
            if not name.lower().startswith(HOOK_HEADER_PREFIX.lower())
        }

    @property
    def query(self) -> dict[str, str]:
        """The query parameters."""
        return self._request.query_params

    def get_header(self, name: str, default: str | None = None) -> str | None:
        """Read one header, matched case-insensitively."""
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return default

    def get_query(self, name: str, default: str | None = None) -> str | None:
        """Read one query parameter."""
        return self.query.get(name, default)

    # setters

    def set_header(self, name: str, value: Any) -> EditableRequest:
        """Upsert a header: set it when absent, overwrite it when present."""
        return self._edit(OP_SET, TARGET_HEADER, name=name, value=value)

    def remove_header(self, name: str) -> EditableRequest:
        """Remove a header. Removing one that is not there is not an error."""
        return self._edit(OP_REMOVE, TARGET_HEADER, name=name)

    def set_query(self, name: str, value: Any) -> EditableRequest:
        """Upsert a query parameter."""
        return self._edit(OP_SET, TARGET_QUERY, name=name, value=value)

    def remove_query(self, name: str) -> EditableRequest:
        """Remove a query parameter."""
        return self._edit(OP_REMOVE, TARGET_QUERY, name=name)

    def replace_body(self, value: Any, content_type: str | None = None) -> EditableRequest:
        """Replace the whole body, re-stamping its content type."""
        return self._edit(OP_SET, TARGET_BODY, value=value, content_type=content_type)

    def _edit(
        self,
        op: str,
        target: str,
        name: str | None = None,
        value: Any = None,
        content_type: str | None = None,
    ) -> EditableRequest:
        """Record one edit after checking the middleware would act on it."""
        if self._response is not None:
            # After respond(), the app never sees the request, so this edit would be lost.
            raise ValueError(
                "a direct response has already been chosen; call passthrough() before editing the "
                "request"
            )
        if target in (TARGET_HEADER, TARGET_QUERY):
            if not name or not name.strip():
                raise ValueError(f"'{op}' on '{target}' needs a name")
            if target == TARGET_HEADER and _is_protected_request_header(name):
                raise ValueError(f"the extension does not allow hooks to edit the '{name}' header")
        if op != OP_REMOVE and value is None:
            # A JSON null reads as no value at all, and the edit is dropped.
            raise ValueError(f"'{op}' on '{target}' needs a value")

        edit: dict[str, Any] = {"op": op, "target": target}
        if name:
            edit["name"] = name
        if op != OP_REMOVE:
            # Headers and query values are sent as strings; a body is sent as given.
            edit["value"] = value if target == TARGET_BODY else str(value)
        if content_type:
            edit["contentType"] = content_type

        self._edits.append(edit)
        # An edit after passthrough() cancels it: the agent changed its mind.
        self._declined = False
        return self

    def respond(
        self,
        status: int = 200,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        content_type: str | None = None,
    ) -> EditableRequest:
        """Answer the request directly, bypassing the app."""
        self._response = HookResponse(
            status=status,
            headers=dict(headers or {}),
            body=body,
            content_type=content_type,
        )
        self._declined = False
        return self

    def passthrough(self) -> EditableRequest:
        """Undo any edits or response so far, and let the app get the original request."""
        self._edits.clear()
        self._response = None
        self._declined = True
        return self

    @property
    def changed(self) -> bool:
        """True when at least one edit has been recorded."""
        return bool(self._edits)

    @property
    def touched(self) -> bool:
        """True when anything at all was decided: an edit, a response, or an explicit passthrough.

        Distinguishes "the handler looked and chose to do nothing" from "the handler never ran",
        which matters when something downstream has to decide whether to use this answer at all.
        """
        return bool(self._edits) or self._response is not None or self._declined

    @property
    def action(self) -> str:
        """What the middleware will do: ``passthrough``, ``modify``, or ``respond``."""
        if self._response is not None:
            return ACTION_RESPOND
        if self._declined or not self._edits:
            return ACTION_PASSTHROUGH
        return ACTION_MODIFY

    def edits(self) -> list[dict[str, Any]]:
        """The edits recorded so far, in the order they were made."""
        return [] if self.action != ACTION_MODIFY else list(self._edits)

    def to_dict(self, reason: str | None = None) -> dict[str, Any]:
        """Render the answer as the patch object the middleware expects.

        ``reason`` is free text the middleware logs and never acts on.
        """
        action = self.action
        patch: dict[str, Any] = {"action": action}
        if reason:
            patch["reason"] = reason
        if action == ACTION_MODIFY:
            patch["edits"] = list(self._edits)
        elif self._response is not None:
            patch["response"] = self._response.to_dict()
        return patch

    def to_json(self, reason: str | None = None) -> str:
        """Render the answer as the JSON an intercept handler returns as its response body.

        The middleware parses that body directly -- it is not wrapped in an envelope or placed under
        a ``response`` key.
        Values whose types JSON cannot encode are converted to strings.
        """
        return json.dumps(self.to_dict(reason), ensure_ascii=False, default=str)
