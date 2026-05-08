"""
부동산 자산관리 시스템 - 백엔드 서버
국토교통부 실거래가 공공 API를 프록시하여 프론트엔드에 제공합니다.

실행:
    python app.py

환경변수 (.env):
    MOLIT_API_KEY=공공데이터포털에서 발급받은 인증키
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

# ============================================================
# 환경설정
# ============================================================
load_dotenv()
API_KEY = os.environ.get('MOLIT_API_KEY', '').strip()

# 국토부 실거래가 API 엔드포인트 (2024년 신규 HTTPS 엔드포인트)
URL_TRADE = 'https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev'  # 상세
URL_RENT = 'https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent'  # 전월세

# 연립다세대 실거래가
URL_RH_TRADE = 'https://apis.data.go.kr/1613000/RTMSDataSvcRHTrade/getRTMSDataSvcRHTrade'  # 매매
URL_RH_RENT = 'https://apis.data.go.kr/1613000/RTMSDataSvcRHRent/getRTMSDataSvcRHRent'  # 전월세

# 공동주택 단지 정보 (K-apt)
URL_APT_LIST_DONG = 'https://apis.data.go.kr/1611000/AptListService2/getLegaldongAptList'  # 법정동별 단지목록
URL_APT_LIST_ROAD = 'https://apis.data.go.kr/1611000/AptListService2/getRoadnameAptList'  # 도로명별 단지목록
URL_APT_BASIS = 'https://apis.data.go.kr/1611000/AptBasisInfoServiceV1/getAphusBassInfoV1'  # 단지 기본정보
URL_APT_DETAIL = 'https://apis.data.go.kr/1611000/AptBasisInfoServiceV1/getAphusDtlInfoV1'  # 단지 상세정보

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
        },
        'version': 'v2.0-kapt',
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
        raw_items, err = parse_xml_items(xml_text)
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
        basis_items, err = parse_xml_items(basis_xml)
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
            detail_items, _ = parse_xml_items(detail_xml)
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
# 시작
# ============================================================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print('=' * 60)
    print('부동산 자산관리 백엔드 서버')
    print('=' * 60)
    print(f'API 키 설정: {"O" if API_KEY else "X (.env 파일에 MOLIT_API_KEY 추가 필요)"}')
    print(f'법정동 코드: {len(LAWD_CODES)}건 로드됨')
    print(f'서버 시작: http://localhost:{port}')
    print(f'프론트엔드: http://localhost:{port}')
    print(f'API 헬스체크: http://localhost:{port}/api/health')
    print('=' * 60)
    app.run(host='0.0.0.0', port=port, debug=False)
