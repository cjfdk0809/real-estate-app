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
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor  # 🆕 거래사례 월별 병렬 조회

from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import requests
from dotenv import load_dotenv

from lawd_codes import LAWD_CODES, find_lawd_code
from registry_analyzer import registry_bp  # 🆕 등기부 분석 모듈
from housing_price_api import housing_bp  # 🆕 공시가격 조회 모듈
from complex_identifier import identify_complex, identify_dong  # 🆕 Day 8: 단지 교차검증

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
# ============================================================
# 🔒 보안: 에러 메시지 마스킹 함수 (v2.10-sec)
# ============================================================
def mask_sensitive_info(text):
    """에러 메시지에서 인증키 등 민감 정보 자동 마스킹."""
    if not text:
        return text
    text = str(text)
    text = re.sub(r'serviceKey=[^&\s\'"]+', 'serviceKey=***MASKED***', text)
    text = re.sub(r'api[-_]?key[=:]\s*[^&\s\'"]+', 'api-key=***MASKED***', text, flags=re.IGNORECASE)
    text = re.sub(r'authorization[:=]\s*[^\s,\'"]+', 'authorization: ***MASKED***', text, flags=re.IGNORECASE)
    text = re.sub(r'Bearer\s+[^\s,\'"]+', 'Bearer ***MASKED***', text)
    if 'http' in text and len(text) > 300:
        text = re.sub(r'(https?://[^\s]{50})[^\s]{50,}', r'\1...[URL_TRUNCATED]', text)
    return text


def safe_error(msg, e=None):
    """안전한 에러 응답 생성. 모든 오류 메시지에 마스킹 적용."""
    if e is not None:
        return mask_sensitive_info(f'{msg}: {e}')
    return mask_sensitive_info(msg)
API_KEY = os.environ.get('MOLIT_API_KEY', '').strip()
VWORLD_API_KEY = os.environ.get('VWORLD_API_KEY', '').strip()  # V-World 지오코더 (주소→좌표) — 폴백
URL_VWORLD_GEOCODE = 'https://api.vworld.kr/req/address'
# 🆕 카카오 로컬 지오코더 (주소→좌표) — 해외 서버(Render 등)에서도 작동. 기본 지오코더.
KAKAO_REST_KEY = os.environ.get('KAKAO_REST_KEY', '').strip()
URL_KAKAO_GEOCODE = 'https://dapi.kakao.com/v2/local/search/address.json'

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

# 오피스텔 실거래가
URL_OFFI_TRADE = 'https://apis.data.go.kr/1613000/RTMSDataSvcOffiTrade/getRTMSDataSvcOffiTrade'  # 매매
URL_OFFI_RENT = 'https://apis.data.go.kr/1613000/RTMSDataSvcOffiRent/getRTMSDataSvcOffiRent'  # 전월세

# 단독/다가구 실거래가 (전용면적·층 없음 → 연면적/대지면적/주택유형)
URL_SH_TRADE = 'https://apis.data.go.kr/1613000/RTMSDataSvcSHTrade/getRTMSDataSvcSHTrade'  # 매매
URL_SH_RENT = 'https://apis.data.go.kr/1613000/RTMSDataSvcSHRent/getRTMSDataSvcSHRent'  # 전월세

# 공동주택 단지 정보 (K-apt) -  API (2025년 업그레이드)
URL_APT_LIST_DONG = 'https://apis.data.go.kr/1613000/AptListService3/getLegaldongAptList3'  # 법정동별 단지목록
URL_APT_LIST_ROAD = 'https://apis.data.go.kr/1613000/AptListService3/getRoadnameAptList3'  # 도로명별 단지목록
URL_APT_LIST_TOTAL = 'https://apis.data.go.kr/1613000/AptListService3/getTotalAptList3'  # 전체 단지목록 (한번에 모두)
URL_APT_BASIS = 'https://apis.data.go.kr/1613000/AptBasisInfoServiceV4/getAphusBassInfoV4'  # 단지 기본정보
URL_APT_DETAIL = 'https://apis.data.go.kr/1613000/AptBasisInfoServiceV4/getAphusDtlInfoV4'  # 단지 상세정보

# 건축물대장 (HUB)
URL_BR_TITLE = 'https://apis.data.go.kr/1613000/BldRgstHubService/getBrTitleInfo'  # 표제부
URL_BR_EXPOSE = 'https://apis.data.go.kr/1613000/BldRgstHubService/getBrExposPubuseAreaInfo'  # 전유공용면적
URL_BR_PRICE = 'https://apis.data.go.kr/1613000/BldRgstHubService/getBrHsprcInfo'  # 주택가격(공시)

app = Flask(__name__, static_folder='../frontend', static_url_path='')
CORS(app)
app.register_blueprint(registry_bp)  # 🆕 등기부 분석 Blueprint 등록
app.register_blueprint(housing_bp)  # 🆕 공시가격 조회 Blueprint 등록

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
    """K-apt API 응답을 파싱 (XML/JSON 자동 감지, V3/V4 양쪽 지원).
    
    V3 (K-apt): response.body.items.item (items wrapper + item 리스트)
    V4 (신규): body.item (단수형, 객체 직접) ← 본 함수에서 새로 지원
    
    Returns: (items_list, error_message_or_None)
    """
    import json as _json
    text = (response_text or '').strip()
    if not text:
        return [], 'API 응답이 비어있음'
    
    # JSON 시도 (V4 디폴트 형식)
    if text[0] in ('{', '['):
        try:
            data = _json.loads(text)
            response_node = data.get('response', data) if isinstance(data, dict) else {}
            
            header = response_node.get('header', {}) if isinstance(response_node, dict) else {}
            result_code = str(header.get('resultCode', '')).strip()
            result_msg = str(header.get('resultMsg', '')).strip()
            if result_code and result_code not in ('00', '000'):
                return [], f'API 오류 [{result_code}]: {result_msg}'
            
            body = response_node.get('body', {}) if isinstance(response_node, dict) else {}
            items_wrapper = None
            if isinstance(body, dict):
                items_wrapper = body.get('items')
                # V4 호환: items가 없으면 item (단수형) 시도
                if items_wrapper is None or items_wrapper == '':
                    _item_direct = body.get('item')
                    if isinstance(_item_direct, dict):
                        items_wrapper = [_item_direct]
                    elif isinstance(_item_direct, list):
                        items_wrapper = _item_direct
            
            if items_wrapper is None or items_wrapper == '':
                return [], None
            
            if isinstance(items_wrapper, list):
                items = items_wrapper
            elif isinstance(items_wrapper, dict):
                inner = items_wrapper.get('item', [])
                if isinstance(inner, dict):
                    items = [inner]
                elif isinstance(inner, list):
                    items = inner
                else:
                    items = []
            else:
                items = []
            
            normalized = []
            for it in items:
                if isinstance(it, dict):
                    normalized.append({k: ('' if v is None else str(v).strip()) for k, v in it.items()})
            return normalized, None
        except _json.JSONDecodeError as e:
            return [], f'JSON 파싱 실패: {e}. 응답 앞부분: {text[:150]}'
    
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
        'build_year': (raw.get('buildYear') or '').strip(),
        'road': raw.get('roadNm', ''),  # 도로명 (상세 API) - 단지명+도로명 교차검증용
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
        'build_year': (raw.get('buildYear') or '').strip(),
        'road': raw.get('roadNm', ''),  # 도로명 (교차검증용)
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
            'cross_verify': True,  # 🆕 Day 8: 단지 교차검증
            'map_geocode': bool(KAKAO_REST_KEY or VWORLD_API_KEY),  # 지도 좌표변환 가능 여부
            'map_geocode_kakao': bool(KAKAO_REST_KEY),   # 카카오 지오코더(기본)
            'map_geocode_vworld': bool(VWORLD_API_KEY),  # V-World(폴백)
        },
        'supabase': {
            'lib_installed': HAS_SUPABASE_LIB,
            'connected': supabase is not None,
            'url_set': bool(SUPABASE_URL),
            'key_set': bool(SUPABASE_KEY),
        },
        'version': 'v2.16-cross-verify',
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
        return jsonify({'error': safe_error('국토부 API HTTP 오류', e)}), 502
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

    # 🆕 월별 호출을 병렬 처리 (순차 → 동시 최대 8개). 24개월 × 매매·전월세도 몇 초 내 완료.
    #    워커는 HTTP+파싱만 하고, 결과 누적은 메인 스레드에서 처리 → 경쟁조건 없음.
    _ts = cache_ts()

    def _fetch_month(task):
        ym, kind = task
        try:
            if kind == 'trade':
                xml_text = fetch_trade_cached(lawd_cd, ym, _ts)
                raw, err = parse_xml_items(xml_text)
                if err:
                    return ('err', f'{ym} 매매: {err}')
                return ('ok', [normalize_trade_item(x) for x in raw])
            else:
                xml_text = fetch_rent_cached(lawd_cd, ym, _ts)
                raw, err = parse_xml_items(xml_text)
                if err:
                    return ('err', f'{ym} 전월세: {err}')
                return ('ok', [normalize_rent_item(x) for x in raw])
        except Exception as e:
            return ('err', f'{ym} {kind}: {e}')

    tasks = []
    for ym in year_months:
        tasks.append((ym, 'trade'))
        if include_rent:
            tasks.append((ym, 'rent'))

    with ThreadPoolExecutor(max_workers=8) as _ex:
        for status, payload in _ex.map(_fetch_month, tasks):
            if status == 'ok':
                all_items.extend(payload)
            else:
                errors.append(payload)

    # 필터링: 단지명 정밀 매칭
    # (위치 prefix/차수 변형은 허용, 브랜드 단편(예: '성원') 오매칭은 차단)
    if danji_filter:
        nf = danji_filter.replace(' ', '').lower()
        def matches(item_name):
            n = (item_name or '').replace(' ', '').lower()
            if not n or not nf:
                return False
            # 양방향 '완전 포함' 매칭만 허용:
            #   "정왕동대림성원" ⊇ "대림성원"  → 통과 (위치 prefix 변형)
            #   "대림성원1차"   ⊇ "대림성원"  → 통과 (차수 변형)
            #   "현대성원" vs "대림성원"        → 차단 (서로 포함 안 됨)
            # 짧은 쪽 이름이 3글자 미만이면 degenerate 오매칭 위험으로 제외.
            shorter = n if len(n) <= len(nf) else nf
            if len(shorter) < 3:
                return False
            return nf in n or n in nf
        all_items = [x for x in all_items if matches(x.get('name', ''))]
    if min_area is not None:
        all_items = [x for x in all_items if x['area'] >= min_area]
    if max_area is not None:
        all_items = [x for x in all_items if x['area'] <= max_area]

    # v2.17: 도로명 필터 - 단지명+도로명 교차검증 (지번보다 안정적, 같은 이름 다른 단지 제외)
    # 도로명이 '있는데 불일치'하면 다른 단지로 보고 제외.
    # 도로명이 '비어 있는' 거래는 제외하지 않되, road_verified=False 로 표시 →
    #   프론트가 '미검증'으로 라벨링. (빈 도로명을 검증된 것처럼 포함하던 오류 수정)
    road_filter = request.args.get('road_nm', '').strip()
    if road_filter:
        rf = road_filter.replace(' ', '')
        kept = []
        for x in all_items:
            xr = (x.get('road', '') or '').replace(' ', '')
            if not xr:
                x['road_verified'] = False   # 도로명 없음 → 미검증으로 통과
                kept.append(x)
            elif rf in xr or xr in rf:
                x['road_verified'] = True    # 도로명 일치 → 검증
                kept.append(x)
            # else: 도로명이 있는데 불일치 → 다른 단지로 보고 제외
        all_items = kept

    # v2.14: jibun 필터 - 같은 지번(단지)만 통과 (NPL 평가 정확도)
    # 예: 본건 지번 "3278" vs 거래사례 지번 "1008-2" → 다른 단지로 판단해 제외
    jibun_filter = request.args.get('jibun', '').strip()
    if jibun_filter:
        def jibun_main(s):
            # "3278-0" → "3278", "0292" → "292", "292" → "292"
            s = (s or '').strip().lstrip('0')
            return s.split('-')[0].strip() if s else ''
        target = jibun_main(jibun_filter)
        if target:
            all_items = [x for x in all_items if jibun_main(x.get('jibun', '')) == target]

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
        return jsonify({'error': safe_error('국토부 API HTTP 오류', e)}), 502
    except Exception as e:
        return jsonify({'error': safe_error('서버 오류', e)}), 500


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


# ============================================================
# 페이지네이션 지원 함수 (대형 단지용)
# ============================================================
# API 한 페이지당 최대 100건 반환 (numOfRows를 더 높여도 100건만 반환됨)
_PAGE_SIZE = 100


def fetch_br_expose_all_pages(sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji, match_fn=None, buffer_pages=1):
    """전유공용면적 - 모든 페이지 통합 조회 (대형 단지 대응).

    match_fn 제공 시, 대상 호를 찾으면 buffer_pages만큼만 더 보고 조기 종료(속도↑).
    match_fn이 안 맞으면 기존처럼 전체 페이지 조회(안전 폴백).

    Returns: (items_list, error_message_or_None)
    """
    all_items = []
    page = 1
    max_pages = 200
    found_page = None

    while page <= max_pages:
        params = {
            'serviceKey': API_KEY,
            'sigunguCd': sigungu_cd,
            'bjdongCd': bjdong_cd,
            'platGbCd': plat_gb_cd,
            'bun': bun.zfill(4),
            'ji': ji.zfill(4),
            'numOfRows': str(_PAGE_SIZE),
            'pageNo': str(page),
        }
        try:
            r = requests.get(URL_BR_EXPOSE, params=params, timeout=30)
            r.raise_for_status()
        except Exception as e:
            if page == 1:
                return [], f'전유공용면적 API 호출 실패 (page={page}): {e}'
            break  # 중간 페이지 실패 → 그동안 모은 데이터로 진행

        items, err = parse_xml_items(r.text)
        if err:
            if page == 1:
                return [], err
            break
        if not items:
            break  # 더 이상 데이터 없음

        all_items.extend(items)
        # 조기 종료: 대상 호를 찾았으면 buffer_pages만큼만 더 보고 중단
        if match_fn and found_page is None:
            try:
                if any(match_fn(x) for x in items):
                    found_page = page
            except Exception:
                pass
        if found_page is not None and page >= found_page + buffer_pages:
            break
        if len(items) < _PAGE_SIZE:
            break  # 마지막 페이지 (100건 미만 → 끝)
        page += 1

    return all_items, None


def fetch_br_price_all_pages(sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji, match_fn=None, buffer_pages=0):
    """공시가격 - 모든 페이지 통합 조회 (대형 단지 대응).

    match_fn 제공 시 대상 호 발견하면 조기 종료(속도↑). 안 맞으면 전체 조회(안전 폴백).

    Returns: (items_list, error_message_or_None)
    """
    all_items = []
    page = 1
    max_pages = 200
    found_page = None

    while page <= max_pages:
        params = {
            'serviceKey': API_KEY,
            'sigunguCd': sigungu_cd,
            'bjdongCd': bjdong_cd,
            'platGbCd': plat_gb_cd,
            'bun': bun.zfill(4),
            'ji': ji.zfill(4),
            'numOfRows': str(_PAGE_SIZE),
            'pageNo': str(page),
        }
        try:
            r = requests.get(URL_BR_PRICE, params=params, timeout=30)
            r.raise_for_status()
        except Exception as e:
            if page == 1:
                return [], f'공시가격 API 호출 실패 (page={page}): {e}'
            break

        items, err = parse_xml_items(r.text)
        if err:
            if page == 1:
                return [], err
            break
        if not items:
            break

        all_items.extend(items)
        if match_fn and found_page is None:
            try:
                if any(match_fn(x) for x in items):
                    found_page = page
            except Exception:
                pass
        if found_page is not None and page >= found_page + buffer_pages:
            break
        if len(items) < _PAGE_SIZE:
            break
        page += 1

    return all_items, None


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
# 신규: 건축물대장 통합 자동조회 (단지코드 + 동·호수만으로)
# Phase 1: 2단계 작업 (동·호수 입력 시 건축물대장 자동조회)
# 🆕 v2.16 (Day 8): 단지 교차검증 통합 — K-apt 옛 지번 의존 해소
# ============================================================

@app.route('/api/complex/identify')
def api_identify_complex():
    """단지 교차검증 식별 (단독 호출용, v2.16 신규).
    
    Query params:
        kapt_code: 단지코드 (필수, A로 시작)
        force_refresh: '1'이면 캐시 무시 (선택, 디버깅 시 유용)
    
    Returns: complex_identifier.identify_complex() 결과
        - mgm_bldrgst_pk, 신지번(bun/ji), 신뢰도 점수, 매칭 상세 등
    """
    if not API_KEY:
        return jsonify({'error': 'API 키 미설정'}), 500
    
    kapt_code = request.args.get('kapt_code', '').strip()
    force_refresh = request.args.get('force_refresh', '0') == '1'
    
    if not kapt_code or not kapt_code.startswith('A'):
        return jsonify({'error': '유효한 kapt_code 필요 (A로 시작)'}), 400
    
    try:
        # K-apt 기본정보로 단지 메타데이터 확보
        basis_xml = fetch_apt_basis_cached(kapt_code, cache_ts())
        basis_items, err = parse_kapt_response(basis_xml)
        if err:
            return jsonify({'error': safe_error('단지정보 조회 실패', err)}), 502
        if not basis_items:
            return jsonify({'error': '단지 정보를 찾을 수 없습니다'}), 404
        b = basis_items[0]
        
        bjd_code = safe_get(b, 'bjdCode')
        if len(bjd_code) != 10:
            return jsonify({'error': f'법정동코드 형식 오류: {bjd_code}'}), 502
        sigungu_cd = bjd_code[:5]
        bjdong_cd = bjd_code[5:]
        
        household_str = safe_get(b, 'kaptdaCnt')
        try:
            household = int(household_str) if household_str else None
        except (ValueError, TypeError):
            household = None
        
        result = identify_complex(
            kapt_code=kapt_code,
            sigungu_cd=sigungu_cd,
            bjdong_cd=bjdong_cd,
            complex_name=safe_get(b, 'kaptName'),
            road_addr=safe_get(b, 'doroJuso'),
            household_count=household,
            use_approval_date=safe_get(b, 'kaptUsedate'),
            force_refresh=force_refresh,
        )
        return jsonify(result)
    
    except Exception as e:
        return jsonify({'error': safe_error('교차검증 오류', e)}), 500


