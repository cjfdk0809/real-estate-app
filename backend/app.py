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
    return send_from_directory('../frontend', 'index.html')


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
