import socket
import threading
import time

import pytest

from design.runtime import redis_ready, wait_for_redis


def test_redis_probe_checks_auth_database_and_ping_without_disclosing_credentials(capsys):
    commands = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        def serve():
            with listener.accept()[0] as connection, connection.makefile("rb") as reader:
                for response in (b"+OK\r\n", b"+OK\r\n", b"+PONG\r\n"):
                    count = int(reader.readline()[1:])
                    parts = []
                    for _ in range(count):
                        length = int(reader.readline()[1:])
                        parts.append(reader.read(length))
                        assert reader.read(2) == b"\r\n"
                    commands.append(parts)
                    connection.sendall(response)

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        assert redis_ready(f"redis://designer:p%40ssword@127.0.0.1:{port}/3")
        worker.join(timeout=3)
        assert not worker.is_alive()
    assert commands == [[b"AUTH", b"designer", b"p@ssword"], [b"SELECT", b"3"], [b"PING"]]
    assert "p@ssword" not in capsys.readouterr().out


def test_redis_connection_close_is_retried_before_backend_start():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        connections = []

        def serve():
            # A listening proxy can still drop the connection while Redis initializes.
            with listener.accept()[0]:
                connections.append("closed before handshake")
            with listener.accept()[0] as connection, connection.makefile("rb") as reader:
                assert reader.readline() == b"*2\r\n"
                assert reader.readline() == b"$6\r\n"
                assert reader.readline() == b"SELECT\r\n"
                assert reader.readline() == b"$1\r\n"
                assert reader.readline() == b"0\r\n"
                connection.sendall(b"+OK\r\n")
                assert reader.readline() == b"*1\r\n"
                assert reader.readline() == b"$4\r\n"
                assert reader.readline() == b"PING\r\n"
                connection.sendall(b"+PONG\r\n")
                connections.append("ready")

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        assert wait_for_redis(f"redis://127.0.0.1:{port}/0", time.monotonic() + 5, lambda: False)
        worker.join(timeout=3)
        assert connections == ["closed before handshake", "ready"]


def test_redis_wait_has_a_deadline_and_honors_shutdown():
    with pytest.raises(RuntimeError, match="Redis connection did not become ready"):
        wait_for_redis("redis://127.0.0.1:1/0", time.monotonic() - 1, lambda: False)
    assert not wait_for_redis("redis://127.0.0.1:1/0", time.monotonic() + 60, lambda: True)
