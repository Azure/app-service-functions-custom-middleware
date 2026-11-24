"""Tests for the request-editing tools an agent calls.

These answer a model, so a failure has to read as an outcome it can act on -- a sentence, not an
exception. What they edit is the request the handler will answer with.
"""

from __future__ import annotations

import inspect
import json
import logging

import pytest

from app_service_extensions import HookRequest, current_request, hooks
from app_service_extensions.tools import (
    ALL_TOOLS,
    REQUEST_TOOLS,
    passthrough,
    remove_header,
    remove_query,
    replace_body,
    respond,
    set_header,
    set_query,
)

FORWARD = {
    "X-MS-Hook-Name": "expense-normalizer",
    "X-MS-Hook-Mode": "intercept",
    "X-MS-Hook-Path": "/api/expenses",
}


@pytest.fixture
def held():
    """A forward being handled, as the Hosted Skills runtime wrapper would have left it."""
    hook = HookRequest.from_http(
        method="POST",
        headers=dict(FORWARD),
        body='{"amount":"16 dollars","merchant":"uber"}',
        query="debug=1",
        content_type="application/json",
    )
    editable = hooks.hold_request(hook)
    yield editable
    hooks._current.set(None)


@pytest.fixture
def nothing_held():
    hooks._current.set(None)
    yield
    hooks._current.set(None)


def edits(editable):
    return editable.to_dict().get("edits", [])


class TestEditing:
    def test_set_header(self, held):
        assert "Done" in set_header("X-Category", "Travel")
        assert edits(held) == [
            {"op": "set", "target": "header", "name": "X-Category", "value": "Travel"}
        ]

    def test_remove_header(self, held):
        remove_header("X-Debug")
        assert edits(held) == [{"op": "remove", "target": "header", "name": "X-Debug"}]

    def test_set_query(self, held):
        set_query("q", "Karen")
        assert edits(held) == [{"op": "set", "target": "query", "name": "q", "value": "Karen"}]

    def test_remove_query(self, held):
        remove_query("debug")
        assert edits(held) == [{"op": "remove", "target": "query", "name": "debug"}]

    def test_replace_body_parses_json(self, held):
        # Models are asked for JSON and mostly send it.
        replace_body('{"amount": 16.0, "currency": "USD"}')
        assert edits(held)[0]["value"] == {"amount": 16.0, "currency": "USD"}

    def test_replace_body_keeps_plain_text(self, held):
        # A body that is genuinely not JSON still has to survive.
        replace_body("just some text")
        assert edits(held)[0]["value"] == "just some text"

    def test_several_edits_accumulate(self, held):
        set_header("X-A", "1")
        set_query("q", "x")
        replace_body('{"clean": true}')
        assert [e["target"] for e in edits(held)] == ["header", "query", "body"]


class TestDeciding:
    def test_respond_bypasses_the_app(self, held):
        respond(403, '{"error": "over the approval limit"}')
        answer = held.to_dict("policy")
        assert answer["action"] == "respond"
        assert answer["response"]["status"] == 403
        assert answer["response"]["body"] == {"error": "over the approval limit"}

    def test_respond_without_a_body(self, held):
        respond(204)
        assert held.to_dict()["response"] == {"status": 204}

    def test_passthrough_discards_edits(self, held):
        set_header("X-A", "1")
        passthrough()
        assert held.to_dict() == {"action": "passthrough"}

    def test_passthrough_still_counts_as_a_decision(self, held):
        # "Looked and chose to do nothing" is not the same as "never ran".
        passthrough()
        assert held.touched

    def test_an_edit_after_passthrough_is_applied(self, held):
        # The tool tells the agent "Done", so the edit must really be kept.
        passthrough()
        assert set_header("X-A", "1").startswith("Done")
        assert held.to_dict() == {
            "action": "modify",
            "edits": [{"op": "set", "target": "header", "name": "X-A", "value": "1"}],
        }

    def test_an_edit_after_responding_reads_as_an_outcome(self, held):
        respond(403)
        answer = set_header("X-A", "1")
        assert "could not be made" in answer
        assert "passthrough" in answer
        assert held.to_dict() == {"action": "respond", "response": {"status": 403}}


