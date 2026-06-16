"""Test retry/backoff của ISAPIClient._get_xml và helper fingerprint."""

import asyncio

import httpx
import pytest

from app.collector.isapi_client import (
    ISAPIAuthError,
    ISAPIClient,
    normalize_fingerprint,
)

_XML_OK = "<DeviceInfo><model>DS-7616</model></DeviceInfo>"


class _FakeResp:
    def __init__(self, status_code=200, text=_XML_OK):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


class _FakeClient:
    """Giả lập httpx.AsyncClient.get: raise N lần rồi trả response."""

    def __init__(self, fail_times=0, exc=httpx.ReadTimeout("timeout"), resp=None):
        self.fail_times = fail_times
        self.exc = exc
        self.resp = resp or _FakeResp()
        self.calls = 0

    async def get(self, path, auth=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return self.resp


def _client(retries):
    return ISAPIClient(
        "h", "u", "p", retries=retries, retry_backoff_base=0.0  # base 0 -> không sleep lâu
    )


def test_retry_succeeds_after_transient_errors():
    fake = _FakeClient(fail_times=2)
    root = asyncio.run(_client(retries=2)._get_xml(fake, "/x"))
    assert fake.calls == 3  # 2 lần lỗi + 1 lần thành công
    assert root.find("model").text == "DS-7616"


def test_retry_exhausted_raises():
    fake = _FakeClient(fail_times=5)
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(_client(retries=2)._get_xml(fake, "/x"))
    assert fake.calls == 3  # 1 + 2 retry


def test_401_not_retried():
    fake = _FakeClient(fail_times=0, resp=_FakeResp(status_code=401))
    with pytest.raises(ISAPIAuthError):
        asyncio.run(_client(retries=3)._get_xml(fake, "/x"))
    assert fake.calls == 1  # 401 -> raise ngay, không retry


def test_normalize_fingerprint():
    assert normalize_fingerprint("AB:cd:EF 12") == "abcdef12"
    assert normalize_fingerprint("a1b2") == normalize_fingerprint("A1:B2")
