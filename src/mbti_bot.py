"""Billions 트레이딩 MBTI 텔레그램 봇."""
from __future__ import annotations

import io
import os
import urllib.parse
from pathlib import Path

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
)

from .common import (
    allowlist_required,
    fetch_allowlist_from_api,
    get_cached_allowlist,
    get_logger,
    load_allowed_handles,
    require_env,
)

logger = get_logger(__name__)

BOT_TOKEN = require_env("BOT_TOKEN_MBTI")

_PACKAGE_DATA = Path(__file__).resolve().parent / "data"
IMAGE_URL_BASE = os.environ.get("MBTI_IMAGE_URL_BASE", "").strip()
IMAGE_DIR = Path(os.environ.get("MBTI_IMAGE_DIR", _PACKAGE_DATA / "mbti_images"))
ALLOWED_HANDLES_FILE = Path(
    os.environ.get("ALLOWED_HANDLES_FILE", _PACKAGE_DATA / "allowed_handles.txt")
)
EVENT_SCORE_API_URL = os.environ.get("EVENT_SCORE_API_URL", "").rstrip("/")
BOT_SHARED_SECRET = os.environ.get("BOT_SHARED_SECRET", "").strip()
BOT_ALLOWLIST_REFRESH_SEC = int(os.environ.get("BOT_ALLOWLIST_REFRESH_SEC", "60"))
_fallback = load_allowed_handles(ALLOWED_HANDLES_FILE)
if _fallback:
    from .common import _ALLOWLIST_CACHE
    _ALLOWLIST_CACHE["set"] = _fallback
    logger.info("allowlist 파일 fallback 적용: %d 핸들 (API 첫 응답 전까지)", len(_fallback))
SHARE_IMAGE_URL_BASE = os.environ.get("MBTI_SHARE_IMAGE_URL_BASE", "").strip() or IMAGE_URL_BASE

# X(트위터) 공유 — 텍스트·이미지 URL 추후 결정 (placeholder)
# 사용 변수: {code} (예: BHT), {name} (예: 🔥 $BILL or Nothing), {desc}
SHARE_TEXT_TEMPLATE = (
    "내 트레이딩 MBTI는 {code}!\n"
    "{name}\n\n"
    "#BillionsKorea #트레이딩MBTI #{code}"
)

