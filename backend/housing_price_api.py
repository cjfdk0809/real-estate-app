# -*- coding: utf-8 -*-
"""
공동주택 공시가격 조회 API (Blueprint)
====================================================================
housing_price 테이블(국토부 공동주택 호별 공시가격 약 1,558만 건)에서
본건의 [지번주소 + 동 + 호 + 전용면적]으로 공시가격을 찾아 반환합니다.

매칭 전략(3중 안전망):
  1) 지번주소 → legal_dong에서 법정동코드(bjd_code) 변환 → 본번/부번 추출
  2) housing_price를 (bjd_code + 본번 + 부번)으로 후보 조회 (idx_hp_jibun 인덱스 사용)
  3) 전용면적 근사 + 동/호 '숫자만' 정규화 매칭으로 1건 특정
     (예: 데이터 '1201동' vs 본건 '1208' → 숫자 1201 vs 1208 비교)

app.py에 아래 2줄만 추가하면 됩니다:
    from housing_price_api import housing_bp          # (import 구역, registry_bp 옆)
    app.register_blueprint(housing_bp)                # (app 생성 직후, registry 등록 옆)
"""
import os
import re
from flask import Blueprint, request, jsonify

# Supabase 클라이언트 (app.py와 동일한 환경변수 사용)
try:
    from supabase import create_client
    _SB_URL = os.environ.get('SUPABASE_URL', '').strip()
    _SB_KEY = os.environ.get('SUPABASE_KEY', '').strip()
    _sb = create_client(_SB_URL, _SB_KEY) if (_SB_URL and _SB_KEY) else None
except Exception:
    _sb = None

housing_bp = Blueprint('housing_bp', __name__)


# ------------------------------------------------------------
# 유틸
# ------------------------------------------------------------
def _digits(s):
    """문자열에서 숫자만 추출. '1201동'->'1201', '1904'->'1904', '101호'->'101'."""
    return re.sub(r'\D', '', str(s or ''))


def _parse_jibun(addr):
    """지번주소에서 (시군구, 법정동, 본번, 부번) 추출.
    '경기 하남시 감이동 530'    -> ('하남시', '감이동', '530', '0')
    '서울 강남구 역삼동 736-1'  -> ('강남구', '역삼동', '736', '1')
    """
    if not addr:
        return None, None, None, None
    addr = addr.strip()
    # 끝부분 지번(숫자 또는 숫자-숫자) 추출
    m = re.search(r'(\d+)(?:-(\d+))?\s*$', addr)
    bonbun = bubun = None
    if m:
        bonbun = str(int(m.group(1)))
        bubun = str(int(m.group(2))) if m.group(2) else '0'
        head = addr[:m.start()].strip()
    else:
        head = addr
    parts = head.split()
    dong = None
    for p in parts:                       # 법정동(…동/…리/…가)
        if re.search(r'(동|리|가)$', p):
            dong = p
    sigungu = None
    for p in parts:                       # 시군구(…시/…군/…구) — 법정동은 제외
        if p != dong and re.search(r'(시|군|구)$', p):
            sigungu = p
    return sigungu, dong, bonbun, bubun


def _lookup_bjd_code(sigungu, dong):
    """legal_dong에서 (시군구+동) -> 법정동코드(10자리)."""
    if not _sb or not dong:
        return None
    rows = []
    try:                                   # 1차: 동 정확 매칭
        rows = (_sb.table('legal_dong')
                .select('bjd_code, sido, sigungu, dong')
                .eq('dong', dong).eq('is_active', True)
                .limit(50).execute().data or [])
    except Exception:
        rows = []
    if not rows:                           # 2차: 동 부분 매칭
        try:
            rows = (_sb.table('legal_dong')
                    .select('bjd_code, sido, sigungu, dong')
                    .ilike('dong', f'%{dong}%').eq('is_active', True)
                    .limit(50).execute().data or [])
        except Exception:
            rows = []
    if not rows:
        return None
    if sigungu:                            # 시군구로 동명 중복 해소
        for r in rows:
            if sigungu in (r.get('sigungu') or ''):
                return r.get('bjd_code')
    return rows[0].get('bjd_code')         # 동명이 유일하면 그대로


