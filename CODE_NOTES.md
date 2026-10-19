# CODE_NOTES — Ghi chú kỹ thuật toàn bộ pipeline Code 1 (Nhóm 4, Stablecoin Depeg Early-Warning)

> **Quy tắc duy trì:** mỗi lần code được viết hoặc sửa, file này phải được cập nhật (mục 9 *Nhật ký thay đổi* + các mục bị ảnh hưởng).
> Mục đích: để người làm các phần sau (Code 2, Code 3, Code 4, Viết 1, Viết 2) nắm được code hiện **làm được gì, giới hạn ở đâu, đã xử lý gì**.
> Trạng thái ghi nhận: **bản v1-dev, 18/10/2026**. Bộ test: **27/27 pass** (cả khi ép mã hóa mặc định kiểu Windows) (chạy bằng dữ liệu giả, không cần mạng).

---

## 1. Tóm tắt một đoạn

Pipeline tải dữ liệu thô từ Yahoo Finance, CryptoCompare, Binance, DefiLlama và API công khai của Curve (số dư pool; Dune là tùy chọn nhưng cần gói trả phí), chuẩn hóa về **UTC theo ngày**, ghép thành panel
**stablecoin × ngày** và ghi `data/processed/panel.parquet`. Pipeline cũng tự sinh khung Phụ lục A (data dictionary), khung Phụ lục B (source log),
báo cáo QC và hai bảng rà soát tay. Pipeline **không** tạo nhãn depeg và **không** huấn luyện mô hình (việc của Code 2/3).

## 2. Cấu trúc thư mục

```
code1/
├── README.md              cách chạy ngắn gọn
├── CODE_NOTES.md          (file này) ghi chú kỹ thuật đầy đủ
├── requirements.txt
├── .gitignore             loại .env, __pycache__, .venv...
├── run_all.bat            (Windows) chạy trọn bộ: cài thư viện → tải số dư Curve 3pool (API Curve, không cần key) → dựng panel → kiểm tra → test
├── .env.example           mẫu; sao chép thành .env rồi điền key (KHÔNG commit .env)
├── config/config.yaml     toàn bộ tham số
├── src/data/
│   ├── fetch.py           tải từng nguồn, cache, source log, đọc .env
│   ├── build_panel.py     ghép panel, tính biến, nạp on-chain, ghi parquet + bảng
│   ├── dictionary.py      định nghĩa cột (nguồn gốc của data_dictionary.csv và thứ tự cột panel)
│   ├── qc.py              kiểm tra chất lượng + bảng rà soát tay
│   ├── fetch_curve.py     tải số dư lịch sử Curve 3pool từ API công khai của Curve (KHÔNG cần key); --probe để soi phản hồi thật
│   ├── fetch_onchain.py   (tùy chọn, cần gói Dune trả phí) tải chuyển ròng/mint-burn từ Dune qua API
│   ├── check_onchain.py   chẩn đoán dữ liệu on-chain (python -m src.data.check_onchain)
│   └── dune_queries.sql   2 query Dune (Curve 3pool, mint/burn)
├── tests/test_panel.py    27 test (kể cả Curve API và Dune API bằng phản hồi giả)
├── data/raw/              cache dữ liệu thô từng nguồn (CSV); data/raw/onchain/ cho dữ liệu on-chain
├── data/processed/        panel.parquet, panel_v0.parquet, panel_v1.parquet
└── outputs/tables/        data_dictionary.csv, source_log_raw.csv, coverage_by_coin.csv, qc_report.csv,
                           review_src_disagree_days.csv, review_price_outliers.csv
```

## 3. Cách chạy

```
pip install -r requirements.txt
Copy-Item .env.example .env          # rồi điền CRYPTOCOMPARE_API_KEY=... (và DUNE_API_KEY=... nếu muốn on-chain) vào .env
.\run_all.bat                          # hoặc nhấp đúp: chạy trọn bộ (cài thư viện, on-chain, panel, kiểm tra, test)
python -m src.data.fetch_curve --probe                 # soi phản hồi thật của API Curve (nên chạy trước lần đầu)
python -m src.data.fetch_curve                         # tải số dư Curve 3pool (không cần key)
python -m src.data.fetch_onchain                       # (tùy chọn, cần Dune trả phí) chuyển ròng/mint-burn
python -m src.data.build_panel --version v1            # dùng cache; nguồn lỗi được thử lại
python -m src.data.build_panel --version v1 --refresh  # tải lại toàn bộ
python -m src.data.check_onchain                       # kiểm tra dữ liệu on-chain
pytest -q
```