# ─── 13문제 ───────────────────────────────────────────────────
# score 값: f/c/h/l/n/d 조합 (+ 구분)
QUESTIONS = [
    {
        "q": "Q1. 관심 있는 코인을 발견했다. 나는?",
        "options": [
            ("소량 사고 공부한다", "f"),
            ("조사 후 확신 생기면 진입", "c"),
            ("거래량 터지면 바로 진입", "f"),
            ("이미 오른 거, 다음 기회 기다린다", "c"),
        ]
    },
    {
        "q": "Q2. 코인이 갑자기 -30% 급락했다. 나는?",
        "options": [
            ("오히려 찬스, 더 싸게 살 수 있다", "f"),
            ("원인 파악 먼저, 이유 없으면 진입 고려", "c"),
            ("이 추세면 더 내려갈 것, 일단 패스", "c"),
            ("반등 신호 확인되면 바로 진입", "f"),
        ]
    },
    {
        "q": "Q3. 친한 트레이더가 '이거 10배 간다'고 추천했다. 나는?",
        "options": [
            ("믿고 바로 산다", "f+n"),
            ("차트 분석 후 맞으면 진입", "f+d"),
            ("소셜 분위기 더 확인 후 결정", "c+n"),
            ("근거 데이터 직접 검토 후 결정", "c+d"),
        ]
    },
    {
        "q": "Q4. 매수 타이밍이 왔다고 느껴질 때 나의 진입 방식은?",
        "options": [
            ("지지선 믿고 바로 진입", "f"),
            ("시장 분위기 좋아질 때까지 대기", "c"),
            ("반등 확인 후 추격 진입", "c"),
            ("소량 테스트 진입 먼저", "f"),
        ]
    },
    {
        "q": "Q5. 보유 코인이 목표가에서 20% 더 올랐다. 나는?",
        "options": [
            ("이미 목표가에서 익절했다", "c"),
            ("더 갈 것 같아서 계속 홀드", "f"),
            ("목표가 올리고 계속 들고 간다", "f"),
            ("추세 꺾이는 신호 보이면 그때 판다", "c"),
        ]
    },
    {
        "q": "Q6. 보유 코인이 손절 라인에 도달했다. 나는?",
        "options": [
            ("버틴다, 언젠간 오른다", "h"),
            ("즉시 손절", "l"),
            ("물타기로 평단 낮춘다", "h"),
            ("반등 신호 나오면 손절", "l"),
        ]
    },
    {
        "q": "Q7. 손절 라인을 미리 정해놨는데 도달했다. 나는?",
        "options": [
            ("정했으면 무조건 손절", "l"),
            ("조금 더 지켜본다", "h"),
            ("절반만 손절", "l"),
            ("손절 라인을 아래로 조정", "h"),
        ]
    },
    {
        "q": "Q8. 내가 트레이딩에서 가장 크게 잃은 이유는?",
        "options": [
            ("손절 너무 빨리 해서 반등 못 탔다", "l"),
            ("손절 못 하고 버티다 더 크게 잃었다", "h"),
            ("물타기 했다가 더 내려갔다", "h"),
            ("반등 직전에 팔아서 수익을 놓쳤다", "l"),
        ]
    },
    {
        "q": "Q9. 시장 전체가 -40% 폭락했다. 나는?",
        "options": [
            ("이 가격에 더 산다", "h"),
            ("전부 정리하고 현금화", "l"),
            ("핵심만 남기고 나머지 정리", "l"),
            ("시장 회복될 때까지 그냥 둔다", "h"),
        ]
    },
    {
        "q": "Q10. 코인 선택 시 가장 중요하게 보는 것은?",
        "options": [
            ("프로젝트 스토리와 팀", "n"),
            ("차트 패턴과 거래량", "d"),
            ("커뮤니티 분위기와 소셜 버즈", "n"),
            ("온체인 데이터와 펀더멘털", "d"),
        ]
    },
    {
        "q": "Q11. '이 코인 산다!' 결정적인 이유는?",
        "options": [
            ("내러티브 강하고 시장이 좋아할 것 같아서", "n"),
            ("차트 보니 여기서 반등할 것 같아서", "d"),
            ("커뮤니티 활발하고 모멘텀 있어서", "n"),
            ("펀더멘털 대비 저평가라서", "d"),
        ]
    },
    {
        "q": "Q12. 뉴스 없이 코인이 갑자기 +50% 올랐다. 나는?",
        "options": [
            ("이유 모르면 못 탄다, 차트 분석 먼저", "c+d"),
            ("차트 보고 맞으면 추격 진입", "f+d"),
            ("일단 소량 태운다, 커뮤니티 반응 보면서", "f+n"),
            ("소셜 분위기 파악 후 결정, 이유 없으면 패스", "c+n"),
        ]
    },
    {
        "q": "Q13. 내 트레이딩의 가장 큰 무기는?",
        "options": [
            ("시장보다 먼저 내러티브 읽는 눈", "n"),
            ("철저한 기술적 분석과 패턴 인식", "d"),
            ("빠른 판단력과 실행력", "f"),
            ("철저한 리스크 & 자금 관리", "l"),
        ]
    },
]

# ─── 8가지 결과 ────────────────────────────────────────────────
RESULTS = {
    "BHT": ("🔥 $BILL or Nothing",     "느낌 오면 바로 들어가서 존버, 그게 전부"),
    "BHC": ("💣 $BILL Breakout King",   "돌파 확인하면 바로 들어가서 끝까지 버틴다"),
    "BST": ("⚔️ $BILL Scalper",         "트위터 보고 빠르게 들어가, 빠르게 나온다"),
    "BSC": ("⚡ $BILL System Trader",   "차트 보고 바로 들어가고, 아니면 칼같이 자른다"),
    "WHT": ("🏯 $BILL True Believer",   "내러티브 믿고 기다렸다가, 들어가면 절대 안 판다"),
    "WHC": ("📚 $BILL Chart Master",    "차트가 말할 때까지 기다리고, 들어가면 끝까지"),
    "WST": ("📡 $BILL Alpha Hunter",    "커뮤니티 정보 모으고 타이밍 재고, 들리면 바로 인정"),
    "WSC": ("🎯 $BILL Sniper",          "차트 보고 놀림목 기다렸다가 쏘고, 틀리면 바로 자른다"),
}

ANSWERING = 0


def make_share_keyboard(code: str, name: str, desc: str) -> InlineKeyboardMarkup:
    text = SHARE_TEXT_TEMPLATE.format(code=code, name=name, desc=desc)
    params: dict[str, str] = {"text": text}
    if SHARE_IMAGE_URL_BASE:
        params["url"] = f"{SHARE_IMAGE_URL_BASE.rstrip('/')}/{code}.png"
    intent_url = "https://x.com/intent/post?" + urllib.parse.urlencode(
        params, quote_via=urllib.parse.quote
    )
    return InlineKeyboardMarkup([[InlineKeyboardButton("𝕏 X에 공유하기", url=intent_url)]])


