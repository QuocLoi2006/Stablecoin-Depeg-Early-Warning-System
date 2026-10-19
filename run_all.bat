@echo off
REM Chay toan bo pipeline Code 1: cai thu vien -> tai so du Curve 3pool -> dung panel -> kiem tra -> test.
REM Khong can key Dune. Chi can key CryptoCompare trong file .env (xem .env.example). Co the nhap dup file nay.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

echo === 1/4 Cai thu vien ===
python -m pip install -r requirements.txt
if errorlevel 1 goto :loi

echo.
echo === 2/4 Tai so du Curve 3pool tu API cong khai cua Curve (khong can key) ===
python -m src.data.fetch_curve
REM Khong dung lai neu buoc nay loi: pipeline van chay, cot on-chain se la NaN (dung proxy DefiLlama).

echo.
echo === 3/4 Dung panel (v1) ===
python -m src.data.build_panel --version v1
if errorlevel 1 goto :loi

echo.
echo === 4/4 Kiem tra on-chain va chay test ===
python -m src.data.check_onchain
python -m pytest -q

echo.
echo XONG. Xem ket qua o: data\processed\panel.parquet va thu muc outputs\tables\
pause
exit /b 0

:loi
echo.
echo CO LOI. Chup lai man hinh va gui de duoc ho tro.
pause
exit /b 1
