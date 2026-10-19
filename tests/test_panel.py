"""Test panel bằng dữ liệu giả (không cần mạng). Chạy: pytest -q"""
import copy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.data.build_panel import build_panel
from src.data.fetch import empty_ohlcv
from src.data.qc import run_qc

CFG = {
    "llama_lag_days": 1, "llama_ffill_limit": 3,
    "market": {"vol_window": 7},
    "coins": {
        "USDC": {"role": "core", "type": "fiat_backed"},
        "DAI": {"role": "core", "type": "crypto_backed"},
        "UST": {"role": "case_study", "type": "algorithmic", "last_valid_date": "2020-06-30"},
    },
}


def _ohlc(idx, base=1.0, seed=0, vol=0.001):
    rng = np.random.default_rng(seed)
    c = base + rng.normal(0, vol, len(idx))
    df = pd.DataFrame({"open": c + rng.normal(0, vol / 2, len(idx)), "close": c}, index=idx)
    df["high"] = df[["open", "close"]].max(axis=1) + 0.0005
    df["low"] = df[["open", "close"]].min(axis=1) - 0.0005
    df["volume"] = rng.uniform(1e6, 5e6, len(idx))
    df.index.name = "date_utc"
    return df[["open", "high", "low", "close", "volume"]]


@pytest.fixture()
def raw():
    idx = pd.date_range("2020-01-01", "2020-09-30", freq="D", tz="UTC")
    r = {"price": {}, "supply": {}}
    for i, coin in enumerate(CFG["coins"]):
        full = _ohlc(idx, seed=i)
        ya = full.drop(full.index[[40, 41]])        # khoảng trống Yahoo => CryptoCompare lấp
        cc = full.copy()                            # nguồn thứ hai gần giống nguồn thứ nhất, như dữ liệu thật
        cc["close"] = cc["close"] + 1e-5
        r["price"][coin] = {"yahoo": ya, "cc": cc, "binance": empty_ohlcv()}
        r["supply"][coin] = pd.DataFrame({"supply": np.linspace(1e9, 2e9, len(idx)),
                                          "supply_eth": np.nan}, index=idx)
    r["btc"] = _ohlc(idx, base=9000, seed=7, vol=100)
    r["eth"] = _ohlc(idx, base=200, seed=8, vol=5)
    r["tvl"] = pd.DataFrame({"tvl": np.linspace(1e9, 5e10, len(idx))}, index=idx)
    return r


def test_structure(raw):
    p = build_panel(raw, CFG)
    assert not p.duplicated(["stablecoin", "timestamp"]).any()
    assert str(p["timestamp"].dt.tz) == "UTC"
    assert (p["peg_deviation"].dropna() == (p["price"] - 1).abs().dropna()).all()
    assert (p["high"] >= p["low"]).all()
    assert p.loc[p.stablecoin == "UST", "timestamp"].max() == pd.Timestamp("2020-06-30", tz="UTC")
    # Yahoo thiếu 2 ngày => CryptoCompare lấp, không có hàng NaN giá
    assert not p["is_missing_price"].any()
    assert (p["price_src"] == "cryptocompare").sum() == 2 * 3


def test_contiguous_and_qc(raw):
    p = build_panel(raw, CFG)
    rep = run_qc(p, raw, CFG)
    failed = rep[rep.status == "FAIL"]
    assert failed.empty, failed.to_string()


def test_supply_is_lagged(raw):
    p = build_panel(raw, CFG)
    coin = p[p.stablecoin == "USDC"].set_index("timestamp")
    t = pd.Timestamp("2020-05-10", tz="UTC")
    src = raw["supply"]["USDC"]["supply"]
    assert coin.loc[t, "supply"] == pytest.approx(src.loc[t - pd.Timedelta(days=1)])


def test_no_lookahead_perturbation(raw):
    """Đổi toàn bộ dữ liệu SAU ngày T không được làm thay đổi bất kỳ giá trị nào tại hoặc trước T."""
    T = pd.Timestamp("2020-07-15", tz="UTC")
    base = build_panel(raw, CFG)
    pert = copy.deepcopy(raw)
    after = lambda df: df.index > T  # noqa: E731
    for coin in CFG["coins"]:
        for src in ("yahoo", "cc"):
            df = pert["price"][coin][src]
            df.loc[after(df), ["open", "high", "low", "close"]] *= 1.37
            df.loc[after(df), "volume"] *= 3
        s = pert["supply"][coin]
        s.loc[after(s), "supply"] *= 2.5
    for k in ("btc", "eth"):
        pert[k].loc[after(pert[k]), ["open", "high", "low", "close"]] *= 1.9
    pert["tvl"].loc[after(pert["tvl"]), "tvl"] *= 4
    new = build_panel(pert, CFG)

    a = base[base.timestamp <= T].reset_index(drop=True)
    b = new[new.timestamp <= T].reset_index(drop=True)
    num = a.select_dtypes("number").columns
    pd.testing.assert_frame_equal(a[num], b[num], check_exact=False, rtol=1e-12)


