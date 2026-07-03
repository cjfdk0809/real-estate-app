"""
등기부등본 자동 분석 모듈
- PDF 텍스트 추출
- Claude API를 통한 권리분석
- NPL 회수 시뮬레이션

이 파일을 backend/registry_analyzer.py 로 저장하고,
backend/app.py 에서 import 해서 사용합니다.

[2026-06-01 수정]
- max_tokens 8000 -> 16000 (긴 등기부에서 JSON이 잘려 파싱 실패하던 문제 해결)
- 고정 지시문/스키마를 system 으로 분리 (구조 정리)
- 응답 파싱 안정화: 코드펜스 제거 + 길이초과(max_tokens) 감지 시 명확한 안내
"""

import os
import io
import json
import re
from flask import Blueprint, request, jsonify

# PDF 텍스트 추출 라이브러리 (PyPDF2 또는 pdfplumber)
try:
    import pdfplumber
    PDF_LIB = 'pdfplumber'
except ImportError:
    try:
        from PyPDF2 import PdfReader
        PDF_LIB = 'pypdf2'
    except ImportError:
        PDF_LIB = None

# Anthropic Claude SDK
try:
    from anthropic import Anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False


# Blueprint 등록
registry_bp = Blueprint('registry', __name__)


def extract_pdf_text(pdf_bytes):
    """PDF 바이트에서 텍스트 추출"""
    if PDF_LIB == 'pdfplumber':
        text_parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        return '\n\n'.join(text_parts)
    elif PDF_LIB == 'pypdf2':
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text_parts = []
        for page in reader.pages:
            text_parts.append(page.extract_text() or '')
        return '\n\n'.join(text_parts)
    else:
        raise RuntimeError('PDF 처리 라이브러리가 설치되지 않았습니다. pip install pdfplumber')


