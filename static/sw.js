// おはなしのきおく Service Worker（オフライン用にアプリ本体をキャッシュ）
// 資産やこのファイルを更新したら C の版番号を上げること（古いキャッシュを確実に破棄するため）。
const C = 'ohanashi-v4';
const SHELL = ['/', '/manifest.webmanifest', '/static/manifest.webmanifest',
  '/static/icon-192.png', '/static/icon-512.png', '/static/apple-touch-icon.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(C).then(c => c.addAll(SHELL).catch(() => {})).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  // 消すのは自分(ohanashi-v*)の古い版だけ。PRO(/pro/)のキャッシュ(ohanashi-pro-*)には触らない
  e.waitUntil(caches.keys().then(ks => Promise.all(
    ks.filter(k => k.startsWith('ohanashi-v') && k !== C).map(k => caches.delete(k))
  )).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.pathname.startsWith('/api/')) return; // APIはネット必須・キャッシュしない
  if (u.pathname.startsWith('/pro/')) return;   // PROは専用SW(/pro/sw.js)の担当。ここで音声を握るとiOSで無音になる
  if (e.request.headers.has('range')) return;   // メディアのRange要求は素通し（206はキャッシュに保存できない）
  if (e.request.mode === 'navigate' || u.pathname === '/') {
    // 画面本体：ネット優先（更新を反映）、失敗時はキャッシュ（オフライン）。
    // オフライン起動(start_url='/')で必ずメインアプリが開くよう、キャッシュのキーは常に '/'。
    // ただし保存するのはトップページ('/')の正常応答だけにする。
    // /compare や /pack のナビゲーションで '/' を上書きしない（別画面すり替わり防止）。
    // 401/500 等のエラー応答もキャッシュしない（オフラインでエラー画面固定になるのを防ぐ）。
    e.respondWith(
      fetch(e.request).then(resp => {
        if (resp.ok && u.pathname === '/') {
          const c = resp.clone();
          caches.open(C).then(ca => ca.put('/', c));
        }
        return resp;
      }).catch(() => caches.match('/'))
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