def test_binance_fallback_and_qc_warn(raw):
    """Coin không có Yahoo/CryptoCompare nhưng bật binance_fallback => dùng Binance, gắn nhãn nguồn; QC cảnh báo thiếu CC."""
    idx = raw["price"]["UST"]["yahoo"].index
    bn = _ohlc(idx, seed=99)
    raw["price"]["UST"] = {"yahoo": empty_ohlcv(), "cc": empty_ohlcv(), "binance": bn}
    cfg = copy.deepcopy(CFG)
    cfg["coins"]["UST"]["binance_fallback"] = True
    p = build_panel(raw, cfg)
    u = p[p.stablecoin == "UST"]
    assert len(u) > 0
    assert (u.loc[~u["is_missing_price"], "price_src"] == "binance_usdt").all()
    assert u["is_missing_price"].sum() == 2          # 2 ngày thiếu được giữ lại để lưới ngày liên tục
    rep = run_qc(p, raw, cfg)
    assert rep[rep.status == "FAIL"].empty
    assert (rep.status == "WARN").any()           # UST không có đối chiếu CryptoCompare
    # không bật cờ => coin bị loại khỏi panel thay vì dùng giá sai nguồn
    cfg2 = copy.deepcopy(CFG)
    assert "UST" not in set(build_panel(raw, cfg2)["stablecoin"])


def test_stale_price_warning(raw):
    p = build_panel(raw, CFG)
    p.loc[p.stablecoin == "DAI", "price"] = 1.0       # giả lập giá đứng yên như coin đã ngừng giao dịch
    rep = run_qc(p, raw, CFG)
    row = rep[rep.check.str.contains("dữ liệu cũ")].iloc[0]
    assert row.status == "WARN" and "DAI" in row.detail


def test_real_depeg_is_warn_not_fail(raw):
    p = build_panel(raw, CFG)
    i = p.index[(p.stablecoin == "USDC")][100]
    p.loc[i, "price"] = 0.30                      # depeg rất sâu (kiểu UST) nhưng là giá hợp lệ
    rep = run_qc(p, raw, CFG)
    assert rep[rep.status == "FAIL"].empty
    assert (rep[rep.check.str.contains("thường là depeg thật")].status == "WARN").all()
    from src.data.qc import review_tables
    assert len(review_tables(p)["review_price_outliers"]) == 1


def test_consensus_deviation(raw):
    p = build_panel(raw, CFG)
    both = p.dropna(subset=["peg_deviation_cc"])
    assert len(both) > 0
    assert (both["peg_deviation_min"] <= both["peg_deviation"] + 1e-15).all()
    assert (both["peg_deviation_min"] <= both["peg_deviation_cc"] + 1e-15).all()
    only_one = p[p["peg_deviation_cc"].isna() & p["peg_deviation"].notna()]
    assert (only_one["peg_deviation_min"] == only_one["peg_deviation"]).all()


def test_consensus_hl(raw):
    p = build_panel(raw, CFG)
    both = p.dropna(subset=["peg_deviation_hl_cc"])
    assert len(both) > 0
    assert (both["peg_deviation_hl_min"] <= both["peg_deviation_hl"] + 1e-15).all()
    assert (both["peg_deviation_hl_min"] <= both["peg_deviation_hl_cc"] + 1e-15).all()


def test_env_loader_and_secret_hygiene(tmp_path, monkeypatch):
    from src.data import fetch
    envf = tmp_path / ".env"
    envf.write_bytes("\ufeff# ghi chú\nexport CC_TEST_KEY = \"abc123\"\nEMPTY=\nCC_TEST_PRE=file\n".encode("utf-8"))
    monkeypatch.delenv("CC_TEST_KEY", raising=False)
    monkeypatch.setenv("CC_TEST_PRE", "terminal")             # biến terminal phải thắng file
    fetch.load_env(envf)
    import os
    assert os.environ["CC_TEST_KEY"] == "abc123"
    assert os.environ["CC_TEST_PRE"] == "terminal"
    monkeypatch.delenv("CC_TEST_KEY", raising=False)

    seen = {}
    class R:
        def raise_for_status(self): pass
        def json(self): return {"Data": {"Data": []}}
    def fake_get(url, params=None, headers=None, timeout=None):
        seen["params"], seen["headers"] = params, headers
        return R()
    monkeypatch.setattr(fetch.requests, "get", fake_get)
    fetch.fetch_cryptocompare("USDC", "2020-01-01", "2020-01-10", "SECRET123")
    assert "api_key" not in seen["params"] and "SECRET123" not in str(seen["params"])
    assert seen["headers"]["authorization"] == "Apikey SECRET123"

    log = fetch.SourceLog(redact=["SECRET123"])
    def boom(): raise RuntimeError("401 for url https://x?api_key=SECRET123")
    log.run("CryptoCompare", "t", "e", boom)
    assert "SECRET123" not in log.to_frame().loc[0, "error"]


