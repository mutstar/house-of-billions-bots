#!/usr/bin/env python3
import io
import json
import logging
import random
import time
import httpx
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = "8735556443:AAFgUHh-_VNEicrFqYHQcQ9R-Q8gdEe3Qag"
ATTEMPT_FILE = Path("/home/mbti_bot/attempt_counts.json")

with open("/home/user/quiz_data.json", "r", encoding="utf-8") as f:
    QUESTIONS = json.load(f)["questions"]

DIFFICULTY_LABELS = {"easy": "⬜ 쉬움", "medium": "🟨 보통", "hard": "🟥 어려움"}

ASK_NAME, QUIZ = range(2)


def load_attempts() -> dict:
    if ATTEMPT_FILE.exists():
        with open(ATTEMPT_FILE, "r") as f:
            return json.load(f)
    return {}


def save_attempt(user_id: int) -> int:
    data = load_attempts()
    uid = str(user_id)
    data[uid] = data.get(uid, 0) + 1
    ATTEMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ATTEMPT_FILE, "w") as f:
        json.dump(data, f)
    return data[uid]


def get_attempt_count(user_id: int) -> int:
    data = load_attempts()
    return data.get(str(user_id), 0)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # mid-quiz /start → 경고만 표시, 재시작 안 함
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
        parse_mode="Markdown"
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

    easy   = [q for q in QUESTIONS if q["difficulty"] == "easy"]
    medium = [q for q in QUESTIONS if q["difficulty"] == "medium"]
    hard   = [q for q in QUESTIONS if q["difficulty"] == "hard"]
    random.shuffle(easy)
    random.shuffle(medium)
    random.shuffle(hard)
    context.user_data["questions"] = easy + medium + hard

    await update.message.reply_text(
        f"안녕하세요, *{name}*님! 🎯\n\n총 25문제가 시작됩니다. 집중하세요!",
        parse_mode="Markdown"
    )
    await send_question(update, context)
    return QUIZ


async def send_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = context.user_data["current_q"]
    q = context.user_data["questions"][idx]

    diff_label = DIFFICULTY_LABELS[q["difficulty"]]
    caption = (
        f"*문제 {idx + 1} / 25* — {diff_label} (+{q['points']}점)\n\n"
        "이 이미지는 🤖 AI가 생성한 것인가요, 📸 실제 사진인가요?"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🤖 AI 생성", callback_data=f"ans_{idx}_ai"),
            InlineKeyboardButton("📸 실제 사진", callback_data=f"ans_{idx}_real"),
        ]
    ])

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
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"이미지 전송 실패 Q{idx + 1}: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ 이미지를 불러올 수 없습니다.\n\n{caption}",
            parse_mode="Markdown",
            reply_markup=keyboard
        )


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    data = query.data  # ans_{idx}_{answer}
    parts = data.split("_")
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
            f"{result_text}\n현재 점수: {score_so_far}점 ({context.user_data['correct']}/{q_idx + 1} 정답)"
        )
        await send_question(update, context)
        return QUIZ
    else:
        await finish_quiz(update, context, result_text)
        return ConversationHandler.END


async def finish_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE, last_result: str):
    elapsed = time.time() - context.user_data["start_time"]
    elapsed_int = int(elapsed)
    score = context.user_data["score"]
    correct = context.user_data["correct"]
    name = context.user_data["name"]
    user_id = update.effective_user.id

    minutes = elapsed_int // 60
    seconds = elapsed_int % 60

    attempt_num = save_attempt(user_id)
    attempt_text = f"📌 {attempt_num}번째 시도" if attempt_num > 1 else "📌 첫 번째 시도"

    rank_text = "🥇 완벽!" if score == 100 else ("🥈 대단해요!" if score >= 80 else ("🥉 잘했어요!" if score >= 60 else "💪 다음엔 더 잘할 수 있어요!"))

    await update.effective_message.reply_text(
        f"{last_result}\n\n"
        f"🎉 *퀴즈 완료!*\n\n"
        f"👤 {name}\n"
        f"🎯 점수: *{score}점* / 100점\n"
        f"✅ 정답: {correct} / 25\n"
        f"⏱ 소요 시간: {minutes}분 {seconds}초\n"
        f"{attempt_text}\n\n"
        f"{rank_text}\n\n"
        f"📋 이 화면을 스태프에게 보여주세요!\n"
        f"스태프가 점수를 리더보드에 등록해 드립니다 🏆",
        parse_mode="Markdown"
    )
    # 유저 데이터 초기화 (재시작 허용)
    context.user_data.clear()


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("퀴즈가 취소되었습니다. /start 로 다시 시작할 수 있습니다.")
    return ConversationHandler.END


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)],
            QUIZ: [CallbackQueryHandler(handle_answer, pattern=r"^ans_\d+_(ai|real)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )

    app.add_handler(conv_handler)
    logger.info("딥페이크 퀴즈 봇 시작")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
