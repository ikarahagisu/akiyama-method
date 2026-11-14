"""Dataset registry: where each dataset comes from and when its rows became knowable.

``available_at`` is the earliest JST time at which we treat a row as usable for
a decision. Where the source gives a publish time we use it (basis "exact");
otherwise a conservative assumed time from settings is used and the basis says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Avail:
    """How to derive available_at.

    kind:
      exact        date_col + time_col from the source
      day_time     date_col + an assumed time (setting name in ``time_setting``)
      next_tday    next trading day after date_col + assumed time
      pub_or_next  ``date_col`` (a publish date) + assumed time if present,
                   else next trading day after ``fallback_col``
      none         reference data with no point-in-time meaning (calendar)
    """

    kind: str
    date_col: str = ""
    time_col: str = ""
    time_setting: str = ""
    fallback_col: str = ""


@dataclass(frozen=True)
class Dataset:
    name: str
    endpoint: str
    key_cols: tuple[str, ...]
    avail: Avail
    str_cols: tuple[str, ...] = field(default=("Code",))


DATASETS: dict[str, Dataset] = {
    d.name: d
    for d in [
        Dataset("calendar", "/markets/calendar", ("Date",), Avail("none"), ("HolDiv",)),
        Dataset(
            "master",
            "/equities/master",
            ("Date", "Code"),
            Avail("day_time", "Date", time_setting="master_time"),
            ("Code", "S17", "S33", "Mkt", "Mrgn", "ProdCat"),
        ),
        Dataset(
            "prices",
            "/equities/bars/daily",
            ("Date", "Code"),
            Avail("day_time", "Date", time_setting="prices_close"),
            ("Code", "UL", "LL"),
        ),
        Dataset(
            "topix",
            "/indices/bars/daily/topix",
            ("Date",),
            Avail("day_time", "Date", time_setting="index_close"),
            (),
        ),
        Dataset(
            "indices",
            "/indices/bars/daily",
            ("Date", "Code"),
            Avail("day_time", "Date", time_setting="index_close"),
        ),
        Dataset(
            "fins_summary",
            "/fins/summary",
            ("DiscNo", "Code"),
            Avail("exact", "DiscDate", "DiscTime"),
            ("Code", "DiscNo"),
        ),
        Dataset(
            "earnings_date",
            "/fins/earnings-date",
            ("PubDate", "Code", "FQName", "SchDate"),
            Avail("day_time", "PubDate", time_setting="pub_date_time"),
            ("Code", "FYE"),
        ),
        Dataset(
            "margin_interest",
            "/markets/margin-interest",
            ("Date", "Code", "IssType"),
            Avail("pub_or_next", "PubDate", time_setting="pub_date_time", fallback_col="Date"),
            ("Code", "IssType"),
        ),
        Dataset(
            "short_ratio",
            "/markets/short-ratio",
            ("Date", "S33"),
            Avail("next_tday", "Date", time_setting="next_day_time"),
            ("S33",),
        ),
        Dataset(
            "investor_types",
            "/equities/investor-types",
            ("PubDate", "Section", "StDate"),
            Avail("day_time", "PubDate", time_setting="pub_date_time"),
            (),
        ),
        Dataset(
            "short_sale_report",
            "/markets/short-sale-report",
            ("DiscDate", "CalcDate", "Code", "SSName", "DICName", "FundName"),
            Avail("day_time", "DiscDate", time_setting="pub_date_time"),
            ("Code", "SSName", "SSAddr", "DICName", "DICAddr", "FundName", "Notes"),
        ),
        Dataset(
            "margin_alert",
            "/markets/margin-alert",
            ("PubDate", "Code", "AppDate"),
            Avail("day_time", "PubDate", time_setting="pub_date_time"),
            ("Code", "PubReason", "ShrtOutRatio", "LongOutRatio", "TSEMrgnRegCls"),
        ),
        Dataset(
            "valuation",
            "/equities/valuation",
            ("Date", "Code"),
            Avail("day_time", "Date", time_setting="prices_close"),
        ),
    ]
}

# TDnet disclosures are synced separately (td/bulk + td/list) but normalised the same way.
TD_DATASET = Dataset(
    "td",
    "/td/list",
    ("DiscNo", "RevNo"),
    Avail("exact", "DiscDate", "DiscTime"),
    ("Code", "DiscNo", "RevNo", "DiscItems", "Docs"),
)


def code5(code: str | int) -> str:
    """'8174' -> '81740' (J-Quants uses 5-character codes)."""
    s = str(code).strip()
    return s + "0" if len(s) == 4 else s
