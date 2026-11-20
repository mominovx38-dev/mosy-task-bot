import io
import os
from datetime import datetime
from typing import List, Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
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
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook
from pydantic import BaseModel

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DEFAULT_ADMIN_ID = 9020389495

raw_admins = os.getenv("ADMIN_IDS", str(DEFAULT_ADMIN_ID)).strip()
ADMIN_IDS = {
    int(x.strip())
    for x in raw_admins.split(",")
    if x.strip().isdigit()
}
ADMIN_IDS.add(DEFAULT_ADMIN_ID)

DB_PATH = os.getenv("DB_PATH", "mosy_tasks.db").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://mosy-task-bot-1.onrender.com").strip().rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi. Environment sozlamalarini tekshiring.")

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
    if user_id is None:
        return False
    return int(user_id) in ADMIN_IDS


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def main_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="📱 Boshqaruv Paneli",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}/"),
                )
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
                call_message TEXT DEFAULT '',
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
                last_reminded_at TEXT,
                called_10m INTEGER NOT NULL DEFAULT 0,
                answer_text TEXT,
                file_id TEXT,
                file_name TEXT,
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
        await db.commit()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user_id = message.from_user.id

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
            await db.commit()
            await message.answer(
                "✅ <b>Admin huquqi faollashtirildi!</b>\n"
                "Pastdagi <b>📱 Boshqaruv Paneli</b> tugmasini bosib topshiriq bering va monitoring qiling.",
                parse_mode="HTML",
                reply_markup=main_reply_kb(),
            )
            return

        cur = await db.execute("SELECT full_name, mfy FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()

    if row:
        await message.answer(
            f"👋 Xush kelibsiz, <b>{row[0]}</b> ({row[1]})!\n"
            "Vazifalarni ko‘rish va javob yuborish uchun pastdagi <b>📱 Boshqaruv Paneli</b> tugmasini bosing.",
            parse_mode="HTML",
            reply_markup=main_reply_kb(),
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
    if len(full_name) < 3:
        await message.answer("Iltimos, to‘liq ism-familiyangizni kiriting:")
        return
    await state.update_data(full_name=full_name)
    await message.answer("Qaysi <b>MFY</b> da faoliyat yuritishingizni yozing:", parse_mode="HTML")
    await state.set_state(RegStates.waiting_mfy)


@router.message(RegStates.waiting_mfy)
async def reg_mfy(message: Message, state: FSMContext) -> None:
    mfy = (message.text or "").strip()
    if len(mfy) < 2:
        await message.answer("Iltimos, MFY nomini kiriting:")
        return
    data = await state.get_data()
    full_name = data["full_name"]
    user_id = message.from_user.id
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
    await message.answer(
        f"✅ Ro‘yxatdan o‘tdingiz: <b>{full_name}</b> ({mfy}).\n"
        "Endi <b>📱 Boshqaruv Paneli</b> tugmasidan foydalanishingiz mumkin.",
        parse_mode="HTML",
        reply_markup=main_reply_kb(),
    )


@router.callback_query(F.data.startswith("read_task:"))
async def cb_read_task(callback: CallbackQuery) -> None:
    assignment_id = int(callback.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT ta.id, ta.status, ta.read_at, t.title, t.description, t.instruction, t.deadline, t.answer_type
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
    await callback.message.answer(
        f"📌 <b>{row[3]}</b>\n\n"
        f"📝 <b>Mazmuni:</b> {row[4]}\n"
        f"🛠 <b>Bajarish tartibi:</b> {row[5] or 'Ko‘rsatilmagan'}\n"
        f"⏰ <b>Muddat:</b> {row[6]}\n"
        f"📎 <b>Talab etiladigan format:</b> {type_labels.get(row[7], row[7])}\n\n"
        "Javob yuborish uchun quyidagi <b>✍️ Javob yuborish</b> tugmasini bosing.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✍️ Javob yuborish", callback_data=f"ans_task:{assignment_id}")]
            ]
        ),
    )
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
        mfy_clean = row[5].replace(" ", "_")
        file_id = None
        file_name = None
        file_type = None
        answer_text = message.caption or message.text or ""

        if has_photo:
            file_id = message.photo[-1].file_id
            file_name = f"{mfy_clean}_{stamp}.jpg"
            file_type = "photo"
        elif has_doc:
            file_id = message.document.file_id
            orig_name = message.document.file_name or "fayl"
            ext = os.path.splitext(orig_name)[1]
            file_name = f"{mfy_clean}_{stamp}{ext}"
            file_type = "document"
        else:
            file_type = "text"

        submitted = now_str()
        await db.execute(
            """
            UPDATE task_assignments
            SET status='submitted', submitted_at=?, answer_text=?, file_id=?, file_name=?, file_type=?
            WHERE id=?
            """,
            (submitted, answer_text, file_id, file_name, file_type, assignment_id),
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

    for adm_id in ADMIN_IDS:
        try:
            if file_type == "photo" and file_id:
                await bot.send_photo(adm_id, file_id, caption=caption, parse_mode="HTML", reply_markup=admin_kb)
            elif file_type == "document" and file_id:
                await bot.send_document(
                    adm_id,
                    file_id,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=admin_kb,
                )
            else:
                await bot.send_message(adm_id, caption, parse_mode="HTML", reply_markup=admin_kb)
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
            SELECT ta.id, ta.user_id, ta.status, ta.read_at, ta.last_reminded_at, ta.called_10m,
                   t.title, t.deadline, t.call_message, u.full_name, u.mfy
            FROM task_assignments ta
            JOIN tasks t ON t.id = ta.task_id
            JOIN users u ON u.user_id = ta.user_id
            WHERE ta.status IN ('assigned', 'read', 'rejected')
            """
        )
        rows = await cur.fetchall()
        now_dt = datetime.now()

        for row in rows:
            a_id, u_id, status, read_at_s, last_rem_s, called_10m, title, deadline, call_msg, full_name, mfy = row

            # 10 daqiqada o'qilmagan topshiriq nazorati (keyingi bosqichda haqiqiy qo'ng'iroqqa ulanadi)
            if not read_at_s and not called_10m:
                try:
                    created_dt = datetime.strptime(t_created := now_str(), "%Y-%m-%d %H:%M")
                    _ = created_dt
                except Exception:
                    pass
                cur_t = await db.execute("SELECT created_at FROM tasks WHERE id = (SELECT task_id FROM task_assignments WHERE id=?)", (a_id,))
                t_row = await cur_t.fetchone()
                if t_row:
                    try:
                        created_dt = datetime.strptime(t_row[0], "%Y-%m-%d %H:%M")
                        if (now_dt - created_dt).total_seconds() >= 600:
                            await db.execute("UPDATE task_assignments SET called_10m = 1 WHERE id = ?", (a_id,))
                            await db.commit()
                            spoken = call_msg or f"{full_name}, sizga «{title}» topshirig‘i yuborilgan, hali o‘qilmadi!"
                            try:
                                await bot.send_message(
                                    u_id,
                                    f"📞 <b>10 daqiqalik shoshilinch ogohlantirish!</b>\n"
                                    f"Topshiriq: <b>{title}</b>\n"
                                    f"Ovozli xabar matni: <i>{spoken}</i>",
                                    parse_mode="HTML",
                                )
                            except Exception:
                                pass
                            for adm_id in ADMIN_IDS:
                                try:
                                    await bot.send_message(
                                        adm_id,
                                        f"⚠️ <b>10 daqiqada o‘qilmadi:</b> {full_name} ({mfy}) — «{title}».",
                                        parse_mode="HTML",
                                    )
                                except Exception:
                                    pass
                    except Exception:
                        pass

            # Har 1 soatda bajarilmagan topshiriqni eslatish
            should_remind = False
            if not last_rem_s:
                should_remind = True
            else:
                try:
                    last_dt = datetime.strptime(last_rem_s, "%Y-%m-%d %H:%M")
                    if (now_dt - last_dt).total_seconds() >= 3600:
                        should_remind = True
                except Exception:
                    should_remind = True

            if should_remind and status in ("assigned", "read", "rejected"):
                await db.execute("UPDATE task_assignments SET last_reminded_at = ? WHERE id = ?", (now_str(), a_id))
                await db.commit()
                try:
                    await bot.send_message(
                        u_id,
                        f"⏰ <b>Eslatma (har 1 soatlik nazorat):</b>\n"
                        f"Topshiriq: <b>{title}</b>\n"
                        f"Muddat: <b>{deadline}</b>\n"
                        f"Holat: <b>{status}</b>",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="📖 O‘qish va Javob berish", callback_data=f"read_task:{a_id}")]
                            ]
                        ),
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


class CreateTaskPayload(BaseModel):
    admin_id: Optional[int] = DEFAULT_ADMIN_ID
    title: str
    description: str
    instruction: Optional[str] = ""
    deadline: str
    answer_type: str = "photo"
    call_message: Optional[str] = ""
    user_ids: List[int]


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
    import asyncio
    asyncio.create_task(dp.start_polling(bot, handle_signals=False))


@app.get("/", response_class=HTMLResponse)
async def serve_webapp() -> HTMLResponse:
    with open("webapp/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/api/me")
async def api_me(user_id: int = Query(default=DEFAULT_ADMIN_ID)) -> dict:
    return {"user_id": user_id, "is_admin": is_admin_user(user_id)}


@app.get("/api/workers")
async def api_workers() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id, full_name, mfy FROM users WHERE role != 'admin' ORDER BY full_name ASC"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


@app.post("/api/tasks")
async def api_create_task(payload: CreateTaskPayload) -> dict:
    if not payload.user_ids:
        raise HTTPException(status_code=400, detail="Kamida 1 ta xodim tanlanishi kerak.")

    stamp = now_str()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO tasks (title, description, instruction, deadline, answer_type, call_message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.title,
                payload.description,
                payload.instruction or "",
                payload.deadline,
                payload.answer_type,
                payload.call_message or "",
                stamp,
            ),
        )
        task_id = cur.lastrowid

        for uid in payload.user_ids:
            a_cur = await db.execute(
                """
                INSERT INTO task_assignments (task_id, user_id, status, last_reminded_at)
                VALUES (?, ?, 'assigned', ?)
                """,
                (task_id, uid, stamp),
            )
            assignment_id = a_cur.lastrowid
            try:
                await bot.send_message(
                    uid,
                    f"🆕 <b>Yangi topshiriq biriktirildi!</b>\n"
                    f"📌 <b>{payload.title}</b>\n"
                    f"📝 {payload.description}\n"
                    f"⏰ Muddat: <b>{payload.deadline}</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="📖 O‘qish va Javob berish",
                                    callback_data=f"read_task:{assignment_id}",
                                )
                            ]
                        ]
                    ),
                )
            except Exception:
                pass

        await db.commit()

    return {"ok": True, "task_id": task_id}


@app.get("/api/monitoring")
async def api_monitoring() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT ta.id, ta.status, ta.read_at, ta.submitted_at, ta.file_name,
                   t.title, u.full_name, u.mfy
            FROM task_assignments ta
            JOIN tasks t ON t.id = ta.task_id
            JOIN users u ON u.user_id = ta.user_id
            ORDER BY ta.id DESC
            """
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


@app.get("/api/my-tasks")
async def api_my_tasks(user_id: int = Query(default=0)) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT ta.id, ta.status, ta.last_reason,
                   t.title, t.description, t.instruction, t.deadline, t.answer_type
            FROM task_assignments ta
            JOIN tasks t ON t.id = ta.task_id
            WHERE ta.user_id = ?
            ORDER BY ta.id DESC
            """,
            (user_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


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
    ws.append(["ID", "Xodim", "MFY", "Topshiriq", "Holat", "O‘qilgan vaqt", "Topshirilgan vaqt", "Fayl nomi"])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT ta.id, u.full_name, u.mfy, t.title, ta.status, ta.read_at, ta.submitted_at, ta.file_name
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