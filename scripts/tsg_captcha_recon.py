"""
TSG CAPTCHA Selector Recon - Session 2.5 ADIM 0

Captures exact CAPTCHA img src and input selectors for:
1. ilangoruntuleme.php search form
2. pdf_goster.php popup

Run: uv run python scripts/tsg_captcha_recon.py
"""
import asyncio
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE_URL   = "https://www.ticaretsicil.gov.tr"
ILAN_PATH  = "/view/hizlierisim/ilangoruntuleme.php"
FIXTURES   = Path("tests/fixtures/tsg")


async def inspect_inputs(page, label: str):
    """Print all form inputs and CAPTCHA images on current page."""
    print(f"\n{'='*60}")
    print(f"FORM INPUTS ({label}) - URL: {page.url}")
    print('='*60)
    inputs = await page.query_selector_all("form input, form select, form textarea")
    for el in inputs:
        name = await el.get_attribute("name") or ""
        id_  = await el.get_attribute("id") or ""
        typ  = await el.get_attribute("type") or "text"
        maxl = await el.get_attribute("maxlength") or ""
        ph   = await el.get_attribute("placeholder") or ""
        print(f"  input name={name!r} id={id_!r} type={typ!r} maxlength={maxl!r} placeholder={ph[:30]!r}")

    captchas = await page.query_selector_all("img[src*='captcha'], img[src*='Captcha']")
    print(f"\nCAPTCHA images: {len(captchas)}")
    for img in captchas:
        src = await img.get_attribute("src") or ""
        id_ = await img.get_attribute("id") or ""
        cls = await img.get_attribute("class") or ""
        print(f"  img src={src!r} id={id_!r} class={cls!r}")

    forms = await page.query_selector_all("form")
    print(f"\nForms: {len(forms)}")
    for form in forms:
        name = await form.get_attribute("name") or ""
        id_  = await form.get_attribute("id") or ""
        method = await form.get_attribute("method") or ""
        action = await form.get_attribute("action") or ""
        print(f"  form name={name!r} id={id_!r} method={method!r} action={action[:60]!r}")


async def main() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx  = await browser.new_context()
        page = await ctx.new_page()

        # ── Login ────────────────────────────────────────────────────────
        await page.goto(BASE_URL)
        print("=== GIRIS YAPIN (max 180s) ===")
        await page.wait_for_selector(
            "a[href*='logout'], a[href*='cikis'], .user-info, #user-panel",
            timeout=180_000,
        )
        print("Login OK\n")

        # ── ilangoruntuleme.php ───────────────────────────────────────────
        await page.goto(BASE_URL + ILAN_PATH)
        await page.wait_for_load_state("networkidle")
        await inspect_inputs(page, "ilangoruntuleme.php BEFORE search")

        # Save the search form HTML
        html = await page.content()
        (FIXTURES / "ilan_search_form.html").write_text(html, encoding="utf-8")
        print(f"\nSaved: tests/fixtures/tsg/ilan_search_form.html ({len(html)} bytes)")

        # ── Submit search to get result page, then click a PDF link ──────
        print("\n--- Submitting 'Hera Teknik' search ---")
        await page.fill("input[name='TicaretUnvani']", "Hera Teknik")
        print("Form filled. CAPTCHA cozun ve SORGULA'ya basin (max 90s)...")
        await page.wait_for_selector("tbody tr[role='row']", timeout=90_000)
        print("Search results loaded.")

        # Click the first PDF link to see the popup
        pdf_links = await page.query_selector_all("a[href*='pdf_goster']")
        print(f"\nPDF links in results: {len(pdf_links)}")
        if not pdf_links:
            print("No PDF links found. Exiting.")
            await browser.close()
            return

        print("\n--- Opening PDF popup (click first link) ---")
        print("POPUP'TA CAPTCHA CIKACAK - goruntuleyin ama COZMEYIN (sadece inspect)")
        print("5 saniye bekleniyor popup icin...\n")

        async with page.expect_popup(timeout=30_000) as popup_info:
            await pdf_links[0].click()

        popup = await popup_info.value
        await popup.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(2)

        await inspect_inputs(popup, "pdf_goster.php POPUP")
        popup_html = await popup.content()
        (FIXTURES / "pdf_popup.html").write_text(popup_html, encoding="utf-8")
        print(f"\nSaved: tests/fixtures/tsg/pdf_popup.html ({len(popup_html)} bytes)")

        print("\n" + "="*60)
        print("RECON TAMAMLANDI")
        print("="*60)
        print("Yukaridaki 'input name=' ve 'img src=' degerlerini client.py'a yaz.")
        print("\nBekliyor (Enter'a bas kapatmak icin)...")

        try:
            await popup.close()
        except Exception:
            pass

        # Keep browser open for manual inspection
        await page.wait_for_timeout(10_000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
