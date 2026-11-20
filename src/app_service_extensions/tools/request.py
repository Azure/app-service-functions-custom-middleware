"""Agent tools that edit, answer, or pass through the hook request being handled."""

from __future__ import annotations

import functools
import json
from typing import Any, Callable

from ..hooks import NoHookError, current_request, logger

#: How an edit tool signs off. The middleware applies what was recorded, then the app runs.
_APPLIED = "The app will serve the request with this applied."


def _answers_in_words(tool: Callable[..., str]) -> Callable[..., str]:
    """Return expected edit failures as text for the model; propagate unexpected errors."""

    @functools.wraps(tool)
    def answering(*args: Any, **kwargs: Any) -> str:
        try:
            return tool(*args, **kwargs)
        except NoHookError:
            return (
                "There is no in-flight request to edit here. This tool only works while handling "
                "a hook."
            )
        except ValueError as err:
            logger.warning(
                "Request tool '%s' rejected an edit (%s)", tool.__name__, type(err).__name__
            )
            return f"That edit could not be made: {err}"
        except RuntimeError as err:
            logger.error("Request tool '%s' failed unexpectedly (%s)", tool.__name__, type(err).__name__)
            raise

    return answering


@_answers_in_words
def set_header(name: str, value: str) -> str:
    """Set a header on the in-flight request.

    Args:
        name: The header name, e.g. "X-Category".
        value: The value to set. Replaces any existing value for that header.
    """
    current_request().set_header(name, value)
    return f"Done - set header {name} to {value!r}. {_APPLIED}"


@_answers_in_words
def remove_header(name: str) -> str:
    """Remove a header from the in-flight request.

    Args:
        name: The header name to remove.
    """
    current_request().remove_header(name)
    return f"Done - removed header {name}. {_APPLIED}"


@_answers_in_words
def set_query(name: str, value: str) -> str:
    """Set a query parameter on the in-flight request.

    Args:
        name: The parameter name, e.g. "q".
        value: The value to set. Replaces any existing value for that parameter.
    """
    current_request().set_query(name, value)
    return f"Done - set query {name} to {value!r}. {_APPLIED}"


@_answers_in_words
def remove_query(name: str) -> str:
    """Remove a query-string parameter from the in-flight request.

    Args:
        name: The parameter name to remove.
    """
    current_request().remove_query(name)
    return f"Done - removed query {name}. {_APPLIED}"


@_answers_in_words
def replace_body(body: str) -> str:
    """Replace the whole body of the in-flight request.

    Args:
        body: The complete replacement body as a JSON string.
    """
    current_request().replace_body(_maybe_json(body))
    return f"Done - replaced the body. {_APPLIED}"


@_answers_in_words
def respond(status: int = 200, body: str = "") -> str:
    """Answer the request yourself, without letting the app handle it at all.

    To edit the request after this, call passthrough first.

    Args:
        status: The HTTP status to return.
        body: The response body as a JSON string, e.g. '{"error": "over the approval limit"}'.
    """
    current_request().respond(status=status, body=_maybe_json(body) if body else None)
    return (
        f"Done - answering directly with {status}. The app will not run, and the caller gets "
        "this answer."
    )


@_answers_in_words
def passthrough() -> str:
    """Let the app serve the original request unchanged.

    Undoes any edits or response so far. Edits made after this are kept.
    """
    current_request().passthrough()
    return "Done - passing through. The app will serve the original request."


def _maybe_json(value: str) -> Any:
    """Parse a model-supplied body as JSON, keeping it as text when it is not."""
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value
