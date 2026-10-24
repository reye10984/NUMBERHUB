const CACHE_NAME = "numberhub-static-v1";

self.addEventListener("install", event => {
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    (async () => {
      await self.clients.claim();

      const names = await caches.keys();

      await Promise.all(
        names
          .filter(name => name.startsWith("numberhub-") && name !== CACHE_NAME)
          .map(name => caches.delete(name))
      );
    })()
  );
});

self.addEventListener("fetch", event => {
  if (event.request.method !== "GET") return;

  const url = new URL(event.request.url);

  if (url.origin !== self.location.origin) return;

  if (
    url.pathname.startsWith("/static/") &&
    !url.pathname.endsWith("/sw.js")
  ) {
    event.respondWith(
      fetch(event.request).catch(async () => {
        const cached = await caches.match(event.request);
        if (cached) return cached;
        throw new Error("Resource unavailable offline");
      })
    );
  }
});