## 4. Luồng xử lý

1. `fetch_all` đọc config, tải từng (coin, nguồn); mỗi lần tải ghi một dòng vào source log (trạng thái, số dòng, ngày đầu/cuối, lỗi, ghi chú khớp DefiLlama).
   Nguồn lỗi **không làm sập pipeline**; lỗi được ghi log. Dữ liệu tải thành công được cache vào `data/raw/*.csv`; lần chạy sau dùng cache, nguồn lỗi được thử lại.
2. `fetch_curve` gọi API công khai của Curve (`prices.curve.finance/v1/snapshots/ethereum/<pool>/tvl`, không cần key), tải số dư DAI/USDC/USDT của Curve 3pool theo ngày (cửa sổ 250 ngày/lần) → `data/raw/onchain/curve3pool_balances.csv`. (Tùy chọn, cần Dune trả phí) `fetch_onchain` tải chuyển ròng/mint-burn từ Dune.
   `load_onchain` quét `data/raw/onchain/*.csv` (nhận diện theo **tên cột**, không theo tên file) → tỷ trọng Curve 3pool, quy mô pool, mint/burn.
3. `build_panel` ghép: khối giá từng coin → lưới ngày liên tục → biến thị trường/DefiLlama/on-chain → cờ chất lượng → sắp cột theo `DICTIONARY`.
4. Ghi parquet và các bảng; `run_qc` kiểm tra và ghi `qc_report.csv`.

## 5. Quy tắc xử lý dữ liệu (đã cài đặt)

**Thời gian.** Mọi mốc là UTC 00:00 theo ngày. Hàng `(stablecoin, t)` chỉ chứa thông tin biết được đến hết ngày t. QC xác nhận lệch nến Yahoo–CryptoCompare tối ưu = 0 ngày.

**Lưới ngày.** Mỗi coin có lưới ngày **liên tục** từ ngày đầu đến ngày cuối có giá (ngày thiếu giữ lại với `is_missing_price = True`, cột giá = NaN) để `shift(-h)` tạo nhãn đúng.
Panel là **không cân bằng**: mỗi coin chỉ có mặt trong thời gian tồn tại (xem mục 7).

**Chọn nguồn giá cho mỗi ngày:** Yahoo → (nếu thiếu) CryptoCompare → (chỉ coin bật `binance_fallback`, hiện là UST) Binance quy USDT. Nguồn từng dòng nằm ở `price_src`.

**High/Low.** Nếu `high < max(open, close)` hoặc `low > min(open, close)` thì chỉnh lại và đặt `hl_fixed = True`.

**Độ lệch peg.** `peg_deviation` theo Yahoo; `peg_deviation_cc` theo CryptoCompare; `peg_deviation_min` = nhỏ hơn trong hai (chỉ lớn khi cả hai nguồn cùng thấy lệch); tương tự cho High/Low (`*_hl`, `*_hl_cc`, `*_hl_min`).

**DefiLlama (supply, TVL).** Trễ `llama_lag_days` (mặc định 1) ngày để loại trừ look-ahead; ffill tối đa `llama_ffill_limit` (3) ngày và gắn `supply_ffilled`.
`supply_change`, `tvl_change` được tính trên **chuỗi đầy đủ của nguồn** rồi mới đưa vào lưới coin (tránh mất ngày đầu của coin niêm yết muộn); chia cho 0 (TVL = 0 những năm đầu) được đổi thành NaN.

**BTC/ETH.** Log-return và độ lệch chuẩn trượt `vol_window` (7) ngày tính trên **toàn chuỗi**; tải sớm hơn mẫu `warmup_days` (30) ngày. Cùng một ngày mọi coin nhận cùng giá trị (QC kiểm tra).

**Ngày cuối đóng băng.** `sample.end` cố định (hiện `2026-10-16`) để panel tái lập được và mọi nguồn đều có dữ liệu. Khi chốt v1, đặt = ngày UTC gần nhất mà Yahoo và DefiLlama đều có dữ liệu.