def test_market_vars_consistent_across_coins_and_late_start(raw):
    """Coin niêm yết muộn vẫn có btc_vol ngay ngày đầu; cùng ngày mọi coin cùng giá trị biến thị trường."""
    long_idx = pd.date_range("2019-11-01", "2020-09-30", freq="D", tz="UTC")
    raw["btc"] = _ohlc(long_idx, base=9000, seed=7, vol=100)       # có giai đoạn khởi động trước mẫu
    raw["eth"] = _ohlc(long_idx, base=200, seed=8, vol=5)
    late = pd.Timestamp("2020-06-01", tz="UTC")
    for src in ("yahoo", "cc"):
        df = raw["price"]["DAI"][src]
        raw["price"]["DAI"][src] = df[df.index >= late]
    p = build_panel(raw, CFG)
    first = p[(p.stablecoin == "DAI")].sort_values("timestamp").iloc[0]
    assert first["timestamp"] == late
    assert pd.notna(first["btc_vol"]) and pd.notna(first["eth_vol"]) and pd.notna(first["tvl_change"])
    assert pd.notna(first["supply_change"])                         # supply của ngày trước đó có trong nguồn
    by_day = p.groupby("timestamp")[["btc_vol", "eth_vol", "btc_ret", "tvl", "tvl_change"]].nunique(dropna=True)
    assert (by_day <= 1).all().all()
    rep = run_qc(p, raw, CFG)
    row = rep[rep.check.str.contains("cấp thị trường")].iloc[0]
    assert row.status == "PASS", row.detail


def test_sample_end_freeze_and_tvl_zero_safe(raw):
    cfg = copy.deepcopy(CFG)
    cfg["sample"] = {"start": "2020-02-01", "end": "2020-08-31"}
    raw["tvl"].iloc[:60, 0] = 0.0                                     # TVL = 0 những ngày đầu như dữ liệu DefiLlama thật
    p = build_panel(raw, cfg)
    assert p["timestamp"].min() >= pd.Timestamp("2020-02-01", tz="UTC")
    assert p["timestamp"].max() <= pd.Timestamp("2020-08-31", tz="UTC")
    assert not np.isinf(p["tvl_change"].dropna()).any()


def test_onchain_loader_multi_files_and_panel(raw, tmp_path):
    from src.data.build_panel import load_onchain
    d = tmp_path / "data" / "raw" / "onchain"
    d.mkdir(parents=True)
    rng = np.random.default_rng(3)
    days = pd.date_range("2020-02-01", "2020-09-30", freq="D")
    def rows(ds):
        return [{"day": x.strftime("%Y-%m-%d 00:00:00.000 UTC"), "symbol": s, "net": float(rng.normal(1e5, 2e4))}
                for x in ds for s in ("DAI", "USDC", "USDT")]
    pd.DataFrame(rows(days[:100])).to_csv(d / "curve3pool_net_transfers_2020a.csv", index=False)
    pd.DataFrame(rows(days[90:])).to_csv(d / "curve3pool_net_transfers_2020b.csv", index=False)  # 10 ngày chồng lấn
    cfg = copy.deepcopy(CFG)
    cfg["onchain"] = {"curve3pool_csv": "data/raw/onchain/curve3pool_net_transfers.csv",
                      "mintburn_csv": "data/raw/onchain/mint_burn_eth.csv"}
    oc = load_onchain(cfg, tmp_path)
    assert oc["curve"] is not None and oc["mintburn"] is None
    assert abs(float(oc["curve"].iloc[-1].sum())) < 1e-9          # độ lệch ba coin cộng lại bằng 0
    p = build_panel(raw, cfg, oc)
    day = pd.Timestamp("2020-06-15", tz="UTC")
    x = p[p.timestamp == day].set_index("stablecoin")
    assert x.loc[["USDC", "DAI"], "dex_imbalance"].notna().all()
    assert x.loc[["USDC", "DAI"], "dex_pool_size"].gt(0).all()
    assert pd.isna(x.loc["UST", "dex_imbalance"]) and pd.isna(x.loc["UST", "dex_pool_size"])
    assert (p.loc[p.stablecoin == "UST", "onchain_available"] == False).all()  # noqa: E712


def _dune_rows(days, rng, extra=None):
    return [{"day": x.strftime("%Y-%m-%d 00:00:00.000 UTC"), "symbol": s, "net": float(rng.normal(1e5, 2e4))}
            for x in days for s in ("DAI", "USDC", "USDT")]


