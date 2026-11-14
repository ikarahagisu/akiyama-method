"""Company-quality features, point in time, and a profile of the legacy 8 stocks.

For each (Code, Date) the features use only full-year results whose disclosure
(available_at) is before Date 18:00 JST. If a company disclosed the same fiscal
year more than once (corrections), the latest version known at that time is used.

Features
  fy_n            full years available (max 5 used)
  op_pos_streak   consecutive latest years with operating profit > 0
  np_pos_5        years with net profit > 0 among the last 5
  op_cv           std / |mean| of operating profit over the last 3-5 years (lower = stabler)
  roe             latest net profit / equity
  roe_avg3        mean ROE over the last 3 years
  eq_ratio        latest equity-to-asset ratio
  sales_cagr3     3-year sales growth per year
  div_payer       latest annual dividend > 0
  beta250, vol250 from daily returns vs TOPIX over 250 trading days up to Date
"""

from __future__ import annotations

import numpy as np
import pandas as pd

QUALITY_FEATURES = ["fy_n", "op_pos_streak", "np_pos_5", "op_cv", "roe", "roe_avg3", "eq_ratio",
                    "sales_cagr3", "div_payer", "beta250", "vol250"]

FY_SQL = """
SELECT Code, CurFYEn, available_at, DocType,
       TRY_CAST(Sales AS DOUBLE) AS Sales, TRY_CAST(OP AS DOUBLE) AS OP, TRY_CAST(NP AS DOUBLE) AS NP,
       TRY_CAST(Eq AS DOUBLE) AS Eq, TRY_CAST(EqAR AS DOUBLE) AS EqAR, TRY_CAST(DivAnn AS DOUBLE) AS DivAnn
FROM fins_summary
WHERE CurPerType = 'FY' AND DocType LIKE 'FYFinancialStatements%' AND Code IN (SELECT Code FROM _q_codes)
"""

RISK_SQL = """
WITH r AS (
  SELECT p.Code, p.Date, LN(p.AdjC / LAG(p.AdjC) OVER (PARTITION BY p.Code ORDER BY p.Date)) AS rs,
         LN(t.C / LAG(t.C) OVER (PARTITION BY p.Code ORDER BY p.Date)) AS rm
  FROM prices_adj p JOIN topix t ON t.Date = p.Date
  WHERE p.Code IN (SELECT Code FROM _q_codes)
),
w AS (
  SELECT Code, Date,
         REGR_SLOPE(rs, rm) OVER win AS beta250,
         STDDEV_SAMP(rs) OVER win AS vol250,
         COUNT(rs) OVER win AS n250
  FROM r WINDOW win AS (PARTITION BY Code ORDER BY Date ROWS BETWEEN 249 PRECEDING AND CURRENT ROW)
)
SELECT Code, Date, CASE WHEN n250 >= 200 THEN beta250 END AS beta250, CASE WHEN n250 >= 200 THEN vol250 END AS vol250
FROM w WHERE (Code, Date) IN (SELECT Code, Date FROM _q_keys)
"""


def _fy_features(fy: pd.DataFrame) -> dict:
    """Features from up to 5 full years, oldest first."""
    fy = fy.tail(5)
    op, np_, sales, eq = fy["OP"].to_numpy(), fy["NP"].to_numpy(), fy["Sales"].to_numpy(), fy["Eq"].to_numpy()
    out = {"fy_n": len(fy)}
    streak = 0
    for v in op[::-1]:
        if np.isfinite(v) and v > 0:
            streak += 1
        else:
            break
    out["op_pos_streak"] = streak
    out["np_pos_5"] = int(np.sum(np.isfinite(np_) & (np_ > 0)))
    o = op[np.isfinite(op)]
    out["op_cv"] = float(np.std(o) / abs(np.mean(o))) if len(o) >= 3 and np.mean(o) != 0 else np.nan
    roe = np.where((eq > 0) & np.isfinite(np_), np_ / np.where(eq > 0, eq, np.nan), np.nan)
    out["roe"] = float(roe[-1]) if len(roe) else np.nan
    r3 = roe[-3:]
    out["roe_avg3"] = float(np.nanmean(r3)) if np.isfinite(r3).sum() >= 2 else np.nan
    out["eq_ratio"] = float(fy["EqAR"].iloc[-1]) if len(fy) else np.nan
    if len(sales) >= 4 and sales[-4] > 0 and np.isfinite(sales[-1]):
        out["sales_cagr3"] = float((sales[-1] / sales[-4]) ** (1 / 3) - 1)
    else:
        out["sales_cagr3"] = np.nan
    d = fy["DivAnn"].iloc[-1] if len(fy) else np.nan
    out["div_payer"] = float(d > 0) if np.isfinite(d) else np.nan
    return out