**Proxy on-chain từ DefiLlama (không cần key Dune).** Từ cột `supply_eth` đã có sẵn trong cache `llama_supply_*.csv` (supply của coin trên mạng Ethereum), panel tính `supply_eth`, `supply_eth_change`, `supply_eth_share` (cùng độ trễ `llama_lag_days` và cùng chốt chặn look-ahead như `supply`). Đây là **proxy phát hành/hủy ròng trên Ethereum** (gồm cả phần chuyển qua cầu nối), KHÔNG thay được `dex_imbalance` (tỷ trọng trong pool) và không phải mint/burn thuần. Nếu Dune không dùng được, đây là phương án on-chain duy nhất của dự án và phải ghi rõ là proxy trong báo cáo.

**On-chain trực tiếp (ưu tiên số dư từ API Curve).** `dex_imbalance` = tỷ trọng coin trong Curve 3pool − 1/3; `dex_pool_size` = tổng số dư ba coin. Chỉ USDT, USDC, DAI. Nguồn ưu tiên: **số dư pool theo ngày từ API Curve** (`curve3pool_balances.csv`; không cần lịch sử tích lũy nên không có rủi ro thiếu năm), áp **độ trễ `onchain.curve_balances_lag_days` (mặc định 1 ngày)** vì chưa rõ snapshot ngày được cắt ở thời điểm nào (bảo thủ chống look-ahead). Nếu không có, dùng chuyển ròng từ Dune (số dư = tổng tích lũy kể từ khi pool triển khai). Mint/burn (`mint_eth`, `burn_eth`) chỉ có khi có dữ liệu Dune (USDC, DAI trên Ethereum).

**Chốt chặn:** dữ liệu số dư bị BỎ QUA (kèm lý do trong báo cáo) nếu thiếu coin, có số dư âm, hoặc đơn vị ba token không đồng nhất (một coin chiếm >98% hoặc <2% tỷ trọng trung vị); dữ liệu Dune bị bỏ qua nếu bắt đầu muộn hơn 31/12/2020, có khoảng trống >14 ngày, hoặc có ngày số dư âm.

**Lưu ý diễn giải quan trọng:** theo trang Etherscan (31/07/2026) pool hiện chỉ còn khoảng 160 triệu USD với USDT ~68%, USDC ~17%, DAI ~15%: tức đã lệch CẤU TRÚC. Vì vậy mức tuyệt đối của `dex_imbalance` các năm gần đây phản ánh thay đổi cơ cấu pool chứ không hẳn là áp lực bán; Code 2 nên dùng thay đổi hoặc độ lệch so với trung bình trượt, và đọc kèm `dex_pool_size`.

Thiếu dữ liệu on-chain → các cột này NaN, `onchain_available = False`, pipeline vẫn chạy bằng proxy DefiLlama.
**Chốt chặn dữ liệu Curve:** số dư là tổng tích lũy nên cần lịch sử liền mạch; `load_onchain` BỎ QUA toàn bộ dữ liệu Curve (kèm lý do trong báo cáo) nếu dữ liệu bắt đầu muộn hơn 31/12/2020, có khoảng trống > 14 ngày, hoặc có ngày số dư âm. Nhờ đó dữ liệu thiếu năm không bị đưa lặng lẽ vào panel.

**Bảo mật key.** Key CryptoCompare đọc từ biến môi trường hoặc `.env`; gửi qua header (không nằm trong URL); bị che `***` nếu xuất hiện trong source log.

## 6. Cột của `panel.parquet`

Nhãn depeg **không** có trong panel (Code 2 tạo từ `peg_deviation*`). Bảng dưới sinh tự động từ `dictionary.py`.