def test_onchain_filename_agnostic_and_formats(tmp_path):
    """Nhận diện theo cột: tên file tùy ý, dấu ';' + phẩy thập phân + BOM, file lạ được báo cáo chứ không làm hỏng."""
    from src.data.build_panel import load_onchain
    d = tmp_path / "data" / "raw" / "onchain"
    d.mkdir(parents=True)
    rng = np.random.default_rng(5)
    days = pd.date_range("2020-03-01", "2020-06-30", freq="D")
    pd.DataFrame(_dune_rows(days[:60], rng)).to_csv(d / "query_5738291.csv", index=False)              # tên Dune mặc định
    # file thứ hai: lưu qua Excel/WPS kiểu Việt Nam (dấu ; và dấu phẩy thập phân) + BOM
    rows = _dune_rows(days[50:], rng)
    txt = "day;symbol;net\n" + "\n".join(f"{r['day']};{r['symbol']};{str(r['net']).replace('.', ',')}" for r in rows)
    (d / "curve3pool_net_transfers (1).csv").write_bytes(("\ufeff" + txt).encode("utf-8"))
    pd.DataFrame({"foo": [1], "bar": [2]}).to_csv(d / "linh_tinh.csv", index=False)                   # file không liên quan
    (d / "ghi_chu.txt").write_text("không phải csv", encoding="utf-8")                                                    # bị bỏ qua
    cfg = {"onchain": {"curve3pool_csv": "data/raw/onchain/curve3pool_net_transfers.csv",
                       "mintburn_csv": "data/raw/onchain/mint_burn_eth.csv"}}
    oc = load_onchain(cfg, tmp_path)
    assert oc["curve"] is not None and list(oc["curve"].columns) == ["DAI", "USDC", "USDT"]
    assert oc["curve"].index.min() == pd.Timestamp("2020-03-01", tz="UTC")
    assert oc["curve"].index.max() == pd.Timestamp("2020-06-30", tz="UTC")
    assert oc["curve_size"].gt(0).all()
    kinds = {r["file"]: r["kind"] for r in oc["report"]}
    assert kinds["query_5738291.csv"] == "curve" and kinds["curve3pool_net_transfers (1).csv"] == "curve"
    assert kinds["linh_tinh.csv"] == "KHÔNG NHẬN DIỆN" and "ghi_chu.txt" not in kinds


def test_onchain_missing_coin_is_reported_not_crash(tmp_path):
    from src.data.build_panel import load_onchain
    d = tmp_path / "data" / "raw" / "onchain"
    d.mkdir(parents=True)
    rng = np.random.default_rng(6)
    rows = [r for r in _dune_rows(pd.date_range("2020-03-01", "2020-04-01", freq="D"), rng) if r["symbol"] != "DAI"]
    pd.DataFrame(rows).to_csv(d / "x.csv", index=False)
    cfg = {"onchain": {"curve3pool_csv": "data/raw/onchain/a.csv", "mintburn_csv": "data/raw/onchain/b.csv"}}
    oc = load_onchain(cfg, tmp_path)
    assert oc["curve"] is None
    assert any(r["kind"] == "BỎ QUA curve" for r in oc["report"])
    assert load_onchain(cfg, tmp_path / "khong_ton_tai")["curve"] is None                              # thư mục không tồn tại


# ----------------------------------------------------------------- on-chain: guard + Dune API (phản hồi giả)
def _curve_csv(folder, days, rng, mean=1e5):
    rows = [{"day": x.strftime("%Y-%m-%d 00:00:00.000 UTC"), "symbol": s, "net": float(rng.normal(mean, 2e4))}
            for x in days for s in ("DAI", "USDC", "USDT")]
    pd.DataFrame(rows).to_csv(folder / f"c_{days[0].date()}.csv", index=False)


def _cfg_oc():
    return {"onchain": {"curve3pool_csv": "data/raw/onchain/a.csv", "mintburn_csv": "data/raw/onchain/b.csv"}}


