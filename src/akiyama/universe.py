"""Point-in-time eligible universe and fixed-seed stratified rosters.

Eligibility on trading day D uses only rows with available_at <= D + decision_time
(default 18:00 JST, i.e. after the close, deciding for the next open):

  listed      in the master snapshot for D, domestic common stock (ProdCat 011),
              not TOKYO PRO MARKET / other (Mkt 0105, 0109)
  mktcap      >= 10,000 million yen (J-Quants MktCap; if missing, shares from the
              latest disclosed fins summary x close; basis column says which)
  turnover    median of daily traded value over the last 60 trading days >= 100M yen,
              requiring 60 observations
  lot cost    close x 100 <= 1,000,000 yen. Before 2018-10-01 some stocks traded in
              1,000-share units and J-Quants does not give the unit, so the cost is
              computed for 100 shares and ``lot_assumed`` is true.

Thresholds come from the 120-company screening protocol in the handoff. They are
data/tradeability filters, not investment judgements.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# Development set used in the earlier research; kept out of evaluation rosters.
LEGACY_DEV_CODES = ("33880", "39230", "61610", "69410", "74530", "75450", "75640", "81740")


@dataclass(frozen=True)
class Criteria:
    min_mktcap_mil: float = 10_000.0  # 100億円
    min_turnover_med60: float = 100_000_000.0  # 1億円
    max_lot_cost: float = 1_000_000.0  # 100株で100万円
    window: int = 60
    decision_time: str = "18:00:00"
    lot_known_from: str = "2018-10-01"


def eligibility_sql(c: Criteria, date_from: str, date_to: str) -> str:
    """SQL over the DuckDB views from ``store.connect``."""
    return f"""
    WITH m AS (
      SELECT Date, Code, CoName, S17, S17Nm, S33, S33Nm, ScaleCat, Mkt, MktNm, ProdCat
      FROM master
    ),
    p AS (
      SELECT Date, Code, C, Va, MktCap,
             (CAST(Date AS DATE) + INTERVAL '{c.decision_time}') AT TIME ZONE 'Asia/Tokyo' AS decision_ts,
             MEDIAN(Va) OVER w AS turnover_med60,
             COUNT(Va) OVER w AS n_obs
      FROM prices
      WINDOW w AS (PARTITION BY Code ORDER BY Date ROWS BETWEEN {c.window - 1} PRECEDING AND CURRENT ROW)
    ),
    sh AS (
      SELECT Code, available_at, ShOutFY, TrShFY
      FROM fins_summary
      WHERE ShOutFY IS NOT NULL
    ),
    j AS (
      SELECT p.*, sh.ShOutFY, sh.TrShFY
      FROM p ASOF LEFT JOIN sh ON p.Code = sh.Code AND p.decision_ts >= sh.available_at
      WHERE p.Date BETWEEN '{date_from}' AND '{date_to}'
    )
    SELECT
      j.Date, j.Code, m.CoName, m.S17, m.S17Nm, m.S33, m.S33Nm, m.ScaleCat, m.Mkt, m.MktNm,
      j.C AS close, j.Va AS turnover, j.turnover_med60, j.n_obs,
      COALESCE(j.MktCap, (j.ShOutFY - COALESCE(j.TrShFY, 0)) * j.C / 1e6) AS mktcap_mil,
      CASE WHEN j.MktCap IS NOT NULL THEN 'jquants'
           WHEN j.ShOutFY IS NOT NULL THEN 'fins_shares_x_close' ELSE 'missing' END AS mktcap_basis,
      j.C * 100 AS lot_cost,
      j.Date < '{c.lot_known_from}' AS lot_assumed,
      (m.Code IS NOT NULL AND m.ProdCat = '011' AND m.Mkt NOT IN ('0105', '0109')) AS ok_listed,
      COALESCE(mktcap_mil >= {c.min_mktcap_mil}, FALSE) AS ok_mktcap,
      COALESCE(j.n_obs >= {c.window} AND j.turnover_med60 >= {c.min_turnover_med60}, FALSE) AS ok_turnover,
      COALESCE(j.C * 100 <= {c.max_lot_cost}, FALSE) AS ok_lot,
      (ok_listed AND ok_mktcap AND ok_turnover AND ok_lot) AS eligible
    FROM j LEFT JOIN m ON j.Date = m.Date AND j.Code = m.Code
    WHERE m.Code IS NULL OR m.ProdCat = '011'
    """


def compute_universe(con, date_from: str, date_to: str, criteria: Criteria = Criteria()) -> pd.DataFrame:
    return con.execute(eligibility_sql(criteria, date_from, date_to)).df()


def write_universe(df: pd.DataFrame, out_dir: Path) -> list[Path]:
    paths = []
    df = df.copy()
    df["_y"] = df["Date"].astype(str).str[:4]
    for y, part in df.groupby("_y"):
        d = Path(out_dir) / f"year={y}"
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "data.parquet.part"
        part.drop(columns="_y").to_parquet(tmp, index=False)
        tmp.replace(d / "data.parquet")
        paths.append(d / "data.parquet")
    return paths


# ------------------------------------------------------------------ rosters
def size_bucket(scale_cat: pd.Series) -> pd.Series:
    s = scale_cat.fillna("").astype(str)
    return np.where(
        s.str.contains("Core30|Large70"), "large", np.where(s.str.contains("Mid400"), "mid", "small")
    )


def allocate(counts: pd.Series, n: int) -> pd.Series:
    """Proportional allocation with largest remainders; never more than available."""
    counts = counts[counts > 0]
    n = min(n, int(counts.sum()))
    raw = counts / counts.sum() * n
    alloc = np.floor(raw).astype(int).clip(upper=counts)
    rem = (raw - np.floor(raw)).sort_values(ascending=False)
    for k in rem.index:
        if alloc.sum() >= n:
            break
        if alloc[k] < counts[k]:
            alloc[k] += 1
    # if caps blocked some, fill from strata with spare capacity
    for k in (counts - alloc).sort_values(ascending=False).index:
        while alloc.sum() < n and alloc[k] < counts[k]:
            alloc[k] += 1
    return alloc


@dataclass
class RosterMeta:
    name: str
    asof: str
    n: int
    seed: int
    criteria: dict
    excluded_codes: list
    eligible_count: int
    strata: dict
    created_at: str
    note: str = ""


def stratified_roster(
    universe_day: pd.DataFrame,
    n: int,
    seed: int,
    exclude: tuple[str, ...] = LEGACY_DEV_CODES,
) -> tuple[pd.DataFrame, dict]:
    """Sample ``n`` eligible companies stratified by S17 sector x size bucket.

    Selection depends only on the eligible set on ``asof`` and the seed, never on
    later prices or past performance.
    """
    df = universe_day[universe_day["eligible"] & ~universe_day["Code"].isin(exclude)].copy()
    df["size_bucket"] = size_bucket(df["ScaleCat"])
    df["stratum"] = df["S17"].astype(str) + "|" + df["size_bucket"]
    df = df.sort_values("Code")  # deterministic order before random draw
    alloc = allocate(df.groupby("stratum").size(), n)
    rng = np.random.default_rng(seed)
    picks = []
    for stratum in sorted(alloc.index):
        g = df[df["stratum"] == stratum]
        k = int(alloc[stratum])
        if k:
            idx = rng.choice(len(g), size=k, replace=False)
            picks.append(g.iloc[np.sort(idx)])
    out = pd.concat(picks).sort_values(["S17", "Code"]) if picks else df.head(0)
    return out, {s: int(v) for s, v in alloc.items()}


def save_roster(roster: pd.DataFrame, meta: RosterMeta, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv = out_dir / f"{meta.name}.csv"
    if csv.exists():
        raise FileExistsError(f"roster {csv} exists; rosters are frozen, choose a new name")
    cols = ["Code", "CoName", "S17", "S17Nm", "S33Nm", "ScaleCat", "size_bucket", "MktNm",
            "mktcap_mil", "turnover_med60", "close", "lot_assumed"]
    roster[cols].to_csv(csv, index=False, encoding="utf-8-sig")
    (out_dir / f"{meta.name}.meta.json").write_text(
        json.dumps(asdict(meta), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return csv


def make_meta(name, asof, n, seed, criteria: Criteria, exclude, eligible_count, strata, note="") -> RosterMeta:
    return RosterMeta(
        name=name, asof=asof, n=n, seed=seed, criteria=asdict(criteria), excluded_codes=list(exclude),
        eligible_count=eligible_count, strata=strata,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), note=note,
    )
