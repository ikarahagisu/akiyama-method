import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from akiyama.datasets import DATASETS, Avail, code5
from akiyama.ingest import key_period, sync_bulk
from akiyama.jquants import JQuantsClient, JQuantsError
from akiyama.normalize import add_availability, next_trading_day, normalize_bulk
from akiyama.rawstore import RawStore
from akiyama.store import connect

AVAIL = {"prices_close": "16:30", "master_time": "07:00", "pub_date_time": "23:59", "next_day_time": "23:59", "index_close": "16:30"}


# ------------------------------------------------------------------ fake HTTP
class Resp:
    def __init__(self, status=200, payload=None, content=b"", headers=None):
        self.status_code = status
        self._payload = payload
        self.content = content
        self.headers = headers or {}
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError
        return self._payload

    def iter_content(self, chunk_size):
        yield self.content


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None, stream=False):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        return self.handler(url, dict(params or {}))


def make_client(handler):
    sess = FakeSession(handler)
    return JQuantsClient("SECRET", min_interval_sec=0, session=sess, sleep=lambda s: None), sess


# ---------------------------------------------------------------- client
def test_pagination_merges_pages():
    def h(url, params):
        if "pagination_key" not in params:
            return Resp(payload={"data": [1, 2], "pagination_key": "p2"})
        return Resp(payload={"data": [3]})

    c, sess = make_client(h)
    assert c.get("/x")["data"] == [1, 2, 3]
    assert sess.calls[1][1]["pagination_key"] == "p2"


def test_retries_429_then_succeeds():
    seq = iter([Resp(429, {"message": "slow"}, headers={"Retry-After": "1"}), Resp(payload={"data": []})])
    c, sess = make_client(lambda u, p: next(seq))
    assert c.get("/x") == {"data": []}
    assert len(sess.calls) == 2


def test_4xx_raises_with_message():
    c, _ = make_client(lambda u, p: Resp(403, {"message": "not on your plan"}))
    with pytest.raises(JQuantsError) as ei:
        c.get("/fins/details")
    assert ei.value.status == 403 and "plan" in ei.value.message


def test_api_key_never_sent_to_download_host(tmp_path):
    c, sess = make_client(lambda u, p: Resp(content=b"abc"))
    c.download("https://bucket.example.com/file.csv.gz?sig=1", tmp_path / "f")
    assert "x-api-key" not in sess.calls[0][2]
    with pytest.raises(ValueError):
        c._request("https://evil.example.com/x", None, with_key=True)


# ---------------------------------------------------------------- helpers
def test_key_period_and_code5():
    assert key_period("a/historical/2022/x_202202.csv.gz") == "202202"
    assert key_period("a/live/x_20261113.csv.gz") == "20261113"
    assert key_period("markets/calendar/live/markets_calendar.csv.gz") == ""
    assert code5("8174") == "81740" and code5("81740") == "81740"


def test_next_trading_day_skips_weekend():
    tdays = np.array(["2026-11-13", "2026-11-16"], dtype="datetime64[D]")  # Fri, Mon
    out = next_trading_day(pd.Series(["2026-11-13", "2026-11-14", "2026-11-16"]), tdays)
    assert out.tolist()[:2] == ["2026-11-16", "2026-11-16"]
    assert pd.isna(out.iloc[2])  # beyond calendar -> unknown, not guessed


def test_availability_exact_and_missing_time():
    df = pd.DataFrame({"DiscDate": ["2022-02-18", "2022-02-18"], "DiscTime": ["15:45", None]})
    out = add_availability(df, Avail("exact", "DiscDate", "DiscTime"), AVAIL, None)
    assert str(out["available_at"].iloc[0]) == "2022-02-18 15:45:00+09:00"
    assert out["available_at_basis"].tolist() == ["exact", "assumed_end_of_day"]
    assert str(out["available_at"].iloc[1]) == "2022-02-18 23:59:00+09:00"


def test_availability_pub_or_next():
    tdays = np.array(["2022-02-04", "2022-02-07"], dtype="datetime64[D]")
    df = pd.DataFrame({"PubDate": [None, "2022-02-08"], "Date": ["2022-02-04", "2022-02-04"]})
    out = add_availability(df, DATASETS["margin_interest"].avail, AVAIL, tdays)
    assert str(out["available_at"].iloc[0]) == "2022-02-07 23:59:00+09:00"
    assert str(out["available_at"].iloc[1]) == "2022-02-08 23:59:00+09:00"
    assert out["available_at_basis"].tolist() == ["assumed_next_trading_day", "assumed_pub_date_time"]


