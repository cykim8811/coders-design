import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from design.app import create_app, export_urls, forwarded, object_redirect
from design.identity import Identities, Identity


def test_core_identity_is_required_and_passwords_are_scoped():
    for headers in (
        {},
        {"coders-user-id": "u", "coders-resource-id": "r", "coders-resource-role": "admin"},
    ):
        with pytest.raises(HTTPException):
            Identity.read(headers)
    identities = Identities("unused", "a" * 32, "http://localhost")
    a = identities.credentials(Identity("u", "space-a", "owner"))
    assert a != identities.credentials(Identity("v", "space-a", "member"))
    assert a != identities.credentials(Identity("u", "space-b", "owner"))
    assert a == identities.credentials(Identity("u", "space-a", "member"))


def test_storage_redirect_cannot_escape_its_workspace():
    base = "http://127.0.0.1:9000/v1/objects"
    valid = base + "/workspace/key?X-Amz-Signature=secret"
    assert object_redirect(valid, base) == valid
    for url in (
        "https://evil.test/workspace/key",
        base + "/other/key",
        base + "/workspace/%2e%2e/key",
        "http://user@127.0.0.1:9000/v1/objects/workspace/key",
    ):
        with pytest.raises(HTTPException):
            object_redirect(url, base)


def test_browser_credentials_and_core_identity_are_not_forwarded():
    headers = forwarded(
        {
            "Cookie": "forged",
            "Authorization": "Bearer forged",
            "Coders-User-Id": "other",
            "X-Forwarded-Host": "evil",
            "Content-Type": "image/png",
        }
    )
    assert headers == {"Content-Type": "image/png"}


def test_public_proxy_blocks_auth_and_never_exposes_penpot_sessions(monkeypatch):
    app = create_app()

    async def initialize():
        pass

    async def session(identity, client):
        return "server-session-for-" + identity.user

    monkeypatch.setattr(app.state.identities, "initialize", initialize)
    monkeypatch.setattr(app.state.identities, "session", session)
    seen = []

    async def backend(request):
        seen.append(request.headers["cookie"])
        return httpx.Response(200, json={"ok": True}, headers={"Set-Cookie": "auth-token=private"})

    identity = {
        "Coders-User-Id": "alice",
        "Coders-Resource-Id": "space",
        "Coders-Resource-Role": "owner",
    }
    with TestClient(app) as client:
        assert client.get("/api/rpc/command/get-profile").status_code == 401
        for path in (
            "api/auth/oidc",
            "api/rpc/command/register-profile",
            "api/rpc/command/login-with-password",
            "api/main/methods/login-with-password",
            "api/main/methods/register-profile",
        ):
            assert client.post("/" + path, headers=identity).status_code == 403
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(backend))
        for user in ("alice", "bob"):
            result = client.get(
                "/api/rpc/command/get-profile",
                headers={**identity, "Coders-User-Id": user, "Cookie": "auth-token=forged"},
            )
            assert result.status_code == 200
            assert "set-cookie" not in result.headers
        assert seen == ["auth-token=server-session-for-alice", "auth-token=server-session-for-bob"]


def test_export_metadata_uses_app_origin_and_compression_is_preserved():
    value = {"~:uri": {"~#uri": "http://127.0.0.1:8080/assets/by-id/test"}}
    assert export_urls(value) == {"~:uri": {"~#uri": "/assets/by-id/test"}}
    headers = {"Content-Encoding": "gzip", "Content-Length": "12"}
    assert forwarded(headers, request=True) == headers
    assert forwarded(headers) == {}
