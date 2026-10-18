# QUALITY SWING: reconstrucción point-in-time

Objetivo: comparar score combinado SIDI > 8.5 vs control 6.5–8.5, TP 10/15%, distintos SL y horizontes, sin alterar FULL/WHITE BIRD.

## Estado
El workflow original terminó con 0 señales porque solo había snapshots recientes, posteriores a los precios históricos usados. Este script solo obtiene fundamentales con fechas `filed` <= fecha de señal; **no genera puntuaciones todavía**.

## Criterios obligatorios
1. Reconstruir cada cierre histórico: precios Open/High/Low/Close/Volume, indicadores de `modules/ingesta/tecnico.py` y `tech_score` histórico.
2. Obtener SEC facts de TODAS las compañías elegibles a la fecha, por `filed` y no por `end`, y reconstruir TTM con periodos correctos, flujos y balances. No incluir revisiones presentadas posteriormente. Calcular estimaciones de ROE, deuda, crecimiento, FCF, comparables sectoriales con cobertura suficiente.
3. Reproducir exactamente `modules/ingesta/scoring.py` (medianas sectoriales, 35/35/30 fundamental, 60/40 score combinado). Completar valoraciones PE/PB y market cap de esa fecha con precios y acciones disponibles entonces; si no, marcar muestra no comparable y NO sustituir por scores actuales.
4. Evitar supervivencia: universo S&P 500 vigente en cada fecha, no solo lista actual; identificar sustituciones y delistings.
5. Entrar en próxima apertura, conservador al tocar SL/TP misma sesión, contabilizar gaps, comisiones, calendario y señales no solapadas.
6. Separar training / holdout por fecha y mostrar N, WR con intervalos, PF, drawdown, duración y sensibilidad sectorial.

## Fuente
`research/sec_pit_fundamentals.py` extrae SEC XBRL CompanyFacts con provenance de fecha `filed`, periodo y accession. Necesita un contacto real `SEC_USER_AGENT` para cumplir política de identificación SEC. No es una fuente suficiente por sí sola para valoraciones sectoriales completas.

## Bloqueo
Hasta integrar históricos financieros y de composición con cobertura contrastada, no ejecutar un análisis publicable de WR. No emitir un porcentaje a partir de un score proyectado retrospectivamente.