def test_onchain_guards_reject_bad_history(tmp_path):
    from src.data.build_panel import load_onchain
    rng = np.random.default_rng(11)
    # (a) thiếu một đoạn dài -> bỏ qua
    a = tmp_path / "a" / "data" / "raw" / "onchain"; a.mkdir(parents=True)
    _curve_csv(a, pd.date_range("2020-09-15", "2020-10-15", freq="D"), rng)
    _curve_csv(a, pd.date_range("2021-03-01", "2021-04-01", freq="D"), rng)
    oc = load_onchain(_cfg_oc(), tmp_path / "a")
    assert oc["curve"] is None and any("khoảng trống" in r["detail"] for r in oc["report"])
    # (b) thiếu lịch sử đầu (bắt đầu 2021) -> bỏ qua
    b = tmp_path / "b" / "data" / "raw" / "onchain"; b.mkdir(parents=True)
    _curve_csv(b, pd.date_range("2021-03-01", "2021-06-01", freq="D"), rng)
    oc = load_onchain(_cfg_oc(), tmp_path / "b")
    assert oc["curve"] is None and any("thiếu lịch sử đầu" in r["detail"] for r in oc["report"])
    # (c) số dư âm (dòng chảy ròng âm) -> bỏ qua
    c = tmp_path / "c" / "data" / "raw" / "onchain"; c.mkdir(parents=True)
    _curve_csv(c, pd.date_range("2020-09-15", "2020-12-01", freq="D"), rng, mean=-1e5)
    oc = load_onchain(_cfg_oc(), tmp_path / "c")
    assert oc["curve"] is None and any("ÂM" in r["detail"] for r in oc["report"])
    # (d) dữ liệu đúng -> dùng được
    d = tmp_path / "d" / "data" / "raw" / "onchain"; d.mkdir(parents=True)
    _curve_csv(d, pd.date_range("2020-09-15", "2021-06-01", freq="D"), rng)
    assert load_onchain(_cfg_oc(), tmp_path / "d")["curve"] is not None


class _Resp:
    def __init__(self, js, status=200, headers=None):
        self._js, self.status_code, self.headers, self.text = js, status, headers or {}, str(js)
    def json(self):
        return self._js


def test_dune_run_sql_polling_pagination_and_key_in_header(monkeypatch):
    from src.data import fetch_onchain as fo
    calls = []
    states = iter(["QUERY_STATE_PENDING", "QUERY_STATE_EXECUTING", "QUERY_STATE_COMPLETED"])
    pages = {0: {"result": {"rows": [{"day": "d1", "symbol": "DAI", "net": 1.0}] * 2}, "next_offset": 2},
             2: {"result": {"rows": [{"day": "d2", "symbol": "USDC", "net": 2.0}]}}}
    def fake(method, url, headers=None, json=None, params=None, timeout=None):
        calls.append((method, url, headers, params))
        if url.endswith("/sql/execute"):
            return _Resp({"execution_id": "E1"})
        if url.endswith("/status"):
            return _Resp({"state": next(states)})
        return _Resp(pages[params["offset"]])
    monkeypatch.setattr(fo.requests, "request", fake)
    monkeypatch.setattr(fo.time, "sleep", lambda *_: None)
    df = fo.run_sql("SELECT 1", "SECRETKEY")
    assert len(df) == 3 and list(df.columns) == ["day", "symbol", "net"]
    assert all(h == {"X-Dune-API-Key": "SECRETKEY"} for _, _, h, _ in calls)      # key chỉ nằm trong header
    assert all("SECRETKEY" not in u for _, u, _, _ in calls)


def test_dune_errors_and_rate_limit(monkeypatch):
    from src.data import fetch_onchain as fo
    monkeypatch.setattr(fo.time, "sleep", lambda *_: None)
    seq = iter([_Resp({}, 429, {"Retry-After": "1"}), _Resp({"execution_id": "E"}), _Resp({"state": "QUERY_STATE_FAILED", "error": "boom"})])
    monkeypatch.setattr(fo.requests, "request", lambda *a, **k: next(seq))
    with pytest.raises(fo.DuneError, match="boom"):
        fo.run_sql("SELECT 1", "K")
    monkeypatch.setattr(fo.requests, "request", lambda *a, **k: _Resp({"error": "x"}, 403))
    with pytest.raises(fo.DuneAuthError):
        fo.run_sql("SELECT 1", "K")


def test_fetch_years_resume_and_failures(tmp_path):
    from src.data import fetch_onchain as fo
    ran = []
    def runner(sql, key):
        ran.append(sql)
        return pd.DataFrame({"day": ["2020-09-15 00:00:00.000 UTC"], "symbol": ["DAI"], "net": [1.0]})
    cols = ["day", "symbol", "net"]
    f = fo.fetch_years("curve", fo.sql_curve, cols, tmp_path, 2020, 2022, "K", runner=runner)
    assert f == [] and sorted(p.name for p in tmp_path.glob("*.csv")) == ["curve_2020.csv", "curve_2021.csv", "curve_2022.csv"]
    ran.clear()
    fo.fetch_years("curve", fo.sql_curve, cols, tmp_path, 2020, 2022, "K", runner=runner)
    assert len(ran) == 1 and "2022-01-01" in ran[0]                                # chỉ tải lại năm hiện tại
    (tmp_path / "curve_2021.csv").unlink()
    def flaky(sql, key):
        if "2021-01-01" in sql:
            raise fo.DuneError("timeout")
        return runner(sql, key)
    fails = fo.fetch_years("curve", fo.sql_curve, cols, tmp_path, 2020, 2022, "K", runner=flaky)
    assert [y for y, _ in fails] == [2021]
    def auth(sql, key):
        raise fo.DuneAuthError("HTTP 403")
    t2 = tmp_path / "t2"
    fails = fo.fetch_years("curve", fo.sql_curve, cols, t2, 2020, 2024, "K", runner=auth)
    assert len(fails) == 1                                                         # lỗi quyền: dừng ngay, không thử năm sau


