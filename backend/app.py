"""
부동산 자산관리 시스템 - 백엔드 서버
국토교통부 실거래가 공공 API를 프록시하여 프론트엔드에 제공합니다.
+ Supabase DB 기반 전국 단지/법정동 자동완성 검색.

실행:
    python app.py

환경변수 (.env):
    MOLIT_API_KEY=공공데이터포털에서 발급받은 인증키
    SUPABASE_URL=Supabase 프로젝트 URL (예: https://xxxxx.supabase.co)
    SUPABASE_KEY=Supabase service_role(secret) 키 (sb_secret_... 또는 eyJ...)
"""
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import lru_cache

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests
from dotenv import load_dotenv

from lawd_codes import LAWD_CODES, find_lawd_code

# Supabase 클라이언트 (선택적 - 미설치/미설정 시에도 기존 기능은 정상 작동)
try:
    from supabase import create_client
    HAS_SUPABASE_LIB = True
except ImportError:
    HAS_SUPABASE_LIB = False

# ============================================================
# 환경설정
# ============================================================
load_dotenv()
API_KEY = os.environ.get('MOLIT_API_KEY', '').strip()

# Supabase 연결 (자동완성 DB) - 환경변수 미설정 시 None으로 두고 기존 기능은 그대로
SUPABASE_URL = os.environ.get('SUPABASE_URL', '').strip()
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '').strip()
supabase = None
if HAS_SUPABASE_LIB and SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print(f'[INFO] Supabase 연결 성공: {SUPABASE_URL[:50]}...')
    except Exception as e:
        print(f'[WARN] Supabase 연결 실패 (자동완성 비활성화): {e}')
        supabase = None
elif not HAS_SUPABASE_LIB:
    print('[INFO] supabase 패키지 미설치 - 자동완성 기능 비활성화')
elif not SUPABASE_URL or not SUPABASE_KEY:
    print('[INFO] SUPABASE_URL/SUPABASE_KEY 환경변수 미설정 - 자동완성 기능 비활성화')

# 관리자 인증 (데이터 로드용 admin 엔드포인트)
ADMIN_SECRET = os.environ.get('ADMIN_SECRET', '').strip()

# 국토부 실거래가 API 엔드포인트 (2024년 신규 HTTPS 엔드포인트)
URL_TRADE = 'https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev'  # 상세
URL_RENT = 'https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent'  # 전월세

# 연립다세대 실거래가
URL_RH_TRADE = 'https://apis.data.go.kr/1613000/RTMSDataSvcRHTrade/getRTMSDataSvcRHTrade'  # 매매
URL_RH_RENT = 'https://apis.data.go.kr/1613000/RTMSDataSvcRHRent/getRTMSDataSvcRHRent'  # 전월세

# 공동주택 단지 정보 (K-apt) - V3 API (2025년 업그레이드)
URL_APT_LIST_DONG = 'https://apis.data.go.kr/1613000/AptListService3/getLegaldongAptList3'  # 법정동별 단지목록
URL_APT_LIST_ROAD = 'https://apis.data.go.kr/1613000/AptListService3/getRoadnameAptList3'  # 도로명별 단지목록
URL_APT_LIST_TOTAL = 'https://apis.data.go.kr/1613000/AptListService3/getTotalAptList3'  # 전체 단지목록 (한번에 모두)
URL_APT_BASIS = 'https://apis.data.go.kr/1613000/AptBasisInfoServiceV3/getAphusBassInfoV3'  # 단지 기본정보
URL_APT_DETAIL = 'https://apis.data.go.kr/1613000/AptBasisInfoServiceV3/getAphusDtlInfoV3'  # 단지 상세정보

# 건축물대장 (HUB)
URL_BR_TITLE = 'https://apis.data.go.kr/1613000/BldRgstHubService/getBrTitleInfo'  # 표제부
URL_BR_EXPOSE = 'https://apis.data.go.kr/1613000/BldRgstHubService/getBrExposPubuseAreaInfo'  # 전유공용면적
URL_BR_PRICE = 'https://apis.data.go.kr/1613000/BldRgstHubService/getBrHsprcInfo'  # 주택가격(공시)

app = Flask(__name__, static_folder='../frontend', static_url_path='')
CORS(app)


