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
