"""chronos_v4 candidate sets: v2 price condition + company-quality filter (+ v3 market)."""

from __future__ import annotations

import pandas as pd

from .strategy_v3 import market_ok, risk_ok


def quality_ok(df: pd.DataFrame, cfg4: dict) -> pd.Series:
    q = cfg4["quality"]
    ok = (
        (df["op_pos_streak"] >= q["op_pos_streak_min"]).fillna(False)
        & (df["roe_avg3"] >= q["roe_avg3_min"]).fillna(False)
        & (df["eq_ratio"] >= q["eq_ratio_min"]).fillna(False)
    )
    if q.get("dividend"):
        ok &= (df["div_payer"] == 1).fillna(False)
    return ok


def candidate_sets(feats: pd.DataFrame, cfg3: dict, cfg4: dict) -> dict:
    m, r, q = market_ok(feats, cfg3), risk_ok(feats, cfg3), quality_ok(feats, cfg4)
    return {
        "v2_price": (feats, ("R",)),
        "v3a_market": (feats[m], ("R",)),
        "v3b_market_risk": (feats[m & r], ("R",)),
        "v4q_quality": (feats[q], ("R",)),
        "v4qm_quality_market": (feats[q & m], ("R",)),
    }