def make_keyboard(q_idx: int) -> InlineKeyboardMarkup:
    q = QUESTIONS[q_idx]
    buttons = [
        [InlineKeyboardButton(f"{chr(65+i)}) {label}", callback_data=f"{q_idx}:{i}")]
        for i, (label, _) in enumerate(q["options"])
    ]
    return InlineKeyboardMarkup(buttons)


def calc_result(scores: dict) -> str:
    b_w = "B" if scores["f"] >= scores["c"] else "W"
    h_s = "H" if scores["h"] >= scores["l"] else "S"
    t_c = "T" if scores["n"] >= scores["d"] else "C"
    return b_w + h_s + t_c


async def fetch_result_image(code: str) -> bytes | None:
    if IMAGE_URL_BASE:
        url = f"{IMAGE_URL_BASE.rstrip('/')}/{code}.png"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url)
            if resp.status_code == 200:
                return resp.content
        except Exception as exc:
            logger.warning("이미지 URL fetch 실패 code=%s: %s", code, exc)
        return None
    local_path = IMAGE_DIR / f"{code}.png"
    if local_path.exists():
        try:
            return local_path.read_bytes()
        except Exception as exc:
            logger.warning("이미지 로컬 읽기 실패 code=%s: %s", code, exc)
    return None


@allowlist_required(get_cached_allowlist)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["q_idx"] = 0
    context.user_data["scores"] = {"f": 0, "c": 0, "h": 0, "l": 0, "n": 0, "d": 0}

    await update.message.reply_text(
        "🔥 *빌리언즈 트레이딩 MBTI*\n\n"
        "13개 질문으로 나의 트레이딩 유형을 알아보세요\\!\n"
        "가장 나다운 답변을 골라주세요 👇",
        parse_mode="MarkdownV2",
    )
    await update.message.reply_text(
        f"*{QUESTIONS[0]['q']}*",
        parse_mode="Markdown",
        reply_markup=make_keyboard(0),
    )
    return ANSWERING


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    q_idx, opt_idx = map(int, query.data.split(":"))

    # 이미 지난 질문 버튼 무시
    if q_idx != context.user_data.get("q_idx", 0):
        return ANSWERING

    # 점수 집계
    _, score_str = QUESTIONS[q_idx]["options"][opt_idx]
    scores = context.user_data["scores"]
    for s in score_str.split("+"):
        scores[s] += 1

    next_idx = q_idx + 1
    context.user_data["q_idx"] = next_idx

    if next_idx >= len(QUESTIONS):
        # 결과 계산
        code = calc_result(scores)
        name, desc = RESULTS.get(code, ("❓ Unknown", ""))
        caption = (
            f"✅ 테스트 완료!\n\n"
            f"당신의 트레이딩 유형은...\n\n"
            f"{name}\n"
            f"{desc}\n\n"
            f"#{code} #Billions #트레이딩MBTI\n\n"
            f"📸 결과 캡처 후 X(트위터)에 #BillionsKorea #트레이딩MBTI 태그와 함께 올리고 아래 폼에 제출하면 $BILL 에어드랍 추첨에 참여할 수 있어요!\n"
            f"👉 https://forms.gle/8yPz49NwnDtNAtxz5"
        )
        image_bytes = await fetch_result_image(code)
        share_kb = make_share_keyboard(code, name, desc)
        await query.edit_message_text("결과를 불러오는 중...")
        if image_bytes is not None:
            bio = io.BytesIO(image_bytes)
            bio.name = f"{code}.png"
            await query.message.reply_photo(photo=bio, caption=caption, reply_markup=share_kb)
        else:
            await query.message.reply_text(caption, reply_markup=share_kb)
        return ConversationHandler.END
    else:
        progress = f"({next_idx}/13)"
        q = QUESTIONS[next_idx]
        await query.edit_message_text(
            f"*{q['q']}* {progress}",
            parse_mode="Markdown",
            reply_markup=make_keyboard(next_idx),
        )
        return ANSWERING


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    async def _refresh_allowlist_job(context: ContextTypes.DEFAULT_TYPE) -> None:
        await fetch_allowlist_from_api(EVENT_SCORE_API_URL, BOT_SHARED_SECRET)

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

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={ANSWERING: [CallbackQueryHandler(handle_answer)]},
        fallbacks=[CommandHandler("start", start)],
        per_message=False,
    )

    app.add_handler(conv)
    logger.info("MBTI 봇 시작")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
