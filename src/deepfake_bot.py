"""House of Billions — 딥페이크 탐지 퀴즈 봇."""
from __future__ import annotations

import asyncio
import fcntl
import io
import json
import os
import random
import time
from pathlib import Path

import httpx
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

from .common import (
    allowlist_required,
    fetch_allowlist_from_api,
    get_cached_allowlist,
    get_logger,
    load_allowed_handles,
    post_init_shared_http,
    post_shutdown_shared_http,
    push_score_to_event_score,
    require_env,
)

logger = get_logger(__name__)

BOT_TOKEN = require_env("BOT_TOKEN_DEEPFAKE")

_PACKAGE_DATA = Path(__file__).resolve().parent / "data"
QUIZ_DATA_FILE = Path(os.environ.get("QUIZ_DATA_FILE", _PACKAGE_DATA / "quiz_data.json"))
ATTEMPT_FILE = Path(os.environ.get("ATTEMPT_FILE_PATH", "/data/attempt_counts.json"))
ALLOWED_HANDLES_FILE = Path(
    os.environ.get("ALLOWED_HANDLES_FILE", _PACKAGE_DATA / "allowed_handles.txt")
)

with QUIZ_DATA_FILE.open("r", encoding="utf-8") as f:
    QUESTIONS = json.load(f)["questions"]

EVENT_SCORE_API_URL = os.environ.get("EVENT_SCORE_API_URL", "").rstrip("/")
BOT_SHARED_SECRET = os.environ.get("BOT_SHARED_SECRET", "").strip()
BOT_ALLOWLIST_REFRESH_SEC = int(os.environ.get("BOT_ALLOWLIST_REFRESH_SEC", "60"))
_fallback = load_allowed_handles(ALLOWED_HANDLES_FILE)
if _fallback:
    from .common import _ALLOWLIST_CACHE
    _ALLOWLIST_CACHE["set"] = _fallback
    logger.info("allowlist 파일 fallback 적용: %d 핸들 (API 첫 응답 전까지)", len(_fallback))

DIFFICULTY_LABELS = {"easy": "⬜ 쉬움", "medium": "🟨 보통", "hard": "🟥 어려움"}

ASK_NAME, QUIZ = range(2)

# Telegram file_id 캐시 — 100명 × 25문제 = 2500 → 25 fetch로 감소
# [경고] per-URL lock — 글로벌 lock 시 99 send_photo 직렬화 + 다른 URL도 head-of-line blocking
_FILE_ID_CACHE: dict[str, str] = {}
_FILE_ID_LOCKS: dict[str, asyncio.Lock] = {}
_FILE_ID_LOCKS_GUARD = asyncio.Lock()


async def _get_file_id_lock(image_url: str) -> asyncio.Lock:
    """per-URL asyncio.Lock — guard로 dict mutation 보호 후 회수."""
    async with _FILE_ID_LOCKS_GUARD:
        lk = _FILE_ID_LOCKS.get(image_url)
        if lk is None:
            lk = asyncio.Lock()
            _FILE_ID_LOCKS[image_url] = lk
        return lk


