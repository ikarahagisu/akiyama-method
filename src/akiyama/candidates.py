"""C0 check and the candidate table for chronos_v2.

C0 on day D for stock S (all inputs knowable at D 18:00 JST):
  r_hat(S, D) > max(cost, P_pct of S's last ``ref_count`` weekly reference forecasts
                     made strictly before D), requiring at least ``ref_min`` of them.

Every price-condition day is kept with both flags so "price only" and
"price + Chronos" can be compared on the same candidate set.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .universe import LEGACY_DEV_CODES


def c0_flags(cands: pd.DataFrame, forecasts: pd.DataFrame, ref_days: set[str], strategy: dict) -> pd.DataFrame:
    c0 = strategy["c0"]
    f = forecasts.assign(Code=forecasts["Code"].astype(str), Date=forecasts["Date"].astype(str))
    refs = f[f["Date"].isin(ref_days)].sort_values(["Code", "Date"])
    ref_by_code = {k: (g["Date"].to_numpy(), g["r_hat"].to_numpy()) for k, g in refs.groupby("Code")}
    out = cands.assign(Code=cands["Code"].astype(str), Date=cands["Date"].astype(str)).merge(
        f[["Code", "Date", "r_hat", "q10", "q50", "q90", "last_close", "n_ctx"]], on=["Code", "Date"], how="left"
    )
    p, n = np.full(len(out), np.nan), np.zeros(len(out), dtype=int)
    for i, (code, d) in enumerate(zip(out["Code"], out["Date"])):
        dates, vals = ref_by_code.get(code, (np.array([]), np.array([])))
        j = np.searchsorted(dates, d, side="left")  # strictly before D
        window = vals[max(0, j - c0["ref_count"]) : j]
        window = window[~np.isnan(window)]
        n[i] = len(window)
        if len(window) >= c0["ref_min"]:
            p[i] = np.quantile(window, c0["ref_pct"])
    out["ref_p"] = p
    out["ref_n"] = n
    thr = np.maximum(c0["cost"], out["ref_p"])
    out["c0"] = (out["r_hat"] > thr).fillna(False) & out["ref_p"].notna()
    out["c0_status"] = np.select(
        [out["r_hat"].isna(), out["ref_p"].isna(), out["c0"]],
        ["no_forecast", "insufficient_ref", "pass"],
        default="fail",
    )
    return out


def annotate(df: pd.DataFrame, strategy: dict) -> pd.DataFrame:
    per = strategy["periods"]
    df = df.copy()
    df["strategy"] = f"{strategy['name']}@{strategy['version']}"
    df["is_dev_code"] = df["Code"].isin(LEGACY_DEV_CODES)
    df["period"] = np.where(df["Date"] <= per["dev_until"], "dev", "eval")
    df["chronos_period"] = np.select(
        [df["Date"] < per["chronos_release"], df["Date"] < per["chronos_revision_date"]],
        ["pre_release", "post_release_pre_revision"],
        default="post_revision",
    )
    return df
