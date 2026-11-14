import numpy as np
import pandas as pd
import pytest

from akiyama.backtest import AccountRules, PriceBook
from akiyama.gc_rsi import GCParams, rsi_wilder, signals, simulate_trades, trade_path, trades

DAYS = pd.bdate_range("2024-01-01", periods=60).strftime("%Y-%m-%d").tolist()
P = GCParams(max_days=5)


def _pb(o, h, l, c=None, code="A"):
    n = len(o)
    return PriceBook(pd.DataFrame({"Code": code, "Date": DAYS[:n], "O": o, "AdjO": o, "AdjH": h, "AdjL": l,
                                   "AdjC": c if c is not None else o}))


def test_rsi_bounds_and_flat():
    r = rsi_wilder(np.r_[np.linspace(100, 120, 20)], 14)
    assert np.isnan(r[:14]).all() and r[14] == 100.0


def test_tp_sl_same_day_modes_and_gap():
    o = [100, 100, 100, 100, 100, 100, 100, 100]
    h = [100, 101, 106, 100, 100, 100, 100, 100]
    l = [100, 99, 96, 100, 100, 100, 100, 100]
    pb = _pb(o, h, l)
    c = trade_path(pb, "A", DAYS[0], P, "conservative")
    o_ = trade_path(pb, "A", DAYS[0], P, "optimistic")
    assert c["exit_reason"] == "sl" and c["exit_price"] == pytest.approx(97) and c["both_hit"]
    assert o_["exit_reason"] == "tp" and o_["exit_price"] == pytest.approx(105)
    pb2 = _pb([100, 100, 90, 90, 90, 90, 90], [100] * 7, [99, 99, 89, 89, 89, 89, 89])
    g = trade_path(pb2, "A", DAYS[0], P, "conservative")
    assert g["exit_reason"] == "gap_sl" and g["exit_price"] == 90 and g["exit_at"] == "open"


def test_time_exit_after_max_days():
    pb = _pb([100] * 10, [101] * 10, [99] * 10)
    r = trade_path(pb, "A", DAYS[0], P, "conservative")
    assert r["exit_reason"] == "time" and r["exit_date"] == DAYS[6]


def test_signal_requires_cross_and_rsi_and_eligibility():
    c = np.r_[np.linspace(110, 100, 40), [100] * 10, [106]]  # slow fall, flat, jump
    px = pd.DataFrame({"Code": "A", "Date": DAYS[:len(c)], "AdjC": c})
    elig = pd.DataFrame({"Code": "A", "Date": DAYS[:len(c)]})
    s = signals(px, elig, GCParams())
    assert s["Date"].tolist() == [DAYS[50]]
    assert signals(px, elig.iloc[:0], GCParams()).empty


def test_account_cash_and_intraday_exit():
    pb = _pb([100, 100, 100, 100, 100, 100, 100, 100], [100, 106, 100, 100, 100, 100, 100, 100], [100] * 8)
    sig = pd.DataFrame({"Code": ["A"], "Date": [DAYS[0]]})
    t = trades(sig, pb, P, "conservative")
    res = simulate_trades(t, pb, DAYS[:8], AccountRules(slots=3, max_per_position=1_000_000))
    assert res["trades"] == 1
    assert res["cumulative"] == pytest.approx(1_000_000 * (1.05 * 0.999 - 1.001) / 3_000_000)
