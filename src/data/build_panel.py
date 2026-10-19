"""Dựng panel.parquet (stablecoin x ngày, UTC) từ dữ liệu thô.

Quy ước point-in-time: hàng (stablecoin, t) chỉ chứa thông tin biết được vào cuối ngày t (UTC).
Nhãn depeg (t+h) do Code 2 tạo bằng shift(-h) trên lưới ngày liên tục mà file này đảm bảo.

Chạy:  python -m src.data.build_panel --config config/config.yaml [--refresh]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from src.data.dictionary import DICTIONARY
from src.data.fetch import empty_ohlcv, fetch_all
from src.data import qc as qcmod

ROOT = Path(__file__).resolve().parents[2]
CURVE_COINS = ("DAI", "USDC", "USDT")  # 3pool: DAI/USDC/USDT
CURVE_LAUNCH_DEADLINE = "2020-12-31"   # dữ liệu Curve phải bắt đầu không muộn hơn mốc này (pool ra đời 2020)
CURVE_MAX_GAP_DAYS = 14                # pool luôn có giao dịch: khoảng trống dài hơn = thiếu dữ liệu


# ---------------------------------------------------------------- on-chain (tùy chọn)
def _num(s: pd.Series) -> pd.Series:
    """Ép về số; chấp nhận dấu phẩy thập phân kiểu Việt Nam ("123,45") khi CSV từng được lưu qua Excel/WPS."""
    if not pd.api.types.is_numeric_dtype(s):  # object hoặc kiểu chuỗi mới của pandas 3 (`str`)
        s = s.astype(str).str.strip().str.replace(",", ".", regex=False)
    return pd.to_numeric(s, errors="coerce")


def scan_onchain_dir(folder: Path) -> dict:
    """Quét mọi *.csv trong thư mục on-chain và nhận diện loại theo TÊN CỘT (không phụ thuộc tên file):
         day, symbol, balance             -> 'balances' (số dư pool theo ngày, từ API của Curve: fetch_curve.py)
         day, symbol, net                 -> 'curve'    (Query Dune 1: chuyển ròng của Curve 3pool)
         day, symbol, minted, burned      -> 'mintburn' (Query Dune 2)
    Tự đoán dấu phân cách (`,` hoặc `;`), bỏ BOM, gộp nhiều file (vd. tách theo năm) và khử trùng (day, symbol).
    Trả về {'curve': df|None, 'mintburn': df|None, 'report': [dict mỗi file]}."""
    kinds: dict = {"balances": [], "curve": [], "mintburn": []}
    report: list[dict] = []
    if not folder.exists():
        return {"balances": None, "curve": None, "mintburn": None, "report": report}
    for f in sorted(folder.glob("*.csv")):
        try:
            df = pd.read_csv(f, sep=None, engine="python", encoding="utf-8-sig")
        except Exception as e:  # file rỗng/hỏng
            report.append({"file": f.name, "kind": "LỖI ĐỌC", "rows": 0, "detail": f"{type(e).__name__}: {e}"[:160]})
            continue
        df.columns = [str(c).strip().lower() for c in df.columns]
        cols = set(df.columns)
        if {"day", "symbol", "balance"} <= cols:
            kind, need = "balances", ["balance"]
        elif {"day", "symbol", "net"} <= cols:
            kind, need = "curve", ["net"]
        elif {"day", "symbol", "minted", "burned"} <= cols:
            kind, need = "mintburn", ["minted", "burned"]
        else:
            report.append({"file": f.name, "kind": "KHÔNG NHẬN DIỆN", "rows": len(df),
                           "detail": f"cột tìm thấy: {list(df.columns)[:8]}; cần day,symbol,balance hoặc day,symbol,net hoặc day,symbol,minted,burned"})
            continue
        df["day"] = pd.to_datetime(df["day"], utc=True, format="mixed", errors="coerce").dt.normalize()
        df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
        for c in need:
            df[c] = _num(df[c])
        n_bad = int(df[["day"] + need].isna().any(axis=1).sum())
        df = df.dropna(subset=["day"] + need)
        kinds[kind].append(df[["day", "symbol"] + need])
        report.append({"file": f.name, "kind": kind, "rows": len(df),
                       "detail": (f"{df['day'].min().date()} -> {df['day'].max().date()}" if len(df) else "rỗng")
                                 + (f"; BỎ {n_bad} dòng lỗi định dạng" if n_bad else "")})
    out: dict = {"report": report}
    for k, parts in kinds.items():
        out[k] = (pd.concat(parts, ignore_index=True).drop_duplicates(subset=["day", "symbol"], keep="last")
                  if parts else None)
    return out


def load_onchain(cfg: dict, root: Path = ROOT) -> dict:
    """Trả về {'curve': day x coin (tỷ trọng - 1/3), 'curve_size': tổng số dư pool, 'mintburn': DataFrame, 'report': ...}.
    Thư mục quét = thư mục chứa `onchain.curve3pool_csv` trong config (mặc định data/raw/onchain/)."""
    folder = (root / cfg["onchain"]["curve3pool_csv"]).parent
    scan = scan_onchain_dir(folder)
    out: dict = {"curve": None, "curve_size": None, "mintburn": scan["mintburn"], "report": scan["report"]}

    # (a) ƯU TIÊN số dư pool trực tiếp (API Curve): không cần lịch sử tích lũy nên không có rủi ro thiếu năm
    bal_df = scan["balances"]
    if bal_df is not None:
        lag = int(cfg["onchain"].get("curve_balances_lag_days", 1))
        wide = bal_df.pivot_table(index="day", columns="symbol", values="balance", aggfunc="last").sort_index()
        missing = [c for c in CURVE_COINS if c not in wide.columns]
        problem = None
        if missing:
            problem = f"thiếu coin {missing}; cần đủ {list(CURVE_COINS)}"
        else:
            wide = wide[list(CURVE_COINS)].dropna(how="any")
            if wide.empty:
                problem = "không có ngày nào đủ cả ba coin"
            elif (wide < 0).any().any():
                problem = "có số dư ÂM: dữ liệu không hợp lệ"
            else:
                med = wide.div(wide.sum(axis=1), axis=0).median()
                if (med < 0.02).any() or (med > 0.98).any():
                    problem = ("đơn vị ba token không đồng nhất (một coin chiếm gần như toàn bộ): có thể số dư thô chưa chia "
                               "số thập phân")
        if problem:
            out["report"].append({"file": "(số dư trực tiếp)", "kind": "BỎ QUA balances", "rows": 0, "detail": problem})
        else:
            grid = pd.date_range(wide.index.min(), wide.index.max(), freq="D", tz="UTC")
            wide = wide.reindex(grid).ffill(limit=3)
            if lag:  # hàng t chỉ dùng số dư gắn nhãn <= t - lag (không rõ mốc cắt của snapshot => bảo thủ)
                wide = wide.reindex(pd.date_range(grid.min(), grid.max() + pd.Timedelta(days=lag), freq="D", tz="UTC"))
                wide = wide.shift(lag)
            tot = wide.sum(axis=1, min_count=len(CURVE_COINS)).replace(0, np.nan)
            out["curve"] = wide.div(tot, axis=0) - 1.0 / len(CURVE_COINS)
            out["curve_size"] = tot
            out["report"].append({"file": "(số dư trực tiếp)", "kind": "dùng balances", "rows": len(wide),
                                  "detail": f"độ trễ {lag} ngày; ưu tiên hơn dữ liệu chuyển ròng của Dune"})
            return out

    # (b) dữ liệu chuyển ròng từ Dune (cần lịch sử liền mạch vì số dư là tổng tích lũy)
    cur = scan["curve"]
    if cur is not None:
        net = cur.pivot_table(index="day", columns="symbol", values="net", aggfunc="sum").sort_index()
        missing = [c for c in CURVE_COINS if c not in net.columns]
        if missing:
            out["report"].append({"file": "(tổng hợp)", "kind": "BỎ QUA curve", "rows": 0,
                                  "detail": f"thiếu coin {missing}; cần đủ {list(CURVE_COINS)} để tính tỷ trọng"})
        else:
            net = net[list(CURVE_COINS)]
            present = net.index.sort_values()
            max_gap = present.to_series().diff().max()
            problem = None
            if present.min() > pd.Timestamp(CURVE_LAUNCH_DEADLINE, tz="UTC"):
                problem = (f"dữ liệu bắt đầu {present.min().date()}, muộn hơn {CURVE_LAUNCH_DEADLINE}: thiếu lịch sử đầu "
                           "nên số dư tích lũy sẽ sai")
            elif pd.notna(max_gap) and max_gap > pd.Timedelta(days=CURVE_MAX_GAP_DAYS):
                problem = f"có khoảng trống {max_gap.days} ngày trong dữ liệu: thiếu một đoạn (một năm?) nên số dư tích lũy sẽ sai"
            net = net.reindex(pd.date_range(net.index.min(), net.index.max(), freq="D", tz="UTC")).fillna(0.0)
            bal = net.cumsum()  # số dư pool = tổng chuyển ròng tích lũy kể từ lúc triển khai
            if problem is None and (bal < -1.0).any().any():
                problem = "có ngày số dư ÂM: thiếu lịch sử hoặc sai địa chỉ pool/token"
            if problem:  # chặn dữ liệu sai thay vì đưa lặng lẽ vào panel
                out["report"].append({"file": "(tổng hợp)", "kind": "BỎ QUA curve", "rows": 0, "detail": problem})
            else:
                tot = bal.sum(axis=1).replace(0, np.nan)
                out["curve"] = bal.div(tot, axis=0) - 1.0 / len(CURVE_COINS)  # lệch so với tỷ trọng đều 1/3
                out["curve_size"] = tot  # tổng số dư pool (coin ~ USD): đánh giá độ tin cậy khi pool teo dần
    return out


# ---------------------------------------------------------------- khối giá
def _price_block(p: dict, last_valid: str | None, binance_fallback: bool = False,
                 start: str | None = None, end: str | None = None) -> pd.DataFrame | None:
    ya = p.get("yahoo", empty_ohlcv())
    cc = p.get("cc", empty_ohlcv())
    bn = p.get("binance", empty_ohlcv())
    prim = ya.dropna(subset=["close"])
    prim = prim[prim["close"] > 0]
    sec = cc.dropna(subset=["close"])
    sec = sec[sec["close"] > 0]
    sec_only = sec.loc[~sec.index.isin(prim.index)]
    parts = [d.assign(price_src=s) for d, s in ((prim, "yahoo"), (sec_only, "cryptocompare")) if len(d)]
    if binance_fallback:  # bậc 3 (chỉ coin bật cờ, vd. UST): Binance quy USDT, KHÔNG phải USD
        bn_ok = bn.dropna(subset=["close"])
        bn_ok = bn_ok[bn_ok["close"] > 0]
        have = prim.index.union(sec.index)
        ter = bn_ok.loc[~bn_ok.index.isin(have)]
        if len(ter):
            parts.append(ter.assign(price_src="binance_usdt"))
    if not parts:
        return None
    price = pd.concat(parts).sort_index()
    if last_valid:
        price = price.loc[: pd.Timestamp(last_valid, tz="UTC")]
    if start:
        price = price.loc[pd.Timestamp(start, tz="UTC"):]
    if end:  # đóng băng ngày cuối: panel không đổi khi chạy lại vào ngày khác
        price = price.loc[: pd.Timestamp(end, tz="UTC")]
    if price.empty:
        return None

    # Sửa High/Low không nhất quán (High < max(O,C) hoặc Low > min(O,C)); đánh dấu hl_fixed
    hi = price[["open", "high", "close"]].max(axis=1)
    lo = price[["open", "low", "close"]].min(axis=1)
    price["hl_fixed"] = (hi != price["high"]) | (lo != price["low"])
    price["high"], price["low"] = hi, lo

    idx = pd.date_range(price.index.min(), price.index.max(), freq="D", tz="UTC", name="timestamp")
    price = price.reindex(idx)
    price["is_missing_price"] = price["close"].isna()
    price["hl_fixed"] = price["hl_fixed"].fillna(False).astype(bool)
    price["cc_close"] = cc["close"].reindex(idx)
    price["cc_high"] = cc["high"].reindex(idx)
    price["cc_low"] = cc["low"].reindex(idx)
    for col in ("close", "high", "low"):
        price[f"bn_{col}"] = bn[col].reindex(idx) if len(bn) else np.nan
    gap = (price["close"] - price["cc_close"]).abs() / price["close"]
    price["src_disagree"] = (gap > 0.005).fillna(False)  # lệch > 0.5% giữa Yahoo và CryptoCompare
    return price


def _lagged_ffill(s: pd.Series, idx: pd.DatetimeIndex, lag: int, limit: int):
    """Đưa chuỗi DefiLlama vào lưới ngày, trễ `lag` ngày (chống look-ahead), ffill có giới hạn."""
    s = s.dropna()
    if lag:
        s = s.shift(lag, freq="D")
    direct = s.reindex(idx)
    full = s.reindex(s.index.union(idx)).ffill(limit=limit).reindex(idx)
    return full, (direct.isna() & full.notna())


def _market_full(df: pd.DataFrame | None, window: int):
    """Log-return và độ biến động trượt của BTC/ETH tính trên TOÀN BỘ chuỗi (không theo lưới từng coin),
    để cùng một ngày mọi coin nhận cùng giá trị và coin niêm yết muộn không mất dữ liệu đầu kỳ."""
    if df is None or not len(df):
        e = pd.Series(dtype=float)
        return e, e
    close = df["close"].astype(float)
    close = close.reindex(pd.date_range(close.index.min(), close.index.max(), freq="D", tz="UTC"))
    ret = np.log(close).diff()
    return ret, ret.rolling(window, min_periods=window).std()  # chỉ dùng quan sát <= t


def _level_change(s: pd.Series, grid_end: pd.Timestamp, lag: int, limit: int):
    """Mức (trễ `lag` ngày, ffill có giới hạn), cờ ffill và % thay đổi ngày — tính trên lưới đầy đủ từ ngày đầu của nguồn."""
    s = s.dropna()
    if s.empty:
        e = pd.Series(dtype=float)
        return e, pd.Series(dtype=bool), e
    grid = pd.date_range(s.index.min(), max(grid_end, s.index.min()), freq="D", tz="UTC")
    level, ffilled = _lagged_ffill(s, grid, lag, limit)
    change = (level / level.shift(1) - 1.0).replace([np.inf, -np.inf], np.nan)  # TVL = 0 những năm đầu => chia 0
    return level, ffilled, change


# ---------------------------------------------------------------- panel
def build_panel(raw: dict, cfg: dict, onchain: dict | None = None) -> pd.DataFrame:
    onchain = onchain or {"curve": None, "mintburn": None}
    lag = int(cfg.get("llama_lag_days", 1))
    lim = int(cfg.get("llama_ffill_limit", 3))
    w = int(cfg["market"]["vol_window"])
    samp = cfg.get("sample", {}) or {}
    pbs = {}
    for coin, c in cfg["coins"].items():
        pb = _price_block(raw["price"].get(coin, {}), c.get("last_valid_date"), bool(c.get("binance_fallback")),
                          samp.get("start"), samp.get("end"))
        if pb is not None:
            pbs[coin] = pb
    if not pbs:
        raise ValueError("Không coin nào có dữ liệu giá trong khoảng mẫu")
    g_end = max(pb.index.max() for pb in pbs.values())
    btc_ret, btc_vol = _market_full(raw.get("btc"), w)
    eth_ret, eth_vol = _market_full(raw.get("eth"), w)
    tvl = raw.get("tvl")
    tvl_level = tvl_chg = None
    if tvl is not None and len(tvl):
        tvl_level, _, tvl_chg = _level_change(tvl["tvl"], g_end, lag, lim)

    frames = []
    for coin, pb in pbs.items():
        c = cfg["coins"][coin]
        idx = pb.index
        d = pd.DataFrame(index=idx)
        d["stablecoin"] = coin
        d["role"] = c["role"]
        d["coin_type"] = c["type"]
        d["price"] = pb["close"]
        d["open"], d["high"], d["low"] = pb["open"], pb["high"], pb["low"]
        d["peg_dev_signed"] = d["price"] - 1.0
        d["peg_deviation"] = d["peg_dev_signed"].abs()
        # Độ lệch theo nguồn thứ hai và độ lệch "đồng thuận" (nhỏ hơn trong hai nguồn): dùng cho robustness của nhãn
        d["peg_deviation_cc"] = (pb["cc_close"] - 1.0).abs()
        d["peg_deviation_min"] = pd.concat([d["peg_deviation"], d["peg_deviation_cc"]], axis=1).min(axis=1)
        d["peg_deviation_hl"] = pd.concat([(d["high"] - 1).abs(), (d["low"] - 1).abs()], axis=1).max(axis=1)
        d["peg_deviation_hl_cc"] = pd.concat([(pb["cc_high"] - 1).abs(), (pb["cc_low"] - 1).abs()], axis=1).max(axis=1)
        d["peg_deviation_hl_min"] = pd.concat([d["peg_deviation_hl"], d["peg_deviation_hl_cc"]], axis=1).min(axis=1)
        d["volume"] = pb["volume"]
        d["range_hl"] = (d["high"] - d["low"]) / d["price"]

        sup = raw["supply"].get(coin)
        if sup is not None and len(sup) and "supply" in sup.columns:
            s_lvl, s_ff, s_chg = _level_change(sup["supply"], idx.max(), lag, lim)
            d["supply"] = s_lvl.reindex(idx)
            d["supply_ffilled"] = s_ff.reindex(idx).fillna(False).astype(bool)
            d["supply_change"] = s_chg.reindex(idx)
        else:
            d["supply"], d["supply_ffilled"], d["supply_change"] = np.nan, False, np.nan
        d["turnover"] = d["volume"] / d["supply"]

        # Proxy on-chain từ DefiLlama (không cần key Dune): supply của coin TRÊN ETHEREUM theo ngày
        d["supply_eth"], d["supply_eth_change"] = np.nan, np.nan
        if sup is not None and "supply_eth" in sup.columns and sup["supply_eth"].notna().any():
            e_lvl, _, e_chg = _level_change(sup["supply_eth"], idx.max(), lag, lim)
            d["supply_eth"] = e_lvl.reindex(idx)
            d["supply_eth_change"] = e_chg.reindex(idx)
        d["supply_eth_share"] = (d["supply_eth"] / d["supply"]).replace([np.inf, -np.inf], np.nan)

        d["tvl"] = tvl_level.reindex(idx) if tvl_level is not None else np.nan
        d["tvl_change"] = tvl_chg.reindex(idx) if tvl_chg is not None else np.nan

        d["btc_ret"], d["btc_vol"] = btc_ret.reindex(idx), btc_vol.reindex(idx)
        d["eth_ret"], d["eth_vol"] = eth_ret.reindex(idx), eth_vol.reindex(idx)

        # on-chain (tùy chọn)
        d["dex_imbalance"] = np.nan
        d["dex_pool_size"] = np.nan
        if onchain.get("curve") is not None and coin in onchain["curve"].columns:
            d["dex_imbalance"] = onchain["curve"][coin].reindex(idx)
            d["dex_pool_size"] = onchain["curve_size"].reindex(idx)
        for col in ("mint_eth", "burn_eth"):
            d[col] = np.nan
        mb = onchain.get("mintburn")
        if mb is not None and coin in set(mb["symbol"]):
            m = mb[mb["symbol"] == coin].set_index("day").sort_index()
            m = m[~m.index.duplicated(keep="last")]
            span = (idx >= m.index.min()) & (idx <= m.index.max())
            for src, dst in (("minted", "mint_eth"), ("burned", "burn_eth")):
                v = m[src].reindex(idx)
                d[dst] = v.where(~span, v.fillna(0.0))  # ngày không có bản ghi trong khoảng phủ = 0
        d["net_issuance_eth"] = d["mint_eth"] - d["burn_eth"]
        d["onchain_available"] = d[["dex_imbalance", "mint_eth"]].notna().any(axis=1)

        for col in ("price_src", "hl_fixed", "is_missing_price", "src_disagree"):
            d[col] = pb[col]
        for col in ("cc_close", "bn_close", "bn_high", "bn_low"):
            d[col] = pb[col]
        frames.append(d.reset_index())

    panel = pd.concat(frames, ignore_index=True)
    cols = [x["column"] for x in DICTIONARY if x["column"] in panel.columns]
    panel = panel[cols].sort_values(["stablecoin", "timestamp"]).reset_index(drop=True)
    return panel


# ---------------------------------------------------------------- CLI
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config" / "config.yaml"))
    ap.add_argument("--refresh", action="store_true", help="tải lại, bỏ qua cache data/raw")
    ap.add_argument("--version", default="v0", help="nhãn phiên bản ghi vào tên file lưu kèm (v0/v1)")
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))

    raw, source_log = fetch_all(cfg, ROOT / "data" / "raw", refresh=args.refresh)
    onchain = load_onchain(cfg)
    panel = build_panel(raw, cfg, onchain)

    out_tab = ROOT / "outputs" / "tables"
    out_tab.mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "processed").mkdir(parents=True, exist_ok=True)
    panel.to_parquet(ROOT / "data" / "processed" / "panel.parquet", index=False)
    panel.to_parquet(ROOT / "data" / "processed" / f"panel_{args.version}.parquet", index=False)

    source_log.to_csv(out_tab / "source_log_raw.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(DICTIONARY).to_csv(out_tab / "data_dictionary.csv", index=False, encoding="utf-8-sig")
    report = qcmod.run_qc(panel, raw, cfg)
    report.to_csv(out_tab / "qc_report.csv", index=False, encoding="utf-8-sig")
    qcmod.coverage(panel).to_csv(out_tab / "coverage_by_coin.csv", index=False, encoding="utf-8-sig")
    for name, tab in qcmod.review_tables(panel).items():
        tab.to_csv(out_tab / f"{name}.csv", index=False, encoding="utf-8-sig")

    print(source_log[["source", "key", "status", "rows", "first_date", "last_date"]].to_string(index=False))
    print("\n", report.to_string(index=False))
    print(f"\npanel: {panel.shape[0]} hàng, {panel['stablecoin'].nunique()} coin -> data/processed/panel.parquet")


if __name__ == "__main__":
    main()
