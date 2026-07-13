"""Playwright client for ticaretsicil.gov.tr (TOBB gazette portal).

Recon (TASK-007 S2 + S2.5) confirmed:
- Login form (FormUyeGirisi) has a 4-char image CAPTCHA (img#CaptchaImg,
  input#Captcha, submit via button.c-btn-login).
- Search form (FormIlanGoruntuleme) has NO CAPTCHA for authenticated users.
- PDF popup (pdf_goster.php) has NO extra CAPTCHA for authenticated users;
  the PDF is served directly from /tmp_gazete/{guid}.pdf.

Semi-automatic by design: the session opens a visible (headful) browser and
the operator logs in once - solving the login CAPTCHA by hand - after which
the search and PDF-fetch steps run automatically for the rest of the run.
The site's anti-bot control is respected, not circumvented.
"""
from __future__ import annotations

from types import TracebackType
from typing import Any

from trailing_edge.core.config import get_config
from trailing_edge.core.logging import get_logger
from trailing_edge.scrapers.ticaret_sicil.parser import IlanRow, parse_search_results

_log = get_logger(__name__)

_LOGGED_IN_SELECTOR = (
    "a[href*='logout'], a[href*='cikis'], .user-info, #user-panel, "
    ".nav-user, [class*='user-name']"
)
_RESULTS_SELECTOR = "tbody tr[role='row']"


class TsgClient:
    """Headful Playwright session against ticaretsicil.gov.tr."""

    def __init__(self) -> None:
        cfg = get_config().get("tsg", {})
        self._base_url: str = cfg.get("base_url", "https://www.ticaretsicil.gov.tr")
        self._ilan_path: str = cfg.get("ilan_path", "/view/hizlierisim/ilangoruntuleme.php")
        self._login_timeout = int(cfg.get("login_timeout_s", 180)) * 1000
        self._pw: Any = None
        self._browser: Any = None
        self._ctx: Any = None
        self._page: Any = None

    async def __aenter__(self) -> "TsgClient":
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        # Headful by design: the operator solves the login CAPTCHA by hand.
        self._browser = await self._pw.chromium.launch(headless=False)
        self._ctx = await self._browser.new_context()
        self._page = await self._ctx.new_page()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def login(self) -> None:
        """Log in to ticaretsicil.gov.tr.

        Opens a visible browser and waits for the operator to sign in,
        solving the login CAPTCHA by hand. Once the authenticated state is
        detected, the rest of the run proceeds automatically.
        """
        assert self._page is not None
        page = self._page
        await page.goto(self._base_url)

        print("\n=== TSG GİRİŞ GEREKLİ ===")
        print("Açılan tarayıcıda ticaretsicil.gov.tr'a giriş yapın (CAPTCHA dahil).")
        print(f"Giriş algılanınca otomatik devam eder (max {self._login_timeout // 1000}s)...\n")
        await page.wait_for_selector(_LOGGED_IN_SELECTOR, timeout=self._login_timeout)

        _log.info("tsg_login_ok")

    async def search_company(self, company_name: str) -> list[IlanRow]:
        """Search gazette announcements by trade name. Returns ilan rows.

        The search form (FormIlanGoruntuleme) uses AJAX and requires a
        SicilMudurluguId (Chamber district) value. To cover all chambers,
        we query İSTANBUL (232), ANKARA (18), and İZMİR (233) separately
        and merge results - most target companies are in one of these three.
        The AJAX endpoint is called directly via page.evaluate() to bypass
        jQuery form validation.
        """
        assert self._page is not None
        page = self._page

        # Always navigate to ensure jQuery and form are loaded.
        await page.goto(self._base_url + self._ilan_path)
        await page.wait_for_load_state("networkidle")

        # Search across major chambers: Istanbul, Ankara, Izmir.
        # FormIlanGoruntuleme requires SicilMudurluguId (validated by jQuery).
        # We select each chamber, trigger jQuery submit, wait for DivIlanSonuc.
        _MAJOR_CHAMBERS = ["232", "18", "233"]  # İSTANBUL, ANKARA, İZMİR
        all_rows: list[IlanRow] = []

        for chamber_id in _MAJOR_CHAMBERS:
            await page.select_option("select#SicilMudurluguId", value=chamber_id)
            await page.fill("input#TicaretUnvani", company_name)
            # Trigger jQuery submit handler (runs validation + AJAX, no CAPTCHA)
            await page.evaluate("$('#FormIlanGoruntuleme').trigger('submit')")
            # Wait for AJAX to populate DivIlanSonuc
            try:
                await page.wait_for_function(
                    "() => document.getElementById('DivIlanSonuc') && "
                    "document.getElementById('DivIlanSonuc').innerHTML.trim() !== ''",
                    timeout=15_000,
                )
            except Exception:
                pass  # No results for this chamber - continue

            div_html = await page.evaluate(
                "document.getElementById('DivIlanSonuc') ? "
                "document.getElementById('DivIlanSonuc').innerHTML : ''"
            )
            rows = parse_search_results(f"<html><body>{div_html}</body></html>")
            all_rows.extend(rows)

            # Clear DivIlanSonuc before next chamber search
            await page.evaluate(
                "if (document.getElementById('DivIlanSonuc')) "
                "document.getElementById('DivIlanSonuc').innerHTML = ''"
            )

        _log.info("tsg_search_done", company=company_name, rows=len(all_rows))
        return all_rows

    async def fetch_pdf_bytes(self, pdf_guid: str) -> bytes | None:
        """Fetch the gazette PDF for authenticated users.

        For authenticated users pdf_goster.php embeds the real PDF URL in an
        <object data="/tmp_gazete/..."> tag. In headless mode Chromium does not
        render <object>/<embed> PDF plugins, so route-interception never fires.
        Fix: navigate to the gate page, extract the /tmp_gazete/ URL from the
        HTML, then httpx-GET the PDF bytes using the Playwright context cookies.
        """
        assert self._ctx is not None

        gate_url = (
            f"{self._base_url}/view/hizlierisim/pdf_goster.php?Guid={pdf_guid}"
        )
        _log.debug("tsg_pdf_opening", guid=pdf_guid[:12])
        popup = await self._ctx.new_page()
        try:
            await popup.goto(gate_url, wait_until="networkidle", timeout=30_000)
            html = await popup.content()
        except Exception as e:
            _log.warning("tsg_pdf_gate_error", guid=pdf_guid, error=str(e))
            html = ""
        finally:
            try:
                await popup.close()
            except Exception:
                pass

        # Extract /tmp_gazete/ URL from <object data="..."> or <embed src="...">
        import re as _re
        m = _re.search(
            r'(?:data|src)="(/tmp_gazete/[^"]+\.pdf)"',
            html,
            _re.IGNORECASE,
        )
        if not m:
            _log.warning("tsg_pdf_url_not_found", guid=pdf_guid)
            return None

        tmp_pdf_path = m.group(1)
        full_pdf_url = self._base_url + tmp_pdf_path

        # Fetch with the authenticated session cookies via httpx
        cookies = {c["name"]: c["value"] for c in await self._ctx.cookies()}
        import httpx
        try:
            async with httpx.AsyncClient(cookies=cookies, follow_redirects=True,
                                         timeout=30.0) as http:
                resp = await http.get(full_pdf_url)
                resp.raise_for_status()
                pdf_bytes = resp.content
        except Exception as e:
            _log.warning("tsg_pdf_download_error", guid=pdf_guid, error=str(e))
            return None

        _log.info("tsg_pdf_fetched", guid=pdf_guid, bytes=len(pdf_bytes))
        return pdf_bytes
