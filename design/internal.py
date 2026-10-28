"""Loopback-only renderer origin; Penpot validates the export session on API requests."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from design.app import forwarded, object_redirect


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(timeout=120, trust_env=False, follow_redirects=False) as client:
        app.state.client = client
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "DELETE"])
async def render(request: Request, path: str):
    if path.startswith(("api/", "assets/")):
        headers = forwarded(request.headers, request=True)
        for name in ("cookie", "authorization"):
            if value := request.headers.get(name):
                headers[name] = value
        url = os.environ.get("DESIGN_BACKEND_URL", "http://127.0.0.1:6060") + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        upstream = await app.state.client.send(
            app.state.client.build_request(
                request.method, url, headers=headers, content=request.stream()
            ),
            stream=True,
        )
        media = None
        if path.startswith("assets/") and upstream.status_code == 307:
            location = object_redirect(
                upstream.headers["location"], os.environ["AWS_ENDPOINT_URL_S3"]
            )
            media = upstream.headers.get("x-mtype")
            await upstream.aclose()
            upstream = await app.state.client.send(
                app.state.client.build_request("GET", location), stream=True
            )
        headers = forwarded(upstream.headers)
        if media:
            headers["content-type"] = media
        return StreamingResponse(
            upstream.aiter_bytes(),
            status_code=upstream.status_code,
            headers=headers,
            background=BackgroundTask(upstream.aclose),
        )
    root = Path(os.environ.get("DESIGN_FRONTEND_DIR", "/var/www/app")).resolve()
    file = (root / (path or "index.html")).resolve()
    if request.method not in ("GET", "HEAD") or not file.is_relative_to(root) or not file.is_file():
        raise HTTPException(404)
    return FileResponse(file)
