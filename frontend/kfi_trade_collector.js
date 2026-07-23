/* =============================================================================
 * KFI 거래사례 자동수집 모듈  (kfi_trade_collector.js)
 * 키움에프앤아이 자산관리 시스템 - 국토부 아파트 매매 실거래 수집기
 *
 * 해결하는 문제 2가지
 *  1) "시군구 코드 없음 - 자동수집 skip"  → 주소에서 시군구 코드(LAWD_CD)를 직접 추출
 *  2) 단지명 표기 불일치(에스케이 vs SK)  → 단지명이 아닌 "법정동 + 지번"으로 매칭
 *
 * 사용법 (index.html)
 *   <script src="kfi_trade_collector.js"></script>
 *   const r = await KFITradeCollector.collectComparables(
 *     { jibunAddress: "서울특별시 광진구 중곡동 292", excluUseAr: 84.6, aptName: "에스케이아파트" },
 *     { months: 12, fetchFn: myBackendFetch }   // 또는 { serviceKey: "디코딩키" }
 *   );
 *   // r = { ok, lawdCd, target, count, items: [...] }
 *
 * fetchFn 주의
 *   - 이미 Render 백엔드에 국토부 호출 프록시(/api/...)가 있으면 그걸 fetchFn으로 넘기세요.
 *     (브라우저에서 apis.data.go.kr 직접 호출은 CORS로 막힐 수 있음)
 *   - 직접 호출 경로(defaultFetch)를 쓰려면 serviceKey는 반드시 "Decoding 키"를 넣으세요.
 * ============================================================================= */

