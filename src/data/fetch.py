"""Tải dữ liệu thô từ các nguồn đã chốt. Mọi lần tải đều được ghi vào source log.

Nguồn: Yahoo Finance (giá OHLC), CryptoCompare (đối chiếu/lấp khoảng trống),
Binance (đối chiếu, quote USDT), DefiLlama (supply, TVL).
Mọi mốc thời gian được chuẩn hóa về UTC 00:00 của ngày tương ứng.
"""
from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

import pandas as pd
import requests

def load_env(path: Path | None = None) -> None:
    """Đọc file .env (dòng KEY=VALUE) ở thư mục gốc project vào os.environ.
    Biến môi trường đã đặt sẵn trong terminal được ưu tiên. File .env nằm trong .gitignore."""
    path = path or Path(__file__).resolve().parents[2] / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():  # utf-8-sig: bỏ BOM do Notepad thêm
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


UA = {"User-Agent": "Mozilla/5.0 (academic research pipeline, Code 1)"}
OHLCV = ["open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------- helpers
def empty_ohlcv() -> pd.DataFrame:
    df = pd.DataFrame(columns=OHLCV, dtype=float)
    df.index = pd.DatetimeIndex([], tz="UTC", name="date_utc")
    return df


def to_utc_midnight(idx) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(pd.to_datetime(idx))
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    return idx.normalize()


def tidy(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Chuẩn hóa: cột OHLCV float, index UTC, bỏ trùng, cắt theo [start, end]."""
    df = df[OHLCV].astype(float).copy()
    df.index = to_utc_midnight(df.index)
    df.index.name = "date_utc"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    lo, hi = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    return df.loc[(df.index >= lo) & (df.index <= hi)]


# ---------------------------------------------------------------- price sources
def fetch_yahoo(ticker: str, start: str, end: str) -> pd.DataFrame:
    import yfinance as yf

    stop = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")  # end là exclusive
    df = yf.download(ticker, start=start, end=stop, interval="1d",
                     auto_adjust=False, progress=False)
    if df is None or df.empty:
        return empty_ohlcv()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    return tidy(df, start, end)


def fetch_cryptocompare(symbol: str, start: str, end: str, api_key: str | None = None) -> pd.DataFrame:
    url = "https://min-api.cryptocompare.com/data/v2/histoday"
    to_ts = int((pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)).timestamp())
    start_ts = int(pd.Timestamp(start, tz="UTC").timestamp())
    rows: list[dict] = []
    while True:
        params = {"fsym": symbol, "tsym": "USD", "limit": 2000, "toTs": to_ts}
        headers = dict(UA)
        if api_key:  # gửi qua header để key không xuất hiện trong URL / thông báo lỗi / source log
            headers["authorization"] = f"Apikey {api_key}"
        r = requests.get(url, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        js = r.json()
        if js.get("Response") == "Error":
            raise RuntimeError(js.get("Message", "CryptoCompare error"))
        data = js["Data"]["Data"]
        if not data:
            break
        rows.extend(data)
        first = data[0]["time"]
        if first <= start_ts or len(data) < 2:
            break
        to_ts = first - 1
        time.sleep(0.3)
    if not rows:
        return empty_ohlcv()
    df = pd.DataFrame(rows)
    df["date_utc"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("date_utc").rename(columns={"volumeto": "volume"})  # volumeto = quote (USD)
    df = df[df["close"] > 0]  # CryptoCompare trả 0 cho giai đoạn chưa niêm yết
    return tidy(df, start, end)


def fetch_binance(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Nến 1d spot. Giá quy về USDT (KHÔNG phải USD) => chỉ dùng đối chiếu."""
    bases = ["https://api.binance.com", "https://data-api.binance.vision"]
    cur = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int((pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)).timestamp() * 1000)
    rows: list[list] = []
    while cur < end_ms:
        data, err = None, None
        for base in bases:
            try:
                r = requests.get(f"{base}/api/v3/klines", headers=UA, timeout=30,
                                 params={"symbol": symbol, "interval": "1d",
                                         "startTime": cur, "endTime": end_ms, "limit": 1000})
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:  # thử mirror kế tiếp
                err = e
        if data is None:
            raise err  # type: ignore[misc]
        if not data:
            break
        rows.extend(data)
        cur = data[-1][0] + 86_400_000
        if len(data) < 1000:
            break
        time.sleep(0.2)
    if not rows:
        return empty_ohlcv()
    cols = ["open_time", "open", "high", "low", "close", "base_volume", "close_time",
            "volume", "n_trades", "tb_base", "tb_quote", "ignore"]
    df = pd.DataFrame(rows, columns=cols)
    df["date_utc"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("date_utc")  # volume = quote asset volume (USDT)
    return tidy(df, start, end)


# ---------------------------------------------------------------- DefiLlama
def _circ_now(asset: dict) -> float:
    c = asset.get("circulating") or {}
    v = c.get("peggedUSD")
    return float(v) if v else 0.0


def fetch_llama_supply(symbol: str, name: str, llama_id=None, alt=None) -> pd.DataFrame:
    """Circulating supply theo ngày (tổng các chain) + phần trên Ethereum."""
    base = "https://stablecoins.llama.fi"
    lst = requests.get(f"{base}/stablecoins?includePrices=true", headers=UA, timeout=30).json()["peggedAssets"]
    if llama_id is not None:
        pick = [a for a in lst if str(a.get("id")) == str(llama_id)]
    else:
        cands = [a for a in lst if str(a.get("symbol", "")).upper() == symbol.upper()]
        exact = [a for a in cands if str(a.get("name", "")).lower() == name.lower()]
        pick = exact or cands
        if not pick and alt:  # coin đã đổi tên/ký hiệu (vd. TerraUSD -> TerraClassicUSD/USTC)
            keys = [k.lower() for k in alt]
            pick = [a for a in lst if any(k in str(a.get("name", "")).lower()
                                          or k == str(a.get("symbol", "")).lower() for k in keys)]
    if not pick:
        raise ValueError(f"DefiLlama: không thấy stablecoin {symbol}/{name} (thử đặt llama_id trong config)")
    asset = max(pick, key=_circ_now)
    matched = f"{asset.get('name')} ({asset.get('symbol')}, id={asset.get('id')})"
    js = requests.get(f"{base}/stablecoin/{asset['id']}", headers=UA, timeout=60).json()

    recs = []
    for chain, v in (js.get("chainBalances") or {}).items():
        for t in v.get("tokens", []):
            circ = t.get("circulating") or {}
            val = circ.get("peggedUSD", next(iter(circ.values()), None))
            recs.append((chain, t.get("date"), val))
    if recs:
        df = pd.DataFrame(recs, columns=["chain", "ts", "circ"])
        df["ts"] = pd.to_numeric(df["ts"], errors="coerce")
        df["circ"] = pd.to_numeric(df["circ"], errors="coerce")
        df = df.dropna(subset=["ts"])
        df["date_utc"] = to_utc_midnight(pd.to_datetime(df["ts"], unit="s", utc=True))
        wide = df.pivot_table(index="date_utc", columns="chain", values="circ", aggfunc="last").sort_index()
        wide = wide.ffill()  # chain không có bản ghi hôm đó giữ giá trị gần nhất, tránh tổng bị tụt giả
        out = pd.DataFrame({"supply": wide.sum(axis=1, min_count=1)})
        out["supply_eth"] = wide["Ethereum"] if "Ethereum" in wide.columns else float("nan")
    else:  # fallback: mảng tokens ở cấp cao nhất
        toks = js.get("tokens", [])
        df = pd.DataFrame([{"ts": t["date"], "supply": (t.get("circulating") or {}).get("peggedUSD")} for t in toks])
        df["date_utc"] = to_utc_midnight(pd.to_datetime(df["ts"], unit="s", utc=True))
        out = df.set_index("date_utc")[["supply"]]
        out["supply_eth"] = float("nan")
    out.index.name = "date_utc"
    res = out[~out.index.duplicated(keep="last")].sort_index()
    res.attrs["matched"] = matched
    return res


def fetch_llama_tvl() -> pd.DataFrame:
    """TVL toàn thị trường DeFi (tổng các chain), theo ngày."""
    js = requests.get("https://api.llama.fi/v2/historicalChainTvl", headers=UA, timeout=60).json()
    df = pd.DataFrame(js)
    df["date_utc"] = to_utc_midnight(pd.to_datetime(df["date"], unit="s", utc=True))
    out = df.set_index("date_utc")[["tvl"]].astype(float)
    return out[~out.index.duplicated(keep="last")].sort_index()


# ---------------------------------------------------------------- orchestration
class SourceLog:
    """Mỗi lần tải ghi một dòng — chính là khung của Phụ lục B (Source Log)."""

    def __init__(self, redact: list[str] | None = None) -> None:
        self.rows: list[dict] = []
        self.redact = [s for s in (redact or []) if s]

    def run(self, source: str, key: str, endpoint: str, fn, cache_path: Path | None = None,
            refresh: bool = False):
        t0 = dt.datetime.now(dt.timezone.utc)
        status, err, df = "ok", "", None
        try:
            if cache_path is not None and cache_path.exists() and not refresh:
                df = pd.read_csv(cache_path, index_col=0)
                df.index = to_utc_midnight(df.index)
                df.index.name = "date_utc"
                status = "ok_cache"
            else:
                df = fn()
                if cache_path is not None:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    df.to_csv(cache_path)
            if df is None or len(df) == 0:
                status = "empty"
        except Exception as e:  # nguồn lỗi KHÔNG làm sập pipeline; ghi log và đi tiếp
            status, err, df = "error", f"{type(e).__name__}: {e}"[:300], None
            for secret in self.redact:
                err = err.replace(secret, "***")
        n = 0 if df is None else len(df)
        self.rows.append({
            "source": source, "key": key, "endpoint": endpoint, "status": status, "rows": n,
            "first_date": str(df.index.min().date()) if n else "",
            "last_date": str(df.index.max().date()) if n else "",
            "fetched_at_utc": t0.strftime("%Y-%m-%d %H:%M:%S"), "error": err,
            "note": (df.attrs.get("matched", "") if df is not None else ""),
        })
        return df if df is not None else empty_ohlcv()

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows)


def fetch_all(cfg: dict, raw_dir: Path, refresh: bool = False):
    start = cfg["sample"]["start"]
    end = cfg["sample"]["end"] or (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()
    load_env()
    cc_key = os.environ.get(cfg.get("cryptocompare_api_key_env", ""), None)
    if not cc_key:
        print("[CẢNH BÁO] Chưa có CRYPTOCOMPARE_API_KEY (tạo file .env từ .env.example) — CryptoCompare sẽ lỗi 401.")
    log = SourceLog(redact=[cc_key] if cc_key else None)
    raw: dict = {"price": {}, "supply": {}, "start": start, "end": end}

    for coin, c in cfg["coins"].items():
        p = raw["price"][coin] = {}
        p["yahoo"] = log.run("Yahoo Finance", f"{coin}:{c['yahoo']}", "yfinance.download(interval=1d)",
                             lambda c=c: fetch_yahoo(c["yahoo"], start, end),
                             raw_dir / f"yahoo_{coin}.csv", refresh)
        p["cc"] = log.run("CryptoCompare", f"{coin}:{c['cc']}", "min-api.cryptocompare.com/data/v2/histoday",
                          lambda c=c: fetch_cryptocompare(c["cc"], start, end, cc_key),
                          raw_dir / f"cc_{coin}.csv", refresh)
        if c.get("binance"):
            p["binance"] = log.run("Binance", f"{coin}:{c['binance']}", "api.binance.com/api/v3/klines (1d)",
                                   lambda c=c: fetch_binance(c["binance"], start, end),
                                   raw_dir / f"binance_{coin}.csv", refresh)
        else:
            p["binance"] = empty_ohlcv()
        raw["supply"][coin] = log.run("DefiLlama", f"{coin}:{c['llama_symbol']}",
                                      "stablecoins.llama.fi/stablecoin/{id}",
                                      lambda c=c: fetch_llama_supply(c["llama_symbol"], c["llama_name"], c.get("llama_id"), c.get("llama_alt")),
                                      raw_dir / f"llama_supply_{coin}.csv", refresh)

    m = cfg["market"]
    warm = int(m.get("warmup_days", 30))  # tải sớm hơn mẫu để cửa sổ trượt có đủ dữ liệu từ ngày đầu
    wstart = (pd.Timestamp(start) - pd.Timedelta(days=warm)).strftime("%Y-%m-%d")
    raw["btc"] = log.run("Yahoo Finance", f"BTC:{m['btc_yahoo']}", "yfinance.download(interval=1d)",
                         lambda: fetch_yahoo(m["btc_yahoo"], wstart, end), raw_dir / "yahoo_BTC_warm.csv", refresh)
    raw["eth"] = log.run("Yahoo Finance", f"ETH:{m['eth_yahoo']}", "yfinance.download(interval=1d)",
                         lambda: fetch_yahoo(m["eth_yahoo"], wstart, end), raw_dir / "yahoo_ETH_warm.csv", refresh)
    raw["tvl"] = log.run("DefiLlama", "TVL:all_chains", "api.llama.fi/v2/historicalChainTvl",
                         fetch_llama_tvl, raw_dir / "llama_tvl.csv", refresh)
    return raw, log.to_frame()
