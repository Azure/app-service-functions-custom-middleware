"""Tests for call_web_app: reaching the web app's own API with application credentials.

urllib is stubbed at the module boundary, so what is asserted is the request that would go out --
which credentials it carries, where it is pointed, and what comes back from each answer.
"""

from __future__ import annotations

import json
import logging
import urllib.error

import pytest

from app_service_extensions import AppCallError, auth, call_web_app, hooks
from app_service_extensions.tools import app as app_module


@pytest.fixture(autouse=True)
def configured():
    """A web app to call, an application credential, and a callback chain."""
    auth.configure(base_url="https://webapp.example", presenter=lambda: "MI-TOKEN")
    with hooks._use_hook_invocation("HOOK-CHAIN"):
        yield
    auth._base_url = None
    auth._presenter = None


class FakeResponse:
    def __init__(self, body=b"{}", content_type="application/json"):
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def sent(monkeypatch):
    """Capture the outbound request instead of making it."""
    calls = []

    def urlopen(request, timeout=None):
        calls.append(request)
        return calls_response[0]

    calls_response = [FakeResponse()]
    monkeypatch.setattr(app_module._opener, "open", urlopen)

    class Sent:
        requests = calls

        @property
        def last(self):
            return calls[-1]

        def answers(self, response):
            calls_response[0] = response

    return Sent()


def http_error(status, body=b"", url="https://webapp.example/api/x"):
    return urllib.error.HTTPError(url, status, "err", {}, None) if body == b"" else _with_body(
        status, body, url
    )


def _with_body(status, body, url):
    import io

    return urllib.error.HTTPError(url, status, "err", {}, io.BytesIO(body))


class TestTheCredentials:
    def test_application_credentials_and_the_callback_chain_travel(self, sent):
        call_web_app("GET", "/api/expenses")

        assert sent.last.get_header("Authorization") == "Bearer MI-TOKEN"
        assert sent.last.get_header("X-ms-hook-invocation") == "HOOK-CHAIN"
        assert {name.lower() for name, _ in sent.last.header_items()} == {
            "authorization", "x-ms-hook-invocation", "accept",
        }

    def test_a_callback_chain_is_not_required_for_application_calls(self, sent):
        with hooks._use_hook_invocation(None):
            call_web_app("GET", "/api/expenses")
        assert sent.last.get_header("Authorization")
        assert sent.last.get_header("X-ms-hook-invocation") is None

    def test_the_host_cannot_be_redirected(self, sent):
        # Application credentials must only reach the configured app.
        call_web_app("GET", "https://attacker.example/steal")

        assert sent.last.full_url.startswith("https://webapp.example/")
        assert "attacker.example" not in sent.last.host


class TestTheRequest:
    def test_the_path_is_joined_to_the_app(self, sent):
        call_web_app("GET", "/api/expenses")
        assert sent.last.full_url == "https://webapp.example/api/expenses"

    def test_a_path_without_a_slash_still_works(self, sent):
        call_web_app("GET", "api/expenses")
        assert sent.last.full_url == "https://webapp.example/api/expenses"

    def test_params_are_encoded(self, sent):
        call_web_app("GET", "/api/expenses", params={"month": "2026-08", "q": "a b"})
        assert sent.last.full_url.endswith("/api/expenses?month=2026-08&q=a+b")

    def test_the_method_is_used(self, sent):
        call_web_app("post", "/api/expenses", body={"amount": 16.0})
        assert sent.last.get_method() == "POST"

    def test_a_body_is_sent_as_json(self, sent):
        call_web_app("POST", "/api/expenses", body={"amount": 16.0})

        assert json.loads(sent.last.data) == {"amount": 16.0}
        assert sent.last.get_header("Content-type") == "application/json"

    def test_a_get_carries_no_body(self, sent):
        call_web_app("GET", "/api/expenses")
        assert sent.last.data is None

    def test_extra_headers_are_added(self, sent):
        call_web_app("GET", "/api/expenses", headers={"X-Trace": "abc"})
        assert sent.last.get_header("X-trace") == "abc"

    @pytest.mark.parametrize(
        "header",
        [
            "Authorization",
            "X-MS-HOOK-INVOCATION",
        ],
    )
    def test_trusted_headers_cannot_be_overridden(self, sent, header):
        with pytest.raises(ValueError, match="managed by call_web_app"):
            call_web_app("GET", "/api/expenses", headers={header: "override"})

        assert sent.requests == []


