"""Tải dữ liệu on-chain từ Dune qua API và lưu vào data/raw/onchain/.

Cần DUNE_API_KEY trong file .env (tạo key miễn phí: dune.com -> Settings -> API). Không có key thì script
BỎ QUA nhẹ nhàng (thoát mã 0) và pipeline vẫn chạy bằng proxy DefiLlama.

Chạy:
    python -m src.data.fetch_onchain                    # Curve 3pool, tách theo năm, có tiếp tục từ chỗ dừng
    python -m src.data.fetch_onchain --with-mintburn    # thêm mint/burn USDC, DAI (không bắt buộc)
    python -m src.data.fetch_onchain --refresh          # tải lại mọi năm

Ghi chú: giao thức API (endpoint, tên trường) viết theo tài liệu Dune mà tác giả nhớ và CHƯA chạy thử với
dịch vụ thật; mọi lỗi HTTP đều được in rõ để chỉnh sửa. Địa chỉ pool/token được tự kiểm tra bằng một query nhỏ.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import time
from pathlib import Path

import pandas as pd
import requests
import yaml

from src.data.build_panel import ROOT
from src.data.fetch import load_env

API = "https://api.dune.com/api/v1"
POOL = "0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7"   # Curve 3pool
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
DAI = "0x6B175474E89094C44Da98b954EedeAC495271d0F"
ZERO = "0x0000000000000000000000000000000000000000"
FIRST_YEAR = 2020  # pool ra đời 2020; cần lịch sử liền mạch vì số dư là tổng tích lũy


class DuneError(RuntimeError):
    pass


class DuneAuthError(DuneError):
    """Key sai hoặc gói không cho phép thao tác (không nên thử lại)."""


# ---------------------------------------------------------------- SQL
def _span(year: int) -> str:
    return f"evt_block_time >= TIMESTAMP '{year}-01-01' AND evt_block_time < TIMESTAMP '{year + 1}-01-01'"


def sql_precheck() -> str:
    """Một ngày duy nhất: pool phải có giao dịch của đúng ba token => địa chỉ pool/token khớp nhau."""
    return f"""
SELECT contract_address, COUNT(*) AS n
FROM erc20_ethereum.evt_Transfer
WHERE evt_block_time >= TIMESTAMP '2021-06-01' AND evt_block_time < TIMESTAMP '2021-06-02'
  AND contract_address IN ({USDT}, {USDC}, {DAI})
  AND ("to" = {POOL} OR "from" = {POOL})
GROUP BY 1""".strip()


def sql_curve(year: int) -> str:
    return f"""
SELECT
  date_trunc('day', evt_block_time) AS day,
  CASE contract_address
    WHEN {USDT} THEN 'USDT'
    WHEN {USDC} THEN 'USDC'
    WHEN {DAI} THEN 'DAI'
  END AS symbol,
  SUM(
    (CASE WHEN "to"   = {POOL} THEN CAST(value AS DOUBLE) ELSE 0 END)
  - (CASE WHEN "from" = {POOL} THEN CAST(value AS DOUBLE) ELSE 0 END)
  ) / POWER(10, CASE WHEN contract_address = {DAI} THEN 18 ELSE 6 END) AS net
FROM erc20_ethereum.evt_Transfer
WHERE contract_address IN ({USDT}, {USDC}, {DAI})
  AND ("to" = {POOL} OR "from" = {POOL})
  AND {_span(year)}
GROUP BY 1, 2
ORDER BY 1, 2""".strip()


def sql_mintburn(year: int) -> str:
    return f"""
SELECT
  date_trunc('day', evt_block_time) AS day,
  CASE contract_address
    WHEN {USDC} THEN 'USDC'
    WHEN {DAI} THEN 'DAI'
  END AS symbol,
  SUM(CASE WHEN "from" = {ZERO} THEN CAST(value AS DOUBLE) ELSE 0 END)
    / POWER(10, CASE WHEN contract_address = {DAI} THEN 18 ELSE 6 END) AS minted,
  SUM(CASE WHEN "to" = {ZERO} THEN CAST(value AS DOUBLE) ELSE 0 END)
    / POWER(10, CASE WHEN contract_address = {DAI} THEN 18 ELSE 6 END) AS burned
