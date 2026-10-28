"""Supervise all app processes as one Core HTTP image; fail if any child exits."""

import os
import signal
import subprocess
import sys
import time
import urllib.request
from urllib.parse import unquote, urlsplit


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
        backend = launch(["/bin/bash", "run.sh"], "/opt/penpot/backend")
        deadline = time.monotonic() + 160
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