# Claude API에 전송할 고정 지시문 + 스키마 (NPL 자산관리부 관점)
# 등기부 텍스트는 매번 달라지므로 system(고정)과 user(가변)로 분리한다.
SYSTEM_INSTRUCTIONS = """당신은 키움에프앤아이 자산관리부의 NPL(부실채권) 권리분석 전문가입니다.
사용자가 제공하는 등기부등본 텍스트를 분석하여 정확한 JSON 형식으로만 응답하세요.
설명이나 다른 텍스트는 절대 포함하지 마세요. 코드블록(```)도 사용하지 마세요.

다음 JSON 스키마에 정확히 맞춰 응답하세요:

{
  "property": {
    "address_road": "도로명주소",
    "address_lot": "지번주소",
    "complex_name": "단지명/건물명",
    "dong": "동 번호",
    "ho": "호수",
    "floor": 해당층(숫자),
    "total_floors": 총층수(숫자),
    "exclusive_area_sqm": 전용면적(숫자, ㎡),
    "land_share_sqm": 대지지분(숫자, ㎡),
    "land_share_ratio": "대지권비율(예: 7893.4분의 24.95)",
    "registration_date": "최초등기일(YYYY-MM-DD)",
    "structure": "건물구조(예: 철근콘크리트구조)",
    "main_use": "주용도(예: 공동주택)",
    "building_type": "건물유형: 아파트/연립주택/다세대주택/오피스텔/단독주택/다가구주택/근린생활시설/기타 중 정확히 하나. 표제부의 주용도·건물내역·구조·층수·건물명칭을 종합해 판별할 것(예: 주용도가 '공동주택'이어도 규모·세대·명칭으로 아파트/연립/다세대를 구분, 업무시설이지만 주거형태면 오피스텔)"
  },
  "ownership": {
    "current_owner": "현재 소유자명",
    "owner_birth_year": "출생연도(주민번호 앞 2자리, 예: 71)",
    "acquisition_date": "취득일(YYYY-MM-DD)",
    "acquisition_cause": "취득사유(예: 매매)"
  },
  "rights_timeline": [
    {
      "seq": 순위번호,
      "section": "갑구 or 을구",
      "type": "권리유형(소유권보존/소유권이전/근저당권설정/가압류/강제경매개시결정/주택임차권/지상권 등)",
      "date": "접수일(YYYY-MM-DD)",
      "receipt_number": "접수번호(예: 제160089호)",
      "creditor_or_holder": "권리자명",
      "amount": 금액(원, 숫자, 없으면 null),
      "description": "주요 등기사항 요약",
      "is_cancelled": false 또는 true (말소 여부)
    }
  ],
  "analysis": {
    "baseline_right": {
      "seq": 말소기준권리 순위번호,
      "type": "권리유형",
      "date": "일자",
      "holder": "권리자",
      "reasoning": "왜 이것이 말소기준권리인지 설명"
    },
    "rights_classification": [
      {
        "seq": 순위번호,
        "type": "권리유형",
        "holder": "권리자",
        "date": "일자",
        "amount": 금액(숫자, 없으면 null),
        "classification": "인수 or 소멸 or 말소기준 or 배당참여",
        "reasoning": "분류 사유"
      }
    ],
    "tenant_analysis": {
      "has_tenant": true 또는 false,
      "tenant_name": "임차인명 (있을 경우)",
      "deposit": 임차보증금(숫자, 원),
      "monthly_rent": 월차임(숫자, 원, 없으면 null),
      "lease_contract_date": "임대차계약일(YYYY-MM-DD)",
      "move_in_date": "주민등록(전입)일자(YYYY-MM-DD)",
      "fixed_date": "확정일자(YYYY-MM-DD)",
      "lease_registration_date": "임차권등기명령 등기일자(YYYY-MM-DD, 없으면 null)",
      "has_opposing_power": true 또는 false,
      "has_priority_payment_right": true 또는 false,
      "opposing_power_reasoning": "대항력 판단 사유",
      "is_accepted_by_buyer": true 또는 false,
      "buyer_acceptance_reasoning": "낙찰자 인수 여부 판단 사유"
    },
    "auction_status": {
      "is_under_auction": true 또는 false,
      "auction_type": "강제경매 or 임의경매 or 공매 or null",
      "case_number": "사건번호(예: 2025타경51592)",
      "court": "관할법원",
      "auction_start_date": "경매개시결정일(YYYY-MM-DD)",
      "creditor": "경매신청 채권자"
    },
    "risk_factors": [
      {
        "level": "HIGH or MEDIUM or LOW",
        "title": "리스크 제목",
        "description": "상세 설명"
      }
    ],
    "total_secured_debt": 담보채권 총액(숫자, 원, 근저당 채권최고액 합계),
    "total_lien_amount": 가압류 총액(숫자, 원),
    "expected_recovery_difficulty": "상/중/하 (회수 난이도)"
  }
}

분석 시 주의사항:
1. 말소기준권리: 최선순위 (근)저당권, 가압류, 담보가등기, 경매개시결정 중 가장 빠른 것
2. 임차인 대항력: 주민등록(전입)일이 말소기준권리보다 빠른 경우 대항력 인정
3. 우선변제권: 확정일자 + 주민등록 + 점유 모두 갖춘 경우 인정
4. 금액은 모두 원 단위 숫자 (예: 540,000,000원 → 540000000). 숫자 안에 쉼표(,)를 절대 넣지 마세요.
5. 말소된 등기는 is_cancelled: true로 표시
6. JSON 외 다른 텍스트는 절대 포함하지 마세요
"""


# ============================================================
# 🆕 분석 속도 개선 (모델·입력 최적화)
# ============================================================
# 등기부 권리추출은 '정형 데이터 뽑기'라 Haiku로도 충분히 처리되며 Sonnet보다 빠릅니다.
# ⚠️ 정확도 우선이면 아래 두 줄을 주석 교체해서 Sonnet으로 되돌리세요.
REGISTRY_MODEL = "claude-haiku-4-5-20251001"   # 빠름·저렴 (실제 등기부 2~3건 검증 후 운영 권장)
# REGISTRY_MODEL = "claude-sonnet-4-6"          # 정확도 우선(느림)


def _clean_registry_text(text):
    """등기부 텍스트의 정렬용 공백·빈 줄만 축소해 입력 토큰을 줄인다.
    갑구·을구·표제부의 실제 '내용'은 한 글자도 삭제하지 않는다 (정확도 보존)."""
    if not text:
        return text
    text = re.sub(r'[ \t\u3000]{2,}', ' ', text)   # 연속 공백(전각 포함) → 1칸
    text = re.sub(r'\n{3,}', '\n\n', text)          # 3줄 이상 빈 줄 → 1줄
    return text.strip()


