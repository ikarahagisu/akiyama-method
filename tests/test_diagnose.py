import numpy as np
import pandas as pd

from akiyama.diagnose import bucket_tables


def test_bucket_edges_from_dev_and_day_mean():
    rng = np.random.default_rng(0)
    n = 400
    df = pd.DataFrame({
        "Date": np.r_[np.repeat(["2020-01-06", "2020-01-07"], [300, 100]),],
        "Code": [str(i) for i in range(n)],
        "net": np.r_[np.full(300, 0.05), np.full(100, -0.01)],
        "mae_low": -0.02, "exit_reason": "scheduled", "c0_status": "pass", "period": "dev",
        **{f: rng.random(n) for f in ["vol60", "topix_r20", "excess20", "breadth", "close", "mktcap_mil", "turnover_med60", "PBR", "ret60"]},
        "streak": 1, "S17Nm": "x",
    })
    t = bucket_tables(df)
    yr = t[(t["feature"] == "year") & (t["c0_group"] == "all")].iloc[0]
    assert yr["mean"] == np.mean(df["net"])
    assert np.isclose(yr["day_mean"], (0.05 - 0.01) / 2)  # crowded day does not dominate
    assert set(t["feature"]) >= {"vol60", "breadth", "streak", "sector", "year"}
