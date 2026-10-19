-- Dune (DuneSQL). Chạy từng query, xuất CSV, lưu vào data/raw/onchain/ đúng tên file dưới đây.
-- LƯU Ý: kiểm tra lại địa chỉ pool và địa chỉ token trên Etherscan trước khi chạy.
--   Curve 3pool          : 0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7
--   USDT                 : 0xdAC17F958D2ee523a2206206994597C13D831ec7  (6 decimals)
--   USDC                 : 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48  (6 decimals)
--   DAI                  : 0x6B175474E89094C44Da98b954EedeAC495271d0F  (18 decimals)

-- ===== Query 1 -> data/raw/onchain/curve3pool_net_transfers.csv  (cột: day, symbol, net) =====
-- Nếu tách theo năm, đặt tên curve3pool_net_transfers_2020.csv, ..._2021.csv, ... (pipeline tự gộp, không cần nối tay).
-- Số dư pool = tổng tích lũy (chuyển vào - chuyển ra); pipeline tự cumsum và tính tỷ trọng - 1/3.
SELECT
  date_trunc('day', evt_block_time) AS day,
  CASE contract_address
    WHEN 0xdAC17F958D2ee523a2206206994597C13D831ec7 THEN 'USDT'
    WHEN 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48 THEN 'USDC'
    WHEN 0x6B175474E89094C44Da98b954EedeAC495271d0F THEN 'DAI'
  END AS symbol,
  SUM(
    (CASE WHEN "to"   = 0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7 THEN CAST(value AS DOUBLE) ELSE 0 END)
  - (CASE WHEN "from" = 0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7 THEN CAST(value AS DOUBLE) ELSE 0 END)
  ) / POWER(10, CASE WHEN contract_address = 0x6B175474E89094C44Da98b954EedeAC495271d0F THEN 18 ELSE 6 END) AS net
FROM erc20_ethereum.evt_Transfer
WHERE contract_address IN (
        0xdAC17F958D2ee523a2206206994597C13D831ec7,
        0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,
        0x6B175474E89094C44Da98b954EedeAC495271d0F)
  AND ("to" = 0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7 OR "from" = 0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7)
  AND evt_block_time >= TIMESTAMP '2020-01-01'   -- nếu bị timeout: tách theo năm, thêm  AND evt_block_time < TIMESTAMP '2021-01-01'  rồi đổi mốc cho từng năm
GROUP BY 1, 2
ORDER BY 1, 2;

-- ===== Query 2 -> data/raw/onchain/mint_burn_eth.csv  (cột: day, symbol, minted, burned) =====
-- Cũng có thể tách năm: mint_burn_eth_2020.csv, mint_burn_eth_2021.csv, ...
-- USDC và DAI: mint = Transfer từ 0x0, burn = Transfer tới 0x0. USDT không phát qua Transfer (dùng sự kiện Issue/Redeem) nên bỏ qua.
SELECT
  date_trunc('day', evt_block_time) AS day,
  CASE contract_address
    WHEN 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48 THEN 'USDC'
    WHEN 0x6B175474E89094C44Da98b954EedeAC495271d0F THEN 'DAI'
  END AS symbol,
  SUM(CASE WHEN "from" = 0x0000000000000000000000000000000000000000 THEN CAST(value AS DOUBLE) ELSE 0 END)
    / POWER(10, CASE WHEN contract_address = 0x6B175474E89094C44Da98b954EedeAC495271d0F THEN 18 ELSE 6 END) AS minted,
  SUM(CASE WHEN "to"   = 0x0000000000000000000000000000000000000000 THEN CAST(value AS DOUBLE) ELSE 0 END)
    / POWER(10, CASE WHEN contract_address = 0x6B175474E89094C44Da98b954EedeAC495271d0F THEN 18 ELSE 6 END) AS burned
FROM erc20_ethereum.evt_Transfer
WHERE contract_address IN (
        0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,
        0x6B175474E89094C44Da98b954EedeAC495271d0F)
  AND ("from" = 0x0000000000000000000000000000000000000000 OR "to" = 0x0000000000000000000000000000000000000000)
  AND evt_block_time >= TIMESTAMP '2020-01-01'
GROUP BY 1, 2
ORDER BY 1, 2;
