import duckdb
import numpy as np
import pandas as pd
import pytest

from akiyama.universe import Criteria, allocate, compute_universe, save_roster, make_meta, stratified_roster


def _con(prices, master, fins):
    con = duckdb.connect()
    con.register("prices", prices)
    con.register("master", master)
    con.register("fins_summary", fins)
    return con


def _days(n, start="2024-01-01"):
    return pd.bdate_range(start, periods=n).strftime("%Y-%m-%d").tolist()


def test_eligibility_rules_and_point_in_time_shares():
    days = _days(61)
    rows = []
    for d in days:
        rows += [
            {"Date": d, "Code": "10000", "C": 1000.0, "Va": 2e8, "MktCap": 50_000.0},  # passes
            {"Date": d, "Code": "20000", "C": 1000.0, "Va": 5e7, "MktCap": 50_000.0},  # low turnover
            {"Date": d, "Code": "30000", "C": 20000.0, "Va": 2e8, "MktCap": 50_000.0},  # lot too expensive
            {"Date": d, "Code": "40000", "C": 1000.0, "Va": 2e8, "MktCap": None},  # cap from fins
        ]
    prices = pd.DataFrame(rows)
    master = pd.DataFrame(
        [{"Date": d, "Code": c, "CoName": c, "S17": "1", "S17Nm": "x", "S33": "0050", "S33Nm": "x",
          "ScaleCat": "TOPIX Small 1", "Mkt": "0111", "MktNm": "プライム", "ProdCat": "011"}
         for d in days for c in ("10000", "20000", "30000", "40000")]
    )
    last = days[-1]
    # 40000: shares disclosed at 15:00 on the last day -> usable at 18:00 decision;
    # a later disclosure (next day) must not be used.
    fins = pd.DataFrame([
        {"Code": "40000", "available_at": pd.Timestamp(f"{last} 15:00", tz="Asia/Tokyo"), "ShOutFY": 20_000_000.0, "TrShFY": 0.0},
        {"Code": "40000", "available_at": pd.Timestamp(f"{last} 15:00", tz="Asia/Tokyo") + pd.Timedelta(days=1), "ShOutFY": 1.0, "TrShFY": 0.0},
    ])
    u = compute_universe(_con(prices, master, fins), last, last).set_index("Code")
    assert u.loc["10000", "eligible"]
    assert not u.loc["20000", "ok_turnover"]
    assert not u.loc["30000", "ok_lot"]
    assert u.loc["40000", "mktcap_basis"] == "fins_shares_x_close"
    assert u.loc["40000", "mktcap_mil"] == pytest.approx(20_000.0)
    assert u.loc["40000", "eligible"]

    # with only 59 days of history the turnover rule cannot pass
    first59 = days[58]
    u59 = compute_universe(_con(prices, master, fins), first59, first59).set_index("Code")
    assert not u59.loc["10000", "ok_turnover"]


def test_allocate_sums_and_caps():
    counts = pd.Series({"a": 50, "b": 30, "c": 2})
    a = allocate(counts, 20)
    assert a.sum() == 20 and (a <= counts).all()
    assert allocate(pd.Series({"a": 3}), 10).sum() == 3


def test_roster_deterministic_and_excludes_dev(tmp_path):
    n = 300
    day = pd.DataFrame({
        "Code": [f"{10000 + i}" for i in range(n)] + ["81740"],
        "CoName": "x", "S17": [str(i % 5) for i in range(n)] + ["1"], "S17Nm": "x", "S33Nm": "x",
        "ScaleCat": ["TOPIX Mid400" if i % 3 == 0 else "TOPIX Small 1" for i in range(n)] + ["TOPIX Mid400"],
        "MktNm": "プライム", "mktcap_mil": 1.0, "turnover_med60": 1.0, "close": 1.0, "lot_assumed": False,
        "eligible": [i % 4 != 0 for i in range(n)] + [True],
    })
    r1, s1 = stratified_roster(day, 40, seed=7)
    r2, _ = stratified_roster(day, 40, seed=7)
    assert r1["Code"].tolist() == r2["Code"].tolist()
    assert len(r1) == 40 and "81740" not in set(r1["Code"])
    assert set(r1["Code"]) <= set(day.loc[day["eligible"], "Code"])
    meta = make_meta("t", "2024-01-01", 40, 7, Criteria(), (), 1, s1)
    save_roster(r1, meta, tmp_path)
    with pytest.raises(FileExistsError):
        save_roster(r1, meta, tmp_path)
