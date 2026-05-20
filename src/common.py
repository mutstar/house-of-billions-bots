"""공통 유틸: 환경변수 로드, 로깅, 화이트리스트 데코레이터, 공유 HTTP 클라이언트."""
from __future__ import annotations

import logging
import os
import time
from functools import wraps
from pathlib import Path
from typing import Awaitable, Callable, Union

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    # dotenv는 로컬 개발 편의용. 운영(Fly secrets)에선 없어도 무방
    pass

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


logger = get_logger(__name__)

_ALLOWLIST_CACHE: dict = {
    "set": frozenset(),
    "fetched_at": 0.0,
    "fail_count": 0,
}
_ALLOWLIST_TTL_SEC = 60.0


def make_shared_http_client() -> httpx.AsyncClient:
    """공유 AsyncClient — 100명 동시 push 대비 connection pool 확장."""
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
    timeout = httpx.Timeout(5.0, connect=3.0)
    return httpx.AsyncClient(limits=limits, timeout=timeout)


async def post_init_shared_http(application) -> None:
    application.bot_data["http_client"] = make_shared_http_client()
    logger.info("shared httpx.AsyncClient 생성 (limits=200/50, timeout=5s)")


async def post_shutdown_shared_http(application) -> None:
    client: httpx.AsyncClient | None = application.bot_data.get("http_client")
    if client is not None:
        await client.aclose()
        logger.info("shared httpx.AsyncClient close 완료")


def get_cached_allowlist() -> frozenset[str]:
    """현재 캐시된 allowlist 반환 (synchronous, decorator 내 호출용)."""
    return _ALLOWLIST_CACHE["set"]


async def fetch_allowlist_from_api(
    api_url: str,
    secret: str,
    *,
    http: httpx.AsyncClient,
    timeout: float = 5.0,
) -> frozenset[str]:
    """event-score /api/bot/allowlist 호출 → 핸들 set 반환, 캐시 갱신.
    실패 시: 기존 캐시 유지 + fail_count 증가 + 로그. 예외 X.
    """
    if not api_url or not secret:
        logger.warning("allowlist fetch skipped (env 미설정)")
        return _ALLOWLIST_CACHE["set"]
    endpoint = f"{api_url.rstrip('/')}/api/bot/allowlist"
    headers = {"X-Bot-Secret": secret}
    try:
        resp = await http.get(endpoint, headers=headers, timeout=timeout)
        if resp.status_code // 100 != 2:
            _ALLOWLIST_CACHE["fail_count"] += 1
            logger.warning(
                "allowlist fetch 실패: status=%s body=%s fail_count=%d (캐시 유지: %d 핸들)",
                resp.status_code,
                resp.text[:200],
                _ALLOWLIST_CACHE["fail_count"],
                len(_ALLOWLIST_CACHE["set"]),
            )
            return _ALLOWLIST_CACHE["set"]
        data = resp.json()
        raw = data.get("handles", [])
        if not isinstance(raw, list):
            logger.warning("allowlist response handles 비-list (캐시 유지)")
            return _ALLOWLIST_CACHE["set"]
        new_set = frozenset(
            normalize_handle(h) for h in raw if isinstance(h, str) and h.strip()
        )
        _ALLOWLIST_CACHE["set"] = new_set
        _ALLOWLIST_CACHE["fetched_at"] = time.time()
        _ALLOWLIST_CACHE["fail_count"] = 0
        logger.info("allowlist 갱신: %d 핸들 (count=%s)", len(new_set), data.get("count"))
        return new_set
    except Exception as e:
        _ALLOWLIST_CACHE["fail_count"] += 1
        logger.warning(
            "allowlist fetch 예외: %s fail_count=%d (캐시 유지: %d 핸들)",
            e,
            _ALLOWLIST_CACHE["fail_count"],
            len(_ALLOWLIST_CACHE["set"]),
        )
        return _ALLOWLIST_CACHE["set"]


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"환경변수 미설정: {name}")
    return value


def normalize_handle(raw: str) -> str:
    return raw.strip().lstrip("@").lower()


def load_allowed_handles(path: str | os.PathLike[str]) -> frozenset[str]:
    """파일에서 허용 핸들 집합 로드. 1줄당 1 핸들, `#` 주석·빈 줄 무시."""
    p = Path(path)
    if not p.exists():
        # 미존재 = 명시적 차단 (open-by-default 방지)
        # [경고] 파일 부재 시 모든 사용자 차단 — 운영 중 파일 삭제 사고 차단용
        logger.warning("allowlist 파일 부재: %s — 모든 사용자 차단", p)
        return frozenset()

    handles: set[str] = set()
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        h = normalize_handle(line)
        if h:
            handles.add(h)
    logger.info("allowlist 로드: %d 핸들 (%s)", len(handles), p)
    return frozenset(handles)


