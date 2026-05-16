# House of Billions Bots

## 1. 개요

두 개의 텔레그램 봇으로 구성된 House of Billions 이벤트용 봇 패키지.

- **딥페이크 퀴즈 봇** (`src/deepfake_bot.py`): 딥페이크 판별 퀴즈 25문항. 정답률에 따라 결과 메시지 제공. 참여 횟수 제한 기능 포함.
- **트레이딩 MBTI 봇** (`src/mbti_bot.py`): 13문항으로 트레이딩 성향을 분석해 8가지 유형(BHT·BHC·BST·BSC·WHT·WHC·WST·WSC) 중 하나를 도출. 결과 이미지 + X(트위터) 공유 버튼 제공.

두 봇 모두 `src/data/allowed_handles.txt` 화이트리스트 기반 접근 제어 적용.

## 2. 로컬 개발

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env
# .env 파일을 열어 BOT_TOKEN_DEEPFAKE, BOT_TOKEN_MBTI 등 값을 채웁니다
```

봇 실행:

```bash
python -m src.deepfake_bot
python -m src.mbti_bot
```

## 3. 화이트리스트 관리

`src/data/allowed_handles.txt` 파일에 허용할 텔레그램 `@handle`을 `@` 없이 한 줄씩 추가합니다.

```
# 예시
alice_dev
bob_trader
```

- `#` 으로 시작하는 줄은 주석, 빈 줄은 무시됩니다.
- 대소문자 구분 없이 처리됩니다.
- 파일이 존재하지 않으면 모든 사용자 차단 상태가 됩니다.
- 변경 후 재배포(`fly deploy`) 또는 머신 재시작으로 반영됩니다.

## 4. MBTI 결과 이미지 설정

결과 이미지는 GCS에 업로드하여 URL로 제공하거나, 로컬 디렉토리에서 읽어올 수 있습니다.

**GCS 사용 (권장):**

```bash
# 이미지 업로드
gsutil cp *.png gs://<bucket>/mbti/

# 공개 읽기 권한 부여
gsutil iam ch allUsers:objectViewer gs://<bucket>

# .env 또는 fly secrets에 설정
MBTI_IMAGE_URL_BASE=https://storage.googleapis.com/<bucket>/mbti
```

봇은 `<MBTI_IMAGE_URL_BASE>/<code>.png` 형태로 이미지를 fetch합니다.
코드 목록: `BHT`, `BHC`, `BST`, `BSC`, `WHT`, `WHC`, `WST`, `WSC`

**로컬 fallback (개발용):**

```bash
MBTI_IMAGE_DIR=/path/to/mbti_images
```

## 5. Fly.io 배포

```bash
# 앱 초기화 (fly.toml 유지, 설정 덮어쓰지 않도록 주의)
fly launch --no-deploy

# 딥페이크 봇 시도 횟수 저장용 볼륨 생성
fly volumes create data --size 1 --region nrt

# 봇 토큰 및 환경변수 설정
fly secrets set \
  BOT_TOKEN_DEEPFAKE=<deepfake-bot-token> \
  BOT_TOKEN_MBTI=<mbti-bot-token> \
  MBTI_IMAGE_URL_BASE=https://storage.googleapis.com/<bucket>/mbti

# 배포
fly deploy
```

## 6. 운영

```bash
# 프로세스별 로그 확인
fly logs -p deepfake
fly logs -p mbti

# 상태 확인
fly status

# allowlist 갱신 후 재배포
fly deploy
```

## 7. [경고] 보안

이전 버전 코드(`billions_mbti_bot.py`, `billions_deepfake_bot.py`)에 `BOT_TOKEN`이 하드코딩되어 git history에 노출되어 있습니다.

**즉시 조치 필요:**
1. Telegram `@BotFather`에서 두 봇 토큰 모두 revoke (`/revoke`)
2. 새 토큰 발급 후 `fly secrets set`으로 등록

git history 정리는 별도 작업이 필요합니다 (`git filter-repo` 또는 새 repository 생성).
