"""Tests for editing a request in flight and rendering the answer."""

from __future__ import annotations

import json

import pytest

from app_service_extensions import HookRequest


def build(body='{"amount":"16 dollars","merchant":"uber"}', query="", **kwargs):
    base = {
        "X-MS-Hook-Name": "expense-patcher-intercept",
        "X-MS-Hook-Mode": "intercept",
        "X-MS-Hook-Path": "/api/expenses",
        "X-MS-Hook-Route": "/api/expenses",
        "X-Service-Token": "SECRET",
        "x-functions-key": "KEY",
        "User-Agent": "Mozilla/5.0",
    }
    base.update(kwargs.pop("headers", {}))
    return HookRequest.from_http(
        method="POST",
        headers=base,
        body=body,
        query=query,
        content_type="application/json",
    ).edit()


def edits_of(request, reason=None):
    return request.to_dict(reason).get("edits", [])


class TestReading:
    """The view shows the request the app will serve, not the forward the handler received."""

    @pytest.mark.parametrize(
        "hidden", ["X-Service-Token", "x-functions-key", "X-MS-Example", "MWH-Example"]
    )
    def test_private_headers_are_not_exposed(self, hidden):
        request = HookRequest(headers={hidden: "secret-value"}).edit()
        assert hidden not in request.headers

    def test_injected_hook_headers_are_not_exposed(self):
        assert "X-MS-Hook-Name" not in build().headers

    def test_forwarded_client_headers_are_exposed(self):
        assert build().headers["User-Agent"] == "Mozilla/5.0"

    def test_get_header_is_case_insensitive(self):
        assert build().get_header("user-agent") == "Mozilla/5.0"

    def test_get_header_default(self):
        assert build().get_header("X-Absent", "fallback") == "fallback"

    def test_query(self):
        request = build(query="q=Karen&debug=1")
        assert request.query == {"q": "Karen", "debug": "1"}
        assert request.get_query("q") == "Karen"

    def test_body_is_the_raw_string(self):
        assert build().body == '{"amount":"16 dollars","merchant":"uber"}'

    def test_headers_is_a_copy(self):
        request = build()
        request.headers["X-Sneaky"] = "1"
        assert "X-Sneaky" not in request.headers
        assert not request.changed

    def test_path_and_method(self):
        request = build()
        assert request.path == "/api/expenses"
        assert request.method == "POST"

    def test_original_request_is_reachable(self):
        assert build().request.hook == "expense-patcher-intercept"

    def test_reading_is_not_changing(self):
        request = build()
        _ = request.headers, request.query, request.body, request.path
        assert request.action == "passthrough"


class TestEditing:
    def test_set_header(self):
        assert edits_of(build().set_header("X-Normalized", "true")) == [
            {"op": "set", "target": "header", "name": "X-Normalized", "value": "true"}
        ]

    def test_remove_header_carries_no_value(self):
        edit = edits_of(build().remove_header("User-Agent"))[0]
        assert edit == {"op": "remove", "target": "header", "name": "User-Agent"}
        assert "value" not in edit

    @pytest.mark.parametrize(
        "header",
        [
            "Authorization",
            " cookie ",
            "X_MS_Example",
            "mWh-Example",
            " MWH_Example ",
            "X-MS-Client-Request-Id",
            "X-Forwarded-Host",
            "X-MS-Hook-Name",
            "Host",
            "Content-Length",
            "DISGUISED-HOST",
        ],
    )
    @pytest.mark.parametrize("method", ["set_header", "remove_header"])
    def test_protected_headers_cannot_be_edited(self, method, header):
        request = build()
        args = (header,) if method == "remove_header" else (header, "forged")
        with pytest.raises(ValueError, match="does not allow hooks to edit"):
            getattr(request, method)(*args)
        assert not request.touched

    @pytest.mark.parametrize(
        "header", ["X-Custom-Token", "X-MSish-Example", "MWHish-Example", "Accept"]
    )
    def test_ordinary_headers_remain_editable(self, header):
        assert edits_of(build().set_header(header, "1"))[0]["name"] == header

    def test_set_query(self):
        assert edits_of(build().set_query("q", "Karen")) == [
            {"op": "set", "target": "query", "name": "q", "value": "Karen"}
        ]

    def test_remove_query(self):
        assert edits_of(build().remove_query("debug")) == [
            {"op": "remove", "target": "query", "name": "debug"}
        ]

    def test_path_edits_are_not_offered(self):
        # Hooks never allows path edits, so the method shouldn't exist.
        assert not hasattr(build(), "set_path")

    def test_replace_body_with_an_object(self):
        # Hooks only understands "set" for the body, not "replace".
        assert edits_of(build().replace_body({"amount": 16.0})) == [
            {"op": "set", "target": "body", "value": {"amount": 16.0}}
        ]

    def test_replace_body_keeps_the_value_untouched(self):
        # Only headers and query values are sent as strings.
        assert edits_of(build().replace_body({"n": 1}))[0]["value"] == {"n": 1}

    def test_replace_body_with_content_type(self):
        edit = edits_of(build().replace_body("plain", content_type="text/plain"))[0]
        assert edit["value"] == "plain"
        assert edit["contentType"] == "text/plain"

    def test_header_values_are_coerced_to_strings(self):
        assert edits_of(build().set_header("X-Count", 3))[0]["value"] == "3"

    def test_edits_chain_and_keep_their_order(self):
        request = build()
        result = request.set_header("X-A", "1").remove_query("debug").replace_body({"ok": True})
        assert result is request
        assert [edit["target"] for edit in edits_of(request)] == ["header", "query", "body"]

    def test_repeated_edits_are_all_recorded(self):
        # The middleware applies them in order, so the last one wins.
        request = build().set_header("X-A", "1").set_header("X-A", "2")
        assert [edit["value"] for edit in edits_of(request)] == ["1", "2"]

    def test_editing_does_not_alter_the_view(self):
        request = build().set_header("X-New", "1").replace_body({"clean": True})
        assert "X-New" not in request.headers
        assert request.body == '{"amount":"16 dollars","merchant":"uber"}'


