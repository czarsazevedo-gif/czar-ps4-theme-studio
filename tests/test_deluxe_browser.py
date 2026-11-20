"""Touch Playwright verification for Ouroboros Cinematic Deluxe on Galaxy Tab S10 Ultra viewports."""

from __future__ import annotations

import io
import os
import socket
import tempfile
import zipfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from browser_server import server
from tests import fixtures_deluxe as fx


def main():
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        root = Path(tmp)
        frames, _ = fx.scenario_k_static_dynamic(16, size=(320, 180))
        movie = fx.write_mp4(root / "deluxe_scene.mp4", frames, fps=10.0)

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        browser = pw.chromium.launch(executable_path=os.environ.get("CZAR_CHROMIUM"))
        ctx = browser.new_context(viewport={"width": 1480, "height": 924}, has_touch=True, is_mobile=True)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        with server(root / "projects", port) as base:
            # 1. Create project & upload animated source
            page.goto(base)
            page.locator("#project-new").tap()
            page.locator("#project-dialog-name").fill("Ouroboros Deluxe")
            page.get_by_role("button", name="Confirmar", exact=True).tap()
            expect(page.locator("#project-name")).to_have_value("Ouroboros Deluxe")
            page.locator("label:has(#bgmode-animated)").tap()
            page.locator("#anim-file").set_input_files(movie)
            expect(page.locator("#anim-src-name")).to_have_text(movie.name, timeout=45_000)
            expect(page.locator("#project-status")).to_have_text("Salvo", timeout=20_000)

            # 2. Open Ouroboros Cinematic Deluxe Studio
            page.locator("#ouroboros-link").tap()
            expect(page.locator("#video")).to_contain_text(movie.name)
            deluxe_url = page.url

            # 3. Run Sentinela Supreme (A–E) + Círculo Supreme
            page.locator("#deluxe-analyze").tap()
            expect(page.locator("#deluxe-diag")).to_be_visible(timeout=120_000)
            expect(page.locator("#deluxe-summary")).to_contain_text("Classificação Supreme")

            # 4. Run Temporal Calibrator Supreme
            page.locator("#dlx-end").fill("1.6")
            page.locator("#dlx-calibrate").tap()
            expect(page.locator("#dlx-cal-recs .dlx-card")).to_have_count(5, timeout=120_000)

            # 5. Draw a regional protection mask via touch Pointer Events, test Undo/Redo, and save recipe
            cv = page.locator("#dlx-region-canvas")
            cv.scroll_into_view_if_needed()
            box = cv.bounding_box()
            assert box is not None
            page.mouse.move(box["x"] + box["width"] * 0.1, box["y"] + box["height"] * 0.1)
            page.mouse.down()
            page.mouse.move(box["x"] + box["width"] * 0.9, box["y"] + box["height"] * 0.45)
            page.mouse.up()
            expect(page.locator("#dlx-region-list li")).to_have_count(1)
            page.locator("#dlx-undo").tap()
            expect(page.locator("#dlx-region-list li")).to_have_count(0)
            page.locator("#dlx-redo").tap()
            expect(page.locator("#dlx-region-list li")).to_have_count(1)
            page.locator("#dlx-save-recipe").tap()
            expect(page.locator("#dlx-recipe-status")).to_contain_text("Receita salva", timeout=15_000)

            # 6. Render Deluxe loop with Deluxe Finish + post-DDS validation
            page.locator("#dlx-frames").fill("8")
            page.locator("#dlx-wait").fill("0.05")
            page.locator("#dlx-render").tap()
            expect(page.locator("#player")).to_be_visible(timeout=120_000)
            expect(page.locator("#measurement")).to_contain_text("Deluxe Pós-DDS")

            # 7. Verify 10 full playback cycles + live rAF presentation telemetry
            page.wait_for_function(
                "Number(document.querySelector('#counter').textContent.match(/(\\d+) voltas/)?.[1]) >= 10",
                timeout=60_000,
            )
            expect(page.locator("#dlx-telemetry")).to_contain_text("Telemetria player:")

            # 8. Portrait orientation check (924x1480) — zero horizontal overflow + seam inspection
            page.set_viewport_size({"width": 924, "height": 1480})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            page.locator("#seam").tap()
            expect(page.locator("#counter")).to_have_text("Inspeção da junção")

            # Verify Deluxe RAF download works
            dl_href = page.locator("#download").get_attribute("href")
            dl_resp = ctx.request.get(dl_href)
            assert dl_resp.ok and dl_resp.body().startswith(b"PK")
            with zipfile.ZipFile(io.BytesIO(dl_resp.body())) as zf:
                dds_entries = [k for k in zf.namelist() if k.endswith(".dds")]
                assert len(dds_entries) == 8

            # 9. Apply Deluxe loop transactionally to the active project & return to main editor
            page.locator("#dlx-apply").tap()
            expect(page.locator("#dlx-apply-status")).to_contain_text("Aplicado ao projeto!", timeout=30_000)
            page.locator("#back").tap()
            expect(page.locator("#anim-src-name")).to_contain_text("Ouroboros Deluxe", timeout=30_000)
            expect(page.locator("#anim-frame-total")).to_have_text("08", timeout=15_000)

        # 10. Restart Uvicorn server -> reload project -> confirm persisted Deluxe loop & recipe!
        with server(root / "projects", port):
            page.reload()
            expect(page.locator("#anim-src-name")).to_contain_text("Ouroboros Deluxe", timeout=30_000)
            expect(page.locator("#anim-frame-total")).to_have_text("08", timeout=15_000)
            page.goto(deluxe_url)
            expect(page.locator("#dlx-region-list li")).to_have_count(1, timeout=15_000)

        assert not errors, errors
        browser.close()
    print("Ouroboros Cinematic Deluxe Chromium touch (paisagem/retrato, 10 ciclos, máscaras, aplicar, reinício) OK")


if __name__ == "__main__":
    main()
