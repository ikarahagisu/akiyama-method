"""chronos_v3 candidate sets: v2 price condition + market-wide filter + risk filter.

All features are knowable at the decision time (see diagnose.add_features).
"""

from __future__ import annotations

import pandas as pd


def market_ok(df: pd.DataFrame, cfg: dict) -> pd.Series:
    m = cfg["market"]
    return (df["breadth"] >= m["breadth_min"]).fillna(False) | (df["topix_r20"] <= m["topix_r20_max"]).fillna(False)


def risk_ok(df: pd.DataFrame, cfg: dict) -> pd.Series:
    r = cfg["risk"]
    return (
        (df["vol60"] <= r["vol60_max"]).fillna(False)
        & (df["PBR"] <= r["pbr_max"]).fillna(False)  # missing PBR excluded
        & (df["mktcap_mil"] >= r["mktcap_min_mil"]).fillna(False)
    )


def candidate_sets(feats: pd.DataFrame, cfg: dict) -> dict[str, tuple[pd.DataFrame, tuple[str, ...]]]:
    """Named candidate sets with the rankings to simulate for each."""
    m, r = market_ok(feats, cfg), risk_ok(feats, cfg)
    c0 = feats["c0"].fillna(False).astype(bool)
    return {
        "v2_price": (feats, ("R",)),
        "v2_c0": (feats[c0], ("A", "R")),
        "v3a_market": (feats[m], ("R",)),
        "v3r_risk": (feats[r], ("R",)),
        "v3b_market_risk": (feats[m & r], ("R",)),
        "v3b_c0": (feats[m & r & c0], ("A", "R")),
    }
