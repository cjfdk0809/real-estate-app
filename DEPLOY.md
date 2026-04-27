# 클라우드 배포 가이드 (Render 무료 호스팅)

이 문서는 부동산 자산관리 시스템을 Render에 배포하여 팀원 20명이 어디서든 사용할 수 있게 만드는 방법입니다.

## 배포 후 사용 환경

- ✅ 외부 어디서든 접속 (4G/LTE 포함)
- ✅ PC 끄셔도 됨
- ✅ 팀원에게 URL 한 개 공유하면 끝
- ⚠️ 무료 플랜은 15분 미사용 시 자동 슬립 (첫 접속 시 30초 정도 깨어남)
- ⚠️ 각 팀원의 데이터는 개별 브라우저에 저장 (공용 DB 아님)

## 필요한 것

1. **GitHub 계정** (무료)
2. **Render 계정** (무료)
3. **인터넷 브라우저** (Chrome, Edge 등)

추가 프로그램 설치 불필요. 모두 웹 브라우저에서 진행합니다.

---

# 1단계: GitHub에 코드 업로드

## 1-1) GitHub 가입

1. https://github.com 접속
2. 우측 상단 **"Sign up"** 클릭
3. 이메일·비밀번호·사용자명 입력 → 가입 완료
4. 이메일 인증 완료

## 1-2) 새 저장소(Repository) 생성

1. 로그인 후 우측 상단 **➕ → "New repository"** 클릭
2. 다음 입력:
   - **Repository name**: `real-estate-app` (원하는 이름)
   - **Description**: 부동산 자산관리 시스템 (선택사항)
   - **Public** 선택 (Render 무료 플랜은 Public 저장소만 가능)
   - ⚠️ **"Add a README file" 체크 해제** (기존 README가 있으므로)
   - ⚠️ **"Add .gitignore" None으로 두기**
3. 우측 하단 **[Create repository]** 버튼 클릭

## 1-3) 파일 업로드

저장소가 생성되면 빈 페이지가 나옵니다.

1. **"uploading an existing file"** 링크 클릭 (페이지 중간에 있음)
2. **드래그 앤 드롭 영역**이 나타납니다.

### ⚠️ 매우 중요: 업로드 전 확인사항

- **`.env` 파일을 절대 업로드하지 마세요!** API 키가 노출됩니다.
- `.gitignore` 파일에 `.env`가 등록되어 있어 자동 차단되지만, 한 번 더 확인하세요.

### 업로드할 파일·폴더

`real_estate_app` 폴더 안의 다음 항목들을 모두 드래그:
- ✅ `backend/` 폴더 전체 (안에 app.py, lawd_codes.py, requirements.txt, .env.example 만 있어야 함)
- ✅ `frontend/` 폴더 전체 (index.html)
- ✅ `.gitignore`
- ✅ `render.yaml`
- ✅ `README.md`
- ✅ 본 문서 (`DEPLOY.md`)

### 업로드 방법

1. 파일 탐색기에서 `C:\Users\fldel\Downloads\real_estate_app` 폴더 열기
2. **`backend` 폴더 안에 들어가서 `.env` 파일이 있으면 잠시 다른 곳으로 옮기기** (또는 삭제)
   - `.env.example` 은 그대로 두기 (안전함)
3. 다시 `real_estate_app` 폴더로 돌아와서, 그 안의 모든 파일·폴더를 **선택 후 GitHub 업로드 영역에 드래그**
4. 업로드가 완료될 때까지 대기 (1~2분)
5. 페이지 하단에서:
   - **Commit message**: `Initial commit` (또는 자유)
   - **[Commit changes]** 버튼 클릭

업로드 완료. 페이지가 새로고침되며 파일 목록이 보입니다.

---

# 2단계: Render 가입 및 배포

## 2-1) Render 가입

1. https://render.com 접속
2. 우측 상단 **"Sign Up"** 또는 **"Get Started"** 클릭
3. **"GitHub로 가입"** 추천 (편리함)
4. GitHub 계정 인증 → Render 가입 완료

## 2-2) 새 Web Service 생성

1. Render 대시보드에서 **"+ New"** → **"Web Service"** 클릭
2. **"Build and deploy from a Git repository"** 선택 → **[Next]**
3. GitHub 저장소 목록에서 방금 만든 **`real-estate-app`** 찾기
   - 보이지 않으면 **"Configure account"** 클릭하여 권한 부여
4. 해당 저장소의 **[Connect]** 버튼 클릭

## 2-3) 배포 설정

다음 화면에서 입력:

