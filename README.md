# 부동산 자산관리 시스템 (백엔드 포함)

국토교통부 실거래가 공공 API를 자동으로 수집하는 백엔드 서버 + 프론트엔드 웹앱.

## 폴더 구조

```
real_estate_app/
├── backend/
│   ├── app.py              # Flask 서버 메인
│   ├── lawd_codes.py       # 법정동 코드 매핑
│   ├── requirements.txt    # Python 패키지
│   ├── .env.example        # 환경변수 템플릿
│   └── .env                # ← 직접 만들어야 함 (API 키 입력)
├── frontend/
│   └── index.html          # 자산관리 웹앱
└── README.md               # 본 문서
```

## 설치 (1회만)

### 1단계 — Python 설치 확인

터미널(맥/리눅스) 또는 PowerShell(윈도우)에서:

```bash
python --version
# 또는
python3 --version
```

3.9 이상이 나오면 OK. 없으면 https://www.python.org 에서 다운로드.

### 2단계 — 공공데이터포털 API 키 발급 (5분 소요)

1. https://www.data.go.kr 접속 → 회원가입 (무료)
2. 검색창에 "**아파트 매매 실거래가 상세**" → 첫 번째 결과 클릭
3. **활용신청** 버튼 클릭 → 활용 목적 적당히 입력 → 신청 (자동승인, 10~60분 후 사용 가능)
4. 똑같이 "**아파트 전월세 실거래가**"도 활용신청
5. 우측 상단 **마이페이지 > 오픈API > 운영계정** → 신청한 API 클릭
6. **일반 인증키 (Decoding)** 복사 (Encoding 키 아님 주의)

### 3단계 — 패키지 설치

터미널에서 프로젝트 폴더로 이동:

```bash
cd real_estate_app/backend
pip install -r requirements.txt
```

(권한 오류 시 `pip install --user -r requirements.txt`)

### 4단계 — API 키 설정

`backend/.env.example` 파일을 복사해서 `backend/.env`로 이름 변경 후 텍스트 에디터로 열기.

```bash
cp .env.example .env
```

`.env` 파일에 발급받은 인증키를 붙여넣기:

```
MOLIT_API_KEY=여기에_발급받은_Decoding_키를_붙여넣으세요
```

저장 후 닫기.

## 실행

```bash
cd real_estate_app/backend
python app.py
```

콘솔에 다음과 같이 표시되면 성공:

```
============================================================
부동산 자산관리 백엔드 서버
============================================================
API 키 설정: O
법정동 코드: 100건 로드됨
서버 시작: http://localhost:5000
프론트엔드: http://localhost:5000
API 헬스체크: http://localhost:5000/api/health
============================================================
```

브라우저에서 **http://localhost:5000** 접속 → 자산관리 시스템이 열림.

## 사용법 — 실거래가 자동수집

1. 좌측 **02. 거래사례 비교** 탭 클릭
2. 우측 상단 **⚡ 국토부 자동수집** 버튼 클릭
3. 모달에서 다음 입력:
   - **법정동코드**: 시군구명 입력 (예: "강서구") → 추천 항목 클릭
   - **조회 개월**: 6 (기본값. 최근 6개월)
   - **단지명 필터**: 자동으로 본건 단지명이 채워져 있음 (수정 가능)
   - **전용면적 최소/최대**: 자동으로 본건 ±1㎡로 설정됨 (수정 가능)
4. **📥 데이터 가져오기** 클릭
5. 미리보기 확인 후 **[이 N건을 거래사례에 추가]** 클릭

→ 거래사례 목록에 자동 등록되며, 시계열 차트와 시세 추정에 즉시 반영됨.

## 팀원 공유 방법

### 옵션 A — 한 명의 PC에서 호스팅하고 공유

1. 호스팅할 PC에서 위와 동일하게 설치·실행
2. 그 PC의 IP 주소 확인 (`ipconfig` 또는 `ifconfig`)
3. 팀원들은 브라우저에서 `http://[그 PC의 IP]:5000` 접속 (같은 네트워크여야 함)
4. 방화벽에서 5000 포트 허용 필요할 수 있음

### 옵션 B — 클라우드 무료 호스팅 (Render / Railway)

1. GitHub에 이 폴더 push
2. https://render.com 회원가입 → New Web Service → GitHub 연동
3. 빌드 명령: `pip install -r backend/requirements.txt`
4. 시작 명령: `cd backend && gunicorn app:app` (gunicorn 추가 설치 필요)
5. 환경변수에 MOLIT_API_KEY 추가
6. 배포 완료 후 받은 URL을 팀원들에게 공유

### 옵션 C — 사내 서버 (Linux)

```bash
# 백그라운드 실행
nohup python app.py &

# 또는 systemd 서비스 등록 (영구)
sudo nano /etc/systemd/system/real-estate.service
```

## API 일일 한도

- 공공데이터포털 개발계정: **일 1,000회**
- 운영계정으로 업그레이드 시: **일 10,000~100,000회** (재신청 필요)
- 본 시스템은 **1시간 단위 캐시**가 적용되어 같은 (시군구·년월) 조합은 재호출하지 않음

## 자주 발생하는 문제

**"백엔드 서버 연결 실패"**
→ `python app.py`가 실행 중인지 확인. 터미널에 오류 메시지 있는지 확인.

**"API 키가 설정되지 않음"**
→ `backend/.env` 파일이 있는지, 키가 정확히 붙여넣어졌는지 확인. **Decoding 키** 사용 (Encoding 키 아님).

**"인증키 에러" 또는 "SERVICE KEY IS NOT REGISTERED"**
→ 공공데이터포털에서 활용신청 후 10~60분 대기 필요. 너무 빨리 호출 시 발생.

**조회 결과 0건**
→ 단지명 필터를 줄이거나 빼고 다시 시도. 단지 표기 차이 가능 (예: "감일 한라비발디" vs "감일한라비발디").

**XML 파싱 실패**
→ API 키 형식 오류. Decoding 키 사용 재확인.

## 다음 단계 — 추가 기능 아이디어

- 공시가격 자동 조회 (부동산공시가격 알리미 API 연동)
- 건축물대장 자동 조회 (정부24 행정정보공동이용 API — 기관 인증 필요)
- 단지별 실거래가 자동 알림 (cron + 이메일)
- 다인 협업 (PostgreSQL/Supabase로 DB 분리)
- 인증 추가 (팀원 로그인)
