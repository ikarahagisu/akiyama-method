"""Command line entry point: ``python -m akiyama <command>``."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import api_key_from_env, load_settings
from .datasets import DATASETS
from .rawstore import RawStore


def _client(settings):
    from .jquants import JQuantsClient

    return JQuantsClient(
        api_key_from_env(),
        base_url=settings.base_url,
        min_interval_sec=settings.min_interval_sec,
        max_retries=settings.max_retries,
    )


def cmd_sync_bulk(args, settings) -> int:
    from .ingest import sync_bulk

    names = args.datasets.split(",") if args.datasets else settings.default_datasets
    unknown = [n for n in names if n not in DATASETS]
    if unknown:
        print(f"unknown datasets: {unknown}; known: {sorted(DATASETS)}", file=sys.stderr)
        return 2
    reports = sync_bulk(_client(settings), RawStore(settings.raw_dir), names, since=args.since or "")
    bad = 0
    for name, rep in reports.items():
        print(f"{name:16s} {rep.summary()}")
        for e in rep.errors[:5]:
            print(f"   ! {e}")
        bad += len(rep.errors)
    return 1 if bad else 0


def cmd_sync_td(args, settings) -> int:
    from .ingest import sync_td

    rep = sync_td(_client(settings), RawStore(settings.raw_dir))
    print(f"td {rep.summary()}")
    for e in rep.errors[:5]:
        print(f"   ! {e}")
    return 1 if rep.errors else 0


def cmd_normalize(args, settings) -> int:
    from .normalize import normalize_bulk, normalize_td

    store = RawStore(settings.raw_dir)
    names = args.datasets.split(",") if args.datasets else settings.default_datasets
    # calendar first: other datasets need trading days
    names = sorted((n for n in names if n in DATASETS), key=lambda n: n != "calendar")
    for n in names:
        print(f"{n:16s} rows={normalize_bulk(store, settings.norm_dir, n, settings.availability)}")
    if not args.datasets or "td" in args.datasets.split(","):
        print(f"{'td':16s} rows={normalize_td(store, settings.norm_dir, settings.availability)}")
    return 0


def cmd_fetch_docs(args, settings) -> int:
    from .ingest import fetch_td_docs
    from .store import connect

    con = connect(settings.norm_dir)
    df = con.execute("SELECT DiscNo, Code, DiscDate FROM td").df()
    rep = fetch_td_docs(
        _client(settings),
        RawStore(settings.raw_dir),
        df,
        args.codes.split(","),
        args.date_from or "",
        args.date_to or "",
        settings.td_doc_types,
    )
    print(f"docs {rep.summary()}")
    for e in rep.errors[:5]:
        print(f"   ! {e}")
    return 1 if rep.errors else 0


def cmd_daily(args, settings) -> int:
    """Routine after-close update: new bulk files, TDnet, then normalise."""
    rc = cmd_sync_bulk(argparse.Namespace(datasets=None, since=args.since), settings)
    rc |= cmd_sync_td(args, settings)
    rc |= cmd_normalize(argparse.Namespace(datasets=None), settings)
    return rc


def cmd_status(args, settings) -> int:
    from collections import Counter

    store = RawStore(settings.raw_dir)
    kinds = Counter(e.kind for e in store.entries())
    size = sum(e.size for e in store.entries())
    print(f"data_dir: {settings.data_dir}")
    print(f"raw files: {dict(kinds)}  total {size / 1e6:.1f} MB")
    if settings.norm_dir.exists():
        from .store import connect

        con = connect(settings.norm_dir)
        for (name,) in con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal ORDER BY 1").fetchall():
            if name == "prices_adj":
                continue
            cols = [r[0] for r in con.execute(f"DESCRIBE {name}").fetchall()]
            dcol = next((c for c in ("Date", "DiscDate", "PubDate") if c in cols), None)
            q = f"SELECT COUNT(*), MIN({dcol}), MAX({dcol}) FROM {name}" if dcol else f"SELECT COUNT(*), NULL, NULL FROM {name}"
            n, lo, hi = con.execute(q).fetchone()
            print(f"  {name:16s} {n:>12,d} rows  {lo} .. {hi}")
    return 0


def _derived(settings, name):
    return settings.data_dir / "derived" / name


def _year_chunks(date_from: str, date_to: str):
    """Split a date range by calendar year to keep memory flat on long ranges."""
    for y in range(int(date_from[:4]), int(date_to[:4]) + 1):
        lo = max(date_from, f"{y}-01-01")
        hi = min(date_to, f"{y}-12-31")
        if lo <= hi:
            yield lo, hi


def cmd_universe(args, settings) -> int:
    from .derived import upsert_partitioned
    from .store import connect
    from .universe import compute_universe

    con = connect(settings.norm_dir)
    for lo, hi in _year_chunks(args.date_from, args.date_to):
        df = compute_universe(con, lo, hi)
        if df.empty:
            continue
        upsert_partitioned(df, _derived(settings, "universe"), lo, hi)
        daily = df.groupby("Date")["eligible"].sum()
        print(f"universe {lo}..{hi} rows={len(df):,} eligible/day median={int(daily.median())} "
              f"min={int(daily.min())} max={int(daily.max())}")
    return 0


def cmd_signals(args, settings) -> int:
    from .derived import load_strategy, upsert_partitioned
    from .signals import compute_signals
    from .store import connect

    con = connect(settings.norm_dir)
    strategy = load_strategy()
    for lo, hi in _year_chunks(args.date_from, args.date_to):
        df = compute_signals(con, strategy, lo, hi)
        if df.empty:
            continue
        upsert_partitioned(df, _derived(settings, "signals"), lo, hi)
        print(f"signals {lo}..{hi} rows={len(df):,} price-condition days={int(df['cond_price'].sum()):,}")
    return 0


def _ref_days(con) -> set[str]:
    from .signals import week_end_days

    days = [r[0] for r in con.execute("SELECT Date FROM calendar WHERE HolDiv = '1'").fetchall()]
    return week_end_days(days)


def cmd_forecast(args, settings) -> int:
    from .derived import load_strategy
    from .forecast import chronos_predictor, model_tag, run_forecasts
    from .signals import forecast_tasks
    from .store import connect

    strategy = load_strategy()
    con = connect(settings.norm_dir)
    sig = con.execute(
        "SELECT Date, Code, eligible, cond_price FROM signals WHERE Date BETWEEN ? AND ?", [args.date_from, args.date_to]
    ).df()
    tasks = forecast_tasks(sig, _ref_days(con))
    print(f"tasks: {len(tasks):,} ({tasks['kind'].value_counts().to_dict()})")
    n = run_forecasts(con, tasks, strategy, _derived(settings, "forecasts") / model_tag(strategy),
                      chronos_predictor(strategy), limit=args.limit)
    print(f"new forecasts: {n:,}")
    return 0


def cmd_candidates(args, settings) -> int:
    import duckdb

    from .candidates import annotate, c0_flags
    from .derived import load_strategy, upsert_partitioned
    from .forecast import model_tag
    from .store import connect

    strategy = load_strategy()
    con = connect(settings.norm_dir)
    cands = con.execute(
        "SELECT * FROM signals WHERE cond_price AND Date BETWEEN ? AND ?", [args.date_from, args.date_to]
    ).df()
    cache = _derived(settings, "forecasts") / model_tag(strategy)
    if not any(cache.glob("part-*.parquet")):
        print("no forecasts yet; run `forecast` first", file=sys.stderr)
        return 2
    fc = duckdb.sql(f"SELECT * FROM read_parquet('{(cache / 'part-*.parquet').as_posix()}')").df()
    out = annotate(c0_flags(cands, fc, _ref_days(con), strategy), strategy)
    upsert_partitioned(out, _derived(settings, "candidates"), args.date_from, args.date_to)
    print(f"price-condition days={len(out):,}  c0 pass={int(out['c0'].sum()):,}  "
          f"status={out['c0_status'].value_counts().to_dict()}")
    return 0


def cmd_pipeline(args, settings) -> int:
    """universe -> signals -> forecast -> candidates for a date range."""
    for fn in (cmd_universe, cmd_signals, cmd_forecast, cmd_candidates):
        rc = fn(args, settings)
        if rc:
            return rc
    return 0


def cmd_roster(args, settings) -> int:
    from .config import REPO_ROOT
    from .store import connect
    from .universe import LEGACY_DEV_CODES, Criteria, compute_universe, make_meta, save_roster, stratified_roster

    con = connect(settings.norm_dir)
    day = compute_universe(con, args.asof, args.asof)
    if day.empty:
        print(f"no price rows on {args.asof} (not a trading day or data missing)", file=sys.stderr)
        return 2
    roster, strata = stratified_roster(day, args.n, args.seed, LEGACY_DEV_CODES)
    meta = make_meta(args.name, args.asof, args.n, args.seed, Criteria(), LEGACY_DEV_CODES,
                     int(day["eligible"].sum()), strata, args.note or "")
    path = save_roster(roster, meta, REPO_ROOT / "rosters")
    print(f"eligible on {args.asof}: {meta.eligible_count}; sampled {len(roster)} -> {path}")
    print(roster.groupby("S17Nm").size().to_string())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="akiyama")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--data-dir", help="override data_dir from settings")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync-bulk", help="download J-Quants bulk files not yet stored")
    s.add_argument("--datasets", help=f"comma list; default from settings. known: {','.join(DATASETS)}")
    s.add_argument("--since", help="YYYY-MM: skip files for earlier periods")
    s.set_defaults(fn=cmd_sync_bulk)

    s = sub.add_parser("sync-td", help="download TDnet disclosure index (5y bulk + recent days)")
    s.set_defaults(fn=cmd_sync_td)

    s = sub.add_parser("normalize", help="build point-in-time Parquet tables from raw files")
    s.add_argument("--datasets", help="comma list (add 'td' for TDnet); default: all configured + td")
    s.set_defaults(fn=cmd_normalize)

    s = sub.add_parser("fetch-docs", help="download TDnet PDFs for given codes")
    s.add_argument("--codes", required=True, help="comma list, e.g. 8174,3388")
    s.add_argument("--from", dest="date_from")
    s.add_argument("--to", dest="date_to")
    s.set_defaults(fn=cmd_fetch_docs)

    s = sub.add_parser("daily", help="sync-bulk + sync-td + normalize")
    s.add_argument("--since", help="YYYY-MM to limit bulk listing (e.g. current month)")
    s.set_defaults(fn=cmd_daily)

    s = sub.add_parser("universe", help="compute point-in-time eligibility for a date range")
    s.add_argument("--from", dest="date_from", required=True)
    s.add_argument("--to", dest="date_to", required=True)
    s.set_defaults(fn=cmd_universe)

    for name, fn, hlp in (
        ("signals", cmd_signals, "price condition for each eligible stock-day"),
        ("forecast", cmd_forecast, "Chronos-2 forecasts for reference and candidate days (resumable)"),
        ("candidates", cmd_candidates, "C0 check and candidate table"),
        ("pipeline", cmd_pipeline, "universe + signals + forecast + candidates"),
    ):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("--from", dest="date_from", help="default: 40 days ago")
        s.add_argument("--to", dest="date_to", help="default: today")
        s.add_argument("--limit", type=int, help="forecast: stop after N new forecasts (testing)")
        s.set_defaults(fn=fn)

    s = sub.add_parser("roster", help="freeze a fixed-seed stratified sample of eligible companies")
    s.add_argument("--name", required=True, help="file name under rosters/ (never overwritten)")
    s.add_argument("--asof", required=True, help="trading day YYYY-MM-DD")
    s.add_argument("--n", type=int, default=120)
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--note")
    s.set_defaults(fn=cmd_roster)

    s = sub.add_parser("status", help="show stored raw files and table coverage")
    s.set_defaults(fn=cmd_status)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    settings = load_settings(data_dir=args.data_dir)
    if hasattr(args, "date_to") and args.cmd in ("universe", "signals", "forecast", "candidates", "pipeline"):
        from datetime import date, timedelta

        args.date_to = min(args.date_to or "9999-12-31", date.today().isoformat())
        args.date_from = args.date_from or (date.today() - timedelta(days=40)).isoformat()
    return args.fn(args, settings)


if __name__ == "__main__":
    sys.exit(main())
