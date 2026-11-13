"""Minimal J-Quants API v2 client.

* Auth: ``x-api-key`` header, sent only to the API host. Presigned download URLs
  (bulk files, TDnet documents) are fetched without the key.
* Pagination: follows ``pagination_key`` until exhausted.
* Retries 429/5xx and connection errors with backoff (honours Retry-After).
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

RETRY_STATUS = {429, 500, 502, 503, 504}


class JQuantsError(RuntimeError):
    def __init__(self, status: int, message: str, path: str):
        super().__init__(f"J-Quants {status} on {path}: {message}")
        self.status = status
        self.message = message
        self.path = path


class JQuantsClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.jquants.com/v2",
        min_interval_sec: float = 0.6,
        max_retries: int = 5,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.api_host = urlparse(self.base_url).hostname
        self.min_interval_sec = min_interval_sec
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.sleep = sleep
        self.timeout = timeout
        self._last_call = 0.0
        self.calls = 0

    # ------------------------------------------------------------------ core
    def _throttle(self) -> None:
        wait = self._last_call + self.min_interval_sec - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._last_call = time.monotonic()

    def _request(self, url: str, params: dict[str, Any] | None, with_key: bool, stream: bool = False):
        headers = {"User-Agent": "akiyama-method/0.1"}
        if with_key:
            if urlparse(url).hostname != self.api_host:
                raise ValueError(f"refusing to send API key to {url}")
            headers["x-api-key"] = self.api_key
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if with_key:
                self._throttle()
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout, stream=stream)
            except requests.RequestException as exc:  # connection reset, timeout...
                last_exc = exc
                self.sleep(min(60.0, 2.0**attempt))
                continue
            self.calls += 1
            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                ra = resp.headers.get("Retry-After")
                delay = float(ra) if ra and ra.replace(".", "", 1).isdigit() else min(60.0, 2.0**attempt)
                self.sleep(delay)
                continue
            return resp
        raise RuntimeError(f"request failed after retries: {url}") from last_exc

    def get(self, path: str, params: dict[str, Any] | None = None, paginate: bool = True) -> dict[str, Any]:
        """GET an API path. With ``paginate`` the ``data`` lists of all pages are merged."""
        url = f"{self.base_url}{path}"
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        merged: dict[str, Any] | None = None
        while True:
            resp = self._request(url, query, with_key=True)
            try:
                payload = resp.json()
            except ValueError:
                payload = {"message": resp.text[:300]}
            if resp.status_code != 200:
                raise JQuantsError(resp.status_code, str(payload.get("message", payload)), path)
            if merged is None:
                merged = payload
            elif isinstance(payload.get("data"), list):
                merged["data"].extend(payload["data"])
            pk = payload.get("pagination_key")
            if not (paginate and pk):
                merged.pop("pagination_key", None)
                return merged
            query["pagination_key"] = pk

    def download(self, url: str, dest: Path) -> tuple[int, str]:
        """Stream a presigned URL to ``dest`` (via a temp file). Returns (size, sha256)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        resp = self._request(url, None, with_key=False, stream=True)
        if resp.status_code != 200:
            raise JQuantsError(resp.status_code, resp.text[:200], urlparse(url).path)
        h = hashlib.sha256()
        size = 0
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
        os.replace(tmp, dest)
        return size, h.hexdigest()

    # ------------------------------------------------------------- endpoints
    def bulk_list(self, endpoint: str) -> list[dict[str, Any]]:
        return self.get("/bulk/list", {"endpoint": endpoint}).get("data", [])

    def bulk_url(self, key: str) -> str:
        return self.get("/bulk/get", {"key": key}, paginate=False)["url"]

    def td_bulk(self) -> dict[str, Any]:
        return self.get("/td/bulk", paginate=False)

    def td_list(self, date: str = "", code: str = "", from_: str = "", to: str = "") -> list[dict[str, Any]]:
        return self.get("/td/list", {"date": date, "code": code, "from": from_, "to": to}).get("data", [])

    def td_files(self, disc_no: str) -> dict[str, str]:
        return self.get("/td/files", {"discNo": disc_no}, paginate=False).get("files", {})
