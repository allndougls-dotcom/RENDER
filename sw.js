// Service Worker mínimo de STOCK-RADAR.
//
// Existe para que Chrome en Android reconozca la app como PWA instalable
// ("Instalar aplicación" en vez de solo "Añadir a pantalla de inicio").
// No guarda nada en caché: los datos cambian a diario y deben venir
// siempre del servidor. Todas las peticiones pasan directas a la red.

self.addEventListener('install', () => {
  // Activar la versión nueva sin esperar a que se cierren las pestañas abiertas
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  // Solo GET del propio dominio; el resto (POST a la API, CDNs, logos…)
  // lo gestiona el navegador como si no hubiera Service Worker.
  if (req.method !== 'GET' || new URL(req.url).origin !== self.location.origin) return;

  event.respondWith(
    fetch(req).catch(() => {
      if (req.mode === 'navigate') {
        return new Response(
          '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
          '<body style="background:#07090f;color:#d8ddf0;font-family:monospace;padding:24px">' +
          '<h2>STOCK-RADAR</h2><p>Sin conexión. Vuelve a intentarlo cuando tengas red.</p></body>',
          { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
        );
      }
      return Response.error();
    })
  );
});
