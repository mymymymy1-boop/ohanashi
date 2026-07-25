/* お話の記憶 PRO — Service Worker（/pro/ スコープ限定）
   旧アプリ(/)には一切干渉しない。アプリシェルはネットワーク優先(更新反映)、
   絵カード画像・音声mp3はキャッシュ優先(オフライン再生・即時反復)。 */
const VERSION = "ohanashi-pro-v1";
const SHELL = VERSION + "-shell";
const MEDIA = VERSION + "-media";
const SHELL_ASSETS = [
  "/pro/play",
  "/pro/play.webmanifest",
  "/pro/icon-192.png",
  "/pro/icon-512.png",
  "/pro/content/review_manifest.json",
  "/pro/content/image_alias_map.json",
  "/pro/content/choice_labels.json",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(SHELL).then((c) => c.addAll(SHELL_ASSETS).catch(() => {})).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== SHELL && k !== MEDIA).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

function isMedia(url) {
  return url.pathname.startsWith("/pro/content/images/") || url.pathname.endsWith(".mp3");
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith("/pro/")) return; // 旧アプリ・外部は素通し

  if (isMedia(url)) {
    // 画像・音声: キャッシュ優先 → なければ取得してキャッシュ
    e.respondWith(
      caches.open(MEDIA).then((cache) =>
        cache.match(req).then((hit) =>
          hit || fetch(req).then((res) => { if (res.ok) cache.put(req, res.clone()); return res; })
                       .catch(() => hit)
        )
      )
    );
    return;
  }

  // アプリシェル・JSON: ネットワーク優先 → 失敗時キャッシュ（オフライン起動）
  e.respondWith(
    fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(SHELL).then((c) => c.put(req, copy)); }
      return res;
    }).catch(() => caches.match(req).then((hit) => hit || caches.match("/pro/play")))
  );
});