class TestAnsweringAModel:
    def test_tools_confirm_in_words(self, held):
        assert set_header("X-A", "1").startswith("Done")
        assert "query q" in set_query("q", "v2")

    def test_an_invalid_edit_reads_as_an_outcome(self, held):
        answer = set_header("", "value")
        assert "could not be made" in answer
        assert not held.touched

    @pytest.mark.parametrize("tool", [set_header, set_query])
    def test_invalid_edits_are_logged_without_values(self, held, monkeypatch, caplog, tool):
        error = ValueError("SYNTHETIC-ERROR-DETAIL")

        def invalid(*args, **kwargs):
            raise error

        monkeypatch.setattr(held, tool.__name__, invalid)
        with caplog.at_level(logging.WARNING, logger="azure.functions.AppServiceExtensions"):
            answer = tool("SYNTHETIC-NAME", "SYNTHETIC-VALUE")

        assert answer == f"That edit could not be made: {error}"
        assert not held.touched
        assert caplog.messages == [
            f"Request tool '{tool.__name__}' rejected an edit (ValueError)"
        ]
        assert caplog.records[0].levelno == logging.WARNING
        assert "SYNTHETIC" not in caplog.text

    @pytest.mark.parametrize(
        "header", ["Authorization", "X-MS-Example", "MWH-Example", "X-Forwarded-For"]
    )
    def test_a_protected_header_edit_reads_as_an_outcome(self, held, header):
        answer = set_header(header, "forged")
        assert "could not be made" in answer
        assert not held.touched

    def test_no_request_reads_as_an_outcome(self, nothing_held):
        # A model calling this outside a forward gets a sentence, not a traceback.
        answer = set_header("X-A", "1")
        assert "no in-flight request" in answer

    @pytest.mark.parametrize("mode", ["mirror", "MiRrOr", "proxy", None])
    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            (set_header, ("X-Category", "Travel")),
            (remove_header, ("X-Debug",)),
            (set_query, ("q", "Alex")),
            (remove_query, ("debug",)),
            (replace_body, ('{"changed": true}',)),
            (respond, (403,)),
            (passthrough, ()),
        ],
    )
    def test_request_tools_cannot_decide_non_intercept_hooks(self, nothing_held, mode, tool, args):
        editable = hooks.hold_request(HookRequest(hook="observer", mode=mode))

        answer = tool(*args)

        assert "intercept" in answer.lower()
        assert not answer.startswith("Done")
        assert not editable.touched
        if (mode or "").lower() == "mirror":
            assert "mirror" in answer.lower()
            assert "continues independently" in answer

    def test_an_unrelated_runtime_error_is_not_mistaken_for_a_missing_request(
        self, held, monkeypatch
    ):
        def broken(*args, **kwargs):
            raise RuntimeError("SYNTHETIC-FAILURE")

        monkeypatch.setattr(held, "set_header", broken)
        with pytest.raises(RuntimeError, match="SYNTHETIC-FAILURE"):
            set_header("X-A", "1")

    def test_every_tool_documents_itself(self):
        # The docstring is the schema a model reads. Wording is editorial; having one, and naming
        # any arguments a model has to supply, is not.
        for tool in REQUEST_TOOLS:
            doc = (tool.__doc__ or "").strip()
            assert doc, f"{tool.__name__} has no docstring"
            if inspect.signature(tool).parameters:
                assert "Args:" in doc, f"{tool.__name__} lacks argument docs"

class TestRegistry:
    def test_only_request_editing_tools_are_exposed_by_default(self):
        assert len(REQUEST_TOOLS) == 7
        assert ALL_TOOLS == REQUEST_TOOLS

    def test_path_rewriting_is_not_offered(self):
        # Hooks never allows path edits, so don't offer the tool.
        assert "set_path" not in {tool.__name__ for tool in ALL_TOOLS}

    def test_names_do_not_collide(self):
        names = [tool.__name__ for tool in ALL_TOOLS]
        assert len(names) == len(set(names))


class TestEndToEnd:
    def test_tool_calls_become_the_patch(self, held):
        # What an intercept agent would do, then what the handler answers with.
        set_header("X-Normalized-By", "agent")
        remove_query("debug")
        replace_body('{"amount": 16.0, "merchant": "Uber", "category": "Travel"}')

        answer = json.loads(held.to_json("normalized the expense"))
        assert answer["action"] == "modify"
        assert answer["reason"] == "normalized the expense"
        assert [e["op"] for e in answer["edits"]] == ["set", "remove", "set"]

    def test_the_held_request_is_the_one_the_tools_edit(self, held):
        set_header("X-A", "1")
        assert current_request() is held