class TestTheAnswer:
    """The body comes back as it arrived. Making sense of it belongs to whoever called."""

    def test_the_body_comes_back_verbatim(self, sent):
        sent.answers(FakeResponse(json.dumps({"items": [1, 2]}).encode()))
        assert call_web_app("GET", "/api/expenses") == '{"items": [1, 2]}'

    def test_a_non_json_answer_comes_back_as_text(self, sent):
        sent.answers(FakeResponse(b"plain words", content_type="text/plain"))
        assert call_web_app("GET", "/api/note") == "plain words"

    def test_an_empty_answer_is_empty(self, sent):
        sent.answers(FakeResponse(b"", content_type="application/json"))
        assert call_web_app("DELETE", "/api/expenses/1") == ""

    def test_a_lying_content_type_does_not_raise(self, sent):
        # The app said JSON and sent something else; nothing here parses, so nothing here breaks.
        sent.answers(FakeResponse(b"<html>oops</html>"))
        assert call_web_app("GET", "/api/expenses") == "<html>oops</html>"


class TestFailures:
    @pytest.mark.parametrize("status", [401, 403, 404, 500])
    def test_the_message_names_the_status_and_the_endpoint(self, sent, monkeypatch, status):
        monkeypatch.setattr(app_module._opener, "open", _raising(http_error(status)))
        with pytest.raises(AppCallError, match=f"GET /api/expenses returned {status}") as caught:
            call_web_app("GET", "/api/expenses")

        assert caught.value.status == status

    def test_the_app_s_own_body_is_reachable_on_the_cause(self, sent, monkeypatch):
        # Not put in the message: the caller knows its own API, so the body is theirs to read.
        monkeypatch.setattr(
            app_module._opener, "open", _raising(http_error(400, b'{"error":"bad month"}'))
        )
        with pytest.raises(AppCallError) as caught:
            call_web_app("GET", "/api/expenses")

        assert caught.value.__cause__.read() == b'{"error":"bad month"}'

    def test_an_unreachable_app_says_so(self, sent, monkeypatch):
        monkeypatch.setattr(
            app_module._opener, "open", _raising(urllib.error.URLError("no route"))
        )
        with pytest.raises(AppCallError) as caught:
            call_web_app("GET", "/api/expenses")

        assert caught.value.status is None
        assert "could not reach" in str(caught.value)

    def test_a_redirect_is_refused_rather_than_followed(self, sent, monkeypatch):
        # urllib carries every header but content-type/length onto the redirect target, so
        # following one would resend application credentials to whatever host it names.
        import io

        redirect = urllib.error.HTTPError(
            "https://webapp.example/api/expenses",
            302,
            "Found",
            {"Location": "https://attacker.example/collect"},
            io.BytesIO(b""),
        )
        monkeypatch.setattr(app_module._opener, "open", _raising(redirect))

        with pytest.raises(AppCallError) as caught:
            call_web_app("GET", "/api/expenses")

        # Surfaced as the redirect it is, rather than followed to attacker.example.
        assert caught.value.status == 302
        assert caught.value.__cause__.headers["Location"] == "https://attacker.example/collect"

    def test_the_opener_does_not_follow_redirects(self):
        # The guard is the opener itself, not just the message above.
        redirects = app_module._NoRedirects()
        assert redirects.redirect_request(None, None, 302, "", {}, "http://x") is None


def _raising(error):
    def urlopen(request, timeout=None):
        raise error

    return urlopen


