import asyncio
import io
import os
import re
from datetime import datetime
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook

load_dotenv()

PRIMARY_ADMIN_ID = 7798667020
ADMIN_IDS = {PRIMARY_ADMIN_ID}
BUILD_TAG = "v20261120_worker_exact"

DEFAULT_BOT_TOKEN = "9053563311:AAEqtJpOU7Mi_tZMox2NDRLnxLUSUyyWLmM"
BOT_TOKEN = os.getenv("BOT_TOKEN", DEFAULT_BOT_TOKEN).strip() or DEFAULT_BOT_TOKEN

DB_PATH = os.getenv("DB_PATH", "mosy_tasks.db").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://mosy-task-bot-1.onrender.com").strip().rstrip("/")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads").strip()
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALERT_SECONDS_MAP = {
    "10m": 600,
    "1h": 3600,
    "2h": 7200,
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)
scheduler = AsyncIOScheduler(timezone="Asia/Tashkent")


class RegStates(StatesGroup):
    waiting_name = State()
    waiting_mfy = State()


class AnswerStates(StatesGroup):
    waiting_answer = State()
    waiting_reject_reason = State()


def is_admin_user(user_id: Optional[int]) -> bool:
    if not user_id:
        return False
    return int(user_id) == PRIMARY_ADMIN_ID


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return cleaned or "file.bin"


def admin_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="📱 Boshqaruv Paneli",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}/?v={BUILD_TAG}&role=admin&user_id={PRIMARY_ADMIN_ID}"),
                )
            ]
        ],
        resize_keyboard=True,
    )


