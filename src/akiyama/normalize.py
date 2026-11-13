"""Turn raw files into point-in-time Parquet tables.

Output: ``norm/<dataset>/year=<YYYY>/data.parquet`` (or ``year=all``).
Every row carries:
  available_at        tz-aware JST timestamp (NaT for reference data)
  available_at_basis  how it was derived (exact / assumed_* )
  src_key, src_version  the raw file it came from
Rows are de-duplicated on the dataset's key, keeping the copy from the newest
file (daily "live" files override the monthly historical file).
"""

from __future__ import annotations

import gzip
import json
import logging
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from .datasets import DATASETS, TD_DATASET, Avail, Dataset
from .ingest import key_period
from .rawstore import RawStore, version_slug

log = logging.getLogger(__name__)
JST = "Asia/Tokyo"


# --------------------------------------------------------------- availability
def _norm_time(t: pd.Series) -> pd.Series:
    t = t.fillna("").astype(str).str.strip()
    return t.where(t.str.count(":") != 1, t + ":00")


def _at(dates: pd.Series, times: pd.Series | str) -> pd.Series:
    s = dates.astype(str) + " " + (times if isinstance(times, str) else times)
    return pd.to_datetime(s, format="%Y-%m-%d %H:%M:%S", errors="coerce").dt.tz_localize(JST)


def _hms(hhmm: str) -> str:
    return hhmm if hhmm.count(":") == 2 else hhmm + ":00"


def next_trading_day(dates: pd.Series, trading_days: np.ndarray) -> pd.Series:
    """For each date, the first trading day strictly after it."""
    d = pd.to_datetime(dates, errors="coerce").values.astype("datetime64[D]")
    idx = np.searchsorted(trading_days, d, side="right")
    out = np.full(len(d), np.datetime64("NaT", "D"), dtype="datetime64[D]")
    ok = (idx < len(trading_days)) & ~np.isnat(d)
    out[ok] = trading_days[idx[ok]]
    return pd.Series(pd.to_datetime(out).strftime("%Y-%m-%d"), index=dates.index).where(ok)


def add_availability(
    df: pd.DataFrame, avail: Avail, settings_avail: dict[str, str], trading_days: np.ndarray | None
) -> pd.DataFrame:
    df = df.copy()
    t = _hms(settings_avail.get(avail.time_setting, "23:59")) if avail.time_setting else ""
    if avail.kind == "none":
        df["available_at"] = pd.Series(pd.NaT, index=df.index, dtype=f"datetime64[ns, {JST}]")
        df["available_at_basis"] = "reference"
    elif avail.kind == "exact":
        times = _norm_time(df[avail.time_col])
        at = _at(df[avail.date_col], times)
        missing = times.eq("") | at.isna()
        # no time recorded -> end of that day, flagged
        at = at.where(~missing, _at(df[avail.date_col], "23:59:00"))
        df["available_at"] = at
        df["available_at_basis"] = np.where(missing, "assumed_end_of_day", "exact")
    elif avail.kind == "day_time":
        df["available_at"] = _at(df[avail.date_col], t)
        df["available_at_basis"] = f"assumed_{avail.time_setting}"
    elif avail.kind in ("next_tday", "pub_or_next"):
        if trading_days is None:
            raise RuntimeError("calendar must be normalised before datasets that need trading days")
        base_col = avail.date_col if avail.kind == "next_tday" else avail.fallback_col
        nxt = _at(next_trading_day(df[base_col], trading_days), _hms(settings_avail.get("next_day_time", "23:59")))
        if avail.kind == "next_tday":
            df["available_at"] = nxt
            df["available_at_basis"] = "assumed_next_trading_day"
        else:
            pub = df[avail.date_col].fillna("").astype(str)
            has_pub = pub.str.len() > 0
            df["available_at"] = _at(pub.where(has_pub, "1900-01-01"), t).where(has_pub, nxt)
            df["available_at_basis"] = np.where(has_pub, f"assumed_{avail.time_setting}", "assumed_next_trading_day")
    else:
        raise ValueError(avail.kind)
    return df


# -------------------------------------------------------------------- reading
def _read_csv(path: Path, ds: Dataset) -> pd.DataFrame:
    dtype = {c: "string" for c in ds.str_cols}
    for c in ("Date", "PubDate", "DiscDate", "SchDate", "StDate", "EnDate", "DiscTime"):
        dtype[c] = "string"
    return pd.read_csv(path, dtype=dtype, keep_default_na=True, low_memory=False)


