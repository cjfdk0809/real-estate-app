"""
낙찰가율 추정 통계 엔진 (auction_estimate.py)
================================================
경공매 원시 낙찰데이터(auction_sales)로부터 본건의 예상 낙찰가율(매각가/감정가, %)을
통계적으로 추정한다. Flask/Supabase에 의존하지 않는 순수 함수 모듈이라 단위테스트가 쉽다.

설계 요지 (기존 winner-take-all 캐스케이드 대비 개선점):
  1) 최근성 가중   — 오래된 사례일수록 지수감쇠(반감기 halflife개월)로 가중치를 낮춘다.
                     "표본 N건 넘는 가장 짧은 기간" 방식의 노이즈 추종을 제거.
  2) 계층적 수축   — 동(洞) → 시군구 → 시도 → 전국 프라이어 순으로 부분 풀링(partial pooling).
                     표본이 적은 좁은 지역은 상위 지역 값으로 자동으로 끌어당겨 소표본 튐을 억제.
  3) 유찰횟수 보정 — 같은 표본에서 bid_rate ~ fail_count 가중회귀 기울기를 구해,
                     본건의 예상 유찰차수에 맞춰 추정치를 보정. (유찰이 많을수록 낙찰가율↓)
  4) 분포 노출     — 고정 ±5%p 대신 표본의 가중 사분위(P25/P75)를 시나리오로 제공.

데이터 가정(기존 app.py 쿼리에서 확인된 스키마):
  bid_rate  : 매각가/감정가 × 100 (퍼센트, 예 87.3)
  fail_count: 유찰 횟수(정수), sale_date: ISO 날짜 문자열
좌표·준공연도가 없고 주소가 동 단위인 제약을 감안해 '지역×용도' 층화만 사용한다
  (낙찰가율은 비율이라 면적 민감도가 낮아 면적 하드필터는 적용하지 않는다).
"""
from datetime import date, datetime

DEFAULT_CFG = {
    'min_rate': 30.0,       # 이상치 하한(%) — 미만 사례 제외
    'max_rate': 130.0,      # 이상치 상한(%)
    'halflife_months': 12.0,  # 최근성 가중 반감기
    'k_shrink': 8.0,        # 수축 의사표본수(클수록 상위지역으로 더 끌어당김)
    'national_prior': 85.0,  # 전국 프라이어(최상위 기준값)
    'min_n_slope': 12,      # 유찰보정 기울기를 신뢰할 최소 표본
    'min_neff_spread': 4.0,  # 분포(P25/P75)를 자체 표본에서 쓸 최소 유효표본
    'slope_clamp': (-12.0, 0.0),   # 유찰 1회당 보정 기울기 허용범위(%p). 상한 0: 유찰↑→율↑(역방향) 노이즈 차단
    'delta_clamp': (-25.0, 8.0),   # 최종 유찰보정 총량 허용범위(%p)
    'default_spread': 5.0,  # 분포를 못 구할 때의 ±시나리오 폭
}


# ------------------------------------------------------------------
# 순수 통계 헬퍼
# ------------------------------------------------------------------
def months_between(past, ref):
    """past~ref 사이 개월수(대략). past/ref는 date 또는 ISO 문자열."""
    p = _as_date(past)
    r = _as_date(ref)
    if p is None or r is None:
        return 0.0
    return (r.year - p.year) * 12 + (r.month - p.month) + (r.day - p.day) / 30.0


def _as_date(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v)[:10]
    try:
        return date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    except (ValueError, IndexError):
        return None


def recency_weight(months_ago, halflife):
    if halflife <= 0:
        return 1.0
    return 0.5 ** (max(0.0, months_ago) / halflife)


def effective_n(weights):
    """유효표본수 (Σw)² / Σw². 가중이 한쪽에 쏠릴수록 작아진다."""
    s = sum(weights)
    s2 = sum(w * w for w in weights)
    return (s * s / s2) if s2 > 0 else 0.0


def weighted_mean(pairs):
    """pairs = [(value, weight)]"""
    sw = sum(w for _, w in pairs)
    if sw <= 0:
        return None
    return sum(v * w for v, w in pairs) / sw


def weighted_quantile(pairs, q):
    """가중 분위수. pairs = [(value, weight)], q in [0,1]. 선형보간."""
    pts = sorted(((float(v), float(w)) for v, w in pairs if w > 0), key=lambda t: t[0])
    if not pts:
        return None
    total = sum(w for _, w in pts)
    if total <= 0:
        return None
    # 누적가중의 중앙을 각 표본에 배치(Hazen 방식과 유사)
    cum = 0.0
    target = q * total
    prev_v, prev_c = pts[0][0], 0.0
    for v, w in pts:
        c = cum + w / 2.0     # 이 표본의 대표 누적위치
        if c >= target:
            if c == prev_c:
                return v
            frac = (target - prev_c) / (c - prev_c)
            return prev_v + frac * (v - prev_v)
        prev_v, prev_c = v, c
        cum += w
    return pts[-1][0]


