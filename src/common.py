"""공통 유틸: 환경변수 로드, 로깅, 화이트리스트 데코레이터."""
from __future__ import annotations

import logging
import os
from functools import wraps
from pathlib import Path
from typing import Awaitable, Callable

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


def allowlist_required(
    allowed: frozenset[str],
    block_message: str = (
        "⚠️ 이 봇은 사전 등록된 참여자만 사용할 수 있습니다.\n\n"
        "텔레그램 username(@핸들)이 등록된 명단에 있어야 합니다.\n"
        "등록 문의는 행사 스태프에게 부탁드립니다."
    ),
    no_handle_message: str = (
        "⚠️ 텔레그램 username 미설정 상태입니다.\n\n"
        "설정 > 편집 > Username 에서 @핸들을 설정한 뒤 다시 시도해 주세요.\n"
        "그래도 안 되면 스태프에게 문의해 주세요."
    ),
) -> Callable[
    [Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[object]]],
    Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[object]],
]:
    """`update.effective_user.username` 기반 사전 등록 핸들 검사 데코레이터."""

    def decorator(
        handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[object]],
    ) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[object]]:
        @wraps(handler)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

            if normalize_handle(username) not in allowed:
                logger.info("차단 (미등록 핸들) @%s user_id=%s", username, user.id if user else None)
                await _reply(block_message)
                return ConversationHandler.END

            return await handler(update, context)

        return wrapper

    return decorator
