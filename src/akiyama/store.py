"""DuckDB views over the normalised Parquet tables.

``prices_adj`` adds split-adjusted OHLCV computed from J-Quants ``AdjFactor``
(the factor applies on its own date to all earlier prices). Use raw O/H/L/C
for anything that depends on the actual yen price (e.g. 100-share cost).
"""

from __future__ import annotations

from pathlib import Path

import duckdb


def connect(norm_dir: Path, db_path: str = ":memory:") -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(db_path)
    for d in sorted(p for p in Path(norm_dir).iterdir() if p.is_dir()) if Path(norm_dir).exists() else []:
        if not any(d.glob("year=*/data.parquet")):
            continue
        glob = (d / "year=*" / "data.parquet").as_posix()
        con.execute(f"CREATE OR REPLACE VIEW {d.name} AS SELECT * FROM read_parquet('{glob}', union_by_name=true)")
    if (Path(norm_dir) / "prices").exists():
        con.execute(
            """
            CREATE OR REPLACE VIEW prices_adj AS
            WITH f AS (
              SELECT *,
                COALESCE(EXP(SUM(LN(NULLIF(AdjFactor, 0))) OVER (
                  PARTITION BY Code ORDER BY Date
                  ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING)), 1.0) AS adj_mult
              FROM prices
            )
            SELECT *, O*adj_mult AS AdjO, H*adj_mult AS AdjH, L*adj_mult AS AdjL, C*adj_mult AS AdjC,
                   Vo/adj_mult AS AdjVo
            FROM f
            """
        )
    return con
