"""Helpers for derived tables partitioned by year (universe, signals, forecasts...)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

from .config import REPO_ROOT


def upsert_partitioned(df: pd.DataFrame, out_dir: Path, date_from: str, date_to: str, date_col: str = "Date") -> list[Path]:
    """Replace rows with ``date_from <= date <= date_to`` in each year file, keep the rest.

    Re-running a short recent range therefore never wipes earlier rows of the year.
    """
    out_dir = Path(out_dir)
    paths = []
    df = df.copy()
    df[date_col] = df[date_col].astype(str)
    years = sorted(set(df[date_col].str[:4]) | {str(y) for y in range(int(date_from[:4]), int(date_to[:4]) + 1)})
    for y in years:
        f = out_dir / f"year={y}" / "data.parquet"
        new = df[df[date_col].str[:4] == y]
        if f.exists():
            old = pd.read_parquet(f)
            old[date_col] = old[date_col].astype(str)
            old = old[(old[date_col] < date_from) | (old[date_col] > date_to)]
            new = pd.concat([old, new], ignore_index=True) if len(old) else new
        if new.empty:
            continue
        new = new.sort_values([date_col] + [c for c in ("Code",) if c in new.columns])
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name("data.parquet.part")
        new.to_parquet(tmp, index=False)
        tmp.replace(f)
        paths.append(f)
    return paths


def register(con, name: str, out_dir: Path) -> bool:
    """Create a DuckDB view over a derived table if it exists."""
    out_dir = Path(out_dir)
    if not any(out_dir.glob("year=*/data.parquet")) and not any(out_dir.glob("*.parquet")):
        return False
    glob = (out_dir / "year=*" / "data.parquet").as_posix() if any(out_dir.glob("year=*")) else (out_dir / "*.parquet").as_posix()
    con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{glob}', union_by_name=true)")
    return True


def load_strategy(name: str = "chronos_v2") -> dict:
    return yaml.safe_load((REPO_ROOT / "config" / f"strategy_{name}.yaml").read_text(encoding="utf-8"))
