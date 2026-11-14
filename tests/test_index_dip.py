import numpy as np
import pandas as pd
import pytest

from akiyama.index_dip import DipParams, simulate, xirr


def _px(values, start="2024-01-01"):
    d = pd.bdate_range(start, periods=len(values)).strftime("%Y-%m-%d")
    return pd.DataFrame({"Date": d, "C": np.asarray(values, float)})


def test_flat_index_no_dividend_keeps_money():
    px = _px([100.0] * 300)
    p = DipParams(div_yield=0.0)
    for st in ("dca", "dip", "half", "dip_cap"):
        r = simulate(px, st, p)
        assert r["final_value"] == pytest.approx(r["contributed"])


def test_dip_buys_on_drop_and_cap_invests_old_cash():
    v = [100.0] * 40 + [90.0] * 5 + [100.0] * 200
    p = DipParams(div_yield=0.0, threshold=-0.05)
    dip = simulate(_px(v), "dip", p)
    assert dip["dip_buys"] == 1
    flat = [100.0] * 260
    cap = simulate(_px(flat), "dip_cap", p)
    assert cap["cash_left"] < 6 * p.monthly + 1  # nothing waits longer than the cap


def test_xirr_simple():
    assert xirr([("2020-01-01", -100.0), ("2021-01-01", 110.0)]) == pytest.approx(0.0997, abs=1e-3)