def _partition(period: str) -> str:
    return period[:4] if period else "all"


def _write(df: pd.DataFrame, out_dir: Path, part: str) -> Path:
    d = out_dir / f"year={part}"
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "data.parquet.part"
    df.to_parquet(tmp, index=False)
    final = d / "data.parquet"
    tmp.replace(final)
    return final


def trading_days(norm_dir: Path) -> np.ndarray | None:
    files = list((norm_dir / "calendar").glob("year=*/data.parquet"))
    if not files:
        return None
    cal = pd.concat(pd.read_parquet(f) for f in files)
    days = pd.to_datetime(cal.loc[cal["HolDiv"].astype(str) == "1", "Date"]).values.astype("datetime64[D]")
    return np.unique(days)


def normalize_bulk(store: RawStore, norm_dir: Path, name: str, settings_avail: dict[str, str]) -> int:
    ds = DATASETS[name]
    prefix = ds.endpoint.strip("/") + "/"
    latest = {k: e for k, e in store.latest("bulk").items() if k.startswith(prefix)}
    # /indices/bars/daily must not swallow /indices/bars/daily/topix files
    if name == "indices":
        latest = {k: e for k, e in latest.items() if "/topix/" not in k}
    if not latest:
        log.warning("%s: no raw files", name)
        return 0
    tdays = trading_days(norm_dir) if ds.avail.kind in ("next_tday", "pub_or_next") else None
    groups: dict[str, list] = defaultdict(list)
    for key, e in latest.items():
        groups[_partition(key_period(key))].append((key_period(key), version_slug(e.version), key, e))
    rows = 0
    for part, items in sorted(groups.items()):
        frames = []
        for period, _, key, e in sorted(items):  # monthly before daily, older version first
            df = _read_csv(store.root / e.path, ds)
            df["src_key"], df["src_version"] = key, e.version
            frames.append(df)
        df = pd.concat(frames, ignore_index=True)
        df = df.drop_duplicates(subset=list(ds.key_cols), keep="last")
        df = add_availability(df, ds.avail, settings_avail, tdays)
        _write(df, norm_dir / name, part)
        rows += len(df)
    log.info("%s: %d rows in %d partitions", name, rows, len(groups))
    return rows


def normalize_td(store: RawStore, norm_dir: Path, settings_avail: dict[str, str]) -> int:
    ds = TD_DATASET
    frames = []
    # every stored copy of the rolling 5-year file, oldest first, so disclosures that
    # have left the provider's window are kept
    bulks = sorted(
        (e for e in store.entries() if e.kind == "td_bulk" and (store.root / e.path).exists()),
        key=lambda e: version_slug(e.version),
    )
    seen = set()
    for e in bulks:
        if e.version in seen:
            continue
        seen.add(e.version)
        df = _read_csv(store.root / e.path, ds)
        df["src_key"], df["src_version"] = "td_bulk", e.version
        frames.append(df)
        if len(frames) > 1:  # keep memory flat
            frames = [pd.concat(frames, ignore_index=True).drop_duplicates(subset=list(ds.key_cols), keep="last")]
    for day, e in sorted(store.latest("td_list").items()):
        with gzip.open(store.root / e.path, "rt", encoding="utf-8") as f:
            rows = json.load(f)
        if not rows:
            continue
        df = pd.DataFrame(rows)
        for c in ("DiscItems", "Docs"):
            if c in df:
                df[c] = df[c].map(lambda v: ";".join(map(str, v)) if isinstance(v, list) else v)
        df["src_key"], df["src_version"] = f"td_list/{day}", e.version
        frames.append(df)
    if not frames:
        return 0
    df = pd.concat(frames, ignore_index=True)
    for c in ds.str_cols + ("DiscDate", "DiscTime"):
        if c in df:
            df[c] = df[c].astype("string")
    df = df.drop_duplicates(subset=list(ds.key_cols), keep="last")
    df = add_availability(df, ds.avail, settings_avail, None)
    df["_year"] = df["DiscDate"].str[:4]
    n = 0
    for year, part in df.groupby("_year"):
        _write(part.drop(columns="_year"), norm_dir / "td", year)
        n += len(part)
    return n
