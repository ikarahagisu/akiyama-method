"""Price condition and Chronos task lists for chronos_v2.

Price condition on trading day D (all from split-adjusted closes up to D):
  eligible on D  AND  ret60 <= ret60_max  AND  ret20 < ret20_max
where retN = AdjC[D] / AdjC[D - N trading days] - 1 (per-stock trading days).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def price_signals_sql(strategy: dict, date_from: str, date_to: str) -> str:
    pc = strategy["price_condition"]
    return f"""
    WITH r AS (
      SELECT Date, Code, AdjC,
             AdjC / LAG(AdjC, 60) OVER w - 1 AS ret60,
             AdjC / LAG(AdjC, 20) OVER w - 1 AS ret20
      FROM prices_adj
      WINDOW w AS (PARTITION BY Code ORDER BY Date)
    )
    SELECT u.Date, u.Code, u.CoName, u.S17, u.S17Nm, u.ScaleCat, u.eligible, r.AdjC, r.ret60, r.ret20,
           COALESCE(u.eligible AND r.ret60 <= {pc['ret60_max']} AND r.ret20 < {pc['ret20_max']}, FALSE) AS cond_price
    FROM universe u JOIN r ON u.Date = r.Date AND u.Code = r.Code
    WHERE u.Date BETWEEN '{date_from}' AND '{date_to}'
    """


def compute_signals(con, strategy: dict, date_from: str, date_to: str) -> pd.DataFrame:
    return con.execute(price_signals_sql(strategy, date_from, date_to)).df()


def week_end_days(trading_days: list[str]) -> set[str]:
    """Last trading day of each ISO week. Pass the full exchange calendar (it includes
    future days), so a range ending mid-week does not create an extra reference day."""
    s = pd.Series(pd.to_datetime(sorted(trading_days)))
    iso = s.dt.isocalendar()
    key = iso["year"].astype(str) + "-" + iso["week"].astype(str)
    return set(s.groupby(key.values).max().dt.strftime("%Y-%m-%d"))


def forecast_tasks(signals: pd.DataFrame, ref_days: set[str]) -> pd.DataFrame:
    """(Code, Date) pairs to forecast: reference days (eligible on a week end) + candidate days."""
    s = signals.assign(Date=signals["Date"].astype(str))
    is_ref = s["eligible"].fillna(False).astype(bool) & s["Date"].isin(ref_days)
    is_cand = s["cond_price"].fillna(False).astype(bool)
    sel = is_ref | is_cand
    t = s.loc[sel, ["Date", "Code"]].copy()
    t["kind"] = np.where(is_ref[sel] & is_cand[sel], "ref+cand", np.where(is_ref[sel], "ref", "cand"))
    return t.reset_index(drop=True)