def test_precheck_and_sql_consistency(monkeypatch):
    from src.data import fetch_onchain as fo
    good = pd.DataFrame({"contract_address": [fo.USDT.lower(), fo.USDC.lower(), fo.DAI.lower()], "n": [5, 6, 7]})
    assert fo.precheck("K", runner=lambda s, k: good) is True
    assert fo.precheck("K", runner=lambda s, k: good.iloc[:2]) is False
    assert fo.precheck("K", runner=lambda s, k: pd.DataFrame()) is False
    sql_file = (Path(__file__).resolve().parents[1] / "src" / "data" / "dune_queries.sql").read_text(encoding="utf-8")
    for addr in (fo.POOL, fo.USDT, fo.USDC, fo.DAI):                                # file SQL thủ công và code phải cùng địa chỉ
        assert addr in sql_file


def test_main_without_key_skips_cleanly(monkeypatch, capsys):
    from src.data import fetch_onchain as fo
    monkeypatch.setattr(fo, "load_env", lambda *a, **k: None)
    monkeypatch.delenv("DUNE_API_KEY", raising=False)
    assert fo.main([]) == 0
    assert "BỎ QUA on-chain" in capsys.readouterr().out


def test_supply_eth_proxy_columns_and_no_lookahead(raw):
    """Proxy on-chain từ DefiLlama: tỷ trọng đúng, thay đổi dùng đúng độ trễ, tương lai không ảnh hưởng quá khứ."""
    for coin in CFG["coins"]:
        raw["supply"][coin]["supply_eth"] = raw["supply"][coin]["supply"] * 0.4
    p = build_panel(raw, CFG)
    u = p[p.stablecoin == "USDC"].set_index("timestamp")
    t = pd.Timestamp("2020-05-10", tz="UTC")
    assert u.loc[t, "supply_eth_share"] == pytest.approx(0.4)
    s = raw["supply"]["USDC"]["supply_eth"]
    exp = s.loc[t - pd.Timedelta(days=1)] / s.loc[t - pd.Timedelta(days=2)] - 1.0          # trễ 1 ngày
    assert u.loc[t, "supply_eth_change"] == pytest.approx(exp)
    # nhiễu loạn tương lai
    T0 = pd.Timestamp("2020-07-15", tz="UTC")
    pert = copy.deepcopy(raw)
    for coin in CFG["coins"]:
        x = pert["supply"][coin]
        x.loc[x.index > T0, "supply_eth"] *= 3.0
    a = p[p.timestamp <= T0].reset_index(drop=True)
    b = build_panel(pert, CFG)
    b = b[b.timestamp <= T0].reset_index(drop=True)
    cols = ["supply_eth", "supply_eth_change", "supply_eth_share"]
    pd.testing.assert_frame_equal(a[cols], b[cols], check_exact=False, rtol=1e-12)


def test_supply_eth_missing_gives_nan(raw):
    p = build_panel(raw, CFG)                       # fixture mặc định: supply_eth toàn NaN
    assert p[["supply_eth", "supply_eth_change", "supply_eth_share"]].isna().all().all()


# ----------------------------------------------------------------- Curve API (phản hồi giả, nhiều dạng) + số dư trực tiếp
def _fake_get_factory(payload_fn, calls):
    class R:
        def __init__(self, js, code=200): self._js, self.status_code, self.text = js, code, str(js)
        def json(self): return self._js
    def fake(url, params=None, headers=None, timeout=None):
        calls.append((url, dict(params)))
        r = payload_fn(url, params)
        return r if isinstance(r, R) else R(r)
    return fake, R