def analyze_registry_with_claude(registry_text, api_key):
    """Claude API를 호출하여 등기부 권리분석 수행"""
    if not ANTHROPIC_AVAILABLE:
        raise RuntimeError('anthropic 패키지가 설치되지 않았습니다. pip install anthropic')

    client = Anthropic(api_key=api_key)

    # 🆕 정렬용 공백·빈 줄 축소로 입력 토큰 절감 (내용은 보존)
    cleaned_text = _clean_registry_text(registry_text)

    message = client.messages.create(
        model=REGISTRY_MODEL,       # 🆕 Haiku 기본(빠름) — 파일 상단 상수로 전환 가능
        max_tokens=16000,           # 항목 많은 등기부도 JSON이 잘리지 않도록 상향 유지
        temperature=0,              # 🆕 정형 추출 → 결정적 출력(정확도·일관성·속도 안정)
        system=SYSTEM_INSTRUCTIONS,
        messages=[
            {
                "role": "user",
                "content": (
                    "아래 등기부등본 텍스트를 분석하여 지정된 JSON 스키마로만 응답하세요.\n\n"
                    "[등기부등본 텍스트]\n---\n"
                    f"{cleaned_text}\n---"
                )
            }
        ]
    )

    # 응답이 길이 제한(max_tokens)에 걸려 중간에 잘렸는지 먼저 확인한다.
    # 잘린 응답을 그대로 json.loads 하면 "Expecting ',' delimiter" 류의 파싱 오류가 난다.
    if getattr(message, 'stop_reason', None) == 'max_tokens':
        raise RuntimeError(
            '등기부 항목이 매우 많아 분석 결과가 최대 길이를 초과했습니다. '
            '담당자에게 max_tokens 추가 상향 또는 스키마 간소화를 요청하세요.'
        )

    response_text = message.content[0].text.strip()

    # 혹시 ```json ... ``` 코드펜스로 감싸진 경우 제거
    if response_text.startswith('```'):
        response_text = re.sub(r'^```(?:json)?\s*', '', response_text)
        response_text = re.sub(r'\s*```$', '', response_text)

    # 첫 '{' 부터 마지막 '}' 까지만 추출 (앞뒤 잡텍스트 대비)
    json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
    if json_match:
        response_text = json_match.group(0)

    return json.loads(response_text)


def calculate_npl_recovery(analysis_result, estimated_market_value):
    """NPL 회수 시뮬레이션 계산

    Args:
        analysis_result: Claude가 분석한 권리관계 JSON
        estimated_market_value: 추정 시세 (원)

    Returns:
        회수 시나리오별 시뮬레이션 결과
    """
    scenarios = []

    # 낙찰가율 시나리오: 100%, 80%, 64%, 51%
    for label, ratio in [
        ('1차매각', 1.0),
        ('2차매각(유찰1회)', 0.80),
        ('3차매각(유찰2회)', 0.64),
        ('4차매각(유찰3회)', 0.51),
    ]:
        winning_bid = int(estimated_market_value * ratio)
        auction_cost = int(winning_bid * 0.01)  # 경매비용 약 1%

        # 배당 가능 금액
        distributable = winning_bid - auction_cost

        # 권리 분류에서 배당 받을 권리들 추출
        rights = analysis_result.get('analysis', {}).get('rights_classification', [])
        tenant = analysis_result.get('analysis', {}).get('tenant_analysis', {})

        distributions = []
        remaining = distributable

        # 1순위: 경매비용
        distributions.append({
            'priority': 0,
            'name': '경매비용',
            'amount_claimed': auction_cost,
            'amount_distributed': auction_cost,
            'note': '약 낙찰가의 1%'
        })

        # 2순위 이후: 권리 순서대로 배당
        # 단순화: 날짜 순으로 배당, 임차인 우선변제권은 확정일자 기준
        sorted_rights = sorted(
            [r for r in rights if r.get('classification') in ('말소기준', '소멸', '배당참여')],
            key=lambda x: x.get('date', '9999')
        )

        # 임차인 추가 (확정일자 기준으로 위치 결정)
        if tenant.get('has_tenant') and tenant.get('has_priority_payment_right'):
            sorted_rights.append({
                'seq': 'T',
                'type': '주택임차권 (우선변제권)',
                'holder': tenant.get('tenant_name', '임차인'),
                'date': tenant.get('fixed_date', ''),
                'amount': tenant.get('deposit', 0),
                'classification': '배당참여 (확정일자 기준)'
            })
            sorted_rights.sort(key=lambda x: x.get('date', '9999'))

        for right in sorted_rights:
            amount = right.get('amount') or 0
            if amount == 0:
                continue

            distributed = min(amount, max(remaining, 0))
            distributions.append({
                'priority': right.get('seq'),
                'name': f"{right.get('type')} - {right.get('holder')}",
                'date': right.get('date'),
                'amount_claimed': amount,
                'amount_distributed': distributed,
                'classification': right.get('classification', '')
            })
            remaining -= distributed

        scenarios.append({
            'scenario_name': label,
            'bid_ratio': ratio,
            'winning_bid': winning_bid,
            'auction_cost': auction_cost,
            'distributable': distributable,
            'distributions': distributions,
            'surplus': max(remaining, 0)
        })

    return {
        'estimated_market_value': estimated_market_value,
        'scenarios': scenarios
    }


