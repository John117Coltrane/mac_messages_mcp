"""Tests for the SSE server's bearer-token auth middleware."""

import unittest
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mac_messages_mcp import server


async def _ping(request):
    return JSONResponse({"ok": True})


def _build_app():
    return Starlette(
        routes=[Route("/ping", endpoint=_ping)],
        middleware=[Middleware(server.BearerAuthMiddleware)],
    )


class TestBearerAuthMiddleware(unittest.TestCase):
    def test_no_token_configured_allows_all_requests(self):
        with patch.object(server, "AUTH_TOKEN", ""):
            client = TestClient(_build_app())
            resp = client.get("/ping")
            self.assertEqual(resp.status_code, 200)

    def test_missing_authorization_header_rejected(self):
        with patch.object(server, "AUTH_TOKEN", "secret"):
            client = TestClient(_build_app())
            resp = client.get("/ping")
            self.assertEqual(resp.status_code, 401)

    def test_wrong_token_rejected(self):
        with patch.object(server, "AUTH_TOKEN", "secret"):
            client = TestClient(_build_app())
            resp = client.get("/ping", headers={"Authorization": "Bearer nope"})
            self.assertEqual(resp.status_code, 401)

    def test_correct_token_accepted(self):
        with patch.object(server, "AUTH_TOKEN", "secret"):
            client = TestClient(_build_app())
            resp = client.get("/ping", headers={"Authorization": "Bearer secret"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), {"ok": True})

    def test_non_bearer_scheme_rejected(self):
        with patch.object(server, "AUTH_TOKEN", "secret"):
            client = TestClient(_build_app())
            resp = client.get("/ping", headers={"Authorization": "Basic secret"})
            self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