def test_curve_parse_variants():
    from src.data import fetch_curve as fc
    t0 = 1678320000  # 2023-03-09 00:00 UTC
    a = {"data": [{"timestamp": t0, "tvl_usd": 3e9, "balances": [1e9, 1.2e9, 0.8e9]}]}                         # list theo thứ tự coin
    b = [{"time": "2023-03-09T04:00:00Z", "balances": {"DAI": "1000", "USDC": "2000", "USDT": "3000"}}]          # dict + ISO + chuỗi số
    c = {"snapshots": [{"timestamp": t0 * 1000, "balances": [{"symbol": "usdt", "balance": 3}, {"symbol": "DAI", "balance": 1},
                                                              {"symbol": "USDC", "balance": 2}]}]}              # list đối tượng + ms
    d = {"data": [{"timestamp": t0, "balances": [1e27, 1e15, 2e15]}]}                                          # số dư thô (wei)
    for js in (a, b, c, d):
        df = fc.parse_snapshots(js)
        assert set(df["symbol"]) == {"DAI", "USDC", "USDT"} and len(df) == 3
        assert df["day"].dt.tz is not None and (df["day"] == df["day"].dt.normalize()).all()
    wide = fc.parse_snapshots(d).pivot(index="day", columns="symbol", values="balance")
    assert wide.iloc[0].to_dict() == {"DAI": 1e9, "USDC": 1e9, "USDT": 2e9}                                     # đã chia thập phân
    w_c = fc.parse_snapshots(c).pivot(index="day", columns="symbol", values="balance").iloc[0].to_dict()
    assert w_c == {"DAI": 1.0, "USDC": 2.0, "USDT": 3.0}
    # nhiều bản ghi trong một ngày -> giữ bản cuối
    e = {"data": [{"timestamp": t0, "balances": [1, 1, 1]}, {"timestamp": t0 + 3600, "balances": [5, 5, 5]}]}
    assert fc.parse_snapshots(e)["balance"].eq(5).all()
    for bad in ({"data": []}, {"data": [{"timestamp": t0, "foo": 1}]}, {"data": [{"timestamp": t0, "balances": [1, 2]}]}, {"x": 1}):
        with pytest.raises(fc.SchemaError):
            fc.parse_snapshots(bad)


def test_curve_fetch_range_chunks_retries_and_errors(monkeypatch):
    from src.data import fetch_curve as fc
    monkeypatch.setattr(fc.time, "sleep", lambda *_: None)
    calls = []
    state = {"n": 0}
    def payload(url, params):
        state["n"] += 1
        if state["n"] == 1:
            return R({}, 429)                                              # lần đầu bị giới hạn tốc độ -> thử lại
        days = pd.date_range(pd.to_datetime(params["start"], unit="s"), pd.to_datetime(params["end"], unit="s"), freq="D")
        return {"data": [{"timestamp": int(d.timestamp()), "balances": [1e9, 1e9, 1e9]} for d in days]}
    fake, R = _fake_get_factory(payload, calls)
    monkeypatch.setattr(fc.requests, "get", fake)
    df = fc.fetch_range(__import__("datetime").date(2020, 9, 1), __import__("datetime").date(2021, 6, 30))
    assert df["day"].nunique() == (pd.Timestamp("2021-06-30") - pd.Timestamp("2020-09-01")).days + 1
    assert all(p["unit"] == "day" and "start" in p and "end" in p for _, p in calls)
    assert all("/snapshots/ethereum/" + fc.POOL + "/tvl" in u for u, _ in calls)
    assert max(p["end"] - p["start"] for _, p in calls) < 300 * 86400                                       # < 300 bản ghi mỗi lần
    # lỗi 4xx -> CurveApiError
    fake2, R2 = _fake_get_factory(lambda u, p: R2({"detail": "nope"}, 404), [])
    monkeypatch.setattr(fc.requests, "get", fake2)
    with pytest.raises(fc.CurveApiError, match="404"):
        fc._get("/x", {})


def _balances_csv(folder, days, rng, drift=0.0, name="curve3pool_balances.csv"):
    rows = []
    for i, d in enumerate(days):
        base = 1e9 * (1 + drift * i)
        for s, mult in (("DAI", 1.0), ("USDC", 1.1), ("USDT", 0.9)):
            rows.append({"day": d.strftime("%Y-%m-%d 00:00:00.000 UTC"), "symbol": s, "balance": base * mult * (1 + rng.normal(0, .01))})
    pd.DataFrame(rows).to_csv(folder / name, index=False)


