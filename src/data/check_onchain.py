"""Kiểm tra nhanh dữ liệu Dune trước khi dùng. Chạy:  python -m src.data.check_onchain

In ra: (1) thư mục đang quét và MỌI file trong đó, (2) mỗi file được nhận diện là gì,
(3) các phép kiểm tra hợp lý của dữ liệu Curve 3pool.
"""
from __future__ import annotations

import pandas as pd
import yaml

from src.data.build_panel import ROOT, load_onchain

CHECK_DATES = ["2021-06-01", "2022-05-12", "2023-03-11", "2024-06-01", "2025-06-01"]


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    folder = (ROOT / cfg["onchain"]["curve3pool_csv"]).parent
    print(f"Thư mục đang quét: {folder}")
    if not folder.exists():
        print("  -> THƯ MỤC KHÔNG TỒN TẠI. Hãy tạo nó rồi đặt file CSV từ Dune vào.")
    else:
        allf = sorted(p.name for p in folder.iterdir() if p.name != ".gitkeep")
        print(f"Các file trong thư mục ({len(allf)}): {allf if allf else 'TRỐNG'}")
        odd = [n for n in allf if not n.lower().endswith(".csv")]
        if odd:
            print(f"  ! File KHÔNG phải .csv (bị bỏ qua): {odd}. Kiểm tra Windows có đang ẩn đuôi file không "
                  "(tên có thể là '.csv.csv' hoặc '.xlsx').")

    oc = load_onchain(cfg)
    print("\nNhận diện từng file:")
    if not oc["report"]:
        print("  (không có file .csv nào)")
    for r in oc["report"]:
        print(f"  - {r['file']}: {r['kind']}, {r['rows']} dòng — {r['detail']}")

    if oc["curve"] is None:
        print("\nChưa có dữ liệu Curve 3pool hợp lệ (cần file có cột day, symbol, net và đủ DAI/USDC/USDT).")
    else:
        imb, size = oc["curve"], oc["curve_size"]
        bal = (imb + 1 / 3).mul(size, axis=0)
        print(f"\nCurve 3pool: {imb.index.min().date()} -> {imb.index.max().date()}, {len(imb)} ngày, cột {list(imb.columns)}")
        end = pd.Timestamp(cfg["sample"]["end"] or pd.Timestamp.now("UTC").date(), tz="UTC")
        if imb.index.max() < end - pd.Timedelta(days=7):
            print(f"  ! Dữ liệu Dune kết thúc {imb.index.max().date()}, sớm hơn cuối mẫu {end.date()} "
                  f"=> dex_imbalance sẽ là NaN sau ngày đó. Hãy xuất lại query tới ngày hiện tại.")
        print("Số ngày số dư ÂM (phải = 0 nếu địa chỉ và lịch sử đúng):", (bal < -1.0).sum().to_dict())
        dates = pd.to_datetime(CHECK_DATES, utc=True)
        print("\nQuy mô pool (tổng ba coin) tại vài thời điểm:")
        print(size.reindex(dates, method="nearest").round(0).to_string())
        print("\nTỷ trọng - 1/3 (dương = pool thừa coin đó):")
        print(imb.reindex(dates, method="nearest").round(3).to_string())
        last = imb.dropna(how="all").iloc[-1]
        print(f"\nTrạng thái gần nhất ({imb.dropna(how='all').index[-1].date()}): quy mô {size.dropna().iloc[-1]:,.0f}; "
              f"tỷ trọng - 1/3 = {last.round(3).to_dict()}")
        print("LƯU Ý: nếu pool đã teo và một coin chiếm tỷ trọng cao kéo dài (lệch CẤU TRÚC, không phải tín hiệu), "
              "Code 2 nên dùng thay đổi/độ lệch so với trung bình trượt thay vì mức tuyệt đối.")
        print("\nKỳ vọng: quanh 11/03/2023 (USDC depeg) USDC nên lệch DƯƠNG rõ rệt; ngày bình thường các cột gần 0.")

    mb = oc["mintburn"]
    if mb is None:
        print("\nChưa có dữ liệu mint/burn (không bắt buộc).")
    else:
        print(f"\nMint/burn: {mb['day'].min().date()} -> {mb['day'].max().date()}, coin {sorted(mb['symbol'].unique())}")
        print(mb.groupby("symbol")[["minted", "burned"]].sum().round(0).to_string())


if __name__ == "__main__":
    main()