| Cột | Kiểu | Đơn vị | Định nghĩa | Nguồn | Quy ước thời điểm |
|---|---|---|---|---|---|
| `timestamp` | datetime64[ns, UTC] | UTC 00:00 | Ngày (UTC) của quan sát; khóa cùng với stablecoin. | Pipeline | Hàng t phản ánh thông tin biết được đến hết ngày t (24:00 UTC). |
| `stablecoin` | string | - | Mã stablecoin (USDT, USDC, DAI, BUSD, FDUSD, UST). | config.yaml | - |
| `role` | string | - | Vai trò trong mẫu: core / extension / case_study (mục 3.1 của đề). | config.yaml | - |
| `coin_type` | string | - | Loại: fiat_backed / crypto_backed / algorithmic (dùng cho RQ3). | config.yaml | - |
| `price` | float64 | USD | Giá đóng cửa ngày. Ưu tiên Yahoo; thiếu thì lấy CryptoCompare (xem price_src). | Yahoo Finance / CryptoCompare | Close của nến ngày t (đến 24:00 UTC). |
| `open` | float64 | USD | Giá mở cửa ngày (00:00 UTC). | như price | Biết tại 00:00 ngày t. |
| `high` | float64 | USD | Giá cao nhất trong ngày; đã chỉnh để >= max(open, close) (xem hl_fixed). | như price | Biết đến hết ngày t. |
| `low` | float64 | USD | Giá thấp nhất trong ngày; đã chỉnh để <= min(open, close). | như price | Biết đến hết ngày t. |
| `peg_dev_signed` | float64 | USD | price - 1 (có dấu: âm = dưới neo). | Tính | Dùng price ngày t. |
| `peg_deviation` | float64 | USD (tỷ lệ) | Dev = /price - 1/ theo giá đóng cửa. | Tính | Dùng price ngày t. |
| `peg_deviation_cc` | float64 | USD (tỷ lệ) | /cc_close - 1/: độ lệch theo giá đóng cửa CryptoCompare (nguồn độc lập với Yahoo). NaN nếu thiếu CryptoCompare. | CryptoCompare | Close ngày t. |
| `peg_deviation_min` | float64 | USD (tỷ lệ) | min(peg_deviation, peg_deviation_cc): chỉ lớn khi CẢ HAI nguồn cùng thấy lệch. Dùng tạo nhãn 'bảo thủ' để kiểm tra độ nhạy; nếu thiếu CryptoCompare thì bằng peg_deviation. | Tính | Close ngày t. |
| `peg_deviation_hl` | float64 | USD (tỷ lệ) | Độ lệch trong ngày xấu nhất: max(/high-1/, /low-1/). Bắt depeg chỉ xảy ra trong ngày. | Tính | Dùng high/low ngày t. |
| `peg_deviation_hl_cc` | float64 | USD (tỷ lệ) | max(/cc_high-1/, /cc_low-1/): độ lệch trong ngày theo High/Low CryptoCompare. NaN nếu thiếu CryptoCompare. | CryptoCompare | Cả ngày t. |
| `peg_deviation_hl_min` | float64 | USD (tỷ lệ) | min(peg_deviation_hl, peg_deviation_hl_cc): chỉ lớn khi cả hai nguồn cùng thấy lệch trong ngày. Khuyến nghị dùng cho nhãn chính vì High/Low của Yahoo bị nhiễu giai đoạn 2020–đầu 2021. | Tính | Cả ngày t. |
| `volume` | float64 | USD | Khối lượng giao dịch ngày quy USD. | như price | Cả ngày t. |
| `range_hl` | float64 | tỷ lệ | (high - low) / close. Proxy biến động/căng thẳng thanh khoản trong ngày. | Tính | Cả ngày t. |
| `turnover` | float64 | tỷ lệ | volume / supply. Proxy thanh khoản (không có bid-ask spread lịch sử miễn phí). | Tính | supply đã trễ theo llama_lag_days. |
| `supply` | float64 | USD | Circulating supply tổng các chain, trễ llama_lag_days ngày, ffill tối đa llama_ffill_limit ngày. | DefiLlama | Giá trị gắn nhãn <= t - lag. |
| `supply_ffilled` | bool | - | True nếu supply hàng này là giá trị ffill. | Pipeline | - |
| `supply_change` | float64 | tỷ lệ | supply_t / supply_{t-1} - 1. Proxy mint/burn ròng toàn chain. | Tính | Chỉ dùng t và t-1. |
| `supply_eth` | float64 | USD | Circulating supply của coin trên mạng Ethereum (DefiLlama, tách theo chain), trễ llama_lag_days ngày. Gồm cả phần chuyển qua cầu nối nên KHÔNG phải mint/burn thuần. NaN nếu coin không có trên Ethereum. | DefiLlama | Giá trị gắn nhãn <= t - lag. |
| `supply_eth_change` | float64 | tỷ lệ | supply_eth_t / supply_eth_{t-1} - 1: proxy phát hành/hủy ròng trên Ethereum (thay cho mint_eth/burn_eth khi chưa có dữ liệu Dune). | Tính | Chỉ dùng t và t-1. |
| `supply_eth_share` | float64 | tỷ lệ | supply_eth / supply: tỷ trọng nguồn cung nằm trên Ethereum. Đổi đột ngột = dịch chuyển giữa các chain hoặc phát hành/hủy lớn trên Ethereum. | Tính | Cùng độ trễ với supply. |
| `tvl` | float64 | USD | TVL toàn thị trường DeFi (tổng chain). Biến cấp thị trường, giống nhau giữa các coin. | DefiLlama | Trễ llama_lag_days ngày. |
| `tvl_change` | float64 | tỷ lệ | tvl_t / tvl_{t-1} - 1. | Tính | Chỉ dùng t và t-1. |
| `btc_ret` | float64 | log | Log-return ngày của BTC-USD. | Yahoo Finance | Close ngày t. |
| `btc_vol` | float64 | log-return std | Độ lệch chuẩn log-return BTC trượt vol_window ngày (mặc định 7), chỉ dùng t-6..t. | Tính | Cửa sổ lùi. |
| `eth_ret` | float64 | log | Log-return ngày của ETH-USD (đại diện cú sốc tài sản thế chấp của DAI). | Yahoo Finance | Close ngày t. |
| `eth_vol` | float64 | log-return std | Độ lệch chuẩn log-return ETH trượt vol_window ngày. | Tính | Cửa sổ lùi. |
| `dex_imbalance` | float64 | tỷ lệ | Tỷ trọng coin trong Curve 3pool trừ 1/3 (dương = pool thừa coin đó, thường báo hiệu bị bán tháo). Chỉ có cho USDT/USDC/DAI. NaN nếu chưa có dữ liệu Curve/Dune. Lưu ý lệch CẤU TRÚC khi pool teo và một coin chiếm ưu thế kéo dài: nên dùng thay đổi hoặc độ lệch so với trung bình trượt. | API Curve (snapshot số dư pool) hoặc Dune (chuyển ròng) | Số dư gắn nhãn <= t - curve_balances_lag_days (mặc định 1). |
| `dex_pool_size` | float64 | đơn vị coin (~USD) | Tổng số dư ba coin trong Curve 3pool. Dùng để đánh giá độ tin cậy của dex_imbalance: pool càng nhỏ, tỷ trọng càng nhiễu. NaN nếu chưa có dữ liệu Curve/Dune. | API Curve hoặc Dune | Cùng độ trễ với dex_imbalance. |
| `mint_eth` | float64 | đơn vị coin | Lượng mint trên Ethereum trong ngày (Transfer từ địa chỉ 0x0). Không áp dụng USDT (dùng sự kiện Issue). | Dune | Cả ngày t. |
| `burn_eth` | float64 | đơn vị coin | Lượng burn trên Ethereum trong ngày (Transfer tới 0x0). | Dune | Cả ngày t. |
| `net_issuance_eth` | float64 | đơn vị coin | mint_eth - burn_eth (chỉ Ethereum; không đại diện toàn chain). | Tính | Cả ngày t. |
| `onchain_available` | bool | - | True nếu hàng có ít nhất một biến on-chain trực tiếp. False => chỉ có proxy DefiLlama. | Pipeline | - |
| `price_src` | string | - | Nguồn của price/high/low hàng này: yahoo, cryptocompare, hoặc binance_usdt (chỉ coin bật binance_fallback; giá quy USDT nên lệch nhẹ so với USD). | Pipeline | - |
| `hl_fixed` | bool | - | True nếu high/low đã bị chỉnh vì mâu thuẫn với open/close. | Pipeline | - |
| `is_missing_price` | bool | - | True nếu không có giá ngày đó ở cả hai nguồn (hàng được giữ để lưới ngày liên tục, các cột giá = NaN). | Pipeline | - |
| `src_disagree` | bool | - | True nếu /close Yahoo - close CryptoCompare/ / close > 0.5%. Nên kiểm tra thủ công các ngày này. | Pipeline | - |
| `cc_close` | float64 | USD | Giá đóng cửa CryptoCompare (đối chiếu). | CryptoCompare | - |
| `bn_close` | float64 | USDT | Giá đóng cửa Binance, quy về USDT chứ không phải USD (đối chiếu/robustness). | Binance | - |
| `bn_high` | float64 | USDT | High Binance (USDT). | Binance | - |
| `bn_low` | float64 | USDT | Low Binance (USDT). | Binance | - |

