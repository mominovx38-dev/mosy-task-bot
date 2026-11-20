import os
import io
import asyncio
from datetime import datetime
from dotenv import load_dotenv

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import openpyxl

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import Database

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://example.com")
DB_PATH = os.getenv("DB_PATH", "mosy_tasks.db")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db = Database(DB_PATH)
app = FastAPI(title="MOSY Task Backend")
app.mount("/webapp", StaticFiles(directory="webapp"), name="webapp")

# Foydalanuvchi qaysi topshiriqqa javob yuborayotganini va admin qaysi topshiriqni rad etish sababini yozayotganini saqlash
active_answer_assignment: dict[int, int] = {}
pending_reject_reason: dict[int, int] = {}


class RegisterStates(StatesGroup):
    full_name = State()
    mfy = State()


def main_panel_keyboard() -> ReplyKeyboardMarkup:
    # Ekranda doimiy turadigan o'zgarmas panel tugmasi
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Boshqaruv Paneli", web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp/index.html"))]
        ],
        resize_keyboard=True,
        is_persistent=True
    )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    uid = message.from_user.id
    if is_admin(uid):
        await db.upsert_user(uid, message.from_user.full_name or "Admin", mfy="Markaz", role="admin")
        await message.answer(
            "🏛 <b>YOSHLAR ISHLARI AGENTLIGI — MOSY</b>\n"
            "Boshqaruv paneli tayyor. Pastdagi <b>📱 Boshqaruv Paneli</b> tugmasini bosing.",
            parse_mode="HTML",
            reply_markup=main_panel_keyboard()
        )
        return

    user = await db.get_user(uid)
    if not user:
        await state.set_state(RegisterStates.full_name)
        await message.answer(
            "Assalomu alaykum! <b>MOSY</b> topshiriqlar tizimiga xush kelibsiz.\n"
            "Ro‘yxatdan o‘tish uchun <b>Ism-familiyangizni</b> kiriting:",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            f"Xush kelibsiz, <b>{user['full_name']}</b> ({user['mfy']})!\n"
            "Barcha vazifalar pastdagi <b>📱 Boshqaruv Paneli</b> orqali boshqariladi.",
            parse_mode="HTML",
            reply_markup=main_panel_keyboard()
        )


@dp.message(RegisterStates.full_name)
async def reg_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text.strip())
    await state.set_state(RegisterStates.mfy)
    await message.answer("Endi qaysi <b>MFY / bo‘lim</b> vakili ekanligingizni yozing:", parse_mode="HTML")


@dp.message(RegisterStates.mfy)
async def reg_mfy(message: Message, state: FSMContext):
    data = await state.get_data()
    mfy = message.text.strip()
    await db.upsert_user(message.from_user.id, data["full_name"], mfy=mfy, role="worker")
    await state.clear()
    await message.answer(
        f"✅ Ro‘yxatdan o‘tdingiz!\n<b>F.I.Sh:</b> {data['full_name']}\n<b>MFY:</b> {mfy}",
        parse_mode="HTML",
        reply_markup=main_panel_keyboard()
    )


@dp.callback_query(F.data.startswith("read:"))
async def cb_mark_read(callback: CallbackQuery):
    assignment_id = int(callback.data.split(":")[1])
    await db.mark_read(assignment_id)
    active_answer_assignment[callback.from_user.id] = assignment_id
    info = await db.get_assignment(assignment_id)
    fmt_map = {
        "photo": "🖼 FAQAT RASM",
        "file": "📎 FAQAT FAYL (Excel/Word/PDF)",
        "both": "🖼📎 RASM VA FAYL",
        "text": "✍️ FAQAT MATN"
    }
    await callback.answer("Topshiriq o‘qildi deb belgilandi!", show_alert=False)
    await callback.message.answer(
        f"📌 <b>#{info['task_id']} — {info['title']}</b>\n"
        f"Talab qilinadigan javob formati: <b>{fmt_map.get(info['answer_type'], info['answer_type'])}</b>\n\n"
        "Endi javobingizni shu chatga yuboring:",
        parse_mode="HTML",
        reply_markup=main_panel_keyboard()
    )