def weighted_slope(triples):
    """가중 최소자승 y = a + b·x 의 기울기 b. triples = [(x, y, w)]."""
    sw = sum(w for _, _, w in triples)
    if sw <= 0:
        return None
    mx = sum(x * w for x, _, w in triples) / sw
    my = sum(y * w for _, y, w in triples) / sw
    sxx = sum(w * (x - mx) ** 2 for x, _, w in triples)
    sxy = sum(w * (x - mx) * (y - my) for x, y, w in triples)
    if sxx <= 1e-9:
        return None
    return sxy / sxx


def shrink(n_eff, level_val, parent_val, k):
    """부분 풀링: 유효표본 n_eff와 의사표본 k로 상위(parent) 쪽으로 수축."""
    if level_val is None:
        return parent_val
    denom = n_eff + k
    if denom <= 0:
        return parent_val
    return (n_eff * level_val + k * parent_val) / denom


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ------------------------------------------------------------------
# 층(level) 통계
# ------------------------------------------------------------------
def _level_stats(rows, cfg):
    """rows = [{'bid_rate','fail_count','_w'}]. 가중 분포/유찰기울기 요약."""
    vals = [(r['bid_rate'], r['_w']) for r in rows]
    if not vals:
        return None
    weights = [w for _, w in vals]
    stat = {
        'n': len(rows),
        'n_eff': round(effective_n(weights), 2),
        'median': weighted_quantile(vals, 0.5),
        'p25': weighted_quantile(vals, 0.25),
        'p75': weighted_quantile(vals, 0.75),
    }
    # 유찰 기울기 (fail_count가 있는 사례만)
    fc = [(r['fail_count'], r['bid_rate'], r['_w'])
          for r in rows if r.get('fail_count') is not None]
    stat['fail_mean'] = weighted_mean([(x, w) for x, _, w in fc]) if fc else None
    stat['slope'] = weighted_slope(fc) if len(fc) >= 2 else None
    stat['fail_n'] = len(fc)
    return stat


