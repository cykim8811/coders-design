"""Exercise real Penpot provisioning and workspace boundaries through the adapter."""

import argparse
import json
import os
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url", default=os.environ.get("DESIGN_TEST_URL", "http://127.0.0.1:19664")
    )
    args = parser.parse_args()
    owner = {
        "Coders-User-Id": "local-owner",
        "Coders-Resource-Id": "local-design",
        "Coders-Resource-Role": "owner",
        "Accept": "application/json",
    }
    member = {**owner, "Coders-User-Id": "local-member", "Coders-Resource-Role": "member"}
    with httpx.Client(base_url=args.url, timeout=120) as client:
        assert client.get("/api/rpc/command/get-profile").status_code == 401
        profiles = []
        for identity in (owner, member):
            result = client.get("/api/rpc/command/get-profile", headers=identity)
            assert result.status_code == 200, (result.status_code, result.text[:500])
            assert "set-cookie" not in result.headers
            profiles.append(result.json())
        assert profiles[0]["id"] != profiles[1]["id"]
        assert profiles[0]["defaultTeamId"] == profiles[1]["defaultTeamId"]
        assert profiles[0]["defaultProjectId"] == profiles[1]["defaultProjectId"]
        other = client.get(
            "/api/rpc/command/get-profile", headers={**owner, "Coders-Resource-Id": "other-space"}
        )
        assert other.status_code == 403, other.text
        denied = client.post("/api/rpc/command/login-with-password", headers=owner, json={})
        assert denied.status_code == 403
        created = client.post(
            "/api/rpc/command/create-file",
            headers=owner,
            json={
                "name": "Coders integration smoke",
                "projectId": profiles[0]["defaultProjectId"],
            },
        )
        assert created.status_code == 200, created.text[:500]
        document = created.json()
        fetched = client.get(
            "/api/rpc/command/get-file", headers=member, params={"id": document["id"]}
        )
        assert fetched.status_code == 200, fetched.text[:500]
        Path("artifacts").mkdir(exist_ok=True)
        Path("artifacts/file.json").write_text(fetched.text)
        print(
            json.dumps(
                {
                    "owner": profiles[0]["id"],
                    "member": profiles[1]["id"],
                    "team": profiles[0]["defaultTeamId"],
                    "file": document["id"],
                    "checks": [
                        "Core identity required",
                        "distinct users",
                        "shared workspace",
                        "foreign workspace rejected",
                        "password login blocked",
                        "member reads owner document",
                    ],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