@dp.callback_query(F.data.startswith("approve:"))
async def cb_approve(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    assignment_id = int(callback.data.split(":")[1])
    await db.approve_assignment(assignment_id)
    info = await db.get_assignment(assignment_id)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ <b>{info['full_name']}</b> ({info['mfy']}) topshirig‘i qabul qilindi.", parse_mode="HTML")
    await bot.send_message(
        info["user_id"],
        f"✅ <b>Topshiriq qabul qilindi!</b>\n<b>Vazifa:</b> {info['title']}",
        parse_mode="HTML",
        reply_markup=main_panel_keyboard()
    )


@dp.callback_query(F.data.startswith("reject:"))
async def cb_reject_ask_reason(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    assignment_id = int(callback.data.split(":")[1])
    pending_reject_reason[callback.from_user.id] = assignment_id
    await callback.answer()
    await callback.message.answer(
        "❌ <b>Qayta jo‘natish sababini yozing:</b>\nXodimga shu sabab bilan qayta yuboriladi.",
        parse_mode="HTML"
    )


@dp.message()
async def handle_general_messages(message: Message):
    uid = message.from_user.id

    # 1. Admin rad etish sababini yozayotgan bo'lsa
    if is_admin(uid) and uid in pending_reject_reason:
        assignment_id = pending_reject_reason.pop(uid)
        reason = (message.text or "").strip() or "Kamchiliklar mavjud"
        await db.reject_assignment(assignment_id, reason)
        info = await db.get_assignment(assignment_id)

        await message.answer(
            f"♻️ Sabab saqlandi va <b>{info['full_name']}</b> ({info['mfy']}) ga qayta yuborildi.",
            parse_mode="HTML"
        )
        await bot.send_message(
            info["user_id"],
            f"Sizning bergan topshirigingiz shu sababga ko‘ra rad etildi: <b>{reason}</b>\n\n"
            f"📌 <b>Topshiriq:</b> {info['title']}\n"
            f"🔁 <b>Qayta jo‘natish:</b> Pastdagi <b>O‘qish va Javob berish</b> tugmasini bosib qayta yuklang.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔁 Qayta jo‘natish", callback_data=f"read:{assignment_id}")]
            ])
        )
        return

    # 2. Xodim javob yuborayotgan bo'lsa — topshiriq talab qilgan formatni tekshiramiz
    if uid in active_answer_assignment:
        assignment_id = active_answer_assignment[uid]
        info = await db.get_assignment(assignment_id)
        if not info:
            return
        req_type = info["answer_type"]
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mfy_clean = info["mfy"].replace(" ", "_")

        has_photo = bool(message.photo)
        has_doc = bool(message.document)
        has_text = bool(message.text and not message.text.startswith("/"))

        # Format cheklovi
        if req_type == "photo" and not has_photo:
            await message.answer("⚠️ Bu topshiriq uchun <b>FAQAT RASM</b> yuklash mumkin!", parse_mode="HTML")
            return
        if req_type == "file" and not has_doc:
            await message.answer("⚠️ Bu topshiriq uchun <b>FAQAT FAYL</b> (Excel/Word/PDF) yuklash mumkin!", parse_mode="HTML")
            return
        if req_type == "both" and not (has_photo or has_doc):
            await message.answer("⚠️ Bu topshiriq uchun <b>RASM yoki FAYL</b> yuklang!", parse_mode="HTML")
            return
        if req_type == "text" and not has_text:
            await message.answer("⚠️ Bu topshiriq uchun <b>FAQAT MATN</b> yuborish mumkin!", parse_mode="HTML")
            return

        file_id = ""
        file_name = ""
        file_kind = ""
        ans_text = message.text or message.caption or ""

        if has_photo:
            file_id = message.photo[-1].file_id
            file_name = f"{mfy_clean}_{now_str.replace(':', '-')}.jpg"
            file_kind = "photo"
        elif has_doc:
            file_id = message.document.file_id
            orig_name = message.document.file_name or "file"
            file_name = f"{mfy_clean}_{now_str.replace(':', '-')}_{orig_name}"
            file_kind = "document"

        await db.submit_answer(
            assignment_id=assignment_id,
            answer_text=ans_text,
            file_id=file_id,
            file_name=file_name,
            file_kind=file_kind
        )
        active_answer_assignment.pop(uid, None)

        await message.answer(
            "✅ Javobingiz qabul qilindi va adminga yuborildi!",
            reply_markup=main_panel_keyboard()
        )

        # Adminga MFY va aniq vaqt bilan yuborish
        admin_caption = (
            f"📥 <b>Yangi topshiriq javobi!</b>\n"
            f"🏘 <b>MFY:</b> {info['mfy']}\n"
            f"👤 <b>Xodim:</b> {info['full_name']}\n"
            f"📌 <b>Topshiriq:</b> {info['title']}\n"
            f"🕒 <b>Vaqt:</b> {now_str}\n"
            f"📝 <b>Izoh/Matn:</b> {ans_text or '—'}"
        )
        review_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Qabul qildim", callback_data=f"approve:{assignment_id}"),
                InlineKeyboardButton(text="❌ Qayta jo‘natish", callback_data=f"reject:{assignment_id}")
            ]
        ])

        for adm_id in ADMIN_IDS:
            if file_kind == "photo":
                await bot.send_photo(adm_id, file_id, caption=admin_caption, parse_mode="HTML", reply_markup=review_kb)
            elif file_kind == "document":
                await bot.send_document(adm_id, file_id, caption=admin_caption, parse_mode="HTML", reply_markup=review_kb)
            else:
                await bot.send_message(adm_id, admin_caption, parse_mode="HTML", reply_markup=review_kb)
        return

    # Agar boshqa matn yozilsa, o'zgarmas panelni ko'rsatish
    await message.answer(
        "Barcha amallar uchun pastdagi <b>📱 Boshqaruv Paneli</b> tugmasidan foydalaning.",
        parse_mode="HTML",
        reply_markup=main_panel_keyboard()
    )