# ============================================================
# 유틸: XML 파싱
# ============================================================
def parse_xml_items(xml_text):
    """국토부 API XML 응답을 파싱하여 dict 리스트로 반환."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [], 'XML 파싱 실패. API 키가 올바른지 확인하세요.'

    # 응답 코드 확인
    result_code = root.findtext('.//resultCode', default='')
    result_msg = root.findtext('.//resultMsg', default='')
    if result_code and result_code not in ('00', '000'):
        return [], f'API 오류 [{result_code}]: {result_msg}'

    items = []
    for item in root.findall('.//item'):
        d = {child.tag: (child.text or '').strip() for child in item}
        items.append(d)
    return items, None


def parse_kapt_response(response_text):
    """K-apt V3 API 응답을 파싱 (XML/JSON 자동 감지).
    
    V3 API는 _type 파라미터에 따라 XML 또는 JSON으로 응답하며,
    파라미터 미지정 시 기본 형식이 XML과 다를 수 있음. 양쪽 모두 처리.
    
    Returns: (items_list, error_message_or_None)
    """
    import json as _json
    text = (response_text or '').strip()
    if not text:
        return [], 'API 응답이 비어있음'
    
    # JSON 시도 (V3에서 기본일 가능성 큼)
    if text[0] in ('{', '['):
        try:
            data = _json.loads(text)
            # K-apt JSON 구조: { "response": { "header": {...}, "body": { "items": {...} } } }
            response_node = data.get('response', data) if isinstance(data, dict) else {}
            
            # resultCode 확인
            header = response_node.get('header', {}) if isinstance(response_node, dict) else {}
            result_code = str(header.get('resultCode', '')).strip()
            result_msg = str(header.get('resultMsg', '')).strip()
            if result_code and result_code not in ('00', '000'):
                return [], f'API 오류 [{result_code}]: {result_msg}'
            
            # items 추출
            body = response_node.get('body', {}) if isinstance(response_node, dict) else {}
            items_wrapper = body.get('items') if isinstance(body, dict) else None
            
            if items_wrapper is None or items_wrapper == '':
                return [], None  # 데이터 없음 (오류 아님)
            
            if isinstance(items_wrapper, list):
                items = items_wrapper
            elif isinstance(items_wrapper, dict):
                inner = items_wrapper.get('item', [])
                if isinstance(inner, dict):
                    items = [inner]  # 단일 아이템
                elif isinstance(inner, list):
                    items = inner
                else:
                    items = []
            else:
                items = []
            
            # 모든 값을 string으로 변환 (XML 파싱과 호환)
            normalized = []
            for it in items:
                if isinstance(it, dict):
                    normalized.append({k: ('' if v is None else str(v).strip()) for k, v in it.items()})
            return normalized, None
        except _json.JSONDecodeError as e:
            return [], f'JSON 파싱 실패: {e}. 응답 앞부분: {text[:150]}'
    
    # XML 시도 (V2 형식 호환)
    if text[0] == '<':
        try:
            root = ET.fromstring(text)
            result_code = root.findtext('.//resultCode', default='')
            result_msg = root.findtext('.//resultMsg', default='')
            if result_code and result_code not in ('00', '000'):
                return [], f'API 오류 [{result_code}]: {result_msg}'
            items = []
            for item in root.findall('.//item'):
                d = {child.tag: (child.text or '').strip() for child in item}
                items.append(d)
            return items, None
        except ET.ParseError as e:
            return [], f'XML 파싱 실패: {e}. 응답 앞부분: {text[:150]}'
    
    # 둘 다 아닌 경우 (HTML 에러 페이지 등)
    return [], f'알 수 없는 응답 형식. 응답 앞부분: {text[:200]}'


def normalize_trade_item(raw):
    """매매 거래 항목 정규화."""
    # 거래금액에서 콤마 제거
    deal_amount = (raw.get('dealAmount') or '').replace(',', '').strip()
    try:
        price = int(deal_amount) if deal_amount else None
    except ValueError:
        price = None

    year = raw.get('dealYear', '')
    month = raw.get('dealMonth', '').zfill(2)
    day = raw.get('dealDay', '').zfill(2)
    date = f'{year}-{month}-{day}' if year and month and day else ''

    try:
        area = float(raw.get('excluUseAr', '0') or 0)
    except ValueError:
        area = 0

    try:
        floor = int(raw.get('floor', '') or 0) or None
    except ValueError:
        floor = None

    return {
        'date': date,
        'name': raw.get('aptNm', ''),
        'building': raw.get('aptDong', ''),
        'area': round(area, 2),
        'floor': floor,
        'price': price,  # 만원 단위
        'type': '매매',
        'memo': raw.get('cdealType', ''),  # 해제 등
        'jibun': raw.get('jibun', ''),
        'dong': raw.get('umdNm', ''),
    }


def normalize_rent_item(raw):
    """전월세 항목 정규화."""
    deposit = (raw.get('deposit') or '').replace(',', '').strip()
    monthly = (raw.get('monthlyRent') or '').replace(',', '').strip()
    try:
        deposit = int(deposit) if deposit else None
    except ValueError:
        deposit = None
    try:
        monthly = int(monthly) if monthly else 0
    except ValueError:
        monthly = 0

    year = raw.get('dealYear', '')
    month = raw.get('dealMonth', '').zfill(2)
    day = raw.get('dealDay', '').zfill(2)
    date = f'{year}-{month}-{day}' if year and month and day else ''

    try:
        area = float(raw.get('excluUseAr', '0') or 0)
    except ValueError:
        area = 0
    try:
        floor = int(raw.get('floor', '') or 0) or None
    except ValueError:
        floor = None

    return {
        'date': date,
        'name': raw.get('aptNm', ''),
        'area': round(area, 2),
        'floor': floor,
        'price': deposit,
        'monthly': monthly,
        'type': '월세' if monthly > 0 else '전세',
        'jibun': raw.get('jibun', ''),
        'dong': raw.get('umdNm', ''),
    }


# ============================================================
# 캐시: 같은 (LAWD_CD, YYYYMM) 조합은 1시간 동안 재호출 안 함
# ============================================================
@lru_cache(maxsize=256)
def fetch_trade_cached(lawd_cd, year_month, _ts):
    """캐시된 매매 데이터 조회. _ts는 캐시 무효화용."""
    params = {
        'serviceKey': API_KEY,
        'LAWD_CD': lawd_cd,
        'DEAL_YMD': year_month,
        'numOfRows': '1000',
        'pageNo': '1',
    }
    r = requests.get(URL_TRADE, params=params, timeout=30)
    r.raise_for_status()
    return r.text


@lru_cache(maxsize=256)
def fetch_rent_cached(lawd_cd, year_month, _ts):
    params = {
        'serviceKey': API_KEY,
        'LAWD_CD': lawd_cd,
        'DEAL_YMD': year_month,
        'numOfRows': '1000',
        'pageNo': '1',
    }
    r = requests.get(URL_RENT, params=params, timeout=30)
    r.raise_for_status()
    return r.text


def cache_ts():
    """1시간 단위로 캐시 무효화."""
    now = datetime.now()
    return f'{now.year}-{now.month}-{now.day}-{now.hour}'


# ============================================================
# 라우트: 정적 파일
# ============================================================
@app.route('/')
def index():
    response = send_from_directory('../frontend', 'index.html')
    # HTML은 절대 캐시하지 않음 - 항상 최신 받기
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


# PWA 정적 자원 (manifest, service worker, 아이콘)
@app.route('/manifest.json')
def manifest():
    return send_from_directory('../frontend', 'manifest.json', mimetype='application/manifest+json')


@app.route('/sw.js')
def service_worker():
    response = send_from_directory('../frontend', 'sw.js', mimetype='application/javascript')
    # Service Worker는 스코프 제한이 없도록 헤더 추가
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'  # SW 자체는 캐싱 안 함 (업데이트 즉시 반영)
    return response


@app.route('/icon.svg')
def icon_svg():
    return send_from_directory('../frontend', 'icon.svg', mimetype='image/svg+xml')


@app.route('/icon-192.png')
def icon_192():
    return send_from_directory('../frontend', 'icon-192.png', mimetype='image/png')


@app.route('/icon-512.png')
def icon_512():
    return send_from_directory('../frontend', 'icon-512.png', mimetype='image/png')


@app.route('/apple-touch-icon.png')
def apple_icon():
    return send_from_directory('../frontend', 'apple-touch-icon.png', mimetype='image/png')


@app.route('/kiwoom_ci.jpg')
def kiwoom_ci():
    return send_from_directory('../frontend', 'kiwoom_ci.jpg', mimetype='image/jpeg')


# ============================================================
# 라우트: API
# ============================================================
@app.route('/api/health')
def health():
    return jsonify({
        'status': 'ok',
        'has_api_key': bool(API_KEY),
        'api_key_prefix': API_KEY[:8] + '...' if API_KEY else None,
        'lawd_codes_loaded': len(LAWD_CODES),
        'features': {
            'apt_trade': True,
            'apt_rent': True,
            'rh_trade': True,
            'rh_rent': True,
            'danji_search': True,
            'danji_info': True,
            'building_register': True,
            'price_disclosure': True,
            'supabase_search': supabase is not None,  # 신규: 자동완성 가능 여부
        },
        'supabase': {
            'lib_installed': HAS_SUPABASE_LIB,
            'connected': supabase is not None,
            'url_set': bool(SUPABASE_URL),
            'key_set': bool(SUPABASE_KEY),
        },
        'version': 'v2.4-robust',
    })


@app.route('/api/lawd-codes')
def get_lawd_codes():
    """법정동 코드 검색. ?q=강남 → 매칭되는 시군구 반환."""
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'items': list(LAWD_CODES.items())[:50]})
    matches = [(name, code) for name, code in LAWD_CODES.items() if q in name]
    return jsonify({'items': matches[:50]})


@app.route('/api/transactions')
def get_transactions():
    """매매 실거래 조회.
    Query params:
        lawd_cd: 법정동코드 5자리 (필수)
        year_month: YYYYMM (필수)
        danji_name: 단지명 필터 (선택)
        min_area, max_area: 전용면적 범위 (선택)
    """
    if not API_KEY:
        return jsonify({'error': 'API 키가 설정되지 않았습니다. .env 파일에 MOLIT_API_KEY를 추가하세요.'}), 500

    lawd_cd = request.args.get('lawd_cd', '').strip()
    year_month = request.args.get('year_month', '').strip()
    danji_filter = request.args.get('danji_name', '').strip()
    min_area = request.args.get('min_area', type=float)
    max_area = request.args.get('max_area', type=float)

    if not lawd_cd or not year_month:
        return jsonify({'error': 'lawd_cd 및 year_month 파라미터가 필요합니다.'}), 400

    if len(lawd_cd) != 5 or not lawd_cd.isdigit():
        return jsonify({'error': 'lawd_cd는 5자리 숫자여야 합니다.'}), 400

    try:
        xml_text = fetch_trade_cached(lawd_cd, year_month, cache_ts())
        raw_items, err = parse_xml_items(xml_text)
        if err:
            return jsonify({'error': err}), 502

        items = [normalize_trade_item(x) for x in raw_items]

        # 필터링
        if danji_filter:
            normalized_filter = danji_filter.replace(' ', '').lower()
            items = [x for x in items if normalized_filter in x['name'].replace(' ', '').lower()]
        if min_area is not None:
            items = [x for x in items if x['area'] >= min_area]
        if max_area is not None:
            items = [x for x in items if x['area'] <= max_area]

        # 최신순 정렬
        items.sort(key=lambda x: x['date'], reverse=True)

        return jsonify({
            'count': len(items),
            'items': items,
            'meta': {
                'lawd_cd': lawd_cd,
                'year_month': year_month,
                'filter_danji': danji_filter or None,
            },
        })
    except requests.exceptions.HTTPError as e:
        return jsonify({'error': f'국토부 API HTTP 오류: {e}'}), 502
    except requests.exceptions.Timeout:
        return jsonify({'error': '국토부 API 응답 시간 초과 (30초)'}), 504
    except Exception as e:
        return jsonify({'error': f'서버 오류: {e}'}), 500


@app.route('/api/transactions/rent')
def get_rents():
    """전월세 실거래 조회."""
    if not API_KEY:
        return jsonify({'error': 'API 키가 설정되지 않았습니다.'}), 500

    lawd_cd = request.args.get('lawd_cd', '').strip()
    year_month = request.args.get('year_month', '').strip()
    danji_filter = request.args.get('danji_name', '').strip()
    min_area = request.args.get('min_area', type=float)
    max_area = request.args.get('max_area', type=float)

    if not lawd_cd or not year_month:
        return jsonify({'error': 'lawd_cd 및 year_month 파라미터가 필요합니다.'}), 400

    try:
        xml_text = fetch_rent_cached(lawd_cd, year_month, cache_ts())
        raw_items, err = parse_xml_items(xml_text)
        if err:
            return jsonify({'error': err}), 502

        items = [normalize_rent_item(x) for x in raw_items]

        if danji_filter:
            normalized_filter = danji_filter.replace(' ', '').lower()
            items = [x for x in items if normalized_filter in x['name'].replace(' ', '').lower()]
        if min_area is not None:
            items = [x for x in items if x['area'] >= min_area]
        if max_area is not None:
            items = [x for x in items if x['area'] <= max_area]

        items.sort(key=lambda x: x['date'], reverse=True)

        return jsonify({'count': len(items), 'items': items})
    except Exception as e:
        return jsonify({'error': f'서버 오류: {e}'}), 500


@app.route('/api/transactions/bulk')
def get_transactions_bulk():
    """다월(多月) 일괄 조회. 여러 달을 한 번에 조회.
    Query params:
        lawd_cd: 필수
        months: 조회 개월 수 (기본 6)
        danji_name: 단지 필터
    """
    if not API_KEY:
        return jsonify({'error': 'API 키가 설정되지 않았습니다.'}), 500

    lawd_cd = request.args.get('lawd_cd', '').strip()
    months = request.args.get('months', default=6, type=int)
    danji_filter = request.args.get('danji_name', '').strip()
    min_area = request.args.get('min_area', type=float)
    max_area = request.args.get('max_area', type=float)
    include_rent = request.args.get('include_rent', default='true') == 'true'

    if not lawd_cd:
        return jsonify({'error': 'lawd_cd 필수.'}), 400
    if months < 1 or months > 24:
        return jsonify({'error': 'months는 1~24 사이.'}), 400

    # 최근 N개월 YYYYMM 리스트 생성
    now = datetime.now()
    year_months = []
    y, m = now.year, now.month
    for _ in range(months):
        year_months.append(f'{y}{m:02d}')
        m -= 1
        if m == 0:
            m = 12
            y -= 1

    all_items = []
    errors = []

    for ym in year_months:
        try:
            # 매매
            xml_text = fetch_trade_cached(lawd_cd, ym, cache_ts())
            raw_items, err = parse_xml_items(xml_text)
            if not err:
                all_items.extend(normalize_trade_item(x) for x in raw_items)
            else:
                errors.append(f'{ym} 매매: {err}')

            # 전월세
            if include_rent:
                xml_text = fetch_rent_cached(lawd_cd, ym, cache_ts())
                raw_items, err = parse_xml_items(xml_text)
                if not err:
                    all_items.extend(normalize_rent_item(x) for x in raw_items)
        except Exception as e:
            errors.append(f'{ym}: {e}')

    # 필터링
    if danji_filter:
        normalized_filter = danji_filter.replace(' ', '').lower()
        all_items = [x for x in all_items if normalized_filter in x['name'].replace(' ', '').lower()]
    if min_area is not None:
        all_items = [x for x in all_items if x['area'] >= min_area]
    if max_area is not None:
        all_items = [x for x in all_items if x['area'] <= max_area]

    # 최신순 정렬
    all_items.sort(key=lambda x: x.get('date', ''), reverse=True)

    return jsonify({
        'count': len(all_items),
        'items': all_items,
        'months_queried': year_months,
        'errors': errors,
    })


# ============================================================
# 신규 API: 공동주택 단지 검색 + 기본정보 + 건축물대장
# ============================================================

def safe_get(item, key, default=''):
    """XML 파싱 결과에서 안전하게 값 추출."""
    val = item.get(key, default)
    return val.strip() if val else default


@lru_cache(maxsize=512)
def fetch_apt_list_by_dong_cached(bjd_code, _ts):
    """법정동코드(10자리)로 해당 동의 아파트 단지 목록 조회."""
    params = {
        'serviceKey': API_KEY,
        'bjdCode': bjd_code,
        'numOfRows': '1000',
        'pageNo': '1',
    }
    r = requests.get(URL_APT_LIST_DONG, params=params, timeout=30)
    r.raise_for_status()
    return r.text


@lru_cache(maxsize=512)
def fetch_apt_basis_cached(kapt_code, _ts):
    """단지코드로 단지 기본정보 조회."""
    params = {
        'serviceKey': API_KEY,
        'kaptCode': kapt_code,
    }
    r = requests.get(URL_APT_BASIS, params=params, timeout=30)
    r.raise_for_status()
    return r.text


@lru_cache(maxsize=512)
def fetch_apt_detail_cached(kapt_code, _ts):
    """단지코드로 단지 상세정보(주차/관리/시설 등) 조회."""
    params = {
        'serviceKey': API_KEY,
        'kaptCode': kapt_code,
    }
    r = requests.get(URL_APT_DETAIL, params=params, timeout=30)
    r.raise_for_status()
    return r.text


@app.route('/api/danji/search')
def search_danji_by_dong():
    """법정동코드로 해당 동의 아파트 단지 목록 조회.
    Query params:
        bjd_code: 법정동코드 10자리 (필수)
    """
    if not API_KEY:
        return jsonify({'error': 'API 키 미설정'}), 500
    bjd_code = request.args.get('bjd_code', '').strip()
    if not bjd_code or len(bjd_code) != 10 or not bjd_code.isdigit():
        return jsonify({'error': 'bjd_code는 10자리 법정동코드여야 합니다.'}), 400
    try:
        xml_text = fetch_apt_list_by_dong_cached(bjd_code, cache_ts())
        raw_items, err = parse_kapt_response(xml_text)
        if err:
            return jsonify({'error': err}), 502
        items = []
        for x in raw_items:
            items.append({
                'kaptCode': safe_get(x, 'kaptCode'),
                'kaptName': safe_get(x, 'kaptName'),
                'bjdCode': safe_get(x, 'bjdCode'),
                'as1': safe_get(x, 'as1'),  # 시도
                'as2': safe_get(x, 'as2'),  # 시군구
                'as3': safe_get(x, 'as3'),  # 읍면
                'as4': safe_get(x, 'as4'),  # 동리
            })
        return jsonify({'count': len(items), 'items': items})
    except Exception as e:
        return jsonify({'error': f'서버 오류: {e}'}), 500


@app.route('/api/danji/search-by-name')
def search_danji_by_name():
    """단지명으로 검색 (법정동코드 여러 개 시도해서 매칭).
    Query params:
        name: 단지명 (필수)
        lawd_cd: 시군구 코드 5자리 (선택, 더 빠른 검색)
    
    주의: K-apt API는 시도/시군구/동 범위에서만 검색되므로,
    효율적으로 검색하려면 lawd_cd가 필요합니다.
    """
    if not API_KEY:
        return jsonify({'error': 'API 키 미설정'}), 500
    name = request.args.get('name', '').strip()
    lawd_cd = request.args.get('lawd_cd', '').strip()
    if not name:
        return jsonify({'error': 'name 필수'}), 400
    
    # 단지명 검색은 직접 API가 없으므로, 시군구 단위로 검색 후 필터링
    # lawd_cd(5자리)를 받아 해당 시군구의 모든 동을 순회하는 건 비효율적이므로,
    # 클라이언트에서 bjd_code(10자리)를 직접 보내는 것이 바람직.
    # 여기서는 안내만 반환.
    return jsonify({
        'error': '단지명 직접 검색은 현재 미지원. /api/danji/search?bjd_code=... 사용 권장',
        'hint': '시도/시군구/읍면동을 먼저 선택한 후 해당 동의 단지 목록에서 매칭하세요.',
    }), 400


@app.route('/api/danji/info/<kapt_code>')
def get_danji_info(kapt_code):
    """단지코드(kaptCode)로 단지 기본정보 + 상세정보 통합 조회."""
    if not API_KEY:
        return jsonify({'error': 'API 키 미설정'}), 500
    if not kapt_code or not kapt_code.startswith('A'):
        return jsonify({'error': '유효한 kaptCode 필요 (A로 시작)'}), 400
    try:
        # 기본정보
        basis_xml = fetch_apt_basis_cached(kapt_code, cache_ts())
        basis_items, err = parse_kapt_response(basis_xml)
        if err:
            return jsonify({'error': f'기본정보 조회 실패: {err}'}), 502
        if not basis_items:
            return jsonify({'error': '단지 정보를 찾을 수 없습니다.'}), 404
        b = basis_items[0]
        
        # 사용승인일 포맷팅 (YYYYMMDD → YYYY-MM-DD)
        usedate = safe_get(b, 'kaptUsedate')
        if len(usedate) == 8:
            usedate = f'{usedate[:4]}-{usedate[4:6]}-{usedate[6:8]}'
        
        result = {
            'kaptCode': safe_get(b, 'kaptCode'),
            'name': safe_get(b, 'kaptName'),
            'addrLot': safe_get(b, 'kaptAddr'),  # 지번주소
            'addrRoad': safe_get(b, 'doroJuso'),  # 도로명주소
            'totalUnits': safe_get(b, 'kaptdaCnt'),  # 세대수
            'totalDongs': safe_get(b, 'kaptDongCnt'),  # 동수
            'totalHo': safe_get(b, 'hoCnt'),  # 호수
            'completionDate': usedate,
            'contractor': safe_get(b, 'kaptBcompany'),  # 시공사
            'developer': safe_get(b, 'kaptAcompany'),  # 시행사
            'totalArea': safe_get(b, 'kaptTarea'),  # 연면적
            'usage': safe_get(b, 'codeAptNm'),  # 단지분류 (아파트/주상복합 등)
            'mgmtMethod': safe_get(b, 'codeMgrNm'),  # 관리방식
            'hallType': safe_get(b, 'codeHallNm'),  # 복도유형
            'heatingType': safe_get(b, 'codeHeatNm'),  # 난방방식
            'saleType': safe_get(b, 'codeSaleNm'),  # 분양형태
            'tel': safe_get(b, 'kaptTel'),  # 관리사무소 전화
            'url': safe_get(b, 'kaptUrl'),  # 홈페이지
            'bjdCode': safe_get(b, 'bjdCode'),
            'privArea': safe_get(b, 'privArea'),  # 단지 전용면적합
            # 면적별 세대수 (참고용)
            'mparea_60': safe_get(b, 'kaptMparea_60'),
            'mparea_85': safe_get(b, 'kaptMparea_85'),
            'mparea_135': safe_get(b, 'kaptMparea_135'),
            'mparea_136': safe_get(b, 'kaptMparea_136'),
        }
        
        # 상세정보 (선택적 - 실패해도 무시)
        try:
            detail_xml = fetch_apt_detail_cached(kapt_code, cache_ts())
            detail_items, _ = parse_kapt_response(detail_xml)
            if detail_items:
                d = detail_items[0]
                result['parking'] = {
                    'total': safe_get(d, 'kaptdPcnt'),  # 총주차대수
                    'underground': safe_get(d, 'kaptdPcntu'),  # 지하주차대수
                }
                result['cctv'] = safe_get(d, 'kaptdCccnt')  # CCTV 대수
                result['structure'] = safe_get(d, 'codeStr')  # 건물구조
        except Exception:
            pass
        
        return jsonify(result)
    except requests.exceptions.HTTPError as e:
        return jsonify({'error': f'국토부 API HTTP 오류: {e}'}), 502
    except Exception as e:
        return jsonify({'error': f'서버 오류: {e}'}), 500


# ============================================================
# 건축물대장 (호별 면적 + 공시가격)
# ============================================================

@lru_cache(maxsize=512)
def fetch_br_title_cached(sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji, _ts):
    """건축물대장 표제부 조회."""
    params = {
        'serviceKey': API_KEY,
        'sigunguCd': sigungu_cd,
        'bjdongCd': bjdong_cd,
        'platGbCd': plat_gb_cd,  # 0:대지, 1:산, 2:블록
        'bun': bun.zfill(4),
        'ji': ji.zfill(4),
        'numOfRows': '100',
        'pageNo': '1',
    }
    r = requests.get(URL_BR_TITLE, params=params, timeout=30)
    r.raise_for_status()
    return r.text


@lru_cache(maxsize=512)
def fetch_br_expose_cached(sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji, _ts):
    """건축물대장 전유공용면적 조회 (호별 면적)."""
    params = {
        'serviceKey': API_KEY,
        'sigunguCd': sigungu_cd,
        'bjdongCd': bjdong_cd,
        'platGbCd': plat_gb_cd,
        'bun': bun.zfill(4),
        'ji': ji.zfill(4),
        'numOfRows': '1000',
        'pageNo': '1',
    }
    r = requests.get(URL_BR_EXPOSE, params=params, timeout=30)
    r.raise_for_status()
    return r.text


@lru_cache(maxsize=512)
def fetch_br_price_cached(sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji, _ts):
    """건축물대장 주택가격(공시가격) 조회."""
    params = {
        'serviceKey': API_KEY,
        'sigunguCd': sigungu_cd,
        'bjdongCd': bjdong_cd,
        'platGbCd': plat_gb_cd,
        'bun': bun.zfill(4),
        'ji': ji.zfill(4),
        'numOfRows': '1000',
        'pageNo': '1',
    }
    r = requests.get(URL_BR_PRICE, params=params, timeout=30)
    r.raise_for_status()
    return r.text


@app.route('/api/building/lookup')
def lookup_building():
    """건축물대장 통합 조회 (표제부 + 전유공용면적 + 공시가격).
    Query params:
        sigungu_cd: 시군구코드 5자리 (필수)
        bjdong_cd: 법정동코드 뒷 5자리 (필수)
        bun: 본번 (필수)
        ji: 부번 (선택, 기본 0)
        plat_gb_cd: 0(대지) | 1(산) | 2(블록), 기본 0
        dong_nm: 동 이름 필터 (선택, 예: "1208동")
        ho_nm: 호 이름 필터 (선택, 예: "1904호")
    """
    if not API_KEY:
        return jsonify({'error': 'API 키 미설정'}), 500
    sigungu_cd = request.args.get('sigungu_cd', '').strip()
    bjdong_cd = request.args.get('bjdong_cd', '').strip()
    bun = request.args.get('bun', '').strip()
    ji = request.args.get('ji', '0').strip()
    plat_gb_cd = request.args.get('plat_gb_cd', '0').strip()
    dong_nm_filter = request.args.get('dong_nm', '').strip()
    ho_nm_filter = request.args.get('ho_nm', '').strip()
    
    if not (sigungu_cd and bjdong_cd and bun):
        return jsonify({'error': 'sigungu_cd, bjdong_cd, bun 모두 필수'}), 400
    
    result = {'title': [], 'units': [], 'prices': [], 'errors': []}
    
    # 표제부
    try:
        xml_text = fetch_br_title_cached(sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji, cache_ts())
        items, err = parse_xml_items(xml_text)
        if err:
            result['errors'].append(f'표제부: {err}')
        else:
            for x in items:
                result['title'].append({
                    'dongNm': safe_get(x, 'dongNm'),
                    'mainPurpsCdNm': safe_get(x, 'mainPurpsCdNm'),  # 주용도
                    'strctCdNm': safe_get(x, 'strctCdNm'),  # 구조
                    'totArea': safe_get(x, 'totArea'),  # 연면적
                    'platArea': safe_get(x, 'platArea'),  # 대지면적
                    'archArea': safe_get(x, 'archArea'),  # 건축면적
                    'grndFlrCnt': safe_get(x, 'grndFlrCnt'),  # 지상층수
                    'ugrndFlrCnt': safe_get(x, 'ugrndFlrCnt'),  # 지하층수
                    'hhldCnt': safe_get(x, 'hhldCnt'),  # 세대수
                    'useAprDay': safe_get(x, 'useAprDay'),  # 사용승인일
                    'newPlatPlc': safe_get(x, 'newPlatPlc'),  # 도로명주소
                    'platPlc': safe_get(x, 'platPlc'),  # 지번주소
                })
    except Exception as e:
        result['errors'].append(f'표제부: {e}')
    
    # 전유공용면적
    try:
        xml_text = fetch_br_expose_cached(sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji, cache_ts())
        items, err = parse_xml_items(xml_text)
        if err:
            result['errors'].append(f'전유공용면적: {err}')
        else:
            for x in items:
                dong = safe_get(x, 'dongNm')
                ho = safe_get(x, 'hoNm')
                # 동·호 필터링
                if dong_nm_filter and dong_nm_filter.replace(' ', '') not in dong.replace(' ', ''):
                    continue
                if ho_nm_filter and ho_nm_filter.replace(' ', '') not in ho.replace(' ', ''):
                    continue
                result['units'].append({
                    'dongNm': dong,
                    'hoNm': ho,
                    'flrNoNm': safe_get(x, 'flrNoNm'),  # 층번호
                    'exposPubuseGbCdNm': safe_get(x, 'exposPubuseGbCdNm'),  # 전유/공용
                    'mainAtchGbCdNm': safe_get(x, 'mainAtchGbCdNm'),  # 주/부속
                    'area': safe_get(x, 'area'),  # 면적
                    'mainPurpsCdNm': safe_get(x, 'mainPurpsCdNm'),  # 주용도
                    'etcPurps': safe_get(x, 'etcPurps'),  # 기타용도
                    'strctCdNm': safe_get(x, 'strctCdNm'),  # 구조
                })
    except Exception as e:
        result['errors'].append(f'전유공용면적: {e}')
    
    # 공시가격
    try:
        xml_text = fetch_br_price_cached(sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji, cache_ts())
        items, err = parse_xml_items(xml_text)
        if err:
            result['errors'].append(f'공시가격: {err}')
        else:
            for x in items:
                dong = safe_get(x, 'dongNm')
                ho = safe_get(x, 'hoNm')
                if dong_nm_filter and dong_nm_filter.replace(' ', '') not in dong.replace(' ', ''):
                    continue
                if ho_nm_filter and ho_nm_filter.replace(' ', '') not in ho.replace(' ', ''):
                    continue
                result['prices'].append({
                    'dongNm': dong,
                    'hoNm': ho,
                    'bldRgstPc': safe_get(x, 'bldRgstPc'),  # 건축물 공시가격
                    'bldRgstStdDay': safe_get(x, 'bldRgstStdDay'),  # 기준일
                })
    except Exception as e:
        result['errors'].append(f'공시가격: {e}')
    
    return jsonify(result)


# ============================================================
# 연립다세대 실거래가
# ============================================================

@lru_cache(maxsize=256)
def fetch_rh_trade_cached(lawd_cd, year_month, _ts):
    """연립다세대 매매."""
    params = {
        'serviceKey': API_KEY,
        'LAWD_CD': lawd_cd,
        'DEAL_YMD': year_month,
        'numOfRows': '1000',
        'pageNo': '1',
    }
    r = requests.get(URL_RH_TRADE, params=params, timeout=30)
    r.raise_for_status()
    return r.text


@lru_cache(maxsize=256)
def fetch_rh_rent_cached(lawd_cd, year_month, _ts):
    """연립다세대 전월세."""
    params = {
        'serviceKey': API_KEY,
        'LAWD_CD': lawd_cd,
        'DEAL_YMD': year_month,
        'numOfRows': '1000',
        'pageNo': '1',
    }
    r = requests.get(URL_RH_RENT, params=params, timeout=30)
    r.raise_for_status()
    return r.text


def normalize_rh_trade_item(raw):
    """연립다세대 매매 항목 정규화."""
    deal_amount = (raw.get('dealAmount') or '').replace(',', '').strip()
    try:
        price = int(deal_amount) if deal_amount else None
    except ValueError:
        price = None
    year = raw.get('dealYear', '')
    month = raw.get('dealMonth', '').zfill(2)
    day = raw.get('dealDay', '').zfill(2)
    date = f'{year}-{month}-{day}' if year and month and day else ''
    try:
        area = float(raw.get('excluUseAr', '0') or 0)
    except ValueError:
        area = 0
    try:
        floor = int(raw.get('floor', '') or 0) or None
    except ValueError:
        floor = None
    return {
        'date': date,
        'name': raw.get('mhouseNm', '') or raw.get('houseType', ''),  # 다세대명
        'building': '',
        'area': round(area, 2),
        'floor': floor,
        'price': price,
        'type': '매매',
        'memo': raw.get('cdealType', '') or raw.get('houseType', ''),
        'jibun': raw.get('jibun', ''),
        'dong': raw.get('umdNm', ''),
        'category': '연립다세대',
    }


def normalize_rh_rent_item(raw):
    """연립다세대 전월세 항목 정규화."""
    deposit = (raw.get('deposit') or '').replace(',', '').strip()
    monthly = (raw.get('monthlyRent') or '').replace(',', '').strip()
    try:
        deposit = int(deposit) if deposit else None
    except ValueError:
        deposit = None
    try:
        monthly = int(monthly) if monthly else 0
    except ValueError:
        monthly = 0
    year = raw.get('dealYear', '')
    month = raw.get('dealMonth', '').zfill(2)
    day = raw.get('dealDay', '').zfill(2)
    date = f'{year}-{month}-{day}' if year and month and day else ''
    try:
        area = float(raw.get('excluUseAr', '0') or 0)
    except ValueError:
        area = 0
    try:
        floor = int(raw.get('floor', '') or 0) or None
    except ValueError:
        floor = None
    return {
        'date': date,
        'name': raw.get('mhouseNm', '') or raw.get('houseType', ''),
        'area': round(area, 2),
        'floor': floor,
        'price': deposit,
        'monthly': monthly,
        'type': '월세' if monthly > 0 else '전세',
        'jibun': raw.get('jibun', ''),
        'dong': raw.get('umdNm', ''),
        'category': '연립다세대',
    }


@app.route('/api/transactions/rh-bulk')
def get_rh_transactions_bulk():
    """연립다세대 다월 일괄 조회.
    Query params: 아파트 bulk와 동일 (lawd_cd, months, danji_name, min_area, max_area)
    """
    if not API_KEY:
        return jsonify({'error': 'API 키 미설정'}), 500
    lawd_cd = request.args.get('lawd_cd', '').strip()
    months = request.args.get('months', default=6, type=int)
    danji_filter = request.args.get('danji_name', '').strip()
    min_area = request.args.get('min_area', type=float)
    max_area = request.args.get('max_area', type=float)
    include_rent = request.args.get('include_rent', default='true') == 'true'
    
    if not lawd_cd:
        return jsonify({'error': 'lawd_cd 필수'}), 400
    if months < 1 or months > 24:
        return jsonify({'error': 'months는 1~24'}), 400
    
    now = datetime.now()
    year_months = []
    y, m = now.year, now.month
    for _ in range(months):
        year_months.append(f'{y}{m:02d}')
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    
    all_items = []
    errors = []
    for ym in year_months:
        try:
            xml_text = fetch_rh_trade_cached(lawd_cd, ym, cache_ts())
            raw_items, err = parse_xml_items(xml_text)
            if not err:
                all_items.extend(normalize_rh_trade_item(x) for x in raw_items)
            else:
                errors.append(f'{ym} 매매: {err}')
            if include_rent:
                xml_text = fetch_rh_rent_cached(lawd_cd, ym, cache_ts())
                raw_items, err = parse_xml_items(xml_text)
                if not err:
                    all_items.extend(normalize_rh_rent_item(x) for x in raw_items)
        except Exception as e:
            errors.append(f'{ym}: {e}')
    
    if danji_filter:
        nf = danji_filter.replace(' ', '').lower()
        all_items = [x for x in all_items if nf in x['name'].replace(' ', '').lower()]
    if min_area is not None:
        all_items = [x for x in all_items if x['area'] >= min_area]
    if max_area is not None:
        all_items = [x for x in all_items if x['area'] <= max_area]
    
    all_items.sort(key=lambda x: x.get('date', ''), reverse=True)
    
    return jsonify({
        'count': len(all_items),
        'items': all_items,
        'months_queried': year_months,
        'errors': errors,
    })


# ============================================================
# 신규: 자동완성 검색 (Supabase DB 기반)
# 전국 법정동(약 5만건) + 아파트 단지(약 1.8만개)를 빠르게 검색
# ============================================================

@app.route('/api/search/dong')
def search_dong():
    """동 이름 자동완성. (예: ?q=방학동)
    Query params:
        q: 검색어 (필수, 1자 이상)
        limit: 결과 개수 (기본 10, 최대 30)
    Returns:
        items: [{bjd_code, sido, sigungu, dong, sigungu_cd, dong_cd}]
    """
    if not supabase:
        return jsonify({
            'error': 'Supabase 미연결. 환경변수(SUPABASE_URL/SUPABASE_KEY) 확인 또는 supabase 패키지 설치 필요.',
            'lib_installed': HAS_SUPABASE_LIB,
            'url_set': bool(SUPABASE_URL),
            'key_set': bool(SUPABASE_KEY),
        }), 503
    q = request.args.get('q', '').strip()
    limit = min(request.args.get('limit', default=10, type=int), 30)
    if len(q) < 1:
        return jsonify({'error': 'q(검색어)는 1자 이상이어야 합니다.'}), 400
    try:
        resp = (
            supabase.table('legal_dong')
            .select('bjd_code, sido, sigungu, dong, sigungu_cd, dong_cd')
            .ilike('dong', f'%{q}%')
            .eq('is_active', True)
            .limit(limit)
            .execute()
        )
        return jsonify({'count': len(resp.data), 'items': resp.data})
    except Exception as e:
        return jsonify({'error': f'Supabase 조회 오류: {e}'}), 500


@app.route('/api/search/apt')
def search_apt():
    """아파트 단지명 자동완성. (예: ?q=삼익세라믹)
    Query params:
        q: 검색어 (필수, 2자 이상)
        sido: 시도 필터 (선택)
        sigungu: 시군구 필터 (선택)
        limit: 결과 개수 (기본 10, 최대 30)
    Returns:
        items: [{kapt_code, kapt_name, sido, sigungu, dong, addr_road, total_units, ...}]
    """
    if not supabase:
        return jsonify({'error': 'Supabase 미연결.'}), 503
    q = request.args.get('q', '').strip()
    sido = request.args.get('sido', '').strip()
    sigungu = request.args.get('sigungu', '').strip()
    limit = min(request.args.get('limit', default=10, type=int), 30)
    if len(q) < 2:
        return jsonify({'error': 'q(단지명)는 2자 이상이어야 합니다.'}), 400
    try:
        query = (
            supabase.table('apt_master')
            .select(
                'kapt_code, kapt_name, bjd_code, sido, sigungu, dong, '
                'addr_road, addr_lot, total_units, total_dongs, completion_date'
            )
            .ilike('kapt_name', f'%{q}%')
        )
        if sido:
            query = query.eq('sido', sido)
        if sigungu:
            query = query.eq('sigungu', sigungu)
        resp = query.limit(limit).execute()
        return jsonify({'count': len(resp.data), 'items': resp.data})
    except Exception as e:
        return jsonify({'error': f'Supabase 조회 오류: {e}'}), 500


@app.route('/api/search/address')
def search_address():
    """주소 자동 파싱 → 동/단지 후보 반환.
    
    예시:
        ?q=서울 도봉구 방학동 274
        → 토큰화하여 '방학동' 동 후보 추출 → '서울', '도봉구'로 필터링 → 단지 목록 조회
    
    Query params:
        q: 주소 (필수, 공백 구분)
    Returns:
        query, tokens, dong_candidates, apt_candidates
    """
    if not supabase:
        return jsonify({'error': 'Supabase 미연결.'}), 503
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'error': 'q(주소) 필수'}), 400
    
    # 주소 토큰화 (콤마/공백 구분)
    tokens = q.replace(',', ' ').split()
    if not tokens:
        return jsonify({'error': '주소 형식 오류'}), 400
    
    try:
        # 1. 동 이름 후보 추출 ('~동/읍/면'으로 끝나는 토큰)
        dong_candidates = []
        for tok in tokens:
            if tok and tok[-1] in ('동', '읍', '면'):
                resp = (
                    supabase.table('legal_dong')
                    .select('bjd_code, sido, sigungu, dong, sigungu_cd, dong_cd')
                    .eq('dong', tok)
                    .eq('is_active', True)
                    .limit(20)
                    .execute()
                )
                dong_candidates.extend(resp.data or [])
        
        # 2. 시도/시군구 키워드로 좁히기 (다른 토큰이 시도/시군구에 매칭되면 필터링)
        for tok in tokens:
            if not dong_candidates:
                break
            filtered = [
                d for d in dong_candidates
                if tok in (d.get('sido') or '') or tok in (d.get('sigungu') or '')
            ]
            if filtered:
                dong_candidates = filtered
        
        # 3. 매칭된 동의 단지 목록 (최대 5개 동 × 20개 단지 = 100)
        apt_candidates = []
        seen_codes = set()
        for d in dong_candidates[:5]:
            resp = (
                supabase.table('apt_master')
                .select(
                    'kapt_code, kapt_name, sido, sigungu, dong, '
                    'addr_road, addr_lot, total_units'
                )
                .eq('bjd_code', d['bjd_code'])
                .limit(20)
                .execute()
            )
            for a in (resp.data or []):
                if a['kapt_code'] not in seen_codes:
                    seen_codes.add(a['kapt_code'])
                    apt_candidates.append(a)
        
        return jsonify({
            'query': q,
            'tokens': tokens,
            'dong_candidates': dong_candidates,
            'apt_candidates': apt_candidates,
        })
    except Exception as e:
        return jsonify({'error': f'Supabase 조회 오류: {e}'}), 500


# ============================================================
# 관리자: 데이터 로드 엔드포인트 (Step 4-5: 법정동/단지 마스터 적재)
# - 보안: ADMIN_SECRET 환경변수와 일치하는 ?key= 파라미터 필요
# - 동작: 청크 단위(소량씩)로 Supabase upsert → 타임아웃 회피
# - UI: /admin/load 에서 자동 진행 (JS가 반복 호출)
# ============================================================

# 데이터 파일 경로 (backend/data/legal_dong_data.json)
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
_LEGAL_DONG_FILE = os.path.join(_DATA_DIR, 'legal_dong_data.json')

# 데이터 캐시 (한 번만 메모리에 로드)
_legal_dong_cache = None


def _load_legal_dong_file():
    """legal_dong_data.json 파일을 메모리에 로드 (캐시)."""
    global _legal_dong_cache
    if _legal_dong_cache is None:
        try:
            import json as _json
            with open(_LEGAL_DONG_FILE, encoding='utf-8') as f:
                _legal_dong_cache = _json.load(f)
            print(f'[INFO] legal_dong_data.json 로드: {len(_legal_dong_cache)}건')
        except FileNotFoundError:
            print(f'[ERROR] 데이터 파일 없음: {_LEGAL_DONG_FILE}')
            _legal_dong_cache = []
        except Exception as e:
            print(f'[ERROR] 데이터 파일 로드 실패: {e}')
            _legal_dong_cache = []
    return _legal_dong_cache


def _check_admin(req):
    """관리자 인증 체크."""
    if not ADMIN_SECRET:
        return False, 'ADMIN_SECRET 환경변수가 설정되지 않았습니다.'
    key = req.args.get('key', '')
    if key != ADMIN_SECRET:
        return False, '잘못된 관리자 키.'
    return True, None


@app.route('/admin/load')
def admin_load_page():
    """데이터 로드 진행 페이지 (HTML).
    URL: /admin/load?key=ADMIN_SECRET
    """
    key = request.args.get('key', '')
    if not ADMIN_SECRET:
        return '<h1>ADMIN_SECRET 환경변수가 설정되지 않았습니다.</h1>', 503
    if key != ADMIN_SECRET:
        return '<h1>잘못된 관리자 키입니다.</h1>', 403

    html = '''<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>관리자: 데이터 로드</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, "Apple SD Gothic Neo", sans-serif; padding: 30px; background: #f5f5f7; color: #1d1d1f; }
.container { max-width: 760px; margin: 0 auto; }
h1 { font-size: 24px; margin-bottom: 8px; }
h2 { font-size: 18px; margin: 24px 0 12px; }
.card { background: white; border-radius: 14px; padding: 20px 24px; margin-bottom: 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.04); }
.row { display: flex; align-items: center; gap: 12px; margin: 12px 0; }
button { background: #0a3a6e; color: white; border: 0; padding: 10px 18px; border-radius: 8px; font-size: 14px; cursor: pointer; font-weight: 600; }
button:hover { background: #082c54; }
button:disabled { background: #ccc; cursor: not-allowed; }
.bar-wrap { width: 100%; height: 22px; background: #e8e8ed; border-radius: 11px; overflow: hidden; }
.bar { height: 100%; background: linear-gradient(90deg, #0a3a6e, #1056a6); transition: width 0.2s; width: 0; }
.log { font-family: ui-monospace, "Courier New", monospace; font-size: 12px; background: #1d1d1f; color: #f5f5f7; padding: 12px; border-radius: 8px; max-height: 280px; overflow-y: auto; white-space: pre-wrap; line-height: 1.5; }
.muted { color: #6e6e73; font-size: 13px; }
.warn { color: #ff5a1f; font-weight: 600; }
.ok { color: #0a8a3a; font-weight: 600; }
</style>
</head>
<body>
<div class="container">
<h1>📊 관리자: 데이터 로드</h1>
<p class="muted">법정동 마스터 + 아파트 단지 마스터를 Supabase DB에 채웁니다. 한 번만 실행하시면 됩니다.</p>

<div class="card">
<h2>1️⃣ 법정동 마스터 (약 20,000건)</h2>
<p class="muted">행정표준코드관리시스템 출처. 동/읍/면 + 리 단위 전체.</p>
<div class="row">
<button id="btn-dong" onclick="loadDong()">로드 시작</button>
<span id="dong-status" class="muted">대기 중</span>
</div>
<div class="bar-wrap"><div id="dong-bar" class="bar"></div></div>
<div id="dong-log" class="log" style="display:none;margin-top:12px;"></div>
</div>

<div class="card">
<h2>2️⃣ 아파트 단지 마스터 (약 18,000개)</h2>
<p class="muted">⚠️ 1번 완료 후 진행하세요. K-apt V3 API getTotalAptList3로 페이지당 1000개씩 일괄 조회 (약 2~5분 소요).</p>
<div class="row">
<button id="btn-apt" onclick="loadApt()" disabled>1번 먼저 완료</button>
<span id="apt-status" class="muted">대기 중</span>
</div>
<div class="bar-wrap"><div id="apt-bar" class="bar"></div></div>
<div id="apt-log" class="log" style="display:none;margin-top:12px;"></div>
</div>

<p class="muted" style="text-align:center;margin-top:20px;">⚠️ 페이지를 닫지 마세요. 닫으면 진행이 멈춥니다 (다시 열면 이어서 진행됩니다).</p>
</div>

<script>
const KEY = new URLSearchParams(location.search).get('key');
let dongRunning = false, aptRunning = false;

function setBar(id, percent) {
  document.getElementById(id).style.width = percent + '%';
}
function logLine(id, text) {
  const el = document.getElementById(id);
  el.style.display = 'block';
  el.textContent += text + '\\n';
  el.scrollTop = el.scrollHeight;
}

async function loadDong() {
  if (dongRunning) return;
  dongRunning = true;
  document.getElementById('btn-dong').disabled = true;
  document.getElementById('dong-status').textContent = '진행 중...';
  document.getElementById('dong-log').style.display = 'block';
  document.getElementById('dong-log').textContent = '';

  let offset = 0;
  const size = 500;
  let total = null;
  while (true) {
    const url = `/api/admin/load-legal-dong?key=${encodeURIComponent(KEY)}&offset=${offset}&size=${size}`;
    let r;
    try {
      r = await fetch(url);
    } catch (e) {
      logLine('dong-log', '❌ 네트워크 오류: ' + e.message + ' (10초 후 재시도)');
      await new Promise(res => setTimeout(res, 10000));
      continue;
    }
    const j = await r.json();
    if (j.error) {
      logLine('dong-log', '❌ ' + j.error);
      document.getElementById('dong-status').innerHTML = '<span class="warn">실패</span>';
      dongRunning = false;
      document.getElementById('btn-dong').disabled = false;
      return;
    }
    total = j.total;
    const inserted = j.inserted_so_far;
    const pct = total > 0 ? Math.round(inserted / total * 100) : 0;
    setBar('dong-bar', pct);
    document.getElementById('dong-status').textContent = `${inserted} / ${total} (${pct}%)`;
    logLine('dong-log', `✓ ${j.this_chunk}건 적재 (누적 ${inserted}/${total})`);
    if (j.done) {
      document.getElementById('dong-status').innerHTML = '<span class="ok">완료!</span>';
      logLine('dong-log', '🎉 법정동 마스터 적재 완료!');
      document.getElementById('btn-apt').disabled = false;
      document.getElementById('btn-apt').textContent = '로드 시작';
      dongRunning = false;
      return;
    }
    offset += size;
  }
}

async function loadApt() {
  if (aptRunning) return;
  aptRunning = true;
  document.getElementById('btn-apt').disabled = true;
  document.getElementById('apt-status').textContent = '진행 중...';
  document.getElementById('apt-log').style.display = 'block';
  document.getElementById('apt-log').textContent = '';

  let offset = 0;
  const size = 1;  // V3 API에서는 1페이지(1000개)씩 처리
  while (true) {
    const url = `/api/admin/load-apt-master?key=${encodeURIComponent(KEY)}&offset=${offset}&size=${size}`;
    let r;
    try {
      r = await fetch(url);
    } catch (e) {
      logLine('apt-log', '❌ 네트워크 오류: ' + e.message + ' (15초 후 재시도)');
      await new Promise(res => setTimeout(res, 15000));
      continue;
    }
    const j = await r.json();
    if (j.error) {
      logLine('apt-log', '❌ ' + j.error);
      document.getElementById('apt-status').innerHTML = '<span class="warn">실패</span>';
      aptRunning = false;
      document.getElementById('btn-apt').disabled = false;
      return;
    }
    const total = j.total_dongs;
    const processed = j.processed_dongs;
    const inserted = j.inserted_apts_total;
    const pct = total > 0 ? Math.round(processed / total * 100) : 0;
    setBar('apt-bar', pct);
    document.getElementById('apt-status').textContent =
      `${processed} / ${total} 처리 (단지 ${inserted}개) (${pct}%)`;
    if (j.this_inserted > 0 || j.this_processed > 0) {
      logLine('apt-log', `✓ 페이지 ${j.this_processed}개 처리: 단지 +${j.this_inserted} (누적 ${inserted}개)`);
    }
    if (j.done) {
      document.getElementById('apt-status').innerHTML = '<span class="ok">완료!</span>';
      logLine('apt-log', `🎉 단지 마스터 적재 완료! 총 ${inserted}개 단지.`);
      aptRunning = false;
      return;
    }
    offset += size;
  }
}
</script>
</body>
</html>'''
    return html


@app.route('/api/admin/load-legal-dong')
def admin_load_legal_dong():
    """법정동 데이터를 청크 단위로 Supabase에 적재.
    Query: key=ADMIN_SECRET, offset=N, size=500
    """
    ok, msg = _check_admin(request)
    if not ok:
        return jsonify({'error': msg}), 403
    if not supabase:
        return jsonify({'error': 'Supabase 미연결'}), 503

    offset = request.args.get('offset', default=0, type=int)
    size = min(request.args.get('size', default=500, type=int), 1000)

    data = _load_legal_dong_file()
    total = len(data)
    if total == 0:
        return jsonify({'error': '데이터 파일이 비어있거나 없음'}), 500

    chunk = data[offset:offset + size]
    if not chunk:
        return jsonify({
            'done': True,
            'total': total,
            'inserted_so_far': total,
            'this_chunk': 0,
        })

    try:
        # upsert (이미 있는 bjd_code는 업데이트)
        supabase.table('legal_dong').upsert(chunk, on_conflict='bjd_code').execute()
    except Exception as e:
        return jsonify({'error': f'Supabase upsert 오류: {e}'}), 500

    inserted_so_far = offset + len(chunk)
    return jsonify({
        'done': inserted_so_far >= total,
        'total': total,
        'inserted_so_far': inserted_so_far,
        'this_chunk': len(chunk),
        'next_offset': offset + size,
    })


@app.route('/api/admin/load-apt-master')
def admin_load_apt_master():
    """K-apt V3 API getTotalAptList3로 전국 단지 목록 일괄 적재.
    페이징(numOfRows=1000)으로 한 번에 1000개씩, 약 18~20번 호출로 완료.
    Query: key=ADMIN_SECRET, offset=N (페이지 번호 0부터), size=1 (한 번에 처리할 페이지 수)
    """
    ok, msg = _check_admin(request)
    if not ok:
        return jsonify({'error': msg}), 403
    if not supabase:
        return jsonify({'error': 'Supabase 미연결'}), 503
    if not API_KEY:
        return jsonify({'error': 'MOLIT_API_KEY 미설정'}), 500

    # offset = 처리한 페이지 수 (0부터 시작)
    offset = request.args.get('offset', default=0, type=int)
    # size = 이번 요청에서 처리할 페이지 수 (1~3)
    size = min(request.args.get('size', default=1, type=int), 3)
    num_rows = 1000  # 페이지당 단지 수

    # K-apt API에서 전체 단지를 페이지 단위로 가져옴
    apts_to_insert = []
    total_count = 0
    pages_processed = 0
    errors = []

    for i in range(size):
        page_no = offset + i + 1  # 1-based page number
        params = {
            'serviceKey': API_KEY,
            'numOfRows': str(num_rows),
            'pageNo': str(page_no),
        }
        try:
            r = requests.get(URL_APT_LIST_TOTAL, params=params, timeout=60)
            r.raise_for_status()
            xml_text = r.text
        except requests.exceptions.HTTPError as e:
            errors.append(f'page {page_no} HTTP: {e}')
            continue
        except requests.exceptions.Timeout:
            errors.append(f'page {page_no}: 타임아웃')
            continue
        except Exception as e:
            errors.append(f'page {page_no}: {e}')
            continue

        # 응답 파싱 (V3 API: XML/JSON 자동 감지)
        raw_items, err = parse_kapt_response(xml_text)
        if err:
            errors.append(f'page {page_no}: {err}')
            continue

        # 첫 번째 호출에서 totalCount 추출 (XML/JSON 양쪽 시도)
        if i == 0:
            try:
                # XML 형식
                if xml_text.strip().startswith('<'):
                    root = ET.fromstring(xml_text)
                    tc = root.findtext('.//totalCount')
                    if tc and tc.isdigit():
                        total_count = int(tc)
                # JSON 형식
                elif xml_text.strip().startswith('{'):
                    import json as _json
                    data = _json.loads(xml_text)
                    body = data.get('response', {}).get('body', {})
                    tc = body.get('totalCount', 0)
                    if isinstance(tc, (int, str)) and str(tc).isdigit():
                        total_count = int(tc)
            except Exception:
                pass

        if not raw_items:
            # 더 이상 데이터 없음
            pages_processed = i  # 실제로 처리된 페이지 수
            break

        pages_processed = i + 1

        for x in raw_items:
            kapt_code = safe_get(x, 'kaptCode')
            kapt_name = safe_get(x, 'kaptName')
            if not kapt_code or not kapt_name:
                continue
            apts_to_insert.append({
                'kapt_code': kapt_code,
                'kapt_name': kapt_name,
                'kapt_name_normalized': kapt_name.replace(' ', '').lower(),
                'bjd_code': safe_get(x, 'bjdCode'),
                'sido': safe_get(x, 'as1'),
                'sigungu': safe_get(x, 'as2'),
                'dong': safe_get(x, 'as4') or safe_get(x, 'as3'),
            })

    # 중복 제거 + upsert
    unique_apts = {}
    for a in apts_to_insert:
        unique_apts[a['kapt_code']] = a
    apts_list = list(unique_apts.values())

    inserted_count = 0
    if apts_list:
        try:
            supabase.table('apt_master').upsert(apts_list, on_conflict='kapt_code').execute()
            inserted_count = len(apts_list)
        except Exception as e:
            return jsonify({'error': f'apt_master upsert 오류: {e}'}), 500

    # 단지 총 개수 조회 (이미 적재된 것 포함)
    try:
        apt_count_resp = (
            supabase.table('apt_master')
            .select('kapt_code', count='exact')
            .execute()
        )
        inserted_apts_total = apt_count_resp.count or 0
    except Exception:
        inserted_apts_total = 0

    # 다음 페이지 offset 계산
    next_offset = offset + pages_processed

    # 종료 조건: 이번에 가져온 행이 num_rows 미만이거나, totalCount 도달
    is_done = False
    if pages_processed == 0:  # 빈 응답 받음
        is_done = True
    elif total_count > 0 and next_offset * num_rows >= total_count:
        is_done = True
    elif len(apts_list) < num_rows * size:  # 마지막 페이지 (full size 안 됨)
        # totalCount를 확인 못했어도 페이지가 비어있으면 종료
        if pages_processed < size:
            is_done = True

    # 진행률 계산용 (loader UI 호환)
    if total_count > 0:
        # totalCount를 기반으로 진행률 산정
        processed_dongs_for_ui = min(next_offset * num_rows, total_count)
        total_dongs_for_ui = total_count
    else:
        # totalCount 못 받았으면 페이지 수 기반
        processed_dongs_for_ui = next_offset
        total_dongs_for_ui = 25  # 예상 페이지 수 (1만8천 / 1000 ≈ 18, 여유 25)

    return jsonify({
        'done': is_done,
        'total_dongs': total_dongs_for_ui,
        'processed_dongs': processed_dongs_for_ui,
        'this_processed': pages_processed,
        'this_inserted': inserted_count,
        'inserted_apts_total': inserted_apts_total,
        'total_apt_count_from_api': total_count,
        'errors': errors[-3:] if errors else [],
    })


@app.route('/api/admin/diag-kapt')
def admin_diag_kapt():
    """K-apt V3 API 응답을 raw 그대로 반환 (진단용).
    Query: key=ADMIN_SECRET, bjd_code=4113510300 (선택, 기본 분당 정자동)
    """
    ok, msg = _check_admin(request)
    if not ok:
        return jsonify({'error': msg}), 403
    if not API_KEY:
        return jsonify({'error': 'MOLIT_API_KEY 미설정'}), 500
    
    bjd_code = request.args.get('bjd_code', '4113510300').strip()
    test_total = request.args.get('total', '0') == '1'
    
    if test_total:
        url = URL_APT_LIST_TOTAL
        params = {
            'serviceKey': API_KEY,
            'numOfRows': '5',
            'pageNo': '1',
        }
    else:
        url = URL_APT_LIST_DONG
        params = {
            'serviceKey': API_KEY,
            'bjdCode': bjd_code,
            'numOfRows': '5',
            'pageNo': '1',
        }
    
    try:
        r = requests.get(url, params=params, timeout=30)
        return jsonify({
            'request_url': url,
            'status_code': r.status_code,
            'content_type': r.headers.get('Content-Type', ''),
            'response_first_1000_chars': r.text[:1000],
            'response_length': len(r.text),
        })
    except Exception as e:
        return jsonify({'error': str(e), 'request_url': url}), 500


# ============================================================
# 시작
# ============================================================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print('=' * 60)
    print('부동산 자산관리 백엔드 서버 (v2.4-robust)')
    print('=' * 60)
    print(f'API 키 설정: {"O" if API_KEY else "X (.env 파일에 MOLIT_API_KEY 추가 필요)"}')
    print(f'Supabase 연결: {"O" if supabase else "X (선택사항 - 자동완성만 비활성화)"}')
    print(f'법정동 코드: {len(LAWD_CODES)}건 로드됨')
    print(f'서버 시작: http://localhost:{port}')
    print(f'프론트엔드: http://localhost:{port}')
    print(f'API 헬스체크: http://localhost:{port}/api/health')
    print('=' * 60)
    app.run(host='0.0.0.0', port=port, debug=False)