def quality_features(con, keys: pd.DataFrame, decision_time: str = "18:00:00") -> pd.DataFrame:
    """keys: DataFrame with Code, Date. Returns keys + QUALITY_FEATURES."""
    keys = keys[["Code", "Date"]].astype(str).drop_duplicates()
    con.register("_q_codes", pd.DataFrame({"Code": keys["Code"].unique()}))
    con.register("_q_keys", keys)
    fy = con.execute(FY_SQL).df()
    fy["available_at"] = pd.to_datetime(fy["available_at"], utc=True).astype("datetime64[us, UTC]")
    # prefer consolidated statements when both exist for a period
    fy["consol"] = fy["DocType"].str.contains("_Consolidated").astype(int)
    # features change only when a new full-year disclosure arrives: compute one
    # snapshot per disclosure, then attach the latest snapshot known at each Date.
    snaps = []
    for code, f in fy.sort_values("available_at").groupby("Code"):
        for i in range(len(f)):
            known = f.iloc[: i + 1]
            known = known.sort_values(["CurFYEn", "consol", "available_at"]).groupby("CurFYEn").tail(1)
            snaps.append({"Code": code, "snap_at": f["available_at"].iloc[i],
                          **_fy_features(known.sort_values("CurFYEn"))})
    snaps = pd.DataFrame(snaps)
    cut = pd.to_datetime(keys["Date"] + " " + decision_time).dt.tz_localize("Asia/Tokyo").dt.tz_convert("UTC").astype("datetime64[us, UTC]")
    k = keys.assign(_cut=cut).sort_values("_cut")
    if snaps.empty:
        out = k.drop(columns="_cut").assign(fy_n=0)
    else:
        snaps = snaps.sort_values("snap_at")
        out = pd.merge_asof(k, snaps, left_on="_cut", right_on="snap_at", by="Code", direction="backward")
        out = out.drop(columns=["_cut", "snap_at"])
        out["fy_n"] = out["fy_n"].fillna(0)
    risk = con.execute(RISK_SQL).df().assign(Date=lambda x: x["Date"].astype(str))
    return out.merge(risk, on=["Code", "Date"], how="left")


def profile_table(feats: pd.DataFrame, legacy_codes: list[str]) -> pd.DataFrame:
    """Per feature: universe percentiles vs the legacy stocks (pooled and each)."""
    rows = []
    uni = feats[~feats["Code"].isin(legacy_codes)]
    for f in QUALITY_FEATURES:
        u = uni[f].dropna()
        row = {"feature": f, "universe_n": len(u)}
        for q in (0.1, 0.25, 0.5, 0.75, 0.9):
            row[f"uni_p{int(q * 100)}"] = u.quantile(q) if len(u) else np.nan
        leg = feats[feats["Code"].isin(legacy_codes)]
        row["legacy8_median"] = leg[f].median()
        row["legacy8_pct_rank"] = (u < row["legacy8_median"]).mean() if len(u) else np.nan
        for c in legacy_codes:
            row[c] = feats.loc[feats["Code"] == c, f].median()
        rows.append(row)
    return pd.DataFrame(rows)