@app.route('/api/building/auto-lookup')
def auto_lookup_building():
    """🆕 v2.16-cross-verify: 교차검증으로 정확한 신지번 확보 후 3종 API 조회.
    
    Day 8 변경: K-apt가 제공하는 옛 지번 의존을 끊고, 건축HUB 총괄표제부와
    단지명/도로명주소/세대수 교차검증으로 정확한 신지번을 자동 보정.
    
    Query params (기존과 동일 — 프론트엔드 호환):
        kapt_code: 단지코드 (필수, A로 시작)
        dong_nm: 동 이름 (필수, 예: "101" 또는 "101동")
        ho_nm: 호수 (필수, 예: "201" 또는 "201호")
    
    Returns (기존 구조 유지 + complex 필드 추가):
        unit, title, price, errors, lookup_params (기존)
        complex: { match_score, score_breakdown, source, mgm_bldrgst_pk, ... } (🆕)
    """
    if not API_KEY:
        return jsonify({'error': 'API 키 미설정'}), 500
    
    kapt_code = request.args.get('kapt_code', '').strip()
    dong_nm = request.args.get('dong_nm', '').strip()
    ho_nm = request.args.get('ho_nm', '').strip()
    
    if not kapt_code or not kapt_code.startswith('A'):
        return jsonify({'error': '유효한 kapt_code 필요 (A로 시작)'}), 400
    if not dong_nm or not ho_nm:
        return jsonify({'error': 'dong_nm, ho_nm 모두 필수'}), 400
    
    try:
        # ============================================================
        # 1. 단지 기본정보 조회 (V4 API)
        # ============================================================
        basis_xml = fetch_apt_basis_cached(kapt_code, cache_ts())
        basis_items, err = parse_kapt_response(basis_xml)
        if err:
            return jsonify({'error': safe_error('단지정보 조회 실패', err)}), 502
        if not basis_items:
            return jsonify({'error': '단지 정보를 찾을 수 없습니다'}), 404
        b = basis_items[0]
        
        # ============================================================
        # 2. bjdCode → 시군구코드(5자리), 법정동코드(5자리) 추출
        # ============================================================
        bjd_code = safe_get(b, 'bjdCode')
        if len(bjd_code) != 10:
            return jsonify({'error': f'법정동코드 형식 오류: {bjd_code}'}), 502
        sigungu_cd = bjd_code[:5]
        bjdong_cd = bjd_code[5:]
        addr_lot = safe_get(b, 'kaptAddr')
        
        # ============================================================
        # 3. 🆕 교차검증 — 정확한 신지번 확보
        # ============================================================
        household_str = safe_get(b, 'kaptdaCnt')
        try:
            household = int(household_str) if household_str else None
        except (ValueError, TypeError):
            household = None
        
        complex_result = identify_complex(
            kapt_code=kapt_code,
            sigungu_cd=sigungu_cd,
            bjdong_cd=bjdong_cd,
            complex_name=safe_get(b, 'kaptName'),
            road_addr=safe_get(b, 'doroJuso'),
            household_count=household,
            use_approval_date=safe_get(b, 'kaptUsedate'),
        )
        
        complex_summary = None
        used_fallback = False
        
        if complex_result.get('success'):
            # ✅ 교차검증 성공: 정확한 신지번 사용
            plat_gb_cd = complex_result['plat_gb_cd']
            bun = complex_result['bun']
            ji = complex_result['ji']
            complex_summary = {
                'mgm_bldrgst_pk': complex_result.get('mgm_bldrgst_pk'),
                'name': complex_result.get('complex_name'),
                'road_addr': complex_result.get('road_addr'),
                'jibun_addr': complex_result.get('jibun_addr'),
                'match_score': complex_result.get('match_score'),
                'score_breakdown': complex_result.get('score_breakdown'),
                'source': complex_result.get('source'),
                'candidates_count': complex_result.get('candidates_count', 0),
                'rival_candidates': complex_result.get('rival_candidates', 0),
            }
        else:
            # ⚠ 교차검증 실패 → fallback: 기존 방식(주소 파싱)으로 시도
            used_fallback = True
            m = re.search(r'(\d+)(?:-(\d+))?(?:\s|$)', addr_lot)
            if not m:
                return jsonify({
                    'error': f'교차검증 실패 + 지번 파싱 실패: {complex_result.get("message")}',
                    'complex_error': complex_result,
                }), 502
            bun = m.group(1)
            ji = m.group(2) or '0'
            plat_gb_cd = '0'
            complex_summary = {
                'name': safe_get(b, 'kaptName'),
                'match_score': 0,
                'source': 'fallback',
                'fallback_reason': complex_result.get('error'),
                'fallback_message': complex_result.get('message'),
                'candidates_count': complex_result.get('candidates_count', 0),
            }
        
        # ============================================================
        # 4. 동·호수 정규화 (기존 v2.12 로직 그대로)
        # ============================================================
        # 매칭 유틸: "101동"="101", "201호"="201" 등 모두 정규화
        # v2.12: zero-padding ('0802' → '802'), 알파벳 prefix ('B0802' → '802') 처리
        def norm_dong(s):
            s = str(s).replace(' ', '').replace('동', '')
            s = re.sub(r'^[A-Za-z]+', '', s)  # 알파벳 prefix 제거
            if s.isdigit():
                s = str(int(s))  # 선행 0 제거
            return s
        def norm_ho(s):
            s = str(s).replace(' ', '').replace('호', '')
            s = re.sub(r'^[A-Za-z]+', '', s)  # 'B', 'PH' 등 알파벳 prefix 제거
            if s.isdigit():
                s = str(int(s))  # '0802' → '802' (선행 0 제거)
            return s
        
        dong_target = norm_dong(dong_nm)
        ho_target = norm_ho(ho_nm)

        # v2.18: 등기부 지번 폴백 - K-apt 대표지번에 표제부가 없으면 등기부(PDF) 지번으로 전환
        # (대형 재건축 단지에서 K-apt 지번 ≠ 건축물대장 지번인 경우 대응)
        jibun_hint = request.args.get('jibun_hint', '').strip()
        if jibun_hint:
            hint_bun = jibun_hint.split('-')[0].strip()
            if hint_bun and hint_bun != bun:
                try:
                    probe_items, _ = parse_xml_items(
                        fetch_br_title_cached(sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji, cache_ts()))
                    if not probe_items:
                        hint_items, _ = parse_xml_items(
                            fetch_br_title_cached(sigungu_cd, bjdong_cd, plat_gb_cd, hint_bun, '0', cache_ts()))
                        if hint_items:
                            bun, ji = hint_bun, '0'  # 등기부 지번에 표제부 존재 → 전환
                except Exception:
                    pass

        result = {
            'unit': None,
            'title': None,
            'price': None,
            'errors': [],
            'complex': complex_summary,  # 🆕 v2.16: 교차검증 결과
            'lookup_params': {
                'kapt_code': kapt_code,
                'sigungu_cd': sigungu_cd,
                'bjdong_cd': bjdong_cd,
                'bun': bun,
                'ji': ji,
                'plat_gb_cd': plat_gb_cd,
                'dong_nm': dong_nm,
                'ho_nm': ho_nm,
                'dong_target_norm': dong_target,  # v2.12: 정규화 결과
                'ho_target_norm': ho_target,      # v2.12: 정규화 결과
                'kapt_addr': addr_lot,
                'verified_jibun': not used_fallback,  # 🆕 v2.16: 교차검증 신지번 여부
                'version': 'v2.16-cross-verify',
            }
        }
        
        # ============================================================
        # 5. 표제부 조회 (기존 코드 그대로)
        # ============================================================
        try:
            xml_text = fetch_br_title_cached(sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji, cache_ts())
            items, err = parse_xml_items(xml_text)
            if err:
                result['errors'].append(f'표제부 API 오류: {err}')
            elif not items:
                result['errors'].append(f'표제부 데이터 없음 (sigunguCd={sigungu_cd}, bjdongCd={bjdong_cd}, bun={bun.zfill(4)}, ji={ji.zfill(4)}) - 공공데이터 포털에 등록되지 않은 주소일 수 있습니다')
            else:
                # dong 매칭, 없으면 첫 번째
                matched = None
                for x in items:
                    d = norm_dong(safe_get(x, 'dongNm'))
                    if d == dong_target:
                        matched = x
                        break
                if not matched:
                    matched = items[0]
                    available_dongs = [safe_get(x, 'dongNm') for x in items]
                    result['errors'].append(f'표제부에 "{dong_nm}동" 매칭 실패, 첫 번째 동({safe_get(matched, "dongNm")}) 사용 / 단지 내 동 목록: {available_dongs}')
                result['title'] = {
                    'dongNm': safe_get(matched, 'dongNm'),
                    'totalFloors': safe_get(matched, 'grndFlrCnt'),
                    'undergroundFloors': safe_get(matched, 'ugrndFlrCnt'),
                    'totalArea': safe_get(matched, 'totArea'),
                    'platArea': safe_get(matched, 'platArea'),
                    'archArea': safe_get(matched, 'archArea'),
                    'completionDate': safe_get(matched, 'useAprDay'),
                    'mainPurps': safe_get(matched, 'mainPurpsCdNm'),
                    'struct': safe_get(matched, 'strctCdNm'),
                    'hhldCnt': safe_get(matched, 'hhldCnt'),
                }
        except Exception as e:
            result['errors'].append(safe_error('표제부 조회 오류', e))
        
        # ============================================================
        # 6. 전유공용면적 (호별 면적) 조회 - 페이지네이션 적용 (대형 단지 대응)
        # ============================================================
        try:
            def _expose_match(x):
                return (norm_dong(safe_get(x, 'dongNm')) == dong_target
                        and norm_ho(safe_get(x, 'hoNm')) == ho_target
                        and '전유' in safe_get(x, 'exposPubuseGbCdNm'))
            items, err = fetch_br_expose_all_pages(sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji,
                                                   match_fn=_expose_match, buffer_pages=1)
            if err:
                result['errors'].append(f'전유공용면적 API 오류: {err}')
            elif not items:
                result['errors'].append(f'전유공용면적 데이터 없음 (sigunguCd={sigungu_cd}, bjdongCd={bjdong_cd}, bun={bun.zfill(4)}, ji={ji.zfill(4)})')
            else:
                exclu_area = None
                pub_area = 0.0
                floor = None
                struct = None
                pub_count = 0
                match_count = 0
                matched_samples = []  # v2.12: 매칭된 행의 실제 값 (디버그용)
                
                for x in items:
                    d = norm_dong(safe_get(x, 'dongNm'))
                    h = norm_ho(safe_get(x, 'hoNm'))
                    if d != dong_target or h != ho_target:
                        continue
                    match_count += 1
                    gb = safe_get(x, 'exposPubuseGbCdNm')  # 전유/공용
                    main = safe_get(x, 'mainAtchGbCdNm')   # 주/부속
                    area = safe_get(x, 'area')
                    
                    # v2.12: 디버그 샘플 수집 (최대 5건) - 매칭 실패 원인 진단용
                    if len(matched_samples) < 5:
                        matched_samples.append({
                            'dong': safe_get(x, 'dongNm'),
                            'ho': safe_get(x, 'hoNm'),
                            'gb': gb,
                            'main': main,
                            'area': area,
                            'purps': safe_get(x, 'mainPurpsCdNm'),
                        })
                    
                    # v2.12: 매칭 완화 — '전유' 부분 매칭 ('전유'/'전유부분' 모두 처리)
                    if '전유' in gb and exclu_area is None:
                        try:
                            exclu_area = float(area)
                            if not floor:
                                # v2.13: "8층" → 8 (숫자만 추출) - 프론트 number input 호환
                                floor_raw = safe_get(x, 'flrNoNm')
                                m = re.search(r'(-?\d+)', floor_raw)
                                floor = int(m.group(1)) if m else None
                            if not struct:
                                struct = safe_get(x, 'strctCdNm')
                        except (ValueError, TypeError):
                            pass
                    elif '공용' in gb:
                        try:
                            pub_area += float(area)
                            pub_count += 1
                        except (ValueError, TypeError):
                            pass
                
                if exclu_area is not None:
                    result['unit'] = {
                        'dongNm': dong_nm,
                        'hoNm': ho_nm,
                        'excluArea': round(exclu_area, 2),
                        'pubArea': round(pub_area, 2) if pub_count > 0 else None,
                        'supplyArea': round(exclu_area + pub_area, 2) if pub_count > 0 else None,
                        'floor': floor,
                        'struct': struct,
                    }
                elif match_count == 0:
                    # 매칭된 호수가 없음 - 단지 내 동·호수 샘플 보여주기
                    available_dongs = sorted(set([safe_get(x, 'dongNm') for x in items]))[:10]
                    available_hos_in_target_dong = sorted(set([safe_get(x, 'hoNm') for x in items if norm_dong(safe_get(x, 'dongNm')) == dong_target]))[:10]
                    if available_hos_in_target_dong:
                        result['errors'].append(f'전유공용면적: "{dong_nm}동 {ho_nm}호" 매칭 실패 / {dong_nm}동의 호수 일부: {available_hos_in_target_dong}')
                    else:
                        result['errors'].append(f'전유공용면적: "{dong_nm}동" 매칭 실패 / 단지 내 동 목록: {available_dongs}')
                else:
                    # v2.12: 매칭은 됐는데 전유면적 추출 실패 - 실제 데이터 샘플 출력
                    result['errors'].append(f'전유공용면적: "{dong_nm}동 {ho_nm}호" 매칭 {match_count}건 있으나 전유 추출 실패. 샘플(최대5): {matched_samples}')
        except Exception as e:
            result['errors'].append(safe_error('전유공용면적 조회 오류', e))
        
        # ============================================================
        # 7. 공시가격 (가장 최근 기준일 기준) - 페이지네이션 적용
        # ============================================================
        try:
            def _price_match(x):
                dv = next((safe_get(x, k) for k in ('dongNm', 'dongName', 'apartmentDongName', 'dongNo', 'houseDongName', 'apt_dong_nm') if safe_get(x, k)), '')
                hv = next((safe_get(x, k) for k in ('hoNm', 'hoName', 'apartmentHoName', 'hoNo', 'houseHoName', 'apt_ho_nm') if safe_get(x, k)), '')
                return norm_dong(dv) == dong_target and norm_ho(hv) == ho_target
            items, err = fetch_br_price_all_pages(sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji,
                                                  match_fn=_price_match, buffer_pages=0)
            if err:
                result['errors'].append(f'공시가격 API 오류: {err}')
            elif not items:
                result['errors'].append(f'공시가격 데이터 없음 (sigunguCd={sigungu_cd}, bjdongCd={bjdong_cd}, bun={bun.zfill(4)}, ji={ji.zfill(4)})')
            else:
                # v2.13: 공시가격 API는 dongNm/hoNm 외 다른 필드명 가능성 (apartmentDongName, dongName, dongNo 등)
                # 다양한 필드명 fallback 시도
                def get_dong_field(x):
                    for k in ('dongNm', 'dongName', 'apartmentDongName', 'dongNo', 'houseDongName', 'apt_dong_nm'):
                        v = safe_get(x, k)
                        if v:
                            return v
                    return ''
                def get_ho_field(x):
                    for k in ('hoNm', 'hoName', 'apartmentHoName', 'hoNo', 'houseHoName', 'apt_ho_nm'):
                        v = safe_get(x, k)
                        if v:
                            return v
                    return ''
                
                matched_prices = []
                for x in items:
                    d = norm_dong(get_dong_field(x))
                    h = norm_ho(get_ho_field(x))
                    if d != dong_target or h != ho_target:
                        continue
                    matched_prices.append(x)
                matched_prices.sort(key=lambda x: safe_get(x, 'bldRgstStdDay'), reverse=True)
                
                if matched_prices:
                    p = matched_prices[0]
                    result['price'] = {
                        'value': safe_get(p, 'bldRgstPc'),
                        'stdDay': safe_get(p, 'bldRgstStdDay'),
                    }
                else:
                    # v2.13: 매칭 실패 시 첫 행의 모든 키 목록 출력 (실제 필드명 진단용)
                    same_dong_hos = sorted(set([get_ho_field(x) for x in items if norm_dong(get_dong_field(x)) == dong_target]))[:15]
                    sample_rows = [{'dong': get_dong_field(x), 'ho': get_ho_field(x)} for x in items[:5]]
                    available_dongs = sorted(set([get_dong_field(x) for x in items]))[:10]
                    all_keys = sorted(list(items[0].keys())) if items else []
                    if same_dong_hos:
                        result['errors'].append(f'공시가격: "{dong_nm}동 {ho_nm}호" 매칭 실패 (전체 {len(items)}건) / {dong_nm}동의 호수 일부: {same_dong_hos}')
                    else:
                        result['errors'].append(f'공시가격: "{dong_nm}동" 매칭 실패 (전체 {len(items)}건) / 동 목록: {available_dongs} / 샘플: {sample_rows} / 첫행 전체키: {all_keys}')
        except Exception as e:
            result['errors'].append(safe_error('공시가격 조회 오류', e))
        
        return jsonify(result)
    
    except requests.exceptions.HTTPError as e:
        return jsonify({'error': safe_error('국토부 API HTTP 오류', e)}), 502
    except Exception as e:
        return jsonify({'error': safe_error('서버 오류', e)}), 500




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
        'build_year': (raw.get('buildYear') or '').strip(),
        'house_type': (raw.get('houseType') or '').strip(),
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
        'build_year': (raw.get('buildYear') or '').strip(),
        'house_type': (raw.get('houseType') or '').strip(),
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
# 오피스텔 실거래가 (구조: 연립다세대와 동일 — 전용면적·층·단지명 offiNm)
# ============================================================

@lru_cache(maxsize=256)
def fetch_offi_trade_cached(lawd_cd, year_month, _ts):
    """오피스텔 매매."""
    params = {
        'serviceKey': API_KEY,
        'LAWD_CD': lawd_cd,
        'DEAL_YMD': year_month,
        'numOfRows': '1000',
        'pageNo': '1',
    }
    r = requests.get(URL_OFFI_TRADE, params=params, timeout=30)
    r.raise_for_status()
    return r.text


@lru_cache(maxsize=256)
def fetch_offi_rent_cached(lawd_cd, year_month, _ts):
    """오피스텔 전월세."""
    params = {
        'serviceKey': API_KEY,
        'LAWD_CD': lawd_cd,
        'DEAL_YMD': year_month,
        'numOfRows': '1000',
        'pageNo': '1',
    }
    r = requests.get(URL_OFFI_RENT, params=params, timeout=30)
    r.raise_for_status()
    return r.text


def normalize_offi_trade_item(raw):
    """오피스텔 매매 항목 정규화."""
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
        'name': raw.get('offiNm', ''),  # 오피스텔 단지명
        'building': '',
        'area': round(area, 2),
        'floor': floor,
        'price': price,
        'type': '매매',
        'memo': raw.get('cdealType', ''),
        'jibun': raw.get('jibun', ''),
        'dong': raw.get('umdNm', ''),
        'build_year': (raw.get('buildYear') or '').strip(),
        'category': '오피스텔',
    }


def normalize_offi_rent_item(raw):
    """오피스텔 전월세 항목 정규화."""
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
        'name': raw.get('offiNm', ''),
        'area': round(area, 2),
        'floor': floor,
        'price': deposit,
        'monthly': monthly,
        'type': '월세' if monthly > 0 else '전세',
        'jibun': raw.get('jibun', ''),
        'dong': raw.get('umdNm', ''),
        'build_year': (raw.get('buildYear') or '').strip(),
        'category': '오피스텔',
    }


@app.route('/api/transactions/offi-bulk')
def get_offi_transactions_bulk():
    """오피스텔 다월 일괄 조회. (params: 연립다세대 bulk와 동일)"""
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
            xml_text = fetch_offi_trade_cached(lawd_cd, ym, cache_ts())
            raw_items, err = parse_xml_items(xml_text)
            if not err:
                all_items.extend(normalize_offi_trade_item(x) for x in raw_items)
            else:
                errors.append(f'{ym} 매매: {err}')
            if include_rent:
                xml_text = fetch_offi_rent_cached(lawd_cd, ym, cache_ts())
                raw_items, err = parse_xml_items(xml_text)
                if not err:
                    all_items.extend(normalize_offi_rent_item(x) for x in raw_items)
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
# 단독/다가구 실거래가 (전용면적·층·단지명·지번 없음 → 연면적·대지면적·주택유형)
# ============================================================

@lru_cache(maxsize=256)
def fetch_sh_trade_cached(lawd_cd, year_month, _ts):
    """단독/다가구 매매."""
    params = {
        'serviceKey': API_KEY,
        'LAWD_CD': lawd_cd,
        'DEAL_YMD': year_month,
        'numOfRows': '1000',
        'pageNo': '1',
    }
    r = requests.get(URL_SH_TRADE, params=params, timeout=30)
    r.raise_for_status()
    return r.text


@lru_cache(maxsize=256)
def fetch_sh_rent_cached(lawd_cd, year_month, _ts):
    """단독/다가구 전월세."""
    params = {
        'serviceKey': API_KEY,
        'LAWD_CD': lawd_cd,
        'DEAL_YMD': year_month,
        'numOfRows': '1000',
        'pageNo': '1',
    }
    r = requests.get(URL_SH_RENT, params=params, timeout=30)
    r.raise_for_status()
    return r.text


def normalize_sh_trade_item(raw):
    """단독/다가구 매매 항목 정규화. (면적 = 연면적, 층 없음, 단지명 없음)"""
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
        area = float(raw.get('totalFloorAr', '0') or 0)  # 연면적
    except ValueError:
        area = 0
    try:
        land_area = float(raw.get('plottageAr', '0') or 0)  # 대지면적
    except ValueError:
        land_area = 0
    house_type = (raw.get('houseType', '') or '').strip()  # 단독/다가구
    return {
        'date': date,
        'name': house_type or '단독/다가구',
        'building': '',
        'area': round(area, 2),          # 연면적
        'land_area': round(land_area, 2),  # 대지면적
        'floor': None,
        'price': price,
        'type': '매매',
        'memo': (f'대지 {round(land_area, 1)}㎡' if land_area else '') + (f' · {house_type}' if house_type else ''),
        'jibun': '',
        'dong': raw.get('umdNm', ''),
        'build_year': (raw.get('buildYear') or '').strip(),
        'category': '단독다가구',
    }


def normalize_sh_rent_item(raw):
    """단독/다가구 전월세 항목 정규화. (면적 = 계약면적)"""
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
        area = float(raw.get('totalFloorAr', '0') or 0)  # 계약면적
    except ValueError:
        area = 0
    house_type = (raw.get('houseType', '') or '').strip()
    return {
        'date': date,
        'name': house_type or '단독/다가구',
        'area': round(area, 2),
        'floor': None,
        'price': deposit,
        'monthly': monthly,
        'type': '월세' if monthly > 0 else '전세',
        'jibun': '',
        'dong': raw.get('umdNm', ''),
        'build_year': (raw.get('buildYear') or '').strip(),
        'category': '단독다가구',
    }


@app.route('/api/transactions/sh-bulk')
def get_sh_transactions_bulk():
    """단독/다가구 다월 일괄 조회.
    단지명이 없으므로 dong(법정동명)·면적(연면적)으로 필터링.
    params: lawd_cd(필수), months, dong, min_area, max_area, include_rent
    """
    if not API_KEY:
        return jsonify({'error': 'API 키 미설정'}), 500
    lawd_cd = request.args.get('lawd_cd', '').strip()
    months = request.args.get('months', default=6, type=int)
    dong_filter = request.args.get('dong', '').strip()
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
            xml_text = fetch_sh_trade_cached(lawd_cd, ym, cache_ts())
            raw_items, err = parse_xml_items(xml_text)
            if not err:
                all_items.extend(normalize_sh_trade_item(x) for x in raw_items)
            else:
                errors.append(f'{ym} 매매: {err}')
            if include_rent:
                xml_text = fetch_sh_rent_cached(lawd_cd, ym, cache_ts())
                raw_items, err = parse_xml_items(xml_text)
                if not err:
                    all_items.extend(normalize_sh_rent_item(x) for x in raw_items)
        except Exception as e:
            errors.append(f'{ym}: {e}')

    if dong_filter:
        df = dong_filter.replace(' ', '')
        all_items = [x for x in all_items if df in (x.get('dong') or '').replace(' ', '')]
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
# V-World 지오코딩 (주소→좌표) — 반경 거래사례용
# ============================================================
@lru_cache(maxsize=50000)
def geocode_addr_cached(address, addr_type):
    """지번/도로명 주소 → (lat, lng). 실패 시 None. (lru 캐시로 재호출 최소화)"""
    if not VWORLD_API_KEY or not address:
        return None
    params = {
        'service': 'address', 'request': 'getcoord', 'version': '2.0',
        'crs': 'epsg:4326', 'address': address, 'type': addr_type,
        'format': 'json', 'key': VWORLD_API_KEY,
    }
    try:
        r = requests.get(URL_VWORLD_GEOCODE, params=params, timeout=8)
        d = r.json()
        resp = d.get('response', {})
        if resp.get('status') == 'OK':
            pt = resp.get('result', {}).get('point', {})
            x = pt.get('x'); y = pt.get('y')  # x=경도(lng), y=위도(lat)
            if x and y:
                return (float(y), float(x))
    except Exception:
        return None
    return None


@lru_cache(maxsize=50000)
def kakao_geocode_cached(address):
    """카카오 로컬 주소검색 → (lat, lng). 실패 시 None.
    지번·도로명·동 단위 모두 한 번의 쿼리로 처리(동 단위는 지역 좌표 반환)."""
    if not KAKAO_REST_KEY or not address:
        return None
    try:
        r = requests.get(URL_KAKAO_GEOCODE, params={'query': address, 'size': 1},
                         headers={'Authorization': 'KakaoAK ' + KAKAO_REST_KEY}, timeout=8)
        docs = (r.json() or {}).get('documents') or []
        if docs:
            x = docs[0].get('x'); y = docs[0].get('y')  # x=경도(lng), y=위도(lat)
            if x and y:
                return (float(y), float(x))
    except Exception:
        return None
    return None


