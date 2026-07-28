"""
AI 도우미 — 앱 사용법·용어와 '현재 열린 물건' 값을 참고해 실시간으로 답변한다.
등기부 분석(registry_analyzer)과 동일하게 Anthropic Claude + ANTHROPIC_API_KEY 를 재사용한다.
  · 모델: Haiku 4.5 (빠르고 저렴)
  · 엔드포인트: POST /api/assistant   { messages:[{role,content}], context:{...} } → { reply }
"""
import os

from flask import Blueprint, request, jsonify

try:
    from anthropic import Anthropic
    ANTHROPIC_AVAILABLE = True
except Exception:
    ANTHROPIC_AVAILABLE = False

assistant_bp = Blueprint('assistant', __name__)

ASSISTANT_MODEL = "claude-haiku-4-5-20251001"   # 빠름·저렴 (등기부 분석과 동일 모델)

# 대화·입력 상한 (오남용·비용 방지)
_MAX_TURNS = 24
_MAX_CHARS_PER_MSG = 6000

# 앱 지식 — 시스템 프롬프트(정적, 프롬프트 캐싱 대상)
SYSTEM_KNOWLEDGE = """당신은 '키움에프앤아이 부동산 자산관리 시스템'의 사용 도우미입니다.
이 앱은 NPL·경매 물건의 시세·추정 낙찰가·권리분석을 자동 산출하는 사내 도구입니다.
사용자(자산운용 담당자)의 앱 사용법·용어 질문과, 현재 열려 있는 물건에 대한 질문에 한국어로 간결하고 정확하게 답합니다.

[좌측 모듈 구성]
- 00 앱 안내서: 전체 사용법 요약.
- 01 본건 정보: 물건 기본정보(주소·면적·용도·감정가 등).
- 02 거래사례 비교: 국토부 실거래(매매·전세·월세) 수집, 시세 차트, '본건 예상시세'(공시가격 개별보정 추정시세) 산출. 표의 매매 행을 클릭하면 지도가 그 위치로 이동.
- 03 매물현황: 네이버 부동산 현재 호가(참고).
- 04 경매사례: 유사 경매 낙찰사례로 예상 낙찰가율의 근거 확보.
- 05 추정낙찰가: 거래사례비교법으로 낙찰예상가 산정. 계산식 = 기준시세 × 가치형성요인 × 낙찰가율. 'AI안'(요인 1.00·낙찰가율 캐스케이드)과 '담당자안'(요인·낙찰가율 직접보정) 중 하나를 최종 채택.
- 06 권리분석: 등기부 기반 말소기준권리·인수/소멸 판단과 '예상 배당표(가배당)'(옥션원 양식 준용) 산출.
- 07 수익률·세금: 취득·보유·양도 관련 수익성 검토.
- 08 담당자 의견: 최종 검토 요약.
- 09 리포트: 전체 분석을 PDF로 출력.

[낙찰가율 산정 방식(핵심)]
- 낙찰가율 = 낙찰가 ÷ 감정가. 05·04에서 사용.
- 캐스케이드(우선순위): 동일단지 사례 → 시군구 사례 → 유사도가중 실측 → 동 단위 실측 → 시군구 실측 → 시군구 통계 → 시도 통계 → 전국 → 기본값(90%).
- '실측'은 INFOCARE(인포케어) 경매 낙찰 데이터로 채운 auction_rate_stats(시군구·용도군·기간별)에서 나오며, 표본(min_n=5)이 충분할 때 그 값을 그대로 씀.
- 용도군(use_group): apt(아파트·주상복합주거) / rh(연립·다세대·빌라) / offi(오피스텔) / sh(단독·다가구). 인포케어 집합건물 데이터는 apt·rh·offi만 채우고 sh는 정적표 근사.
- 표본이 부족하면 한국부동산원 시군구/시도 통계(용도무관)에 용도계수(rh 0.80·offi 0.78·sh 0.74)를 곱한 근사값으로 폴백.

[예상 배당표(06)]
- 실제배당할금액 = 매각대금(예상낙찰가) + 선순위총액(입력) − 경매비용. 담보물권 설정일(등기순위) 순 배당. 최우선변제·당해세·안분배당·경매비용(입력분 제외)은 미반영이며 참고용(가배당)임.

[답변 규칙]
- 항상 한국어로, 담당자에게 말하듯 간결하고 정확하게. 불필요한 서론·면책 나열은 피하고 핵심부터.
- 모르면 모른다고 하고, 앱에서 확인할 위치(예: '05 추정낙찰가 탭')를 안내.
- '현재 물건 정보' 블록이 주어지면 그 값을 근거로 답하되, 값이 없으면 일반 설명으로.
- 이 앱과 무관한 질문에는 정중히 앱 관련으로 유도.
- 산출값은 참고용 추정임을 필요할 때 짧게 덧붙이고, 투자 확정 조언이나 법률 단정은 피함."""


