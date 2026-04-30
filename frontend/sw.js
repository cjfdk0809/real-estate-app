/**
 * Service Worker for 부동산 자산관리 시스템
 * - 정적 파일 캐싱 (오프라인 지원)
 * - API 요청은 항상 네트워크 우선 (실거래가는 최신 데이터 필요)
 */

const CACHE_VERSION = 'v1.0.0';
const CACHE_NAME = `real-estate-app-${CACHE_VERSION}`;

// 캐시할 정적 자원 목록
const STATIC_ASSETS = [
  '/',
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png',
  '/icon.svg',
];

// CDN 자원 (네트워크 우선, 캐시 fallback)
const CDN_PATTERNS = [
  /^https:\/\/fonts\.googleapis\.com/,
  /^https:\/\/fonts\.gstatic\.com/,
  /^https:\/\/cdn\.jsdelivr\.net/,
];

// ============================================================
// 설치 단계 — 정적 자원 미리 캐싱
// ============================================================
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      // 개별 추가로 실패해도 다른 항목은 캐싱 진행
      return Promise.all(
        STATIC_ASSETS.map((url) =>
          cache.add(url).catch((err) => console.warn('SW: 캐싱 실패', url, err))
        )
      );
    })
  );
  self.skipWaiting(); // 새 SW가 즉시 활성화되도록
});

// ============================================================
// 활성화 단계 — 오래된 캐시 정리
// ============================================================
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_NAME)
          .map((name) => caches.delete(name))
      );
    })
  );
  self.clients.claim();
});

// ============================================================
// 요청 가로채기
// ============================================================
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // API 요청은 항상 네트워크 (캐싱 안 함 - 최신 데이터 필요)
  if (url.pathname.startsWith('/api/')) {
    return; // 기본 동작 (네트워크)
  }

  // POST 등 GET 외 요청은 캐싱 안 함
  if (event.request.method !== 'GET') {
    return;
  }

  // CDN 자원은 stale-while-revalidate (빠른 로딩 + 백그라운드 업데이트)
  const isCDN = CDN_PATTERNS.some((p) => p.test(event.request.url));
  if (isCDN) {
    event.respondWith(
      caches.open(CACHE_NAME).then((cache) =>
        cache.match(event.request).then((cached) => {
          const fetchPromise = fetch(event.request)
            .then((response) => {
              if (response && response.status === 200) {
                cache.put(event.request, response.clone());
              }
              return response;
            })
            .catch(() => cached);
          return cached || fetchPromise;
        })
      )
    );
    return;
  }

  // 같은 origin 정적 자원: cache-first
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request)
        .then((response) => {
          if (response && response.status === 200 && response.type === 'basic') {
            const responseToCache = response.clone();
            caches.open(CACHE_NAME).then((cache) => {
              cache.put(event.request, responseToCache);
            });
          }
          return response;
        })
        .catch(() => {
          // 오프라인 + 캐시 없음 → 메인 페이지 fallback
          if (event.request.mode === 'navigate') {
            return caches.match('/');
          }
        });
    })
  );
});