def worker_reply_kb(user_id: int) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="📋 Topshiriq",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}/?v={BUILD_TAG}&role=worker&tab=my-tasks&user_id={user_id}"),
                ),
                KeyboardButton(
                    text="♻️ Rad etilgan",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}/?v={BUILD_TAG}&role=worker&tab=rejected&user_id={user_id}"),
                ),
            ]
        ],
        resize_keyboard=True,
    )


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                mfy TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'worker',
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                instruction TEXT DEFAULT '',
                deadline TEXT NOT NULL,
                answer_type TEXT NOT NULL DEFAULT 'photo',
                alert_interval TEXT NOT NULL DEFAULT '10m',
                call_message TEXT DEFAULT '',
                sample_photo_name TEXT,
                sample_photo_path TEXT,
                task_file_name TEXT,
                task_file_path TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS task_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'assigned',
                read_at TEXT,
                submitted_at TEXT,
                last_alerted_at TEXT,
                alert_count INTEGER NOT NULL DEFAULT 0,
                answer_text TEXT,
                file_id TEXT,
                file_name TEXT,
                file_path TEXT,
                file_type TEXT,
                last_reason TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS rejections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assignment_id INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                rejected_date TEXT NOT NULL,
                rejected_at TEXT NOT NULL
            )
            """
        )
        for col_sql in [
            "ALTER TABLE tasks ADD COLUMN alert_interval TEXT NOT NULL DEFAULT '10m'",
            "ALTER TABLE tasks ADD COLUMN sample_photo_name TEXT",
            "ALTER TABLE tasks ADD COLUMN sample_photo_path TEXT",
            "ALTER TABLE tasks ADD COLUMN task_file_name TEXT",
            "ALTER TABLE tasks ADD COLUMN task_file_path TEXT",
            "ALTER TABLE task_assignments ADD COLUMN file_path TEXT",
            "ALTER TABLE task_assignments ADD COLUMN last_alerted_at TEXT",
            "ALTER TABLE task_assignments ADD COLUMN alert_count INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                await db.execute(col_sql)
            except Exception:
                pass

        await db.execute("UPDATE users SET role = 'worker' WHERE user_id != ?", (PRIMARY_ADMIN_ID,))
        await db.commit()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user_id = int(message.from_user.id)

    async with aiosqlite.connect(DB_PATH) as db:
        if is_admin_user(user_id):
            await db.execute(
                """
                INSERT INTO users (user_id, full_name, mfy, role, created_at)
                VALUES (?, ?, ?, 'admin', ?)
                ON CONFLICT(user_id) DO UPDATE SET role='admin'
                """,
                (user_id, message.from_user.full_name or "Admin", "Boshqaruv", now_str()),
            )
            await db.execute("UPDATE users SET role = 'worker' WHERE user_id != ?", (PRIMARY_ADMIN_ID,))
            await db.commit()
            await message.answer(
                f"✅ <b>Admin huquqi faollashtirildi!</b> (ID: <code>{user_id}</code>)\n"
                "Pastdagi <b>📱 Boshqaruv Paneli</b> tugmasini bosing.",
                parse_mode="HTML",
                reply_markup=admin_reply_kb(),
            )
            return

        await db.execute("UPDATE users SET role = 'worker' WHERE user_id = ?", (user_id,))
        await db.commit()

        cur = await db.execute("SELECT full_name, mfy FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()

    if row:
        await message.answer(
            f"👋 Xush kelibsiz, <b>{row[0]}</b> ({row[1]})!",
            parse_mode="HTML",
            reply_markup=worker_reply_kb(user_id),
        )
    else:
        await message.answer(
            "Assalomu alaykum! Ro‘yxatdan o‘tish uchun <b>Ism-familiyangizni</b> kiriting:",
            parse_mode="HTML",
        )
        await state.set_state(RegStates.waiting_name)


@router.message(RegStates.waiting_name)
async def reg_name(message: Message, state: FSMContext) -> None:
    full_name = (message.text or "").strip()
    if len(full_name) < 3 or full_name.startswith("/"):
        await message.answer("Iltimos, to‘liq ism-familiyangizni kiriting:")
        return
    await state.update_data(full_name=full_name)
    await message.answer("Qaysi <b>MFY</b> da faoliyat yuritishingizni yozing:", parse_mode="HTML")
    await state.set_state(RegStates.waiting_mfy)


@router.message(RegStates.waiting_mfy)
async def reg_mfy(message: Message, state: FSMContext) -> None:
    mfy = (message.text or "").strip()
    if len(mfy) < 2 or mfy.startswith("/"):
        await message.answer("Iltimos, MFY nomini kiriting:")
        return
    data = await state.get_data()
    full_name = data["full_name"]
    user_id = int(message.from_user.id)
    role = "admin" if is_admin_user(user_id) else "worker"

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, full_name, mfy, role, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, mfy=excluded.mfy, role=excluded.role
            """,
            (user_id, full_name, mfy, role, now_str()),
        )
        await db.commit()

    await state.clear()
    kb = admin_reply_kb() if is_admin_user(user_id) else worker_reply_kb(user_id)
    await message.answer(
        f"✅ Ro‘yxatdan o‘tdingiz: <b>{full_name}</b> ({mfy}).",
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("read_task:"))
async def cb_read_task(callback: CallbackQuery) -> None:
    assignment_id = int(callback.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT ta.id, ta.status, ta.read_at, t.title, t.description, t.instruction,
                   t.deadline, t.answer_type, t.sample_photo_name, t.sample_photo_path
            FROM task_assignments ta
            JOIN tasks t ON t.id = ta.task_id
            WHERE ta.id = ?
            """,
            (assignment_id,),
        )
        row = await cur.fetchone()
        if not row:
            await callback.answer("Topshiriq topilmadi.", show_alert=True)
            return

        if not row[2]:
            await db.execute(
                "UPDATE task_assignments SET read_at = ?, status = CASE WHEN status='assigned' THEN 'read' ELSE status END WHERE id = ?",
                (now_str(), assignment_id),
            )
            await db.commit()

    type_labels = {
        "photo": "Faqat rasm",
        "file": "Faqat fayl (Excel / Word / PDF)",
        "both": "Rasm va fayl",
        "text": "Faqat matn",
    }
    text_msg = (
        f"📌 <b>{row[3]}</b>\n\n"
        f"📝 <b>Mazmuni:</b> {row[4]}\n"
        f"🛠 <b>Izoh / Yo‘riqnoma:</b> {row[5] or 'Ko‘rsatilmagan'}\n"
        f"⏰ <b>Muddat:</b> {row[6]}\n"
        f"📎 <b>Javob formati:</b> {type_labels.get(row[7], row[7])}\n\n"
        "Javob yuborish uchun quyidagi <b>✍️ Javob yuborish</b> tugmasini bosing."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Javob yuborish", callback_data=f"ans_task:{assignment_id}")]
        ]
    )

    sample_path = row[9]
    if sample_path and os.path.exists(sample_path):
        await callback.message.answer_photo(
            photo=open(sample_path, "rb"),
            caption=f"{text_msg}\n\n🖼 <i>Namuna rasm: shu ko‘rinishda bajarilsin.</i>",
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        await callback.message.answer(text_msg, parse_mode="HTML", reply_markup=kb)

    await callback.answer("O‘qilgan deb belgilandi.")


@router.callback_query(F.data.startswith("ans_task:"))
async def cb_ans_task(callback: CallbackQuery, state: FSMContext) -> None:
    assignment_id = int(callback.data.split(":")[1])
    await state.update_data(assignment_id=assignment_id)
    await state.set_state(AnswerStates.waiting_answer)
    await callback.message.answer(
        "Javobingizni yuboring (topshiriq talabiga ko‘ra rasm, fayl yoki matn):"
    )
    await callback.answer()


@router.message(AnswerStates.waiting_answer)
async def process_answer(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    assignment_id = data.get("assignment_id")
    if not assignment_id:
        await state.clear()
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT ta.id, ta.user_id, t.title, t.answer_type, u.full_name, u.mfy
            FROM task_assignments ta
            JOIN tasks t ON t.id = ta.task_id
            JOIN users u ON u.user_id = ta.user_id
            WHERE ta.id = ?
            """,
            (assignment_id,),
        )
        row = await cur.fetchone()
        if not row:
            await state.clear()
            await message.answer("Topshiriq topilmadi.")
            return

        required_type = row[3]
        has_photo = bool(message.photo)
        has_doc = bool(message.document)
        has_text = bool(message.text and not message.text.startswith("/"))

        if required_type == "photo" and not has_photo:
            await message.answer("❌ Bu topshiriq uchun <b>faqat rasm</b> yuborish talab qilinadi!", parse_mode="HTML")
            return
        if required_type == "file" and not has_doc:
            await message.answer("❌ Bu topshiriq uchun <b>faqat fayl</b> (Excel / Word / PDF) yuborish talab qilinadi!", parse_mode="HTML")
            return
        if required_type == "both" and not (has_photo or has_doc):
            await message.answer("❌ Bu topshiriq uchun <b>rasm yoki fayl</b> yuborish talab qilinadi!", parse_mode="HTML")
            return
        if required_type == "text" and not has_text:
            await message.answer("❌ Bu topshiriq uchun <b>faqat matnli javob</b> yuborish talab qilinadi!", parse_mode="HTML")
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        mfy_clean = safe_filename(row[5])
        file_id = None
        file_name = None
        file_path = None
        file_type = None
        answer_text = message.caption or message.text or ""

        if has_photo:
            ph = message.photo[-1]
            file_id = ph.file_id
            file_name = f"{mfy_clean}_{stamp}.jpg"
            file_path = os.path.join(UPLOAD_DIR, f"ans_{assignment_id}_{file_name}")
            tg_file = await bot.get_file(file_id)
            await bot.download_file(tg_file.file_path, destination=file_path)
            file_type = "photo"
        elif has_doc:
            file_id = message.document.file_id
            orig_name = message.document.file_name or "fayl"
            ext = os.path.splitext(orig_name)[1]
            file_name = f"{mfy_clean}_{stamp}{ext}"
            file_path = os.path.join(UPLOAD_DIR, f"ans_{assignment_id}_{safe_filename(file_name)}")
            tg_file = await bot.get_file(file_id)
            await bot.download_file(tg_file.file_path, destination=file_path)
            file_type = "document"
        else:
            file_type = "text"

        submitted = now_str()
        await db.execute(
            """
            UPDATE task_assignments
            SET status='submitted', submitted_at=?, answer_text=?, file_id=?, file_name=?, file_path=?, file_type=?
            WHERE id=?
            """,
            (submitted, answer_text, file_id, file_name, file_path, file_type, assignment_id),
        )
        await db.commit()

    await state.clear()
    await message.answer(
        f"✅ Javobingiz qabul qilindi va adminga yuborildi!\n"
        f"📎 Fayl nomi: <b>{file_name or 'Matnli javob'}</b>",
        parse_mode="HTML",
    )

    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Qabul qildim", callback_data=f"approve_ans:{assignment_id}"),
                InlineKeyboardButton(text="❌ Qayta jo‘natish", callback_data=f"reject_ans:{assignment_id}"),
            ]
        ]
    )
    caption = (
        f"📥 <b>Yangi topshiriq javobi!</b>\n"
        f"👤 Xodim: <b>{row[4]}</b> ({row[5]})\n"
        f"📌 Topshiriq: <b>{row[2]}</b>\n"
        f"🕒 Vaqt: {submitted}\n"
        f"📝 Izoh/Matn: {answer_text or '—'}\n"
        f"📎 Fayl nomi: <code>{file_name or '—'}</code>"
    )

    try:
        if file_type == "photo" and file_id:
            await bot.send_photo(PRIMARY_ADMIN_ID, file_id, caption=caption, parse_mode="HTML", reply_markup=admin_kb)
        elif file_type == "document" and file_id:
            await bot.send_document(PRIMARY_ADMIN_ID, file_id, caption=caption, parse_mode="HTML", reply_markup=admin_kb)
        else:
            await bot.send_message(PRIMARY_ADMIN_ID, caption, parse_mode="HTML", reply_markup=admin_kb)
    except Exception:
        pass