def _sanitize_messages(raw):
    """프론트가 보낸 대화 이력을 role user/assistant, user 시작, 빈 내용 제거로 정리."""
    out = []
    for m in (raw or []):
        role = (m or {}).get('role')
        content = (m or {}).get('content')
        if role not in ('user', 'assistant') or not isinstance(content, str):
            continue
        content = content.strip()[:_MAX_CHARS_PER_MSG]
        if not content:
            continue
        out.append({'role': role, 'content': content})
    # 최근 _MAX_TURNS 개만, user 로 시작하도록 앞부분 정리
    out = out[-_MAX_TURNS:]
    while out and out[0]['role'] != 'user':
        out.pop(0)
    return out


def _format_context(ctx):
    """현재 물건 정보를 사람이 읽을 수 있는 한 블록으로 요약(없으면 빈 문자열)."""
    if not isinstance(ctx, dict):
        return ''
    def _has(v):
        return v not in (None, '', 0)
    lines = []
    if _has(ctx.get('name')):        lines.append(f"- 단지/물건명: {ctx['name']}")
    if _has(ctx.get('address')):     lines.append(f"- 소재지: {ctx['address']}")
    if _has(ctx.get('use')):         lines.append(f"- 용도: {ctx['use']}")
    if _has(ctx.get('area')):        lines.append(f"- 전용면적: {ctx['area']}㎡")
    if _has(ctx.get('baseValue')):   lines.append(f"- 기준시세(요인·낙찰가율 적용 전): {ctx['baseValue']}만원")
    if _has(ctx.get('bidRatePct')):  lines.append(f"- 적용 낙찰가율: {ctx['bidRatePct']}%"
                                                  + (f" (근거: {ctx.get('bidRateScope')})" if _has(ctx.get('bidRateScope')) else ""))
    if _has(ctx.get('estimatedBid')):lines.append(f"- 추정 낙찰예상가: {ctx['estimatedBid']}만원"
                                                  + (f" ({ctx.get('decision')} 채택)" if _has(ctx.get('decision')) else ""))
    if _has(ctx.get('currentView')): lines.append(f"- 사용자가 보고 있는 화면: {ctx['currentView']}")
    if not lines:
        return ''
    return "[현재 물건 정보]\n" + "\n".join(lines)


@assistant_bp.route('/api/assistant', methods=['POST'])
def assistant_chat():
    if not ANTHROPIC_AVAILABLE:
        return jsonify({'error': 'anthropic 패키지가 설치되지 않았습니다.'}), 503
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'error': 'AI 도우미가 아직 설정되지 않았습니다. 관리자에게 ANTHROPIC_API_KEY 설정을 요청하세요.'}), 503

    body = request.get_json(silent=True) or {}
    messages = _sanitize_messages(body.get('messages'))
    if not messages:
        return jsonify({'error': '질문 내용이 없습니다.'}), 400

    # 현재 물건 정보를 마지막 사용자 메시지 앞에 덧붙인다(대화 이력은 오염시키지 않음).
    ctx_block = _format_context(body.get('context'))
    if ctx_block:
        last = messages[-1]
        if last['role'] == 'user':
            last['content'] = ctx_block + "\n\n[질문]\n" + last['content']

    try:
        client = Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=ASSISTANT_MODEL,
            max_tokens=1024,
            temperature=0.3,
            system=[{"type": "text", "text": SYSTEM_KNOWLEDGE, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        reply = "".join(getattr(b, 'text', '') for b in resp.content if getattr(b, 'type', '') == 'text').strip()
        if not reply:
            reply = "죄송합니다, 답변을 생성하지 못했습니다. 질문을 조금 더 구체적으로 적어 주세요."
        return jsonify({'reply': reply})
    except Exception as e:
        return jsonify({'error': f'AI 도우미 오류: {e}'}), 502


@assistant_bp.route('/api/assistant/health', methods=['GET'])
def assistant_health():
    return jsonify({
        'anthropic_available': ANTHROPIC_AVAILABLE,
        'api_key_set': bool(os.getenv('ANTHROPIC_API_KEY')),
        'model': ASSISTANT_MODEL,
    })