# ------------------------------------------------- end to end on fake bulk API
def _csv_gz(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"))


PRICES_HIST = "Date,Code,O,H,L,C,UL,LL,Vo,Va,AdjFactor,MktCap,ExRT\n" \
    "2022-02-01,81740,1000,1000,1000,1000,0,0,100,1,1.0,,\n" \
    "2022-02-02,81740,1000,1000,1000,1000,0,0,100,1,0.5,,\n" \
    "2022-02-03,81740,500,500,500,500,0,0,200,1,1.0,,\n"
PRICES_LIVE = "Date,Code,O,H,L,C,UL,LL,Vo,Va,AdjFactor,MktCap,ExRT\n" \
    "2022-02-03,81740,510,510,510,510,0,0,200,1,1.0,,\n"


def fake_bulk_api(files: dict[str, tuple[str, bytes]]):
    """files: key -> (LastModified, content)"""

    def h(url, params):
        if url.endswith("/bulk/list"):
            prefix = params["endpoint"].strip("/") + "/"
            return Resp(payload={"data": [
                {"Key": k, "LastModified": v[0], "Size": len(v[1])} for k, v in files.items() if k.startswith(prefix)
            ]})
        if url.endswith("/bulk/get"):
            return Resp(payload={"url": "https://bucket.example.com/" + params["key"]})
        if url.startswith("https://bucket.example.com/"):
            return Resp(content=files[url.split("bucket.example.com/")[1]][1])
        return Resp(404, {"message": "nope"})

    return h


def test_sync_is_append_only_and_normalize_prefers_live(tmp_path):
    files = {
        "equities/bars/daily/historical/2022/equities_bars_daily_202202.csv.gz": ("2026-08-10T01:50:32+00:00", _csv_gz(PRICES_HIST)),
        "equities/bars/daily/live/equities_bars_daily_20220203.csv.gz": ("2022-02-03T06:51:00+00:00", _csv_gz(PRICES_LIVE)),
    }
    c, _ = make_client(fake_bulk_api(files))
    store = RawStore(tmp_path / "raw")
    rep = sync_bulk(c, store, ["prices"])
    assert len(rep["prices"].downloaded) == 2
    # second run: nothing new
    rep = sync_bulk(c, store, ["prices"])
    assert rep["prices"].downloaded == [] and rep["prices"].skipped == 2
    # provider republishes the monthly file -> stored as a new version, old kept
    k = "equities/bars/daily/historical/2022/equities_bars_daily_202202.csv.gz"
    files[k] = ("2026-09-01T00:00:00+00:00", files[k][1])
    rep = sync_bulk(c, RawStore(tmp_path / "raw"), ["prices"])
    assert rep["prices"].downloaded == [k]
    assert len([p for p in (tmp_path / "raw" / "bulk").rglob("*.csv.gz") if p.is_file()]) == 3

    norm = tmp_path / "norm"
    n = normalize_bulk(RawStore(tmp_path / "raw"), norm, "prices", AVAIL)
    assert n == 3
    df = pd.read_parquet(norm / "prices" / "year=2022" / "data.parquet")
    assert df.loc[df["Date"] == "2022-02-03", "C"].item() == 510  # live overrides historical
    assert df["Code"].iloc[0] == "81740"  # code kept as string
    assert str(df["available_at"].iloc[0]) == "2022-02-01 16:30:00+09:00"

    con = connect(norm)
    adj = con.execute("SELECT Date, AdjC FROM prices_adj ORDER BY Date").fetchall()
    # split factor 0.5 on 02-02 halves prices before 02-02
    assert [round(r[1], 2) for r in adj] == [500.0, 1000.0, 510.0]


# ------------------------------------------------------------------ TDnet
TD_HDR = "DiscNo,Code,Name,DiscDate,DiscTime,Title,DiscStatus,RevNo,DiscItems,Docs\n"


def test_td_rolling_window_kept_and_bulk_refreshed_monthly(tmp_path):
    from datetime import date

    from akiyama.ingest import sync_td
    from akiyama.normalize import normalize_td

    state = {"bulk_version": "2026-11-12T17:00:00Z",
             "bulk": TD_HDR + "1,81740,a,2021-11-13,15:00,old,,1,1,g\n2,81740,a,2026-11-12,15:00,x,,1,1,g\n"}
    calls = {"bulk": 0}

    def h(url, params):
        if url.endswith("/td/bulk"):
            calls["bulk"] += 1
            return Resp(payload={"lastUpdated": state["bulk_version"], "url": "https://bucket.example.com/td"})
        if url == "https://bucket.example.com/td":
            return Resp(content=_csv_gz(state["bulk"]))
        if url.endswith("/td/list"):
            rows = [{"DiscNo": "3", "Code": "81740", "Name": "a", "DiscDate": params["date"], "DiscTime": "16:00",
                     "Title": "new", "DiscStatus": None, "RevNo": "1", "DiscItems": ["1", "2"], "Docs": ["g"]}] \
                if params["date"] == "2026-11-14" else []
            return Resp(payload={"data": rows})
        return Resp(404, {"message": "?"})

    c, _ = make_client(h)
    store = RawStore(tmp_path / "raw")
    sync_td(c, store, today=date(2026, 11, 14))
    # next day the provider window rolls: the 2021-11-13 row disappears from the bulk file
    state["bulk_version"] = "2026-11-13T17:00:00Z"
    state["bulk"] = TD_HDR + "2,81740,a,2026-11-12,15:00,x,,1,1,g\n"
    sync_td(c, RawStore(tmp_path / "raw"), today=date(2026, 11, 15))
    assert calls["bulk"] == 1  # not re-downloaded within the refresh interval

    n = normalize_td(RawStore(tmp_path / "raw"), tmp_path / "norm", AVAIL)
    df = pd.concat(pd.read_parquet(p) for p in (tmp_path / "norm" / "td").rglob("*.parquet"))
    assert set(df["DiscNo"]) == {"1", "2", "3"} and n == 3
    assert df.loc[df["DiscNo"] == "3", "DiscItems"].item() == "1;2"

    # after the refresh interval a new bulk copy is stored, and old rows still survive
    sync_td(c, RawStore(tmp_path / "raw"), today=date(2026, 12, 12))
    assert calls["bulk"] == 2
    normalize_td(RawStore(tmp_path / "raw"), tmp_path / "norm", AVAIL)
    df = pd.concat(pd.read_parquet(p) for p in (tmp_path / "norm" / "td").rglob("*.parquet"))
    assert "1" in set(df["DiscNo"])
