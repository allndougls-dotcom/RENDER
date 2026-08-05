# SIDI STOCKS · Cloud Server

Pipeline completo desplegado en **Render** (gratis).
Sirve datos a la app móvil/escritorio y se actualiza automáticamente cada día.

---

## Despliegue en Render — paso a paso

### PASO 1: Subir este código a GitHub
1. Crea un repo (puede ser el mismo que ya tenías) y sube TODO el contenido de esta carpeta a la RAÍZ del repo (no dentro de una subcarpeta).

### PASO 2: Crear el servicio en Render
1. Ve a **render.com** → Sign up con GitHub
2. **New +** → **Web Service**
3. Conecta tu repo `stock-radar-cloud` (o el nombre que tenga)
4. Render detectará automáticamente `render.yaml` — si no, configura manualmente:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120`
   - **Plan:** Free

### PASO 3: Variables de entorno
En Render → tu servicio → **Environment** → añade:

| Variable | Valor |
|---|---|
| `RENDER` | `true` |
| `UPDATE_TOKEN` | un token secreto que elijas |
| `ANTHROPIC_API_KEY` | tu key de Anthropic |
| `OPENAI_API_KEY` | tu key de OpenAI |
| `TAVILY_API_KEY` | tu key de Tavily |
| `ALERT_EMAIL_TO` | tu email destino |
| `ALERT_EMAIL_FROM` | tu Gmail que envía |
| `ALERT_EMAIL_PASS` | contraseña de aplicación Gmail (16 caracteres) |

### PASO 4: Obtener tu URL
Render te da automáticamente una URL del tipo:
```
https://sidi-stocks-cloud.onrender.com
```

### PASO 5: Primera ingesta de datos
```powershell
Invoke-WebRequest -Uri "https://TU-URL.onrender.com/trigger" -Method POST -Headers @{"X-Update-Token"="tu-token-secreto";"Content-Type"="application/json"} -Body "{}" -UseBasicParsing
```
Tarda ~20 minutos. Verifica progreso en Render → **Logs**.

### PASO 6: Verificar
```
https://TU-URL.onrender.com/status
```

### PASO 7: Instalar en Android
1. Chrome → `https://TU-URL.onrender.com/mobile`
2. Menú ⋮ → "Añadir a pantalla de inicio"

### Escritorio completo
```
https://TU-URL.onrender.com/desktop
```

---

## ⚠️ Diferencia importante vs Railway: el "sueño" del plan gratuito

En el plan gratuito de Render, el servicio se **duerme tras 15 minutos sin tráfico web** y tarda ~30-50 segundos en despertar la próxima vez que alguien accede a la URL.

**Esto NO afecta al scheduler diario** (el pipeline de las 08:00 UTC corre igual, porque es un proceso en segundo plano dentro de la misma app, no depende de una visita externa) **siempre que el servicio esté despierto en ese momento**. Para garantizarlo:

- Opción simple: visita la URL manualmente cada mañana antes de las 08:00 UTC.
- Opción automática: usa un servicio gratuito de "ping" externo (p. ej. UptimeRobot o cron-job.org) que haga una petición GET a `/status` cada 10 minutos, manteniendo el servicio despierto 24/7.

---

## Endpoints disponibles

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/` | GET | Info general |
| `/status` | GET | Estado del servidor |
| `/market` | GET | SPY + VIX + régimen |
| `/data` | GET | CSV completo como JSON |
| `/hot` | GET | Solo setups HOT |
| `/mobile` | GET | App móvil PWA |
| `/desktop` | GET | App de escritorio completa |
| `/trigger` | POST | Forzar actualización (requiere token) |

## Pipeline diario automático (08:00 UTC)
1. Ingesta de datos (503 empresas)
2. Recarga de cache
3. Agente Claude — análisis cuantitativo de cada HOT
4. Agente GPT News — inteligencia empresarial (Tavily + GPT-4o)
5. Email resumen combinado (⭐ Alta Convicción cuando ambos coinciden)