@router.callback_query(F.data.startswith("approve_ans:"))
async def cb_approve_ans(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Faqat admin tasdiqlay oladi.", show_alert=True)
        return
    assignment_id = int(callback.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT ta.user_id, t.title
            FROM task_assignments ta
            JOIN tasks t ON t.id = ta.task_id
            WHERE ta.id = ?
            """,
            (assignment_id,),
        )
        row = await cur.fetchone()
        if not row:
            await callback.answer("Topilmadi.", show_alert=True)
            return
        await db.execute("UPDATE task_assignments SET status='approved' WHERE id=?", (assignment_id,))
        await db.commit()

    try:
        await bot.send_message(
            row[0],
            f"✅ <b>«{row[1]}»</b> topshirig‘i bo‘yicha yuborgan javobingiz admin tomonidan <b>tasdiqlandi</b>!",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Tasdiqlandi!")


@router.callback_query(F.data.startswith("reject_ans:"))
async def cb_reject_ans(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Faqat admin rad eta oladi.", show_alert=True)
        return
    assignment_id = int(callback.data.split(":")[1])
    await state.update_data(reject_assignment_id=assignment_id)
    await state.set_state(AnswerStates.waiting_reject_reason)
    await callback.message.answer(
        "Ushbu topshiriqni <b>nima sababdan rad etayotganingizni</b> yozing (bu sabab xodimga yuboriladi):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AnswerStates.waiting_reject_reason)
async def process_reject_reason(message: Message, state: FSMContext) -> None:
    reason = (message.text or "").strip()
    if len(reason) < 2:
        await message.answer("Iltimos, rad etish sababini yozing:")
        return
    data = await state.get_data()
    assignment_id = data.get("reject_assignment_id")
    if not assignment_id:
        await state.clear()
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT ta.task_id, ta.user_id, t.title
            FROM task_assignments ta
            JOIN tasks t ON t.id = ta.task_id
            WHERE ta.id = ?
            """,
            (assignment_id,),
        )
        row = await cur.fetchone()
        if not row:
            await state.clear()
            await message.answer("Topshiriq topilmadi.")
            return

        stamp = now_str()
        day = today_str()
        await db.execute(
            "UPDATE task_assignments SET status='rejected', last_reason=? WHERE id=?",
            (reason, assignment_id),
        )
        await db.execute(
            """
            INSERT INTO rejections (assignment_id, task_id, user_id, reason, rejected_date, rejected_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (assignment_id, row[0], row[1], reason, day, stamp),
        )
        await db.commit()

    await state.clear()
    await message.answer("♻️ Topshiriq rad etildi, sabab bazaga yozildi va xodimga yuborildi.")

    try:
        await bot.send_message(
            row[1],
            f"❌ <b>«{row[2]}»</b> topshirig‘ingiz rad etildi!\n"
            f"📌 <b>Sabab:</b> {reason}\n\n"
            "Iltimos, kamchilikni tuzatib, qayta jo‘nating.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Qayta jo‘natish", callback_data=f"ans_task:{assignment_id}")]
                ]
            ),
        )
    except Exception:
        pass


async def monitor_tasks_job() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT ta.id, ta.user_id, ta.status, ta.read_at, ta.last_alerted_at,
                   t.title, t.deadline, t.alert_interval, t.call_message, t.created_at, u.full_name, u.mfy
            FROM task_assignments ta
            JOIN tasks t ON t.id = ta.task_id
            JOIN users u ON u.user_id = ta.user_id
            WHERE ta.status IN ('assigned', 'read', 'rejected')
            """
        )
        rows = await cur.fetchall()
        now_dt = datetime.now()

        for row in rows:
            a_id, u_id, status, read_at_s, last_alert_s, title, deadline, alert_interval, call_msg, t_created_s, full_name, mfy = row
            interval_sec = ALERT_SECONDS_MAP.get(alert_interval or "10m", 600)
            label_map = {"10m": "10 daqiqa", "1h": "1 soat", "2h": "2 soat"}
            interval_label = label_map.get(alert_interval or "10m", "10 daqiqa")

            ref_time_s = last_alert_s or t_created_s
            if not ref_time_s:
                continue

            try:
                ref_dt = datetime.strptime(ref_time_s, "%Y-%m-%d %H:%M")
            except Exception:
                continue

            if (now_dt - ref_dt).total_seconds() >= interval_sec:
                await db.execute(
                    "UPDATE task_assignments SET last_alerted_at = ?, alert_count = COALESCE(alert_count, 0) + 1 WHERE id = ?",
                    (now_str(), a_id),
                )
                await db.commit()

                spoken = call_msg or f"{full_name}, «{title}» topshirig‘i bo‘yicha {interval_label}lik ogohlantirish!"
                try:
                    await bot.send_message(
                        u_id,
                        f"⏰ <b>Ogohlantirish ({interval_label} oralig‘ida):</b>\n"
                        f"📌 Topshiriq: <b>{title}</b>\n"
                        f"🕒 Muddat: <b>{deadline}</b>\n"
                        f"📢 Xabar: <i>{spoken}</i>",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="📖 O‘qish va Javob berish", callback_data=f"read_task:{a_id}")]
                            ]
                        ),
                    )
                except Exception:
                    pass

                try:
                    await bot.send_message(
                        PRIMARY_ADMIN_ID,
                        f"⚠️ <b>{interval_label} ogohlantirish:</b> {full_name} ({mfy}) — «{title}» (holat: {status}).",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass


app = FastAPI(title="MOSY Topshiriqlar Nazorat Markazi")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="webapp"), name="static")