def test_loader_prefers_balances_with_lag_and_guards(tmp_path):
    from src.data.build_panel import load_onchain
    rng = np.random.default_rng(21)
    cfg = {"onchain": {"curve3pool_csv": "data/raw/onchain/a.csv", "mintburn_csv": "data/raw/onchain/b.csv",
                       "curve_balances_lag_days": 1}}
    d = tmp_path / "ok" / "data" / "raw" / "onchain"; d.mkdir(parents=True)
    days = pd.date_range("2021-01-01", "2021-03-31", freq="D")
    _balances_csv(d, days, rng)
    _curve_csv(d, pd.date_range("2020-09-15", "2021-03-31", freq="D"), rng)                  # có cả dữ liệu Dune: phải ưu tiên balances
    oc = load_onchain(cfg, tmp_path / "ok")
    assert oc["curve"] is not None and any(r["kind"] == "dùng balances" for r in oc["report"])
    assert oc["curve"].dropna(how="all").index.min() == pd.Timestamp("2021-01-02", tz="UTC")  # trễ 1 ngày: ngày đầu có giá trị là 02/01
    assert oc["curve"].index.max() == pd.Timestamp("2021-04-01", tz="UTC")                   # không mất ngày cuối do dịch
    assert abs(float(oc["curve"].iloc[10].sum())) < 1e-9 and oc["curve_size"].dropna().gt(0).all()
    raw_df = pd.read_csv(d / "curve3pool_balances.csv")
    day0 = raw_df[raw_df.day.str.startswith("2021-01-10")].set_index("symbol")["balance"]
    exp = day0["USDC"] / day0.sum() - 1 / 3
    assert oc["curve"].loc[pd.Timestamp("2021-01-11", tz="UTC"), "USDC"] == pytest.approx(exp)
    # lag = 0 -> không dịch
    cfg0 = {"onchain": dict(cfg["onchain"], curve_balances_lag_days=0)}
    assert load_onchain(cfg0, tmp_path / "ok")["curve"].dropna(how="all").index.min() == pd.Timestamp("2021-01-01", tz="UTC")
    # đơn vị không đồng nhất (DAI thô 1e27) -> bỏ qua
    u = tmp_path / "unit" / "data" / "raw" / "onchain"; u.mkdir(parents=True)
    rows = [{"day": d_.strftime("%Y-%m-%d"), "symbol": s, "balance": v} for d_ in days for s, v in (("DAI", 1e27), ("USDC", 1e9), ("USDT", 1e9))]
    pd.DataFrame(rows).to_csv(u / "x.csv", index=False)
    oc = load_onchain(cfg, tmp_path / "unit")
    assert oc["curve"] is None and any("đơn vị" in r["detail"] for r in oc["report"])
    # thiếu coin / số dư âm -> bỏ qua
    m = tmp_path / "miss" / "data" / "raw" / "onchain"; m.mkdir(parents=True)
    pd.DataFrame([r for r in rows if r["symbol"] != "USDT"]).to_csv(m / "x.csv", index=False)
    assert load_onchain(cfg, tmp_path / "miss")["curve"] is None
    n = tmp_path / "neg" / "data" / "raw" / "onchain"; n.mkdir(parents=True)
    pd.DataFrame([dict(r, balance=-5.0) if r["symbol"] == "DAI" else r for r in rows]).to_csv(n / "x.csv", index=False)
    assert load_onchain(cfg, tmp_path / "neg")["curve"] is None


def test_balances_panel_columns_and_no_lookahead(raw, tmp_path):
    from src.data.build_panel import load_onchain
    rng = np.random.default_rng(31)
    d = tmp_path / "data" / "raw" / "onchain"; d.mkdir(parents=True)
    days = pd.date_range("2020-02-01", "2020-09-30", freq="D")
    _balances_csv(d, days, rng, drift=0.002)
    cfg = copy.deepcopy(CFG)
    cfg["onchain"] = {"curve3pool_csv": "data/raw/onchain/a.csv", "mintburn_csv": "data/raw/onchain/b.csv"}   # lag mặc định = 1
    p = build_panel(raw, cfg, load_onchain(cfg, tmp_path))
    x = p[p.timestamp == pd.Timestamp("2020-06-15", tz="UTC")].set_index("stablecoin")
    assert x.loc[["USDC", "DAI"], ["dex_imbalance", "dex_pool_size"]].notna().all().all()
    assert pd.isna(x.loc["UST", "dex_imbalance"]) and pd.isna(x.loc["UST", "dex_pool_size"])
    assert (p.loc[p.stablecoin == "UST", "onchain_available"] == False).all()                            # noqa: E712
    # nhiễu loạn: đổi toàn bộ số dư SAU ngày T không được đổi bất kỳ giá trị nào tại/trước T
    T0 = pd.Timestamp("2020-07-15", tz="UTC")
    df = pd.read_csv(d / "curve3pool_balances.csv")
    late = pd.to_datetime(df["day"], utc=True) > T0
    df.loc[late, "balance"] *= np.where(df.loc[late, "symbol"] == "USDC", 5.0, 1.0)
    d2 = tmp_path / "p" / "data" / "raw" / "onchain"; d2.mkdir(parents=True)
    df.to_csv(d2 / "curve3pool_balances.csv", index=False)
    p2 = build_panel(raw, cfg, load_onchain(cfg, tmp_path / "p"))
    cols = ["dex_imbalance", "dex_pool_size"]
    a = p[p.timestamp <= T0].reset_index(drop=True)[cols]
    b = p2[p2.timestamp <= T0].reset_index(drop=True)[cols]
    pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-12)
