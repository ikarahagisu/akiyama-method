"""Backtest for chronos_v2 candidates.

Two views, kept separate:

1. Candidate level (no account): for every price-condition day D of stock S,
   buy at the open of S's next trading day, sell at the open 20 trading days later.
   Gives net return, MAE (intraday low) and MFE per candidate, so C0 pass vs fail
   can be compared on identical rules without slot effects.

2. Account simulation: 3,000,000 yen, 3 slots, <= 1,000,000 yen per position in
   100-share lots (share value <= cap, fee on top), no duplicate holdings, entry at next open, exit at the open
   20 trading days later, 0.10% per side. When candidates exceed free slots they
   are ranked (A: C0 strength, B: depth of 60-day fall, R: seeded random).

Prices: split-adjusted O/H/L/C for returns and MAE, unadjusted open for the lot
cost. Sale proceeds are assumed usable for purchases at the same open.
A stock with no trading at the scheduled exit is closed at its last close
(``exit_reason = 'no_price'``).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

MAE_LEVELS = (-0.05, -0.075, -0.10, -0.15)


@dataclass
class AccountRules:
    initial_cash: float = 3_000_000.0
    slots: int = 3
    max_per_position: float = 1_000_000.0
    lot: int = 100
    fee: float = 0.001
    hold_days: int = 20


# ------------------------------------------------------------------ prices
class PriceBook:
    """Per-code arrays of dates and prices for fast lookups."""

    def __init__(self, df: pd.DataFrame):
        df = df.sort_values(["Code", "Date"])
        self.codes, starts = np.unique(df["Code"].to_numpy(), return_index=True)
        self.bounds = dict(zip(self.codes, zip(starts, np.append(starts[1:], len(df)))))
        self.date = df["Date"].astype(str).to_numpy()
        for c in ("O", "AdjO", "AdjH", "AdjL", "AdjC"):
            setattr(self, c, df[c].to_numpy(dtype=float))
        self.last_date = self.date.max() if len(self.date) else ""

    def rows(self, code: str) -> tuple[int, int]:
        return self.bounds.get(code, (0, 0))

    def index_after(self, code: str, d: str) -> int:
        """Global row index of the first trading day of ``code`` after date ``d`` (or -1)."""
        lo, hi = self.rows(code)
        i = lo + np.searchsorted(self.date[lo:hi], d, side="right")
        return i if i < hi else -1


def load_pricebook(con, codes) -> PriceBook:
    con.register("_bt_codes", pd.DataFrame({"Code": sorted(set(map(str, codes)))}))
    df = con.execute(
        "SELECT Code, Date, O, AdjO, AdjH, AdjL, AdjC FROM prices_adj "
        "WHERE Code IN (SELECT Code FROM _bt_codes) AND AdjO IS NOT NULL ORDER BY Code, Date"
    ).df()
    return PriceBook(df)


# --------------------------------------------------------- candidate level
def candidate_outcomes(cands: pd.DataFrame, pb: PriceBook, rules: AccountRules) -> pd.DataFrame:
    """Forward outcome of each candidate under the fixed entry/exit rule."""
    out = cands.copy()
    n = len(out)
    res = {k: np.full(n, np.nan) for k in ("entry_open", "exit_open", "net", "mae_low", "mae_close", "mfe_high")}
    entry_date = np.empty(n, dtype=object)
    exit_reason = np.empty(n, dtype=object)
    for k, (code, d) in enumerate(zip(out["Code"].astype(str), out["Date"].astype(str))):
        i = pb.index_after(code, d)
        if i < 0:
            exit_reason[k] = "no_entry"
            continue
        lo, hi = pb.rows(code)
        j = i + rules.hold_days
        e = pb.AdjO[i]
        if j < hi:
            x, reason, last = pb.AdjO[j], "scheduled", j - 1
        else:
            x, reason, last = pb.AdjC[hi - 1], "no_price", hi - 1
            if pb.date[hi - 1] >= pb.last_date:
                reason = "open"  # still running at the end of data
        entry_date[k] = pb.date[i]
        res["entry_open"][k], res["exit_open"][k] = e, x
        res["net"][k] = (x / e) * (1 - rules.fee) / (1 + rules.fee) - 1
        seg = slice(i, last + 1)
        res["mae_low"][k] = np.nanmin(pb.AdjL[seg]) / e - 1
        res["mae_close"][k] = np.nanmin(pb.AdjC[seg]) / e - 1
        res["mfe_high"][k] = np.nanmax(pb.AdjH[seg]) / e - 1
        exit_reason[k] = reason
    for k, v in res.items():
        out[k] = v
    out["entry_date"], out["exit_reason"] = entry_date, exit_reason
    return out


def summarize_candidates(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    d = df[df["exit_reason"].isin(["scheduled", "no_price"])]
    agg = {"n": ("net", "size"), "mean_net": ("net", "mean"), "median_net": ("net", "median"),
           "win_rate": ("net", lambda s: (s > 0).mean())}
    for lv in MAE_LEVELS:
        agg[f"mae{int(-lv * 1000) / 10:g}"] = ("mae_low", lambda s, lv=lv: (s <= lv).mean())
    return d.groupby(by, dropna=False).agg(**agg).reset_index()


# ------------------------------------------------------------ account sim
@dataclass
class Position:
    code: str
    entry_idx: int
    exit_idx: int  # -1: data ends before the scheduled exit
    shares: int
    cost: float  # yen paid incl. fee
    entry_adj: float
    decision_date: str
    rank_value: float


@dataclass
class SimResult:
    trades: pd.DataFrame
    equity: pd.DataFrame
    summary: dict = field(default_factory=dict)


def rank_candidates(day: pd.DataFrame, ranking: str, rng: np.random.Generator) -> pd.DataFrame:
    if ranking == "A":  # C0 strength: forecast return over its threshold
        key = day["r_hat"] - np.maximum(day["c0_cost"], day["ref_p"].fillna(-np.inf))
        return day.assign(rank_value=key).sort_values(["rank_value", "Code"], ascending=[False, True])
    if ranking == "B":  # deepest 60-day fall first
        return day.assign(rank_value=day["ret60"]).sort_values(["rank_value", "Code"])
    if ranking == "R":
        return day.assign(rank_value=rng.random(len(day))).sort_values("rank_value")
    raise ValueError(ranking)


def simulate(
    cands: pd.DataFrame,
    pb: PriceBook,
    trading_days: list[str],
    rules: AccountRules,
    ranking: str,
    seed: int = 0,
) -> SimResult:
    """Event-driven account simulation over ``trading_days`` (sorted)."""
    rng = np.random.default_rng(seed)
    by_decision = {d: g for d, g in cands.groupby(cands["Date"].astype(str))}
    cash = rules.initial_cash
    held: dict[str, Position] = {}
    trades, equity = [], []
    prev_day = None
    for t in trading_days:
        # 1) exits at today's open (scheduled), or at the last close of a stock that stopped trading
        for code in list(held):
            p = held[code]
            lo, hi = pb.rows(code)
            if p.exit_idx >= 0 and pb.date[p.exit_idx] <= t:
                proceeds = (p.cost / (1 + rules.fee)) * pb.AdjO[p.exit_idx] / p.entry_adj * (1 - rules.fee)
                trades.append(_trade(p, pb, proceeds, "scheduled"))
            elif p.exit_idx < 0 and pb.date[hi - 1] < t:
                proceeds = (p.cost / (1 + rules.fee)) * pb.AdjC[hi - 1] / p.entry_adj * (1 - rules.fee)
                trades.append(_trade(p, pb, proceeds, "no_price", exit_idx=hi - 1))
            else:
                continue
            cash += proceeds
            del held[code]
        # 2) entries at today's open from yesterday's decisions
        if prev_day is not None and prev_day in by_decision and len(held) < rules.slots:
            day = by_decision[prev_day]
            day = day[~day["Code"].isin(held)]
            for _, c in rank_candidates(day, ranking, rng).iterrows():
                if len(held) >= rules.slots:
                    break
                code = str(c["Code"])
                i = pb.index_after(code, prev_day)
                if i < 0 or pb.date[i] != t or np.isnan(pb.O[i]):
                    continue  # not trading at this open
                budget = min(rules.max_per_position, cash / (1 + rules.fee))
                shares = int(budget // (pb.O[i] * rules.lot)) * rules.lot
                if shares <= 0:
                    continue
                cost = shares * pb.O[i] * (1 + rules.fee)
                cash -= cost
                lo, hi = pb.rows(code)
                exit_idx = i + rules.hold_days if i + rules.hold_days < hi else -1
                held[code] = Position(code, i, exit_idx, shares, cost, pb.AdjO[i],
                                      prev_day, float(c["rank_value"]))
        # 3) mark to market at today's close
        mv = 0.0
        for code, p in held.items():
            lo, hi = pb.rows(code)
            k = min(lo + np.searchsorted(pb.date[lo:hi], t, side="right") - 1, hi - 1)
            mv += (p.cost / (1 + rules.fee)) * pb.AdjC[k] / p.entry_adj
        equity.append((t, cash, mv, len(held)))
        prev_day = t
    eq = pd.DataFrame(equity, columns=["Date", "cash", "stock_value", "positions"])
    eq["equity"] = eq["cash"] + eq["stock_value"]
    tr = pd.DataFrame(trades)
    return SimResult(tr, eq, account_summary(tr, eq, rules))


def _trade(p: Position, pb: PriceBook, proceeds: float, reason: str, exit_idx: int | None = None) -> dict:
    xi = p.exit_idx if exit_idx is None else exit_idx
    seg = slice(p.entry_idx, (xi if reason == "scheduled" else xi + 1))
    return {
        "Code": p.code, "decision_date": p.decision_date, "entry_date": pb.date[p.entry_idx],
        "exit_date": pb.date[xi], "shares": p.shares, "cost": p.cost, "proceeds": proceeds,
        "profit": proceeds - p.cost, "net": proceeds / p.cost - 1,
        "mae_low": np.nanmin(pb.AdjL[seg]) / p.entry_adj - 1, "exit_reason": reason, "rank_value": p.rank_value,
    }


def account_summary(tr: pd.DataFrame, eq: pd.DataFrame, rules: AccountRules) -> dict:
    if eq.empty:
        return {}
    e = eq["equity"].to_numpy()
    peak = np.maximum.accumulate(e)
    years = max((pd.Timestamp(eq["Date"].iloc[-1]) - pd.Timestamp(eq["Date"].iloc[0])).days / 365.25, 1e-9)
    s = {
        "start": eq["Date"].iloc[0], "end": eq["Date"].iloc[-1],
        "final_equity": float(e[-1]), "cumulative": float(e[-1] / rules.initial_cash - 1),
        "cagr": float((e[-1] / rules.initial_cash) ** (1 / years) - 1),
        "max_dd": float((e / peak - 1).min()),
        "mean_invested_ratio": float((eq["stock_value"] / eq["equity"]).mean()),
        "days_full": float((eq["positions"] >= rules.slots).mean()),
        "trades": int(len(tr)),
    }
    if len(tr):
        s.update({
            "win_rate": float((tr["net"] > 0).mean()), "mean_net": float(tr["net"].mean()),
            "worst_trade": float(tr["net"].min()),
            **{f"trade_mae{int(-lv * 1000) / 10:g}": int((tr["mae_low"] <= lv).sum()) for lv in MAE_LEVELS},
        })
    return s


# --------------------------------------------------------------- runner
VARIANTS = {
    # name: (candidate filter, rankings)
    "price_c0": ("c0", ("A", "B", "R")),
    "price_only": ("price", ("B", "R")),
}


def run_all(cands: pd.DataFrame, pb: PriceBook, trading_days: list[str], periods: dict[str, tuple[str, str]],
            rules: AccountRules = AccountRules(), seeds: int = 20, c0_cost: float = 0.002) -> dict:
    cands = cands.assign(Code=cands["Code"].astype(str), Date=cands["Date"].astype(str), c0_cost=c0_cost)
    results = {}
    for pname, (lo, hi) in periods.items():
        days = [d for d in trading_days if lo <= d <= hi]
        for vname, (filt, rankings) in VARIANTS.items():
            sub = cands[(cands["Date"] >= lo) & (cands["Date"] <= hi)]
            sub = sub[sub["c0"]] if filt == "c0" else sub
            for r in rankings:
                runs = range(seeds) if r == "R" else [0]
                sims = [simulate(sub, pb, days, rules, r, seed=s) for s in runs]
                key = f"{pname}|{vname}|{r}"
                if r == "R":
                    df = pd.DataFrame([x.summary for x in sims])
                    num = df.select_dtypes("number")
                    results[key] = {"median": num.median().to_dict(), "p10": num.quantile(0.1).to_dict(),
                                    "p90": num.quantile(0.9).to_dict(), "seeds": seeds}
                else:
                    results[key] = {"summary": sims[0].summary, "trades": sims[0].trades, "equity": sims[0].equity}
    return results


def run_sets(sets: dict[str, tuple[pd.DataFrame, tuple[str, ...]]], pb: PriceBook, trading_days: list[str],
             periods: dict[str, tuple[str, str]], sizings: list[dict], seeds: int = 20, c0_cost: float = 0.002,
             hold_days: int = 20, progress=print) -> dict:
    """Like run_all, for arbitrary candidate sets and account sizings.

    sets: name -> (candidates, rankings). Keys of the result: period|set@sizing|ranking.
    """
    results = {}
    for sz in sizings:
        rules = AccountRules(slots=sz["slots"], max_per_position=sz["max_per_position"], hold_days=hold_days)
        for pname, (lo, hi) in periods.items():
            days = [d for d in trading_days if lo <= d <= hi]
            for sname, (cands, rankings) in sets.items():
                c = cands.assign(Code=cands["Code"].astype(str), Date=cands["Date"].astype(str), c0_cost=c0_cost)
                c = c[(c["Date"] >= lo) & (c["Date"] <= hi)]
                for r in rankings:
                    runs = range(seeds) if r == "R" else [0]
                    sims = [simulate(c, pb, days, rules, r, seed=k) for k in runs]
                    key = f"{pname}|{sname}@{sz['name']}|{r}"
                    if r == "R":
                        df = pd.DataFrame([x.summary for x in sims])
                        num = df.select_dtypes("number")
                        results[key] = {"median": num.median().to_dict(), "p10": num.quantile(0.1).to_dict(),
                                        "p90": num.quantile(0.9).to_dict(), "seeds": seeds}
                    else:
                        results[key] = {"summary": sims[0].summary, "trades": sims[0].trades, "equity": sims[0].equity}
                    progress(f"  done {key}")
    return results


def save_results(results: dict, cand_table: pd.DataFrame, cand_summary: pd.DataFrame, out_dir: Path, meta: dict) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for key, r in results.items():
        period, variant, ranking = key.split("|")
        if "summary" in r:
            rows.append({"period": period, "variant": variant, "ranking": ranking, "stat": "value", **r["summary"]})
            if len(r["trades"]):
                r["trades"].to_csv(out_dir / f"trades_{period}_{variant}_{ranking}.csv", index=False)
            r["equity"].to_csv(out_dir / f"equity_{period}_{variant}_{ranking}.csv", index=False)
        else:
            for stat in ("median", "p10", "p90"):
                rows.append({"period": period, "variant": variant, "ranking": ranking, "stat": stat, **r[stat]})
    pd.DataFrame(rows).to_csv(out_dir / "account_summary.csv", index=False)
    cand_summary.to_csv(out_dir / "candidate_summary.csv", index=False)
    cand_table.to_parquet(out_dir / "candidate_outcomes.parquet", index=False)
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return out_dir