def _geocode_any(address):
    """카카오 우선(해외 서버에서도 작동), 실패 시 V-World(지번→도로명) 폴백."""
    if not address:
        return None
    c = kakao_geocode_cached(address)
    if c:
        return c
    c = geocode_addr_cached(address, 'parcel')
    if c is None:
        c = geocode_addr_cached(address, 'road')
    return c


@app.route('/api/geocode/diag')
def geocode_diag():
    """지오코딩(카카오/ V-World)이 왜 실패하는지 원문 응답을 그대로 보여주는 진단.
    공개 주소 1건을 좌표변환. 비밀값 미노출. 키 없이 열람 가능.
    URL: /api/geocode/diag?addr=서울특별시 송파구 잠실동 40"""
    addr = (request.args.get('addr') or '서울특별시 송파구 잠실동 40').strip()
    referer = (request.args.get('referer') or 'https://real-estate-app-xzia.onrender.com').strip()
    out = {'kakao_key_set': bool(KAKAO_REST_KEY), 'vworld_key_set': bool(VWORLD_API_KEY),
           'address': addr, 'referer_tested': referer}

    # 카카오 진단
    if KAKAO_REST_KEY:
        try:
            r = requests.get(URL_KAKAO_GEOCODE, params={'query': addr, 'size': 1},
                             headers={'Authorization': 'KakaoAK ' + KAKAO_REST_KEY}, timeout=10)
            docs = None
            try:
                docs = (r.json() or {}).get('documents')
            except Exception:
                pass
            pt = None
            if docs:
                pt = {'lat': docs[0].get('y'), 'lng': docs[0].get('x')}
            out['kakao'] = {'http': r.status_code, 'found': bool(docs), 'point': pt,
                            'body': r.text[:300]}
        except Exception as e:
            out['kakao'] = {'exception': str(e)}

    if not VWORLD_API_KEY:
        return jsonify(out)

    def _try(base, headers):
        params = {'service': 'address', 'request': 'getcoord', 'version': '2.0',
                  'crs': 'epsg:4326', 'address': addr, 'type': 'parcel',
                  'format': 'json', 'key': VWORLD_API_KEY}
        try:
            r = requests.get(base, params=params, headers=headers, timeout=10)
            body = r.text[:400]
            status = pt = err = None
            try:
                resp = (r.json() or {}).get('response', {})
                status = resp.get('status'); err = resp.get('error')
                pt = (resp.get('result') or {}).get('point')
            except Exception:
                pass
            return {'http': r.status_code, 'status': status, 'error': err,
                    'point': pt, 'body': body}
        except Exception as e:
            return {'exception': str(e)}

    # 3가지 조합으로 원인 격리: https(referer無/有) · http(referer無)
    out['https_no_referer'] = _try(URL_VWORLD_GEOCODE, None)
    out['https_with_referer'] = _try(URL_VWORLD_GEOCODE, {'Referer': referer})
    out['http_no_referer'] = _try(URL_VWORLD_GEOCODE.replace('https://', 'http://'), None)
    return jsonify(out)


@app.route('/api/geocode/coords', methods=['POST'])
def geocode_coords():
    """본건 주소 + 거래 지번주소 리스트 → 좌표. 반경 밴드는 프론트에서 계산.
    body: { origin: "서울특별시 중랑구 중화동 274-77", items: [{id, addr}] }
    resp: { key: bool, origin: [lat,lng]|null, coords: { id: [lat,lng] } }
    """
    if not (KAKAO_REST_KEY or VWORLD_API_KEY):
        return jsonify({'key': False, 'origin': None, 'coords': {}})
    data = request.get_json(force=True, silent=True) or {}
    origin_addr = (data.get('origin') or '').strip()
    items = data.get('items') or []
    origin = _geocode_any(origin_addr) if origin_addr else None

    def _g(it):
        return (str(it.get('id')), _geocode_any((it.get('addr') or '').strip()))

    coords = {}
    if items:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for _id, c in ex.map(_g, items[:300]):
                if c:
                    coords[_id] = c
    return jsonify({'key': True, 'origin': origin, 'coords': coords})


# ============================================================
# 실측 낙찰가율 — 지역(시군구→시도→전국) × 기간(1→3→6→12→24→전체) 캐스케이드
# 표본 min_n 이상인 '가장 좁은 지역 × 가장 짧은 기간' 채택 + 산출근거 반환
# ============================================================
_PERIOD_ORDER = [1, 3, 6, 12, 24, 0]   # 0 = 전체 기간
_PERIOD_LABEL = {1: '최근 1개월', 3: '최근 3개월', 6: '최근 6개월',
                 12: '최근 12개월', 24: '최근 24개월', 0: '전체 기간'}

# 유찰횟수 → 낙찰가율 기울기 추정용 파라미터
_FAIL_MIN_PER_LEVEL = 5          # 유찰단계(0회/1회/…)별 최소 표본 수
_FAIL_MIN_LEVELS = 2             # 기울기를 신뢰하기 위한 최소 유효 유찰단계 수
_FAIL_SLOPE_CLAMP = (-30.0, 5.0)  # 유찰 1회당 %p 변화의 상식적 허용 범위
_FAIL_SLOPE_MIN_MONTHS = 24      # 기울기 추정 최소 기간: 실측 중앙값 기간(짧을 수 있음)과
#   분리해 항상 넓은 창에서 추정 → 소표본으로 기울기가 과격해지는 것을 완화.

# 동(洞) 단위 실측 낙찰가율 채택 파라미터
_DONG_MIN_N = 5                  # 동일 동 낙찰사례가 이 건수 이상일 때만 동 기준 채택
_DONG_MONTHS = 24               # 동 단위 낙찰가율 집계 기간(개월)


def _row_dong(address, sido, sigungu):
    """auction_sales 주소에서 시도·시군구를 떼어 행정동명(첫 토큰)을 얻는다.
    (번지 없는 동 단위 주소 전제 — comparables의 _dong_of와 동일 규칙)"""
    a = address or ''
    for t in (sido or '', sigungu or ''):
        a = a.replace(t, '')
    a = a.strip()
    return a.split()[0] if a else ''


def _dong_stat(rows, sido, sigungu, dong):
    """같은 동(dong) 낙찰기록의 중앙 낙찰가율·사분위·평균유찰을 계산.
    dong 매칭 건이 _DONG_MIN_N 미만이면 None(→ 동 기준 미채택, 상위 스코프로 폴백)."""
    if not dong:
        return None
    matched = []
    for r in rows:
        br = r.get('bid_rate')
        if br is None:
            continue
        if _row_dong(r.get('address'), sido, sigungu) != dong:
            continue
        try:
            matched.append((float(br), r.get('fail_count')))
        except (TypeError, ValueError):
            continue
    if len(matched) < _DONG_MIN_N:
        return None
    rates = sorted(v for v, _ in matched)
    n = len(rates)

    def _q(p):
        i = min(n - 1, max(0, int(round(p * (n - 1)))))
        return round(rates[i], 2)

    fails = [int(fc) for _, fc in matched if fc is not None]
    ref = round(sum(fails) / len(fails), 2) if fails else None
    return {'rate': round(_median(rates), 2), 'p25': _q(0.25), 'p75': _q(0.75),
            'n': n, 'ref_fail': ref}


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def _estimate_fail_slope(rows):
    """원본 낙찰기록(fail_count·bid_rate)에서 '유찰 1회당 낙찰가율 변화(기울기)'를 추정.

    방식: 유찰단계별 낙찰가율 '중앙값'을 구한 뒤, 이웃 단계 간 변화율(%p/유찰1회)을
    모아 다시 '중앙값'으로 요약 → 이상치·소표본에 강함(구간 중앙값 차이).
    기준점(ref_fail)은 표본 전체의 평균 유찰횟수 = 실측 중앙값에 이미 섞인 평균 유찰 수준.

    반환: (slope_per_fail, ref_fail, levels) 또는 (None, None, None).
    """
    pts = []
    for r in rows:
        fc = r.get('fail_count')
        br = r.get('bid_rate')
        if fc is None or br is None:
            continue
        try:
            fc = int(fc)
            br = float(br)
        except (TypeError, ValueError):
            continue
        if fc < 0 or br <= 0:
            continue
        pts.append((fc, br))
    if len(pts) < _FAIL_MIN_PER_LEVEL:
        return None, None, None

    ref_fail = sum(fc for fc, _ in pts) / len(pts)

    by_level = {}
    for fc, br in pts:
        by_level.setdefault(fc, []).append(br)
    levels = []
    for fc in sorted(by_level):
        vals = by_level[fc]
        if len(vals) >= _FAIL_MIN_PER_LEVEL:
            levels.append({'fail': fc, 'n': len(vals),
                           'median_rate': round(_median(vals), 2)})
    if len(levels) < _FAIL_MIN_LEVELS:
        return None, None, None

    deltas = []
    for a, b in zip(levels, levels[1:]):
        df = b['fail'] - a['fail']
        if df > 0:
            deltas.append((b['median_rate'] - a['median_rate']) / df)
    if not deltas:
        return None, None, None

    slope = _median(deltas)
    slope = max(_FAIL_SLOPE_CLAMP[0], min(_FAIL_SLOPE_CLAMP[1], slope))
    return round(slope, 3), round(ref_fail, 2), levels


# ── 유사도 가중 실측 낙찰가율 ────────────────────────────────────────────────
# auction_sales에는 연식·대지지분·좌표가 없어, 신뢰 가능한 유사도 축은
#   ① 면적 근접도(가우시안)  ② 지역 근접도(같은 동>같은 구>같은 시)  뿐이다.
# 이 둘로 각 사례에 가중치를 줘 '가중 중앙 낙찰가율'과 '유효표본수(N_eff)'를 낸다.
# 연립·다세대·나홀로아파트처럼 표본이 얇고 개별성이 큰 물건에서 특히 효과적.
_SIM_AREA_BW = 0.15               # 면적 근접 대역폭(본건 면적 대비 비율)
_SIM_TIER_W = {'dong': 1.0, 'sigungu': 0.55, 'sido': 0.30}  # 지역 근접 가중
_SIM_MONTHS = 36                  # 유사도 가중 집계 기간(개월) — 지역은 시군구로 좁히되 기간은 넓혀 표본 확보
_SIM_MIN_NEFF = 3.0               # 이 유효표본수 미만이면 미채택(→ 기존 방식 폴백)


def _weighted_percentile(pairs, q):
    """pairs=[(값, 가중치)], q∈[0,1] → 가중 분위수(스텝)."""
    if not pairs:
        return None
    s = sorted(pairs, key=lambda t: t[0])
    total = sum(w for _, w in s)
    if total <= 0:
        return None
    thresh = q * total
    acc = 0.0
    for v, w in s:
        acc += w
        if acc >= thresh:
            return round(v, 2)
    return round(s[-1][0], 2)


def _similarity_weighted_stat(rows, target_area, sido, sigungu, dong):
    """면적 근접도 × 지역 근접도로 가중한 실측 낙찰가율.
    반환: {rate,p25,p75,n,neff,ref_fail} 또는 None(면적 없음·표본 부족)."""
    if not target_area or target_area <= 0:
        return None
    import math
    weighted = []       # (bid_rate, weight)
    fail_weighted = []  # (fail_count, weight)
    for r in rows:
        br = r.get('bid_rate')
        ar = r.get('area_sqm')
        if br is None or ar is None:
            continue
        try:
            br = float(br)
            ar = float(ar)
        except (TypeError, ValueError):
            continue
        if ar <= 0 or br <= 0:
            continue
        # 지역 근접 tier — 행의 자기 시도/시군구로 동을 추출해 본건 동과 비교
        rd = _row_dong(r.get('address'), r.get('sido'), r.get('sigungu'))
        if dong and rd == dong:
            tw = _SIM_TIER_W['dong']
        elif sigungu and (r.get('sigungu') or '') == sigungu:
            tw = _SIM_TIER_W['sigungu']
        else:
            tw = _SIM_TIER_W['sido']
        # 면적 근접 (가우시안 커널)
        z = (ar - target_area) / (target_area * _SIM_AREA_BW)
        w = tw * math.exp(-0.5 * z * z)
        if w <= 1e-6:
            continue
        weighted.append((br, w))
        fc = r.get('fail_count')
        if fc is not None:
            try:
                fail_weighted.append((int(fc), w))
            except (TypeError, ValueError):
                pass
    if not weighted:
        return None
    sw = sum(w for _, w in weighted)
    sw2 = sum(w * w for _, w in weighted)
    neff = (sw * sw / sw2) if sw2 > 0 else 0.0
    if neff < _SIM_MIN_NEFF:
        return None
    ref_fail = None
    if fail_weighted:
        fsw = sum(w for _, w in fail_weighted)
        if fsw > 0:
            ref_fail = round(sum(f * w for f, w in fail_weighted) / fsw, 2)
    return {'rate': _weighted_percentile(weighted, 0.5),
            'p25': _weighted_percentile(weighted, 0.25),
            'p75': _weighted_percentile(weighted, 0.75),
            'n': len(weighted), 'neff': round(neff, 1), 'ref_fail': ref_fail}


@app.route('/api/auction/rates')
def auction_rates():
    if not supabase:
        return jsonify({'available': False, 'reason': 'Supabase 미설정'})
    use_group = (request.args.get('use_group') or '').strip()
    sido = (request.args.get('sido') or '').strip()
    sigungu = (request.args.get('sigungu') or '').strip()
    dong = (request.args.get('dong') or '').strip()
    min_n = request.args.get('min_n', default=5, type=int)
    if not use_group:
        return jsonify({'error': 'use_group 필수'}), 400
    try:
        rows = (supabase.table('auction_rate_stats')
                .select('*').eq('use_group', use_group).execute().data) or []
    except Exception as e:
        return jsonify({'available': False, 'reason': str(e)})

    # (sido, sigungu, period) → row 색인
    idx = {(r.get('sido'), r.get('sigungu'), r.get('period_months')): r for r in rows}

    # 지역 캐스케이드: 시군구 → 시도 → 전국
    scopes = []
    if sigungu:
        scopes.append(('sigungu', sido or None, sigungu))
    if sido:
        scopes.append(('sido', sido, None))
    scopes.append(('national', None, None))

    hit = None
    chosen_scope = None
    chosen_period = None
    for scope_name, s_sido, s_sgg in scopes:
        for m in _PERIOD_ORDER:
            r = idx.get((s_sido, s_sgg, m))
            if r and (r.get('sample_n') or 0) >= min_n:
                hit, chosen_scope, chosen_period = r, scope_name, m
                break
        if hit:
            break

    # 집계(auction_rate_stats) 미존재해도 아래 동/유사도 가중은 원본에서 계산 가능하므로
    # 여기서 바로 종료하지 않는다. 최종적으로 hit·dong·sim 모두 없을 때만 미제공 처리.
    if hit:
        scope_region = (hit.get('sigungu') if chosen_scope == 'sigungu'
                        else hit.get('sido') if chosen_scope == 'sido' else '전국')
        period_label = _PERIOD_LABEL.get(chosen_period, f'{chosen_period}개월')
        # 산출근거 문구: "서울특별시 강남구 · 오피스텔 · 최근 6개월 (n=9, 중앙값)"
        derivation = f'{scope_region} · {period_label} · 표본 {hit.get("sample_n")}건 (중앙값)'
    else:
        scope_region = sigungu or sido or '전국'
        chosen_scope = 'sigungu' if sigungu else 'sido' if sido else 'national'
        period_label = None
        derivation = None

    # ── 유찰횟수 보정용 기울기 ────────────────────────────────────────────
    # 실측 중앙값은 '평균적인 유찰 섞임'을 반영하므로, 대상 물건이 표본 평균보다 더/덜
    # 유찰됐을 때만 가감하기 위한 기울기·기준점을 같은 지역·용도의 원본 낙찰기록에서 추정한다.
    # 기간은 median_rate 기간(짧을 수 있음)에 묶지 않고 항상 넓은 창(≥24개월)에서 뽑아
    # 소표본으로 기울기가 과격해지는 것을 완화한다. 표본 부족 시 None(→ 무보정).
    fail_slope = fail_ref = fail_levels = None
    fail_slope_months = 0 if chosen_period == 0 else max(chosen_period or 0, _FAIL_SLOPE_MIN_MONTHS)
    try:
        fq = (supabase.table('auction_sales')
              .select('fail_count,bid_rate,sale_date,sido,sigungu,use_group')
              .eq('use_group', use_group)
              .not_.is_('bid_rate', 'null')
              .not_.is_('fail_count', 'null')
              .limit(4000))
        _fs_sigungu = (hit.get('sigungu') if hit else sigungu) or None
        _fs_sido = (hit.get('sido') if hit else sido) or None
        if chosen_scope == 'sigungu' and _fs_sigungu:
            fq = fq.eq('sigungu', _fs_sigungu)
            if _fs_sido:
                fq = fq.eq('sido', _fs_sido)
        elif chosen_scope == 'sido' and _fs_sido:
            fq = fq.eq('sido', _fs_sido)
        if fail_slope_months:   # 0 = 전체 기간이면 날짜 필터 없음
            import datetime as _dt2
            _cut = (_dt2.date.today() - _dt2.timedelta(days=fail_slope_months * 30)).isoformat()
            fq = fq.gte('sale_date', _cut)
        frows = (fq.execute().data) or []
        fail_slope, fail_ref, fail_levels = _estimate_fail_slope(frows)
    except Exception:
        fail_slope = fail_ref = fail_levels = None

    # ── 동(洞) 단위 실측 낙찰가율 ─────────────────────────────────────────
    # 본건과 같은 동(예: 잠실동) 낙찰사례가 _DONG_MIN_N(5)건 이상이면, 시도 실측보다
    # 관련성이 높으므로 동 단위 중앙 낙찰가율을 함께 내려보내 프론트가 우선 채택하게 한다.
    # 부족하면 None → 프론트는 기존 시도 실측으로 폴백. 아파트·비아파트 모두 동일 규칙.
    dong_stat = None
    if dong and sigungu:
        try:
            dq = (supabase.table('auction_sales')
                  .select('bid_rate,fail_count,address,sido,sigungu,use_group')
                  .eq('use_group', use_group)
                  .eq('sigungu', sigungu)
                  .not_.is_('bid_rate', 'null')
                  .limit(3000))
            if sido:
                dq = dq.eq('sido', sido)
            import datetime as _dt4
            _dcut = (_dt4.date.today() - _dt4.timedelta(days=_DONG_MONTHS * 30)).isoformat()
            dq = dq.gte('sale_date', _dcut)
            drows = (dq.execute().data) or []
            dong_stat = _dong_stat(drows, sido, sigungu, dong)
        except Exception:
            dong_stat = None

    # ── 유사도 가중 실측 낙찰가율 (면적×지역 근접 가중) ──────────────────────
    # 본건 면적(area)이 주어졌을 때만 계산. 범위는 '같은 시·군·구'로 한정한다.
    #   ─ 시도 전체(예: 경기도)로 넓히면 파주 물건에 용인·화성이 섞여 시장이 달라짐 →
    #     신뢰성 붕괴. 지역은 시군구까지만 좁히고, 표본은 시간(기간)을 넓혀 확보한다.
    #   본건 시군구에 표본이 부족하면 sim_stat=None → 지역 통계(한국부동산원)로 폴백.
    area = request.args.get('area', type=float)
    sim_stat = None
    if area and sigungu:
        try:
            sq = (supabase.table('auction_sales')
                  .select('bid_rate,area_sqm,fail_count,address,sido,sigungu,use_group,sale_date')
                  .eq('use_group', use_group)
                  .eq('sigungu', sigungu)
                  .not_.is_('bid_rate', 'null')
                  .not_.is_('area_sqm', 'null')
                  .order('sale_date', desc=True)
                  .limit(4000))
            if sido:
                sq = sq.eq('sido', sido)
            import datetime as _dt5
            _scut = (_dt5.date.today() - _dt5.timedelta(days=_SIM_MONTHS * 30)).isoformat()
            sq = sq.gte('sale_date', _scut)
            srows = (sq.execute().data) or []
            sim_stat = _similarity_weighted_stat(srows, area, sido, sigungu, dong)
        except Exception:
            sim_stat = None

    # 집계·동·유사도 중 하나라도 있어야 제공. 모두 없으면 미제공(→ 프론트는 정적 통계 폴백).
    if not (hit or dong_stat or sim_stat):
        return jsonify({'available': False, 'reason': '표본 부족'})
    _h = hit or {}

    return jsonify({
        'available': True,
        'scope': chosen_scope, 'region': scope_region,
        'period_months': chosen_period, 'period_label': period_label,
        'use_group': use_group, 'sido': _h.get('sido') or sido, 'sigungu': _h.get('sigungu') or sigungu,
        'sample_n': _h.get('sample_n'),
        'median_rate': _h.get('median_rate'), 'avg_rate': _h.get('avg_rate'),
        'p25_rate': _h.get('p25_rate'), 'p75_rate': _h.get('p75_rate'),
        'avg_bidders': _h.get('avg_bidders'), 'asof': _h.get('asof'),
        'derivation': derivation,
        # 유찰보정: 유찰 1회당 %p 변화(fail_slope) · 기준 유찰횟수(fail_ref) · 단계별 내역
        # fail_slope_months = 기울기 추정에 사용한 기간(개월, 0=전체). 실측 기간과 분리됨.
        'fail_slope': fail_slope, 'fail_ref': fail_ref, 'fail_levels': fail_levels,
        'fail_slope_months': fail_slope_months,
        # 동 단위 실측(있으면 프론트가 시도 실측보다 우선 채택). 부족하면 None.
        'dong': dong or None,
        'dong_rate': (dong_stat or {}).get('rate'),
        'dong_p25': (dong_stat or {}).get('p25'),
        'dong_p75': (dong_stat or {}).get('p75'),
        'dong_n': (dong_stat or {}).get('n'),
        'dong_ref_fail': (dong_stat or {}).get('ref_fail'),
        # 유사도 가중 실측(면적×지역 근접). 있으면 프론트가 우선 채택. 부족하면 None.
        'sim_rate': (sim_stat or {}).get('rate'),
        'sim_p25': (sim_stat or {}).get('p25'),
        'sim_p75': (sim_stat or {}).get('p75'),
        'sim_n': (sim_stat or {}).get('n'),
        'sim_neff': (sim_stat or {}).get('neff'),
        'sim_ref_fail': (sim_stat or {}).get('ref_fail'),
    })


