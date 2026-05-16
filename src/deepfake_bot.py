"""House of Billions — 딥페이크 탐지 퀴즈 봇."""
from __future__ import annotations

import asyncio
import io
import json
import os
import random
import time
from pathlib import Path

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .common import (
    allowlist_required,
    get_logger,
    load_allowed_handles,
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

ALLOWED_HANDLES = load_allowed_handles(ALLOWED_HANDLES_FILE)

DIFFICULTY_LABELS = {"easy": "⬜ 쉬움", "medium": "🟨 보통", "hard": "🟥 어려움"}

ASK_NAME, QUIZ, CONFIRM_CANCEL = range(3)


def load_attempts() -> dict:
    if ATTEMPT_FILE.exists():
        with ATTEMPT_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_attempt(user_id: int) -> int:
    data = load_attempts()
    uid = str(user_id)
    data[uid] = data.get(uid, 0) + 1
    ATTEMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with ATTEMPT_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f)
    return data[uid]


@allowlist_required(ALLOWED_HANDLES)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # mid-quiz /start 진입 시 경고만, 재시작 안 함
    if context.user_data.get("current_q", 0) > 0:
        await update.message.reply_text(
            "⚠️ 퀴즈가 진행 중입니다!\n\n"
            "퀴즈를 포기하려면 /cancel 을 입력하세요.\n"
            "현재 문제로 돌아가려면 계속 답변을 선택해 주세요."
        )
        return QUIZ

    await update.message.reply_text(
        "🤖 *딥페이크 탐지 퀴즈에 오신 것을 환영합니다!*\n\n"
        "AI가 만든 가짜 얼굴인지, 실제 사람의 사진인지 맞춰보세요.\n\n"
        "📊 *점수 시스템*\n"
        "⬜ 쉬움: 2점 × 10문제\n"
        "🟨 보통: 4점 × 10문제\n"
        "🟥 어려움: 8점 × 5문제\n"
        "🏆 최고 점수: 100점\n\n"
        "먼저 이름을 입력해 주세요 👇",
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
        f"안녕하세요, *{name}*님! 🎯\n\n총 25문제가 시작됩니다. 집중하세요!",
        parse_mode="Markdown",
    )
    await send_question(update, context)
    return QUIZ


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
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(q["image_url"])
            resp.raise_for_status()
            photo_bytes = io.BytesIO(resp.content)
            photo_bytes.name = "image.jpg"
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=photo_bytes,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
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
    await update.effective_message.reply_text(
        f"{last_result}\n\n"
        f"🎉 *퀴즈 완료!*\n\n"
        f"👤 {name}\n"
        f"🎯 점수: *{score}점* / 100점\n"
        f"✅ 정답: {correct} / 25\n"
        f"⏱ 소요 시간: {minutes}분 {seconds}초\n"
        f"{attempt_text}\n\n"
        f"{rank_text}\n\n"
        f"{register_text}",
        parse_mode="Markdown",
    )

    # 백그라운드 push — 결과 도착 시 follow-up 메시지로 알림
    chat_id = update.effective_chat.id
    asyncio.create_task(
        _push_and_notify(
            bot=context.bot,
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


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # 퀴즈 진행 중에만 포기 확인 — 시도 횟수 차감 정책 안내
    if context.user_data.get("current_q", 0) > 0:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ 네, 포기할게요", callback_data="cancel_confirm"),
                    InlineKeyboardButton("❌ 계속할게요", callback_data="cancel_deny"),
                ]
            ]
        )
        await update.message.reply_text(
            "⚠️ *퀴즈를 포기하시겠어요?*\n\n"
            "취소하면 *시도 횟수가 1회 차감*됩니다.\n"
            "스태프는 2번째 시도부터 결과를 인정하지 않아요.",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return CONFIRM_CANCEL

    context.user_data.clear()
    await update.message.reply_text("취소되었습니다. /start 로 다시 시작할 수 있습니다.")
    return ConversationHandler.END


async def handle_cancel_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_confirm":
        user_id = update.effective_user.id
        attempt_num = save_attempt(user_id)
        context.user_data.clear()
        await query.edit_message_text(
            f"퀴즈가 취소되었습니다. (📌 {attempt_num}번째 시도 기록)\n\n"
            "/start 로 다시 시작할 수 있습니다."
        )
        return ConversationHandler.END

    await query.edit_message_text("계속 진행합니다! 퀴즈로 돌아가세요 👇")
    return QUIZ


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)],
            QUIZ: [CallbackQueryHandler(handle_answer, pattern=r"^ans_\d+_(ai|real)$")],
            CONFIRM_CANCEL: [
                CallbackQueryHandler(handle_cancel_confirm, pattern=r"^cancel_(confirm|deny)$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )

    app.add_handler(conv_handler)
    logger.info("딥페이크 퀴즈 봇 시작 (allowlist=%d 핸들)", len(ALLOWED_HANDLES))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
