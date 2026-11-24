/*
 * Service worker. It exists so incoming-call notifications work on phones
 * (Android only shows notifications through a service worker) and so tapping
 * one brings the app to the front. It deliberately caches nothing: the app is
 * useless without the server, and a cache would only serve stale code.
 */

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windows) => {
      // Each organisation has its own copy of this worker; only raise its own page.
      const scope = self.registration.scope;
      for (const client of windows) {
        if (client.url.startsWith(scope) && !client.url.slice(scope.length).startsWith('o/') && 'focus' in client) return client.focus();
      }
      return self.clients.openWindow(scope);
    }),
  );
});
