@echo off
chcp 65001 >nul
title SIDI STOCKS - Launcher
color 0A

cd /d "%~dp0"

set RENDER_URL=https://render-s7w8.onrender.com
set UPDATE_TOKEN=stock-radar-2026

if not exist "venv\Scripts\activate.bat" (
    echo.
    echo  Primera vez: creando entorno virtual...
    python -m venv venv
    call venv\Scripts\activate.bat
    echo  Instalando dependencias...
    pip install -r requirements.txt
    echo  Listo.
) else (
    call venv\Scripts\activate.bat
    echo  Entorno virtual activado.
)

:menu
cls
echo.
echo  ================================================
echo   SIDI STOCKS - Launcher
echo  ================================================
echo.
echo   -- DATOS LOCALES --
echo   [1] Actualizar datos completos       ~20 min
echo   [2] Actualizar solo tecnico           ~5 min
echo   [3] Test rapido 20 empresas           ~2 min
echo.
echo   -- APP --
echo   [4] Abrir app local (HTML)
echo   [5] Abrir app online (Render)
echo   [6] Forzar actualizacion en Render (remota)
echo.
echo   -- BACKTEST --
echo   [7] Backtest rapido (comparar variantes)
echo   [8] Backtest completo (503 tickers)
echo   [9] Walk-Forward Analysis
echo.
echo   -- MODOS RAPIDOS --
echo   [A] MODO DIA    (datos completos + abrir app local)
echo   [B] MODO RAPIDO (tecnico + abrir app local)
echo.
echo   [0] Salir
echo  ================================================
echo.
set /p opcion="  Elige una opcion: "

if /i "%opcion%"=="1" goto full
if /i "%opcion%"=="2" goto tech
if /i "%opcion%"=="3" goto test
if /i "%opcion%"=="4" goto app_local
if /i "%opcion%"=="5" goto app_render
if /i "%opcion%"=="6" goto trigger_render
if /i "%opcion%"=="7" goto backtest_rapido
if /i "%opcion%"=="8" goto backtest_full
if /i "%opcion%"=="9" goto backtest_wf
if /i "%opcion%"=="A" goto modo_dia
if /i "%opcion%"=="B" goto modo_rapido
if "%opcion%"=="0" goto end
goto menu

:full
echo.
echo  Actualizando datos completos (~20 min)...
echo.
python main_ingesta.py
echo.
echo  Datos actualizados.
pause
goto menu

:tech
echo.
echo  Actualizando solo tecnico (~5 min)...
echo.
python main_ingesta.py --solo-tech
echo.
echo  Tecnico actualizado.
pause
goto menu

:test
echo.
echo  Test rapido (20 empresas)...
echo.
python main_ingesta.py --test
echo.
echo  Test completado.
pause
goto menu

:app_local
echo.
echo  Abriendo app local en el navegador...
call :abrir_app_local
pause
goto menu

:app_render
echo.
echo  Abriendo app online (Render) en el navegador...
start "" "%RENDER_URL%"
pause
goto menu

:trigger_render
echo.
echo  Forzando actualizacion de datos en Render...
echo  (el servidor descargara los datos mas recientes en segundo plano, ~20 min)
echo.
powershell -Command "try { $r = Invoke-WebRequest -Uri '%RENDER_URL%/trigger' -Method POST -Headers @{'X-Update-Token'='%UPDATE_TOKEN%';'Content-Type'='application/json'} -Body '{}' -UseBasicParsing; Write-Host $r.Content } catch { Write-Host 'Error al conectar con Render:' $_.Exception.Message -ForegroundColor Red }"
echo.
pause
goto menu

:backtest_rapido
echo.
echo  Backtest rapido - comparando variantes (~5-10 min)...
echo.
python backtest.py --rapido
echo.
pause
goto menu

:backtest_full
echo.
echo  Backtest completo - 503 tickers (~30-45 min, con pausas anti rate-limit)...
echo.
python backtest.py
echo.
pause
goto menu

:backtest_wf
echo.
set /p variante="  Nombre de variante para Walk-Forward (Enter = todas): "
if "%variante%"=="" (
    python backtest.py --walkforward
) else (
    python backtest.py --walkforward --solo %variante%
)
echo.
pause
goto menu

:modo_dia
echo.
echo  MODO DIA: datos completos + abrir app local
echo.
echo  Paso 1/2: Actualizando datos...
python main_ingesta.py
echo.
echo  Paso 2/2: Abriendo app...
call :abrir_app_local
echo.
echo  Todo listo.
pause
goto menu

:modo_rapido
echo.
echo  MODO RAPIDO: tecnico + abrir app local
echo.
echo  Paso 1/2: Actualizando tecnico...
python main_ingesta.py --solo-tech
echo.
echo  Paso 2/2: Abriendo app...
call :abrir_app_local
echo.
echo  Todo listo.
pause
goto menu

:abrir_app_local
if exist "%~dp0stock-radar-v3.html" (
    start "" "%~dp0stock-radar-v3.html"
    exit /b
)
if exist "%USERPROFILE%\Downloads\stock-radar-v3.html" (
    start "" "%USERPROFILE%\Downloads\stock-radar-v3.html"
    exit /b
)
echo  No encontre stock-radar-v3.html en esta carpeta ni en Downloads.
exit /b

:end
echo.
echo  Hasta luego.
timeout /t 2 >nul