# ----------------- HAR 1 SOATLIK ESLATMA VA 10 DAQIQALIK NAZORAT -----------------
async def hourly_reminder_job():
    pending = await db.get_pending_hourly_reminders()
    now = datetime.now()
    for item in pending:
        last_str = item["last_reminded_at"] or item["created_at"]
        try:
            last_dt = datetime.strptime(last_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if (now - last_dt).total_seconds() >= 3600:
            try:
                await bot.send_message(
                    item["user_id"],
                    f"⏰ <b>Soatlik eslatma!</b>\n"
                    f"Topshiriq hali bajarilmagan: <b>{item['title']}</b>\n"
                    f"Muddat: <b>{item['deadline']}</b>\n"
                    f"Iltimos, topshiriqni bajaring!",
                    parse_mode="HTML",
                    reply_markup=main_panel_keyboard()
                )
                await db.mark_reminded(item["assignment_id"])
            except Exception:
                pass


async def ten_min_unread_check_job():
    unread = await db.get_unread_over_10_min()
    now = datetime.now()
    for item in unread:
        try:
            created_dt = datetime.strptime(item["created_at"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if (now - created_dt).total_seconds() >= 600:
            await db.mark_call_triggered(item["assignment_id"])
            call_txt = item["call_message"] or f"Sizga '{item['title']}' topshirig‘i yuborilgan, hali o‘qilmadi!"
            # 2-qadamda qo'shiladigan Avto-Qo'ng'iroq (Pyrogram + TTS) hook nuqtasi:
            for adm_id in ADMIN_IDS:
                try:
                    await bot.send_message(
                        adm_id,
                        f"📞 <b>10 daqiqa o‘tdi — o‘qilmadi!</b>\n"
                        f"Xodim: <b>{item['full_name']}</b> ({item['mfy']})\n"
                        f"Topshiriq: <b>{item['title']}</b>\n"
                        f"Qo‘ng‘iroq matni: <i>{call_txt}</i>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass


# ----------------- FASTAPI ENDPOINTS (MINI APP & EXCEL EXPORT) -----------------
@app.get("/", response_class=HTMLResponse)
async def root_page():
    with open("webapp/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/me")
async def api_me(user_id: int):
    return {"user_id": user_id, "is_admin": is_admin(user_id)}


@app.get("/api/workers")
async def api_workers():
    return await db.get_all_workers()


@app.post("/api/tasks")
async def api_create_task(req: Request):
    body = await req.json()
    if not is_admin(int(body.get("admin_id", 0))):
        return JSONResponse({"ok": False, "error": "Ruxsat yo‘q"}, status_code=403)

    user_ids = [int(x) for x in body.get("user_ids", [])]
    task_id = await db.create_task_with_assignments(
        title=body["title"],
        description=body["description"],
        instruction=body.get("instruction", ""),
        deadline=body["deadline"],
        answer_type=body.get("answer_type", "text"),
        call_message=body.get("call_message", ""),
        user_ids=user_ids
    )

    # Har bir tanlangan xodimga topshiriqni yuborish
    async with __import__("aiosqlite").connect(DB_PATH) as conn:
        conn.row_factory = __import__("aiosqlite").Row
        cur = await conn.execute("SELECT id, user_id FROM assignments WHERE task_id = ?", (task_id,))
        assign_rows = await cur.fetchall()

    for row in assign_rows:
        msg_text = (
            f"🆕 <b>Yangi topshiriq #{task_id}</b>\n"
            f"📌 <b>{body['title']}</b>\n\n"
            f"📝 <b>Mazmuni:</b> {body['description']}\n"
            f"🛠 <b>Qanday qilinadi:</b> {body.get('instruction') or 'Mazmunga muvofiq'}\n"
            f"⏳ <b>Muddat:</b> {body['deadline']}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👁 O‘qish va Javob berish", callback_data=f"read:{row['id']}")]
        ])
        try:
            await bot.send_message(row["user_id"], msg_text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    return {"ok": True, "task_id": task_id}


@app.get("/api/monitoring")
async def api_monitoring():
    return await db.get_monitoring_rows()


@app.get("/api/my-tasks")
async def api_my_tasks(user_id: int):
    return await db.get_user_assignments(user_id=user_id)


@app.get("/api/rejections")
async def api_rejections(user_id: int | None = None):
    return await db.get_rejections_history(user_id=user_id)


@app.get("/api/export/excel")
async def api_export_excel():
    rows = await db.get_monitoring_rows()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Monitoring"
    ws.append(["Topshiriq", "MFY", "Xodim", "Holat", "O‘qilgan vaqt", "Topshirilgan vaqt", "Fayl nomi", "Javob matni"])
    for r in rows:
        ws.append([
            r["title"], r["mfy"], r["full_name"], r["status"],
            r["read_at"] or "", r["submitted_at"] or "",
            r["file_name"] or "", r["answer_text"] or ""
        ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=mosy_monitoring.xlsx"}
    )


@app.on_event("startup")
async def on_startup():
    await db.init()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(hourly_reminder_job, "interval", minutes=5)
    scheduler.add_job(ten_min_unread_check_job, "interval", seconds=30)
    scheduler.start()
    asyncio.create_task(dp.start_polling(bot))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)