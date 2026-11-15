import numpy as np
import pandas as pd
import pytest

from akiyama.backtest import AccountRules, PriceBook, candidate_outcomes, rank_candidates, simulate

DAYS = pd.bdate_range("2024-01-01", periods=40).strftime("%Y-%m-%d").tolist()


def _px(code, opens, lows=None, closes=None, days=DAYS, adj=1.0):
    n = len(opens)
    o = np.asarray(opens, dtype=float)
    return pd.DataFrame({
        "Code": code, "Date": days[:n], "O": o / adj, "AdjO": o, "AdjH": o * 1.01,
        "AdjL": o * 0.99 if lows is None else np.asarray(lows, float), "AdjC": o if closes is None else closes,
    })


RULES = AccountRules(hold_days=5, fee=0.001)


def test_candidate_outcome_entry_next_open_exit_after_hold():
    opens = [100, 100, 110, 100, 90, 95, 100, 120, 120, 120]
    lows = [99, 99, 108, 99, 80, 94, 99, 119, 119, 119]
    pb = PriceBook(_px("A", opens, lows))
    out = candidate_outcomes(pd.DataFrame({"Code": ["A"], "Date": [DAYS[1]]}), pb, RULES).iloc[0]
    assert out["entry_date"] == DAYS[2] and out["entry_open"] == 110
    assert out["exit_open"] == 120  # DAYS[7] = entry + 5
    assert out["net"] == pytest.approx(120 / 110 * 0.999 / 1.001 - 1)
    assert out["mae_low"] == pytest.approx(80 / 110 - 1)  # lows of DAYS[2..6], exit day excluded


def test_candidate_outcome_open_at_end_of_data():
    pb = PriceBook(_px("A", [100] * 4))
    out = candidate_outcomes(pd.DataFrame({"Code": ["A"], "Date": [DAYS[0]]}), pb, RULES).iloc[0]
    assert out["exit_reason"] == "open"


def test_simulation_slots_ranking_and_cash():
    px = pd.concat([_px(c, [1000] * 20) for c in ("A", "B", "C", "D")]
                   + [_px("E", [1000] * 6 + [2000] * 14)])
    pb = PriceBook(px)
    cands = pd.DataFrame({
        "Code": ["A", "B", "C", "D", "E"], "Date": DAYS[0],
        "r_hat": [0.01, 0.05, 0.02, 0.03, 0.04], "ref_p": 0.0, "c0_cost": 0.002, "ret60": -0.2,
    })
    res = simulate(cands, pb, DAYS[:20], RULES, "A")
    tr = res.trades.sort_values("Code")
    assert set(tr["Code"]) == {"B", "D", "E"}  # top-3 by C0 strength, 3 slots
    e = tr[tr["Code"] == "E"].iloc[0]
    assert e["shares"] == 1000  # share value <= 1,000,000 (fee on top)
    d = tr[tr["Code"] == "D"].iloc[0]
    assert d["shares"] == 900  # third slot: remaining cash / (1+fee) = 997,003 -> 9 lots
    assert e["entry_date"] == DAYS[1] and e["exit_date"] == DAYS[6]
    assert e["net"] == pytest.approx(2.0 * 0.999 / 1.001 - 1)
    final = res.equity["equity"].iloc[-1]
    expected = 3_000_000 + tr["profit"].sum()
    assert final == pytest.approx(expected)


def test_no_duplicate_holding_and_random_is_seeded():
    pb = PriceBook(pd.concat([_px(c, [1000] * 20) for c in ("A", "B", "C", "D")]))
    cands = pd.DataFrame({"Code": ["A", "B", "C", "D", "A"], "Date": [DAYS[0]] * 4 + [DAYS[2]],
                          "r_hat": 0.05, "ref_p": 0.0, "c0_cost": 0.002, "ret60": [-0.1, -0.3, -0.2, -0.4, -0.5]})
    r1 = simulate(cands, pb, DAYS[:20], RULES, "R", seed=3).trades["Code"].tolist()
    r2 = simulate(cands, pb, DAYS[:20], RULES, "R", seed=3).trades["Code"].tolist()
    assert r1 == r2
    b = simulate(cands, pb, DAYS[:20], RULES, "B").trades
    assert sorted(b.loc[b["decision_date"] == DAYS[0], "Code"]) == ["B", "C", "D"]  # deepest falls
    assert b["Code"].value_counts().max() == 1 or (b.groupby("Code")["entry_date"].nunique() == b["Code"].value_counts()).all()


def test_delisted_position_closed_at_last_close():
    px = _px("A", [1000] * 3, closes=[1000, 1000, 500])
    other = _px("Z", [1] * 20)
    pb = PriceBook(pd.concat([px, other]))
    cands = pd.DataFrame({"Code": ["A"], "Date": [DAYS[0]], "r_hat": 0.05, "ref_p": 0.0, "c0_cost": 0.002, "ret60": -0.2})
    tr = simulate(cands, pb, DAYS[:20], RULES, "A").trades.iloc[0]
    assert tr["exit_reason"] == "no_price" and tr["exit_date"] == DAYS[2]
    assert tr["net"] == pytest.approx(0.5 * 0.999 / 1.001 - 1)


def test_rank_a_uses_margin_over_threshold():
    day = pd.DataFrame({"Code": ["X", "Y"], "r_hat": [0.05, 0.04], "ref_p": [0.045, 0.0], "c0_cost": 0.002, "ret60": -0.1})
    assert rank_candidates(day, "A", np.random.default_rng(0))["Code"].tolist() == ["Y", "X"]


def test_ai_reviewer_removal_rates():
    from akiyama.ai_sensitivity import apply_reviewer

    c = pd.DataFrame({"mae_low": [-0.2] * 1000 + [0.0] * 1000})
    assert len(apply_reviewer(c, 1.0, 0.0)) == 1000
    assert len(apply_reviewer(c, 0.0, 0.0)) == 2000
    kept = apply_reviewer(c, 0.5, 0.1)
    assert 400 < (kept["mae_low"] < -0.1).sum() < 600 and 850 < (kept["mae_low"] == 0).sum() < 950
