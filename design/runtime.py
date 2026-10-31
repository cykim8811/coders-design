"""Supervise all app processes as one Core HTTP image; fail if any child exits."""

import os
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from urllib.parse import unquote, urlsplit


def redis_ready(uri):
    """Check the actual data tunnel, without logging its URI or credentials."""
    parsed = urlsplit(uri)
    if parsed.scheme not in ("redis", "rediss") or not parsed.hostname:
        raise RuntimeError("REDIS_URL must be a Redis connection URL")
    commands = []
    if parsed.password is not None:
        credentials = [unquote(parsed.password)]
        if parsed.username:
            credentials.insert(0, unquote(parsed.username))
        commands.append(["AUTH", *credentials])
    database = parsed.path.lstrip("/") or "0"
    if not database.isdecimal():
        raise RuntimeError("REDIS_URL must specify a numeric database")
    commands.extend([["SELECT", database], ["PING"]])
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 6379), timeout=2) as raw:
            connection = (
                ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
                if parsed.scheme == "rediss"
                else raw
            )
            with connection, connection.makefile("rb") as reader:
                for command in commands:
                    parts = [part.encode() for part in command]
                    payload = b"*" + str(len(parts)).encode() + b"\r\n"
                    for part in parts:
                        payload += b"$" + str(len(part)).encode() + b"\r\n" + part + b"\r\n"
                    connection.sendall(payload)
                    expected = b"+PONG\r\n" if command[0] == "PING" else b"+OK\r\n"
                    if reader.readline(1024) != expected:
                        return False
        return True
    except OSError:
        return False


def wait_for_redis(uri, deadline, stopping):
    print("Waiting for the workspace Redis connection", flush=True)
    while not stopping():
        if time.monotonic() >= deadline:
            raise RuntimeError("Workspace Redis connection did not become ready")
        if redis_ready(uri):
            print("Workspace Redis connection is ready", flush=True)
            return True
        time.sleep(0.5)
    return False


def environment():
    env = os.environ.copy()
    database = urlsplit(env["DATABASE_URL"])
    env.update(
        {
            "PENPOT_DATABASE_URI": "postgresql://"
            + database.hostname
            + ":"
            + str(database.port or 5432)
            + database.path,
            "PENPOT_DATABASE_USERNAME": unquote(database.username or ""),
            "PENPOT_DATABASE_PASSWORD": unquote(database.password or ""),
            "PENPOT_REDIS_URI": env["REDIS_URL"],
            "PENPOT_DATABASE_MIN_POOL_SIZE": "1",
            "PENPOT_DATABASE_MAX_POOL_SIZE": "2",
            "PENPOT_HTTP_SERVER_HOST": "127.0.0.1",
            "PENPOT_HTTP_SERVER_PORT": "6060",
            "PENPOT_EXPORTER_PORT": "6061",
            "PENPOT_BROWSER_POOL_MAX": "1",
            "PENPOT_BROWSER_POOL_MIN": "0",
            "PENPOT_PUBLIC_URI": "http://127.0.0.1:8080",
            "PENPOT_INTERNAL_URI": "http://127.0.0.1:6062",
            "PENPOT_FLAGS": "enable-registration enable-login-with-password disable-email-verification disable-secure-session-cookies disable-telemetry disable-smtp disable-prepl-server disable-admin-console disable-mcp",
            "PENPOT_SECRET_KEY": env["DESIGN_AUTH_SECRET"],
            "PENPOT_OBJECTS_STORAGE_BACKEND": "s3",
            "PENPOT_OBJECTS_STORAGE_S3_BUCKET": "workspace",
            "PENPOT_OBJECTS_STORAGE_S3_REGION": "us-east-1",
            "PENPOT_OBJECTS_STORAGE_S3_ENDPOINT": env["AWS_ENDPOINT_URL_S3"],
            "JVM_OPTS": "-Xmx768m -XX:ActiveProcessorCount=2",
            "JAVA_TOOL_OPTIONS": "-Daws.requestChecksumCalculation=WHEN_REQUIRED -Daws.responseChecksumValidation=WHEN_REQUIRED",
            "HOME": "/tmp",
            "XDG_CACHE_HOME": "/tmp/.cache",
        }
    )
    return env


def main():
    env = environment()
    children = []
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def launch(command, cwd, extra=None):
        child = subprocess.Popen(
            command, cwd=cwd, env={**env, **(extra or {})}, start_new_session=True
        )
        children.append(child)
        return child

    status = 0
    try:
        deadline = time.monotonic() + 160
        if not wait_for_redis(env["REDIS_URL"], deadline, lambda: stopping):
            return
        backend = launch(["/bin/bash", "run.sh"], "/opt/penpot/backend")
        while not stopping:
            if backend.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Penpot backend did not become ready")
            try:
                with urllib.request.urlopen("http://127.0.0.1:6060/readyz", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            time.sleep(0.5)
        if not stopping:
            launch(["node", "app.js"], "/opt/penpot/exporter", {"PENPOT_HTTP_SERVER_PORT": "6061"})
            # Internal-only export renderer uses Penpot's own session cookie.
            launch(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "design.internal:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "6062",
                ],
                "/app",
            )
            launch(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "design.app:app",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    env.get("PORT", "8080"),
                ],
                "/app",
            )
        while not stopping:
            if any(child.poll() is not None for child in children):
                raise RuntimeError("A design service exited")
            time.sleep(0.25)
    except (RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        status = 1
    finally:
        stop(None, None)
        deadline = time.monotonic() + 10
        for child in children:
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
    sys.exit(status)


if __name__ == "__main__":
    main()