def _read_attempts_locked(f) -> dict:
    """flock 보유 상태에서 호출 — partial JSON 보호."""
    f.seek(0)
    raw = f.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def load_attempts() -> dict:
    ATTEMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not ATTEMPT_FILE.exists():
        return {}
    with ATTEMPT_FILE.open("r", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            return _read_attempts_locked(f)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def save_attempt(user_id: int) -> int:
    """exclusive lock 보호 read-modify-write — 동시 호출 안전."""
    ATTEMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    ATTEMPT_FILE.touch(exist_ok=True)
    with ATTEMPT_FILE.open("r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            data = _read_attempts_locked(f)
            uid = str(user_id)
            data[uid] = data.get(uid, 0) + 1
            f.seek(0)
            f.truncate()
            json.dump(data, f)
            f.flush()
            return data[uid]
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@allowlist_required(get_cached_allowlist)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # 재시도 완전 차단
    _attempts = load_attempts()
    if str(update.effective_user.id) in _attempts:
        # [경고] 본 블록 편집 시 triple-quoted 유지 — raw newline 삽입에도 SyntaxError 미발생
        await update.message.reply_text(
            """⚠️ 딥페이크 퀴즈는 1회만 참여 가능합니다.
이미 참여하셨습니다! 🙅"""
        )
        return ConversationHandler.END

    # mid-quiz /start 진입 시 경고만, 재시작 안 함
    if context.user_data.get("current_q", 0) > 0:
        # [경고] triple-quoted 유지
        await update.message.reply_text(
            """⚠️ 퀴즈가 진행 중입니다!

계속 답변을 선택해 주세요. 퀴즈는 1인 1회만 참여 가능합니다."""
        )
        return QUIZ

    await update.message.reply_text(
        "🤖 *딥페이크 탐지 퀴즈에 오신 것을 환영합니다!*\n\n"
        "AI가 만든 가짜 얼굴인지, 실제 사람의 사진인지 맞춰보세요.\n\n"
        "📊 *점수 시스템*\n"
        "⬜ 쉬움: 2점 × 10문제\n"
        "🟨 보통: 4점 × 10문제\n"
        "🟥 어려움: 8점 × 5문제\n"
        "🏆 최고 점수: 100점",
        parse_mode="Markdown",
    )
    return ASK_NAME


async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if not name or len(name) > 30:
        await update.message.reply_text("이름은 1~30자로 입력해 주세요.")
        return ASK_NAME

    context.user_data["name"] = name
    context.user_data["current_q"] = 0
    context.user_data["score"] = 0
    context.user_data["correct"] = 0
    context.user_data["start_time"] = time.time()
    context.user_data["answered"] = False

    easy = [q for q in QUESTIONS if q["difficulty"] == "easy"]
    medium = [q for q in QUESTIONS if q["difficulty"] == "medium"]
    hard = [q for q in QUESTIONS if q["difficulty"] == "hard"]
    random.shuffle(easy)
    random.shuffle(medium)
    random.shuffle(hard)
    context.user_data["questions"] = easy + medium + hard

    await update.message.reply_text(
        f"안녕하세요, *{escape_markdown(name, version=1)}*님! 🎯\n\n"
        "총 25문제가 시작됩니다. 집중하세요!",
        parse_mode="Markdown",
    )
    await send_question(update, context)
    return QUIZ


async def _send_question_photo(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    image_url: str,
    caption: str,
    keyboard: InlineKeyboardMarkup,
):
    """file_id 캐시 hit 시 재업로드 0. miss 시 single-flight lock 으로 thundering herd 차단."""
    cached = _FILE_ID_CACHE.get(image_url)
    if cached:
        try:
            return await context.bot.send_photo(
                chat_id=chat_id,
                photo=cached,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception as e:
            # [주의] file_id 만료 또는 invalidation — 다시 다운로드 경로로 진입
            logger.warning("file_id cache hit failed (will refresh): %s", e)
            _FILE_ID_CACHE.pop(image_url, None)

    # per-URL lock — 첫 fetch만 직렬화. 다른 URL은 병렬, cached send는 lock 밖에서.
    lk = await _get_file_id_lock(image_url)
    async with lk:
        cached2 = _FILE_ID_CACHE.get(image_url)
        if cached2 is None:
            http: httpx.AsyncClient = context.application.bot_data["http_client"]
            resp = await http.get(image_url, timeout=15.0)
            resp.raise_for_status()
            photo_bytes = io.BytesIO(resp.content)
            photo_bytes.name = "image.jpg"
            sent = await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo_bytes,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            if sent.photo:
                _FILE_ID_CACHE[image_url] = sent.photo[-1].file_id
            return sent

    # cache 채워짐 → lock 밖에서 send_photo (병렬 가능)
    return await context.bot.send_photo(
        chat_id=chat_id,
        photo=_FILE_ID_CACHE[image_url],
        caption=caption,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def send_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    idx = context.user_data["current_q"]
    q = context.user_data["questions"][idx]

    diff_label = DIFFICULTY_LABELS[q["difficulty"]]
    caption = (
        f"*문제 {idx + 1} / 25* — {diff_label} (+{q['points']}점)\n\n"
        "이 이미지는 🤖 AI가 생성한 것인가요, 📸 실제 사진인가요?"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🤖 AI 생성", callback_data=f"ans_{idx}_ai"),
                InlineKeyboardButton("📸 실제 사진", callback_data=f"ans_{idx}_real"),
            ]
        ]
    )

    chat_id = update.effective_chat.id
    try:
        await _send_question_photo(context, chat_id, q["image_url"], caption, keyboard)
    except Exception as e:
        logger.error("이미지 전송 실패 Q%d: %s", idx + 1, e)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ 이미지를 불러올 수 없습니다.\n\n{caption}",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")  # ans_{idx}_{answer}
    q_idx = int(parts[1])
    user_answer = parts[2]

    current_q = context.user_data.get("current_q", 0)
    if q_idx != current_q:
        return QUIZ

    q = context.user_data["questions"][q_idx]
    correct = user_answer == q["answer"]

    if correct:
        context.user_data["score"] += q["points"]
        context.user_data["correct"] += 1
        result_text = f"✅ 정답! +{q['points']}점"
    else:
        correct_label = "🤖 AI 생성" if q["answer"] == "ai" else "📸 실제 사진"
        result_text = f"❌ 오답! 정답은 [{correct_label}]"

    await query.edit_message_reply_markup(reply_markup=None)

    context.user_data["current_q"] += 1
    next_idx = context.user_data["current_q"]

    if next_idx < len(context.user_data["questions"]):
        score_so_far = context.user_data["score"]
        await query.message.reply_text(
            f"{result_text}\n현재 점수: {score_so_far}점 "
            f"({context.user_data['correct']}/{q_idx + 1} 정답)"
        )
        await send_question(update, context)
        return QUIZ

    await finish_quiz(update, context, result_text)
    return ConversationHandler.END


async def finish_quiz(
    update: Update, context: ContextTypes.DEFAULT_TYPE, last_result: str
) -> None:
    elapsed = int(time.time() - context.user_data["start_time"])
    score = context.user_data["score"]
    correct = context.user_data["correct"]
    total = len(context.user_data["questions"])
    name = context.user_data["name"]
    user = update.effective_user
    user_id = user.id
    username = (user.username or "").strip() if user else ""

    minutes, seconds = divmod(elapsed, 60)

    attempt_num = save_attempt(user_id)
    attempt_text = (
        f"📌 {attempt_num}번째 시도" if attempt_num > 1 else "📌 첫 번째 시도"
    )

    if score == 100:
        rank_text = "🥇 완벽!"
    elif score >= 80:
        rank_text = "🥈 대단해요!"
    elif score >= 60:
        rank_text = "🥉 잘했어요!"
    else:
        rank_text = "💪 다음엔 더 잘할 수 있어요!"

    register_text = (
        "📋 결과 등록은 잠시 후 자동 반영됩니다.\n"
        "반영되지 않으면 스태프에게 이 화면을 보여주세요 🏆"
    )

    # 결과 메시지 먼저 전송 — push가 chat critical path를 차단하지 않도록
    safe_name = escape_markdown(name, version=1)
    await update.effective_message.reply_text(
        f"{last_result}\n\n"
        f"🎉 *퀴즈 완료!*\n\n"
        f"👤 {safe_name}\n"
        f"🎯 점수: *{score}점* / 100점\n"
        f"✅ 정답: {correct} / 25\n"
        f"⏱ 소요 시간: {minutes}분 {seconds}초\n"
        f"{attempt_text}\n\n"
        f"{rank_text}\n\n"
        f"{register_text}",
        parse_mode="Markdown",
    )

    # 백그라운드 push — 결과 도착 시 follow-up 메시지로 알림
    # [경고] asyncio.create_task X — Application.create_task로 tracked, graceful shutdown 보장
    chat_id = update.effective_chat.id
    http: httpx.AsyncClient = context.application.bot_data["http_client"]
    context.application.create_task(
        _push_and_notify(
            bot=context.bot,
            http=http,
            chat_id=chat_id,
            telegram_user_id=user_id,
            telegram_handle=username or None,
            player_name=name,
            score=score,
            correct_count=correct,
            total_count=total,
            elapsed_sec=elapsed,
            attempt_num=attempt_num,
        )
    )
    context.user_data.clear()


async def _push_and_notify(
    *,
    bot,
    http: httpx.AsyncClient,
    chat_id: int,
    telegram_user_id: int,
    telegram_handle: str | None,
    player_name: str,
    score: int,
    correct_count: int,
    total_count: int,
    elapsed_sec: int,
    attempt_num: int,
) -> None:
    """비동기 push — 결과 채팅 차단 X. 완료 시 follow-up 메시지 1건."""
    try:
        matched = await push_score_to_event_score(
            http=http,
            telegram_user_id=telegram_user_id,
            telegram_handle=telegram_handle,
            player_name=player_name,
            score=score,
            correct_count=correct_count,
            total_count=total_count,
            elapsed_sec=elapsed_sec,
            attempt_num=attempt_num,
        )
        if matched:
            await bot.send_message(
                chat_id=chat_id,
                text="✅ 점수가 리더보드에 자동 등록되었습니다 🏆",
            )
    except Exception as e:
        # [경고] 백그라운드 task — 예외 전파 시 unhandled task 경고 발생. 명시 로그만
        logger.warning("push_and_notify 예외: %s", e)


async def _post_init(application) -> None:
    await post_init_shared_http(application)
    # /cancel 같은 잔존 메뉴 항목 제거 — start 1개만 노출
    await application.bot.set_my_commands([BotCommand("start", "퀴즈 시작")])
    logger.info("set_my_commands: /start only")


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(post_shutdown_shared_http)
        .build()
    )

    async def _refresh_allowlist_job(context: ContextTypes.DEFAULT_TYPE) -> None:
        http: httpx.AsyncClient = context.application.bot_data["http_client"]
        await fetch_allowlist_from_api(EVENT_SCORE_API_URL, BOT_SHARED_SECRET, http=http)

    if EVENT_SCORE_API_URL and BOT_SHARED_SECRET:
        app.job_queue.run_repeating(
            _refresh_allowlist_job,
            interval=BOT_ALLOWLIST_REFRESH_SEC,
            first=1.0,
            name="allowlist_refresh",
        )
        logger.info("allowlist 자동 새로고침 활성 (간격=%ds)", BOT_ALLOWLIST_REFRESH_SEC)
    else:
        logger.warning("allowlist API env 미설정 — 파일 fallback만 사용")

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)],
            QUIZ: [CallbackQueryHandler(handle_answer, pattern=r"^ans_\d+_(ai|real)$")],
        },
        fallbacks=[ CommandHandler("start", start)],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )

    app.add_handler(conv_handler)
    logger.info("딥페이크 퀴즈 봇 시작")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
