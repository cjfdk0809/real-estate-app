"""
backend/complex_identifier.py

Day 8 신규 모듈 — 단지 교차검증 식별기

[목적]
    K-apt 데이터 단일 신뢰의 한계(옛 지번, 산번지 오분류 등)를 극복하기 위해,
    건축HUB 총괄표제부 API를 통해 같은 법정동의 모든 단지를 받아오고,
    단지명·도로명주소·세대수·사용승인일 4가지 요소를 점수화하여
    가장 신뢰도 높은 단지로 교차검증한다.

[핵심 함수]
    identify_complex(...) : 단지 단위 식별 (캐시 우선 → fresh)
    identify_dong(...)    : 단지 내 특정 동(棟) mgmBldrgstPk 식별

[캐시 정책]
    한 번 검증된 결과는 complex_mapping 테이블에 영구 저장.
    K-apt 코드로 즉시 hit. force_refresh=True 옵션으로 재검증 가능.
"""

import os
import re
import requests
from datetime import datetime, date
from typing import Optional, Tuple, List, Dict, Any
from supabase import create_client, Client


# ======================================================================
# 환경변수 (Render Environment에서 설정)
# ======================================================================
MOLIT_API_KEY = os.environ.get('MOLIT_API_KEY', '')
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')  # service_role 권한 키


# ======================================================================
# 상수
# ======================================================================
RECAP_TITLE_URL = 'http://apis.data.go.kr/1613000/BldRgstHubService/getBrRecapTitleInfo'
TITLE_URL = 'http://apis.data.go.kr/1613000/BldRgstHubService/getBrTitleInfo'

PAGE_SIZE = 100
MAX_PAGES = 30          # Render 무료티어 안전: 한 법정동 최대 3,000건
HTTP_TIMEOUT = 12       # API 응답 대기 (초)
MIN_MATCH_SCORE = 60    # 60점 미만은 매칭 실패로 간주


# ======================================================================
# Supabase 클라이언트 (싱글톤)
# ======================================================================
_supabase_client: Optional[Client] = None


def get_supabase() -> Client:
    """싱글톤 패턴으로 Supabase 클라이언트 재사용"""
    global _supabase_client
    if _supabase_client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError(
                'SUPABASE_URL 또는 SUPABASE_KEY 환경변수 미설정. '
                'Render Environment 탭에서 확인하세요.'
            )
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase_client


# ======================================================================
# 헬퍼 함수
# ======================================================================

def normalize_name(name: str) -> str:
    """단지명 정규화: 공백/특수문자/구두점 제거 + 소문자화

    예) '구미푸르지오 센트럴파크' → '구미푸르지오센트럴파크'
    예) 'e-편한세상(101동)' → 'e편한세상101동'
    """
    if not name:
        return ''
    return re.sub(r'[\s\-\(\)\[\]_·\.,/&]', '', str(name)).lower()


def normalize_road_addr(addr: str) -> str:
    """도로명주소 정규화 (다중 공백 → 단일 공백, trim, lower)"""
    if not addr:
        return ''
    return re.sub(r'\s+', ' ', str(addr)).strip().lower()


def parse_plat_plc(plat_plc: str) -> Tuple[str, str, str]:
    """대지위치 문자열에서 (plat_gb_cd, bun, ji) 추출

    예) '경상북도 구미시 고아읍 원호리 834'        → ('0', '0834', '0000')
    예) '경상북도 구미시 고아읍 원호리 산44-26'    → ('1', '0044', '0026')
    예) '경상북도 구미시 고아읍 원호리 12-34번지' → ('0', '0012', '0034')
    """
    if not plat_plc:
        return ('0', '0000', '0000')

    cleaned = str(plat_plc).strip()
    plat_gb = '1' if re.search(r'\s산\s*\d', cleaned) else '0'

    # 끝부분 "[산]본번-부번" 패턴 추출
    match = re.search(r'산?\s*(\d+)(?:\s*-\s*(\d+))?\s*(?:번지)?\s*$', cleaned)
    if not match:
        return (plat_gb, '0000', '0000')

    bun = match.group(1).zfill(4)
    ji = (match.group(2) or '0').zfill(4)
    return (plat_gb, bun, ji)


def parse_use_apr_day(s: Any) -> Optional[date]:
    """사용승인일 변환: 'YYYYMMDD' / 'YYYY-MM-DD' / 'YYYY.MM.DD' → date"""
    if not s:
        return None
    s = str(s).strip()
    try:
        if len(s) == 8 and s.isdigit():
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        s_clean = s.replace('.', '-').replace('/', '-')
        return date.fromisoformat(s_clean[:10])
    except (ValueError, TypeError):
        return None