# ============================================================
# 유사 낙찰사례 조회 (04 경공매 사례 탭 — 참고용)
# 같은 시군구 내에서 [같은 동 / 같은 용도 / 면적 ±20%] 중 2개 이상 일치.
# 기간 1→3→6→12→24개월 순차 확대 (min_results 이상이면 중단).
# ============================================================
@app.route('/api/auction/comparables')
def auction_comparables():
    if not supabase:
        return jsonify({'available': False, 'reason': 'Supabase 미설정', 'items': []})
    sido = (request.args.get('sido') or '').strip()
    sigungu = (request.args.get('sigungu') or '').strip()
    dong = (request.args.get('dong') or '').strip()
    use_group = (request.args.get('use_group') or '').strip()
    area = request.args.get('area', type=float)
    area_tol = request.args.get('area_tol', default=0.20, type=float)
    # 스코프별 채택 임계치: 좁은 동 단계는 낮게(관련도 높음), 구 단계는 기본값.
    min_dong = request.args.get('min_results_dong', default=3, type=int)
    min_results = request.args.get('min_results', default=5, type=int)
    if not sigungu:
        return jsonify({'available': False, 'reason': '본건 시군구 필요', 'items': []})

    import datetime as _dt
    today = _dt.date.today()
    # 지역은 시군구까지만 좁히고, 표본은 기간(최대 60개월)을 넓혀 확보 → 타 시·군 혼입 방지.
    cutoff24 = (today - _dt.timedelta(days=60 * 30)).isoformat()

    _SELECT = ('court_name,case_no,item_no,use_type,use_group,sido,sigungu,'
               'address,area_sqm,appraisal_price,sale_price,sale_date,'
               'result,fail_count,bid_rate')

    # 지역 스코프별로 필요할 때만 조회(lazy). 'sigungu' 결과는 동·구 단계에서 재사용하고,
    # 'sido' 결과는 시도 단계에 도달했을 때만 추가로 당겨온다. (auction_sales에는 좌표·번지·
    #  준공연도가 없어 행정구역 단위 확대만 가능)
    _fetch_cache = {}

    def _fetch(level):
        if level in _fetch_cache:
            return _fetch_cache[level]
        q = (supabase.table('auction_sales')
             .select(_SELECT)
             .not_.is_('sale_price', 'null')
             .gte('sale_date', cutoff24)
             .order('sale_date', desc=True)
             .limit(1000))
        if level == 'sido':
            q = q.eq('sido', sido)
        else:  # 'sigungu'
            q = q.eq('sigungu', sigungu)
            if sido:
                q = q.eq('sido', sido)
        data = (q.execute().data) or []
        _fetch_cache[level] = data
        return data

    def _dong_of(r):
        a = (r.get('address') or '')
        for t in (r.get('sido') or '', r.get('sigungu') or ''):
            a = a.replace(t, '')
        return a.strip()

    def _dong_match(r):
        # auction_sales 주소는 동 단위까지(번지 없음)라 보통 그대로 일치하지만,
        # 잔여 토큰(예: '방학동 산12')이 있어도 첫 토큰(행정동명)으로 견고하게 비교.
        if not dong:
            return False
        dd = _dong_of(r)
        return bool(dd) and dd.split()[0] == dong

    # 아파트는 용도 일치를 항상 강제(같은 아파트 용도끼리만). 그 외 용도는 확대 최종
    # 단계에서만 용도를 완화한다.
    require_use = (use_group == 'apt')

    def _region_ok(r, level):
        if level == 'dong':
            return _dong_match(r)
        if level == 'sigungu':
            return (r.get('sigungu') or '') == sigungu and (not sido or (r.get('sido') or '') == sido)
        return (r.get('sido') or '') == sido   # 'sido'

    def _hits(r, tol):
        # 뱃지 표시용: 동/용도/면적 중 무엇이 일치했는지 (지역·면적이 하드 필터여도 표기는 유지)
        hits = []
        if _dong_match(r):
            hits.append('동')
        if use_group and r.get('use_group') == use_group:
            hits.append('용도')
        ra = r.get('area_sqm')
        if area and ra and abs(ra - area) / area <= tol:
            hits.append('면적')
        return hits

    # 확대 사다리: (지역스코프, 면적허용%, 기간개월). 좁고 최근인 단계 → 넓고 오래된 단계.
    # ★ 지역은 '시군구'까지만 확대한다. 시도 전체(예: 경기도)로 넓히면 파주 물건에
    #   용인·화성이 섞여 시장이 달라져 신뢰성이 붕괴되므로, 지역은 좁게 고정하고
    #   부족분은 '기간(최대 60개월)'을 넓혀 같은 시·군의 예전 사례로 채운다.
    #   시군구에도 사례가 없으면 → count 0 → 프론트가 '사례 없음'으로 표기.
    LADDER = [
        ('dong',    5,  6),
        ('dong',    10, 12),
        ('dong',    20, 24),
        ('dong',    20, 36),
        ('sigungu', 10, 12),
        ('sigungu', 20, 24),
        ('sigungu', 30, 36),
        ('sigungu', 30, 60),   # 최종 단계: 비아파트는 여기서만 용도 완화
    ]
    # 스코프별 채택 임계치(이 건수 이상이면 멈추고 채택). 좁을수록 낮게.
    THRESH = {'dong': min_dong, 'sigungu': min_results, 'sido': 1}
    _LEVEL_LABEL = {'dong': '동', 'sigungu': '시·군·구', 'sido': '시도'}
    _LEVEL_NAME = {'dong': dong, 'sigungu': sigungu, 'sido': sido}

    chosen_level, chosen_tol_pct, chosen_period, picked = 'sigungu', 30, 60, []
    try:
        for idx, (level, tol_pct, m) in enumerate(LADDER):
            # 시도 확대는 사용하지 않음(지역 정합성 우선). 방어적으로 남겨둠.
            if level == 'sido' and not sido:
                continue
            rows = _fetch('sido' if level == 'sido' else 'sigungu')
            tol = tol_pct / 100.0
            cut = (today - _dt.timedelta(days=m * 30)).isoformat()
            # 마지막(가장 넓은) 단계에서만 비아파트 용도 완화
            relax_use = (idx == len(LADDER) - 1) and not require_use
            seen_cases = {}   # (법원,사건번호,물건번호) → row : 같은 사건 중복 제거
            for r in rows:
                if (r.get('sale_date') or '') < cut:
                    continue
                if not _region_ok(r, level):
                    continue
                # 면적: 본건 면적이 주어졌으면 허용범위 내여야 함(하드)
                if area:
                    ra = r.get('area_sqm')
                    if not ra or abs(ra - area) / area > tol:
                        continue
                # 용도: 아파트는 항상, 그 외는 완화 단계 전까지 일치 강제
                if require_use or not relax_use:
                    if not (use_group and r.get('use_group') == use_group):
                        continue
                key = (r.get('court_name'), r.get('case_no'), r.get('item_no'))
                if key in seen_cases:
                    continue
                hits = _hits(r, tol)
                seen_cases[key] = {
                    'court_name': r.get('court_name'), 'case_no': r.get('case_no'),
                    'item_no': r.get('item_no'), 'use_type': r.get('use_type'),
                    'dong': _dong_of(r), 'address': r.get('address'),
                    'area_sqm': r.get('area_sqm'),
                    'appraisal_price': r.get('appraisal_price'),
                    'sale_price': r.get('sale_price'), 'bid_rate': r.get('bid_rate'),
                    'sale_date': r.get('sale_date'), 'result': r.get('result'),
                    'fail_count': r.get('fail_count'),
                    'match_score': len(hits), 'match_hits': hits,
                }
            cur = list(seen_cases.values())
            # 관련도(일치 항목 수) → 최근순으로 정렬
            cur.sort(key=lambda x: (x.get('match_score', 0), x.get('sale_date') or ''), reverse=True)
            chosen_level, chosen_tol_pct, chosen_period, picked = level, tol_pct, m, cur
            if len(cur) >= THRESH[level]:
                break
    except Exception as e:
        return jsonify({'available': False, 'reason': str(e), 'items': []})

    # 참고 지표: 조회된 사례의 중앙값 낙찰가율 (추정 반영은 안 함)
    rates = sorted(x['bid_rate'] for x in picked if x.get('bid_rate') is not None)
    med = None
    if rates:
        n = len(rates)
        med = rates[n // 2] if n % 2 else round((rates[n // 2 - 1] + rates[n // 2]) / 2, 2)

    _plabel = {1: '최근 1개월', 3: '최근 3개월', 6: '최근 6개월',
               12: '최근 12개월', 24: '최근 24개월', 36: '최근 36개월', 60: '최근 60개월'}
    return jsonify({
        'available': True, 'count': len(picked),
        'area_tol_pct': chosen_tol_pct,
        'period_months': chosen_period, 'period_label': _plabel.get(chosen_period),
        'region': _LEVEL_NAME.get(chosen_level) or sigungu,
        'region_level': _LEVEL_LABEL.get(chosen_level),
        'median_bid_rate': med,
        'items': picked,
    })


# ============================================================
# 진단: auction_sales의 fail_count 채움률 · 유찰차수 분포 · 보정 적용가능 범위
#   URL: /admin/diag-fail-count?key=ADMIN_SECRET
#   유찰횟수 보정 기능이 실제로 몇 %의 데이터에서 작동하는지 사람이 눈으로 확인.
# ============================================================
@app.route('/admin/diag-fail-count')
def admin_diag_fail_count():
    key = request.args.get('key', '')
    if not ADMIN_SECRET:
        return jsonify({'error': 'ADMIN_SECRET 환경변수가 설정되지 않았습니다.'}), 503
    if key != ADMIN_SECRET:
        return jsonify({'error': '잘못된 관리자 키'}), 403
    if not supabase:
        return jsonify({'error': 'Supabase 미설정'}), 503

    import datetime as _dt3

    def _count(build):
        try:
            q = supabase.table('auction_sales').select('*', count='exact').limit(1)
            return build(q).execute().count
        except Exception:
            return None

    total = _count(lambda q: q)
    with_fail = _count(lambda q: q.not_.is_('fail_count', 'null'))
    with_rate = _count(lambda q: q.not_.is_('bid_rate', 'null'))
    with_both = _count(lambda q: q.not_.is_('fail_count', 'null').not_.is_('bid_rate', 'null'))

    # 분포·버킷 판정용 표본: 최근 12개월, fail_count·bid_rate 보유분을 페이지네이션 수집
    cut12 = (_dt3.date.today() - _dt3.timedelta(days=365)).isoformat()
    sample, page, PAGE, MAX = [], 0, 1000, 10000
    try:
        while len(sample) < MAX:
            rows = (supabase.table('auction_sales')
                    .select('fail_count,bid_rate,use_group,sido,sigungu,sale_date')
                    .not_.is_('fail_count', 'null').not_.is_('bid_rate', 'null')
                    .gte('sale_date', cut12)
                    .order('sale_date', desc=True)
                    .range(page * PAGE, page * PAGE + PAGE - 1)
                    .execute().data) or []
            sample.extend(rows)
            if len(rows) < PAGE:
                break
            page += 1
    except Exception as e:
        return jsonify({'error': f'표본 수집 실패: {e}'}), 502

    sample_capped = len(sample) >= MAX

    # 유찰차수 분포
    dist = {}
    for r in sample:
        try:
            fc = int(r.get('fail_count'))
        except (TypeError, ValueError):
            continue
        dist[fc] = dist.get(fc, 0) + 1

    # 지역×용도 버킷별로 기울기 산출 가능한지 판정
    buckets = {}
    for r in sample:
        k = (r.get('use_group') or '?', r.get('sido') or '?', r.get('sigungu') or '?')
        buckets.setdefault(k, []).append(r)
    qualifying = []
    for k, rows in buckets.items():
        slope, ref, levels = _estimate_fail_slope(rows)
        if slope is not None:
            qualifying.append({'use_group': k[0], 'sido': k[1], 'sigungu': k[2],
                               'n': len(rows), 'slope': slope, 'ref': ref,
                               'levels': len(levels)})
    qualifying.sort(key=lambda x: x['n'], reverse=True)

    def pct(a, b):
        return f'{round(100.0 * a / b, 1)}%' if (a is not None and b) else '—'

    fill_pct = pct(with_fail, total)
    rows_html = ''.join(
        f'<tr><td>{fc}회</td><td style="text-align:right">{dist[fc]:,}건</td></tr>'
        for fc in sorted(dist))
    q_html = ''.join(
        f'<tr><td>{q["use_group"]}</td><td>{q["sido"]}</td><td>{q["sigungu"]}</td>'
        f'<td style="text-align:right">{q["n"]:,}</td><td style="text-align:right">{q["levels"]}</td>'
        f'<td style="text-align:right">{q["slope"]:+.2f}%p</td><td style="text-align:right">{q["ref"]:.2f}회</td></tr>'
        for q in qualifying[:40])

    verdict = ('✅ 데이터가 충분해 유찰보정이 여러 지역에서 작동합니다.'
               if len(qualifying) >= 5 else
               '⚠️ 보정이 켜지는 지역이 적습니다. fail_count 수집 보강을 검토하세요.'
               if len(qualifying) >= 1 else
               '❌ 현재 표본으로는 보정이 거의 켜지지 않습니다(대부분 무보정). fail_count 채움 보강이 우선입니다.')

    html = f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>유찰보정 데이터 진단</title>
<style>
 body{{font-family:system-ui,'Malgun Gothic',sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#1e293b;line-height:1.5}}
 h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px;border-bottom:2px solid #e2e8f0;padding-bottom:4px}}
 table{{border-collapse:collapse;width:100%;font-size:14px;margin-top:8px}}
 th,td{{border:1px solid #e2e8f0;padding:6px 10px}} th{{background:#f8fafc;text-align:left}}
 .big{{font-size:15px}} .kpi{{display:flex;gap:16px;flex-wrap:wrap;margin-top:10px}}
 .kpi div{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px 16px;min-width:150px}}
 .kpi b{{display:block;font-size:22px;color:#0f766e}} .muted{{color:#64748b;font-size:13px}}
 .verdict{{margin-top:14px;padding:12px 16px;border-radius:8px;background:#f0fdfa;border:1px solid #99f6e4;font-weight:600}}
</style></head><body>
<h1>유찰횟수 보정 — 데이터 진단</h1>
<p class="muted">auction_sales 테이블 기준 · 생성 시각(서버): 조회 시점</p>
<div class="verdict">{verdict}</div>
<h2>1) 채움률 (전체 기간)</h2>
<div class="kpi">
 <div>전체 행<b>{(f'{total:,}' if total is not None else '—')}</b></div>
 <div>fail_count 채움<b>{(f'{with_fail:,}' if with_fail is not None else '—')}</b><span class="muted">채움률 {fill_pct}</span></div>
 <div>bid_rate 있음<b>{(f'{with_rate:,}' if with_rate is not None else '—')}</b></div>
 <div>둘 다 있음<b>{(f'{with_both:,}' if with_both is not None else '—')}</b></div>
</div>
<h2>2) 유찰차수 분포 <span class="muted">(최근 12개월 표본 {len(sample):,}건{' · 상한도달' if sample_capped else ''})</span></h2>
<p class="muted">0회·1회·2회… 로 <b>퍼져 있어야</b> 기울기(유찰 1회당 변화)를 추정할 수 있습니다.</p>
<table><thead><tr><th>유찰차수</th><th style="text-align:right">건수</th></tr></thead><tbody>{rows_html or '<tr><td colspan=2>표본 없음</td></tr>'}</tbody></table>
<h2>3) 보정이 실제로 켜지는 지역×용도 <span class="muted">({len(qualifying)}개 버킷, 상위 40개 표시)</span></h2>
<p class="muted">각 버킷에서 유찰단계별 표본(단계당 ≥5건)이 2단계 이상 모여 기울기가 산출된 경우만.</p>
<table><thead><tr><th>용도</th><th>시도</th><th>구</th><th style="text-align:right">표본</th><th style="text-align:right">유찰단계</th><th style="text-align:right">기울기</th><th style="text-align:right">기준유찰</th></tr></thead>
<tbody>{q_html or '<tr><td colspan=7>기울기 산출 가능한 버킷이 없습니다.</td></tr>'}</tbody></table>
<p class="muted" style="margin-top:24px">※ 이 표본은 최근 12개월·최대 {MAX:,}건 기준입니다. 전체 채움률(1번)은 count 쿼리로 정확히 집계했습니다.</p>
</body></html>'''
    return Response(html, mimetype='text/html; charset=utf-8')


# ============================================================
# 낙찰가율 추정 정확도 백테스트 (leave-one-out)
#   완료된 낙찰 건을 '자기 자신을 빼고' 유사도 가중으로 예측 → 실제 낙찰가율과 비교.
#   유사도 가중(신규) vs 단순 중앙값(구 방식)을 용도별로 비교해 개선 효과를 실측.
#   읽기전용 집계(민감정보 없음)라 키 없이 공개. (진단 페이지와 동일 정책)
#   URL: /admin/backtest?sido=서울특별시&months=24&sample=250
# ============================================================
@app.route('/admin/backtest')
def admin_backtest():
    if not supabase:
        return jsonify({'error': 'Supabase 미설정'}), 503

    import datetime as _dtb
    sido = (request.args.get('sido') or '서울특별시').strip()
    months = request.args.get('months', default=24, type=int)
    sample_n = request.args.get('sample', default=250, type=int)
    fmt = (request.args.get('format') or 'html').strip()
    cut = (_dtb.date.today() - _dtb.timedelta(days=months * 30)).isoformat()

    USE_LABELS = {'apt': '아파트', 'rh': '연립·다세대', 'sh': '단독·다가구', 'offi': '오피스텔'}
    POOL_MAX, MIN_POOL = 2500, 8

    def _fetch_pool(ug):
        out, page, PAGE = [], 0, 1000
        while len(out) < POOL_MAX:
            try:
                rows = (supabase.table('auction_sales')
                        .select('bid_rate,area_sqm,fail_count,address,sido,sigungu,sale_date')
                        .eq('use_group', ug).eq('sido', sido)
                        .not_.is_('bid_rate', 'null').not_.is_('area_sqm', 'null')
                        .gte('sale_date', cut)
                        .order('sale_date', desc=True)
                        .range(page * PAGE, page * PAGE + PAGE - 1)
                        .execute().data) or []
            except Exception:
                break
            out.extend(rows)
            if len(rows) < PAGE:
                break
            page += 1
        return out[:POOL_MAX]

    def _abs_stats(errs):
        if not errs:
            return None
        a = [abs(e) for e in errs]
        return {'n': len(a), 'mae': round(sum(a) / len(a), 2),
                'med': round(_median(a), 2),
                'hit5': round(100.0 * sum(1 for x in a if x <= 5) / len(a), 1),
                'hit10': round(100.0 * sum(1 for x in a if x <= 10) / len(a), 1)}

    results = []
    g_sim, g_base = [], []   # 전체(용도무관) 공정비교용 누적
    for ug in ['apt', 'rh', 'sh', 'offi']:
        pool = _fetch_pool(ug)
        if len(pool) < MIN_POOL:
            results.append({'ug': ug, 'label': USE_LABELS[ug], 'pool': len(pool),
                            'insufficient': True})
            continue
        idxs = list(range(len(pool)))
        if len(idxs) > sample_n:                     # 균등 간격 샘플
            step = len(idxs) / float(sample_n)
            idxs = sorted(set(int(i * step) for i in range(sample_n)))
        sim_all, sim_paired, base_paired = [], [], []
        for t in idxs:
            row = pool[t]
            try:
                actual = float(row.get('bid_rate'))
                area = float(row.get('area_sqm'))
            except (TypeError, ValueError):
                continue
            if actual <= 0 or area <= 0:
                continue
            sgg = row.get('sigungu')
            dong = _row_dong(row.get('address'), row.get('sido'), sgg)
            # 운영과 동일하게 '같은 시·군·구'로만 한정(타 시·군 혼입 금지). 자기 자신 제외.
            pool_sgg = [pool[j] for j in range(len(pool))
                        if j != t and pool[j].get('sigungu') == sgg]
            sim = _similarity_weighted_stat(pool_sgg, area, sido, sgg, dong)
            pred_sim = sim['rate'] if sim else None
            # 베이스라인(구 방식): 같은 시·군·구 낙찰가율 중앙값 — 면적 무관
            same_sgg = [float(r['bid_rate']) for r in pool_sgg if r.get('bid_rate') is not None]
            pred_base = _median(same_sgg) if len(same_sgg) >= 3 else None
            if pred_sim is not None:
                sim_all.append(pred_sim - actual)
                if pred_base is not None:            # 공정비교: 둘 다 예측된 건만
                    sim_paired.append(pred_sim - actual)
                    base_paired.append(pred_base - actual)
        g_sim.extend(sim_paired)
        g_base.extend(base_paired)
        results.append({
            'ug': ug, 'label': USE_LABELS[ug], 'pool': len(pool), 'tested': len(idxs),
            'coverage': round(100.0 * len(sim_all) / len(idxs), 1) if idxs else 0,
            'sim': _abs_stats(sim_paired), 'base': _abs_stats(base_paired),
            'sim_all': _abs_stats(sim_all),
        })

    overall = {'sim': _abs_stats(g_sim), 'base': _abs_stats(g_base)}

    if fmt == 'json':
        return jsonify({'sido': sido, 'months': months, 'sample': sample_n,
                        'results': results, 'overall': overall})

    # ── HTML 리포트 ──────────────────────────────────────────────────────
    def _cell(s, k):
        return f'{s[k]}' if s else '—'

    def _row_html(r):
        if r.get('insufficient'):
            return (f'<tr><td><b>{r["label"]}</b></td>'
                    f'<td colspan="8" class="muted">표본 부족(풀 {r["pool"]}건) — 백테스트 생략</td></tr>')
        sim, base = r.get('sim'), r.get('base')
        delta = None
        if sim and base:
            delta = round(base['mae'] - sim['mae'], 2)   # +면 유사도가중이 더 정확
        dcol = '#0f766e' if (delta or 0) > 0 else '#b45309' if (delta or 0) < 0 else '#64748b'
        return (f'<tr><td><b>{r["label"]}</b><div class="muted">풀 {r["pool"]:,} · 테스트 {r["tested"]} · 커버리지 {r["coverage"]}%</div></td>'
                f'<td style="text-align:right">{_cell(base,"mae")}</td>'
                f'<td style="text-align:right">{_cell(base,"med")}</td>'
                f'<td style="text-align:right">{_cell(base,"hit5")}%</td>'
                f'<td style="text-align:right;background:#f0fdfa"><b>{_cell(sim,"mae")}</b></td>'
                f'<td style="text-align:right;background:#f0fdfa">{_cell(sim,"med")}</td>'
                f'<td style="text-align:right;background:#f0fdfa">{_cell(sim,"hit5")}%</td>'
                f'<td style="text-align:right;color:{dcol};font-weight:700">'
                f'{("+" + str(delta)) if (delta is not None and delta > 0) else (str(delta) if delta is not None else "—")}%p</td></tr>')

    rows_html = ''.join(_row_html(r) for r in results)
    ov = overall
    verdict = '표본 부족으로 판정 보류'
    if ov['sim'] and ov['base']:
        d = round(ov['base']['mae'] - ov['sim']['mae'], 2)
        if d > 0.3:
            verdict = f'✅ 유사도 가중이 단순 중앙값보다 평균오차를 <b>{d}%p</b> 줄였습니다 (전체 기준).'
        elif d < -0.3:
            verdict = f'⚠️ 이 지역·기간에선 유사도 가중이 오히려 <b>{abs(d)}%p</b> 더 큽니다. 가중치 조정 검토 필요.'
        else:
            verdict = f'➖ 두 방식 차이가 미미합니다(±{abs(d)}%p). 표본을 늘리거나 가중치 조정을 검토하세요.'

    ov_sim_mae = ov['sim']['mae'] if ov['sim'] else '—'
    ov_base_mae = ov['base']['mae'] if ov['base'] else '—'
    ov_n = ov['sim']['n'] if ov['sim'] else 0

    html = f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>낙찰가율 정확도 백테스트</title>
<style>
 body{{font-family:system-ui,'Malgun Gothic',sans-serif;max-width:920px;margin:24px auto;padding:0 16px;color:#1e293b;line-height:1.55}}
 h1{{font-size:20px}} h2{{font-size:15px;margin-top:24px;color:#334155}}
 table{{border-collapse:collapse;width:100%;font-size:14px;margin-top:8px}}
 th,td{{border:1px solid #e2e8f0;padding:7px 10px}} th{{background:#f8fafc;text-align:right}} th:first-child,td:first-child{{text-align:left}}
 .muted{{color:#64748b;font-size:12px;font-weight:400}}
 .verdict{{margin:14px 0;padding:13px 16px;border-radius:8px;background:#f0fdfa;border:1px solid #99f6e4;font-weight:600}}
 .kpi{{display:flex;gap:14px;flex-wrap:wrap;margin:10px 0}}
 .kpi div{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px 16px;min-width:150px}}
 .kpi b{{display:block;font-size:22px;color:#0f766e}}
 .note{{color:#64748b;font-size:12.5px;margin-top:18px}}
</style></head><body>
<h1>낙찰가율 추정 정확도 백테스트 <span class="muted">· {sido} · 최근 {months}개월</span></h1>
<p class="muted">완료된 낙찰 건을 <b>자기 자신을 빼고</b>(leave-one-out) 예측해 실제 낙찰가율과 비교합니다. 오차 = |예측 낙찰가율 − 실제 낙찰가율| (%p).</p>
<div class="verdict">{verdict}</div>
<div class="kpi">
 <div>전체 평균오차(유사도)<b>{ov_sim_mae}%p</b><span class="muted">낮을수록 정확</span></div>
 <div>전체 평균오차(단순)<b>{ov_base_mae}%p</b><span class="muted">구 방식</span></div>
 <div>비교 표본<b>{ov_n:,}건</b><span class="muted">두 방식 모두 예측된 건</span></div>
</div>
<h2>용도별 비교 <span class="muted">(초록 = 유사도 가중 · 개선폭 = 단순−유사도, +면 개선)</span></h2>
<table>
<thead><tr><th>용도</th>
<th>단순 MAE</th><th>단순 중앙</th><th>단순 ±5%p</th>
<th>유사도 MAE</th><th>유사도 중앙</th><th>유사도 ±5%p</th><th>개선폭</th></tr></thead>
<tbody>{rows_html or '<tr><td colspan=8>데이터 없음</td></tr>'}</tbody></table>
<p class="note">※ MAE=평균절대오차, 중앙=중앙절대오차, ±5%p=오차 5%p 이내 적중률. 공정비교를 위해 두 방식 모두 예측한 건만 집계했습니다(커버리지=유사도 가중이 산출된 비율).<br>
※ 이 백테스트는 <b>낙찰가율 산정 방식(유사도 가중 vs 단순 중앙값)</b>만 비교하며, 유찰횟수 보정은 아직 반영하지 않았습니다. 등기부는 감정가·낙찰가를 담지 않아 이 검증에 쓰이지 않습니다(정확도 검증엔 auction_sales의 완료 낙찰 건이 사용됨).<br>
※ 파라미터: <code>?sido=서울특별시&amp;months=24&amp;sample=250</code> — 다른 시도/기간으로 바꿔 조회할 수 있습니다.</p>
</body></html>'''
    return Response(html, mimetype='text/html; charset=utf-8')


# ============================================================
# 데이터 커버리지 진단 — auction_sales에 전국 어느 지역·용도가 얼마나 있나
#   시도 × 용도(아파트/연립·다세대/오피스텔/단독·다가구) 매트릭스(낙찰가율 보유 건수).
#   읽기전용 집계라 키 없이 공개. URL: /admin/coverage
# ============================================================
@app.route('/admin/coverage')
def admin_coverage():
    if not supabase:
        return jsonify({'error': 'Supabase 미설정'}), 503

    def _count(build):
        try:
            return build(supabase.table('auction_sales').select('*', count='exact').limit(1)).execute().count
        except Exception:
            return None

    total = _count(lambda q: q)
    with_rate = _count(lambda q: q.not_.is_('bid_rate', 'null'))

    # 분포 표본: 최근순 페이지네이션(시도·용도·낙찰가율·날짜만) — 지역 값을 자동 발견.
    SAMPLE_MAX, page, PAGE = 20000, 0, 1000
    sample = []
    try:
        while len(sample) < SAMPLE_MAX:
            rows = (supabase.table('auction_sales')
                    .select('sido,use_group,bid_rate,sale_date')
                    .order('sale_date', desc=True)
                    .range(page * PAGE, page * PAGE + PAGE - 1)
                    .execute().data) or []
            sample.extend(rows)
            if len(rows) < PAGE:
                break
            page += 1
    except Exception as e:
        return jsonify({'error': f'표본 수집 실패: {e}'}), 502
    sample_capped = len(sample) >= SAMPLE_MAX

    UGS = ['apt', 'rh', 'offi', 'sh']
    UG_LABEL = {'apt': '아파트', 'rh': '연립·다세대', 'offi': '오피스텔', 'sh': '단독·다가구', '기타': '기타'}
    mat, sido_tot, dmin, dmax = {}, {}, None, None
    for r in sample:
        if r.get('bid_rate') is None:
            continue
        s = (r.get('sido') or '(미상)').strip()
        ug = r.get('use_group') or '기타'
        if ug not in UGS:
            ug = '기타'
        mat.setdefault(s, {}).setdefault(ug, 0)
        mat[s][ug] += 1
        sido_tot[s] = sido_tot.get(s, 0) + 1
        d = r.get('sale_date')
        if d:
            dmin = d if (dmin is None or d < dmin) else dmin
            dmax = d if (dmax is None or d > dmax) else dmax

    cols = UGS + (['기타'] if any('기타' in v for v in mat.values()) else [])
    order = sorted(sido_tot, key=lambda k: sido_tot[k], reverse=True)

    def _c(s, ug):
        v = mat.get(s, {}).get(ug, 0)
        bg = '#fef2f2' if v == 0 else '#f0fdf4' if v >= 30 else ''
        col = '#b91c1c' if v == 0 else '#166534' if v >= 30 else '#334155'
        return f'<td style="text-align:right;background:{bg};color:{col}">{v:,}</td>'

    body_rows = ''.join(
        f'<tr><td><b>{s}</b></td>' + ''.join(_c(s, ug) for ug in cols)
        + f'<td style="text-align:right;font-weight:700">{sido_tot[s]:,}</td></tr>'
        for s in order) or '<tr><td colspan="9">데이터 없음</td></tr>'
    col_head = ''.join(f'<th>{UG_LABEL.get(c, c)}</th>' for c in cols)
    empty_sido = [s for s in order if any(mat.get(s, {}).get(ug, 0) == 0 for ug in ['apt', 'rh'])]

    def _fmt(n):
        return f'{n:,}' if n is not None else '—'

    html = f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>낙찰데이터 커버리지</title>
<style>
 body{{font-family:system-ui,'Malgun Gothic',sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#1e293b;line-height:1.55}}
 h1{{font-size:20px}} h2{{font-size:15px;margin-top:22px;color:#334155}}
 table{{border-collapse:collapse;width:100%;font-size:14px;margin-top:8px}}
 th,td{{border:1px solid #e2e8f0;padding:6px 10px}} th{{background:#f8fafc;text-align:right}} th:first-child,td:first-child{{text-align:left}}
 .kpi{{display:flex;gap:14px;flex-wrap:wrap;margin:10px 0}}
 .kpi div{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px 16px;min-width:150px}}
 .kpi b{{display:block;font-size:22px;color:#0f766e}}
 .muted{{color:#64748b;font-size:12.5px}}
 .warn{{margin-top:12px;padding:12px 16px;border-radius:8px;background:#fffbeb;border:1px solid #fde68a;font-weight:500}}
</style></head><body>
<h1>낙찰데이터 커버리지 <span class="muted">· auction_sales</span></h1>
<p class="muted">전국 어느 지역·용도의 낙찰(낙찰가율) 데이터가 얼마나 있는지 봅니다. 값 = 낙찰가율 보유 건수(빨강=0건).</p>
<div class="kpi">
 <div>전체 행<b>{_fmt(total)}</b><span class="muted">auction_sales 전체</span></div>
 <div>낙찰가율 보유<b>{_fmt(with_rate)}</b><span class="muted">추정에 쓸 수 있는 건</span></div>
 <div>표본 기간<b style="font-size:15px">{(dmin or '—')} ~ {(dmax or '—')}</b><span class="muted">표본 {len(sample):,}건{' · 상한도달' if sample_capped else ''}</span></div>
</div>
<h2>시도 × 용도별 보유 건수 <span class="muted">(표본 {len(sample):,}건 기준 · 낙찰가율 보유분)</span></h2>
<table><thead><tr><th>시도</th>{col_head}<th>합계</th></tr></thead>
<tbody>{body_rows}</tbody></table>
<div class="warn">🔎 <b>아파트·연립다세대가 0건(빨강)인 시도</b>가 곧 추정이 '사례 없음'으로 빠지는 지역입니다{f': {", ".join(empty_sido[:12])}' if empty_sido else ' — 없음(대체로 채워져 있음)'}.</div>
<p class="muted" style="margin-top:16px">※ 매트릭스는 <b>최근 {SAMPLE_MAX:,}건 표본</b> 분포입니다(상한 도달 시 전체 아님). 전체 건수(위 KPI)는 정확한 count입니다.<br>
※ 용도: 아파트·연립다세대·오피스텔 = 주거용 집합건물. 단독·다가구는 집합건물 아님.</p>
</body></html>'''
    return Response(html, mimetype='text/html; charset=utf-8')


# ============================================================
# INFOCARE 시군구 통계 CSV 적재 — 집합건물 주거용만 추출해 auction_rate_stats 갱신
#   용도(집합건물): 다세대·연립→rh, 아파트·주상복합(주거)→apt, 오피스텔·오피스텔(주거)→offi
#   낙찰가율 = Σ총낙찰가/Σ총감정가(용도군 재집계), sample_n = Σ낙찰건수.
#   GET: 업로드 폼. POST: 파싱→미리보기(기본). commit=1 + key=ADMIN_SECRET 이면 실제 적재.
#   URL: /admin/import-rates
# ============================================================
_INFOCARE_MAP = {'아파트': 'apt', '주상복합(주거)': 'apt', '다세대': 'rh', '연립': 'rh',
                 '오피스텔': 'offi', '오피스텔(주거)': 'offi'}

# 파일명 시도 인식 → 저장 표준형(주소 파싱과 동일한 현행 공식명)
_SIDO_CANON = {
    '서울특별시': '서울특별시', '서울': '서울특별시',
    '부산광역시': '부산광역시', '부산': '부산광역시',
    '대구광역시': '대구광역시', '대구': '대구광역시',
    '인천광역시': '인천광역시', '인천': '인천광역시',
    '광주광역시': '광주광역시', '광주': '광주광역시',
    '대전광역시': '대전광역시', '대전': '대전광역시',
    '울산광역시': '울산광역시', '울산': '울산광역시',
    '세종특별자치시': '세종특별자치시', '세종': '세종특별자치시',
    '경기도': '경기도', '경기': '경기도',
    '강원특별자치도': '강원특별자치도', '강원도': '강원특별자치도', '강원': '강원특별자치도',
    '충청북도': '충청북도', '충북': '충청북도',
    '충청남도': '충청남도', '충남': '충청남도',
    '전북특별자치도': '전북특별자치도', '전라북도': '전북특별자치도', '전북': '전북특별자치도',
    '전라남도': '전라남도', '전남': '전라남도',
    '경상북도': '경상북도', '경북': '경상북도',
    '경상남도': '경상남도', '경남': '경상남도',
    '제주특별자치도': '제주특별자치도', '제주도': '제주특별자치도', '제주': '제주특별자치도',
}


def _detect_region_from_name(fname):
    """파일명에서 (시도표준형, 시군구) 추출. 못 찾으면 해당 값 None.
    예: '서울특별시 강남구 통계.csv' → ('서울특별시','강남구')."""
    import re as _re
    base = (fname or '').replace('\\', '/').rsplit('/', 1)[-1]
    base = _re.sub(r'\.csv$', '', base, flags=_re.I)
    sido = None
    rest = base
    for k in sorted(_SIDO_CANON, key=len, reverse=True):
        if k in base:
            sido = _SIDO_CANON[k]
            rest = base.replace(k, ' ')
            break
    # 앱의 주소 파싱(_parseRegion)은 시도 다음 '첫 시/군/구 토큰'을 시군구로 본다.
    # (예: '경기도 성남시 분당구' → '성남시'). 조회가 맞물리도록 동일 규칙으로 첫 토큰 채택.
    # {1,}?로 한 글자+구(중구·동구·서구·남구·북구)도 인식.
    toks = _re.findall(r'[가-힣]{1,}?[시군구]', rest)
    sgg = toks[0] if toks else None
    return sido, sgg


def _upsert_rate_stat(sido, sigungu, use_group, months, sample_n, rate, asof):
    """(sido,sigungu,use_group,period_months) 키로 select→update/insert. on_conflict 미가정."""
    rec = {'sido': sido, 'sigungu': sigungu, 'use_group': use_group,
           'period_months': months, 'sample_n': sample_n,
           'median_rate': rate, 'avg_rate': rate, 'asof': asof}
    ex = (supabase.table('auction_rate_stats').select('sido')
          .eq('sido', sido).eq('sigungu', sigungu)
          .eq('use_group', use_group).eq('period_months', months)
          .limit(1).execute().data)
    if ex:
        (supabase.table('auction_rate_stats').update(rec)
         .eq('sido', sido).eq('sigungu', sigungu)
         .eq('use_group', use_group).eq('period_months', months).execute())
        return '갱신'
    supabase.table('auction_rate_stats').insert(rec).execute()
    return '신규'


def _infocare_num(s):
    try:
        return float(str(s).replace(',', '').replace('"', '').strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_infocare_csv(raw_bytes):
    """INFOCARE 용도별 집계 CSV → 집합건물 주거용 6종 추출 → use_group 재집계.
    반환: (agg_list, picked_detail) / 오류 시 예외."""
    import csv as _csv
    import io as _io
    text = None
    for enc in ('utf-8-sig', 'cp949', 'euc-kr', 'utf-8'):
        try:
            text = raw_bytes.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        raise ValueError('인코딩 판별 실패 (UTF-8/CP949 아님)')
    reader = list(_csv.reader(_io.StringIO(text)))
    section, picked = None, {}
    for r in reader[1:]:
        if len(r) < 8:
            continue
        c0 = (r[0] or '').strip()
        c1 = (r[1] or '').strip()
        if c0:
            section = c0
        if section == '집합건물' and c1 in _INFOCARE_MAP:
            picked[c1] = {'appraisal': _infocare_num(r[2]), 'sale': _infocare_num(r[3]),
                          'rate': _infocare_num(r[4]), 'n_total': int(_infocare_num(r[5])),
                          'n_sold': int(_infocare_num(r[7]))}
    if not picked:
        raise ValueError("'집합건물' 구간에서 주거용 용도(다세대·아파트·연립·오피스텔 등)를 못 찾았습니다. 파일 형식을 확인하세요.")
    agg = {}
    for sub, d in picked.items():
        ug = _INFOCARE_MAP[sub]
        a = agg.setdefault(ug, {'appraisal': 0.0, 'sale': 0.0, 'n_sold': 0, 'n_total': 0, 'subs': []})
        a['appraisal'] += d['appraisal']; a['sale'] += d['sale']
        a['n_sold'] += d['n_sold']; a['n_total'] += d['n_total']; a['subs'].append(sub)
    out = []
    for ug, a in agg.items():
        rate = round(100.0 * a['sale'] / a['appraisal'], 2) if a['appraisal'] > 0 else None
        out.append({'use_group': ug, 'rate': rate, 'sample_n': a['n_sold'],
                    'n_total': a['n_total'], 'subs': ', '.join(a['subs'])})
    out.sort(key=lambda x: x['use_group'])
    return out, picked


@app.route('/admin/import-rates', methods=['GET', 'POST'])
def admin_import_rates():
    UG_LABEL = {'apt': '아파트(+주상복합주거)', 'rh': '연립·다세대', 'offi': '오피스텔'}
    form = ('<form method="post" enctype="multipart/form-data" '
            'style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:16px 18px;margin:12px 0;display:grid;gap:10px;max-width:520px">'
            '<label>시도 <input name="sido" placeholder="예: 서울특별시" required style="width:100%;padding:6px 8px"></label>'
            '<label>시군구 <input name="sigungu" placeholder="예: 강남구" required style="width:100%;padding:6px 8px"></label>'
            '<label>기간(개월) <input name="months" value="12" style="width:100%;padding:6px 8px"></label>'
            '<label>기준월(asof) <input name="asof" placeholder="예: 2026-06 (비우면 오늘)" style="width:100%;padding:6px 8px"></label>'
            '<label>INFOCARE 용도별 CSV <input type="file" name="file" accept=".csv" required></label>'
            '<label><input type="checkbox" name="commit" value="1"> 실제 적재(체크 안 하면 미리보기만)</label>'
            '<label>관리자 키(적재 시 필요) <input name="key" style="width:100%;padding:6px 8px"></label>'
            '<button type="submit" style="padding:9px;background:#0f766e;color:#fff;border:0;border-radius:6px;font-weight:700;cursor:pointer">파싱 / 적재</button>'
            '</form>')
    head = ('<!doctype html><meta charset="utf-8"><title>INFOCARE 낙찰가율 적재</title>'
            '<style>body{font-family:system-ui,"Malgun Gothic",sans-serif;max-width:760px;margin:24px auto;padding:0 16px;color:#1e293b;line-height:1.55}'
            'table{border-collapse:collapse;width:100%;font-size:14px;margin-top:8px}th,td{border:1px solid #e2e8f0;padding:6px 10px}'
            'th{background:#f8fafc}.muted{color:#64748b;font-size:13px}.ok{color:#166534;font-weight:700}.err{color:#b91c1c;font-weight:700}</style>'
            '<h1>INFOCARE 시군구 낙찰가율 적재</h1>'
            '<p class="muted">INFOCARE 용도별 통계 CSV를 올리면 <b>집합건물 주거용</b>(다세대·아파트·연립·오피스텔·주상복합주거)만 뽑아 '
            'use_group별로 재집계해 <code>auction_rate_stats</code>에 넣습니다. 먼저 <b>미리보기</b>로 확인 후 적재하세요.</p>')

    if request.method == 'GET':
        return Response(head + form, mimetype='text/html; charset=utf-8')

    if not supabase:
        return Response(head + form + '<p class="err">Supabase 미설정</p>', mimetype='text/html; charset=utf-8')
    sido = (request.form.get('sido') or '').strip()
    sigungu = (request.form.get('sigungu') or '').strip()
    months = int(_infocare_num(request.form.get('months') or 12)) or 12
    asof = (request.form.get('asof') or '').strip()
    commit = request.form.get('commit') == '1'
    key = request.form.get('key') or ''
    f = request.files.get('file')
    if not asof:
        import datetime as _dti
        asof = _dti.date.today().isoformat()
    if not (sido and sigungu and f):
        return Response(head + form + '<p class="err">시도·시군구·파일은 필수입니다.</p>', mimetype='text/html; charset=utf-8')
    try:
        agg, picked = _parse_infocare_csv(f.read())
    except Exception as e:
        return Response(head + form + f'<p class="err">파싱 실패: {e}</p>', mimetype='text/html; charset=utf-8')

    prev = ''.join(
        f'<tr><td>{UG_LABEL.get(x["use_group"], x["use_group"])} <span class="muted">({x["subs"]})</span></td>'
        f'<td style="text-align:right"><b>{x["rate"]}</b>%</td>'
        f'<td style="text-align:right">{x["sample_n"]}</td><td style="text-align:right">{x["n_total"]}</td></tr>'
        for x in agg)
    preview = (f'<h2>{sido} {sigungu} · 최근 {months}개월 · 기준 {asof}</h2>'
               '<table><thead><tr><th>use_group</th><th>낙찰가율</th><th>낙찰건수(sample_n)</th><th>총건수</th></tr></thead>'
               f'<tbody>{prev}</tbody></table>')

    if not commit:
        return Response(head + preview + '<p class="muted" style="margin-top:12px">☝️ 미리보기입니다. 값이 맞으면 아래에서 <b>실제 적재</b> 체크 + 관리자 키 입력 후 다시 올리세요.</p>' + form,
                        mimetype='text/html; charset=utf-8')

    if ADMIN_SECRET and key != ADMIN_SECRET:
        return Response(head + preview + '<p class="err">적재하려면 올바른 관리자 키가 필요합니다.</p>' + form, mimetype='text/html; charset=utf-8')

    # 적재: (sido,sigungu,use_group,period_months) 키로 select→update/insert (on_conflict 미가정)
    results = []
    for x in agg:
        if x['rate'] is None:
            continue
        rec = {'sido': sido, 'sigungu': sigungu, 'use_group': x['use_group'],
               'period_months': months, 'sample_n': x['sample_n'],
               'median_rate': x['rate'], 'avg_rate': x['rate'], 'asof': asof}
        try:
            ex = (supabase.table('auction_rate_stats').select('*')
                  .eq('sido', sido).eq('sigungu', sigungu)
                  .eq('use_group', x['use_group']).eq('period_months', months)
                  .limit(1).execute().data)
            if ex:
                (supabase.table('auction_rate_stats').update(rec)
                 .eq('sido', sido).eq('sigungu', sigungu)
                 .eq('use_group', x['use_group']).eq('period_months', months).execute())
                results.append(f'<tr><td>{x["use_group"]}</td><td class="ok">갱신</td><td>{x["rate"]}% (n={x["sample_n"]})</td></tr>')
            else:
                supabase.table('auction_rate_stats').insert(rec).execute()
                results.append(f'<tr><td>{x["use_group"]}</td><td class="ok">신규</td><td>{x["rate"]}% (n={x["sample_n"]})</td></tr>')
        except Exception as e:
            results.append(f'<tr><td>{x["use_group"]}</td><td class="err">실패</td><td>{e}</td></tr>')
    done = ('<h2>적재 결과</h2><table><thead><tr><th>use_group</th><th>결과</th><th>내용</th></tr></thead>'
            f'<tbody>{"".join(results)}</tbody></table>'
            '<p class="muted" style="margin-top:12px">완료. 앱에서 해당 시군구 물건을 열면 이 통계가 낙찰가율로 반영됩니다(개별 실측이 쌓이면 자동으로 그쪽 우선).</p>')
    return Response(head + preview + done + form, mimetype='text/html; charset=utf-8')


# ============================================================
# INFOCARE 대량 적재 — 여러 시군구 CSV를 한 번에. 파일명에서 시도·시군구 자동 인식.
#   ※ 반드시 '앱의 이 화면(브라우저)'에서 업로드해야 진짜 파일명이 전달됩니다.
#     (채팅 업로드는 파일명이 지워짐)  파일명 예: '서울특별시 강남구.csv'
#   GET: 폼. POST: 파일별 파싱+지역인식→미리보기(기본). commit=1+key로 전체 적재.
#   URL: /admin/import-rates-bulk
# ============================================================
@app.route('/admin/import-rates-bulk', methods=['GET', 'POST'])
def admin_import_rates_bulk():
    UG_LABEL = {'apt': '아파트', 'rh': '연립·다세대', 'offi': '오피스텔'}
    form = ('<form method="post" enctype="multipart/form-data" '
            'style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:16px 18px;margin:12px 0;display:grid;gap:10px;max-width:560px">'
            '<label>기간(개월) <input name="months" value="12" style="width:100%;padding:6px 8px"></label>'
            '<label>기준월(asof) <input name="asof" placeholder="예: 2026-06 (비우면 오늘)" style="width:100%;padding:6px 8px"></label>'
            '<label>CSV 여러 개 선택 <input type="file" name="files" accept=".csv" multiple required></label>'
            '<label><input type="checkbox" name="commit" value="1"> 전체 적재(체크 안 하면 미리보기만)</label>'
            '<label>관리자 키(적재 시 필요) <input name="key" style="width:100%;padding:6px 8px"></label>'
            '<button type="submit" style="padding:9px;background:#0f766e;color:#fff;border:0;border-radius:6px;font-weight:700;cursor:pointer">파싱 / 적재</button>'
            '</form>')
    head = ('<!doctype html><meta charset="utf-8"><title>INFOCARE 대량 적재</title>'
            '<style>body{font-family:system-ui,"Malgun Gothic",sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#1e293b;line-height:1.55}'
            'table{border-collapse:collapse;width:100%;font-size:13.5px;margin-top:8px}th,td{border:1px solid #e2e8f0;padding:6px 9px}'
            'th{background:#f8fafc}.muted{color:#64748b;font-size:13px}.ok{color:#166534;font-weight:700}.err{color:#b91c1c;font-weight:700}</style>'
            '<h1>INFOCARE 시군구 낙찰가율 — 대량 적재</h1>'
            '<p class="muted">여러 시군구 CSV를 한 번에 올립니다. <b>파일명에 시도+시군구</b>가 있어야 자동 인식됩니다(예: <code>서울특별시 강남구.csv</code>). '
            '⚠️ 반드시 <b>이 브라우저 화면</b>에서 올리세요(채팅에 올리면 파일명이 지워집니다).</p>')

    if request.method == 'GET':
        return Response(head + form, mimetype='text/html; charset=utf-8')
    if not supabase:
        return Response(head + form + '<p class="err">Supabase 미설정</p>', mimetype='text/html; charset=utf-8')

    months = int(_infocare_num(request.form.get('months') or 12)) or 12
    asof = (request.form.get('asof') or '').strip()
    commit = request.form.get('commit') == '1'
    key = request.form.get('key') or ''
    if not asof:
        import datetime as _dtb2
        asof = _dtb2.date.today().isoformat()
    files = request.files.getlist('files')
    if not files:
        return Response(head + form + '<p class="err">파일을 선택하세요.</p>', mimetype='text/html; charset=utf-8')
    if commit and ADMIN_SECRET and key != ADMIN_SECRET:
        return Response(head + form + '<p class="err">전체 적재하려면 올바른 관리자 키가 필요합니다. (미리보기는 키 없이 가능)</p>', mimetype='text/html; charset=utf-8')

    rows_html, ok_cnt, skip_cnt, write_cnt = [], 0, 0, 0
    for f in files:
        fname = f.filename or '(이름없음)'
        sido, sgg = _detect_region_from_name(fname)
        try:
            agg, _picked = _parse_infocare_csv(f.read())
        except Exception as e:
            skip_cnt += 1
            rows_html.append(f'<tr><td>{fname}</td><td class="err">파싱실패</td><td colspan="4">{e}</td></tr>')
            continue
        vals = {x['use_group']: x for x in agg}
        cells = ''.join(
            f'<td style="text-align:right">{(vals[u]["rate"] if u in vals else "—")}{"%" if u in vals else ""}'
            f'<div class="muted">n={vals[u]["sample_n"] if u in vals else 0}</div></td>'
            for u in ('apt', 'rh', 'offi'))
        if not (sido and sgg):
            skip_cnt += 1
            rows_html.append(f'<tr><td>{fname}</td><td class="err">지역인식실패</td>'
                             f'<td>{sido or "?"} {sgg or "?"}</td>{cells}</tr>')
            continue
        action = '미리보기'
        if commit:
            try:
                for x in agg:
                    if x['rate'] is not None:
                        _upsert_rate_stat(sido, sgg, x['use_group'], months, x['sample_n'], x['rate'], asof)
                        write_cnt += 1
                action = '<span class="ok">적재완료</span>'
            except Exception as e:
                action = f'<span class="err">적재실패: {e}</span>'
        ok_cnt += 1
        rows_html.append(f'<tr><td>{fname}</td><td>{action}</td><td><b>{sido} {sgg}</b></td>{cells}</tr>')

    summary = (f'<p style="margin-top:10px">인식 <b>{ok_cnt}</b>개 · 건너뜀 <b>{skip_cnt}</b>개'
               + (f' · <span class="ok">적재 {write_cnt}건</span>' if commit else ' · <b>미리보기</b>(적재 안 함)') + '</p>')
    tbl = ('<table><thead><tr><th>파일명</th><th>상태</th><th>인식지역</th>'
           '<th>아파트</th><th>연립·다세대</th><th>오피스텔</th></tr></thead>'
           f'<tbody>{"".join(rows_html)}</tbody></table>')
    tip = ('' if commit else '<p class="muted" style="margin-top:12px">☝️ 값·지역이 맞으면 <b>전체 적재</b> 체크 + 관리자 키 입력 후 같은 파일들을 다시 올리세요. '
           '지역인식 실패는 파일명에 <code>시도 시군구</code>를 넣어 다시 시도하세요.</p>')
    return Response(head + summary + tbl + tip + form, mimetype='text/html; charset=utf-8')


# ============================================================
# 적재된 지역 낙찰가율 통계 조회 — 구별로 값이 제대로 들어갔는지 확인
#   URL: /admin/rate-stats?sido=서울특별시&months=12  (읽기전용, 키 불필요)
# ============================================================
@app.route('/admin/rate-stats')
def admin_rate_stats():
    if not supabase:
        return jsonify({'error': 'Supabase 미설정'}), 503
    sido = (request.args.get('sido') or '서울특별시').strip()
    months = request.args.get('months', default=12, type=int)
    try:
        rows = (supabase.table('auction_rate_stats')
                .select('sido,sigungu,use_group,median_rate,sample_n,period_months,asof')
                .eq('sido', sido).eq('period_months', months)
                .limit(3000).execute().data) or []
    except Exception as e:
        return jsonify({'error': str(e)}), 502

    mat = {}
    for r in rows:
        sg = r.get('sigungu') or '(전체)'
        mat.setdefault(sg, {})[r.get('use_group')] = (r.get('median_rate'), r.get('sample_n'), r.get('asof'))
    order = sorted(mat)
    apt_vals = [mat[s].get('apt', (None,))[0] for s in order if 'apt' in mat[s]]
    all_same = len(apt_vals) >= 3 and len(set(apt_vals)) == 1

    def _cell(sg, ug):
        v = mat.get(sg, {}).get(ug)
        if not v or v[0] is None:
            return '<td style="text-align:right;color:#b91c1c">—</td>'
        return f'<td style="text-align:right">{v[0]}%<div class="muted">n={v[1]}</div></td>'

    body = ''.join(f'<tr><td><b>{sg}</b></td>' + ''.join(_cell(sg, u) for u in ('apt', 'rh', 'offi'))
                   + f'<td class="muted">{mat[sg].get("apt",(None,None,""))[2] or ""}</td></tr>' for sg in order)
    warn = ('<div style="margin:12px 0;padding:12px 16px;border-radius:8px;background:#fef2f2;border:1px solid #fecaca;color:#b91c1c;font-weight:600">'
            '⚠️ 모든 구의 아파트 낙찰가율이 동일합니다 — INFOCARE에서 <b>‘시군구전체’</b>로 받으셨을 가능성이 큽니다. '
            '구를 하나씩 바꿔가며 다시 받아 올려주세요.</div>' if all_same else
            ('<div style="margin:12px 0;padding:12px 16px;border-radius:8px;background:#f0fdf4;border:1px solid #bbf7d0;color:#166534;font-weight:600">'
             f'✅ 구별로 값이 다르게 들어갔습니다 ({len(order)}개 지역). 정상입니다.</div>' if order else ''))
    html = (f'<!doctype html><meta charset="utf-8"><title>적재된 낙찰가율</title>'
            '<style>body{font-family:system-ui,"Malgun Gothic",sans-serif;max-width:760px;margin:24px auto;padding:0 16px;color:#1e293b;line-height:1.5}'
            'table{border-collapse:collapse;width:100%;font-size:14px;margin-top:8px}th,td{border:1px solid #e2e8f0;padding:6px 10px}'
            'th{background:#f8fafc;text-align:right}th:first-child,td:first-child{text-align:left}.muted{color:#64748b;font-size:12px}</style>'
            f'<h1>적재된 낙찰가율 · {sido} <span class="muted">· 최근 {months}개월 · {len(order)}개 지역</span></h1>'
            f'{warn}'
            '<table><thead><tr><th>시군구</th><th>아파트</th><th>연립·다세대</th><th>오피스텔</th><th>기준월</th></tr></thead>'
            f'<tbody>{body or "<tr><td colspan=5>데이터 없음</td></tr>"}</tbody></table>'
            '<p class="muted" style="margin-top:14px">※ 다른 시도는 <code>?sido=경기도</code> 처럼 바꿔서 조회하세요.</p>')
    return Response(html, mimetype='text/html; charset=utf-8')


# ============================================================
# 자산 분석 리포트 PDF (매 페이지 CI 머릿말 — 서버사이드 reportlab)
# ============================================================
@app.route('/api/report/pdf', methods=['POST'])
def report_pdf():
    try:
        from pdf_report import build_report_pdf
    except Exception as e:
        return jsonify({'error': 'PDF 모듈 로드 실패: %s' % e}), 500
    try:
        data = request.get_json(force=True, silent=True) or {}
        pdf_bytes = build_report_pdf(data)
        resp = Response(pdf_bytes, mimetype='application/pdf')
        resp.headers['Content-Disposition'] = 'attachment; filename="asset_report.pdf"'
        resp.headers['Content-Length'] = str(len(pdf_bytes))
        return resp
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
        cols = ('kapt_code, kapt_name, bjd_code, sido, sigungu, dong, '
                'addr_road, addr_lot, total_units, total_dongs, completion_date')

        def _run(col):
            qy = supabase.table('apt_master').select(cols).ilike(col, f'%{q}%')
            if sido:
                qy = qy.eq('sido', sido)
            if sigungu:
                qy = qy.eq('sigungu', sigungu)
            return qy.limit(limit).execute().data or []

        # 1) 단지명 검색 → 2) 부족하면 도로명주소로도 검색해 병합 (단지명/도로명 둘 다 지원)
        items = _run('kapt_name')
        if len(items) < limit:
            seen = {it.get('kapt_code') for it in items}
            for it in _run('addr_road'):
                if it.get('kapt_code') not in seen:
                    items.append(it)
                    seen.add(it.get('kapt_code'))
                if len(items) >= limit:
                    break
        return jsonify({'count': len(items), 'items': items})
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
<h2>1⃣ 법정동 마스터 (약 20,000건)</h2>
<p class="muted">행정표준코드관리시스템 출처. 동/읍/면 + 리 단위 전체.</p>
<div class="row">
<button id="btn-dong" onclick="loadDong()">로드 시작</button>
<span id="dong-status" class="muted">대기 중</span>
</div>
<div class="bar-wrap"><div id="dong-bar" class="bar"></div></div>
<div id="dong-log" class="log" style="display:none;margin-top:12px;"></div>
</div>

<div class="card">
<h2>2⃣ 아파트 단지 마스터 (약 18,000개)</h2>
<p class="muted">⚠ 1번 완료 후 진행하세요. K-apt  API getTotalAptList3로 페이지당 1000개씩 일괄 조회 (약 2~5분 소요).</p>
<div class="row">
<button id="btn-apt" onclick="loadApt()" disabled>1번 먼저 완료</button>
<span id="apt-status" class="muted">대기 중</span>
</div>
<div class="bar-wrap"><div id="apt-bar" class="bar"></div></div>
<div id="apt-log" class="log" style="display:none;margin-top:12px;"></div>
</div>

<p class="muted" style="text-align:center;margin-top:20px;">⚠ 페이지를 닫지 마세요. 닫으면 진행이 멈춥니다 (다시 열면 이어서 진행됩니다).</p>
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
  const size = 1;  //  API에서는 1페이지(1000개)씩 처리
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
    """K-apt  API getTotalAptList3로 전국 단지 목록 일괄 적재.
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

        # 응답 파싱 ( API: XML/JSON 자동 감지)
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


@app.route('/npl')
def npl_analysis_page():
    """NPL 자산 분석 페이지: 단지 자동완성 + 12개월 실거래가 분석."""
    html = '''<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NPL 자산 분석 - 키움에프앤아이</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif; background: #f5f5f7; color: #1d1d1f; padding: 0; }
.container { max-width: 1200px; margin: 0 auto; padding: 20px; }

/* 헤더 */
header { background: linear-gradient(135deg, #0a3a6e 0%, #1056a6 100%); color: white; padding: 24px 0; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
header .container { display: flex; align-items: center; justify-content: space-between; padding: 0 20px; }
header h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.5px; }
header h1 .icon { margin-right: 8px; }
header a { color: white; text-decoration: none; padding: 8px 14px; background: rgba(255,255,255,0.15); border-radius: 8px; font-size: 14px; font-weight: 500; transition: 0.2s; }
header a:hover { background: rgba(255,255,255,0.25); }

/* 검색 카드 */
.search-card { background: white; border-radius: 16px; padding: 32px; margin-bottom: 24px; box-shadow: 0 2px 12px rgba(0,0,0,0.05); }
.search-card h2 { font-size: 18px; margin-bottom: 16px; color: #1d1d1f; }
.search-wrap { position: relative; }
.search-input { width: 100%; padding: 14px 18px; font-size: 16px; border: 2px solid #e8e8ed; border-radius: 10px; outline: none; transition: 0.2s; }
.search-input:focus { border-color: #0a3a6e; box-shadow: 0 0 0 4px rgba(10, 58, 110, 0.08); }
.search-hint { font-size: 13px; color: #6e6e73; margin-top: 8px; }

/* 자동완성 드롭다운 */
.autocomplete-list { position: absolute; top: 100%; left: 0; right: 0; background: white; border: 1px solid #e8e8ed; border-radius: 10px; max-height: 360px; overflow-y: auto; z-index: 100; margin-top: 4px; box-shadow: 0 4px 16px rgba(0,0,0,0.1); display: none; }
.autocomplete-list.show { display: block; }
.autocomplete-item { padding: 12px 16px; cursor: pointer; border-bottom: 1px solid #f5f5f7; transition: 0.15s; }
.autocomplete-item:last-child { border-bottom: 0; }
.autocomplete-item:hover, .autocomplete-item.active { background: #f0f4fa; }
.autocomplete-item .name { font-weight: 600; color: #1d1d1f; font-size: 15px; }
.autocomplete-item .addr { font-size: 13px; color: #6e6e73; margin-top: 3px; }
.autocomplete-item .badge { display: inline-block; background: #0a3a6e; color: white; font-size: 11px; padding: 2px 8px; border-radius: 10px; margin-right: 8px; vertical-align: middle; }
.autocomplete-item .badge.dong { background: #6e6e73; }

.autocomplete-section { padding: 8px 16px; font-size: 11px; font-weight: 700; color: #0a3a6e; background: #f0f4fa; text-transform: uppercase; letter-spacing: 0.5px; }

/* 결과 영역 */
.results { display: none; }
.results.show { display: block; }

/* 단지 헤더 */
.danji-header { background: white; border-radius: 16px; padding: 24px 32px; margin-bottom: 16px; box-shadow: 0 2px 12px rgba(0,0,0,0.05); }
.danji-header .name { font-size: 24px; font-weight: 700; color: #1d1d1f; margin-bottom: 6px; }
.danji-header .addr { font-size: 14px; color: #6e6e73; }
.danji-header .codes { display: flex; gap: 16px; margin-top: 12px; }
.danji-header .code-item { font-size: 12px; color: #6e6e73; padding: 4px 10px; background: #f5f5f7; border-radius: 6px; font-family: ui-monospace, monospace; }

/* 로딩 */
.loading-card { background: white; border-radius: 16px; padding: 40px; margin-bottom: 24px; text-align: center; box-shadow: 0 2px 12px rgba(0,0,0,0.05); }
.loading-card .spinner { display: inline-block; width: 48px; height: 48px; border: 4px solid #e8e8ed; border-top-color: #0a3a6e; border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.loading-card .msg { font-size: 16px; color: #1d1d1f; margin-top: 16px; font-weight: 500; }
.loading-card .sub { font-size: 13px; color: #6e6e73; margin-top: 6px; }

/* 요약 카드 그리드 */
.summary-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
.summary-card { background: white; border-radius: 14px; padding: 20px; box-shadow: 0 2px 12px rgba(0,0,0,0.05); }
.summary-card .label { font-size: 12px; color: #6e6e73; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
.summary-card .value { font-size: 28px; font-weight: 700; color: #0a3a6e; line-height: 1.2; }
.summary-card .sub { font-size: 12px; color: #6e6e73; margin-top: 4px; }
.summary-card.highlight { background: linear-gradient(135deg, #0a3a6e 0%, #1056a6 100%); color: white; }
.summary-card.highlight .label, .summary-card.highlight .sub { color: rgba(255,255,255,0.85); }
.summary-card.highlight .value { color: white; }

/* 차트 카드 */
.chart-card { background: white; border-radius: 16px; padding: 24px; margin-bottom: 24px; box-shadow: 0 2px 12px rgba(0,0,0,0.05); }
.chart-card h3 { font-size: 16px; font-weight: 700; margin-bottom: 16px; color: #1d1d1f; }
.chart-wrap { position: relative; height: 300px; }

/* 표 카드 */
.table-card { background: white; border-radius: 16px; padding: 24px; margin-bottom: 24px; box-shadow: 0 2px 12px rgba(0,0,0,0.05); overflow: hidden; }
.table-card h3 { font-size: 16px; font-weight: 700; margin-bottom: 16px; color: #1d1d1f; }
.table-controls { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
.table-controls button { background: #f5f5f7; color: #1d1d1f; border: 0; padding: 8px 14px; border-radius: 8px; font-size: 13px; cursor: pointer; font-weight: 500; transition: 0.2s; }
.table-controls button:hover { background: #e8e8ed; }
.table-controls button.active { background: #0a3a6e; color: white; }

table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead th { background: #f5f5f7; padding: 10px 12px; text-align: left; font-weight: 600; color: #1d1d1f; border-bottom: 2px solid #e8e8ed; cursor: pointer; user-select: none; }
thead th:hover { background: #e8e8ed; }
thead th.sorted-asc::after { content: ' ▲'; font-size: 10px; color: #0a3a6e; }
thead th.sorted-desc::after { content: ' ▼'; font-size: 10px; color: #0a3a6e; }
tbody td { padding: 10px 12px; border-bottom: 1px solid #f5f5f7; }
tbody tr:hover { background: #f9f9fb; }
tbody tr.target-jibun { background: #fff8e1; }
tbody tr.target-jibun:hover { background: #fff3c4; }
.badge-trade { background: #e8f0ff; color: #0a3a6e; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
.badge-rent { background: #e8f5e8; color: #0a8a3a; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
.badge-monthly { background: #fff3e0; color: #f57c00; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }

/* 평형별 분석 */
.area-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
.area-card { background: #f9f9fb; border-radius: 10px; padding: 14px; }
.area-card .area { font-size: 12px; color: #6e6e73; font-weight: 600; }
.area-card .price { font-size: 18px; font-weight: 700; color: #0a3a6e; margin: 4px 0; }
.area-card .meta { font-size: 11px; color: #6e6e73; }

/* NPL 추정 박스 */
.npl-box { background: linear-gradient(135deg, #fff8e1 0%, #ffe082 100%); border: 1px solid #ffca28; border-radius: 16px; padding: 24px 28px; margin-bottom: 24px; }
.npl-box h3 { font-size: 16px; color: #5d4037; margin-bottom: 12px; font-weight: 700; }
.npl-box .estimate { font-size: 32px; font-weight: 800; color: #5d4037; }
.npl-box .estimate-range { font-size: 14px; color: #6d4c41; margin-top: 4px; }
.npl-box .desc { font-size: 13px; color: #5d4037; margin-top: 12px; line-height: 1.5; }

/* 빈 상태 */
.empty-state { background: white; border-radius: 16px; padding: 60px 20px; text-align: center; box-shadow: 0 2px 12px rgba(0,0,0,0.05); }
.empty-state .icon { font-size: 64px; margin-bottom: 16px; }
.empty-state h3 { font-size: 18px; color: #1d1d1f; margin-bottom: 8px; }
.empty-state p { font-size: 14px; color: #6e6e73; }

/* 에러 박스 */
.error-box { background: #ffe8e8; border: 1px solid #ffc8c8; color: #c62828; padding: 16px; border-radius: 10px; margin-bottom: 16px; font-size: 14px; }

/* 반응형 */
@media (max-width: 768px) {
  .summary-grid { grid-template-columns: repeat(2, 1fr); }
  header h1 { font-size: 18px; }
  .container { padding: 16px; }
  .search-card, .danji-header, .chart-card, .table-card { padding: 16px; }
}
</style>
</head>
<body>

<header>
  <div class="container">
    <h1><span class="icon">🏢</span> NPL 자산 분석 시스템</h1>
    <a href="/">← 메인으로</a>
  </div>
</header>

<div class="container">

  <!-- 검색 카드 -->
  <div class="search-card">
    <h2>🔍 단지 검색 (자동완성)</h2>
    <div class="search-wrap">
      <input type="text" id="search-input" class="search-input" placeholder="단지명, 동(예: 방학동), 또는 시군구를 입력하세요..." autocomplete="off">
      <div id="autocomplete-list" class="autocomplete-list"></div>
    </div>
    <p class="search-hint">💡 단지명(예: "삼익세라믹"), 동 이름(예: "방학동"), 시군구(예: "도봉구") 등으로 검색 가능합니다.</p>
  </div>

  <!-- 빈 상태 -->
  <div id="empty-state" class="empty-state">
    <div class="icon">📊</div>
    <h3>NPL 담보 부동산을 분석해보세요</h3>
    <p>위 검색창에 단지명이나 주소를 입력하시면, 12개월 실거래가 데이터를 자동으로 분석합니다.</p>
  </div>

  <!-- 결과 영역 -->
  <div id="results" class="results">

    <!-- 단지 헤더 -->
    <div id="danji-header" class="danji-header"></div>

    <!-- 로딩 -->
    <div id="loading" class="loading-card" style="display:none;">
      <div class="spinner"></div>
      <div class="msg" id="loading-msg">실거래가 데이터 수집 중...</div>
      <div class="sub" id="loading-sub">12개월간의 매매 + 전월세 거래를 조회합니다 (약 30~60초)</div>
    </div>

    <!-- 에러 -->
    <div id="error-box" class="error-box" style="display:none;"></div>

    <!-- 분석 결과 -->
    <div id="analysis" style="display:none;">

      <!-- 지번 선택 박스 -->
      <div id="jibun-selector-card" class="search-card" style="display:none; margin-bottom:16px; background:linear-gradient(135deg, #fff8e1 0%, #fffde7 100%); border-left:4px solid #ffb300;">
        <h2 style="font-size:16px;">🎯 분석 대상 지번 선택</h2>
        <p class="search-hint" style="margin-top:4px;">⚠ 같은 단지명에 여러 지번이 있을 수 있어요. 사장님 부동산의 지번을 선택하면 <strong>해당 지번 거래만</strong> 분석합니다.</p>
        <select id="jibun-selector" style="width:100%; padding:12px 14px; font-size:14px; border:2px solid #ffb300; border-radius:8px; margin-top:12px; background:white; font-weight:500; cursor:pointer; outline:none;"></select>
        <div id="jibun-current" style="margin-top:8px; font-size:13px; color:#5d4037; font-weight:500;"></div>
      </div>

      <!-- 요약 카드 4개 -->
      <div class="summary-grid">
        <div class="summary-card">
          <div class="label">평균 매매가</div>
          <div class="value" id="avg-trade">-</div>
          <div class="sub" id="avg-trade-sub">-</div>
        </div>
        <div class="summary-card">
          <div class="label">평균 전세가</div>
          <div class="value" id="avg-rent">-</div>
          <div class="sub" id="avg-rent-sub">-</div>
        </div>
        <div class="summary-card">
          <div class="label">12개월 거래</div>
          <div class="value" id="total-count">-</div>
          <div class="sub" id="total-count-sub">-</div>
        </div>
        <div class="summary-card highlight">
          <div class="label">전세가율</div>
          <div class="value" id="rent-ratio">-</div>
          <div class="sub" id="rent-ratio-sub">매매 대비 전세 비율</div>
        </div>
      </div>

      <!-- NPL 회수 추정 -->
      <div class="npl-box">
        <h3>💰 NPL 회수 가능 금액 추정 (담보 평가)</h3>
        <div class="estimate" id="npl-estimate">-</div>
        <div class="estimate-range" id="npl-range">-</div>
        <div class="desc" id="npl-desc">-</div>
      </div>

      <!-- 시세 추이 차트 -->
      <div class="chart-card">
        <h3>📈 월별 매매가 추이</h3>
        <div class="chart-wrap">
          <canvas id="price-chart"></canvas>
        </div>
      </div>

      <!-- 평형별 분석 -->
      <div class="table-card">
        <h3>📐 평형별 시세 분석 — 매매</h3>
        <div id="area-grid-trade" class="area-grid"></div>
      </div>

      <div class="table-card">
        <h3>📐 평형별 시세 분석 — 전세</h3>
        <div id="area-grid-jeonse" class="area-grid"></div>
      </div>

      <div class="table-card">
        <h3>📐 평형별 시세 분석 — 월세 <span style="font-size:12px; color:#6e6e73; font-weight:400;">(보증금 / 월세금액)</span></h3>
        <div id="area-grid-wolse" class="area-grid"></div>
      </div>

      <!-- 거래 상세 내역 -->
      <div class="table-card">
        <h3>📋 거래 상세 내역</h3>
        <div class="table-controls">
          <button class="filter-btn active" data-filter="all">전체</button>
          <button class="filter-btn" data-filter="매매">매매</button>
          <button class="filter-btn" data-filter="전세">전세</button>
          <button class="filter-btn" data-filter="월세">월세</button>
          <button class="filter-btn" data-filter="target">대상 지번만</button>
        </div>
        <input type="text" id="jibun-filter" placeholder="지번 입력 시 필터링 (예: 274)" style="width:100%; padding:8px 12px; margin-bottom:12px; border:1px solid #e8e8ed; border-radius:6px; font-size:13px;">
        <div style="overflow-x:auto;">
          <table id="trans-table">
            <thead>
              <tr>
                <th data-sort="date">거래일</th>
                <th data-sort="type">유형</th>
                <th>단지</th>
                <th data-sort="jibun">지번</th>
                <th data-sort="building">동</th>
                <th data-sort="floor">층</th>
                <th data-sort="area">면적(㎡)</th>
                <th data-sort="price">보증금/매매가<br>(만원)</th>
                <th data-sort="monthly">월세<br>(만원)</th>
              </tr>
            </thead>
            <tbody id="trans-tbody"></tbody>
          </table>
        </div>
        <p class="search-hint" style="margin-top:12px;" id="table-info">-</p>
      </div>

    </div>

  </div>

</div>

<script>
// ============ 상태 ============
let currentDanji = null;
let allItems = [];
let priceChart = null;
let activeFilter = 'all';
let jibunFilter = '';
let sortColumn = 'date';
let sortDesc = true;

// ============ 자동완성 ============
const searchInput = document.getElementById('search-input');
const autocompleteList = document.getElementById('autocomplete-list');
let acTimeoutId = null;
let activeIdx = -1;
let acItems = [];

searchInput.addEventListener('input', (e) => {
  clearTimeout(acTimeoutId);
  const q = e.target.value.trim();
  if (q.length < 2) {
    hideAutocomplete();
    return;
  }
  acTimeoutId = setTimeout(() => doAutocomplete(q), 250);
});

searchInput.addEventListener('keydown', (e) => {
  if (!autocompleteList.classList.contains('show')) return;
  if (e.key === 'ArrowDown') { e.preventDefault(); navigateAc(1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); navigateAc(-1); }
  else if (e.key === 'Enter') { e.preventDefault(); selectAc(activeIdx); }
  else if (e.key === 'Escape') { hideAutocomplete(); }
});

document.addEventListener('click', (e) => {
  if (!searchInput.contains(e.target) && !autocompleteList.contains(e.target)) {
    hideAutocomplete();
  }
});

async function doAutocomplete(q) {
  try {
    const [aptRes, dongRes] = await Promise.all([
      fetch(`/api/search/apt?q=${encodeURIComponent(q)}`).then(r => r.json()),
      fetch(`/api/search/dong?q=${encodeURIComponent(q)}`).then(r => r.json())
    ]);
    
    const apts = (aptRes.items || []).slice(0, 8);
    const dongs = (dongRes.items || []).slice(0, 4);
    
    if (apts.length === 0 && dongs.length === 0) {
      autocompleteList.innerHTML = '<div style="padding:20px; text-align:center; color:#6e6e73;">검색 결과가 없습니다.</div>';
      autocompleteList.classList.add('show');
      return;
    }
    
    let html = '';
    acItems = [];
    
    if (apts.length > 0) {
      html += '<div class="autocomplete-section">단지 (' + apts.length + ')</div>';
      apts.forEach((apt) => {
        acItems.push({ type: 'apt', data: apt });
        const idx = acItems.length - 1;
        html += `<div class="autocomplete-item" data-idx="${idx}">
          <div class="name"><span class="badge">단지</span>${escapeHtml(apt.kapt_name)}</div>
          <div class="addr">${escapeHtml(apt.sido || '')} ${escapeHtml(apt.sigungu || '')} ${escapeHtml(apt.dong || '')}</div>
        </div>`;
      });
    }
    
    if (dongs.length > 0) {
      html += '<div class="autocomplete-section">법정동 (' + dongs.length + ')</div>';
      dongs.forEach((dong) => {
        acItems.push({ type: 'dong', data: dong });
        const idx = acItems.length - 1;
        html += `<div class="autocomplete-item" data-idx="${idx}">
          <div class="name"><span class="badge dong">동</span>${escapeHtml(dong.dong)}</div>
          <div class="addr">${escapeHtml(dong.sido)} ${escapeHtml(dong.sigungu)}</div>
        </div>`;
      });
    }
    
    autocompleteList.innerHTML = html;
    autocompleteList.classList.add('show');
    activeIdx = -1;
    
    // 클릭 핸들러
    autocompleteList.querySelectorAll('.autocomplete-item').forEach((el) => {
      el.addEventListener('click', () => {
        selectAc(parseInt(el.dataset.idx));
      });
    });
  } catch (err) {
    console.error('자동완성 오류:', err);
  }
}

function navigateAc(dir) {
  const items = autocompleteList.querySelectorAll('.autocomplete-item');
  if (items.length === 0) return;
  if (activeIdx >= 0) items[activeIdx].classList.remove('active');
  activeIdx += dir;
  if (activeIdx < 0) activeIdx = items.length - 1;
  if (activeIdx >= items.length) activeIdx = 0;
  items[activeIdx].classList.add('active');
  items[activeIdx].scrollIntoView({ block: 'nearest' });
}

function selectAc(idx) {
  if (idx < 0 || idx >= acItems.length) return;
  const item = acItems[idx];
  hideAutocomplete();
  if (item.type === 'apt') {
    selectDanji(item.data);
  } else {
    // 동 선택 시: 해당 시군구의 동에 단지 검색하도록 안내
    searchInput.value = item.data.dong + ' ';
    searchInput.focus();
    doAutocomplete(item.data.dong);
  }
}

function hideAutocomplete() {
  autocompleteList.classList.remove('show');
  activeIdx = -1;
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/[&<>"']/g, (m) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[m]));
}

// ============ 단지 선택 → 분석 ============
async function selectDanji(danji) {
  currentDanji = danji;
  searchInput.value = danji.kapt_name;
  
  document.getElementById('empty-state').style.display = 'none';
  document.getElementById('results').classList.add('show');
  document.getElementById('analysis').style.display = 'none';
  document.getElementById('error-box').style.display = 'none';
  document.getElementById('loading').style.display = 'block';
  
  // 단지 헤더
  document.getElementById('danji-header').innerHTML = `
    <div class="name">${escapeHtml(danji.kapt_name)}</div>
    <div class="addr">${escapeHtml(danji.sido || '')} ${escapeHtml(danji.sigungu || '')} ${escapeHtml(danji.dong || '')}</div>
    <div class="codes">
      <span class="code-item">단지 코드: ${escapeHtml(danji.kapt_code)}</span>
      <span class="code-item">법정동 코드: ${escapeHtml(danji.bjd_code || '-')}</span>
    </div>
  `;
  
  // 12개월 데이터 조회
  const lawd_cd = (danji.bjd_code || '').substring(0, 5);
  if (!lawd_cd) {
    showError('법정동 코드가 없어 실거래가 조회 불가.');
    return;
  }
  
  // 단지명에서 핵심 키워드 추출 (위치 prefix 제거)
  // 예: "방학삼익세라믹" + dong="방학동" → "삼익세라믹"
  let searchKeyword = danji.kapt_name;
  if (danji.dong) {
    const dongStripped = danji.dong.replace(/(동|읍|면|리|가)$/, '');
    if (dongStripped && searchKeyword.startsWith(dongStripped) && searchKeyword.length > dongStripped.length + 2) {
      searchKeyword = searchKeyword.substring(dongStripped.length).trim();
    }
  }
  // 시군구 prefix도 제거 시도
  if (danji.sigungu) {
    const sgStripped = danji.sigungu.replace(/(시|군|구)$/, '').replace(/\s/g, '');
    if (sgStripped && searchKeyword.startsWith(sgStripped) && searchKeyword.length > sgStripped.length + 2) {
      searchKeyword = searchKeyword.substring(sgStripped.length).trim();
    }
  }
  
  console.log('[NPL] 검색 키워드:', searchKeyword, '(원래 단지명:', danji.kapt_name, ')');
  
  // 로딩 메시지 카운터
  let elapsed = 0;
  const loadingTimer = setInterval(() => {
    elapsed += 1;
    document.getElementById('loading-sub').textContent = 
      `12개월간의 매매 + 전월세 거래를 조회 중 (${elapsed}초 경과, 30~60초 예상)`;
  }, 1000);
  
  try {
    // 1차 시도: 단지명 필터 적용
    let url = `/api/transactions/bulk?lawd_cd=${lawd_cd}&months=12&danji_name=${encodeURIComponent(searchKeyword)}`;
    let res = await fetch(url);
    if (!res.ok) throw new Error('서버 오류 ' + res.status);
    let data = await res.json();
    if (data.error) { showError('조회 실패: ' + data.error); clearInterval(loadingTimer); return; }
    
    let items = data.items || [];
    let fallbackUsed = false;
    
    // Fallback: 단지명 매칭 0건이면 필터 없이 재시도 + 클라이언트 측 매칭
    if (items.length === 0) {
      document.getElementById('loading-msg').textContent = '단지명 자동매칭 실패, 시군구 전체 데이터로 재시도 중...';
      document.getElementById('loading-sub').textContent = '거의 다 됐어요. 추가 30~60초 소요.';
      
      url = `/api/transactions/bulk?lawd_cd=${lawd_cd}&months=12`;
      res = await fetch(url);
      if (!res.ok) throw new Error('서버 오류 ' + res.status);
      data = await res.json();
      if (data.error) { showError('조회 실패: ' + data.error); clearInterval(loadingTimer); return; }
      
      const rawItems = data.items || [];
      
      // 클라이언트 측 단지명 매칭 (양방향 + 부분 매칭)
      const kw = (searchKeyword || '').replace(/\s/g, '').toLowerCase();
      // 키워드의 핵심 부분 (3-4글자) 추출 시도
      const kwSuffix = kw.length >= 4 ? kw.slice(-4) : kw;
      const kwPrefix = kw.length >= 4 ? kw.slice(0, 4) : kw;
      
      items = rawItems.filter(x => {
        const n = (x.name || '').replace(/\s/g, '').toLowerCase();
        if (!n) return false;
        // 양방향 부분 매칭
        if (n.includes(kw) || kw.includes(n)) return true;
        // 마지막 4글자 매칭 (브랜드명)
        if (kwSuffix.length >= 3 && n.includes(kwSuffix)) return true;
        // 첫 4글자 매칭
        if (kwPrefix.length >= 3 && n.includes(kwPrefix)) return true;
        return false;
      });
      
      fallbackUsed = true;
      console.log(`[NPL] Fallback 매칭: ${rawItems.length}건 중 ${items.length}건 매칭 (키워드: ${kw})`);
    }
    
    clearInterval(loadingTimer);
    document.getElementById('loading').style.display = 'none';
    
    allItems = items;
    
    if (allItems.length === 0) {
      showError(fallbackUsed 
        ? '시군구 전체 데이터에서도 매칭되는 단지가 없습니다. 단지명을 확인하시거나, 메인 시군구 코드(' + lawd_cd + ')가 맞는지 확인해주세요.'
        : '해당 단지의 12개월 거래 내역이 없습니다. 거래가 적은 단지일 수 있습니다.');
      return;
    }
    
    // Fallback 사용 시 사용자에게 알림
    const oldBanner = document.getElementById('fallback-banner');
    if (oldBanner) oldBanner.remove();
    if (fallbackUsed) {
      const banner = document.createElement('div');
      banner.id = 'fallback-banner';
      banner.className = 'error-box';
      banner.style.background = '#fff3e0';
      banner.style.borderColor = '#ffcc80';
      banner.style.color = '#bf6900';
      banner.innerHTML = `ℹ 단지명 자동매칭으로 시군구(${lawd_cd}) 전체에서 ${items.length}건 추출 (검색 키워드: <strong>${escapeHtml(searchKeyword)}</strong>). 정확도가 낮을 수 있으니, 거래 표 하단의 <strong>지번 입력</strong>으로 사장님 단지를 좁혀주세요.`;
      document.getElementById('analysis').insertBefore(banner, document.getElementById('analysis').firstChild);
    }
    
    // 분석
    analyzeAndRender();
    document.getElementById('analysis').style.display = 'block';
    
  } catch (err) {
    clearInterval(loadingTimer);
    showError('네트워크 오류: ' + err.message);
  }
}

function showError(msg) {
  document.getElementById('loading').style.display = 'none';
  const eb = document.getElementById('error-box');
  eb.textContent = '⚠ ' + msg;
  eb.style.display = 'block';
}

// ============ 분석 ============
let selectedJibunKey = 'ALL';  // 'ALL' 또는 '동|지번'

function normalizeJibun(jibun) {
  if (!jibun) return '';
  // 하이픈 앞부분만 메인 번지로 (예: "274-1" → "274")
  return String(jibun).split('-')[0].trim();
}

function analyzeAndRender() {
  setupJibunSelector();
  renderAnalysis();
}

function setupJibunSelector() {
  // 전체 거래에서 동+메인지번으로 그룹화
  const groups = {};
  allItems.forEach(x => {
    const dong = x.dong || '(미상)';
    const jibun = normalizeJibun(x.jibun) || '(미상)';
    const key = `${dong}|${jibun}`;
    if (!groups[key]) {
      groups[key] = { dong, jibun, items: [], trade: 0, rent: 0 };
    }
    groups[key].items.push(x);
    if (x.type === '매매') groups[key].trade++;
    else groups[key].rent++;
  });
  
  // 거래 많은 순으로 정렬
  const sorted = Object.entries(groups)
    .map(([k, v]) => ({ key: k, ...v }))
    .sort((a, b) => b.items.length - a.items.length);
  
  const select = document.getElementById('jibun-selector');
  let html = '';
  sorted.forEach((g, i) => {
    const star = (i === 0 && g.trade > 0) ? ' ⭐ 가장 활발' : '';
    html += `<option value="${escapeHtml(g.key)}">${escapeHtml(g.dong)} ${escapeHtml(g.jibun)}번지${star} — 총 ${g.items.length}건 (매매 ${g.trade} / 전월세 ${g.rent})</option>`;
  });
  html += `<option value="ALL">─ 전체 보기 (${allItems.length}건, 다른 지번 단지 모두 포함) ─</option>`;
  select.innerHTML = html;
  
  // 자동 추천: 가장 거래 많은 지번
  if (sorted.length > 0 && sorted[0].items.length > 0) {
    selectedJibunKey = sorted[0].key;
    select.value = selectedJibunKey;
  } else {
    selectedJibunKey = 'ALL';
    select.value = 'ALL';
  }
  
  // 지번이 여러 개면 선택 박스 표시, 1개면 자동 적용만
  document.getElementById('jibun-selector-card').style.display = 
    sorted.length > 1 ? 'block' : 'none';
  
  // 이벤트
  select.onchange = (e) => {
    selectedJibunKey = e.target.value;
    renderAnalysis();
  };
}

function getFilteredItems() {
  if (selectedJibunKey === 'ALL') return allItems.slice();
  const [dong, jibun] = selectedJibunKey.split('|');
  return allItems.filter(x => {
    const d = x.dong || '(미상)';
    const j = normalizeJibun(x.jibun) || '(미상)';
    return d === dong && j === jibun;
  });
}

function renderAnalysis() {
  // 선택된 지번의 거래만 추출
  const items = getFilteredItems();
  
  const trades = items.filter(x => x.type === '매매');
  const jeonse = items.filter(x => x.type === '전세');
  const wolse = items.filter(x => x.type === '월세');
  
  // 유효한 매매 (해제되지 않은 것)
  const validTrades = trades.filter(x => !x.memo || !x.memo.includes('해제'));
  
  // 평균 매매가
  const avgTrade = validTrades.length > 0 
    ? validTrades.reduce((s, x) => s + x.price, 0) / validTrades.length 
    : 0;
  
  // 평균 전세가
  const avgJeonse = jeonse.length > 0 
    ? jeonse.reduce((s, x) => s + x.price, 0) / jeonse.length 
    : 0;
  
  // 전세가율
  const rentRatio = (avgTrade > 0 && avgJeonse > 0) 
    ? (avgJeonse / avgTrade * 100) 
    : 0;
  
  // 현재 선택 지번 정보 표시
  const jibunInfo = document.getElementById('jibun-current');
  if (selectedJibunKey === 'ALL') {
    jibunInfo.innerHTML = `📍 <strong>전체 지번</strong> 분석 중 (총 ${items.length}건)`;
  } else {
    const [dong, jibun] = selectedJibunKey.split('|');
    jibunInfo.innerHTML = `📍 <strong>${escapeHtml(dong)} ${escapeHtml(jibun)}번지</strong>만 분석 중 (${items.length}건)`;
  }
  
  // 요약 카드 업데이트
  document.getElementById('avg-trade').textContent = avgTrade > 0 ? formatPrice(avgTrade) : '-';
  document.getElementById('avg-trade-sub').textContent = `${validTrades.length}건 평균`;
  document.getElementById('avg-rent').textContent = avgJeonse > 0 ? formatPrice(avgJeonse) : '-';
  document.getElementById('avg-rent-sub').textContent = `${jeonse.length}건 평균`;
  document.getElementById('total-count').textContent = items.length + '건';
  document.getElementById('total-count-sub').textContent = 
    `매매 ${trades.length} / 전세 ${jeonse.length} / 월세 ${wolse.length}`;
  document.getElementById('rent-ratio').textContent = rentRatio > 0 ? rentRatio.toFixed(1) + '%' : '-';
  document.getElementById('rent-ratio-sub').textContent = avgTrade > 0 && avgJeonse > 0 
    ? '매매 대비 전세 비율' : '데이터 부족';
  
  // NPL 회수 추정
  if (avgTrade > 0) {
    const lower = avgTrade * 0.85;  // 보수적 회수율 85%
    const upper = avgTrade * 0.95;  // 적극적 회수율 95%
    const median = avgTrade * 0.90;
    document.getElementById('npl-estimate').textContent = formatPrice(median);
    document.getElementById('npl-range').textContent = 
      `회수 가능 범위: ${formatPrice(lower)} ~ ${formatPrice(upper)}`;
    let descParts = [];
    descParts.push(`📌 평균 매매가 ${formatPrice(avgTrade)}의 85~95%로 추정 (시장 변동성, 처분 비용 반영).`);
    if (validTrades.length < 5) descParts.push('⚠ 거래 건수가 적어(' + validTrades.length + '건) 추정 신뢰도 낮음. 추가 검토 필요.');
    if (avgJeonse > 0 && rentRatio > 0) descParts.push(`💰 전세 보증금 회수 시 약 ${formatPrice(avgJeonse)} 확보 가능.`);
    document.getElementById('npl-desc').innerHTML = descParts.join('<br>');
  } else {
    document.getElementById('npl-estimate').textContent = '추정 불가';
    document.getElementById('npl-range').textContent = '매매 거래 데이터 부족';
    document.getElementById('npl-desc').textContent = '12개월간 유효한 매매 거래가 없어 회수 금액 추정이 불가합니다.';
  }
  
  // 차트 (매매만)
  renderChart(validTrades);
  
  // 평형별 분석 (매매/전세/월세 모두)
  renderAreaAnalysis(items);
  
  // 거래 내역 표
  renderTable();
}

function formatPrice(price) {
  // price는 만원 단위
  if (price >= 10000) {
    const eok = Math.floor(price / 10000);
    const man = Math.round(price % 10000);
    return man > 0 ? `${eok}억 ${man.toLocaleString()}만` : `${eok}억`;
  }
  return Math.round(price).toLocaleString() + '만';
}

// ============ 차트 ============
function renderChart(trades) {
  // 월별 그룹화
  const monthly = {};
  trades.forEach(t => {
    const ym = t.date ? t.date.substring(0, 7) : '';  // YYYY-MM
    if (!monthly[ym]) monthly[ym] = [];
    monthly[ym].push(t.price);
  });
  
  const sortedMonths = Object.keys(monthly).sort();
  const labels = sortedMonths;
  const avgs = sortedMonths.map(m => {
    const arr = monthly[m];
    return arr.reduce((a,b) => a+b, 0) / arr.length;
  });
  const counts = sortedMonths.map(m => monthly[m].length);
  
  if (priceChart) priceChart.destroy();
  const ctx = document.getElementById('price-chart').getContext('2d');
  priceChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        {
          label: '월별 평균 매매가 (만원)',
          data: avgs,
          borderColor: '#0a3a6e',
          backgroundColor: 'rgba(10, 58, 110, 0.1)',
          tension: 0.3,
          fill: true,
          yAxisID: 'y',
        },
        {
          label: '거래 건수',
          data: counts,
          borderColor: '#f57c00',
          backgroundColor: 'rgba(245, 124, 0, 0.1)',
          tension: 0.3,
          type: 'bar',
          yAxisID: 'y1',
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        y: {
          beginAtZero: false,
          position: 'left',
          title: { display: true, text: '가격 (만원)' },
          ticks: { callback: (v) => (v/10000).toFixed(1) + '억' }
        },
        y1: {
          beginAtZero: true,
          position: 'right',
          title: { display: true, text: '거래 건수' },
          grid: { drawOnChartArea: false },
          ticks: { stepSize: 1 }
        }
      },
      plugins: {
        tooltip: {
          callbacks: {
            label: (ctx) => {
              if (ctx.dataset.label.includes('가격') || ctx.dataset.label.includes('매매')) {
                return ctx.dataset.label + ': ' + formatPrice(ctx.parsed.y);
              }
              return ctx.dataset.label + ': ' + ctx.parsed.y + '건';
            }
          }
        }
      }
    }
  });
}

// ============ 평형별 분석 ============
function renderAreaAnalysis(items) {
  // 매매/전세/월세 분리
  const trades = items.filter(x => x.type === '매매' && (!x.memo || !x.memo.includes('해제')));
  const jeonse = items.filter(x => x.type === '전세');
  const wolse = items.filter(x => x.type === '월세');
  
  renderAreaGridSimple('area-grid-trade', trades, '매매');
  renderAreaGridSimple('area-grid-jeonse', jeonse, '전세');
  renderAreaGridWolse('area-grid-wolse', wolse);
}

function groupByArea(items) {
  // 5㎡ 단위 그룹화
  const groups = {};
  items.forEach(t => {
    if (!t.area) return;
    const bucket = Math.floor(t.area / 5) * 5;
    const key = `${bucket}~${bucket+5}㎡`;
    if (!groups[key]) groups[key] = [];
    groups[key].push(t);
  });
  return groups;
}

function renderAreaGridSimple(elemId, items, label) {
  // 매매/전세용 - 단일 가격
  const grid = document.getElementById(elemId);
  const groups = groupByArea(items);
  const sortedKeys = Object.keys(groups).sort((a,b) => parseInt(a) - parseInt(b));
  
  if (sortedKeys.length === 0) {
    grid.innerHTML = `<div style="padding:20px; color:#6e6e73; text-align:center; grid-column: 1 / -1;">${label} 거래 데이터가 없습니다.</div>`;
    return;
  }
  
  grid.innerHTML = sortedKeys.map(k => {
    const arr = groups[k];
    const avg = arr.reduce((s,x) => s+x.price, 0) / arr.length;
    const min = Math.min(...arr.map(x => x.price));
    const max = Math.max(...arr.map(x => x.price));
    return `<div class="area-card">
      <div class="area">${k}</div>
      <div class="price">${formatPrice(avg)}</div>
      <div class="meta">${arr.length}건 / 최저 ${formatPrice(min)} ~ 최고 ${formatPrice(max)}</div>
    </div>`;
  }).join('');
}

function renderAreaGridWolse(elemId, items) {
  // 월세용 - 보증금 + 월세금액 모두 표시
  const grid = document.getElementById(elemId);
  const groups = groupByArea(items);
  const sortedKeys = Object.keys(groups).sort((a,b) => parseInt(a) - parseInt(b));
  
  if (sortedKeys.length === 0) {
    grid.innerHTML = '<div style="padding:20px; color:#6e6e73; text-align:center; grid-column: 1 / -1;">월세 거래 데이터가 없습니다.</div>';
    return;
  }
  
  grid.innerHTML = sortedKeys.map(k => {
    const arr = groups[k];
    // 보증금 평균
    const avgDeposit = arr.reduce((s,x) => s + (x.price || 0), 0) / arr.length;
    // 월세금액 평균 (monthly > 0인 것만)
    const monthlyArr = arr.filter(x => x.monthly && x.monthly > 0);
    const avgMonthly = monthlyArr.length > 0 
      ? monthlyArr.reduce((s,x) => s + x.monthly, 0) / monthlyArr.length 
      : 0;
    const minDeposit = Math.min(...arr.map(x => x.price || 0));
    const maxDeposit = Math.max(...arr.map(x => x.price || 0));
    return `<div class="area-card">
      <div class="area">${k}</div>
      <div class="price" style="font-size:16px;">
        보증 ${formatPrice(avgDeposit)}
        <div style="font-size:14px; color:#f57c00; margin-top:2px;">월 ${Math.round(avgMonthly).toLocaleString()}만</div>
      </div>
      <div class="meta">${arr.length}건 / 보증 ${formatPrice(minDeposit)} ~ ${formatPrice(maxDeposit)}</div>
    </div>`;
  }).join('');
}

// ============ 거래 내역 표 ============
function renderTable() {
  // 지번 선택이 적용된 거래만 표에 표시
  let items = getFilteredItems();
  
  // 필터 (매매/전세/월세 등)
  if (activeFilter === 'target' && jibunFilter) {
    items = items.filter(x => x.jibun === jibunFilter);
  } else if (activeFilter !== 'all' && activeFilter !== 'target') {
    items = items.filter(x => x.type === activeFilter);
  }
  if (jibunFilter && activeFilter !== 'target') {
    items = items.filter(x => x.jibun && x.jibun.includes(jibunFilter));
  }
  
  // 정렬
  items.sort((a, b) => {
    let av = a[sortColumn], bv = b[sortColumn];
    if (av == null) av = '';
    if (bv == null) bv = '';
    if (typeof av === 'number' && typeof bv === 'number') {
      return sortDesc ? bv - av : av - bv;
    }
    return sortDesc 
      ? String(bv).localeCompare(String(av)) 
      : String(av).localeCompare(String(bv));
  });
  
  // 헤더 정렬 표시
  document.querySelectorAll('thead th').forEach(th => {
    th.classList.remove('sorted-asc', 'sorted-desc');
    if (th.dataset.sort === sortColumn) {
      th.classList.add(sortDesc ? 'sorted-desc' : 'sorted-asc');
    }
  });
  
  const tbody = document.getElementById('trans-tbody');
  tbody.innerHTML = items.slice(0, 200).map(x => {
    const typeBadge = x.type === '매매' 
      ? `<span class="badge-trade">매매</span>`
      : x.type === '전세' 
      ? `<span class="badge-rent">전세</span>`
      : `<span class="badge-monthly">${escapeHtml(x.type)}</span>`;
    const isTarget = jibunFilter && x.jibun === jibunFilter;
    // 월세 컬럼: 월세 거래에만 값 표시
    const monthlyCell = (x.type === '월세' && x.monthly != null && x.monthly > 0)
      ? `<strong style="color:#f57c00;">${x.monthly.toLocaleString()}</strong>`
      : '<span style="color:#c0c0c0;">-</span>';
    return `<tr class="${isTarget ? 'target-jibun' : ''}">
      <td>${escapeHtml(x.date || '-')}</td>
      <td>${typeBadge}</td>
      <td>${escapeHtml(x.name || '-')}</td>
      <td>${escapeHtml(x.jibun || '-')}</td>
      <td>${escapeHtml(x.building || '-')}</td>
      <td>${x.floor != null ? x.floor + '층' : '-'}</td>
      <td>${x.area != null ? x.area.toFixed(2) : '-'}</td>
      <td><strong>${x.price != null ? x.price.toLocaleString() : '-'}</strong></td>
      <td>${monthlyCell}</td>
    </tr>`;
  }).join('');
  
  document.getElementById('table-info').textContent = 
    `총 ${items.length}건 중 ${Math.min(200, items.length)}건 표시. (정렬: ${sortColumn}, ${sortDesc ? '내림차순' : '오름차순'})`;
}

// 필터 버튼
document.querySelectorAll('.filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activeFilter = btn.dataset.filter;
    renderTable();
  });
});

// 지번 필터
document.getElementById('jibun-filter').addEventListener('input', (e) => {
  jibunFilter = e.target.value.trim();
  renderTable();
});

// 정렬
document.querySelectorAll('thead th[data-sort]').forEach(th => {
  th.addEventListener('click', () => {
    const col = th.dataset.sort;
    if (sortColumn === col) {
      sortDesc = !sortDesc;
    } else {
      sortColumn = col;
      sortDesc = true;
    }
    renderTable();
  });
});

</script>

</body>
</html>'''
    return html


@app.route('/admin/diag-expose')
def admin_diag_expose():
    """v2.12: 전유공용면적 / 공시가격 raw 진단 페이지.
    
    URL: /admin/diag-expose?key=ADMIN_SECRET&kapt=A14322001&dong=103&ho=802
    
    단지코드+동·호수만 입력하면 두 API의 raw 응답에서
    실제 dongNm/hoNm/exposPubuseGbCdNm 값들을 보여줍니다.
    """
    key = request.args.get('key', '')
    if not ADMIN_SECRET:
        return jsonify({'error': 'ADMIN_SECRET 환경변수가 설정되지 않았습니다.'}), 503
    if key != ADMIN_SECRET:
        return jsonify({'error': '잘못된 관리자 키'}), 403
    
    kapt_code = request.args.get('kapt', '').strip()
    dong_filter = request.args.get('dong', '').strip()
    ho_filter = request.args.get('ho', '').strip()
    
    if not kapt_code:
        return jsonify({'error': 'kapt 파라미터 필수 (예: ?kapt=A14322001&dong=103&ho=802)'}), 400
    
    try:
        # 단지정보로 sigungu/bjdong/bun/ji 추출
        basis_xml = fetch_apt_basis_cached(kapt_code, cache_ts())
        basis_items, err = parse_kapt_response(basis_xml)
        if err or not basis_items:
            return jsonify({'error': f'단지정보 조회 실패: {err}'}), 502
        b = basis_items[0]
        bjd_code = safe_get(b, 'bjdCode')
        sigungu_cd = bjd_code[:5]
        bjdong_cd = bjd_code[5:]
        addr_lot = safe_get(b, 'kaptAddr')
        m = re.search(r'(\d+)(?:-(\d+))?(?:\s|$)', addr_lot)
        bun = m.group(1) if m else ''
        ji = m.group(2) if (m and m.group(2)) else '0'
        
        # 전유공용면적 + 공시가격 모두 페이지네이션으로 가져옴
        expose_items, expose_err = fetch_br_expose_all_pages(sigungu_cd, bjdong_cd, '0', bun, ji)
        price_items, price_err = fetch_br_price_all_pages(sigungu_cd, bjdong_cd, '0', bun, ji)
        
        # 동·호 매칭 필터
        def norm_d(s):
            s = str(s).replace(' ', '').replace('동', '')
            s = re.sub(r'^[A-Za-z]+', '', s)
            return str(int(s)) if s.isdigit() else s
        def norm_h(s):
            s = str(s).replace(' ', '').replace('호', '')
            s = re.sub(r'^[A-Za-z]+', '', s)
            return str(int(s)) if s.isdigit() else s
        
        dt = norm_d(dong_filter) if dong_filter else None
        ht = norm_h(ho_filter) if ho_filter else None
        
        # 전유공용면적 - 매칭된 행 전체 raw 데이터
        expose_matched = []
        if dt and ht:
            for x in expose_items:
                if norm_d(safe_get(x, 'dongNm')) == dt and norm_h(safe_get(x, 'hoNm')) == ht:
                    expose_matched.append(x)
        
        # 공시가격 - 같은 동의 모든 호수 list (호수 형식 진단용)
        price_same_dong = []
        if dt:
            seen = set()
            for x in price_items:
                d = safe_get(x, 'dongNm')
                h = safe_get(x, 'hoNm')
                if norm_d(d) == dt and h not in seen:
                    seen.add(h)
                    price_same_dong.append({'dongNm': d, 'hoNm': h, 'price': safe_get(x, 'bldRgstPc')})
        
        return jsonify({
            'lookup_params': {
                'kapt_code': kapt_code,
                'sigungu_cd': sigungu_cd,
                'bjdong_cd': bjdong_cd,
                'bun': bun,
                'ji': ji,
                'addr': addr_lot,
                'dong_filter': dong_filter,
                'ho_filter': ho_filter,
                'dong_target': dt,
                'ho_target': ht,
            },
            'expose': {
                'total_count': len(expose_items),
                'matched_rows_full_raw': expose_matched,  # 전유/공용 구분 진단용
                'error': expose_err,
            },
            'price': {
                'total_count': len(price_items),
                'same_dong_hos': price_same_dong[:50],  # 호수 형식 진단용
                'first_5_rows': price_items[:5],
                'error': price_err,
            },
        })
    except Exception as e:
        return jsonify({'error': safe_error('진단 오류', e)}), 500


@app.route('/api/admin/diag-kapt')
def admin_diag_kapt():
    """K-apt  API 응답을 raw 그대로 반환 (진단용).
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
# ============================================================
# Phase 4: Supabase 동기화 API (v2.15) - 본건 데이터를 Supabase에 저장/복원
# ============================================================
# 목적: localStorage의 한계(브라우저별 격리) 극복, 다른 PC/모바일에서도 같은 데이터 접근
# URL 파라미터 user=kiwoom-team 처럼 지정하면 팀 공유 모드, 미지정 시 자동 생성된 개인 UUID
# ============================================================

@app.route('/api/state', methods=['GET'])
def api_get_state():
    """저장된 사용자 상태를 Supabase에서 가져옴."""
    user_id = (request.args.get('user_id') or '').strip()
    if not user_id:
        return jsonify({'error': 'user_id required'}), 400
    if not supabase:
        return jsonify({'error': 'supabase unavailable', 'state': None}), 503
    try:
        r = supabase.table('user_states').select('state, updated_at').eq('user_id', user_id).execute()
        if r.data and len(r.data) > 0:
            return jsonify({
                'state': r.data[0].get('state') or None,
                'updated_at': r.data[0].get('updated_at'),
                'user_id': user_id,
            })
        # 신규 사용자 - 빈 상태 반환
        return jsonify({'state': None, 'updated_at': None, 'user_id': user_id})
    except Exception as e:
        return jsonify({'error': safe_error(str(e)), 'state': None}), 500


@app.route('/api/state', methods=['POST'])
def api_save_state():
    """사용자 상태를 Supabase에 저장 (upsert)."""
    data = request.json or {}
    user_id = (data.get('user_id') or '').strip()
    state = data.get('state')
    if not user_id:
        return jsonify({'error': 'user_id required'}), 400
    if state is None:
        return jsonify({'error': 'state required'}), 400
    if not supabase:
        return jsonify({'error': 'supabase unavailable'}), 503
    try:
        supabase.table('user_states').upsert({
            'user_id': user_id,
            'state': state,
        }, on_conflict='user_id').execute()
        return jsonify({'ok': True, 'user_id': user_id})
    except Exception as e:
        return jsonify({'error': safe_error(str(e))}), 500


# ============================================================
# 시작
# ============================================================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print('=' * 60)
    print('부동산 자산관리 백엔드 서버 (v2.16-cross-verify)')
    print('=' * 60)
    print(f'API 키 설정: {"O" if API_KEY else "X (.env 파일에 MOLIT_API_KEY 추가 필요)"}')
    print(f'Supabase 연결: {"O" if supabase else "X (선택사항 - 자동완성만 비활성화)"}')
    print(f'법정동 코드: {len(LAWD_CODES)}건 로드됨')
    print(f'🆕 Day 8: 단지 교차검증(cross_verify) 활성화')
    print(f'서버 시작: http://localhost:{port}')
    print(f'프론트엔드: http://localhost:{port}')
    print(f'API 헬스체크: http://localhost:{port}/api/health')
    print('=' * 60)
    app.run(host='0.0.0.0', port=port, debug=False)
