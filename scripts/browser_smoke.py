"""Run with `uv run --with playwright python scripts/browser_smoke.py`."""

import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright


async def main():
    document = json.loads(Path("artifacts/file.json").read_text())
    base = os.environ.get("DESIGN_TEST_URL", "http://127.0.0.1:19664")
    url = (
        base
        + "/#/workspace?file-id="
        + document["id"]
        + "&team-id="
        + document["teamId"]
        + "&page-id="
        + document["data"]["pages"][0]
    )
    errors, failures = [], []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-features=LocalNetworkAccessChecks,LocalNetworkAccessChecksWebSockets"],
        )
        contexts, pages = [], []
        for user, role in (("local-owner", "owner"), ("local-member", "member")):
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                extra_http_headers={
                    "Coders-User-Id": user,
                    "Coders-Resource-Id": "local-design",
                    "Coders-Resource-Role": role,
                },
            )
            if os.environ.get("DESIGN_TEST_CSP") or os.environ.get("DESIGN_TEST_CSP_FILE"):

                async def policy(route):
                    if route.request.resource_type == "document":
                        response = await route.fetch()
                        await route.fulfill(
                            response=response,
                            headers={
                                **response.headers,
                                "Content-Security-Policy": (
                                    Path(os.environ["DESIGN_TEST_CSP_FILE"]).read_text().strip()
                                    if os.environ.get("DESIGN_TEST_CSP_FILE")
                                    else f"default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; frame-src 'self' {base}/chip/app; frame-ancestors 'self'; base-uri 'none'; form-action 'self'; worker-src 'self' blob:; object-src 'none'; manifest-src 'none'"
                                ),
                            },
                        )
                    else:
                        await route.continue_()

                await context.route("**/*", policy)
            page = await context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on(
                "console",
                lambda m: (
                    print("CONSOLE", m.type, m.text[:1200], flush=True)
                    if m.type in ("error", "warning")
                    else None
                ),
            )
            page.on(
                "response",
                lambda r: (
                    failures.append({"url": r.url, "status": r.status}) if r.status >= 400 else None
                ),
            )
            await page.goto(url, wait_until="networkidle", timeout=60000)
            contexts.append(context)
            pages.append(page)
        await pages[0].wait_for_timeout(3000)
        before = (await pages[1].locator("body").inner_text()).count("Rectangle")
        await pages[0].bring_to_front()
        await pages[0].keyboard.press("Escape")
        await pages[0].wait_for_timeout(300)
        await pages[0].keyboard.press("r")
        await pages[0].wait_for_timeout(800)
        await pages[0].mouse.move(500, 300)
        await pages[0].mouse.down()
        await pages[0].wait_for_timeout(200)
        await pages[0].mouse.move(800, 500, steps=12)
        await pages[0].wait_for_timeout(200)
        await pages[0].mouse.up()
        await pages[0].wait_for_timeout(1000)
        await pages[0].keyboard.press("Escape")
        await pages[0].wait_for_timeout(7000)
        print("OWNER", (await pages[0].locator("body").inner_text())[-1800:], flush=True)
        print("MEMBER", (await pages[1].locator("body").inner_text())[-1800:], flush=True)
        await pages[0].screenshot(path="artifacts/drawing.png")
        await pages[1].wait_for_function(
            "n => (document.body.innerText.match(/Rectangle/g)||[]).length > n",
            arg=before,
            timeout=20000,
        )
        # The Core closes long-lived sockets. Verify missed edits are recovered after reconnect.
        before_ellipse = (await pages[1].locator("body").inner_text()).count("Ellipse")
        await contexts[1].set_offline(True)
        await pages[0].bring_to_front()
        await pages[0].keyboard.press("Escape")
        await pages[0].keyboard.press("e")
        await pages[0].wait_for_timeout(800)
        await pages[0].mouse.move(820, 540)
        await pages[0].mouse.down()
        await pages[0].wait_for_timeout(200)
        await pages[0].mouse.move(1020, 720, steps=12)
        await pages[0].wait_for_timeout(200)
        await pages[0].mouse.up()
        await pages[0].wait_for_timeout(7000)
        await contexts[1].set_offline(False)
        await pages[1].wait_for_function(
            "n => (document.body.innerText.match(/Ellipse/g)||[]).length > n",
            arg=before_ellipse,
            timeout=30000,
        )
        await pages[0].wait_for_timeout(2500)
        await pages[0].screenshot(path="artifacts/editor.png", full_page=True)
        saved = await pages[0].request.get(
            base + "/api/main/methods/get-file?id=" + document["id"],
            headers={"Accept": "application/json"},
        )
        assert saved.status == 200
        assert (await saved.json())["revn"] > document["revn"]
        Path("artifacts/edited-file.json").write_text(await saved.text())
        assert not errors, errors
        assert not failures, failures
        report = {
            "url": pages[0].url,
            "title": await pages[0].title(),
            "text": (await pages[0].locator("body").inner_text())[:5000],
            "collaboration": "member received owner rectangle",
            "reconnection": "member recovered ellipse created while offline",
            "errors": errors,
            "failures": failures,
        }
        Path("artifacts/browser.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