| 항목 | 값 |
|---|---|
| **Name** | `real-estate-app` (또는 원하는 이름. 이게 URL이 됨) |
| **Region** | **Singapore** (한국에서 가장 가까움) |
| **Branch** | `main` |
| **Root Directory** | `backend` ⚠️ |
| **Runtime** | **Python 3** |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `gunicorn app:app --timeout 60 --workers 2` |
| **Instance Type** | **Free** |

## 2-4) 환경변수 추가 (가장 중요)

스크롤 내려서 **"Environment Variables"** 섹션 찾기 → **[Add Environment Variable]** 클릭:

- **Key**: `MOLIT_API_KEY`
- **Value**: 공공데이터포털에서 받은 **Decoding 키** (재발급받은 새 키)

⚠️ 환경변수에 키를 넣으면 GitHub에 노출되지 않고 안전하게 사용됩니다.

## 2-5) 배포 시작

페이지 맨 아래 **[Create Web Service]** 버튼 클릭.

## 2-6) 빌드 진행 확인

빌드 로그가 자동으로 흐르며 진행됩니다 (3~5분 소요).

성공 시 다음 메시지가 나옵니다:
```
==> Your service is live 🎉
```

화면 상단에 URL이 표시됩니다. 예시:
```
https://real-estate-app-xxxx.onrender.com
```

## 2-7) 접속 테스트

위 URL을 브라우저에서 열기 → 자산관리 시스템이 로드되면 성공!

---

# 3단계: 팀원에게 공유

URL을 카카오톡·슬랙·이메일로 팀원 20명에게 공유.

각 팀원은:
- 자신의 PC/휴대폰에서 URL 접속
- 자기만의 데이터로 본건 등록 (브라우저별 분리됨)
- 국토부 자동수집 기능 공유 사용 (백엔드는 공통)

---

# 4단계: 자주 발생하는 문제

## "Application failed to respond"

→ 첫 접속 시 슬립에서 깨어나는 중. 30초~1분 대기 후 새로고침.

## 빌드 실패: "ModuleNotFoundError"

→ requirements.txt에 패키지 누락. 로그에서 어떤 패키지인지 확인 후 추가하고 다시 push.

## "Service Key is not registered"

→ 환경변수 MOLIT_API_KEY가 잘못됨. Render 대시보드 → Environment → 키 다시 확인. **Decoding 키** 사용 확인.

## CORS 오류

→ 같은 도메인에서 서빙되므로 보통 발생 안 함. 만약 발생 시 Render 로그 확인.

## 갑자기 느려짐

→ 무료 플랜의 슬립/웨이크 특성. 

해결책 (선택사항):
- **유료 플랜으로 업그레이드** ($7/월): 항상 깨어있음
- **UptimeRobot** 같은 무료 모니터링으로 5분마다 핑 보내기 (편법)

---

# 5단계: 코드 수정 후 재배포

기능 추가하거나 버그 수정 후 재배포하는 방법:

## 옵션 A — GitHub 웹에서 직접 수정

1. GitHub 저장소에서 수정할 파일 클릭
2. 우측 상단 ✏️ 연필 아이콘 클릭
3. 코드 수정
4. 페이지 하단 [Commit changes] 클릭
5. Render가 자동으로 재배포 (약 3~5분)

## 옵션 B — 파일 다시 업로드

1. GitHub 저장소에서 [Add file] → [Upload files]
2. 새 파일 드래그 앤 드롭
3. 같은 이름이면 덮어쓰기됨
4. [Commit changes] 클릭
5. Render 자동 재배포

---

# 6단계: 추가 개선 아이디어 (장기)

배포가 안정화된 후 다음 기능 추가 가능:

## 6-1) 공용 데이터베이스 (모든 팀원이 같은 데이터)
- **Supabase** (무료, PostgreSQL 기반)
- 본건·거래사례·경매·권리 데이터를 DB에 저장
- 팀원 누구나 조회·수정 가능
- 개발 1~2일

## 6-2) 인증·로그인
- Supabase Auth (이메일/비밀번호 또는 구글 로그인)
- 팀원만 접속 가능
- 권한 분리 (관리자/일반)
- 개발 1일

## 6-3) 알림 기능
- 새 거래 발생 시 카카오톡/슬랙 알림
- cron으로 매일 새벽 자동 수집
- 개발 0.5일

## 6-4) 모바일 반응형 디자인
- 휴대폰에서도 보기 좋게 CSS 추가
- 개발 0.5일

필요해지면 그때 알려주세요.