@registry_bp.route('/api/analyze-registry', methods=['POST'])
def analyze_registry():
    """등기부등본 PDF 업로드 → 자동 권리분석"""
    try:
        # API 키 확인
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            return jsonify({
                'success': False,
                'error': 'Claude API 키가 서버에 설정되지 않았습니다. Render 환경변수 ANTHROPIC_API_KEY를 확인하세요.'
            }), 500

        # PDF 파일 받기
        if 'pdf' not in request.files:
            return jsonify({
                'success': False,
                'error': 'PDF 파일이 업로드되지 않았습니다.'
            }), 400

        pdf_file = request.files['pdf']
        if not pdf_file.filename:
            return jsonify({
                'success': False,
                'error': '파일을 선택해주세요.'
            }), 400

        # PDF 텍스트 추출
        pdf_bytes = pdf_file.read()
        if len(pdf_bytes) > 10 * 1024 * 1024:  # 10MB 제한
            return jsonify({
                'success': False,
                'error': '파일 크기가 10MB를 초과합니다.'
            }), 400

        registry_text = extract_pdf_text(pdf_bytes)
        if len(registry_text.strip()) < 100:
            return jsonify({
                'success': False,
                'error': 'PDF에서 텍스트를 추출할 수 없습니다. 스캔 이미지가 아닌 텍스트 PDF여야 합니다.'
            }), 400

        # Claude API 호출
        analysis_result = analyze_registry_with_claude(registry_text, api_key)

        # 시장가치가 있으면 NPL 시뮬레이션도 같이 수행
        market_value = request.form.get('market_value')
        npl_simulation = None
        if market_value:
            try:
                market_value_int = int(market_value)
                npl_simulation = calculate_npl_recovery(analysis_result, market_value_int)
            except (ValueError, TypeError):
                pass

        return jsonify({
            'success': True,
            'analysis': analysis_result,
            'npl_simulation': npl_simulation,
            'raw_text_length': len(registry_text)
        })

    except json.JSONDecodeError as e:
        return jsonify({
            'success': False,
            'error': f'Claude API 응답을 JSON으로 파싱하는데 실패했습니다: {str(e)}'
        }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'분석 중 오류 발생: {str(e)}',
            'error_type': type(e).__name__
        }), 500


@registry_bp.route('/api/simulate-npl', methods=['POST'])
def simulate_npl():
    """NPL 회수 시뮬레이션만 별도로 계산 (시세 변경 시 재계산용)"""
    try:
        data = request.json
        analysis_result = data.get('analysis')
        market_value = int(data.get('market_value', 0))

        if not analysis_result or market_value <= 0:
            return jsonify({
                'success': False,
                'error': 'analysis와 market_value가 필요합니다.'
            }), 400

        simulation = calculate_npl_recovery(analysis_result, market_value)
        return jsonify({
            'success': True,
            'npl_simulation': simulation
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@registry_bp.route('/api/registry-health', methods=['GET'])
def registry_health():
    """등기부 분석 모듈 상태 체크"""
    return jsonify({
        'pdf_library': PDF_LIB,
        'anthropic_available': ANTHROPIC_AVAILABLE,
        'api_key_set': bool(os.getenv('ANTHROPIC_API_KEY'))
    })
