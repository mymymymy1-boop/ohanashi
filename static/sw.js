// おはなしのきおく Service Worker（オフライン用にアプリ本体をキャッシュ）
const C = 'ohanashi-v1';
const SHELL = ['/', '/manifest.webmanifest', '/static/manifest.webmanifest',
  '/static/icon-192.png', '/static/icon-512.png', '/static/apple-touch-icon.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(C).then(c => c.addAll(SHELL).catch(() => {})).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.pathname.startsWith('/api/')) return; // APIはネット必須・キャッシュしない
  if (e.request.mode === 'navigate' || u.pathname === '/') {
    // 画面本体：ネット優先（更新を反映）、失敗時はキャッシュ（オフライン）
    e.respondWith(
      fetch(e.request).then(resp => { const c = resp.clone(); caches.open(C).then(ca => ca.put('/', c)); return resp; })
        .catch(() => caches.match('/'))
    );
    return;
  }
  // それ以外（manifest/アイコン等）：キャッシュ優先
  e.respondWith(
    caches.match(e.request).then(r => r || fetch(e.request).then(resp => {
      const c = resp.clone(); caches.open(C).then(ca => ca.put(e.request, c)); return resp;
    }))
  );
});