# ======================================================================
# 건축HUB 총괄표제부 페이지네이션 조회
# ======================================================================

def fetch_recap_title_all(sigungu_cd: str, bjdong_cd: str) -> List[Dict]:
    """해당 법정동의 모든 총괄표제부 항목 조회 (페이지네이션 자동)

    Render 무료티어 메모리 보호: 최대 MAX_PAGES(30) 페이지까지만.
    중간에 API 에러가 발생해도 지금까지 수집한 결과를 반환.
    """
    all_items: List[Dict] = []
    page = 1

    while page <= MAX_PAGES:
        params = {
            'serviceKey': MOLIT_API_KEY,
            'sigunguCd': sigungu_cd,
            'bjdongCd': bjdong_cd,
            'numOfRows': PAGE_SIZE,
            'pageNo': page,
            '_type': 'json',
        }

        try:
            resp = requests.get(RECAP_TITLE_URL, params=params, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            # 페이지 일부 실패해도 지금까지 수집한 결과는 반환
            break

        body = data.get('response', {}).get('body', {}) or {}
        total = int(body.get('totalCount', 0) or 0)
        items = body.get('items', {}) or {}

        # items가 빈 문자열이거나 None인 경우 처리
        if not items or items == '':
            break

        item_list = items.get('item', []) if isinstance(items, dict) else []
        if isinstance(item_list, dict):
            item_list = [item_list]

        all_items.extend(item_list)

        if len(all_items) >= total or len(item_list) < PAGE_SIZE:
            break
        page += 1

    return all_items


# ======================================================================
# 매칭 점수 계산
# ======================================================================

def calculate_match_score(
    candidate: Dict,
    target_name: str,
    target_road_addr: Optional[str] = None,
    target_household: Optional[int] = None,
    target_apr_date: Optional[date] = None,
) -> Dict:
    """한 후보에 대해 매칭 신뢰도 점수 산출 (최대 100점)

    점수 배분:
      - 단지명 정확 일치: 40점 / 부분 일치: 25점 / 불일치: 0점 (즉시 탈락)
      - 도로명주소 정확 일치: 25점 / 부분 일치: 15점
      - 세대수 정확 일치: 15점 / ±5 근사: 7점
      - 사용승인일 정확 일치: 10점
      - 단일 후보 보너스: 10점 (호출부에서 추가)
    """
    breakdown: Dict[str, int] = {}

    # 1. 단지명 (최대 40점, 필수 조건)
    cand_name = candidate.get('bldNm', '') or ''
    norm_cand = normalize_name(cand_name)
    norm_target = normalize_name(target_name)

    if not norm_target or not norm_cand:
        return {'total': 0, 'breakdown': {}, 'matched': False}

    if norm_cand == norm_target:
        breakdown['단지명 정확 일치'] = 40
    elif norm_target in norm_cand or norm_cand in norm_target:
        breakdown['단지명 부분 일치'] = 25
    else:
        # 단지명 매칭 실패는 다른 점수 무관하게 즉시 탈락
        return {'total': 0, 'breakdown': {}, 'matched': False}

    # 2. 도로명주소 (최대 25점)
    if target_road_addr:
        cand_road = candidate.get('newPlatPlc', '') or ''
        if cand_road:
            norm_t = normalize_road_addr(target_road_addr)
            norm_c = normalize_road_addr(cand_road)
            if norm_t == norm_c:
                breakdown['도로명주소 정확 일치'] = 25
            elif norm_t in norm_c or norm_c in norm_t:
                breakdown['도로명주소 부분 일치'] = 15

    # 3. 세대수 (최대 15점)
    if target_household:
        try:
            cand_household = int(candidate.get('hhldCnt', 0) or 0)
            if cand_household > 0:
                if cand_household == int(target_household):
                    breakdown[f'세대수 일치 ({cand_household}세대)'] = 15
                elif abs(cand_household - int(target_household)) <= 5:
                    breakdown['세대수 근사 (±5세대)'] = 7
        except (ValueError, TypeError):
            pass

    # 4. 사용승인일 (최대 10점)
    if target_apr_date:
        cand_apr = parse_use_apr_day(candidate.get('useAprDay'))
        if cand_apr and cand_apr == target_apr_date:
            breakdown['사용승인일 일치'] = 10

    total = sum(breakdown.values())
    return {
        'total': total,
        'breakdown': breakdown,
        'matched': total >= MIN_MATCH_SCORE,
    }


# ======================================================================
# 매칭 로그 (실패 분석용)
# ======================================================================

def _log_match(kapt_code: str, success: bool, score: int,
               candidates_count: int, breakdown: Dict, error: Optional[str]) -> None:
    """complex_match_log에 매칭 시도 기록 (실패해도 무시)"""
    try:
        sb = get_supabase()
        sb.table('complex_match_log').insert({
            'kapt_code': kapt_code,
            'success': success,
            'match_score': score,
            'candidates_count': candidates_count,
            'score_breakdown': breakdown,
            'error_message': error,
        }).execute()
    except Exception:
        pass  # 로그 실패는 메인 흐름에 영향 없도록 무시


# ======================================================================
# 메인 함수 1: 단지 식별
# ======================================================================

def identify_complex(
    kapt_code: str,
    sigungu_cd: str,
    bjdong_cd: str,
    complex_name: str,
    road_addr: Optional[str] = None,
    household_count: Optional[int] = None,
    use_approval_date: Optional[str] = None,
    force_refresh: bool = False,
) -> Dict:
    """단지 단위 교차검증 식별 (Day 8 핵심 함수)

    Returns:
        {
            'success': bool,
            'source': 'cache' | 'fresh' | 'error',
            'mgm_bldrgst_pk': str,          # 4719025331-3-08340000
            'complex_name': str,
            'road_addr': str,
            'jibun_addr': str,              # 정확한 신지번
            'plat_gb_cd': str,              # '0' or '1'
            'bun': str,                     # zero-padded 4-digit
            'ji': str,
            'household_count': int,
            'use_approval_date': str (ISO),
            'match_score': int,             # 0~100
            'score_breakdown': dict,        # {'단지명 정확 일치': 40, ...}
            'candidates_count': int,
            'rival_candidates': int,        # 다른 매칭 후보 수
            'error': str (실패 시)
        }
    """
    sb = get_supabase()

    # ---------- 1단계: 캐시 hit 확인 ----------
    if not force_refresh:
        try:
            cached = sb.table('complex_mapping')\
                .select('*').eq('kapt_code', kapt_code).execute()
            if cached.data:
                row = cached.data[0]
                return {
                    'success': True,
                    'source': 'cache',
                    'mgm_bldrgst_pk': row['mgm_bldrgst_pk'],
                    'complex_name': row['complex_name'],
                    'road_addr': row.get('road_addr'),
                    'jibun_addr': row.get('jibun_addr'),
                    'sigungu_cd': row['sigungu_cd'],
                    'bjdong_cd': row['bjdong_cd'],
                    'plat_gb_cd': row.get('plat_gb_cd'),
                    'bun': row.get('bun'),
                    'ji': row.get('ji'),
                    'household_count': row.get('household_count'),
                    'use_approval_date': row.get('use_approval_date'),
                    'match_score': row.get('verified_score', 0),
                    'score_breakdown': {'캐시 적중': row.get('verified_score', 0)},
                }
        except Exception:
            pass  # 캐시 실패는 무시하고 fresh 조회 진행

    # ---------- 2단계: 건축HUB 총괄표제부 전체 조회 ----------
    candidates = fetch_recap_title_all(sigungu_cd, bjdong_cd)

    if not candidates:
        _log_match(kapt_code, False, 0, 0, {}, 'no_candidates')
        return {
            'success': False,
            'source': 'error',
            'error': 'no_candidates',
            'message': f'법정동(sigunguCd={sigungu_cd}, bjdongCd={bjdong_cd})에서 총괄표제부 응답 없음. API 키 또는 법정동코드를 확인하세요.',
            'candidates_count': 0,
        }

    # ---------- 3단계: 매칭 점수 산출 ----------
    target_apr_date = parse_use_apr_day(use_approval_date)
    scored: List[Tuple[int, Dict, Dict]] = []

    for c in candidates:
        result = calculate_match_score(
            c, complex_name, road_addr, household_count, target_apr_date
        )
        if result['matched']:
            scored.append((result['total'], result['breakdown'], c))

    if not scored:
        _log_match(kapt_code, False, 0, len(candidates), {}, 'no_match')
        return {
            'success': False,
            'source': 'error',
            'error': 'no_match',
            'message': f'후보 {len(candidates)}건 중 단지명 매칭 실패. complex_match_log 테이블에서 candidates 목록 확인 가능.',
            'candidates_count': len(candidates),
        }

    # ---------- 4단계: 최고 점수 선택 + 단일 후보 보너스 ----------
    scored.sort(reverse=True, key=lambda x: x[0])
    best_score, best_breakdown, best = scored[0]

    if len(scored) == 1:
        best_breakdown['단일 후보 보너스'] = 10
        best_score = min(best_score + 10, 100)

    # ---------- 5단계: 정식 식별자 추출 ----------
    plat_gb, bun, ji = parse_plat_plc(best.get('platPlc', ''))
    apr_date = parse_use_apr_day(best.get('useAprDay'))

    record = {
        'kapt_code': kapt_code,
        'mgm_bldrgst_pk': best.get('mgmBldrgstPk', ''),
        'complex_name': best.get('bldNm', ''),
        'road_addr': best.get('newPlatPlc', ''),
        'jibun_addr': best.get('platPlc', ''),
        'sigungu_cd': sigungu_cd,
        'bjdong_cd': bjdong_cd,
        'plat_gb_cd': plat_gb,
        'bun': bun,
        'ji': ji,
        'household_count': int(best.get('hhldCnt', 0) or 0) or None,
        'use_approval_date': apr_date.isoformat() if apr_date else None,
        'total_floors': int(best.get('grndFlrCnt', 0) or 0) or None,
        'verified_score': best_score,
        'verified_at': datetime.utcnow().isoformat(),
        'updated_at': datetime.utcnow().isoformat(),
    }

    # ---------- 6단계: Supabase 영구 저장 ----------
    try:
        sb.table('complex_mapping').upsert(record).execute()
    except Exception:
        pass  # 저장 실패해도 식별 결과는 반환

    _log_match(kapt_code, True, best_score, len(candidates), best_breakdown, None)

    return {
        'success': True,
        'source': 'fresh',
        'mgm_bldrgst_pk': record['mgm_bldrgst_pk'],
        'complex_name': record['complex_name'],
        'road_addr': record['road_addr'],
        'jibun_addr': record['jibun_addr'],
        'sigungu_cd': sigungu_cd,
        'bjdong_cd': bjdong_cd,
        'plat_gb_cd': plat_gb,
        'bun': bun,
        'ji': ji,
        'household_count': record['household_count'],
        'use_approval_date': record['use_approval_date'],
        'match_score': best_score,
        'score_breakdown': best_breakdown,
        'candidates_count': len(candidates),
        'rival_candidates': len(scored) - 1,
    }


# ======================================================================
# 메인 함수 2: 동 식별
# ======================================================================

def identify_dong(
    kapt_code: str,
    complex_mgm_pk: str,
    dong_name: str,
    sigungu_cd: str,
    bjdong_cd: str,
    plat_gb_cd: str,
    bun: str,
    ji: str,
) -> Dict:
    """단지 내 특정 동의 mgmBldrgstPk 식별 (캐시 우선)"""
    sb = get_supabase()

    # 동 이름 정규화 ("103동" → "103")
    norm_dong = re.sub(r'동$', '', str(dong_name).strip())

    # ---------- 캐시 hit 확인 ----------
    try:
        cached = sb.table('dong_mapping')\
            .select('*')\
            .eq('kapt_code', kapt_code)\
            .eq('dong_name', norm_dong).execute()
        if cached.data:
            return {'success': True, 'source': 'cache', **cached.data[0]}
    except Exception:
        pass

    # ---------- 표제부 API로 단지 내 모든 동 조회 ----------
    params = {
        'serviceKey': MOLIT_API_KEY,
        'sigunguCd': sigungu_cd,
        'bjdongCd': bjdong_cd,
        'platGbCd': plat_gb_cd,
        'bun': bun,
        'ji': ji,
        'numOfRows': 100,
        'pageNo': 1,
        '_type': 'json',
    }

    try:
        resp = requests.get(TITLE_URL, params=params, timeout=HTTP_TIMEOUT)
        data = resp.json()
        body = data.get('response', {}).get('body', {}) or {}
        items = body.get('items', {}) or {}
        item_list = items.get('item', []) if isinstance(items, dict) else []
        if isinstance(item_list, dict):
            item_list = [item_list]
    except Exception:
        return {'success': False, 'error': 'title_api_failed',
                'message': '표제부 API 호출 실패'}

    # ---------- 동 번호 매칭 ----------
    # bldNm 예: "구미푸르지오센트럴파크 103동"
    for item in item_list:
        bld_nm = item.get('bldNm', '') or ''
        m = re.search(r'(\d+)\s*동', bld_nm)
        if m and m.group(1).lstrip('0') == norm_dong.lstrip('0'):
            record = {
                'kapt_code': kapt_code,
                'dong_name': norm_dong,
                'dong_mgm_pk': item.get('mgmBldrgstPk', ''),
                'total_floors': int(item.get('grndFlrCnt', 0) or 0) or None,
                'household_per_dong': int(item.get('hhldCnt', 0) or 0) or None,
            }
            try:
                sb.table('dong_mapping').upsert(record).execute()
            except Exception:
                pass
            return {'success': True, 'source': 'fresh', **record}

    return {
        'success': False,
        'error': 'dong_not_found',
        'message': f'단지 표제부 {len(item_list)}건 중 "{dong_name}" 매칭 실패',
        'available_dongs': [item.get('bldNm', '') for item in item_list][:10],
    }
