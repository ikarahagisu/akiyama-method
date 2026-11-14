import numpy as np
import pandas as pd
import yaml

from akiyama.config import REPO_ROOT
from akiyama.strategy_v3 import candidate_sets, market_ok, risk_ok

CFG = yaml.safe_load((REPO_ROOT / "config" / "strategy_v3.yaml").read_text(encoding="utf-8"))


def test_market_is_or_and_risk_excludes_missing_pbr():
    df = pd.DataFrame({
        "breadth": [0.25, 0.05, 0.05, np.nan],
        "topix_r20": [0.0, -0.05, 0.01, -0.10],
        "vol60": [0.02, 0.02, 0.05, 0.02],
        "PBR": [1.0, np.nan, 1.0, 5.0],
        "mktcap_mil": [50000, 50000, 50000, 50000],
        "c0": [True, False, True, True],
    })
    assert market_ok(df, CFG).tolist() == [True, True, False, True]
    assert risk_ok(df, CFG).tolist() == [True, False, False, False]
    sets = candidate_sets(df, CFG)
    assert len(sets["v3b_market_risk"][0]) == 1 and len(sets["v3b_c0"][0]) == 1
    assert sets["v2_price"][1] == ("R",)


def test_quality_features_point_in_time():
    import duckdb

    from akiyama.quality import quality_features

    con = duckdb.connect()
    t = lambda s: pd.Timestamp(s, tz="Asia/Tokyo")
    fins = pd.DataFrame([
        # FY2020..2023 for A; FY2023 first disclosed with OP<0 then corrected later
        {"Code": "A", "CurFYEn": f"{y}-03-31", "available_at": t(f"{y}-05-10 15:00"), "DocType": "FYFinancialStatements_Consolidated_JP",
         "CurPerType": "FY", "Sales": 100.0 * (1.1 ** (y - 2020)), "OP": 10.0, "NP": 5.0, "Eq": 50.0, "EqAR": 0.5, "DivAnn": 10.0}
        for y in (2020, 2021, 2022, 2023)
    ] + [{"Code": "A", "CurFYEn": "2023-03-31", "available_at": t("2023-06-01 15:00"), "DocType": "FYFinancialStatements_Consolidated_JP",
          "CurPerType": "FY", "Sales": 133.1, "OP": -1.0, "NP": 5.0, "Eq": 50.0, "EqAR": 0.5, "DivAnn": 10.0}])
    con.register("fins_summary", fins)
    days = pd.bdate_range("2022-01-01", "2023-12-31")
    con.register("prices_adj", pd.DataFrame({"Code": "A", "Date": days.strftime("%Y-%m-%d"), "AdjC": np.linspace(100, 120, len(days))}))
    con.register("topix", pd.DataFrame({"Date": days.strftime("%Y-%m-%d"), "C": np.linspace(1000, 1100, len(days))}))
    keys = pd.DataFrame({"Code": ["A", "A", "A"], "Date": ["2023-05-09", "2023-05-11", "2023-06-02"]})
    q = quality_features(con, keys).set_index("Date")
    assert q.loc["2023-05-09", "fy_n"] == 3 and q.loc["2023-05-09", "op_pos_streak"] == 3
    assert q.loc["2023-05-11", "fy_n"] == 4 and q.loc["2023-05-11", "op_pos_streak"] == 4
    assert q.loc["2023-06-02", "op_pos_streak"] == 0  # correction known after 06-01
    assert np.isclose(q.loc["2023-05-11", "sales_cagr3"], 0.1)
    assert np.isclose(q.loc["2023-05-11", "roe"], 0.1)


def test_v4_quality_filter():
    from akiyama.strategy_v4 import quality_ok

    cfg4 = yaml.safe_load((REPO_ROOT / "config" / "strategy_v4.yaml").read_text(encoding="utf-8"))
    df = pd.DataFrame({"op_pos_streak": [3, 2, 5, 5], "roe_avg3": [0.08, 0.2, 0.05, 0.1],
                       "eq_ratio": [0.5, 0.6, 0.6, 0.6], "div_payer": [1.0, 1.0, 1.0, np.nan]})
    assert quality_ok(df, cfg4).tolist() == [True, False, False, False]