class TestTheAgentTool:
    """The tool answers a model, so it reports rather than raises."""

    def test_a_result_comes_back_readable(self, sent):
        from app_service_extensions.tools import call_web_app_wrapper

        sent.answers(FakeResponse(json.dumps({"items": []}).encode()))
        answer = call_web_app_wrapper("GET", "/api/expenses")

        assert "GET /api/expenses returned" in answer
        assert "items" in answer

    def test_an_empty_response_reports_success(self, sent):
        from app_service_extensions.tools import call_web_app_wrapper

        sent.answers(FakeResponse(b""))
        answer = call_web_app_wrapper("DELETE", "/api/expenses/1")

        assert answer == "DELETE /api/expenses/1 succeeded with an empty response body."

    def test_an_http_failure_names_the_status_and_endpoint(self, sent, monkeypatch):
        from app_service_extensions.tools import call_web_app_wrapper

        monkeypatch.setattr(
            app_module._opener, "open", _raising(http_error(404, b"SYNTHETIC-RESPONSE-BODY"))
        )
        answer = call_web_app_wrapper("GET", "/api/nope")

        assert answer == "Web app request failed: GET /api/nope returned 404"

    def test_a_connection_failure_is_reported_as_a_request_failure(self, sent, monkeypatch):
        from app_service_extensions.tools import call_web_app_wrapper

        monkeypatch.setattr(
            app_module._opener, "open", _raising(urllib.error.URLError("no route"))
        )
        answer = call_web_app_wrapper("GET", "/api/expenses")

        assert answer.startswith(
            "Web app request failed: could not reach the web app at "
            "https://webapp.example/api/expenses"
        )
        assert "no route" in answer

    def test_an_unexpected_failure_is_logged_rather_than_lost(self, sent, monkeypatch, caplog):
        from app_service_extensions.tools import call_web_app_wrapper

        monkeypatch.setattr(app_module._opener, "open", _raising(OSError("SYNTHETIC-FAILURE")))
        with caplog.at_level(logging.WARNING, logger="azure.functions.AppServiceExtensions"):
            answer = call_web_app_wrapper("GET", "/api/expenses")

        assert answer == "The web app call failed unexpectedly."
        assert "SYNTHETIC-FAILURE" in caplog.text

    def test_a_missing_application_credential_is_reported_not_raised(self, sent):
        from app_service_extensions.tools import call_web_app_wrapper

        auth.configure(presenter=lambda: None)
        answer = call_web_app_wrapper("GET", "/api/expenses")

        assert answer.startswith("Web app authentication or configuration failed: ")
        assert "no presenter credential" in answer
        assert "MCP_AUTH_SCOPE" in answer
        assert sent.requests == []

    def test_a_missing_base_url_reports_how_to_configure_it(self, sent, monkeypatch):
        from app_service_extensions.tools import call_web_app_wrapper

        auth.configure(base_url="")
        monkeypatch.delenv("APP_BASE_URL", raising=False)
        monkeypatch.delenv("MCP_BASE_URL", raising=False)
        answer = call_web_app_wrapper("GET", "/api/expenses")

        assert answer == (
            "Web app authentication or configuration failed: app base URL not configured; "
            "set APP_BASE_URL or MCP_BASE_URL, or call configure(base_url=...)"
        )
        assert sent.requests == []

    def test_a_json_body_string_is_parsed(self, sent):
        from app_service_extensions.tools import call_web_app_wrapper

        call_web_app_wrapper("POST", "/api/expenses", body='{"amount": 16.0}')
        assert json.loads(sent.last.data) == {"amount": 16.0}

    def test_it_is_not_handed_to_every_agent(self):
        # Calling any route with application permissions must be explicitly enabled.
        from app_service_extensions.tools import ALL_TOOLS, APP_TOOLS, call_web_app_wrapper

        assert call_web_app_wrapper in APP_TOOLS
        assert call_web_app_wrapper not in ALL_TOOLS
        assert call_web_app not in APP_TOOLS
        assert call_web_app not in ALL_TOOLS