AllowedSource = Union[frozenset[str], Callable[[], frozenset[str]]]


def allowlist_required(
    allowed: AllowedSource,
    block_message: str = (
        "⚠️ Luma 참가자만 이용 가능합니다.\n\n"
        "Luma에서 이벤트에 사전 등록한 분만 이 봇을 사용할 수 있습니다.\n"
        "등록한 텔레그램 username(@핸들)과 동일한 계정으로 접근해 주세요.\n"
        "문의는 행사 스태프에게 부탁드립니다."
    ),
    no_handle_message: str = (
        "⚠️ 텔레그램 username 미설정 상태입니다.\n\n"
        "설정 > 편집 > Username 에서 @핸들을 설정한 뒤 다시 시도해 주세요.\n"
        "그래도 안 되면 스태프에게 문의해 주세요."
    ),
    bypass_if: Callable[[Update, ContextTypes.DEFAULT_TYPE], bool] | None = None,
) -> Callable[
    [Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[object]]],
    Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[object]],
]:
    """`update.effective_user.username` 기반 사전 등록 핸들 검사 데코레이터.

    bypass_if: 참 반환 시 allowlist 게이트를 건너뛴다. 이미 참여한 사용자의 1회
    차단 메시지가 allowlist 미등록 차단보다 우선해야 하는 경우에 사용.
    """

    def decorator(
        handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[object]],
    ) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[object]]:
        @wraps(handler)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if bypass_if is not None:
                try:
                    if bypass_if(update, context):
                        return await handler(update, context)
                except Exception as e:
                    # bypass_if 자체 오류는 차단 우회로 처리하지 않고 정상 게이트 진행
                    logger.warning("bypass_if 예외 무시: %s", e)

            user = update.effective_user
            username = (user.username or "").strip() if user else ""

            async def _reply(text: str) -> None:
                msg = update.effective_message
                if msg is not None:
                    await msg.reply_text(text)

            if not username:
                logger.info("차단 (username 미설정) user_id=%s", user.id if user else None)
                await _reply(no_handle_message)
                return ConversationHandler.END

            current = allowed() if callable(allowed) else allowed
            if normalize_handle(username) not in current:
                logger.info("차단 (미등록 핸들) @%s user_id=%s", username, user.id if user else None)
                await _reply(block_message)
                return ConversationHandler.END

            return await handler(update, context)

        return wrapper

    return decorator


async def push_score_to_event_score(
    *,
    http: httpx.AsyncClient,
    telegram_user_id: int,
    telegram_handle: str | None,
    player_name: str,
    score: int,
    correct_count: int,
    total_count: int,
    elapsed_sec: int,
    attempt_num: int,
) -> bool:
    """event-score API 로 점수 POST. 실패 시 False 반환 (예외 전파 X).

    env:
      EVENT_SCORE_API_URL — 예: https://event-score.vercel.app
      BOT_SHARED_SECRET — event-score 측과 동일한 시크릿
    """
    api_url = os.environ.get("EVENT_SCORE_API_URL", "").rstrip("/")
    secret = os.environ.get("BOT_SHARED_SECRET", "").strip()
    if not api_url or not secret:
        logger.info("event-score push skipped (env 미설정)")
        return False

    endpoint = f"{api_url}/api/bot/scores"
    payload = {
        "gameId": 1,
        "telegramUserId": str(telegram_user_id),
        "telegramHandle": telegram_handle,
        "playerName": player_name,
        "score": int(score),
        "correctCount": int(correct_count),
        "totalCount": int(total_count),
        "elapsedSec": int(elapsed_sec),
        "attemptNum": int(attempt_num),
    }
    headers = {"X-Bot-Secret": secret, "Content-Type": "application/json"}

    try:
        resp = await http.post(endpoint, json=payload, headers=headers, timeout=5.0)
        if resp.status_code // 100 != 2:
            logger.warning(
                "event-score push 실패: status=%s body=%s",
                resp.status_code,
                resp.text[:200],
            )
            return False
        data = resp.json()
        matched = bool(data.get("matched"))
        logger.info(
            "event-score push OK: matched=%s score=%s total=%s",
            matched,
            data.get("score"),
            data.get("total"),
        )
        return matched
    except Exception as e:
        # [경고] 네트워크/타임아웃/JSON 파싱 실패는 모두 fire-and-forget — 사용자 채팅 차단 금지
        logger.warning("event-score push 예외: %s", e)
        return False