## 7. Phạm vi dữ liệu từng coin (cần ghi vào báo cáo)

| Coin | Vai trò | Ghi chú |
|---|---|---|
| USDT, USDC, DAI | core | Từ 01/01/2020; ~2.480 ngày. On-chain (Curve 3pool) áp dụng được |
| BUSD | extension | Yahoo còn báo giá sau 15/12/2023 (không phải giá đứng yên) nhưng volume sụt từ ~4×10⁸ xuống ~4×10⁶ (trung vị) rồi ~1×10⁶/ngày: nhãn giai đoạn sau kém tin cậy |
| FDUSD | extension | Từ 07/2023, ~1.100 ngày |
| UST | case_study | Chỉ 141 ngày (24/12/2021 → 13/05/2022), giá Binance quy **USDT**; chuỗi dừng khi sụp đổ chưa kết thúc (13/05: 0,2458) → event-study không thấy đáy. Không dùng cho mô hình thống kê chung |

## 8. Phát hiện chất lượng dữ liệu & cách đã xử lý

| Vấn đề | Chi tiết | Xử lý |
|---|---|---|
| CoinGecko miễn phí chỉ ~365 ngày | Không đủ cho event-study | Không dùng làm nguồn giá chính |
| CryptoCompare trả 401 | Bắt buộc có API key | `.env` + header; cảnh báo nếu thiếu key |
| UST thiếu giá | Yahoo rỗng, CryptoCompare không có thị trường UST/USD | Fallback Binance (USDT) cho coin bật cờ |
| UST supply không tìm thấy | DefiLlama đã đổi tên | Khớp theo từ khóa `terra` → "TerraClassicUSD (USTC, id=3)" (ghi ở cột `note` của source log) |
| **Yahoo nhiễu đầu 2020** | 244/282 ngày lệch nguồn nằm ở 2020, 30 ngày đầu 2021; từ ~04/2021 gần như hết. USDC 30/01–03/02/2020 Yahoo 1,02–1,04 còn CryptoCompare/Binance ≈ 1,00; 12, 17, 18/03/2020 cũng vậy | Cột đồng thuận `*_min`; khuyến nghị nhãn chính dùng `peg_deviation_min`, nhãn Yahoo làm kiểm tra độ nhạy |
| BUSD tưởng là dữ liệu cũ | Đã kiểm tra: giá và volume vẫn biến động | Bỏ mốc cắt 15/12/2023 |
| 2 giá ngoài [0,5; 1,5] | UST 12/05 (0,3635) và 13/05/2022 (0,2458): depeg thật | QC để WARN thay vì FAIL |
| Biến thị trường lệch giữa coin | Tính theo lưới từng coin làm coin muộn mất ngày đầu | Tính trên chuỗi đầy đủ + warm-up; QC kiểm tra |
| Panel đổi mỗi ngày chạy | `end = hôm qua` | Đóng băng `sample.end` |
| TVL = 0 trước ~2018 | Dữ liệu gốc DefiLlama; nằm ngoài mẫu | `tvl_change` chia 0 → NaN |
| Dune không dùng được ở gói miễn phí | Truy vấn trên web cần Dune Pro; chạy SQL tự động qua API không có ở gói miễn phí (tài liệu Dune: free engine chỉ cho thực thi thủ công trên trình soạn thảo) | Chuyển sang API công khai của Curve (không key): `fetch_curve.py`; Dune giữ làm tùy chọn |
| File Dune không được nhận | Loader cũ chỉ nhận đúng tên file | Nhận diện theo **tên cột**; chịu được tên lạ, hậu tố `(1)`, dấu `;`, phẩy thập phân, BOM; `check_onchain` liệt kê mọi file đã thấy |
| Tải Dune thiếu một năm | Số dư tích lũy sẽ sai mà không báo lỗi | Chốt chặn trong `load_onchain` (đầu muộn / khoảng trống / số dư âm → bỏ qua Curve và báo lý do); `fetch_onchain` liệt kê năm lỗi và cho chạy lại tiếp tục |
| 1 test lỗi trên Windows | Test ghi file chữ tiếng Việt không chỉ định encoding; Windows mặc định không chứa được ký tự (lỗi của test, không phải code chính) | Chỉ định `encoding="utf-8"`; đã chạy cả bộ test với mã hóa mặc định kiểu Windows (`PYTHONUTF8=0 LC_ALL=C`) |
| pandas 3 dùng kiểu `str` | Làm hỏng bước đổi dấu phẩy thập phân (test bắt được) | Kiểm tra bằng `is_numeric_dtype` |

