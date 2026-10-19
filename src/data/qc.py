"""Kiểm tra chất lượng panel. Trả về bảng (check, status, detail) để dán vào Phụ lục B/E."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd


def coverage(panel: pd.DataFrame) -> pd.DataFrame:
    g = panel.groupby("stablecoin")
    out = g.agg(first_date=("timestamp", "min"), last_date=("timestamp", "max"), n_rows=("timestamp", "size"),
                n_missing_price=("is_missing_price", "sum"), n_src_disagree=("src_disagree", "sum"),
                n_hl_fixed=("hl_fixed", "sum"), n_supply_nan=("supply", lambda s: int(s.isna().sum())),
                n_onchain=("onchain_available", "sum"),
                max_peg_dev=("peg_deviation", "max"), max_peg_dev_hl=("peg_deviation_hl", "max")).reset_index()
    return out


def review_tables(panel: pd.DataFrame) -> dict:
    """Hai bảng để rà tay: ngày lệch nguồn và ngày giá cách xa 1 USD."""
    d = panel[panel["src_disagree"]].copy()
    d["gap_vs_cc"] = (d["price"] - d["cc_close"]).abs() / d["price"]
    d = d.sort_values("gap_vs_cc", ascending=False)[
        ["stablecoin", "timestamp", "price", "cc_close", "gap_vs_cc", "high", "low", "volume", "price_src"]]
    pn = panel[~panel["is_missing_price"]]
    far = pn[(pn["price"] < 0.5) | (pn["price"] > 1.5)][
        ["stablecoin", "timestamp", "price", "high", "low", "peg_deviation", "price_src", "bn_close"]]
    return {"review_src_disagree_days": d, "review_price_outliers": far}


def alignment_report(raw: dict, lags=(-1, 0, 1), min_overlap: int = 60) -> pd.DataFrame:
    """So Yahoo với CryptoCompare ở các độ lệch ngày để phát hiện lệch quy ước nến (00:00 UTC mở hay đóng)."""
    rows = []
    for coin, p in raw["price"].items():
        ya, cc = p.get("yahoo"), p.get("cc")
        if ya is None or cc is None or len(ya) == 0 or len(cc) == 0:
            continue
        errs = {}
        for lag in lags:
            a = ya["close"]
            b = cc["close"].shift(lag, freq="D")
            j = pd.concat([a, b], axis=1, join="inner").dropna()
            if len(j) >= min_overlap:
                errs[lag] = float((j.iloc[:, 0] - j.iloc[:, 1]).abs().mean())
        if errs:
            best = min(errs, key=errs.get)
            rows.append({"stablecoin": coin, "best_lag_days": best, **{f"mae_lag{k}": v for k, v in errs.items()}})
    return pd.DataFrame(rows)


def run_qc(panel: pd.DataFrame, raw: dict | None, cfg: dict) -> pd.DataFrame:
    res = []

    def add(name, ok, detail=""):
        res.append({"check": name, "status": "PASS" if ok else "FAIL", "detail": detail})

    ts = panel["timestamp"]
    add("khóa (stablecoin, timestamp) duy nhất", not panel.duplicated(["stablecoin", "timestamp"]).any())
    add("timestamp có múi giờ UTC", str(ts.dt.tz) == "UTC", str(ts.dt.tz))
    add("timestamp đúng 00:00 UTC", bool((ts == ts.dt.normalize()).all()))
    today = pd.Timestamp(dt.datetime.now(dt.timezone.utc).date(), tz="UTC")
    add("không có ngày chưa đóng (>= hôm nay UTC)", bool((ts < today).all()), f"max={ts.max()}")

    contiguous = True
    for coin, g in panel.groupby("stablecoin"):
        d = g["timestamp"].sort_values().diff().dropna()
        contiguous &= bool((d == pd.Timedelta(days=1)).all())
    add("lưới ngày liên tục trong từng coin (cần cho shift(-h) của nhãn)", contiguous)

    pn = panel.loc[~panel["is_missing_price"]]
    broken = pn[(pn["price"] <= 0) | (pn["price"] > 5)]
    add("giá dương và <= 5 USD (ngoài khoảng = lỗi dữ liệu)", broken.empty, f"{len(broken)} quan sát")
    far = pn[(pn["price"] < 0.5) | (pn["price"] > 1.5)]
    listing = "; ".join(f"{r.stablecoin} {r.timestamp.date()} {r.price:.4f} ({r.price_src})"
                        for r in far.head(10).itertuples())
    res.append({"check": "giá ngoài [0.5, 1.5] (thường là depeg thật, cần xem tay và ghi vào bảng sự kiện)",
                "status": "WARN" if len(far) else "PASS", "detail": f"{len(far)} quan sát: {listing}" if len(far) else ""})
    add("high >= low", bool((panel["high"].dropna() >= panel["low"].dropna()).all()))
    add("peg_deviation >= 0", bool((panel["peg_deviation"].dropna() >= 0).all()))
    mk = ["btc_ret", "btc_vol", "eth_ret", "eth_vol", "tvl", "tvl_change"]
    by_day = panel.groupby("timestamp")[mk]
    n_diff = int((by_day.nunique(dropna=True) > 1).any(axis=1).sum())
    n_mixed = int(by_day.agg(lambda s: bool(s.isna().any() and s.notna().any())).any(axis=1).sum())
    add("biến cấp thị trường (BTC/ETH, TVL) giống hệt nhau giữa các coin trong cùng ngày", n_diff == 0 and n_mixed == 0,
        f"{n_diff} ngày khác giá trị, {n_mixed} ngày NaN lẫn không NaN")
    sup = panel["supply"].dropna()
    add("supply không âm", bool((sup >= 0).all()))

    n_cmp = int(panel["cc_close"].notna().sum())
    n_dis = int(panel["src_disagree"].sum())
    if n_cmp == 0:
        res.append({"check": "Yahoo vs CryptoCompare lệch > 0.5%", "status": "WARN",
                    "detail": "KHÔNG có dữ liệu CryptoCompare nên chưa đối chiếu được (kiểm tra API key)"})
    else:
        by = panel[panel["src_disagree"]].groupby("stablecoin").size().to_dict()
        res.append({"check": "Yahoo vs CryptoCompare lệch > 0.5%", "status": "INFO" if n_dis else "PASS",
                    "detail": f"{n_dis}/{n_cmp} ngày so sánh được; theo coin: {by} — xem review_src_disagree_days.csv"})
    no_cc = [c for c, g in panel.groupby("stablecoin") if g["cc_close"].notna().sum() == 0]
    res.append({"check": "mọi coin đều có đối chiếu độc lập với CryptoCompare", "status": "PASS" if not no_cc else "WARN",
                "detail": ", ".join(no_cc)})

    stale = []
    for coin, g in panel.groupby("stablecoin"):
        pr = g["price"]
        run = int(pr.groupby((pr != pr.shift()).cumsum()).size().max()) if pr.notna().any() else 0
        zv = float((g["volume"] == 0).mean())
        if run >= 20 or zv > 0.2:
            stale.append(f"{coin}: giá đứng yên tối đa {run} ngày, {zv:.0%} ngày volume=0")
    res.append({"check": "không nghi dữ liệu cũ/đứng giá (giá giống nhau >= 20 ngày hoặc volume=0 > 20%)",
                "status": "PASS" if not stale else "WARN", "detail": "; ".join(stale)})
    src_counts = panel["price_src"].value_counts().to_dict()
    res.append({"check": "cơ cấu nguồn giá (price_src)", "status": "INFO", "detail": str(src_counts)})
    res.append({"check": "tỷ lệ hàng có biến on-chain trực tiếp", "status": "INFO",
                "detail": f"{panel['onchain_available'].mean():.1%} (phần còn lại dùng proxy DefiLlama)"})

    if raw is not None:
        al = alignment_report(raw)
        if len(al):
            bad = al[al["best_lag_days"] != 0]
            add("quy ước nến Yahoo và CryptoCompare khớp nhau (lag tối ưu = 0)", bad.empty,
                "; ".join(f"{r.stablecoin}: lag {r.best_lag_days}" for r in bad.itertuples()) or "tất cả lag 0")
    return pd.DataFrame(res)
