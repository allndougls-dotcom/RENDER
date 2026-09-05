@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo   SPOT GRID BACKTEST - INSTALACION Y EJECUCION AUTOMATICA
echo ============================================================
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON=py -3"
) else (
    where python >nul 2>nul
    if %errorlevel% neq 0 (
        echo ERROR: No se ha encontrado Python.
        echo Instala Python 3.11 o 3.12 desde https://www.python.org/downloads/
        echo Marca "Add Python to PATH" durante la instalacion.
        pause
        exit /b 1
    )
    set "PYTHON=python"
)

echo [1/3] Instalando/actualizando dependencias en tu Python de usuario...
%PYTHON% -m pip install --user --upgrade pip
if errorlevel 1 goto :error
%PYTHON% -m pip install --user -r requirements.txt
if errorlevel 1 goto :error

if not exist data mkdir data

echo.
echo [2/3] Descargando BTC/USDT - velas 5m - ultimo periodo configurado...
set "START_DATE=2025-01-01"
set "END_DATE=2026-09-01"
%PYTHON% download_ohlcv.py --exchange binance --symbols "BTC/USDT" --timeframe 5m --start %START_DATE% --end %END_DATE% --output-dir data --format parquet
if errorlevel 1 goto :error

set "DATAFILE=data\BTC_USDT_5m_20250101_20260901.parquet"

echo.
echo [3/3] Ejecutando optimizacion del Spot Grid...
%PYTHON% grid_backtest.py --input "%DATAFILE%" --capital 10000 --fee 0.001 --lower-pcts "5,10,15,20,25,30,40" --upper-pcts "5,10,15,20,25,30,40" --grids "10,15,20,25,30,40,50,60" --grid-types "arithmetic,geometric" --objective calmar --output-prefix BTC_grid_results
if errorlevel 1 goto :error

echo.
echo ============================================================
echo   TERMINADO CORRECTAMENTE
echo ============================================================
echo Resultados:
echo   BTC_grid_results_train_all.csv
echo   BTC_grid_results_top_test.csv
echo.
pause
exit /b 0

:error
echo.
echo ============================================================
echo   ERROR DURANTE LA EJECUCION
echo ============================================================
echo Revisa el mensaje mostrado arriba.
pause
exit /b 1
