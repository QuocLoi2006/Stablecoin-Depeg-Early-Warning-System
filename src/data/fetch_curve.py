"""Tải số dư lịch sử của Curve 3pool từ API công khai của Curve (KHÔNG cần key, KHÔNG cần Dune).

Endpoint: GET https://prices.curve.finance/v1/snapshots/ethereum/<pool>/tvl?start=<unix>&end=<unix>&unit=day
(theo tài liệu OpenAPI của Curve: "Get historical TVL, balances and lp price for pool", tối đa 300 bản ghi/lần gọi).

Kết quả: data/raw/onchain/curve3pool_balances.csv (cột day, symbol, balance) -> build_panel tính
dex_imbalance = tỷ trọng - 1/3 và dex_pool_size.

Chạy:
    python -m src.data.fetch_curve --probe     # CHỈ xem phản hồi thật của API (nên chạy trước lần đầu)
    python -m src.data.fetch_curve             # tải toàn bộ 2020-09 -> hôm nay

Ghi chú trung thực: tên trường của phản hồi KHÔNG được xác minh bằng lần gọi thật khi viết code này (chỉ có
tài liệu OpenAPI mô tả đường dẫn). Bộ đọc vì vậy tự dò nhiều dạng phổ biến; nếu không đọc được, nó in
nguyên mẫu JSON để chỉnh code.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path

import pandas as pd
import requests
import yaml

from src.data.build_panel import ROOT

BASE = "https://prices.curve.finance"
CHAIN = "ethereum"
POOL = "0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7"   # Curve 3pool (đã đối chiếu trên Etherscan)
COIN_ORDER = ("DAI", "USDC", "USDT")                   # thứ tự coin trong pool: coins(0)=DAI, (1)=USDC, (2)=USDT
DECIMALS = {"DAI": 1e18, "USDC": 1e6, "USDT": 1e6}
START = dt.date(2020, 9, 1)                            # pool ra đời 2020; trước mốc này không có số dư
CHUNK_DAYS = 250                                       # API trả tối đa 300 bản ghi/lần
UA = {"User-Agent": "Mozilla/5.0 (academic research pipeline, Code 1)"}


class CurveApiError(RuntimeError):
    pass


class SchemaError(CurveApiError):
    """Phản hồi không đúng dạng đã dự đoán (kèm mẫu để chỉnh code)."""


# ---------------------------------------------------------------- HTTP
def _get(path: str, params: dict) -> object:
    url = BASE + path
    last = ""
    for attempt in range(6):
        r = requests.get(url, params=params, headers=UA, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            time.sleep(min(5.0 * (attempt + 1), 30.0))
            continue
        if r.status_code >= 400:
            raise CurveApiError(f"HTTP {r.status_code} cho {path}: {(r.text or '')[:300]}")
        try:
            return r.json()
        except ValueError as e:
            raise CurveApiError(f"phản hồi không phải JSON: {(r.text or '')[:200]}") from e
    raise CurveApiError(f"API lỗi/giới hạn tốc độ nhiều lần liên tiếp ({last})")


# ---------------------------------------------------------------- đọc phản hồi (chịu nhiều dạng)
def _records(js) -> list:
    if isinstance(js, list):
        return js
    if isinstance(js, dict):
        for k in ("data", "snapshots", "result", "results", "items", "history"):
            v = js.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                inner = _records(v)
                if inner:
                    return inner
        for v in js.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def _to_ts(v) -> pd.Timestamp:
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().replace(".", "", 1).isdigit()):
        x = float(v)
        return pd.to_datetime(x / 1000.0 if x > 1e11 else x, unit="s", utc=True)
    return pd.to_datetime(v, utc=True)


def _ts_of(rec: dict) -> pd.Timestamp:
    for k in ("timestamp", "time", "date", "dt", "ts", "t", "block_timestamp"):
        if k in rec and rec[k] is not None:
            return _to_ts(rec[k])
    raise SchemaError(f"bản ghi không có trường thời gian; các trường: {list(rec)}")


def _balances_of(rec: dict) -> dict:
    raw = None
    for k in ("balances", "coin_balances", "reserves", "token_balances"):
        if rec.get(k) is not None:
            raw = rec[k]
            break
    if raw is None:
        raise SchemaError(f"bản ghi không có trường số dư; các trường: {list(rec)}")
    out: dict = {}
    if isinstance(raw, dict):
        items = list(raw.items())
        if all(str(k).isdigit() for k, _ in items):          # {"0": .., "1": .., "2": ..}
            items = [(COIN_ORDER[int(k)], v) for k, v in items if int(k) < len(COIN_ORDER)]
        for k, v in items:
            out[str(k).upper()] = float(v)
    elif isinstance(raw, list):
        if raw and isinstance(raw[0], dict):                 # [{"symbol": "DAI", "balance": ..}, ...]
            for i, e in enumerate(raw):
                sym = next((str(e[k]).upper() for k in ("symbol", "token", "coin", "name") if k in e), None)
                amt = next((e[k] for k in ("balance", "amount", "value", "balance_raw") if k in e), None)
                if amt is None:
                    raise SchemaError(f"phần tử số dư không có trường số lượng: {e}")
                out[sym or COIN_ORDER[i]] = float(amt)
        else:                                                # [x0, x1, x2] theo thứ tự coin của pool
            if len(raw) != len(COIN_ORDER):
                raise SchemaError(f"cần {len(COIN_ORDER)} số dư theo thứ tự {COIN_ORDER}, nhận {len(raw)}: {raw}")
            for sym, v in zip(COIN_ORDER, raw):
                out[sym] = float(v)
    else:
        raise SchemaError(f"trường số dư có kiểu lạ: {type(raw).__name__}")
    miss = [c for c in COIN_ORDER if c not in out]
    if miss:
        raise SchemaError(f"thiếu số dư của {miss}; có {list(out)}")
    return {c: out[c] for c in COIN_ORDER}


def parse_snapshots(js) -> pd.DataFrame:
    """Phản hồi -> DataFrame dài (day, symbol, balance); mỗi ngày giữ bản ghi cuối; tự nhận diện số dư thô."""
    recs = _records(js)
    if not recs:
        raise SchemaError(f"không tìm thấy danh sách bản ghi trong phản hồi: {json.dumps(js)[:400]}")
    rows = []
    for rec in recs:
        if not isinstance(rec, dict):
            raise SchemaError(f"bản ghi không phải đối tượng JSON: {str(rec)[:200]}")
        rows.append((_ts_of(rec), _balances_of(rec)))
    if max(v for _, b in rows for v in b.values()) > 1e13:   # số dư thô (wei): chia theo số thập phân của từng token
        rows = [(t, {c: v / DECIMALS[c] for c, v in b.items()}) for t, b in rows]
    rows.sort(key=lambda x: x[0])
    long = pd.DataFrame([{"ts": t, "symbol": c, "balance": v} for t, b in rows for c, v in b.items()])
    long["day"] = long["ts"].dt.tz_convert("UTC").dt.normalize()
    long = long.sort_values("ts").groupby(["day", "symbol"], as_index=False)["balance"].last()
    return long[["day", "symbol", "balance"]]


# ---------------------------------------------------------------- tải
def fetch_range(start: dt.date, end: dt.date, unit: str = "day") -> pd.DataFrame:
    parts, cur = [], start
    while cur <= end:
        stop = min(cur + dt.timedelta(days=CHUNK_DAYS - 1), end)
        s_ts = int(dt.datetime(cur.year, cur.month, cur.day, tzinfo=dt.timezone.utc).timestamp())
        e_ts = int(dt.datetime(stop.year, stop.month, stop.day, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp())
        js = _get(f"/v1/snapshots/{CHAIN}/{POOL}/tvl", {"start": s_ts, "end": e_ts, "unit": unit})
        if _records(js):
            parts.append(parse_snapshots(js))
            print(f"  {cur} -> {stop}: {parts[-1]['day'].nunique()} ngày")
        else:
            print(f"  {cur} -> {stop}: không có bản ghi")
        cur = stop + dt.timedelta(days=1)
        time.sleep(0.5)  # lịch sự với API công khai
    if not parts:
        raise CurveApiError("không nhận được bản ghi nào trong toàn bộ khoảng thời gian")
    out = pd.concat(parts, ignore_index=True).drop_duplicates(["day", "symbol"], keep="last")
    return out.sort_values(["day", "symbol"]).reset_index(drop=True)


def probe() -> int:
    """Gọi một cửa sổ nhỏ quanh USDC depeg (03/2023) và in phản hồi thật để kiểm tra cấu trúc."""
    s = int(dt.datetime(2023, 3, 8, tzinfo=dt.timezone.utc).timestamp())
    e = int(dt.datetime(2023, 3, 14, tzinfo=dt.timezone.utc).timestamp())
    try:
        js = _get(f"/v1/snapshots/{CHAIN}/{POOL}/tvl", {"start": s, "end": e, "unit": "day"})
    except CurveApiError as err:
        print(f"LỖI khi gọi API: {err}")
        return 1
    print("=== PHẢN HỒI THÔ (rút gọn 1800 ký tự đầu) ===")
    print(json.dumps(js, ensure_ascii=False)[:1800])
    try:
        df = parse_snapshots(js)
    except CurveApiError as err:
        print(f"\n=== KHÔNG ĐỌC ĐƯỢC: {err}\nHãy gửi nguyên đoạn JSON ở trên để chỉnh bộ đọc.")
        return 1
    wide = df.pivot(index="day", columns="symbol", values="balance")
    share = wide.div(wide.sum(axis=1), axis=0).round(3)
    print("\n=== ĐỌC ĐƯỢC. Số dư (đơn vị coin) ===")
    print(wide.round(0).to_string())
    print("\n=== Tỷ trọng (kỳ vọng: USDC tăng rõ quanh 10-11/03/2023 khi USDC mất neo) ===")
    print(share.to_string())
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tải số dư lịch sử Curve 3pool từ API công khai của Curve (không cần key)")
    ap.add_argument("--probe", action="store_true", help="chỉ gọi thử một cửa sổ nhỏ và in phản hồi thật")
    ap.add_argument("--start", default=START.isoformat())
    ap.add_argument("--end", default=None, help="mặc định: hôm qua (UTC)")
    args = ap.parse_args(argv)
    if args.probe:
        return probe()

    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    out_path = (ROOT / cfg["onchain"]["curve3pool_csv"]).parent / "curve3pool_balances.csv"
    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end) if args.end else dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    print(f"Tải số dư Curve 3pool {start} -> {end} (API công khai, không cần key):")
    try:
        df = fetch_range(start, end)
    except CurveApiError as err:
        print(f"\nLỖI: {err}")
        print("Gợi ý: chạy  python -m src.data.fetch_curve --probe  để xem phản hồi thật rồi gửi cho người hỗ trợ.")
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df["day"] = df["day"].dt.strftime("%Y-%m-%d 00:00:00.000 UTC")
    df.to_csv(out_path, index=False, encoding="utf-8")
    days = df["day"].nunique()
    print(f"\nĐã lưu {days} ngày x 3 coin -> {out_path}")
    from src.data import check_onchain  # in báo cáo kiểm tra hợp lý
    print("\n=== Kiểm tra dữ liệu vừa tải ===")
    check_onchain.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
