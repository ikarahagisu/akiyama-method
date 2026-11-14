"""Index accumulation: monthly DCA vs buying on market-wide drops.

Every strategy receives the same contribution on the first trading day of each
month and buys the index at that day's close (TOPIX level as the price).
Holdings accrue a dividend yield (reinvested daily); idle cash earns nothing.

  dca        invest each contribution immediately
  dip        keep contributions as cash; on a day with TOPIX 20-day return <= threshold,
             invest all cash (at most once per ``cooldown`` trading days)
  half       half of each contribution via DCA, half reserved for dips
  dip_cap    like dip, but cash waiting ``max_wait_months`` months is invested anyway
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

STRATEGIES = ("dca", "dip", "half", "dip_cap")


@dataclass(frozen=True)
class DipParams:
    monthly: float = 100_000.0
    threshold: float = -0.03
    cooldown: int = 5
    max_wait_months: int = 6
    div_yield: float = 0.02


def r20_of(c: np.ndarray) -> np.ndarray:
    r = np.full(len(c), np.nan)
    r[20:] = c[20:] / c[:-20] - 1
    return r


def run(dates: np.ndarray, c: np.ndarray, r20: np.ndarray, strategy: str, p: DipParams) -> dict:
    month = pd.to_datetime(pd.Series(dates)).dt.to_period("M").to_numpy()
    first_of_month = np.r_[True, month[1:] != month[:-1]]
    daily_div = (1 + p.div_yield) ** (1 / 245) - 1
    units = cash = contributed = 0.0
    flows: list[tuple[str, float]] = []
    lots: list[tuple[int, float]] = []
    values, cash_ratio = [], []
    last_dip, m_idx, n_dips = -10**9, -1, 0
    for i in range(len(c)):
        units *= 1 + daily_div
        if first_of_month[i]:
            m_idx += 1
            contributed += p.monthly
            flows.append((dates[i], -p.monthly))
            if strategy == "dca":
                units += p.monthly / c[i]
            elif strategy == "half":
                units += p.monthly / 2 / c[i]
                cash += p.monthly / 2
            else:
                cash += p.monthly
                lots.append((m_idx, p.monthly))
        dip_day = np.isfinite(r20[i]) and r20[i] <= p.threshold and i - last_dip >= p.cooldown
        if strategy != "dca" and dip_day and cash > 0:
            units += cash / c[i]
            cash, lots, last_dip, n_dips = 0.0, [], i, n_dips + 1
        if strategy == "dip_cap" and lots:
            old = sum(a for m, a in lots if m_idx - m >= p.max_wait_months)
            if old > 0:
                units += old / c[i]
                cash -= old
                lots = [(m, a) for m, a in lots if m_idx - m < p.max_wait_months]
        v = units * c[i] + cash
        values.append(v)
        cash_ratio.append(cash / v if v > 0 else 0.0)
    v = np.array(values)
    flows.append((dates[-1], v[-1]))
    return {"final_value": v[-1], "contributed": contributed, "gain": v[-1] / contributed - 1,
            "irr": xirr(flows), "max_dd": float((v / np.maximum.accumulate(v) - 1).min()),
            "mean_cash_ratio": float(np.mean(cash_ratio)), "cash_left": cash, "dip_buys": n_dips}


def simulate(px: pd.DataFrame, strategy: str, p: DipParams = DipParams(), warmup: int = 0) -> dict:
    """px: Date, C sorted. The first ``warmup`` rows only feed the 20-day return."""
    c = px["C"].to_numpy(dtype=float)
    r = r20_of(c)
    return run(px["Date"].to_numpy()[warmup:], c[warmup:], r[warmup:], strategy, p)


def xirr(flows: list[tuple[str, float]]) -> float:
    t0 = pd.Timestamp(flows[0][0])
    ts = np.array([(pd.Timestamp(d) - t0).days / 365.25 for d, _ in flows])
    cf = np.array([a for _, a in flows])

    def npv(r):
        return np.sum(cf / (1 + r) ** ts)

    lo, hi = -0.99, 2.0
    if npv(lo) * npv(hi) > 0:
        return float("nan")
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def rolling_windows(px: pd.DataFrame, years: int, p: DipParams) -> pd.DataFrame:
    """One window per starting month; horizon ``years``. IRR and gain per strategy."""
    per = pd.to_datetime(px["Date"]).dt.to_period("M").to_numpy()
    starts = px["Date"].to_numpy()[np.r_[True, per[1:] != per[:-1]]]
    rows = []
    for s in starts:
        end = (pd.Timestamp(s) + pd.DateOffset(years=years)).strftime("%Y-%m-%d")
        i0 = int(np.searchsorted(px["Date"].to_numpy(), s))
        sub = px[(px["Date"] < end)].iloc[max(0, i0 - 20):]
        warm = i0 - max(0, i0 - 20)
        if pd.Timestamp(sub["Date"].iloc[-1]) < pd.Timestamp(end) - pd.Timedelta(days=10) or i0 < 20:
            continue
        row = {"start": s}
        for st in STRATEGIES:
            r = simulate(sub, st, p, warmup=warm)
            row[f"{st}_irr"], row[f"{st}_gain"] = r["irr"], r["gain"]
        rows.append(row)
    return pd.DataFrame(rows)
