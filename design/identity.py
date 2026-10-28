"""Keep Penpot sessions on the server; Core remains the only public identity authority."""

import asyncio
import hashlib
import hmac
import time
from collections import OrderedDict
from dataclasses import dataclass

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row


@dataclass(frozen=True)
class Identity:
    user: str
    resource: str
    role: str

    @classmethod
    def read(cls, headers):
        values = [
            headers.get("coders-" + name, "")
            for name in ("user-id", "resource-id", "resource-role")
        ]
        if not all(values) or values[2] not in ("owner", "member"):
            raise HTTPException(401, "Coders 워크스페이스에서 접속해주세요.")
        if any(len(value) > 200 or any(ord(c) < 32 for c in value) for value in values):
            raise HTTPException(400, "Invalid Core identity")
        return cls(*values)


class Identities:
    def __init__(self, database, secret, backend):
        if len(secret) < 32:
            raise ValueError("DESIGN_AUTH_SECRET must contain at least 32 characters")
        self.database, self.secret, self.backend = database, secret.encode(), backend
        self.sessions = OrderedDict()
        self.lock = asyncio.Lock()

    async def connect(self):
        return await psycopg.AsyncConnection.connect(self.database, row_factory=dict_row)

    async def initialize(self):
        async with await self.connect() as db:
            await db.execute("""CREATE TABLE IF NOT EXISTS coders_design_workspace (
                singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
                resource_id text NOT NULL UNIQUE,
                team_id uuid NOT NULL REFERENCES team(id),
                project_id uuid NOT NULL REFERENCES project(id))""")

    def credentials(self, identity):
        scope = (identity.resource + "\x00" + identity.user).encode()
        email = hashlib.sha256(scope).hexdigest()[:40] + "@users.design.coders.kr"
        password = hmac.new(self.secret, scope, hashlib.sha256).hexdigest() + "aA9!"
        return email, password

    async def rpc(self, client, command, data):
        response = await client.post(
            self.backend + "/api/rpc/command/" + command,
            json=data,
            headers={"Accept": "application/json"},
        )
        if response.is_error:
            # Never return upstream bodies containing registration tokens or credentials.
            raise HTTPException(503, "디자인 사용자 연결을 준비하지 못했습니다.")
        return response

    async def session(self, identity, client):
        key = (identity.resource, identity.user)
        async with self.lock:
            async with await self.connect() as db:
                row = await (await db.execute("SELECT * FROM coders_design_workspace")).fetchone()
                if row and row["resource_id"] != identity.resource:
                    raise HTTPException(403, "Workspace identity mismatch")
                if not row and identity.role != "owner":
                    raise HTTPException(409, "워크스페이스 소유자가 먼저 디자인 공간을 열어주세요.")
            cached = self.sessions.get(key)
            if cached and cached["expires"] > time.monotonic():
                # Roles are refreshed on every API request, including owner handover.
                await self.membership(identity, cached["profile"])
                self.sessions.move_to_end(key)
                return cached["cookie"]
            email, password = self.credentials(identity)
            async with await self.connect() as db:
                profile = await (
                    await db.execute(
                        "SELECT id FROM profile WHERE email=%s AND deleted_at IS NULL", (email,)
                    )
                ).fetchone()
            if not profile:
                prepared = await self.rpc(
                    client,
                    "prepare-register-profile",
                    {
                        "email": email,
                        "password": password,
                        "fullname": "Designer " + email[:6],
                    },
                )
                await self.rpc(client, "register-profile", {"token": prepared.json()["token"]})
            # A separate client is deliberately used by the caller; do not share a cookie jar.
            login = await self.rpc(
                client, "login-with-password", {"email": email, "password": password}
            )
            profile = login.json()
            cookie = login.cookies.get("auth-token")
            if not cookie:
                raise HTTPException(503, "Penpot did not issue a session")
            await self.membership(identity, profile["id"])
            self.sessions[key] = {
                "profile": profile["id"],
                "cookie": cookie,
                "expires": time.monotonic() + 3600,
            }
            while len(self.sessions) > 256:
                self.sessions.popitem(last=False)
            return cookie

    async def membership(self, identity, profile):
        async with await self.connect() as db:
            # Serialize bootstrap and owner handover across processes, not only coroutines.
            await db.execute("SELECT pg_advisory_xact_lock(719321884)")
            workspace = await (await db.execute("SELECT * FROM coders_design_workspace")).fetchone()
            if not workspace:
                if identity.role != "owner":
                    raise HTTPException(409, "Owner setup required")
                defaults = await (
                    await db.execute(
                        "SELECT default_team_id, default_project_id FROM profile WHERE id=%s",
                        (profile,),
                    )
                ).fetchone()
                workspace = {
                    "team_id": defaults["default_team_id"],
                    "project_id": defaults["default_project_id"],
                    "resource_id": identity.resource,
                }
                await db.execute(
                    "INSERT INTO coders_design_workspace VALUES(true,%s,%s,%s)",
                    (identity.resource, workspace["team_id"], workspace["project_id"]),
                )
                await db.execute(
                    "UPDATE team SET name='Coders Design',is_default=false WHERE id=%s",
                    (workspace["team_id"],),
                )
            if workspace["resource_id"] != identity.resource:
                raise HTTPException(403, "Workspace identity mismatch")
            owner = identity.role == "owner"
            if owner:
                await db.execute(
                    "UPDATE team_profile_rel SET is_owner=false, is_admin=false WHERE team_id=%s AND profile_id<>%s",
                    (workspace["team_id"], profile),
                )
            await db.execute(
                """INSERT INTO team_profile_rel(team_id,profile_id,is_owner,is_admin,can_edit)
                VALUES(%s,%s,%s,%s,true) ON CONFLICT(team_id,profile_id) DO UPDATE
                SET is_owner=EXCLUDED.is_owner,is_admin=EXCLUDED.is_admin,can_edit=true""",
                (workspace["team_id"], profile, owner, owner),
            )
            await db.execute(
                "UPDATE profile SET default_team_id=%s,default_project_id=%s WHERE id=%s",
                (workspace["team_id"], workspace["project_id"], profile),
            )
