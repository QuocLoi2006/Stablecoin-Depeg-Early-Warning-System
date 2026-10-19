# Code 1 — Thu thập, chuẩn hóa dữ liệu, tái lập pipeline (Nhóm 4)

Dựng `data/processed/panel.parquet`: panel **stablecoin × ngày (UTC)** cho dự án *Stablecoin Depeg Early-Warning*.

## Chạy

**Cách nhanh nhất (Windows):** điền key CryptoCompare vào `.env` rồi nhấp đúp **`run_all.bat`**. File này cài thư viện, tải số dư Curve 3pool
(API công khai của Curve, **không cần key**), dựng `panel.parquet`, kiểm tra và chạy test.

**Chuẩn bị `.env`** (sao chép `.env.example`; PowerShell: `Copy-Item .env.example .env`):

```
CRYPTOCOMPARE_API_KEY=key_của_bạn        # bắt buộc (miễn phí): CryptoCompare trả 401 nếu thiếu
```

(`DUNE_API_KEY` chỉ cần nếu có gói Dune trả phí; không dùng thì bỏ qua.) `.env` đã nằm trong `.gitignore`.

Chạy từng bước:

```bash
pip install -r requirements.txt
python -m src.data.fetch_curve --probe               # soi phản hồi thật của API Curve (nên chạy trước lần đầu)
python -m src.data.fetch_curve                       # tải số dư Curve 3pool (không cần key)
python -m src.data.build_panel --version v1          # dựng panel (cache ở data/raw; nguồn lỗi được thử lại)
python -m src.data.build_panel --version v1 --refresh # tải lại toàn bộ
python -m src.data.check_onchain                     # kiểm tra dữ liệu on-chain
pytest -q                                            # test không cần mạng
```

Sau khi chạy:

| Tệp | Dùng cho |
|---|---|
| `data/processed/panel.parquet` (+ `panel_v0/v1.parquet`) | Bàn giao cho Code 2 |
| `outputs/tables/data_dictionary.csv` | Khung **Phụ lục A** (Viết 1 dùng cho Mục 3) |
| `outputs/tables/source_log_raw.csv` | Khung **Phụ lục B**: nguồn, endpoint, giờ tải, số dòng, ngày đầu/cuối, lỗi |
| `outputs/tables/coverage_by_coin.csv`, `qc_report.csv` | Độ phủ và kiểm tra chất lượng, dán vào Phụ lục B/E |

## Nếu API Curve không dùng được?

Panel vẫn đầy đủ: các biến on-chain trực tiếp (`dex_imbalance`, `dex_pool_size`; `mint_eth`, `burn_eth` cần Dune) sẽ là NaN, và nhóm dùng proxy DefiLlama (`supply_change`, `tvl_change`, `supply_eth_change`, `supply_eth_share`). Ghi rõ điều này trong báo cáo.

## Quy ước quan trọng (đã ghi trong data dictionary)

1. Mọi mốc thời gian là **UTC 00:00**. Hàng `(stablecoin, t)` chỉ chứa thông tin biết được đến hết ngày t.
2. Lưới ngày **liên tục** trong từng coin (ngày thiếu giá được giữ lại với `is_missing_price = True`), để Code 2 dùng `shift(-h)` tạo nhãn đúng.
3. Dữ liệu DefiLlama (supply, TVL) bị **trễ `llama_lag_days` ngày** (mặc định 1) để loại trừ look-ahead. Đổi trong `config/config.yaml` nếu nhóm xác nhận mốc chụp.
4. Nhãn `depeg_*` **không nằm trong panel**; Code 2 tạo từ `peg_deviation` / `peg_deviation_hl`.
5. `bn_*` (Binance) quy về USDT, không phải USD: chỉ dùng đối chiếu/robustness.
6. **Ngày cuối đóng băng** (`sample.end` trong config): panel không đổi khi chạy lại vào ngày khác. Khi chốt v1 hãy đặt bằng ngày UTC gần nhất mà Yahoo và DefiLlama đều có dữ liệu.
7. Biến cấp thị trường (`btc_*`, `eth_*`, `tvl*`) và `supply_change` được tính trên **toàn bộ chuỗi nguồn** rồi mới đưa vào lưới từng coin; BTC/ETH tải sớm hơn mẫu 30 ngày (`warmup_days`). Nhờ vậy cùng một ngày mọi coin có cùng giá trị, và coin niêm yết muộn không mất dữ liệu đầu kỳ. QC có kiểm tra điều này.
8. Cột `role`: `core` (USDT, USDC, DAI), `extension` (BUSD, FDUSD), `case_study` (UST, bị cắt tại `last_valid_date` để nhãn không bị "depeg vĩnh viễn" sau sụp đổ).

## On-chain (Phần C)

Chạy 2 query trong `src/data/dune_queries.sql` trên Dune, lưu CSV vào `data/raw/onchain/`, rồi chạy lại pipeline. Thiếu file thì `dex_imbalance`, `mint_eth`, `burn_eth` = NaN và `onchain_available = False`; khi đó mô hình dùng proxy DefiLlama (`supply_change`, `tvl_change`). **Chốt giữ/cắt trước 12h 19/10.**

## Việc cần chốt với nhóm

- **Phụ lục E** yêu cầu gì (đề chưa nêu) — nếu là hướng dẫn tái lập pipeline thì README + `qc_report.csv` là nền.
- Tần suất: đề khuyến nghị hourly, nhưng phân công đang dùng m, h theo **ngày** → panel này là daily.
- Danh sách coin cuối cùng và có đưa BUSD, FDUSD vào mẫu mô hình hay chỉ để mô tả.
- Sau lần chạy đầu: rà các ngày `src_disagree = True` và các coin có `first_date` muộn bất thường trong `coverage_by_coin.csv`.