## 9. Nhật ký thay đổi

| Lần | Nội dung |
|---|---|
| 1 | Pipeline đầu tiên: tải Yahoo/CryptoCompare/Binance/DefiLlama, panel daily UTC, parquet, data dictionary + source log tự sinh, QC, 4 test |
| 2 | Phát hiện CryptoCompare cần key; thêm fallback Binance cho UST; khớp DefiLlama theo từ khóa; QC "không có đối chiếu" và "giá đứng yên"; (cắt BUSD tạm thời — sau đó **bỏ**) |
| 3 | QC: giá ngoài khoảng → WARN kèm danh sách; thêm hai bảng rà soát `review_*.csv` |
| 4 | Thêm cột đồng thuận `peg_deviation_cc/min` rồi `peg_deviation_hl_cc/min` sau khi xác minh Yahoo nhiễu đầu 2020 |
| 5 | Bỏ `last_valid_date` của BUSD sau khi kiểm tra Yahoo BUSD |
| 6 | `.env` + `.gitignore` + `.env.example`; key qua header; che key trong log |
| 7 | Biến thị trường và DefiLlama tính trên chuỗi đầy đủ; warm-up BTC/ETH; đóng băng `sample.end`; QC nhất quán biến thị trường (BTC/ETH cache đổi tên `*_warm.csv`) |
| 8 | On-chain: gộp nhiều file Dune; cột `dex_pool_size`; `check_onchain.py` |
| 9 | Loader on-chain nhận diện theo tên cột; `check_onchain` liệt kê mọi file; sửa lỗi pandas 3; bắt đầu duy trì CODE_NOTES.md |
| 10 | Thêm `fetch_onchain.py` (Dune API: kiểm tra địa chỉ, tải theo năm, tiếp tục từ chỗ dừng, thử lại khi 429, dừng khi lỗi quyền, bỏ qua êm khi không có key); chốt chặn dữ liệu Curve thiếu/sai trong `load_onchain`; `run_all.bat`; `.env.example` thêm `DUNE_API_KEY`; test 21/21 |
| 11 | Sửa test lỗi trên Windows (thiếu encoding); thêm proxy on-chain từ DefiLlama: `supply_eth`, `supply_eth_change`, `supply_eth_share` (không cần key Dune); test 23/23 |
| 12 | **(lần này)** Dune không dùng được ở gói miễn phí → thêm `fetch_curve.py` (API công khai của Curve, không key, kèm `--probe`); `load_onchain` nhận số dư trực tiếp (loại `balances`: day, symbol, balance) và ưu tiên hơn dữ liệu Dune, có độ trễ cấu hình được và chốt chặn đơn vị/số âm/thiếu coin; `run_all.bat` đổi sang `fetch_curve`; `check_onchain` in trạng thái gần nhất và lưu ý lệch cấu trúc; test 27/27 |
| 9b | *(chi tiết lần 9, giữ để tham khảo)* Loader on-chain nhận diện theo tên cột; `check_onchain` liệt kê mọi file và cảnh báo khi dữ liệu Dune ngắn hơn mẫu; sửa lỗi pandas 3; **bắt đầu duy trì CODE_NOTES.md**; test 15/15 |

