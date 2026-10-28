"""Verify saved edits, image storage, and PNG/SVG export against the local services."""

import json
import os
import struct
import zlib
from pathlib import Path

import httpx


def main():
    document = json.loads(Path("artifacts/edited-file.json").read_text())
    headers = {
        "Coders-User-Id": "local-owner",
        "Coders-Resource-Id": "local-design",
        "Coders-Resource-Role": "owner",
        "Accept": "application/json",
    }
    with httpx.Client(
        base_url=os.environ.get("DESIGN_TEST_URL", "http://127.0.0.1:19664"),
        headers=headers,
        timeout=120,
    ) as client:
        response = client.get("/api/main/methods/get-file", params={"id": document["id"]})
        response.raise_for_status()
        saved = response.json()
        page = saved["data"]["pages"][0]
        shape = next(
            k
            for k, v in saved["data"]["pagesIndex"][page]["objects"].items()
            if v["type"] == "rect"
        )
        assert saved["revn"] >= 1
        profile = client.get("/api/main/methods/get-profile").json()

        def chunk(tag, body):
            return (
                struct.pack("!I", len(body))
                + tag
                + body
                + struct.pack("!I", zlib.crc32(tag + body) & 0xFFFFFFFF)
            )

        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack("!2I5B", 32, 32, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\x30\x70\xe0" * 32) * 32))
            + chunk(b"IEND", b"")
        )
        previous = Path("artifacts/media.json")
        if previous.exists():
            old = client.get("/assets/by-id/" + json.loads(previous.read_text())["mediaId"])
            assert old.status_code == 200 and old.content == png
        uploaded = client.post(
            "/api/main/methods/upload-file-media-object",
            data={"file-id": saved["id"], "name": "blue.png", "is-local": "true"},
            files={"content": ("blue.png", png, "image/png")},
        )
        uploaded.raise_for_status()
        previous.write_text(uploaded.text)
        asset = client.get("/assets/by-id/" + uploaded.json()["mediaId"])
        assert asset.status_code == 200 and asset.content == png
        assert "location" not in asset.headers
        for extension in ("png", "svg"):
            payload = {
                "~:cmd": "~:export-shapes",
                "~:profile-id": "~u" + profile["id"],
                "~:wait": True,
                "~:is-wasm": False,
                "~:exports": [
                    {
                        "~:file-id": "~u" + saved["id"],
                        "~:page-id": "~u" + page,
                        "~:object-id": "~u" + shape,
                        "~:type": "~:" + extension,
                        "~:scale": 1,
                        "~:suffix": "",
                        "~:name": "rectangle",
                    }
                ],
            }
            exported = client.post(
                "/api/export",
                content=json.dumps(payload),
                headers={"Content-Type": "application/transit+json"},
            )
            assert exported.status_code == 200, exported.text[:500]
            url = exported.json()["~:uri"]["~#uri"]
            assert url.startswith("/assets/by-id/"), url
            download = client.get(url)
            assert download.status_code == 200, download.text[:200]
            assert (
                download.content.startswith(b"\x89PNG")
                if extension == "png"
                else b"<svg" in download.content
            )
            Path("artifacts/rectangle." + extension).write_bytes(download.content)
        print(
            json.dumps(
                {
                    "revision": saved["revn"],
                    "storage": "exact image roundtrip",
                    "exports": ["png", "svg"],
                    "existing_users": "authenticated",
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
