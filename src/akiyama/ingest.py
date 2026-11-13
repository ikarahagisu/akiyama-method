"""Download raw data from J-Quants into the append-only RawStore."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from .datasets import DATASETS, code5
from .jquants import JQuantsClient, JQuantsError
from .rawstore import RawStore

log = logging.getLogger(__name__)

PERIOD_RE = re.compile(r"_(\d{6}|\d{8})\.csv\.gz$")


def key_period(key: str) -> str:
    """'..._202202.csv.gz' -> '202202'; '..._20261113.csv.gz' -> '20261113'; else ''."""
    m = PERIOD_RE.search(key)
    return m.group(1) if m else ""


def _sha256(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return size, h.hexdigest()


@dataclass
class SyncReport:
    downloaded: list[str] = field(default_factory=list)
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"downloaded={len(self.downloaded)} skipped={self.skipped} errors={len(self.errors)}"


def _store_download(client: JQuantsClient, store: RawStore, kind: str, key: str, version: str, url_fn, suffix: str) -> bool:
    """Download one versioned file unless already stored. Returns True if downloaded."""
    if store.has(kind, key, version):
        return False
    dest = store.path_for(kind, key, version, suffix)
    if dest.exists():  # file landed but manifest write was interrupted
        size, sha = _sha256(dest)
    else:
        size, sha = client.download(url_fn(), dest)
    store.record(kind, key, version, dest, size, sha)
    return True


def sync_bulk(
    client: JQuantsClient,
    store: RawStore,
    datasets: Iterable[str],
    since: str = "",
) -> dict[str, SyncReport]:
    """Fetch every bulk file (monthly historical + daily live) not yet stored.

    ``since`` ('YYYY-MM') limits to files whose period starts at or after it.
    A file whose LastModified changed is stored again as a new version.
    """
    since_p = since.replace("-", "")
    reports: dict[str, SyncReport] = {}
    for name in datasets:
        ds = DATASETS[name]
        rep = reports[name] = SyncReport()
        try:
            items = client.bulk_list(ds.endpoint)
        except JQuantsError as exc:
            rep.errors.append(str(exc))
            log.warning("%s: %s", name, exc)
            continue
        for it in items:
            key, version = it["Key"], it.get("LastModified") or "unversioned"
            period = key_period(key)
            if since_p and period and period[: len(since_p)] < since_p:
                rep.skipped += 1
                continue
            try:
                done = _store_download(
                    client, store, "bulk", key, version, lambda k=key: client.bulk_url(k), ".csv.gz"
                )
            except Exception as exc:  # keep going; report at the end
                rep.errors.append(f"{key}: {exc}")
                log.warning("%s: %s", key, exc)
                continue
            if done:
                rep.downloaded.append(key)
                log.info("%s: %s", name, key)
            else:
                rep.skipped += 1
    return reports


def _version_date(version: str) -> date | None:
    try:
        return datetime.fromisoformat(version.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def sync_td(
    client: JQuantsClient, store: RawStore, today: date | None = None, bulk_refresh_days: int = 28
) -> SyncReport:
    """TDnet index.

    The 5-year bulk file is a rolling window, so its old copies are kept and all of
    them are merged at normalisation. To avoid storing ~27 MB every day, a new bulk
    copy is taken only when none is stored or the newest is ``bulk_refresh_days`` old;
    days in between come from per-day ``/td/list`` calls.
    """
    rep = SyncReport()
    today = today or datetime.now(timezone(timedelta(hours=9))).date()
    stored = [e for e in store.entries() if e.kind == "td_bulk" and (store.root / e.path).exists()]
    newest = max((d for d in (_version_date(e.version) for e in stored) if d), default=None)
    if newest is None or (today - newest).days >= bulk_refresh_days:
        info = client.td_bulk()
        version = info.get("lastUpdated", "unversioned")
        if _store_download(client, store, "td_bulk", "tdnet-disclosures-5years", version, lambda: info["url"], ".csv.gz"):
            rep.downloaded.append(f"td_bulk@{version}")
            newest = _version_date(version) or newest
        else:
            rep.skipped += 1
    if newest is None:
        return rep

    # Start one day before the bulk copy (late-evening JST disclosures), or re-check the
    # last two days already fetched by td_list (late additions/corrections).
    start = newest - timedelta(days=1)
    days_done = [date.fromisoformat(k) for k in store.latest("td_list")]
    if days_done:
        start = max(start, max(days_done) - timedelta(days=2))
    d = start
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    while d <= today:
        ds = d.isoformat()
        try:
            rows = client.td_list(date=ds)
        except JQuantsError as exc:
            rep.errors.append(f"td_list {ds}: {exc}")
            d += timedelta(days=1)
            continue
        dest = store.path_for("td_list", ds, stamp, ".json.gz")
        dest.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(rows, ensure_ascii=False).encode("utf-8")
        with gzip.open(dest, "wb") as f:
            f.write(payload)
        size, sha = _sha256(dest)
        store.record("td_list", ds, stamp, dest, size, sha)
        rep.downloaded.append(f"td_list {ds} ({len(rows)})")
        d += timedelta(days=1)
    return rep


def fetch_td_docs(
    client: JQuantsClient,
    store: RawStore,
    disclosures,  # DataFrame with DiscNo, Code, DiscDate
    codes: Iterable[str],
    date_from: str = "",
    date_to: str = "",
    types: Iterable[str] = ("pdf", "summaryPdf"),
) -> SyncReport:
    """Download disclosure documents for the given codes (never the whole market)."""
    rep = SyncReport()
    codes5 = {code5(c) for c in codes}
    df = disclosures[disclosures["Code"].isin(codes5)]
    if date_from:
        df = df[df["DiscDate"] >= date_from]
    if date_to:
        df = df[df["DiscDate"] <= date_to]
    types = list(types)
    for disc_no in sorted(df["DiscNo"].unique()):
        wanted = [t for t in types if not store.has("td_doc", f"{disc_no}/{t}", "1")]
        if not wanted:
            rep.skipped += 1
            continue
        try:
            files = client.td_files(disc_no)
        except JQuantsError as exc:
            rep.errors.append(f"{disc_no}: {exc}")
            continue
        for t in wanted:
            url = files.get(t)
            if not url:
                continue
            suffix = Path(urlparse(url).path).suffix or ".bin"
            try:
                if _store_download(client, store, "td_doc", f"{disc_no}/{t}", "1", lambda u=url: u, suffix):
                    rep.downloaded.append(f"{disc_no}/{t}")
            except Exception as exc:
                rep.errors.append(f"{disc_no}/{t}: {exc}")
    return rep