FROM erc20_ethereum.evt_Transfer
WHERE contract_address IN ({USDC}, {DAI})
  AND ("from" = {ZERO} OR "to" = {ZERO})
  AND {_span(year)}
GROUP BY 1, 2
ORDER BY 1, 2""".strip()


# ---------------------------------------------------------------- HTTP / Dune API
def _short(r) -> str:
    return (getattr(r, "text", "") or "")[:300].replace("\n", " ")


def _request(method: str, path: str, key: str, *, json=None, params=None) -> dict:
    last = ""
    for attempt in range(6):
        r = requests.request(method, API + path, headers={"X-Dune-API-Key": key},
                             json=json, params=params, timeout=60)
        if r.status_code == 429:  # giới hạn tốc độ: chờ rồi thử lại
            last = "429"
            try:
                wait = float(r.headers.get("Retry-After", 10 * (attempt + 1)))
            except (TypeError, ValueError):
                wait = 10 * (attempt + 1)
            time.sleep(min(wait, 60))
            continue
        if r.status_code in (401, 403):
            raise DuneAuthError(f"HTTP {r.status_code}: key sai hoặc gói Dune hiện tại không cho phép thao tác này ({_short(r)})")
        if r.status_code >= 400:
            raise DuneError(f"HTTP {r.status_code}: {_short(r)}")
        return r.json()
    raise DuneError(f"Bị giới hạn tốc độ ({last}) quá nhiều lần liên tiếp")


def run_sql(sql: str, key: str, timeout_s: int = 1500, poll_s: float = 4.0, page: int = 5000) -> pd.DataFrame:
    """Chạy SQL trên Dune, chờ xong, đọc hết các trang kết quả -> DataFrame."""
    eid = _request("POST", "/sql/execute", key, json={"sql": sql, "performance": "medium"}).get("execution_id")
    if not eid:
        raise DuneError("Dune không trả execution_id (phản hồi bất thường)")
    t0 = time.time()
    while True:
        st = _request("GET", f"/execution/{eid}/status", key)
        state = st.get("state", "")
        if state == "QUERY_STATE_COMPLETED":
            break
        if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED"):
            raise DuneError(f"Query {state}: {str(st.get('error', ''))[:300]}")
        if time.time() - t0 > timeout_s:
            raise DuneError(f"Quá {timeout_s}s vẫn chưa xong (trạng thái {state})")
        time.sleep(poll_s)
    rows, offset = [], 0
    while True:
        js = _request("GET", f"/execution/{eid}/results", key, params={"limit": page, "offset": offset})
        rows += (js.get("result") or {}).get("rows", [])
        nxt = js.get("next_offset")
        if nxt is None:
            break
        offset = nxt
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- điều phối
def precheck(key: str, runner=run_sql) -> bool:
    df = runner(sql_precheck(), key)
    found = set(df["contract_address"].astype(str).str.lower()) if len(df) and "contract_address" in df else set()
    want = {USDT.lower(), USDC.lower(), DAI.lower()}
    if found == want:
        print("  Kiểm tra địa chỉ: OK (pool có giao dịch của đúng USDT, USDC, DAI).")
        return True
    print(f"  Kiểm tra địa chỉ: KHÔNG KHỚP. Thấy {sorted(found) or 'không có gì'}; cần {sorted(want)}.")
    print("  => Địa chỉ pool hoặc token trong src/data/fetch_onchain.py có thể sai. Đối chiếu trên Etherscan rồi sửa.")
    return False


def fetch_years(kind: str, sql_fn, cols: list[str], folder: Path, first_year: int, this_year: int,
                key: str, refresh: bool = False, runner=run_sql) -> list[tuple[int, str]]:
    """Tải từng năm, lưu `<kind>_<năm>.csv`; năm đã có (trừ năm hiện tại) thì bỏ qua. Trả về danh sách (năm, lỗi)."""
    failures: list[tuple[int, str]] = []
    folder.mkdir(parents=True, exist_ok=True)
    for y in range(first_year, this_year + 1):
        out = folder / f"{kind}_{y}.csv"
        if out.exists() and not refresh and y < this_year:
            print(f"  {kind} {y}: đã có, bỏ qua")
            continue
        try:
            t0 = time.time()
            df = runner(sql_fn(y), key)
            miss = [c for c in cols if c not in df.columns]
            if len(df) and miss:
                raise DuneError(f"kết quả thiếu cột {miss}; có {list(df.columns)}")
            if df.empty:
                print(f"  {kind} {y}: Dune trả 0 dòng (không lưu file)")
                continue
            df[cols].to_csv(out, index=False, encoding="utf-8")
            print(f"  {kind} {y}: {len(df)} dòng, {time.time() - t0:.0f}s -> {out.name}")
        except DuneAuthError as e:
            failures.append((y, str(e)))
            print(f"  {kind} {y}: LỖI QUYỀN — dừng ngay. {e}")
            break
        except Exception as e:  # noqa: BLE001 — mỗi năm lỗi độc lập; báo cuối cùng
            failures.append((y, f"{type(e).__name__}: {e}"))
            print(f"  {kind} {y}: LỖI — {type(e).__name__}: {e}")
    return failures


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tải dữ liệu on-chain từ Dune (cần DUNE_API_KEY trong .env)")
    ap.add_argument("--with-mintburn", action="store_true", help="tải thêm mint/burn USDC, DAI (không bắt buộc)")
    ap.add_argument("--refresh", action="store_true", help="tải lại mọi năm")
    ap.add_argument("--skip-precheck", action="store_true", help="bỏ qua bước kiểm tra địa chỉ")
    ap.add_argument("--first-year", type=int, default=FIRST_YEAR)
    args = ap.parse_args(argv)

    load_env()
    key = os.environ.get("DUNE_API_KEY", "").strip()
    if not key:
        print("[BỎ QUA on-chain] Chưa có DUNE_API_KEY trong file .env.\n"
              "  1) Đăng ký miễn phí tại dune.com, vào Settings -> API, tạo key.\n"
              "  2) Thêm dòng  DUNE_API_KEY=key_của_bạn  vào file .env rồi chạy lại.\n"
              "  Không có key thì pipeline vẫn chạy bằng proxy DefiLlama (supply_change, tvl_change).")
        return 0

    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    folder = (ROOT / cfg["onchain"]["curve3pool_csv"]).parent
    this_year = dt.datetime.now(dt.timezone.utc).year

    print(f"Thư mục lưu: {folder}")
    try:
        if not args.skip_precheck and not precheck(key):
            return 1
    except DuneError as e:
        print(f"  Không chạy được query kiểm tra: {e}")
        print("  Nếu lỗi quyền/gói: gói Dune hiện tại có thể không cho chạy SQL qua API -> dùng cách thủ công "
              "(src/data/dune_queries.sql) hoặc bỏ on-chain và dùng proxy DefiLlama.")
        return 1

    print("Tải Curve 3pool (net transfers theo ngày):")
    failures = fetch_years("curve3pool_net_transfers", sql_curve, ["day", "symbol", "net"], folder,
                           args.first_year, this_year, key, args.refresh)
    if args.with_mintburn and not any("QUYỀN" in f[1] or "HTTP 40" in f[1] for f in failures):
        print("Tải mint/burn (Ethereum):")
        failures += fetch_years("mint_burn_eth", sql_mintburn, ["day", "symbol", "minted", "burned"], folder,
                                args.first_year, this_year, key, args.refresh)

    if failures:
        print("\nCÁC NĂM CHƯA TẢI ĐƯỢC (số dư Curve cần đủ lịch sử liền mạch, nên pipeline sẽ BỎ QUA dữ liệu Curve "
              "cho tới khi đủ):")
        for y, e in failures:
            print(f"  - {y}: {e}")
        print("Chạy lại lệnh này để tiếp tục từ chỗ dừng (các năm đã tải được giữ nguyên).")
        return 1

    from src.data import check_onchain  # in báo cáo kiểm tra hợp lý của dữ liệu vừa tải
    print("\n=== Kiểm tra dữ liệu vừa tải ===")
    check_onchain.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