@app.on_event("startup")
async def on_startup() -> None:
    await init_db()
    if not scheduler.running:
        scheduler.add_job(monitor_tasks_job, "interval", minutes=1, id="mosy_monitor", replace_existing=True)
        scheduler.start()
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        pass
    asyncio.create_task(dp.start_polling(bot, handle_signals=False))


@app.get("/", response_class=HTMLResponse)
async def serve_webapp() -> HTMLResponse:
    with open("webapp/index.html", "r", encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/api/me")
async def api_me(user_id: int = Query(default=0)) -> dict:
    return {"user_id": user_id, "is_admin": is_admin_user(user_id)}


@app.get("/api/workers")
async def api_workers() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id, full_name, mfy FROM users WHERE user_id != ? ORDER BY full_name ASC",
            (PRIMARY_ADMIN_ID,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


@app.post("/api/tasks")
async def api_create_task(
    admin_id: int = Form(default=PRIMARY_ADMIN_ID),
    title: str = Form(...),
    description: str = Form(...),
    instruction: str = Form(default=""),
    deadline: str = Form(...),
    answer_type: str = Form(default="photo"),
    alert_interval: str = Form(default="10m"),
    call_message: str = Form(default=""),
    user_ids_csv: str = Form(...),
    sample_photo: Optional[UploadFile] = File(default=None),
    task_file: Optional[UploadFile] = File(default=None),
) -> dict:
    if not is_admin_user(admin_id):
        raise HTTPException(status_code=403, detail="Faqat admin topshiriq bera oladi.")

    if alert_interval not in ALERT_SECONDS_MAP:
        alert_interval = "10m"

    user_ids = [
        int(x.strip())
        for x in user_ids_csv.split(",")
        if x.strip().isdigit() and int(x.strip()) != PRIMARY_ADMIN_ID
    ]
    if not user_ids:
        raise HTTPException(status_code=400, detail="Kamida 1 ta xodim tanlanishi kerak.")

    stamp = now_str()
    ts_prefix = datetime.now().strftime("%Y%m%d_%H%M%S")

    sample_photo_name = None
    sample_photo_path = None
    if sample_photo and sample_photo.filename:
        sname = safe_filename(sample_photo.filename)
        spath = os.path.join(UPLOAD_DIR, f"sample_{ts_prefix}_{sname}")
        scontent = await sample_photo.read()
        with open(spath, "wb") as sf:
            sf.write(scontent)
        sample_photo_name = sname
        sample_photo_path = spath

    task_file_name = None
    task_file_path = None
    if task_file and task_file.filename:
        fname = safe_filename(task_file.filename)
        fpath = os.path.join(UPLOAD_DIR, f"task_{ts_prefix}_{fname}")
        content = await task_file.read()
        with open(fpath, "wb") as out_f:
            out_f.write(content)
        task_file_name = fname
        task_file_path = fpath

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO tasks (
                title, description, instruction, deadline, answer_type,
                alert_interval, call_message, sample_photo_name, sample_photo_path,
                task_file_name, task_file_path, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                description,
                instruction or "",
                deadline,
                answer_type,
                alert_interval,
                call_message or "",
                sample_photo_name,
                sample_photo_path,
                task_file_name,
                task_file_path,
                stamp,
            ),
        )
        task_id = cur.lastrowid

        for uid in user_ids:
            a_cur = await db.execute(
                """
                INSERT INTO task_assignments (task_id, user_id, status, last_alerted_at)
                VALUES (?, ?, 'assigned', ?)
                """,
                (task_id, uid, stamp),
            )
            assignment_id = a_cur.lastrowid
            msg_text = (
                f"🆕 <b>Yangi topshiriq biriktirildi!</b>\n"
                f"📌 <b>{title}</b>\n"
                f"📝 {description}\n"
                f"⏰ Muddat: <b>{deadline}</b>"
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📖 O‘qish va Javob berish",
                            callback_data=f"read_task:{assignment_id}",
                        )
                    ]
                ]
            )
            try:
                if sample_photo_path and os.path.exists(sample_photo_path):
                    await bot.send_photo(
                        uid,
                        photo=open(sample_photo_path, "rb"),
                        caption=f"{msg_text}\n\n🖼 <i>Namuna rasm ilova qilingan.</i>",
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                else:
                    await bot.send_message(uid, msg_text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                pass

        await db.commit()

    return {"ok": True, "task_id": task_id}


@app.post("/api/my-tasks/{assignment_id}/submit")
async def api_submit_task_from_panel(
    assignment_id: int,
    user_id: int = Form(...),
    answer_text: str = Form(default=""),
    upload_file: Optional[UploadFile] = File(default=None),
) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT ta.id, ta.user_id, t.title, t.answer_type, u.full_name, u.mfy
            FROM task_assignments ta
            JOIN tasks t ON t.id = ta.task_id
            JOIN users u ON u.user_id = ta.user_id
            WHERE ta.id = ? AND ta.user_id = ?
            """,
            (assignment_id, user_id),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Topshiriq biriktiruvi topilmadi.")

        required_type = row[3]
        has_file = bool(upload_file and upload_file.filename)
        has_text = bool((answer_text or "").strip())

        if required_type in ("photo", "file", "both") and not has_file:
            raise HTTPException(status_code=400, detail="Ushbu topshiriq uchun fayl/rasm yuklash majburiy!")
        if required_type == "text" and not has_text:
            raise HTTPException(status_code=400, detail="Ushbu topshiriq uchun javob matni yozish majburiy!")

        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        mfy_clean = safe_filename(row[5])
        saved_name = None
        saved_path = None
        saved_type = "text"

        if has_file:
            orig_name = safe_filename(upload_file.filename)
            ext = os.path.splitext(orig_name)[1]
            saved_name = f"{mfy_clean}_{stamp}{ext or '.bin'}"
            saved_path = os.path.join(UPLOAD_DIR, f"ans_{assignment_id}_{saved_name}")
            content = await upload_file.read()
            with open(saved_path, "wb") as f:
                f.write(content)
            saved_type = "photo" if ext.lower() in {".jpg", ".jpeg", ".png", ".webp"} else "document"

        submitted = now_str()
        await db.execute(
            """
            UPDATE task_assignments
            SET status='submitted', submitted_at=?, answer_text=?, file_name=?, file_path=?, file_type=?
            WHERE id=?
            """,
            (submitted, answer_text.strip(), saved_name, saved_path, saved_type, assignment_id),
        )
        await db.commit()

    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Qabul qildim", callback_data=f"approve_ans:{assignment_id}"),
                InlineKeyboardButton(text="❌ Qayta jo‘natish", callback_data=f"reject_ans:{assignment_id}"),
            ]
        ]
    )
    caption = (
        f"📥 <b>Panel orqali yangi topshiriq javobi!</b>\n"
        f"👤 Xodim: <b>{row[4]}</b> ({row[5]})\n"
        f"📌 Topshiriq: <b>{row[2]}</b>\n"
        f"🕒 Vaqt: {submitted}\n"
        f"📝 Izoh/Matn: {answer_text.strip() or '—'}\n"
        f"📎 Fayl nomi: <code>{saved_name or '—'}</code>"
    )
    try:
        if saved_path and os.path.exists(saved_path):
            await bot.send_document(
                PRIMARY_ADMIN_ID,
                document=open(saved_path, "rb"),
                caption=caption,
                parse_mode="HTML",
                reply_markup=admin_kb,
            )
        else:
            await bot.send_message(PRIMARY_ADMIN_ID, caption, parse_mode="HTML", reply_markup=admin_kb)
    except Exception:
        pass

    return {"ok": True, "file_name": saved_name}


@app.get("/api/files/sample/{task_id}")
async def api_download_sample_photo(task_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT sample_photo_name, sample_photo_path FROM tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        if not row or not row[1] or not os.path.exists(row[1]):
            raise HTTPException(status_code=404, detail="Namuna rasm topilmadi.")
        return FileResponse(path=row[1], filename=row[0] or "sample.jpg")


@app.get("/api/files/task/{task_id}")
async def api_download_task_file(task_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT task_file_name, task_file_path FROM tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        if not row or not row[1] or not os.path.exists(row[1]):
            raise HTTPException(status_code=404, detail="Topshiriq fayli topilmadi.")
        return FileResponse(path=row[1], filename=row[0] or "task_file")


@app.get("/api/files/answer/{assignment_id}")
async def api_download_answer_file(assignment_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT file_name, file_path FROM task_assignments WHERE id = ?",
            (assignment_id,),
        )
        row = await cur.fetchone()
        if not row or not row[1] or not os.path.exists(row[1]):
            raise HTTPException(status_code=404, detail="Yuklangan fayl topilmadi.")
        return FileResponse(path=row[1], filename=row[0] or "answer_file")


@app.get("/api/monitoring")
async def api_monitoring() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT ta.id, ta.status, ta.read_at, ta.submitted_at, ta.answer_text,
                   ta.file_name, ta.file_path, ta.last_reason,
                   t.id AS task_id, t.title, t.description, t.instruction, t.alert_interval,
                   t.sample_photo_name, t.sample_photo_path, t.task_file_name, t.task_file_path,
                   u.full_name, u.mfy
            FROM task_assignments ta
            JOIN tasks t ON t.id = ta.task_id
            JOIN users u ON u.user_id = ta.user_id
            ORDER BY ta.id DESC
            """
        )
        rows = await cur.fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["has_answer_file"] = bool(item.get("file_path") and os.path.exists(item["file_path"]))
            item["has_task_file"] = bool(item.get("task_file_path") and os.path.exists(item["task_file_path"]))
            item["has_sample_photo"] = bool(item.get("sample_photo_path") and os.path.exists(item["sample_photo_path"]))
            result.append(item)
        return result


@app.get("/api/my-tasks")
async def api_my_tasks(user_id: int = Query(default=0)) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT ta.id, ta.status, ta.last_reason, ta.file_name, ta.answer_text,
                   t.id AS task_id, t.title, t.description, t.instruction, t.deadline,
                   t.answer_type, t.sample_photo_name, t.sample_photo_path, t.task_file_name, t.task_file_path
            FROM task_assignments ta
            JOIN tasks t ON t.id = ta.task_id
            WHERE ta.user_id = ?
            ORDER BY ta.id DESC
            """,
            (user_id,),
        )
        rows = await cur.fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["has_task_file"] = bool(item.get("task_file_path") and os.path.exists(item["task_file_path"]))
            item["has_sample_photo"] = bool(item.get("sample_photo_path") and os.path.exists(item["sample_photo_path"]))
            result.append(item)
        return result


@app.get("/api/rejections")
async def api_rejections(user_id: Optional[int] = Query(default=None)) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if user_id and not is_admin_user(user_id):
            cur = await db.execute(
                """
                SELECT r.id, r.reason, r.rejected_date, r.rejected_at,
                       t.title AS task_title, u.full_name, u.mfy
                FROM rejections r
                JOIN tasks t ON t.id = r.task_id
                JOIN users u ON u.user_id = r.user_id
                WHERE r.user_id = ?
                ORDER BY r.id DESC
                """,
                (user_id,),
            )
        else:
            cur = await db.execute(
                """
                SELECT r.id, r.reason, r.rejected_date, r.rejected_at,
                       t.title AS task_title, u.full_name, u.mfy
                FROM rejections r
                JOIN tasks t ON t.id = r.task_id
                JOIN users u ON u.user_id = r.user_id
                ORDER BY r.id DESC
                """
            )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


@app.get("/api/export/excel")
async def api_export_excel() -> StreamingResponse:
    wb = Workbook()
    ws = wb.active
    ws.title = "Monitoring"
    ws.append([
        "ID", "Xodim", "MFY", "Topshiriq", "Topshiriq matni", "Izoh",
        "Ogohlantirish", "Holat", "O‘qilgan vaqt", "Topshirilgan vaqt",
        "Javob matni", "Fayl nomi"
    ])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT ta.id, u.full_name, u.mfy, t.title, t.description, t.instruction,
                   t.alert_interval, ta.status, ta.read_at, ta.submitted_at,
                   ta.answer_text, ta.file_name
            FROM task_assignments ta
            JOIN tasks t ON t.id = ta.task_id
            JOIN users u ON u.user_id = ta.user_id
            ORDER BY ta.id DESC
            """
        )
        rows = await cur.fetchall()
        for r in rows:
            ws.append(list(r))

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=mosy_monitoring.xlsx"},
    )