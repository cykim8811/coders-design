"""Real Core session/gateway -> HTTP transport -> real Penpot adapter.

Requires a fresh, disposable Penpot database on DESIGN_CORE_TEST_URL.
Only container lifecycle/metering are fixtures; Core authorization, routing,
identity injection and Penpot provisioning are exercised without replacements.
Run from the matching Core checkout, with its tests package on PYTHONPATH.
"""

import json
import os
import uuid
from pathlib import Path

import httpx
import pytest
from coders.execution import HttpResponse
from coders.websocket_transport import relay_url
from starlette.websockets import WebSocketDisconnect
from tests.browser_helpers import connect_project
from tests.test_browser import browser_app, browser_client  # noqa: F401
from tests.test_resource_servers import shared  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


def test_core_sessions_and_routes_reach_real_penpot(shared, monkeypatch):  # noqa: F811
    app, client, owner, _, project, connection, _, _, calls = shared
    base = os.environ["DESIGN_CORE_TEST_URL"].rstrip("/")
    assert base.startswith("http://127.0.0.1:"), "Use a disposable local adapter only"
    accounts = app.state.browser.accounts
    manifest = json.loads((ROOT / "coders.json").read_text())
    # Data services and secrets are supplied to the disposable container beforehand.
    # This harness tests the browser/gateway boundary, not Knative provisioning.
    manifest.pop("secrets")
    manifest["services"] = []
    with app.state.ledger.transaction() as db:
        endpoints = db.execute(
            "SELECT endpoints FROM core_projects WHERE slug=%s", (project,)
        ).fetchone()
        target = json.loads(endpoints["endpoints"])["outer"]
        db.execute(
            "UPDATE core_projects SET endpoints=%s WHERE slug=%s",
            (json.dumps({"server": target}), project),
        )
        db.execute(
            "UPDATE github_deployments SET manifest=%s WHERE project=%s",
            (json.dumps(manifest), project),
        )
    connect_project(accounts, owner["id"], project, 100, ["frontend"])
    connection = accounts.connection(owner["id"], project)
    handle = connection["project"]
    host = app.state.browser.origin(handle)

    async def exchange(item, request, options):
        calls.append((item, request))
        if request.websocket:
            return await relay_url(
                base.replace("http://", "ws://") + request.path,
                request,
                1048576,
                # The managed Core runtime regenerates WebSocket handshake headers.
                [(k, v) for k, v in request.headers if not k.lower().startswith("sec-websocket")],
            )
        async with httpx.AsyncClient(timeout=120, trust_env=False) as upstream:
            result = await upstream.request(
                request.method,
                base + request.path,
                headers=dict(request.headers),
                content=request.body,
            )
        return HttpResponse(result.status_code, tuple(result.headers.items()), result.content)

    monkeypatch.setattr(app.state.gateway.runner.http, "exchange", exchange)

    def headers(user, selected_handle=handle):
        return {
            "Cookie": app.state.browser.cookie("project")
            + "="
            + accounts.session(user, selected_handle),
            "Origin": app.state.browser.origin(selected_handle),
            "Accept": "application/json",
        }

    profile_path = host + "/api/rpc/command/get-profile"
    forged = {
        "Coders-User-Id": "forged-user",
        "Coders-Resource-Id": "forged-workspace",
        "Coders-Resource-Role": "owner",
    }
    denied = client.get(profile_path, headers=forged)
    assert denied.status_code == 401, denied.text
    owner_headers = {**headers(owner["id"]), **forged}
    result = client.get(profile_path, headers=owner_headers)
    assert result.status_code == 200, result.text
    owner_profile = result.json()
    injected = dict(calls[-1][1].headers)
    assert injected["coders-user-id"] == owner["id"]
    assert injected["coders-resource-id"] == connection["resource_id"]
    assert injected["coders-resource-role"] == "owner"
    assert "set-cookie" not in result.headers

    member = accounts.identity("urn:design-test", "member", "Design member")
    app.state.resources.access(owner["id"], connection["resource_id"], member["id"], "member")
    member_handle = accounts.allow(member["id"], project, 0, [], resource=connection["resource_id"])
    member_host = app.state.browser.origin(member_handle)
    member_headers = headers(member["id"], member_handle)
    member_profile_path = member_host + "/api/rpc/command/get-profile"
    result = client.get(member_profile_path, headers={**member_headers, **forged})
    assert result.status_code == 200, result.text
    member_profile = result.json()
    assert member_profile["id"] != owner_profile["id"]
    assert member_profile["defaultTeamId"] == owner_profile["defaultTeamId"]
    assert dict(calls[-1][1].headers)["coders-resource-role"] == "member"

    created = client.post(
        host + "/api/rpc/command/create-file",
        headers=owner_headers,
        json={"name": "Core route verification", "projectId": owner_profile["defaultProjectId"]},
    )
    assert created.status_code == 200, created.text
    document = created.json()
    fetched = client.get(
        member_host + "/api/main/methods/get-file?id=" + document["id"], headers=member_headers
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["id"] == document["id"]
    assert fetched.headers["cache-control"] == "private, no-store"

    renderer = client.get(host + "/rasterizer.html", headers=owner_headers)
    assert renderer.status_code == 200, renderer.text[:500]
    assert "frame-src 'self'" in renderer.headers["content-security-policy"]
    assert "frame-ancestors 'self'" in renderer.headers["content-security-policy"]
    assert (
        client.get(
            member_profile_path, headers={**member_headers, "Origin": "https://foreign.example"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            host + "/api/main/methods/login-with-password", headers=owner_headers, json={}
        ).status_code
        == 403
    )

    socket_url = (
        member_host.replace("https://", "wss://")
        + "/ws/notifications?session-id="
        + str(uuid.uuid4())
    )
    owner_socket_url = (
        host.replace("https://", "wss://") + "/ws/notifications?session-id=" + str(uuid.uuid4())
    )
    subscription = json.dumps(
        ["^ ", "~:type", "~:subscribe-file", "~:file-id", "~u" + document["id"]]
    )
    with client.websocket_connect(owner_socket_url, headers=headers(owner["id"])) as owner_socket:
        owner_socket.send_text(subscription)
        with client.websocket_connect(socket_url, headers=member_headers) as socket:
            socket.send_text(subscription)
            message = owner_socket.receive_text()
            assert "join-file" in message and member_profile["id"] in message, message

    app.state.resources.access(owner["id"], connection["resource_id"], member["id"], None)
    assert client.get(member_profile_path, headers=member_headers).status_code in (401, 403)
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(socket_url, headers=member_headers),
    ):
        pass
    assert client.get(profile_path, headers=owner_headers).status_code == 200

    report = {
        "environment": "isolated Core ledger, real local Penpot container",
        "production_oidc": False,
        "checks": [
            "unauthenticated identity forgery rejected",
            "Core replaces forged user/resource/role headers",
            "distinct owner/member Penpot identities share a team",
            "member reads owner document through Core route and query string",
            "renderer receives same-origin Core frame policy",
            "foreign Origin and native password login rejected",
            "Penpot member presence crosses Core WebSocket gateway",
            "revoked member HTTP and new WebSocket access denied",
            "owner still has access after member revocation",
        ],
    }
    (ROOT / "artifacts/core-routing.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
