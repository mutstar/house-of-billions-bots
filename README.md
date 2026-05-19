# House of Billions Bots

오프라인 House of Billions 이벤트용 텔레그램 봇 패키지. 동일 패키지에서 두 개의 봇 프로세스를 실행하며, 참가자 화이트리스트와 점수 집계는 [event-score](https://github.com/updown256/event-score) 백엔드 API와 연동된다.

## 1. 구성

| 봇 | 모듈 | 기능 |
|----|------|------|
| 딥페이크 퀴즈 | `src/deepfake_bot.py` | 25문항 퀴즈, 정답률·소요시간 측정. 종료 시 event-score `/api/bot/scores` 로 점수 자동 push. `attempt_counts.json` 으로 시도 횟수 제한 |
| 트레이딩 MBTI | `src/mbti_bot.py` | 13문항으로 8가지 유형(BHT·BHC·BST·BSC·WHT·WHC·WST·WSC) 분석. 결과 이미지 + X(트위터) 공유 버튼 제공 |

공통 인프라(`src/common.py`):
- `allowlist_required` 데코레이터 — `update.effective_user.username` 기반 사전 등록 핸들 검사
- `fetch_allowlist_from_api` — event-score `/api/bot/allowlist` 호출, 60초 캐시, 실패 시 기존 캐시 유지
- `push_score_to_event_score` — 점수 fire-and-forget POST (실패해도 사용자 채팅 차단 없음)
- `.env` 자동 로드(`python-dotenv`, 로컬 개발용)

## 2. 화이트리스트 동작 (API 기반)

기본 동작:
1. 봇 부팅 시 `src/data/allowed_handles.txt` 1회 로드 → **fallback 캐시** (API 첫 응답 전까지만 사용)
2. `EVENT_SCORE_API_URL` + `BOT_SHARED_SECRET` 설정 시 PTB JobQueue 가 `BOT_ALLOWLIST_REFRESH_SEC` (기본 60초) 간격으로 `GET /api/bot/allowlist` 호출하여 캐시 갱신
3. 차단 시 메시지: `"⚠️ Luma 참가자만 이용 가능합니다."`
4. username 미설정 사용자는 별도 안내(설정 경로 안내)

운영 룰:
- 단일 진실 원천 = event-score DB (Luma 참가자). 파일 편집 X
- API env 미설정 환경에서는 `allowed_handles.txt` 만으로 동작 (재배포 시 반영)
- API 실패 시 기존 캐시 유지 — 일시적 장애로 인한 전원 차단 방지

## 3. 로컬 개발

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env
# .env 값 채움 (아래 환경변수 표 참조)

python -m src.deepfake_bot
python -m src.mbti_bot
```

`uv` 사용 시:
```bash
uv sync
uv run python -m src.deepfake_bot
```

## 4. 환경변수

| 변수 | 필수 | 용도 |
|------|------|------|
| `BOT_TOKEN_DEEPFAKE` | ✓ (deepfake) | @BotFather 발급 토큰 |
| `BOT_TOKEN_MBTI` | ✓ (mbti) | @BotFather 발급 토큰 |
| `EVENT_SCORE_API_URL` | 권장 | event-score 배포 URL (예: `https://event-score.vercel.app`) |
| `BOT_SHARED_SECRET` | 권장 | event-score 와 동일한 시크릿, `X-Bot-Secret` 헤더로 전송 |
| `BOT_ALLOWLIST_REFRESH_SEC` | — | allowlist 폴링 주기 (기본 `60`) |
| `MBTI_IMAGE_URL_BASE` | 권장 | MBTI 결과 이미지 베이스 URL (GCS 등). 미설정 시 `MBTI_IMAGE_DIR` 사용 |
| `MBTI_IMAGE_DIR` | — | 로컬 fallback 이미지 디렉토리 (기본 `src/data/mbti_images`) |
| `ATTEMPT_FILE_PATH` | — | 딥페이크 시도 횟수 저장 경로 (기본 `/data/attempt_counts.json`, Fly volume) |

`EVENT_SCORE_API_URL` 또는 `BOT_SHARED_SECRET` 미설정 시:
- allowlist 폴링 비활성 → 파일 fallback 만 사용
- 점수 push 비활성 (로그에 `event-score push skipped` 기록)

## 5. MBTI 결과 이미지

권장: GCS 공개 버킷.
```bash
gsutil cp *.png gs://<bucket>/mbti/
gsutil iam ch allUsers:objectViewer gs://<bucket>
# fly secrets / .env
MBTI_IMAGE_URL_BASE=https://storage.googleapis.com/<bucket>/mbti
```
봇은 `<MBTI_IMAGE_URL_BASE>/<code>.png` 형태로 fetch. 코드: `BHT`·`BHC`·`BST`·`BSC`·`WHT`·`WHC`·`WST`·`WSC`.

`MBTI_IMAGE_URL_BASE` 미설정 시 `MBTI_IMAGE_DIR` 의 로컬 PNG 사용(개발용).

## 6. Fly.io 배포

`fly.toml` 핵심:
- `[processes]` 그룹 2개 — `deepfake`, `mbti`. 단일 앱에서 두 프로세스 동시 실행
- `[[mounts]]` — `deepfake` 프로세스에만 `/data` 볼륨 마운트 (`attempt_counts.json` 영속화)
- `entrypoint.sh` 가 `/data` 권한을 `bot:bot` 으로 조정 후 `gosu` 로 비root 강등 실행
- VM: `shared-cpu-1x` / 256MB (양 프로세스 공통)

```bash
# 앱 초기화 (fly.toml 보존)
fly launch --no-deploy

# 딥페이크 시도 횟수 저장용 볼륨
fly volumes create data --size 1 --region nrt

# 시크릿 등록
fly secrets set \
  BOT_TOKEN_DEEPFAKE=<deepfake-bot-token> \
  BOT_TOKEN_MBTI=<mbti-bot-token> \
  EVENT_SCORE_API_URL=https://event-score.vercel.app \
  BOT_SHARED_SECRET=<shared-secret> \
  MBTI_IMAGE_URL_BASE=https://storage.googleapis.com/<bucket>/mbti

fly deploy
```

## 7. 운영

```bash
# 프로세스별 로그
fly logs -p deepfake
fly logs -p mbti

# 상태
fly status

# allowlist 즉시 반영
# → event-score DB 갱신만으로 충분 (다음 폴링에서 자동 반영, 최대 BOT_ALLOWLIST_REFRESH_SEC 지연)

# 차단 메시지·시도 횟수 등 코드 변경 시
fly deploy
```

로그 모니터링 포인트:
- `allowlist 갱신: N 핸들` — 정상 폴링
- `allowlist fetch 실패` / `예외` — API 장애 (캐시 유지 동작 중)
- `event-score push OK: matched=...` — 점수 push 성공·매칭 여부
- `차단 (미등록 핸들) @...` — 사전 등록되지 않은 사용자 접근

## 8. event-score 와의 계약

| 엔드포인트 | 방향 | 헤더 | 용도 |
|-----------|------|------|------|
| `GET /api/bot/allowlist` | bot → event-score | `X-Bot-Secret` | 허용 텔레그램 핸들 목록 |
| `POST /api/bot/scores` | bot → event-score | `X-Bot-Secret` | 딥페이크 퀴즈 결과 점수 push |

`POST /api/bot/scores` payload:
```json
{
  "gameId": 1,
  "telegramUserId": "...",
  "telegramHandle": "...",
  "playerName": "...",
  "score": 0,
  "correctCount": 0,
  "totalCount": 25,
  "elapsedSec": 0,
  "attemptNum": 1
}
```
응답 `{matched: bool, score, total}` — `matched=false` 시 이벤트 참가자와 텔레그램 핸들 매칭 실패(점수 push는 실패해도 봇 흐름은 정상 종료).

## 9. [경고] 보안 — 과거 토큰 노출

초기 버전 (`billions_mbti_bot.py`, `billions_deepfake_bot.py`) 에 `BOT_TOKEN` 이 하드코딩된 채로 git history 에 존재.

즉시 조치:
1. Telegram `@BotFather` 에서 두 봇 토큰 모두 `/revoke`
2. 새 토큰 발급 후 `fly secrets set` 으로 재등록
3. `BOT_SHARED_SECRET` 노출 발견 시 동일 절차로 재발급

`git filter-repo` 또는 history 재작성은 별도 작업.
