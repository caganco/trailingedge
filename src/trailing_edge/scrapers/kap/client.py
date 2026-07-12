"""Low-level KAP HTTP client: warmup, list, detail, PDF download + unwrap."""
from datetime import date, timedelta

from trailing_edge.core.config import get_config
from trailing_edge.core.http import RateLimitedClient
from trailing_edge.core.logging import get_logger

_log = get_logger(__name__)

_PDF_LEN_OFFSET = 23
_PDF_DATA_OFFSET = 27

# KAP's /disclosure/members/byCriteria returns at most this many rows. On overflow it
# keeps the NEWEST rows and drops the older head of the requested range, with no error
# and no pagination cursor - measured, not documented. Any query that comes back at
# exactly this size must be assumed incomplete and bisected.
LIST_RESULT_CAP = 2000


def unwrap_java_pdf(raw: bytes) -> bytes:
    """Strip the Java-serialized byte[] wrapper and return raw PDF bytes."""
    length = int.from_bytes(raw[_PDF_LEN_OFFSET:_PDF_DATA_OFFSET], "big")
    return raw[_PDF_DATA_OFFSET: _PDF_DATA_OFFSET + length]


class KapClient:
    def __init__(self, http_client: RateLimitedClient) -> None:
        cfg = get_config()
        self._base = cfg["kap"]["base_url"].rstrip("/")
        self._endpoints = cfg["kap"]["endpoints"]
        self._filters = cfg["kap"]["filters"]
        self._http = http_client

    async def warmup(self) -> None:
        url = self._base + self._endpoints["warmup"]
        await self._http.get(url)
        _log.info("kap_warmup_done")

    async def _fetch_list_raw(self, from_date: date, to_date: date) -> list[dict]:
        """One unfiltered POST to the list endpoint. May be silently truncated."""
        url = self._base + self._endpoints["list"]
        payload = {
            "fromDate": from_date.strftime("%Y-%m-%d"),
            "toDate": to_date.strftime("%Y-%m-%d"),
            "mkkMemberOidList": [],
            "subjectList": [],
        }
        resp = await self._http.post(url, json=payload)
        data = resp.json()
        return data if isinstance(data, list) else []

    async def _fetch_list_complete(self, from_date: date, to_date: date) -> list[dict]:
        """Every disclosure in [from_date, to_date], working around KAP's result cap.

        The endpoint returns at most LIST_RESULT_CAP rows and, when it truncates, keeps
        the NEWEST ones and silently drops the older head of the range - it does not
        error, paginate, or signal the loss in any way. Asking for all of March 2024
        returns 2,000 rows spanning 25-31 March; the first 24 days are simply gone.

        There is no server-side subject filter to lean on (subjectList expects member
        OIDs, not the display string, and a disclosureClass key is not honoured), so
        the range itself has to be narrowed until it fits. Whenever a window comes back
        at the cap we bisect it and recurse, which converges regardless of how dense
        KAP's traffic was on any given day. Results are merged on disclosureIndex
        because a bisected boundary can return the same row twice.
        """
        data = await self._fetch_list_raw(from_date, to_date)

        if len(data) < LIST_RESULT_CAP:
            return data

        if from_date >= to_date:
            # A single day over the cap cannot be narrowed further by date. Report the
            # loss loudly rather than returning a quietly incomplete day.
            _log.error(
                "kap_list_day_over_cap",
                day=str(from_date),
                cap=LIST_RESULT_CAP,
                hint="single day exceeds the result cap; disclosures on this day are incomplete",
            )
            return data

        span = (to_date - from_date).days
        mid = from_date + timedelta(days=span // 2)
        _log.info(
            "kap_list_bisect",
            from_date=str(from_date),
            to_date=str(to_date),
            reason="response_at_cap",
        )

        left = await self._fetch_list_complete(from_date, mid)
        right = await self._fetch_list_complete(mid + timedelta(days=1), to_date)

        merged: dict[str, dict] = {}
        for item in (*left, *right):
            key = str(item.get("disclosureIndex", ""))
            if key:
                merged[key] = item
        return list(merged.values())

    async def fetch_disclosure_list(self, from_date: date, to_date: date) -> list[dict]:
        """Insider (DKB) disclosures in the range, complete - never silently truncated."""
        data = await self._fetch_list_complete(from_date, to_date)

        target_subject = self._filters["target_subject"]
        target_class = self._filters["target_class"]
        filtered = [
            d for d in data
            if d.get("subject") == target_subject and d.get("disclosureClass") == target_class
        ]
        _log.info(
            "kap_list_fetched",
            total=len(data),
            filtered=len(filtered),
            from_date=str(from_date),
            to_date=str(to_date),
        )
        return filtered

    async def fetch_disclosure_detail(self, disclosure_index: str) -> dict:
        path = self._endpoints["detail"].format(disclosure_index=disclosure_index)
        url = self._base + path
        resp = await self._http.get(url)
        data = resp.json()
        # API returns a list with one element; unwrap it
        if isinstance(data, list):
            return data[0] if data else {}
        return data

    async def fetch_pdf(self, obj_id: str) -> bytes:
        path = self._endpoints["pdf"].format(obj_id=obj_id)
        url = self._base + path
        resp = await self._http.get(url)
        raw = resp.content
        pdf = unwrap_java_pdf(raw)
        _log.debug("pdf_unwrapped", obj_id=obj_id, raw_len=len(raw), pdf_len=len(pdf))
        return pdf
