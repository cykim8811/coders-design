import asyncio
import json
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from design.identity import Identities, Identity

HOP = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
    "cookie",
    "authorization",
    "set-cookie",
    "x-frame-options",
    "content-security-policy",
}
BLOCKED = {
    "prepare-register-profile",
    "register-profile",
    "login-with-password",
    "login-with-ldap",
    "recover-profile",
    "request-profile-recovery",
    "verify-token",
    "update-profile-password",
    "request-email-change",
    "delete-profile",
    "create-access-token",
    "update-profile-email",
    "delete-team",
    "delete-team-member",
    "update-team-member-role",
    "leave-team",
    "create-team-invitation",
    "create-team-invitations",
}


def forwarded(headers, *, request=False):
    excluded = HOP - {"content-length", "content-encoding"} if request else HOP
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in excluded
        and not k.lower().startswith(("coders-", "x-forwarded-", "x-real-"))
    }


def export_urls(value):
    """Return download metadata relative to the current app origin."""
    if isinstance(value, dict):
        if "~#uri" in value and isinstance(value["~#uri"], str):
            uri = urlsplit(value["~#uri"])
            if uri.path.startswith("/assets/by-id/"):
                return {"~#uri": uri.path}
        return {key: export_urls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [export_urls(item) for item in value]
    return value


def object_redirect(location, endpoint):
    """Only consume redirects to the configured workspace object endpoint."""
    parsed, base = urlsplit(location), urlsplit(endpoint)
    if parsed.scheme != base.scheme or parsed.netloc != base.netloc:
        raise HTTPException(502, "Unexpected asset storage origin")
    prefix = base.path.rstrip("/") + "/workspace/"
    path = unquote(parsed.path)
    if not path.startswith(prefix) or any(p in (".", "..") for p in path.split("/")):
        raise HTTPException(502, "Unexpected asset storage path")
    return location


def create_app():
    backend = os.environ.get("DESIGN_BACKEND_URL", "http://127.0.0.1:6060")
    exporter = os.environ.get("DESIGN_EXPORTER_URL", "http://127.0.0.1:6061")
    static = Path(os.environ.get("DESIGN_FRONTEND_DIR", "/var/www/app")).resolve()
    objects = os.environ.get("AWS_ENDPOINT_URL_S3", "")
    identities = Identities(os.environ["DATABASE_URL"], os.environ["DESIGN_AUTH_SECRET"], backend)

    @asynccontextmanager
    async def lifespan(app):
        await identities.initialize()
        async with httpx.AsyncClient(
            timeout=120, trust_env=False, follow_redirects=False
        ) as client:
            app.state.client = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.identities = identities

    @app.exception_handler(httpx.HTTPError)
    async def unavailable(request, exc):
        return JSONResponse({"detail": "디자인 서버를 준비하고 있습니다."}, status_code=503)

    @app.get("/health")
    async def health():
        result = await app.state.client.get(backend + "/readyz")
        return Response(status_code=200 if result.status_code == 200 else 503)

    async def cookie_for(identity):
        # Do not allow HTTPX's response cookie jar to leak sessions across Core users.
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            return await identities.session(identity, client)

    @app.websocket("/{path:path}")
    async def websocket(socket: WebSocket, path: str):
        try:
            identity = Identity.read(socket.headers)
            if path != "ws/notifications":
                await socket.close(1008)
                return
            cookie = await cookie_for(identity)
            query = socket.scope["query_string"].decode("ascii")
            target = backend.replace("http://", "ws://").replace("https://", "wss://") + "/" + path
            if query:
                target += "?" + query
            async with connect(
                target, additional_headers={"Cookie": "auth-token=" + cookie}, max_size=1048576
            ) as upstream:
                await socket.accept()

                async def incoming():
                    while True:
                        message = await socket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        await upstream.send(
                            message.get("text")
                            if message.get("text") is not None
                            else message["bytes"]
                        )

                async def outgoing():
                    async for message in upstream:
                        await (
                            socket.send_text(message)
                            if isinstance(message, str)
                            else socket.send_bytes(message)
                        )

                tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
                try:
                    done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (
            HTTPException,
            httpx.HTTPError,
            WebSocketDisconnect,
            ConnectionClosed,
            InvalidStatus,
        ):
            await socket.close(1008)

    @app.api_route(
        "/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    )
    async def proxy(request: Request, path: str):
        identity = Identity.read(request.headers)
        command = path.rsplit("/", 1)[-1] if path.startswith("api/") else ""
        if (
            path.startswith(("api/auth/", "api/management/", "api/mcp/", "internal/", "_design/"))
            or command in BLOCKED
        ):
            raise HTTPException(403, "계정과 접근 권한은 Coders에서 관리합니다.")
        if path.startswith("api/") and command == "logout":
            identities.sessions.pop((identity.resource, identity.user), None)
            return JSONResponse({})
        if path.startswith(("api/", "assets/")):
            cookie = await cookie_for(identity)
            headers = forwarded(request.headers, request=True)
            headers["Cookie"] = "auth-token=" + cookie
            target = exporter if path.startswith("api/export") else backend
            raw_path = request.scope["raw_path"].decode("ascii")
            query = request.scope["query_string"].decode("ascii")
            url = target + raw_path + ("?" + query if query else "")
            upstream = await app.state.client.send(
                app.state.client.build_request(
                    request.method, url, headers=headers, content=request.stream()
                ),
                stream=True,
            )
            if path.startswith("api/export"):
                body = await upstream.aread()
                await upstream.aclose()
                if upstream.status_code == 200:
                    body = json.dumps(export_urls(json.loads(body))).encode()
                return Response(
                    body,
                    status_code=upstream.status_code,
                    headers={**forwarded(upstream.headers), "cache-control": "private, no-store"},
                )
            if path.startswith("assets/") and upstream.status_code in (301, 302, 307, 308):
                # Penpot checked object permissions. Consume the storage redirect inside the app,
                # never expose its internal endpoint or bearer-like URL to the browser.
                location = object_redirect(upstream.headers.get("location", ""), objects)
                media = upstream.headers.get("x-mtype", "application/octet-stream")
                disposition = upstream.headers.get("content-disposition")
                await upstream.aclose()
                upstream = await app.state.client.send(
                    app.state.client.build_request(request.method, location), stream=True
                )
                response_headers = forwarded(upstream.headers)
                response_headers["content-type"] = media
                if disposition:
                    response_headers["content-disposition"] = disposition
            else:
                response_headers = forwarded(upstream.headers)
            response_headers["cache-control"] = "private, no-store"
            return StreamingResponse(
                upstream.aiter_bytes(),
                status_code=upstream.status_code,
                headers=response_headers,
                background=BackgroundTask(upstream.aclose),
            )
        if request.method not in ("GET", "HEAD"):
            raise HTTPException(405)
        if path == "js/config.js":
            return Response(
                'globalThis.penpotFlags = "disable-onboarding disable-dashboard-templates-section disable-google-fonts-provider";\n'
                'const designTitle = () => { const t = document.title.replace(/Penpot/g, "Coders Design"); if (t !== document.title) document.title = t; };\n'
                'new MutationObserver(designTitle).observe(document.querySelector("title"), {childList:true}); designTitle();\n',
                media_type="application/javascript",
                headers={"Cache-Control": "private, no-store"},
            )
        file = (static / (path or "index.html")).resolve()
        if not file.is_relative_to(static) or not file.is_file():
            raise HTTPException(404)
        if file.name == "index.html":
            text = file.read_text().replace("Penpot | Full-stack design", "Coders Design")
            return Response(
                text, media_type="text/html", headers={"Cache-Control": "private, no-store"}
            )
        return FileResponse(
            file,
            media_type="application/wasm"
            if file.suffix == ".wasm"
            else mimetypes.guess_type(file)[0],
        )

    return app


app = create_app()
