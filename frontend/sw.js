/**
 * Service Worker v2.0.2 — 캐시 무효화 강화 버전
 * 
 * 핵심 변경:
 * - HTML(/, /index.html)은 절대 캐시하지 않음 → 항상 최신 받기
 * - 옛 캐시 발견 시 즉시 모두 삭제
 * - 정적 자원만 짧은 시간 캐싱
 */

const CACHE_VERSION = 'v2.0.3-clean';
const CACHE_NAME = `real-estate-app-${CACHE_VERSION}`;

// 정적 자원만 캐싱 (HTML 제외)
const STATIC_ASSETS = [
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png',
  '/icon.svg',
  '/apple-touch-icon.png',
  '/kiwoom_ci.jpg',
];

// ============================================================
// 설치 — 모든 옛 캐시 즉시 삭제 + 새 자원 캐싱
// ============================================================
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.keys()
      .then(names => Promise.all(names.map(n => caches.delete(n))))  // 옛 캐시 모두 삭제
      .then(() => caches.open(CACHE_NAME))
      .then(cache => Promise.all(
        STATIC_ASSETS.map(url => cache.add(url).catch(() => null))
      ))
  );
  self.skipWaiting();
});

// ============================================================
// 활성화 — 즉시 모든 클라이언트 제어 + 옛 캐시 정리
// ============================================================
self.addEventListener('activate', (event) => {
  event.waitUntil(
    Promise.all([
      // 옛 캐시 삭제
      caches.keys().then(names =>
        Promise.all(names.filter(n => n !== CACHE_NAME).map(n => caches.delete(n)))
      ),
      // 모든 클라이언트 즉시 제어
      self.clients.claim(),
    ])
  );
});

// ============================================================
// fetch — HTML은 항상 네트워크, 정적 자원만 캐시
// ============================================================
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  
  // POST 등 비-GET은 캐싱 안 함
  if (event.request.method !== 'GET') return;
  
  // API 요청은 항상 네트워크 (캐싱 안 함)
  if (url.pathname.startsWith('/api/')) return;
  
  // HTML/루트는 항상 네트워크 (캐싱 안 함) - 가장 중요!
  if (url.pathname === '/' || url.pathname.endsWith('.html') || event.request.mode === 'navigate') {
    return;  // 기본 동작 = 네트워크
  }
  
  // 정적 자원 (이미지, manifest 등): 캐시 우선, 없으면 네트워크
  event.respondWith(
    caches.match(event.request).then(cached => {
      if (cached) return cached;
      return fetch(event.request).then(response => {
        if (response && response.status === 200 && response.type === 'basic') {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => cached);
    })
  );
});

// ============================================================
// 메시지 핸들러 — 클라이언트가 강제 업데이트 요청 시
// ============================================================
self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') {
    self.skipWaiting();
  }
  if (event.data === 'CLEAR_CACHE') {
    caches.keys().then(names => Promise.all(names.map(n => caches.delete(n))));
  }
});
