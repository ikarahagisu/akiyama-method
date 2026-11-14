"""Where do candidate outcomes come from? Bucketed diagnostics of a backtest run.

Adds features known at the decision time (D 18:00 JST) to every candidate:
  vol60       std of daily log returns over the 60 trading days up to D
  topix_r20   TOPIX 20-day return at D
  excess20    stock 20-day return minus TOPIX 20-day return
  breadth     share of eligible stocks meeting the price condition on D
  streak      consecutive trading days the stock has met the price condition (1 = first day)
  close, mktcap_mil, turnover_med60, PBR, PER, S17Nm

Bucket edges are taken from the dev period only and reused for eval, so the
evaluation period does not shape the buckets. Besides plain means, every table
shows ``day_mean``: the average of per-day means, which removes the weight of
days with thousands of candidates (closer to what a few-slot account sees).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

FEATURES = ["vol60", "topix_r20", "excess20", "breadth", "streak", "close", "mktcap_mil", "turnover_med60", "PBR", "ret60"]

FEATURE_SQL = """
WITH lr AS (
  SELECT Code, Date, LN(AdjC / LAG(AdjC) OVER (PARTITION BY Code ORDER BY Date)) AS r FROM prices_adj
),
vol AS (
  SELECT Code, Date, STDDEV_SAMP(r) OVER (PARTITION BY Code ORDER BY Date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS vol60
  FROM lr
),
tpx AS (SELECT Date, C / LAG(C, 20) OVER (ORDER BY Date) - 1 AS topix_r20 FROM topix),
br AS (SELECT Date, AVG(CASE WHEN cond_price THEN 1.0 ELSE 0.0 END) FILTER (WHERE eligible) AS breadth FROM signals GROUP BY Date),
st AS (
  SELECT Code, Date, cond_price,
         ROW_NUMBER() OVER (PARTITION BY Code ORDER BY Date)
         - ROW_NUMBER() OVER (PARTITION BY Code, cond_price ORDER BY Date) AS grp
  FROM signals
),
streak AS (
  SELECT Code, Date, ROW_NUMBER() OVER (PARTITION BY Code, grp ORDER BY Date) AS streak FROM st WHERE cond_price
)
SELECT c.Code, c.Date, vol.vol60, tpx.topix_r20, br.breadth, streak.streak,
       u.close, u.mktcap_mil, u.turnover_med60, u.S17Nm, v.PBR, v.PER
FROM _cands c
LEFT JOIN vol ON vol.Code = c.Code AND vol.Date = c.Date
LEFT JOIN tpx ON tpx.Date = c.Date
LEFT JOIN br ON br.Date = c.Date
LEFT JOIN streak ON streak.Code = c.Code AND streak.Date = c.Date
LEFT JOIN universe u ON u.Code = c.Code AND u.Date = c.Date
LEFT JOIN {valuation} v ON v.Code = c.Code AND v.Date = c.Date
"""


def add_features(con, outcomes: pd.DataFrame) -> pd.DataFrame:
    keys = outcomes[["Code", "Date"]].astype(str).drop_duplicates()
    con.register("_cands", keys)
    views = {r[0] for r in con.execute("SELECT view_name FROM duckdb_views()").fetchall()}
    val = "valuation" if "valuation" in views else "(SELECT NULL::VARCHAR AS Code, NULL::VARCHAR AS Date, NULL::DOUBLE AS PBR, NULL::DOUBLE AS PER)"
    feats = con.execute(FEATURE_SQL.format(valuation=val)).df()
    out = outcomes.assign(Code=outcomes["Code"].astype(str), Date=outcomes["Date"].astype(str))
    out = out.drop(columns=[c for c in feats.columns if c in out.columns and c not in ("Code", "Date")])
    out = out.merge(feats, on=["Code", "Date"], how="left")
    out["excess20"] = out["ret20"] - out["topix_r20"]
    for c in ("PBR", "PER"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def _edges(dev_values: pd.Series, q: int = 5) -> np.ndarray:
    v = dev_values.dropna()
    if v.nunique() < q:
        return np.unique(v)
    return np.unique(np.quantile(v, np.linspace(0, 1, q + 1)))


def _stats(g: pd.DataFrame) -> pd.Series:
    daily = g.groupby("Date")["net"].mean()
    return pd.Series({
        "n": len(g), "days": g["Date"].nunique(),
        "mean": g["net"].mean(), "median": g["net"].median(), "day_mean": daily.mean(),
        "win": (g["net"] > 0).mean(), "mae10": (g["mae_low"] <= -0.10).mean(),
        "net_std": g["net"].std(),
    })


def bucket_tables(df: pd.DataFrame, q: int = 5) -> pd.DataFrame:
    d = df[df["exit_reason"].isin(["scheduled", "no_price"])].copy()
    d["c0_group"] = d["c0_status"].where(d["c0_status"].isin(["pass", "fail"]), "no_c0")
    d = pd.concat([d, d.assign(c0_group="all")], ignore_index=True)
    dev = d[(d["period"] == "dev") & (d["c0_group"] == "all")]
    if dev.empty:  # e.g. a short test run: fall back to all rows
        dev = d[d["c0_group"] == "all"]
    rows = []
    for f in FEATURES:
        # streak uses fixed edges (1 = first day of the episode)
        edges = np.array([0, 1, 2, 5, 10, 20, np.inf]) if f == "streak" else _edges(dev[f], q)
        if len(edges) < 2:
            continue
        b = pd.cut(d[f], np.r_[-np.inf, edges[1:-1], np.inf], duplicates="drop")
        t = d.assign(bucket=b.astype(str)).groupby(["period", "c0_group", "bucket"], observed=True).apply(_stats, include_groups=False)
        rows.append(t.reset_index().assign(feature=f))
    sec = d.groupby(["period", "c0_group", "S17Nm"], observed=True).apply(_stats, include_groups=False).reset_index()
    rows.append(sec.rename(columns={"S17Nm": "bucket"}).assign(feature="sector"))
    yr = d.assign(bucket=d["Date"].str[:4]).groupby(["period", "c0_group", "bucket"]).apply(_stats, include_groups=False)
    rows.append(yr.reset_index().assign(feature="year"))
    out = pd.concat(rows, ignore_index=True)
    return out[["feature", "period", "c0_group", "bucket", "n", "days", "mean", "median", "day_mean", "win", "mae10", "net_std"]]


def latest_run(backtest_dir: Path) -> Path:
    runs = sorted(p for p in Path(backtest_dir).iterdir() if (p / "candidate_outcomes.parquet").exists())
    if not runs:
        raise FileNotFoundError(f"no backtest run with candidate_outcomes.parquet in {backtest_dir}")
    return runs[-1]
