# 운명원 유튜브 댓글 마케팅 에이전트

유튜브 영상의 자막과 시청자 반응을 참고해 댓글을 작성하고, 운영자가 확인한 댓글을 크롬에서 한 글자씩 입력해 게시하는 로컬 대시보드입니다.

## 이용 조건

프로젝트 자체 소스코드는 **열람 목적으로 공개**합니다. 저작권자의 사전 서면 허락 없이 실행, 복제, 수정, 재배포하거나 상업적으로 이용할 수 없습니다. GitHub 서비스 약관과 적용 법률에 따른 권리는 유지되며, 제3자 자료는 해당 권리자의 조건을 따릅니다. 자세한 내용은 [이용 고지](LICENSE)를 확인해 주세요.

아래 실행 방법은 권리자와 별도의 이용 허가를 받은 사람을 위한 안내입니다.

## 주요 기능

- `연애` 검색으로 최신 영상을 불러오거나 유튜브 영상 링크를 직접 입력합니다.
- 자막을 구간별로 선별하고 관련 시청자 댓글의 관점과 말투를 참고해 영상별 댓글을 만듭니다.
- 장면, 관점과 말투를 고른 뒤 새 시안을 생성하고, 기존 시안과 교정안을 비교합니다.
- Jev 진단 신호에 따라 고정된 교정 규칙을 조합합니다. 초안 생성과 검토를 분리해 검토 중에도 다른 시안을 준비할 수 있습니다.
- 유튜브1~3 중 계정을 선택하거나 영상마다 돌아가며 자동 배정합니다. 게시 시 한 글자씩 입력하고 실제 게시와 삭제 결과를 기록합니다.
- 조회수 1,000~5,000회, 시간당 증가량 150회 이상인 영상을 찾아 일반 영상과 같은 절차로 댓글을 준비하고 게시합니다.
- 영상당 게시 한 건, 하루 기본 10건과 진행 상태, 작업 이력을 관리합니다.

자세한 설계와 제작 의도는 [기획안](docs/product-plan.md)에 정리했습니다.

## 연결 설정

댓글 생성과 교정에는 로컬 라우터 프록시 `10100` 포트, OpenAI API, GCP Vertex AI 중 하나를 선택합니다. 기본 모델은 `google-vertex/gemini-4-hydrogen`입니다.

Jev 진단은 서드파티 라우터와 공식 Jev 중 별도로 선택합니다. 서드파티 라우터의 인증 정보는 `EXPERIENTIAL_API_KEY` 또는 `EXPLABS_API_KEY`, 공식 Jev는 `JEV_OFFICIAL_API_KEY` 환경 변수로 설정할 수 있습니다. OpenAI 키는 설정 화면 또는 `OPENAI_API_KEY`로 입력하며, 화면에서 입력한 키는 서버 실행 중 메모리에 유지됩니다. GCP는 Google Cloud 기본 인증과 프로젝트, 리전을 사용합니다.

## 실행 환경

Windows, Python 3.12 이상, Node.js 22 이상과 Google Chrome이 필요합니다. 로컬 라우터를 사용할 때는 `10100` 포트에 연결된 라우터를 별도로 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade "pip>=26.2"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) '.cache\playwright'
.\.venv\Scripts\python.exe -m playwright install ffmpeg
.\.venv\Scripts\python.exe -m uvicorn poc.app:app --host 127.0.0.1 --port 10101
```

브라우저에서 `http://127.0.0.1:10101/`을 열고 사용할 프로필을 선택한 뒤 `로그인 준비`로 로그인합니다. 로그인 창을 닫은 다음 영상과 댓글을 확인하고 게시 버튼을 누릅니다. 게시가 확인된 댓글은 자동으로 삭제합니다.

## 프로젝트 구성

| 경로 | 내용 |
| --- | --- |
| `poc/prototype.html`, `poc/prototype.js` | 대시보드 화면 |
| `poc/prototype_api.py`, `poc/prototype_service.py` | 영상 분석, 시안 생성과 검토 처리 |
| `poc/natural_comment.py` | Jev 진단 및 교정 규칙 조합 |
| `poc/browser.py`, `poc/store.py` | 크롬 입력, 게시 확인, 제한과 이력 |
| `docs/product-plan.md` | 완성된 제품의 기획과 설계 의도 |
| `docs/prompts/` | 댓글 생성 및 교정 지시문 |
| `tests/` | 핵심 기능 검증 |

API 키, 크롬 프로필, 개인 작업 기록, 화면 캡처와 캐시는 저장소에 포함하지 않습니다. `poc/data/comment_tones.json`의 공개 댓글 발췌와 출처는 제3자 자료이며 별도의 조건이 적용됩니다.