# ------------------------------------------------------------
# 엔드포인트
# ------------------------------------------------------------
@housing_bp.route('/api/housing-price')
def housing_price():
    """본건 공동주택 공시가격 조회.

    Query params:
        addr  : 지번주소 (예: '경기 하남시 감이동 530')  ← 권장
        dong  : 동 (예: '1208' 또는 '1208동')
        ho    : 호 (예: '1904')
        area  : 전용면적 ㎡ (예: '84.92')
        (선택) bjd_code, bonbun, bubun 를 직접 넘기면 주소 파싱을 건너뜀
    """
    if not _sb:
        return jsonify({'error': 'Supabase 미연결 (SUPABASE_URL/KEY 확인)'}), 503

    addr     = request.args.get('addr', '').strip()
    dong     = request.args.get('dong', '').strip()
    ho       = request.args.get('ho', '').strip()
    area     = request.args.get('area', '').strip()
    bjd_code = request.args.get('bjd_code', '').strip()
    bonbun   = request.args.get('bonbun', '').strip()
    bubun    = request.args.get('bubun', '').strip()

    # 1) 주소 파싱 (bjd_code/본번이 직접 안 들어온 경우)
    sigungu = dongli = None
    if addr:
        sigungu, dongli, p_bonbun, p_bubun = _parse_jibun(addr)
        if not bonbun:
            bonbun = p_bonbun or ''
        if not bubun:
            bubun = p_bubun or '0'
    if not bjd_code and dongli:
        bjd_code = _lookup_bjd_code(sigungu, dongli) or ''

    if not bonbun:
        return jsonify({'found': False, 'reason': '지번(본번)을 확인할 수 없습니다.',
                        'addr': addr}), 200

    # 2) housing_price 후보 조회 (지번 단위)
    try:
        q = _sb.table('housing_price').select(
            'danji_name, dong_name, ho_name, exclusive_area, '
            'housing_price, bjd_code, bonbun, bubun, road_addr'
        )
        if bjd_code:
            q = q.eq('bjd_code', bjd_code)          # 인덱스 사용 (빠름)
        elif dongli:
            q = q.eq('dongli', dongli)              # 폴백
        q = (q.eq('bonbun', str(int(bonbun)))
              .eq('bubun', str(int(bubun or '0')))
              .limit(3000))
        rows = q.execute().data or []
    except Exception as e:
        return jsonify({'error': f'조회 실패: {e}'}), 502

    if not rows:
        return jsonify({'found': False,
                        'reason': '해당 지번의 공시가격 데이터가 없습니다.',
                        'bjd_code': bjd_code, 'bonbun': bonbun, 'bubun': bubun}), 200

    # 3) 동/호 숫자 정규화 매칭
    cands = rows
    t_dong, t_ho = _digits(dong), _digits(ho)
    if t_dong:
        f = [r for r in cands if _digits(r.get('dong_name')) == t_dong]
        if f:
            cands = f
    if t_ho:
        f = [r for r in cands if _digits(r.get('ho_name')) == t_ho]
        if f:
            cands = f

    # 4) 전용면적 가장 가까운 순
    if area:
        try:
            fa = float(area)
            cands = sorted(cands, key=lambda r: abs(float(r.get('exclusive_area') or 0) - fa))
        except ValueError:
            pass

    if not cands:
        return jsonify({'found': False, 'reason': '동/호 매칭 실패',
                        'candidates_count': len(rows)}), 200

    best = cands[0]
    return jsonify({
        'found': True,
        'housing_price': best.get('housing_price'),     # 원 단위 정수
        'matched': {
            'danji_name': best.get('danji_name'),
            'dong_name': best.get('dong_name'),
            'ho_name': best.get('ho_name'),
            'exclusive_area': best.get('exclusive_area'),
            'road_addr': best.get('road_addr'),
            'bjd_code': best.get('bjd_code'),
            'bonbun': best.get('bonbun'),
            'bubun': best.get('bubun'),
        },
        'candidates_in_jibun': len(rows),
    }), 200