## 10. Giới hạn đã biết & việc còn mở

- **API Curve chưa được gọi thật khi viết code:** đường dẫn endpoint lấy từ tài liệu OpenAPI của Curve (`/v1/snapshots/{chain}/{address}/tvl`, tham số `start`, `end`, `unit`, tối đa 300 bản ghi/lần), nhưng **tên trường của phản hồi chưa được xác minh**; bộ đọc tự dò nhiều dạng và code chỉ được test bằng phản hồi giả. Chạy `python -m src.data.fetch_curve --probe` trước và gửi kết quả nếu báo không đọc được. Chưa rõ: ngày đầu có dữ liệu (có thể muộn hơn 09/2020), mốc cắt của snapshot ngày (nên mặc định trễ 1 ngày), giới hạn tốc độ, và API có thể thay đổi/ngừng. Địa chỉ pool đã đối chiếu với Etherscan (Curve DAI/USDC/USDT Pool). Hạn chốt giữ/cắt on-chain: 12h 19/10.
- **Mint/burn và exchange flows vẫn chưa có** nếu không có Dune; phương án thay thế chưa tích hợp: Google BigQuery (dataset công khai Ethereum; sandbox miễn phí, chưa kiểm tra lại giới hạn hiện hành) hoặc các dịch vụ tương tự. Proxy DefiLlama (`supply_eth*`) là phương án hiện dùng.
- **Phụ lục E** chưa rõ yêu cầu.
- **Quy tắc cho code sau này:** mọi đọc/ghi text phải chỉ định `encoding=` (Windows mặc định không phải UTF-8); thử bộ test bằng `PYTHONUTF8=0 LC_ALL=C pytest`.
- `supply_eth*` là proxy: gồm cả chuyển qua cầu nối; USDT có phần lớn supply ở chuỗi khác (không phải Ethereum) nên tín hiệu yếu; cột `supply_eth` phải có dữ liệu thật trong `llama_supply_<COIN>.csv` (chưa xác minh trên dữ liệu thật của bạn).
- DefiLlama (miễn phí) **không** có tỷ trọng token trong từng pool DEX nên không thay được `dex_imbalance`; dữ liệu pool-level của DefiLlama (TVL pool qua yields API, thành phần token cấp protocol) chưa được tích hợp vì chưa xác minh cấu trúc phản hồi.
- `run_all.bat` mới được viết, chưa chạy thử trên Windows thật (chỉ kiểm tra từng lệnh Python riêng lẻ).
- **Yahoo nhiễu đầu 2020 mới được xác minh cho USDC**; DAI và USDT thiếu nguồn thứ ba. High/Low Yahoo của các coin khác chưa kiểm tra.
- **Tần suất daily.** Đề khuyến nghị hourly và nhãn h1/h6/h24; sheet phân công dùng ngày. Chưa có panel hourly.
- **Không có bid-ask spread lịch sử**; `turnover` và `range_hl` chỉ là proxy cho `liquidity`.
- **Exchange flows và sentiment** chưa thu thập (đề ghi "nếu truy xuất được" / "extension").
- **Mốc thời gian của DefiLlama** chưa kiểm chứng; mặc định trễ 1 ngày là lựa chọn bảo thủ.
- **TVL là biến toàn thị trường**, không riêng từng coin.
- **CryptoCompare đầu 2020** có vẻ làm tròn ~0,001 (quan sát ở USDC): ổn với ngưỡng ≥ 0,5%.
- Nguồn giá trộn (Yahoo chủ yếu; CryptoCompare lấp ngày cuối; Binance cho UST): luôn đọc `price_src`.
- Kiểm tra "giá đứng yên" và "đối chiếu CryptoCompare" chỉ có ý nghĩa khi nguồn tương ứng tải thành công; đọc kèm `source_log_raw.csv`.

## 11. Ghi chú bàn giao cho các vai trò sau

- **Code 2:** nhãn chính nên dựa trên `peg_deviation_min` / `peg_deviation_hl_min`, nhãn từ `peg_deviation` làm kiểm tra độ nhạy; lọc theo `role`; loại UST khỏi mô hình thống kê chung; cân nhắc loại BUSD sau 12/2023 hoặc lọc theo `turnover`;
  rolling volatility của giá tự tính từ `price` (lưới ngày liên tục); dùng `onchain_available` và `dex_pool_size` khi dùng `dex_imbalance`; cẩn thận hàng có `is_missing_price`, `supply_ffilled`, `src_disagree`.
  Thử nghiệm mẫu bắt đầu từ 05/2021 làm kiểm tra độ nhạy (từ đó các nguồn nhất quán).
- **Viết 1 (Mục 3, Phụ lục A):** dùng `outputs/tables/data_dictionary.csv` và mục 5–8 của file này.
- **Viết 2 (Phụ lục G, event table):** chuỗi UST dừng 13/05/2022 → cần bổ sung mức giá từ nguồn khác; xem `review_price_outliers.csv`.
- **Code 3/4:** panel là daily và không cân bằng; không có nhãn; UST chỉ có 141 ngày.
