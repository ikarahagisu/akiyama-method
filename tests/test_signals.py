import numpy as np
import pandas as pd

from akiyama.candidates import annotate, c0_flags
from akiyama.derived import load_strategy, upsert_partitioned
from akiyama.forecast import run_forecasts
from akiyama.signals import forecast_tasks, week_end_days

STRAT = load_strategy()


def test_week_end_uses_calendar_not_range_end():
    days = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16"]
    assert week_end_days(days) == {"2026-11-13", "2026-11-16"}


def test_forecast_tasks_kinds():
    sig = pd.DataFrame({
        "Date": ["2026-11-13", "2026-11-13", "2026-11-12", "2026-11-12"],
        "Code": ["A", "B", "A", "C"],
        "eligible": [True, True, True, False],
        "cond_price": [True, False, True, False],
    })
    t = forecast_tasks(sig, {"2026-11-13"}).set_index(["Date", "Code"])["kind"].to_dict()
    assert t == {("2026-11-13", "A"): "ref+cand", ("2026-11-13", "B"): "ref", ("2026-11-12", "A"): "cand"}


def _refs(code, dates, r):
    return pd.DataFrame({"Code": code, "Date": dates, "r_hat": r, "q10": 0.0, "q50": 0.0, "q90": 0.0,
                         "last_close": 1.0, "n_ctx": 512})


def test_c0_uses_only_past_refs_and_needs_minimum():
    ref_dates = pd.bdate_range("2025-01-03", periods=30, freq="W-FRI").strftime("%Y-%m-%d").tolist()
    refs = _refs("A", ref_dates, np.linspace(0.0, 0.029, 30))  # increasing
    d = ref_dates[25]  # candidate on a ref day: its own forecast must not be in the window
    cand_fc = refs[refs["Date"] == d].assign(r_hat=0.026)
    fc = pd.concat([refs[refs["Date"] != d], cand_fc])
    out = c0_flags(pd.DataFrame({"Code": ["A"], "Date": [d]}), fc, set(ref_dates), STRAT)
    window = np.linspace(0.0, 0.029, 30)[:25]
    assert out["ref_n"].item() == 25
    assert np.isclose(out["ref_p"].item(), np.quantile(window, 0.75))
    assert bool(out["c0"].item())  # 0.026 > p75 of past (≈0.018) and > cost

    early = ref_dates[10]
    out2 = c0_flags(pd.DataFrame({"Code": ["A"], "Date": [early]}), fc, set(ref_dates), STRAT)
    assert out2["c0_status"].item() == "insufficient_ref" and not bool(out2["c0"].item())


def test_c0_requires_beating_cost():
    ref_dates = pd.bdate_range("2025-01-03", periods=25, freq="W-FRI").strftime("%Y-%m-%d").tolist()
    fc = pd.concat([_refs("A", ref_dates[:-1], -0.05), _refs("A", [ref_dates[-1]], 0.001)])
    out = c0_flags(pd.DataFrame({"Code": ["A"], "Date": [ref_dates[-1]]}), fc, set(ref_dates), STRAT)
    assert out["c0_status"].item() == "fail"  # beats p75 (-0.05) but not the 0.2% cost


def test_annotate_periods():
    df = annotate(pd.DataFrame({"Code": ["81740", "10000", "10000"],
                                "Date": ["2021-10-29", "2025-12-01", "2026-07-01"]}), STRAT)
    assert df["period"].tolist() == ["dev", "eval", "eval"]
    assert df["is_dev_code"].tolist() == [True, False, False]
    assert df["chronos_period"].tolist() == ["pre_release", "post_release_pre_revision", "post_revision"]


class FakeCon:
    """Minimal stand-in exposing prices_adj through duckdb."""

    def __init__(self, prices):
        import duckdb

        self.con = duckdb.connect()
        self.con.register("prices_adj", prices)

    def register(self, *a):
        return self.con.register(*a)

    def execute(self, *a):
        return self.con.execute(*a)


def test_run_forecasts_uses_context_up_to_date_and_resumes(tmp_path):
    days = pd.bdate_range("2025-01-01", periods=200).strftime("%Y-%m-%d").tolist()
    prices = pd.DataFrame({"Code": "A", "Date": days, "AdjC": np.arange(1, 201, dtype=float)})
    seen = []

    def fake(series):
        seen.extend(s[-1] for s in series)
        return np.stack([[s[-1], s[-1] * 1.1, s[-1] * 1.2] for s in series])

    tasks = pd.DataFrame({"Code": ["A", "A", "A"], "Date": [days[150], days[199], days[50]]})
    con = FakeCon(prices)
    n = run_forecasts(con, tasks, STRAT, tmp_path, fake, progress=lambda m: None)
    assert n == 2  # days[50] has < min_history (120) closes -> skipped
    assert sorted(seen) == [151.0, 200.0]  # last value is the close on Date, nothing later
    out = pd.read_parquet(next(tmp_path.glob("part-*.parquet")))
    assert np.allclose(out["r_hat"], 0.1)
    assert run_forecasts(con, tasks.head(2), STRAT, tmp_path, fake, progress=lambda m: None) == 0  # cached


def test_upsert_keeps_other_dates(tmp_path):
    a = pd.DataFrame({"Date": ["2026-01-05", "2026-02-02"], "Code": ["A", "A"], "v": [1, 2]})
    upsert_partitioned(a, tmp_path, "2026-01-01", "2026-12-31")
    b = pd.DataFrame({"Date": ["2026-02-02"], "Code": ["A"], "v": [9]})
    upsert_partitioned(b, tmp_path, "2026-02-01", "2026-02-28")
    df = pd.read_parquet(tmp_path / "year=2026" / "data.parquet")
    assert df["v"].tolist() == [1, 9]
