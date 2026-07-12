"""Which transport failures get retried inline, and which are deferred.

The retry policy is not a detail: the old one (retry RemoteProtocolError, 5 attempts,
4+8+16+32s exponential) burned 83% of a backfill's wall clock re-poking KAP's WAF and
still lost 12% of the disclosures. A WAF disconnect must fail fast so the scraper can
defer it to a proper cooldown pass; a genuine network blip must still be retried.
"""
import httpx
import pytest

from trailing_edge.core.http import _is_retryable


def test_waf_disconnect_is_not_retried_inline():
    """'Server disconnected without sending a response' is the WAF, not a blip.

    It does not lift in seconds, so an inline retry just re-triggers the block. It
    is deferred to the scraper's per-chunk cooldown instead.
    """
    assert _is_retryable(httpx.RemoteProtocolError("Server disconnected")) is False


def test_transient_network_failures_are_retried_inline():
    assert _is_retryable(httpx.ConnectError("connection refused")) is True
    assert _is_retryable(httpx.ReadTimeout("timed out")) is True


@pytest.mark.parametrize("status", [429, 503])
def test_backpressure_statuses_are_retried_inline(status):
    resp = httpx.Response(status, request=httpx.Request("GET", "https://kap.org.tr"))
    exc = httpx.HTTPStatusError("throttled", request=resp.request, response=resp)
    assert _is_retryable(exc) is True


@pytest.mark.parametrize("status", [400, 403, 404, 500])
def test_other_statuses_are_not_retried(status):
    resp = httpx.Response(status, request=httpx.Request("GET", "https://kap.org.tr"))
    exc = httpx.HTTPStatusError("nope", request=resp.request, response=resp)
    assert _is_retryable(exc) is False


def test_unrelated_exceptions_are_not_retried():
    assert _is_retryable(ValueError("bad parse")) is False
