# Spot Grid Backtest

Backtest y optimización de un bot Spot Grid con histórico OHLCV descargado mediante CCXT.

## Windows: ejecución rápida

1. Instala Python 3.11 o 3.12 y marca **Add Python to PATH**.
2. Haz doble clic en `EJECUTAR_GRID.bat`.
3. El `.bat` instala las dependencias en el Python del usuario, descarga BTC/USDT y ejecuta el optimizador.

No hace falta crear ni activar un `.venv`.

## Archivos

- `download_ohlcv.py`: descarga histórico OHLCV.
- `grid_backtest.py`: optimiza el rango, número de grids y grid aritmético/geométrico.
- `requirements.txt`: dependencias.
- `EJECUTAR_GRID.bat`: ejecución local en Windows.
- `.github/workflows/grid-backtest-smoke.yml`: prueba automatizada en GitHub Actions.

## Nota sobre el backtest

El modelo usa una hipótesis de recorrido intravela basada en OHLC. Para validar las mejores configuraciones, es preferible repetir el análisis con velas de 1 minuto y después hacer walk-forward/out-of-sample.
