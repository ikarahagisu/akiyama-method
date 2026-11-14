"""Golden cross + RSI strategy with take-profit / stop-loss (separate from chronos).

Signal on day D (split-adjusted closes, eligible stocks only):
  SMA5 crosses above SMA25 on D (SMA5[D-1] <= SMA25[D-1] and SMA5[D] > SMA25[D])
  and RSI14[D-1] <= 50 and RSI14[D] > RSI14[D-1]           (Wilder's RSI)
Trade: buy at the open of the next trading day (price e). From the entry day on:
  - from the second day, an open at or beyond a level exits at that open (gap)
  - intraday: low <= e*(1-sl) -> stop, high >= e*(1+tp) -> take profit
    both on the same day: 'conservative' assumes the stop came first,
    'optimistic' assumes the take profit came first
  - otherwise exit at the open ``max_days`` trading days after entry
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .backtest import AccountRules, PriceBook


@dataclass(frozen=True)
class GCParams:
    fast: int = 5
    slow: int = 25
    rsi_n: int = 14
    rsi_max_prev: float = 50.0
    tp: float = 0.05
    sl: float = 0.03
    max_days: int = 20


def rsi_wilder(close: np.ndarray, n: int = 14) -> np.ndarray:
    out = np.full(len(close), np.nan)
    if len(close) <= n:
        return out
    d = np.diff(close)
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = up[:n].mean(), dn[:n].mean()
    for i in range(n, len(close)):
        if i > n:
            au = (au * (n - 1) + up[i - 1]) / n
            ad = (ad * (n - 1) + dn[i - 1]) / n
        out[i] = 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)
    return out


def signals(prices: pd.DataFrame, eligible: pd.DataFrame, p: GCParams = GCParams()) -> pd.DataFrame:
    """prices: Code, Date, AdjC sorted; eligible: Code, Date of eligible stock-days."""
    rows = []
    for code, g in prices.groupby("Code", sort=False):
        c = g["AdjC"].to_numpy(dtype=float)
        if len(c) < p.slow + 2:
            continue
        f = pd.Series(c).rolling(p.fast).mean().to_numpy()
        s = pd.Series(c).rolling(p.slow).mean().to_numpy()
        r = rsi_wilder(c, p.rsi_n)
        cross = np.zeros(len(c), bool)
        cross[1:] = (f[:-1] <= s[:-1]) & (f[1:] > s[1:])
        rsi_ok = np.zeros(len(c), bool)
        rsi_ok[1:] = (r[:-1] <= p.rsi_max_prev) & (r[1:] > r[:-1])
        idx = np.where(cross & rsi_ok)[0]
        if len(idx):
            rows.append(pd.DataFrame({"Code": code, "Date": g["Date"].to_numpy()[idx], "rsi": r[idx]}))
    sig = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["Code", "Date", "rsi"])
    return sig.merge(eligible[["Code", "Date"]], on=["Code", "Date"], how="inner")


def trade_path(pb: PriceBook, code: str, d: str, p: GCParams, mode: str) -> dict | None:
    """Exit of one trade under TP/SL rules. Returns None if there is no entry."""
    i = pb.index_after(code, d)
    if i < 0:
        return None
    lo, hi = pb.rows(code)
    e = pb.AdjO[i]
    up, dn = e * (1 + p.tp), e * (1 - p.sl)
    both = False
    for k in range(i, min(i + p.max_days, hi)):
        if k > i:
            o = pb.AdjO[k]
            if o <= dn:
                return _res(pb, i, k, o, "gap_sl", "open", e, both)
            if o >= up:
                return _res(pb, i, k, o, "gap_tp", "open", e, both)
        hit_sl, hit_tp = pb.AdjL[k] <= dn, pb.AdjH[k] >= up
        if hit_sl and hit_tp:
            both = True
            if mode == "conservative":
                return _res(pb, i, k, dn, "sl", "intraday", e, both)
            return _res(pb, i, k, up, "tp", "intraday", e, both)
        if hit_sl:
            return _res(pb, i, k, dn, "sl", "intraday", e, both)
        if hit_tp:
            return _res(pb, i, k, up, "tp", "intraday", e, both)
    j = i + p.max_days
    if j < hi:
        return _res(pb, i, j, pb.AdjO[j], "time", "open", e, both)
    reason = "open" if pb.date[hi - 1] >= pb.last_date else "no_price"
    return _res(pb, i, hi - 1, pb.AdjC[hi - 1], reason, "close", e, both)


def _res(pb, i, k, x, reason, at, e, both) -> dict:
    return {"entry_idx": i, "exit_idx": k, "entry_date": pb.date[i], "exit_date": pb.date[k], "entry_adj": e,
            "entry_open_raw": pb.O[i], "exit_price": x, "gross": x / e - 1, "exit_reason": reason, "exit_at": at,
            "both_hit": both}


def trades(sig: pd.DataFrame, pb: PriceBook, p: GCParams, mode: str, fee: float = 0.001) -> pd.DataFrame:
    out = []
    for code, d in zip(sig["Code"].astype(str), sig["Date"].astype(str)):
        r = trade_path(pb, code, d, p, mode)
        if r is not None:
            out.append({"Code": code, "Date": d, **r})
    t = pd.DataFrame(out)
    if len(t):
        t["net"] = (1 + t["gross"]) * (1 - fee) / (1 + fee) - 1
    return t


def simulate_trades(tr: pd.DataFrame, pb: PriceBook, days: list[str], rules: AccountRules, seed: int = 0) -> dict:
    """Account with precomputed exits. Open exits settle before same-day entries;
    intraday/close exits settle after the day's entries (cash usable the next day)."""
    rng = np.random.default_rng(seed)
    by_entry = {d: g for d, g in tr[tr["exit_reason"] != "open"].groupby("entry_date")} if len(tr) else {}
    cash, held, done, eq = rules.initial_cash, {}, [], []
    for t in days:
        for code in [c for c, h in held.items() if h["exit_date"] == t and h["exit_at"] == "open"]:
            h = held.pop(code)
            cash += h["value0"] * (1 + h["gross"]) * (1 - rules.fee)
            done.append(h)
        if t in by_entry and len(held) < rules.slots:
            g = by_entry[t]
            g = g.iloc[rng.permutation(len(g))]
            for _, c in g.iterrows():
                if len(held) >= rules.slots:
                    break
                if c["Code"] in held:
                    continue
                budget = min(rules.max_per_position, cash / (1 + rules.fee))
                shares = int(budget // (c["entry_open_raw"] * rules.lot)) * rules.lot
                if shares <= 0:
                    continue
                v0 = shares * c["entry_open_raw"]
                cash -= v0 * (1 + rules.fee)
                held[c["Code"]] = {**c.to_dict(), "value0": v0, "shares": shares}
        for code in [c for c, h in held.items() if h["exit_date"] == t and h["exit_at"] != "open"]:
            h = held.pop(code)
            cash += h["value0"] * (1 + h["gross"]) * (1 - rules.fee)
            done.append(h)
        mv = 0.0
        for code, h in held.items():
            lo, hi_ = pb.rows(code)
            k = min(lo + np.searchsorted(pb.date[lo:hi_], t, side="right") - 1, hi_ - 1)
            mv += h["value0"] * pb.AdjC[k] / h["entry_adj"]
        eq.append((t, cash, mv, len(held)))
    e = pd.DataFrame(eq, columns=["Date", "cash", "stock_value", "positions"])
    e["equity"] = e["cash"] + e["stock_value"]
    d = pd.DataFrame(done)
    v = e["equity"].to_numpy()
    if not len(v):
        return {}
    years = max((pd.Timestamp(e["Date"].iloc[-1]) - pd.Timestamp(e["Date"].iloc[0])).days / 365.25, 1e-9)
    return {
        "cumulative": v[-1] / rules.initial_cash - 1, "cagr": (v[-1] / rules.initial_cash) ** (1 / years) - 1,
        "max_dd": float((v / np.maximum.accumulate(v) - 1).min()),
        "invested": float((e["stock_value"] / e["equity"]).mean()), "trades": len(d),
        "win_rate": float((d["net"] > 0).mean()) if len(d) else np.nan,
        "mean_net": float(d["net"].mean()) if len(d) else np.nan,
    }


def summarize(t: pd.DataFrame) -> dict:
    d = t[t["exit_reason"] != "open"]
    if not len(d):
        return {"n": 0}
    daily = d.groupby("Date")["net"].mean()
    s = {"n": len(d), "days": d["Date"].nunique(), "mean": d["net"].mean(), "day_mean": daily.mean(),
         "win": (d["net"] > 0).mean(), "both_hit": d["both_hit"].mean()}
    for r in ("tp", "sl", "gap_tp", "gap_sl", "time"):
        s[f"pct_{r}"] = (d["exit_reason"] == r).mean()
    return s