(function (global) {
  'use strict';

  /* --- 시군구 법정동코드 테이블 (앞 5자리) -----------------------------------
   * sido 가 ''(빈 문자열)이면 명칭이 전국 유일하다는 뜻 → 시도 체크 생략.
   * 일반구명(강서구 등)은 부산 등과 충돌하므로 sido 를 지정해 구분.
   * 여기 없는 시군구는 lawdLookup 콜백(기존 전국 법정동 테이블)으로 보충하세요.
   * -------------------------------------------------------------------------- */
  var LAWD_TABLE = [
    // 서울 25개 구
    { sido: '서울', name: '종로구',   code: '11110' },
    { sido: '서울', name: '중구',     code: '11140' },
    { sido: '서울', name: '용산구',   code: '11170' },
    { sido: '서울', name: '성동구',   code: '11200' },
    { sido: '서울', name: '광진구',   code: '11215' },
    { sido: '서울', name: '동대문구', code: '11230' },
    { sido: '서울', name: '중랑구',   code: '11260' },
    { sido: '서울', name: '성북구',   code: '11290' },
    { sido: '서울', name: '강북구',   code: '11305' },
    { sido: '서울', name: '도봉구',   code: '11320' },
    { sido: '서울', name: '노원구',   code: '11350' },
    { sido: '서울', name: '은평구',   code: '11380' },
    { sido: '서울', name: '서대문구', code: '11410' },
    { sido: '서울', name: '마포구',   code: '11440' },
    { sido: '서울', name: '양천구',   code: '11470' },
    { sido: '서울', name: '강서구',   code: '11500' },  // 부산 강서구(26440)와 구분 위해 sido 필수
    { sido: '서울', name: '구로구',   code: '11530' },
    { sido: '서울', name: '금천구',   code: '11545' },
    { sido: '서울', name: '영등포구', code: '11560' },
    { sido: '서울', name: '동작구',   code: '11590' },
    { sido: '서울', name: '관악구',   code: '11620' },
    { sido: '서울', name: '서초구',   code: '11650' },
    { sido: '서울', name: '강남구',   code: '11680' },
    { sido: '서울', name: '송파구',   code: '11710' },
    { sido: '서울', name: '강동구',   code: '11740' },
    // NPL 업무에서 자주 등장하는 시군구 (명칭 유일)
    { sido: '', name: '시흥시', code: '41390' },
    { sido: '', name: '단원구', code: '41273', full: '안산시 단원구' },
    { sido: '', name: '상록구', code: '41271', full: '안산시 상록구' },
    { sido: '', name: '부천시', code: '41190' },
    { sido: '', name: '함양군', code: '48310' }
  ];

  /* 2026-07-01 인천 행정개편: (옛)서구 → 서해구(남부) + 검단구(북부). 옛 시군구코드 28260 폐지.
   * 주소로 새 코드를 판별한다: 검단 법정동이면 검단구(28290), 그 외 옛 서구 지역이면 서해구(28275). */
  var GEOMDAN_DONGS = ['마전동', '당하동', '원당동', '불로동', '오류동', '왕길동', '대곡동', '금곡동'];
  function incheonSeoguNewCode(address) {
    var a = String(address || '').replace(/\s+/g, '');
    if (a.indexOf('인천') === -1) return null;
    if (a.indexOf('검단구') !== -1) return '28290';
    if (a.indexOf('서해구') !== -1) return '28275';
    for (var i = 0; i < GEOMDAN_DONGS.length; i++) {
      if (a.indexOf(GEOMDAN_DONGS[i]) !== -1) return '28290';   // 검단 법정동 → 검단구
    }
    if (a.indexOf('서구') !== -1) return '28275';                // 옛 서구의 검단 외 지역 → 서해구
    return null;
  }

  /* 주소 문자열 → 시군구 코드(LAWD_CD, 5자리). 실패 시 null */
  function addressToLawdCd(address, externalLookup) {
    if (!address) return null;
    var _incheonFix = incheonSeoguNewCode(address);   // 🆕 인천 서구 분구(검단구/서해구) 우선 보정
    if (_incheonFix) return _incheonFix;
    var a = String(address).replace(/\s+/g, '');
    for (var i = 0; i < LAWD_TABLE.length; i++) {
      var r = LAWD_TABLE[i];
      var sidoKey = (r.sido || '').replace(/\s+/g, '');
      var nameKey = (r.name || '').replace(/\s+/g, '');
      if (a.indexOf(nameKey) !== -1 && (!sidoKey || a.indexOf(sidoKey) !== -1)) {
        return r.code;
      }
    }
    // 내장 테이블에 없으면 외부 룩업(기존 전국 법정동 49,861건 테이블 등) 사용
    if (typeof externalLookup === 'function') {
      try { return externalLookup(address) || null; } catch (e) { return null; }
    }
    return null;
  }

  /* 주소에서 법정동(읍/면/동) + 본번/부번 추출
   * "서울특별시 광진구 중곡동 292"     → { umdNm:'중곡동', bonbun:292, bubun:0 }
   * "경기도 시흥시 정왕동 1257-4"       → { umdNm:'정왕동', bonbun:1257, bubun:4 } */
  function parseAddress(address) {
    var a = String(address || '');
    var m = a.match(/([가-힣0-9]+(?:동|읍|면))\s*(\d+)(?:-(\d+))?/);
    return {
      umdNm: m ? m[1] : null,
      bonbun: m ? parseInt(m[2], 10) : null,
      bubun: (m && m[3]) ? parseInt(m[3], 10) : 0
    };
  }

  /* 국토부 jibun 필드 "292" 또는 "292-1" → { bonbun, bubun } */
  function parseJibun(j) {
    if (j === null || j === undefined || j === '') return { bonbun: null, bubun: null };
    var m = String(j).match(/(\d+)(?:-(\d+))?/);
    return m
      ? { bonbun: parseInt(m[1], 10), bubun: m[2] ? parseInt(m[2], 10) : 0 }
      : { bonbun: null, bubun: null };
  }

  /* 단지명 정규화 (보조 매칭용): 에스케이→SK, '아파트' 제거, 특수문자 제거 */
  function normalizeAptName(s) {
    if (!s) return '';
    return String(s).toUpperCase()
      .replace(/에스케이/g, 'SK')
      .replace(/아파트|APT|APARTMENT/g, '')
      .replace(/[^A-Z0-9가-힣]/g, '')
      .trim();
  }

  /* 최근 N개월의 DEAL_YMD(YYYYMM) 배열 (현재 월부터 과거로) */
  function recentYmds(months) {
    var out = [];
    var d = new Date();
    for (var i = 0; i < months; i++) {
      var y = d.getFullYear();
      var m = d.getMonth() + 1;
      out.push('' + y + (m < 10 ? '0' + m : '' + m));
      d.setMonth(d.getMonth() - 1);
    }
    return out;
  }

  /* 국토부 실거래 XML → 표준 객체 배열 (신규 영문/구 한글 태그 모두 대응) */
  function parseMolitXml(xmlText) {
    if (typeof DOMParser === 'undefined') return [];
    var doc = new DOMParser().parseFromString(xmlText, 'text/xml');
    var items = doc.getElementsByTagName('item');
    var out = [];
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var g = function (tag) {
        var e = it.getElementsByTagName(tag)[0];
        return e ? (e.textContent || '').trim() : '';
      };
      var amtRaw = g('dealAmount') || g('거래금액');
      var areaRaw = g('excluUseAr') || g('전용면적');
      out.push({
        aptNm: g('aptNm') || g('아파트'),
        umdNm: g('umdNm') || g('법정동'),
        jibun: g('jibun') || g('지번'),
        excluUseAr: areaRaw ? (parseFloat(areaRaw) || null) : null,
        dealAmount: amtRaw ? (parseInt(amtRaw.replace(/[,\s]/g, ''), 10) || null) : null, // 단위: 만원
        floor: parseInt(g('floor') || g('층'), 10) || null,
        buildYear: parseInt(g('buildYear') || g('건축년도'), 10) || null,
        year: g('dealYear') || g('년'),
        month: g('dealMonth') || g('월'),
        day: g('dealDay') || g('일')
      });
    }
    return out;
  }

  /* 핵심: 법정동 + 지번 본번 + 전용면적(±tol)으로 거래 필터 */
  function filterComparables(items, target, tol) {
    tol = (tol === undefined) ? 1.0 : tol;
    var umd = target.umdNm ? target.umdNm.replace(/\s/g, '') : null;
    return (items || []).filter(function (it) {
      if (umd && it.umdNm && it.umdNm.replace(/\s/g, '') !== umd) return false;
      if (target.bonbun != null) {
        var b = parseJibun(it.jibun).bonbun;
        if (b !== target.bonbun) return false;
      }
      if (target.excluUseAr != null && it.excluUseAr != null) {
        if (Math.abs(it.excluUseAr - target.excluUseAr) > tol) return false;
      }
      return true;
    });
  }

  /* 직접 호출 경로 (백엔드 프록시가 없을 때만 사용 / serviceKey는 Decoding 키) */
  function defaultFetch(lawdCd, ymd, serviceKey) {
    var url = 'https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade'
      + '?serviceKey=' + encodeURIComponent(serviceKey)
      + '&LAWD_CD=' + lawdCd
      + '&DEAL_YMD=' + ymd
      + '&numOfRows=1000&pageNo=1';
    return fetch(url).then(function (res) { return res.text(); }).then(parseMolitXml);
  }

  /* 중복 거래 제거 */
  function dedupeTrades(items) {
    var seen = {};
    var out = [];
    items.forEach(function (it) {
      var key = [it.umdNm, it.jibun, it.excluUseAr, it.floor, it.dealAmount,
                 it.year, it.month, it.day].join('|');
      if (!seen[key]) { seen[key] = true; out.push(it); }
    });
    return out;
  }

  /* 오케스트레이터: 입력 물건 → 동일 단지/면적 거래사례 목록
   * input : { jibunAddress|address, excluUseAr|area, aptName?, lawdCd?, umdNm?, bonbun? }
   * opts  : { months=12, serviceKey?, fetchFn?, lawdLookup?, tol=1.0, dedupe=true } */
  function collectComparables(input, opts) {
    opts = opts || {};
    var months = opts.months || 12;
    var tol = (opts.tol === undefined) ? 1.0 : opts.tol;
    var address = input.jibunAddress || input.address;

    // 1) 시군구 코드 확보 (K-apt 경로면 이미 있고, PDF 경로면 주소에서 추출)
    var lawdCd = input.lawdCd || addressToLawdCd(address, opts.lawdLookup);
    if (!lawdCd) {
      return Promise.resolve({
        ok: false, reason: '시군구 코드 변환 실패: ' + address,
        lawdCd: null, target: null, count: 0, items: []
      });
    }

    // 2) 매칭 기준값 (법정동 + 본번 + 면적)
    var ad = parseAddress(address);
    var target = {
      umdNm: input.umdNm || ad.umdNm,
      bonbun: (input.bonbun != null) ? input.bonbun : ad.bonbun,
      excluUseAr: (input.excluUseAr != null) ? input.excluUseAr
                : (input.area != null ? input.area : null)
    };

    var doFetch = opts.fetchFn || function (l, y) { return defaultFetch(l, y, opts.serviceKey); };
    var ymds = recentYmds(months);

    // 3) 월별 수집
    var all = [];
    var chain = Promise.resolve();
    ymds.forEach(function (ymd) {
      chain = chain.then(function () {
        return Promise.resolve(doFetch(lawdCd, ymd))
          .then(function (rows) { if (Array.isArray(rows)) all = all.concat(rows); })
          .catch(function () { /* 특정 월 실패는 건너뜀 */ });
      });
    });

    // 4) 필터링 + 단지명 폴백 + 정리
    return chain.then(function () {
      var matched = filterComparables(all, target, tol);

      // 지번 매칭이 0건이면(대표지번 누락 등) 단지명 정규화로 보조 시도
      if (matched.length === 0 && input.aptName) {
        var n = normalizeAptName(input.aptName);
        matched = all.filter(function (it) {
          var nm = normalizeAptName(it.aptNm);
          return nm && (nm.indexOf(n) !== -1 || n.indexOf(nm) !== -1);
        });
        if (target.excluUseAr != null) {
          matched = matched.filter(function (it) {
            return it.excluUseAr == null || Math.abs(it.excluUseAr - target.excluUseAr) <= tol;
          });
        }
      }

      if (opts.dedupe !== false) matched = dedupeTrades(matched);

      // 최신 거래순 정렬
      matched.sort(function (a, b) {
        var ka = (a.year || '') + String(a.month || '').padStart(2, '0') + String(a.day || '').padStart(2, '0');
        var kb = (b.year || '') + String(b.month || '').padStart(2, '0') + String(b.day || '').padStart(2, '0');
        return kb.localeCompare(ka);
      });

      return { ok: true, lawdCd: lawdCd, target: target, count: matched.length, items: matched };
    });
  }

  /* 입력 종류 감지: 'object'(PDF/물건객체) | 'jibun'(지번주소) | 'aptName'(단지명) */
  function detectInputType(input) {
    if (input == null || input === '') return 'empty';
    if (typeof input === 'object') return 'object';
    var s = String(input).trim();
    // 동/읍/면 + 번지숫자, 또는 시/군/구 + 숫자 → 지번주소로 판단
    if (/(동|읍|면)\s*\d/.test(s) || (/(시|군|구)/.test(s) && /\d/.test(s))) return 'jibun';
    return 'aptName';
  }

  /* 객체에서 여러 후보 키 중 처음으로 값이 있는 것을 반환 */
  function pick(obj, keys) {
    for (var i = 0; i < keys.length; i++) {
      if (obj[keys[i]] != null && obj[keys[i]] !== '') return obj[keys[i]];
    }
    return null;
  }

  function toNum(v) { var n = parseFloat(v); return isNaN(n) ? null : n; }

  /* 입력 1개 → 물건 식별값 일괄 해석 (지번주소를 허브로)
   *
   * input : 등기부PDF 분석결과(객체) | 지번주소(문자열) | 단지명(문자열)
   * opts  : {
   *   as,              // 'pdf'|'jibun'|'aptName' (생략 시 자동감지; UI 칸이 정해져 있으면 명시 권장)
   *   months, serviceKey, fetchFn, lawdLookup,   // collectComparables 와 동일
   *   kaptLookup,      // (단지명) => [{aptName, jibunAddress, roadAddress, lawdCd?}, ...]
   *                    //   단지명→지번 역조회. 앱의 K-apt DB와 연결. (단지명 입력일 때만 필요)
   *   withComparables  // 기본 true. false면 거래사례 수집 없이 코드/단지명만 빠르게
   * }
   *
   * 반환(Promise): {
   *   ok, source, needsUserChoice, candidates,
   *   jibunAddress, roadAddress, lawdCd, umdNm, bonbun,
   *   formalName,      // 정식명(등기부 표제부) - 있을 때만
   *   marketName,      // 실거래명(국토부 역조회) ex) "중곡SK"
   *   excluUseAr, dong, ho,
   *   comparables, comparableCount,
   *   pdf,             // 원본 PDF 분석결과 보존 - 객체 입력일 때만
   *   warnings
   * }
   *
   * 단지명 입력에서 후보가 여럿이면 needsUserChoice=true, candidates 반환 →
   * 사용자가 고른 후보의 jibunAddress 로 resolveProperty(주소, {as:'jibun', ...}) 재호출. */
  function resolveProperty(input, opts) {
    opts = opts || {};
    var warnings = [];
    var type = opts.as || detectInputType(input);

    var base = {
      ok: false, source: type, needsUserChoice: false, candidates: [],
      jibunAddress: null, roadAddress: null, lawdCd: null, umdNm: null, bonbun: null,
      formalName: null, marketName: null, excluUseAr: null, dong: null, ho: null,
      comparables: [], comparableCount: 0, pdf: null, warnings: warnings
    };

    if (type === 'empty') {
      warnings.push('입력값이 비어 있습니다.');
      return Promise.resolve(base);
    }

    var prep = Promise.resolve();

    // --- 1) 입력별 1차 추출 -----------------------------------------------------
    if (type === 'object' || type === 'pdf') {
      var o = input;
      base.pdf = o;
      base.jibunAddress = pick(o, ['jibunAddress', '지번주소', 'jibun_address', 'jibunAddr']);
      base.roadAddress  = pick(o, ['roadAddress', '도로명주소', 'road_address', 'roadAddr']);
      base.formalName   = pick(o, ['aptName', '단지명', 'complexName', 'aptNm', 'buildingName']);
      base.excluUseAr   = toNum(pick(o, ['excluUseAr', '전용면적', 'area', 'exclusiveArea']));
      base.dong         = pick(o, ['dong', '동']);
      base.ho           = pick(o, ['ho', '호', 'hosu']);
      if (!base.jibunAddress) warnings.push('PDF 결과에서 지번주소를 찾지 못했습니다.');

    } else if (type === 'jibun') {
      base.jibunAddress = String(input).trim();

    } else if (type === 'aptName') {
      base.formalName = String(input).trim();
      if (typeof opts.kaptLookup !== 'function') {
        warnings.push('단지명 입력은 K-apt 역조회(kaptLookup)가 필요합니다. 지번주소를 직접 입력하면 자동 해석됩니다.');
        return Promise.resolve(base);
      }
      prep = Promise.resolve(opts.kaptLookup(base.formalName)).then(function (cands) {
        cands = cands || [];
        if (cands.length === 0) { warnings.push('K-apt에서 일치하는 단지를 찾지 못했습니다.'); return false; }
        if (cands.length > 1) {
          base.needsUserChoice = true;
          base.candidates = cands;
          warnings.push('같은 이름 단지가 ' + cands.length + '곳 있습니다. 하나를 선택 후 다시 호출하세요.');
          return false;
        }
        var c = cands[0];
        base.jibunAddress = c.jibunAddress || c['지번주소'] || base.jibunAddress;
        base.roadAddress  = c.roadAddress || c['도로명주소'] || base.roadAddress;
        base.lawdCd       = c.lawdCd || base.lawdCd;
        return true;
      });
    }

    // --- 2) 지번 확보 후 공통 파생 ----------------------------------------------
    return prep.then(function (cont) {
      if (cont === false) return base;        // 단지명 미해결 / 후보 선택 대기
      if (!base.jibunAddress) return base;
      if (base.excluUseAr == null && opts.excluUseAr != null) base.excluUseAr = toNum(opts.excluUseAr);

      if (!base.lawdCd) base.lawdCd = addressToLawdCd(base.jibunAddress, opts.lawdLookup);
      var ad = parseAddress(base.jibunAddress);
      base.umdNm = ad.umdNm;
      base.bonbun = ad.bonbun;

      if (!base.lawdCd) {
        warnings.push('시군구 코드 변환 실패: ' + base.jibunAddress);
        return base;
      }

      if (opts.withComparables === false) { base.ok = true; return base; }

      // 거래사례 수집 (면적 있으면 면적대까지 좁힘) + 실거래명 역조회
      return collectComparables(
        { jibunAddress: base.jibunAddress, excluUseAr: base.excluUseAr,
          lawdCd: base.lawdCd, aptName: base.formalName },
        opts
      ).then(function (r) {
        base.comparables = r.items || [];
        base.comparableCount = base.comparables.length;
        if (base.comparables.length > 0) base.marketName = base.comparables[0].aptNm || null;

        // 면적대 거래가 0건이면 면적 무시하고 지번만으로 단지명 재시도
        if (!base.marketName) {
          return collectComparables(
            { jibunAddress: base.jibunAddress, lawdCd: base.lawdCd }, opts
          ).then(function (r2) {
            if (r2.items && r2.items.length > 0) base.marketName = r2.items[0].aptNm || null;
            if (!base.marketName) {
              warnings.push('최근 거래가 없어 실거래 단지명을 역조회하지 못했습니다(건축HUB/K-apt로 보완 권장).');
            }
            base.ok = true;
            return base;
          });
        }
        base.ok = true;
        return base;
      });
    });
  }

  var API = {
    LAWD_TABLE: LAWD_TABLE,
    addressToLawdCd: addressToLawdCd,
    incheonSeoguNewCode: incheonSeoguNewCode,
    parseAddress: parseAddress,
    parseJibun: parseJibun,
    normalizeAptName: normalizeAptName,
    recentYmds: recentYmds,
    parseMolitXml: parseMolitXml,
    filterComparables: filterComparables,
    dedupeTrades: dedupeTrades,
    collectComparables: collectComparables,
    resolveProperty: resolveProperty,
    detectInputType: detectInputType
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = API;
  if (global) global.KFITradeCollector = API;

})(typeof window !== 'undefined' ? window : this);
