"""How accurate would an AI risk review have to be? (no API calls)

A hypothetical reviewer removes candidates before they enter the account:
  - a candidate that later fell 10% or more intraday (``mae_low <= crash``) is
    removed with probability ``detect``
  - any other candidate is removed with probability ``false_remove``
Each candidate-day gets an independent draw from a fixed seed (a real reviewer
would be more consistent across consecutive days of the same stock; this is a
simplification). The perfect reviewer (detect=1, false_remove=0) is an upper bound
that uses future information.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DETECT = (0.0, 0.3, 0.5, 0.7, 0.9, 1.0)
FALSE_REMOVE = (0.0, 0.1, 0.3)


def apply_reviewer(c: pd.DataFrame, detect: float, false_remove: float, crash: float = -0.10, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    u = rng.random(len(c))
    is_crash = (c["mae_low"] <= crash).fillna(False).to_numpy()
    remove = np.where(is_crash, u < detect, u < false_remove)
    return c[~remove]


def grid_sets(base_sets: dict[str, pd.DataFrame]) -> dict[str, tuple[pd.DataFrame, tuple[str, ...]]]:
    out = {}
    for name, df in base_sets.items():
        for d in DETECT:
            for f in FALSE_REMOVE:
                out[f"{name}~d{d:.1f}~f{f:.1f}"] = (apply_reviewer(df, d, f), ("R",))
    return out
