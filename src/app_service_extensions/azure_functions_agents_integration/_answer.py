"""Turning whatever the agent returned into the response the middleware reads."""

from __future__ import annotations

import json
from typing import Any

from . import logger
from ._mcp_request_scope import RequestScope

#: Set from the body we are substituting, so carrying the old values over would misdescribe it.
_BODY_HEADERS = {"content-length", "content-type"}


async def collect(response: Any, scope: RequestScope) -> Any:
    """Drain a streaming response into a regular one, with the request's scope still active."""
    scope.adopt()
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.encode() if isinstance(chunk, str) else bytes(chunk))

    return _response(
        content=b"".join(chunks),
        status_code=getattr(response, "status_code", 200),
        media_type=getattr(response, "media_type", None) or "application/json",
        headers=_carried_headers(response),
    )


def succeeded(response: Any) -> bool:
    """True for a 2xx response. A return value with no status code counts as success."""
    return 200 <= getattr(response, "status_code", 200) < 300


def answer_with(response: Any, editable: Any) -> Any:
    """Replace the agent's answer with the patch its tool calls actually built."""
    try:
        body = editable.to_json()
    except Exception as exc:
        logger.error("Could not build hook response (%s)", type(exc).__name__)
        raise

    # Hooks doesn't log the patch, so log it here, minus values (they may hold secrets or PII).
    logger.info("Hook decision: %s", json.dumps(_decision_summary(editable)))

    return _response(
        content=body,
        status_code=200,
        media_type="application/json",
        headers=_carried_headers(response),
    )


def failed_answer(response: Any, editable: Any) -> Any:
    """Return the failure without the agent's edits, so Hooks applies the hook's failurePolicy."""
    if editable.touched:
        logger.warning(
            "The agent run did not complete, so its hook decision was discarded: %s",
            json.dumps(_decision_summary(editable)),
        )

    if not succeeded(response):
        return response

    # A failed stream still says 200, so answer 500 instead.
    return _response(
        content=json.dumps({"error": "The agent run did not complete."}),
        status_code=500,
        media_type="application/json",
        headers=_carried_headers(response),
    )


def _decision_summary(editable: Any) -> dict[str, Any]:
    """What the patch does (action, and each edit's op, target and name), without any values."""
    decision = editable.to_dict()
    summary: dict[str, Any] = {"action": decision["action"]}
    if "edits" in decision:
        summary["edits"] = [
            {key: edit[key] for key in ("op", "target", "name") if key in edit}
            for edit in decision["edits"]
        ]
    if "response" in decision:
        summary["status"] = decision["response"].get("status")
    return summary


def _response(**kwargs: Any) -> Any:
    """Always a plain Starlette Response, so the patch JSON is sent exactly as written."""
    from starlette.responses import Response

    return Response(**kwargs)


def _carried_headers(response: Any) -> dict[str, str]:
    """The agent's own response headers, minus the ones describing a body we are replacing."""
    try:
        headers = dict(getattr(response, "headers", None) or {})
    except Exception as exc:
        logger.error("Could not copy agent response headers (%s)", type(exc).__name__)
        raise

    return {k: v for k, v in headers.items() if k.lower() not in _BODY_HEADERS}