class TestValidation:
    """The middleware fails open, so a bad edit is silent. These make it loud."""

    @pytest.mark.parametrize(
        ("method", "args"),
        [
            ("set_header", ("", "value")),
            ("remove_header", ("",)),
            ("set_query", ("", "value")),
            ("remove_query", ("",)),
            ("set_header", ("  ", "value")),
        ],
    )
    def test_missing_name_raises(self, method, args):
        with pytest.raises(ValueError, match="needs a name"):
            getattr(build(), method)(*args)

    @pytest.mark.parametrize(
        ("method", "args"),
        [
            ("set_header", ("X-A", None)),
            ("set_query", ("q", None)),
            ("replace_body", (None,)),
        ],
    )
    def test_missing_value_raises(self, method, args):
        # A JSON null reads as "no value" to the middleware and the edit is dropped in silence.
        with pytest.raises(ValueError, match="needs a value"):
            getattr(build(), method)(*args)

    def test_a_failed_edit_is_not_recorded(self):
        request = build()
        with pytest.raises(ValueError):
            request.set_header("X-A", None)
        assert not request.changed


class TestPassthrough:
    def test_an_untouched_request_passes_through(self):
        assert build().to_dict() == {"action": "passthrough"}

    def test_reason_survives(self):
        assert build().to_dict("already clean")["reason"] == "already clean"

    def test_no_edits_key_when_passing_through(self):
        assert "edits" not in build().to_dict()

    def test_passthrough_discards_edits(self):
        request = build().set_header("X-A", "1").replace_body({"n": 1})
        request.passthrough()
        assert request.to_dict("changed my mind") == {
            "action": "passthrough",
            "reason": "changed my mind",
        }

    def test_passthrough_cancels_respond(self):
        assert build().respond(403).passthrough().action == "passthrough"

    def test_an_edit_after_passthrough_starts_a_new_modification(self):
        request = build().set_header("X-A", "1").passthrough().set_header("X-B", "2")
        assert request.to_dict() == {
            "action": "modify",
            "edits": [{"op": "set", "target": "header", "name": "X-B", "value": "2"}],
        }


class TestRespond:
    def test_status_and_body(self):
        rendered = build().respond(403, {"error": "denied"}).to_dict("over limit")
        assert rendered["action"] == "respond"
        assert rendered["reason"] == "over limit"
        assert rendered["response"] == {"status": 403, "body": {"error": "denied"}}

    def test_defaults_to_200(self):
        assert build().respond().to_dict()["response"] == {"status": 200}

    def test_headers_and_content_type(self):
        response = (
            build()
            .respond(body="hi", headers={"X-Source": "agent"}, content_type="text/plain")
            .to_dict()["response"]
        )
        assert response["headers"] == {"X-Source": "agent"}
        assert response["contentType"] == "text/plain"

    def test_respond_wins_over_edits(self):
        # The app never runs, so pending edits to its request cannot matter.
        rendered = build().set_header("X-A", "1").respond(403).to_dict()
        assert rendered["action"] == "respond"
        assert "edits" not in rendered

    def test_edits_are_empty_when_responding(self):
        assert build().set_header("X-A", "1").respond(403).edits() == []

    def test_an_edit_after_responding_is_refused(self):
        # After respond(), the app never sees the request, so an edit would be lost.
        request = build().respond(403)
        with pytest.raises(ValueError, match="call passthrough"):
            request.set_header("X-A", "1")
        assert request.to_dict() == {"action": "respond", "response": {"status": 403}}

    def test_passthrough_reopens_editing_after_a_response(self):
        request = build().respond(403).passthrough().set_query("q", "1")
        assert request.action == "modify"


class TestSerialization:
    def test_action_reflects_what_was_done(self):
        assert build().action == "passthrough"
        assert build().set_header("X-A", "1").action == "modify"
        assert build().respond(204).action == "respond"

    def test_to_json_is_the_bare_answer(self):
        # The middleware parses the response body directly; it is not wrapped in an envelope.
        rendered = json.loads(build().set_header("X-A", "1").to_json("r"))
        assert set(rendered) == {"action", "reason", "edits"}

    def test_json_round_trips(self):
        request = build().replace_body({"a": [1, 2]})
        assert json.loads(request.to_json("r")) == request.to_dict("r")

    def test_non_serializable_body_degrades_to_string(self):
        class Odd:
            def __str__(self):
                return "odd"

        rendered = json.loads(build().replace_body(Odd()).to_json())
        assert rendered["edits"][0]["value"] == "odd"

    def test_edits_returns_a_copy(self):
        request = build().set_header("X-A", "1")
        request.edits().append({"op": "set"})
        assert len(request.edits()) == 1