# ------------------------------------------------------------------
# 메인 파이프라인
# ------------------------------------------------------------------
def estimate_bid_rate(rows, target, cfg=None):
    """
    rows   : auction_sales 행 리스트. 각 행 dict 필수키:
             bid_rate(float,%), sale_date(ISO), sido, sigungu, dong, use_group,
             fail_count(int|None). (이미 대상 시도 범위로 조회된 상태를 가정)
    target : {'sido','sigungu','dong','use_group','fail_count'(int|None),'asof'(date|None)}
    반환   : dict (available, point, p25, p75, chosen_level, levels, fail_adjustment, ...)
    """
    cfg = dict(DEFAULT_CFG, **(cfg or {}))
    asof = _as_date(target.get('asof')) or date.today()
    ug = target.get('use_group')
    lo, hi = cfg['min_rate'], cfg['max_rate']

    # 1) 정제 + 최근성 가중 + 용도 일치(하드)
    clean = []
    for r in rows:
        br = r.get('bid_rate')
        if br is None or br < lo or br > hi:
            continue
        if ug and r.get('use_group') != ug:
            continue
        m = months_between(r.get('sale_date'), asof)
        rr = dict(r)
        rr['_w'] = recency_weight(m, cfg['halflife_months'])
        clean.append(rr)

    if not clean:
        return {'available': False, 'reason': '표본 없음'}

    # 2) 층별 버킷 (dong ⊂ sigungu ⊂ sido)
    t_sido, t_sgg, t_dong = target.get('sido'), target.get('sigungu'), target.get('dong')
    sido_rows = [r for r in clean if not t_sido or r.get('sido') == t_sido]
    sgg_rows = [r for r in sido_rows if t_sgg and r.get('sigungu') == t_sgg]
    dong_rows = [r for r in sgg_rows if t_dong and r.get('dong') == t_dong]

    st_sido = _level_stats(sido_rows, cfg)
    st_sgg = _level_stats(sgg_rows, cfg)
    st_dong = _level_stats(dong_rows, cfg)

    # 3) 계층적 수축 (전국 프라이어 → 시도 → 시군구 → 동)
    prior = cfg['national_prior']
    hat_sido = shrink(st_sido['n_eff'], st_sido['median'], prior, cfg['k_shrink']) if st_sido else prior
    hat_sgg = shrink(st_sgg['n_eff'], st_sgg['median'], hat_sido, cfg['k_shrink']) if st_sgg else hat_sido
    hat_dong = shrink(st_dong['n_eff'], st_dong['median'], hat_sgg, cfg['k_shrink']) if st_dong else hat_sgg

    # 가장 구체적이면서 표본이 있는 층 채택
    if st_dong:
        chosen_level, base, chosen_stat = 'dong', hat_dong, st_dong
    elif st_sgg:
        chosen_level, base, chosen_stat = 'sigungu', hat_sgg, st_sgg
    elif st_sido:
        chosen_level, base, chosen_stat = 'sido', hat_sido, st_sido
    else:
        chosen_level, base, chosen_stat = 'national', prior, None

    # 4) 분포(시나리오): 유효표본이 충분한 가장 구체적 층에서 사분위 오프셋을 가져와 base에 적용
    spread_stat = None
    for st in (st_dong, st_sgg, st_sido):
        if st and st['n_eff'] >= cfg['min_neff_spread'] and st['median'] is not None:
            spread_stat = st
            break
    if spread_stat and spread_stat['p25'] is not None and spread_stat['p75'] is not None:
        lo_off = spread_stat['median'] - spread_stat['p25']
        hi_off = spread_stat['p75'] - spread_stat['median']
    else:
        lo_off = hi_off = cfg['default_spread']

    # 5) 유찰 기울기 추출: 신뢰 가능한 가장 안정적인 층에서 bid_rate~fail_count 기울기·평균을 뽑는다.
    #    실제 보정은 호출측(프론트)이 본건의 현재 유찰차수로 즉시 적용할 수 있게 함께 반환한다.
    #    (fail_count를 캐시 키/서버계산에 묶지 않아, 유찰차수 편집이 바로 반영되고 캐싱이 단순해짐)
    slope = fmean = basis = None
    for lvl, st in (('sigungu', st_sgg), ('sido', st_sido), ('dong', st_dong)):
        if st and st.get('slope') is not None and st.get('fail_n', 0) >= cfg['min_n_slope'] \
                and st.get('fail_mean') is not None:
            slope = _clamp(st['slope'], *cfg['slope_clamp'])
            fmean = st['fail_mean']
            basis = lvl
            break

    def _apply_fail(val, tf):
        if tf is None or slope is None or fmean is None:
            return val, 0.0
        d = _clamp(slope * (tf - fmean), *cfg['delta_clamp'])
        return val + d, d

    tf = target.get('fail_count')            # 선택: 있으면 편의상 서버도 보정치를 계산해 반환
    adj, delta = _apply_fail(base, tf)

    point_base = round(_clamp(base, lo, hi), 1)
    p25_base = round(_clamp(base - lo_off, lo, hi), 1)
    p75_base = round(_clamp(base + hi_off, lo, hi), 1)
    point = round(_clamp(adj, lo, hi), 1)
    p25 = round(_clamp(adj - lo_off, lo, hi), 1)
    p75 = round(_clamp(adj + hi_off, lo, hi), 1)

    fail_adj = {'applied': bool(tf is not None and slope is not None),
                'slope': (round(slope, 3) if slope is not None else None),
                'delta': round(delta, 2), 'target_fail': tf,
                'sample_fail_mean': (round(fmean, 2) if fmean is not None else None),
                'basis_level': basis}

    def _lvl(st):
        if not st:
            return None
        return {'n': st['n'], 'n_eff': st['n_eff'], 'median': _r1(st['median']),
                'p25': _r1(st['p25']), 'p75': _r1(st['p75'])}

    return {
        'available': True,
        # 편의상 서버 보정 반영값(fail_count 주어졌을 때). 프론트는 아래 *_base + slope로 자체 보정.
        'point': point, 'p25': p25, 'p75': p75,
        # 유찰 미보정 기준값 + 기울기 → 호출측이 본건 유찰차수로 즉시 보정
        'point_base': point_base, 'p25_base': p25_base, 'p75_base': p75_base,
        'fail_slope': (round(slope, 3) if slope is not None else None),
        'fail_sample_mean': (round(fmean, 2) if fmean is not None else None),
        'fail_basis_level': basis, 'delta_clamp': list(cfg['delta_clamp']),
        'min_rate': lo, 'max_rate': hi,
        'chosen_level': chosen_level,
        'sample_n': (chosen_stat['n'] if chosen_stat else 0),
        'sample_n_eff': (chosen_stat['n_eff'] if chosen_stat else 0),
        'levels': {'dong': _lvl(st_dong), 'sigungu': _lvl(st_sgg), 'sido': _lvl(st_sido)},
        'shrunk': {'sido': _r1(hat_sido), 'sigungu': _r1(hat_sgg), 'dong': _r1(hat_dong)},
        'fail_adjustment': fail_adj,
        'method': 'recency-weighted + hierarchical shrinkage + fail-slope',
        'halflife_months': cfg['halflife_months'], 'k_shrink': cfg['k_shrink'],
    }


def _r1(v):
    return round(v, 1) if isinstance(v, (int, float)) else None
