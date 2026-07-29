/* お話の記憶 PRO — Service Worker（/pro/ スコープ限定）
   旧アプリ(/)には一切干渉しない。アプリシェルはネットワーク優先(更新反映)、
   絵カード画像・音声mp3はキャッシュ優先(オフライン再生・即時反復)。 */
const VERSION = "ohanashi-pro-v2";   // v2: 音声をキャッシュ優先で返すのをやめた（iOSで鳴らないため）
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
  "/pro/content/defect_units.json",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(SHELL).then((c) => c.addAll(SHELL_ASSETS).catch(() => {})).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      // 消すのは自分(ohanashi-pro-)の古い版だけ。旧アプリのキャッシュ(ohanashi-v*)は消さない
      Promise.all(keys.filter((k) => k.startsWith("ohanashi-pro-") && k !== SHELL && k !== MEDIA)
                      .map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// この起動中に裏で取り直したURL（同じ素材を何度も取りに行かないため）
const revalidated = new Set();

function isImage(url) {
  return url.pathname.startsWith("/pro/content/images/")        // 正本(1024px PNG・検品UI)
      || url.pathname.startsWith("/pro/content/images_small/"); // 軽量版(448px WebP・こども画面)
}
function isAudio(url) { return url.pathname.endsWith(".mp3"); }

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith("/pro/")) return; // 旧アプリ・外部は素通し

  // Range付き（＝メディア要素が直接読みにきた）は SW が触らずネットワークに任せる。
  // Cache Storage は 206 を保存できず 200 全文しか返せないため、握ると iOS で無音になる。
  if (req.headers.has("range")) return;

  if (isImage(url) || isAudio(url)) {
    // 画像・音声: キャッシュ優先で即返しつつ、裏で取り直して次回に備える（stale-while-revalidate）。
    // 完全なキャッシュ優先だと、作り直した音声に差し替えても古い音声が鳴り続ける
    // （2026-07-26 に28話の音声を差し替えた実績があり、実際に起こりうる）。
    // 同じURLの再取得は1セッション1回だけにして通信量を増やさない。
    e.respondWith(
      caches.open(MEDIA).then((cache) =>
        cache.match(req).then((hit) => {
          const fresh = () =>
            fetch(req).then((res) => { if (res.status === 200) cache.put(req, res.clone()); return res; });
          if (!hit) return fresh().catch(() => hit);
          if (!revalidated.has(url.href)) {
            revalidated.add(url.href);
            e.waitUntil(fresh().catch(() => {}));   // オフラインなら黙って諦める
          }
          return hit;
        })
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
