"""
TSG Fixture Acquisition Script v7 - TASK-007 Session 2

Confirmed flow:
- ilangoruntuleme.php: TicaretUnvani search, CAPTCHA (kullanici cozer)
- pdf_goster.php: CAPTCHA gate (kullanici cozer), sonra PDF /tmp_gazete/*.pdf'den yuklenior
- Route intercept: /tmp_gazete/*.pdf yakalanir (33KB PDF binary)

Calistir: uv run python scripts/tsg_acquire_fixtures.py
"""
import asyncio
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

FIXTURES_DIR   = Path("tests/fixtures/tsg")
BASE_URL       = "https://www.ticaretsicil.gov.tr"
ILAN_PATH      = "/view/hizlierisim/ilangoruntuleme.php"
SEARCH_COMPANY = "Hera Teknik Yapi"


async def main() -> None:

    from pdfminer.high_level import extract_text as pdf_extract
    from playwright.async_api import async_playwright

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        # ── Fresh login ───────────────────────────────────────────────────
        await page.goto(BASE_URL)
        print("\n=== GIRIS YAPIN (max 180s) ===\n")
        await page.wait_for_selector(
            "a[href*='logout'], a[href*='cikis'], .user-info, #user-panel",
            timeout=180_000,
        )
        print(f"Login basarili. URL: {page.url}\n")

        # ── Ilan araması ──────────────────────────────────────────────────
        if "ilangoruntuleme" not in page.url:
            await page.goto(BASE_URL + ILAN_PATH)
            await page.wait_for_load_state("networkidle")

        await page.fill("input[name='TicaretUnvani']", SEARCH_COMPANY)
        print(f"TicaretUnvani = '{SEARCH_COMPANY}'")
        await page.evaluate("document.getElementById('FormIlanGoruntuleme').requestSubmit()")
        print("Submit. Sonuclar bekleniyor (CAPTCHA cikabilir, max 90s)...")

        await page.wait_for_selector("tbody tr[role='row']", timeout=90_000)
        print("Sonuclar yuklendi.")

        results_html = await page.content()
        (FIXTURES_DIR / "sample_search_results.html").write_text(results_html, encoding="utf-8")
        print(f"Search results -> {len(results_html)} bytes")

        # Find target PDF link (YONETiM tipi)
        pdf_links = await page.query_selector_all("a[href*='pdf_goster']")
        print(f"PDF linkleri: {len(pdf_links)} adet")

        yonetim_link = None
        for row_el in await page.query_selector_all("tbody tr[role='row']"):
            row_text = await row_el.inner_text()
            if any(x in row_text.upper() for x in ['YONET', 'KURUL']):
                link = await row_el.query_selector("a[href*='pdf_goster']")
                if link:
                    yonetim_link = link
                    break

        target_link = yonetim_link or (pdf_links[0] if pdf_links else None)
        if not target_link:
            print("PDF linki bulunamadi.")
            await browser.close()
            return

        href = await target_link.get_attribute("href")
        print(f"\nHedef: {href}")

        # ── PDF yol 1: expect_popup + route intercept ─────────────────────
        pdf_event = asyncio.Event()
        pdf_bytes_holder = []

        async def capture_any_pdf(route, request):
            """Capture PDF from any URL in popup."""
            url = request.url
            # Skip non-HTTP protocols
            if not url.startswith("http"):
                await route.continue_()
                return
            try:
                resp = await route.fetch()
                body = await resp.body()
                ct = resp.headers.get("content-type", "")
                if body[:4] == b'%PDF' or 'application/pdf' in ct:
                    pdf_bytes_holder.append(body)
                    print(f"\nPDF YAKALANDI: {url[:80]} ({len(body)} bytes)")
                    pdf_event.set()
                await route.fulfill(response=resp)
            except Exception as e:
                # Silently skip non-HTTP routes (chrome-extension://, etc.)
                if "chrome-extension" not in str(e):
                    print(f"  Route error: {e}")
                await route.continue_()

        print("\n=== PDF LINKI TIKLANIYOR ===")
        print("Popup'ta CAPTCHA gorunurse COZUN ve DEVAM EDIN (max 120s)...\n")

        try:
            async with page.expect_popup(timeout=120_000) as popup_info:
                await target_link.click()

            popup = await popup_info.value
            await popup.route("**/*", capture_any_pdf)

            # Wait for PDF to be captured or timeout
            try:
                await asyncio.wait_for(pdf_event.wait(), timeout=120.0)
            except asyncio.TimeoutError:
                print("PDF wait timeout.")

            # Get popup content
            try:
                popup_html = await popup.content()
            except Exception:
                popup_html = ""

            try:
                await popup.close()
            except Exception:
                pass

        except Exception as e:
            print(f"Popup hata: {e}")
            popup_html = ""

        # ── PDF kaydet + parse ────────────────────────────────────────────
        if pdf_bytes_holder:
            pdf_bytes = pdf_bytes_holder[0]
            pdf_file = FIXTURES_DIR / "sample_gazette.pdf"
            pdf_file.write_bytes(pdf_bytes)
            print(f"\nPDF kaydedildi -> {pdf_file} ({len(pdf_bytes)} bytes)")

            # Extract text
            pdf_text = pdf_extract(io.BytesIO(pdf_bytes))
            print(f"PDF text ({len(pdf_text)} chars):")
            print("─" * 60)
            print(pdf_text[:3000])
            print("─" * 60)

            # Save as HTML fixture
            detail_html = (
                "<!DOCTYPE html><html lang='tr'><head><meta charset='UTF-8'>"
                "<title>TSG Gazette</title></head><body>"
                "<pre class='gazette-text'>" + pdf_text + "</pre></body></html>"
            )
            detail_file = FIXTURES_DIR / "sample_gazette_detail.html"
            detail_file.write_text(detail_html, encoding="utf-8")
            print(f"\nDetail HTML fixture -> {detail_file}")

        else:
            print("\nPDF yakalanamadi.")
            if popup_html:
                detail_file = FIXTURES_DIR / "sample_gazette_detail.html"
                detail_file.write_text(popup_html, encoding="utf-8")
                print(f"Popup HTML -> {detail_file} ({len(popup_html)} bytes)")

        # ── Ozet ──────────────────────────────────────────────────────────
        print("\n" + "=" * 55)
        print("FIXTURE ACQUISITION OZET")
        print("=" * 55)
        for fname in ["sample_search_results.html", "sample_gazette_detail.html",
                      "sample_gazette.pdf"]:
            f = FIXTURES_DIR / fname
            if f.exists():
                print(f"  {fname}: {f.stat().st_size:,} bytes")
            else:
                print(f"  {fname}: YOK")

        await page.wait_for_timeout(3_000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
