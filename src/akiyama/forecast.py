"""Chronos-2 forecasts with an append-only, resumable cache.

Cache: ``derived/forecasts/<model_tag>/part-*.parquet`` with one row per (Code, Date):
  last_close, q10, q50, q90 (horizon-step forecast of split-adjusted close),
  r_hat = q50 / last_close - 1, n_ctx, model, revision, created_at.
The context for (Code, Date) uses closes up to and including Date only
(available 16:30 JST, before the 18:00 decision time).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

import duckdb
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# predictor: list of 1-D float arrays -> array (n, len(quantiles)) at the horizon step
Predictor = Callable[[Sequence[np.ndarray]], np.ndarray]


def model_tag(strategy: dict) -> str:
    c = strategy["chronos"]
    return f"{c['model'].replace('/', '__')}@{c['revision'][:12]}_ctx{c['context']}_h{c['horizon']}"


def chronos_predictor(strategy: dict) -> Predictor:
    import torch
    from chronos import Chronos2Pipeline

    c = strategy["chronos"]
    pipe = Chronos2Pipeline.from_pretrained(c["model"], revision=c["revision"], device_map="cpu", dtype=torch.float32)

    def predict(series: Sequence[np.ndarray]) -> np.ndarray:
        inp = [torch.tensor(s, dtype=torch.float32) for s in series]
        q, _ = pipe.predict_quantiles(
            inp, prediction_length=c["horizon"], quantile_levels=list(c["quantiles"]), batch_size=c["batch_size"]
        )
        return np.stack([x[0, -1, :].numpy() for x in q])

    return predict


def cached_keys(cache_dir: Path) -> set[tuple[str, str]]:
    files = list(Path(cache_dir).glob("part-*.parquet"))
    if not files:
        return set()
    df = duckdb.sql(f"SELECT DISTINCT Code, Date FROM read_parquet('{(Path(cache_dir) / 'part-*.parquet').as_posix()}')").df()
    return set(zip(df["Code"].astype(str), df["Date"].astype(str)))


def load_closes(con, codes: Sequence[str]) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Code -> (dates as 'YYYY-MM-DD' array, adjusted closes) sorted by date."""
    con.register("_codes", pd.DataFrame({"Code": list(codes)}))
    df = con.execute(
        "SELECT Code, Date, AdjC FROM prices_adj WHERE Code IN (SELECT Code FROM _codes) AND AdjC IS NOT NULL ORDER BY Code, Date"
    ).df()
    out = {}
    for code, g in df.groupby("Code", sort=False):
        out[str(code)] = (g["Date"].astype(str).to_numpy(), g["AdjC"].to_numpy(dtype=float))
    return out


def run_forecasts(
    con,
    tasks: pd.DataFrame,
    strategy: dict,
    cache_dir: Path,
    predictor: Predictor,
    chunk: int = 4096,
    limit: int | None = None,
    progress: Callable[[str], None] = print,
) -> int:
    """Forecast every (Code, Date) in ``tasks`` not already cached. Resumable."""
    c = strategy["chronos"]
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    done = cached_keys(cache_dir)
    todo = tasks[[(str(a), str(b)) not in done for a, b in zip(tasks["Code"], tasks["Date"])]]
    todo = todo.drop_duplicates(["Code", "Date"]).sort_values(["Date", "Code"])
    if limit:
        todo = todo.head(limit)
    total = len(todo)
    if not total:
        progress("forecasts: nothing to do")
        return 0
    closes = load_closes(con, sorted(todo["Code"].astype(str).unique()))
    written = 0
    t0 = time.time()
    for start in range(0, total, chunk):
        part = todo.iloc[start : start + chunk]
        series, keep = [], []
        for code, d in zip(part["Code"].astype(str), part["Date"].astype(str)):
            dates, vals = closes.get(code, (np.array([]), np.array([])))
            i = np.searchsorted(dates, d, side="right")  # index after Date
            if i == 0 or dates[i - 1] != d:
                continue  # no close on that day
            ctx = vals[max(0, i - c["context"]) : i]
            if len(ctx) < c["min_history"]:
                continue
            series.append(ctx)
            keep.append((code, d, ctx[-1], len(ctx)))
        if not series:
            continue
        q = predictor(series)
        rows = pd.DataFrame(keep, columns=["Code", "Date", "last_close", "n_ctx"])
        for j, lvl in enumerate(c["quantiles"]):
            rows[f"q{int(round(lvl * 100))}"] = q[:, j]
        rows["r_hat"] = rows["q50"] / rows["last_close"] - 1
        rows["model"], rows["revision"] = c["model"], c["revision"]
        rows["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        tmp = cache_dir / f"part-{stamp}.parquet.part"
        rows.to_parquet(tmp, index=False)
        tmp.replace(cache_dir / f"part-{stamp}.parquet")
        written += len(rows)
        el = time.time() - t0
        done_n = min(start + chunk, total)
        eta = el / done_n * (total - done_n)
        progress(f"forecasts {done_n:,}/{total:,}  {done_n / el:.0f}/s  eta {eta / 60:.0f} min")
    return written
