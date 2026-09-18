import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
from urllib.parse import parse_qsl
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Asia/Tomsk").strip()
NOTIFICATION_TIME = os.getenv("NOTIFICATION_TIME", "08:00").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "3000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not OWNER_ID_RAW.isdigit():
    raise RuntimeError("OWNER_ID must be a numeric Telegram ID")
OWNER_ID = int(OWNER_ID_RAW)

try:
    LOCAL_TZ = ZoneInfo(BOT_TIMEZONE)
except Exception:
    raise RuntimeError(f"Invalid BOT_TIMEZONE: {BOT_TIMEZONE}")

try:
    NOTIFY_HOUR, NOTIFY_MINUTE = [int(x) for x in NOTIFICATION_TIME.split(":", 1)]
    if not (0 <= NOTIFY_HOUR <= 23 and 0 <= NOTIFY_MINUTE <= 59):
        raise ValueError
except ValueError:
    raise RuntimeError("NOTIFICATION_TIME must be HH:MM")

os.makedirs("data", exist_ok=True)
DB_PATH = "data/bot.sqlite3"
logging.basicConfig(level=logging.INFO)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today():
    return datetime.now(LOCAL_TZ).date().isoformat()


def escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def validate_text(value, label, max_len=4000):
    value = (value or "").strip()
    if not value:
        return None, f"{label} не должен быть пустым."
    if len(value) > max_len:
        return None, f"{label} слишком длинный. Максимум — {max_len} символов."
    return value, None


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS admins (
        telegram_id INTEGER PRIMARY KEY,
        added_by INTEGER NOT NULL,
        added_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS daily_reading (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reading_date TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        reference TEXT NOT NULL,
        description TEXT,
        question TEXT NOT NULL,
        created_by INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS reflections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        reading_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        published_at TEXT,
        UNIQUE(telegram_id, reading_id)
    );

    CREATE TABLE IF NOT EXISTS edit_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reflection_id INTEGER NOT NULL,
        telegram_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS history_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        reference TEXT,
        position INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS reading_views (
        telegram_id INTEGER NOT NULL,
        reading_date TEXT NOT NULL,
        opened_at TEXT NOT NULL,
        PRIMARY KEY (telegram_id, reading_date)
    );

    CREATE TABLE IF NOT EXISTS notification_log (
        reading_date TEXT PRIMARY KEY,
        sent_at TEXT NOT NULL
    );
    """)
    # Migration for existing installations.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_reading)").fetchall()}
    if "text" not in columns:
        conn.execute("ALTER TABLE daily_reading ADD COLUMN text TEXT")

    conn.execute(
        "INSERT OR IGNORE INTO admins(telegram_id, added_by, added_at) VALUES (?, ?, ?)",
        (OWNER_ID, OWNER_ID, now()),
    )
    conn.commit()
    conn.close()


def register_user(user_id):
    conn = db()
    conn.execute("INSERT OR IGNORE INTO users(telegram_id, created_at) VALUES (?, ?)", (user_id, now()))
    conn.commit()
    conn.close()


def is_admin(user_id):
    conn = db()
    row = conn.execute("SELECT 1 FROM admins WHERE telegram_id=?", (user_id,)).fetchone()
    conn.close()
    return row is not None


def is_owner(user_id):
    return user_id == OWNER_ID


def get_today_reading():
    conn = db()
    row = conn.execute("SELECT * FROM daily_reading WHERE reading_date=?", (today(),)).fetchone()
    conn.close()
    return row


def get_reflection(reflection_id):
    conn = db()
    row = conn.execute("SELECT * FROM reflections WHERE id=?", (reflection_id,)).fetchone()
    conn.close()
    return row


def get_pending_reflections():
    conn = db()
    rows = conn.execute("""
        SELECT r.*, d.title, d.reference
        FROM reflections r
        JOIN daily_reading d ON d.id=r.reading_id
        WHERE r.status='pending'
        ORDER BY r.created_at
    """).fetchall()
    conn.close()
    return rows


def get_published_reflections(reading_id):
    conn = db()
    rows = conn.execute("""
        SELECT id, text, published_at
        FROM reflections
        WHERE reading_id=? AND status='published'
        ORDER BY published_at DESC
    """, (reading_id,)).fetchall()
    conn.close()
    return rows


def mark_reading_viewed(user_id, reading_date):
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO reading_views(telegram_id, reading_date, opened_at) VALUES (?, ?, ?)",
        (user_id, reading_date, now()),
    )
    conn.commit()
    conn.close()


def split_text(text, limit=3900):
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < 500:
            cut = text.rfind(" ", 0, limit)
        if cut < 500:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def main_keyboard():
    rows = []
    if WEBAPP_URL:
        rows.append([InlineKeyboardButton(text="✨ Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL))])
    rows.extend([
        [InlineKeyboardButton(text="📖 Что читаем сегодня", callback_data="today")],
        [InlineKeyboardButton(text="💭 Моё размышление", callback_data="my_reflection")],
        [InlineKeyboardButton(text="👥 Что думают другие", callback_data="others")],
        [InlineKeyboardButton(text="📜 Библия — это история", callback_data="history")],
        [InlineKeyboardButton(text="📊 Мой прогресс", callback_data="progress")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_keyboard(owner=False):
    rows = [
        [InlineKeyboardButton(text="📖 Сегодня", callback_data="adm_today")],
        [InlineKeyboardButton(text="🛡 Модерация", callback_data="adm_moderation")],
        [InlineKeyboardButton(text="📜 История", callback_data="adm_history")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
    ]
    if owner:
        rows.append([InlineKeyboardButton(text="👑 Администраторы", callback_data="adm_admins")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_menu_for(user_id):
    return admin_keyboard(is_owner(user_id))


def today_text():
    reading = get_today_reading()
    if not reading:
        return "📖 <b>Сегодняшнее чтение</b>\n\nАдминистратор ещё не добавил чтение на сегодня."
    description = reading["description"] or "Дополнительного контекста нет."
    passage = reading["text"] if "text" in reading.keys() else None
    passage_block = f"\n\n📜 <b>Текст отрывка</b>\n{escape(passage)}" if passage else ""
    return (
        f"📖 <b>{escape(reading['title'])}</b>\n\n"
        f"📚 {escape(reading['reference'])}\n\n"
        f"{escape(description)}{passage_block}\n\n"
        f"❓ <b>Вопрос дня</b>\n{escape(reading['question'])}"
    )


dp = Dispatcher()


# -------------------- FSM --------------------

class ReflectionState(StatesGroup):
    waiting_text = State()


class EditRequestState(StatesGroup):
    waiting_text = State()


class AdminReadingState(StatesGroup):
    waiting_title = State()
    waiting_reference = State()
    waiting_description = State()
    waiting_text = State()
    waiting_question = State()


class AdminHistoryState(StatesGroup):
    waiting_title = State()
    waiting_description = State()
    waiting_reference = State()


class AdminHistoryEditState(StatesGroup):
    waiting_title = State()
    waiting_description = State()
    waiting_reference = State()


class AdminAddState(StatesGroup):
    waiting_id = State()


class AdminEditReflectionState(StatesGroup):
    waiting_text = State()


async def safe_edit(callback: CallbackQuery, text: str, markup=None):
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)


# -------------------- User --------------------

@dp.message(CommandStart())
async def start(message: Message):
    register_user(message.from_user.id)
    await message.answer(
        "📖 <b>Добро пожаловать</b>\n\n"
        "Здесь можно читать заданный на сегодня отрывок Библии, "
        "размышлять над ним и анонимно читать мысли других.",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "ℹ️ <b>Помощь</b>\n\n"
        "📖 Чтение — сегодняшнее чтение и вопрос дня.\n"
        "💭 Размышление — отправка своих мыслей на модерацию.\n"
        "👥 Что думают другие — опубликованные размышления без данных автора.\n"
        "📜 Библия — это история — исторические события, добавленные администрацией.\n"
        "📊 Мой прогресс — дни, когда ты открывал чтение, и статистика размышлений."
    )


@dp.callback_query(F.data == "today")
async def today_handler(callback: CallbackQuery):
    await callback.answer()
    reading = get_today_reading()
    if reading:
        mark_reading_viewed(callback.from_user.id, reading["reading_date"])
    await safe_edit(callback, today_text(), InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💭 Написать размышление", callback_data="write_reflection")],
        [InlineKeyboardButton(text="👥 Что думают другие", callback_data="others")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="home")],
    ]))


@dp.callback_query(F.data == "write_reflection")
async def write_reflection(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    reading = get_today_reading()
    if not reading:
        await callback.message.answer("На сегодня чтение ещё не задано.")
        return
    conn = db()
    existing = conn.execute(
        "SELECT status FROM reflections WHERE telegram_id=? AND reading_id=?",
        (callback.from_user.id, reading["id"]),
    ).fetchone()
    conn.close()
    if existing and existing["status"] == "pending":
        await callback.message.answer("⏳ Твоё размышление уже находится на модерации.")
        return
    if existing and existing["status"] == "published":
        await callback.message.answer("Твоё размышление уже опубликовано. Если нужно что-то изменить, используй кнопку «Попросить изменить» в разделе «Моё размышление».")
        return
    await state.set_state(ReflectionState.waiting_text)
    await callback.message.answer(
        "💭 <b>Твоё размышление</b>\n\n"
        "Напиши свои мысли по сегодняшнему чтению.\n"
        "После отправки они попадут на модерацию."
    )


@dp.message(ReflectionState.waiting_text)
async def save_reflection(message: Message, state: FSMContext):
    reading = get_today_reading()
    if not reading:
        await message.answer("На сегодня чтение ещё не задано.")
        await state.clear()
        return
    text, error = validate_text(message.text, "Размышление")
    if error:
        await message.answer(error)
        return

    conn = db()
    existing = conn.execute(
        "SELECT id, status FROM reflections WHERE telegram_id=? AND reading_id=?",
        (message.from_user.id, reading["id"]),
    ).fetchone()
    if existing and existing["status"] == "pending":
        conn.close()
        await state.clear()
        await message.answer("⏳ У тебя уже есть размышление на модерации.")
        return
    if existing and existing["status"] == "published":
        conn.close()
        await state.clear()
        await message.answer("Твоё размышление уже опубликовано. Для изменения отправь запрос администратору.")
        return

    # Reuse a previously rejected reflection so the UNIQUE constraint does not block resubmission.
    if existing and existing["status"] == "rejected":
        conn.execute(
            "UPDATE reflections SET text=?, status='pending', created_at=?, published_at=NULL WHERE id=?",
            (text, now(), existing["id"]),
        )
        reflection_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO reflections(telegram_id, reading_id, text, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (message.from_user.id, reading["id"], text, now()),
        )
        reflection_id = cur.lastrowid
    conn.commit()
    conn.close()
    await state.clear()

    await message.answer("✅ Размышление отправлено на модерацию.\n\nЕсли администратор одобрит его, оно появится в разделе «Что думают другие» анонимно.")
    await notify_admins_about_reflection(message.bot, reflection_id)


async def notify_admins_about_reflection(bot: Bot, reflection_id: int):
    conn = db()
    row = conn.execute("""
        SELECT r.*, d.title, d.reference
        FROM reflections r JOIN daily_reading d ON d.id=r.reading_id
        WHERE r.id=?
    """, (reflection_id,)).fetchone()
    admins = conn.execute("SELECT telegram_id FROM admins").fetchall()
    conn.close()
    if not row:
        return
    header = (
        "🛡 <b>Новое размышление</b>\n\n"
        f"📖 {escape(row['title'])}\n"
        f"📚 {escape(row['reference'])}\n\n"
        f"🔐 Telegram ID автора: <code>{row['telegram_id']}</code>"
    )
    reflection_text = f"💭 <b>Текст размышления</b>\n{escape(row['text'])}"
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"approve:{reflection_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{reflection_id}"),
    ]])
    for admin in admins:
        try:
            await bot.send_message(admin["telegram_id"], header)
            await bot.send_message(admin["telegram_id"], reflection_text, reply_markup=markup)
        except Exception:
            logging.exception("Could not notify admin %s", admin["telegram_id"])


@dp.callback_query(F.data == "others")
async def others_handler(callback: CallbackQuery):
    await callback.answer()
    reading = get_today_reading()
    if not reading:
        await safe_edit(callback, "Сегодняшнее чтение ещё не задано.", InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
        ]))
        return
    rows = get_published_reflections(reading["id"])
    if not rows:
        text = f"👥 <b>Что думают другие</b>\n\nПо теме «{escape(reading['title'])}» пока нет опубликованных размышлений."
        await safe_edit(callback, text, InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Главное меню", callback_data="home")]]))
        return

    # Keep each Telegram message below the platform limit while preserving full reflection text.
    header = f"👥 <b>Что думают другие</b>\n\nОпубликовано размышлений: {len(rows)}\n"
    chunks = []
    current = header
    for i, row in enumerate(rows[:10], 1):
        block = f"\n<b>💭 Размышление {i}</b>\n{escape(row['text'])}\n"
        if len(current) + len(block) > 3900 and current != header:
            chunks.append(current)
            current = ""
        current += block
    if current:
        chunks.append(current)

    await safe_edit(callback, chunks[0], None if len(chunks) > 1 else InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="home")]
    ]))
    for chunk in chunks[1:]:
        await callback.message.answer(chunk)
    if len(chunks) > 1:
        await callback.message.answer("⬅️", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="home")]
        ]))


@dp.callback_query(F.data == "my_reflection")
async def my_reflection_handler(callback: CallbackQuery):
    await callback.answer()
    reading = get_today_reading()
    if not reading:
        await safe_edit(callback, "Сегодняшнее чтение ещё не задано.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]]))
        return
    conn = db()
    row = conn.execute("SELECT * FROM reflections WHERE telegram_id=? AND reading_id=?", (callback.from_user.id, reading["id"])).fetchone()
    conn.close()
    if not row:
        text = "💭 Сегодня ты ещё не отправлял размышление."
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Написать", callback_data="write_reflection")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")],
        ])
    else:
        status = {"pending": "⏳ На модерации", "published": "✅ Опубликовано анонимно", "rejected": "❌ Отклонено"}.get(row["status"], row["status"])
        text = f"💭 <b>Твоё сегодняшнее размышление</b>\n\n{escape(row['text'])}\n\n<b>Статус:</b> {status}"
        buttons = []
        if row["status"] == "published":
            buttons.append([InlineKeyboardButton(text="✏️ Попросить изменить", callback_data=f"edit_request:{row['id']}")])
        elif row["status"] == "rejected":
            buttons.append([InlineKeyboardButton(text="✍️ Отправить заново", callback_data="write_reflection")])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="home")])
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    await safe_edit(callback, text, markup)


@dp.callback_query(F.data.startswith("edit_request:"))
async def edit_request_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        reflection_id = int(callback.data.split(":")[1])
    except ValueError:
        await callback.message.answer("Некорректный запрос.")
        return
    row = get_reflection(reflection_id)
    if not row or row["telegram_id"] != callback.from_user.id or row["status"] != "published":
        await callback.message.answer("Эту публикацию нельзя изменить через этот запрос.")
        return
    conn = db()
    pending = conn.execute(
        "SELECT 1 FROM edit_requests WHERE reflection_id=? AND telegram_id=? AND status='pending'",
        (reflection_id, callback.from_user.id),
    ).fetchone()
    conn.close()
    if pending:
        await callback.message.answer("⏳ Ты уже отправил просьбу об изменении. Дождись ответа администратора.")
        return
    await state.update_data(reflection_id=reflection_id)
    await state.set_state(EditRequestState.waiting_text)
    await callback.message.answer("✏️ Напиши, что именно нужно изменить.\n\nЭто будет отправлено администратору как просьба об изменении.")


@dp.message(EditRequestState.waiting_text)
async def edit_request_save(message: Message, state: FSMContext):
    data = await state.get_data()
    reflection_id = data.get("reflection_id")
    text, error = validate_text(message.text, "Просьба", 2000)
    if error:
        await message.answer(error)
        return
    reflection = get_reflection(reflection_id)
    if not reflection or reflection["telegram_id"] != message.from_user.id or reflection["status"] != "published":
        await state.clear()
        await message.answer("Публикация больше недоступна для изменения.")
        return

    conn = db()
    cur = conn.execute("INSERT INTO edit_requests(reflection_id, telegram_id, text, status, created_at) VALUES (?, ?, ?, 'pending', ?)", (reflection_id, message.from_user.id, text, now()))
    request_id = cur.lastrowid
    admins = conn.execute("SELECT telegram_id FROM admins").fetchall()
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer("✅ Просьба отправлена администратору.")

    admin_text = (
        "✏️ <b>Просьба изменить публикацию</b>\n\n"
        f"💭 Опубликованный текст:\n{escape(reflection['text'])}\n\n"
        f"📝 Просьба:\n{escape(text)}\n\n"
        f"🔐 ID автора: <code>{message.from_user.id}</code>"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📩 Открыть запрос", callback_data=f"edit_req_view:{request_id}")
    ]])
    for admin in admins:
        try:
            await message.bot.send_message(admin["telegram_id"], admin_text, reply_markup=markup)
        except Exception:
            logging.exception("Could not notify admin")


# -------------------- History --------------------

@dp.callback_query(F.data == "history")
async def history_handler(callback: CallbackQuery):
    await callback.answer()
    conn = db()
    rows = conn.execute("SELECT * FROM history_events ORDER BY position, id").fetchall()
    conn.close()
    if not rows:
        await safe_edit(callback, "📜 <b>Библия — это история</b>\n\nАдминистратор ещё не добавил события.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]]))
        return
    buttons = [[InlineKeyboardButton(text=f"📜 {row['title']}", callback_data=f"hist:{row['id']}")] for row in rows]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="home")])
    await safe_edit(callback, "📜 <b>Библия — это история</b>\n\nВыбери событие:", InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("hist:") & ~F.data.startswith("hist_add"))
async def history_event_handler(callback: CallbackQuery):
    await callback.answer()
    try:
        event_id = int(callback.data.split(":")[1])
    except ValueError:
        return
    conn = db()
    row = conn.execute("SELECT * FROM history_events WHERE id=?", (event_id,)).fetchone()
    conn.close()
    if not row:
        await callback.message.answer("Событие не найдено.")
        return
    reference = f"\n\n📖 <b>Место Писания:</b> {escape(row['reference'])}" if row["reference"] else ""
    await safe_edit(callback, f"📜 <b>{escape(row['title'])}</b>\n\n{escape(row['description'])}{reference}", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К истории", callback_data="history")]
    ]))


@dp.callback_query(F.data == "progress")
async def progress_handler(callback: CallbackQuery):
    await callback.answer()
    conn = db()
    days = conn.execute("SELECT COUNT(*) FROM reading_views WHERE telegram_id=?", (callback.from_user.id,)).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM reflections WHERE telegram_id=?", (callback.from_user.id,)).fetchone()[0]
    published = conn.execute("SELECT COUNT(*) FROM reflections WHERE telegram_id=? AND status='published'", (callback.from_user.id,)).fetchone()[0]
    conn.close()
    await safe_edit(callback, f"📊 <b>Мой прогресс</b>\n\n📖 Дней с открытым чтением: {days}\n💭 Размышлений отправлено: {total}\n🌍 Опубликовано: {published}", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]]))


@dp.callback_query(F.data == "home")
async def home_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await safe_edit(callback, "📖 <b>Главное меню</b>", main_keyboard())


# -------------------- Admin: reading --------------------

@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    await message.answer("⚙️ <b>Панель администратора</b>", reply_markup=admin_menu_for(message.from_user.id))


@dp.callback_query(F.data == "adm_today")
async def adm_today(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    reading = get_today_reading()
    current = "\n\n<b>Сейчас установлено:</b>\n" + today_text() if reading else "\n\n<b>Сейчас чтение не установлено.</b>"
    await callback.message.answer(
        "📖 <b>Управление сегодняшним чтением</b>" + current,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Задать/изменить сегодня", callback_data="set_today")],
            [InlineKeyboardButton(text="🔔 Отправить уведомление сейчас", callback_data="notify_now")],
            [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin_home")],
        ])
    )


@dp.callback_query(F.data == "set_today")
async def set_today(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminReadingState.waiting_title)
    await callback.message.answer("Введите название чтения, например: «Притча о сеятеле».\n\nДля отмены используйте /cancel.")


@dp.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❎ Действие отменено.")


@dp.message(AdminReadingState.waiting_title)
async def admin_title(message: Message, state: FSMContext):
    value, error = validate_text(message.text, "Название чтения", 200)
    if error:
        await message.answer(error)
        return
    await state.update_data(title=value)
    await state.set_state(AdminReadingState.waiting_reference)
    await message.answer("Введите место Писания, например: «Матфея 13:1–23».")


@dp.message(AdminReadingState.waiting_reference)
async def admin_reference(message: Message, state: FSMContext):
    value, error = validate_text(message.text, "Место Писания", 300)
    if error:
        await message.answer(error)
        return
    await state.update_data(reference=value)
    await state.set_state(AdminReadingState.waiting_description)
    await message.answer("Введите дополнительную мысль/контекст. Если не нужен — отправьте «-».")


@dp.message(AdminReadingState.waiting_description)
async def admin_description(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if value != "-" and len(value) > 1500:
        await message.answer("Описание слишком длинное. Максимум — 1500 символов.")
        return
    await state.update_data(description="" if value == "-" else value)
    await state.set_state(AdminReadingState.waiting_text)
    await message.answer("Отправьте текст отрывка, который должен читаться внутри приложения. Можно отправить «-», если текст пока не добавляем. Максимум 12000 символов суммарно.")


@dp.message(AdminReadingState.waiting_text)
async def admin_reading_text(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if value != "-" and len(value) > 4000:
        await message.answer("Текст слишком длинный. Максимум — 4000 символов в одном сообщении Telegram.")
        return
    await state.update_data(text="" if value == "-" else value)
    await state.set_state(AdminReadingState.waiting_question)
    await message.answer("Введите вопрос дня.")


@dp.message(AdminReadingState.waiting_question)
async def admin_question(message: Message, state: FSMContext):
    data = await state.get_data()
    question, error = validate_text(message.text, "Вопрос дня", 1000)
    if error:
        await message.answer(error)
        return
    conn = db()
    conn.execute("""
        INSERT INTO daily_reading(reading_date, title, reference, description, text, question, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(reading_date) DO UPDATE SET
            title=excluded.title, reference=excluded.reference, description=excluded.description,
            text=excluded.text, question=excluded.question, created_by=excluded.created_by, created_at=excluded.created_at
    """, (today(), data["title"], data["reference"], data["description"], data.get("text", ""), question, message.from_user.id, now()))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer("✅ Сегодняшнее чтение и вопрос дня сохранены.", reply_markup=admin_menu_for(message.from_user.id))
    await broadcast_today_reading(message.bot, force=True)


# -------------------- Admin: moderation --------------------

@dp.callback_query(F.data == "adm_moderation")
async def adm_moderation(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    rows = get_pending_reflections()
    if not rows:
        await callback.message.answer("🛡 На модерации ничего нет.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin_home")]]))
        return
    await callback.message.answer(f"🛡 На модерации: {len(rows)}")
    for row in rows:
        header = (
            f"📝 <b>Размышление #{row['id']}</b>\n\n"
            f"📖 {escape(row['title'])}\n"
            f"📚 {escape(row['reference'])}\n\n"
            f"🔐 ID автора: <code>{row['telegram_id']}</code>"
        )
        body = f"💭 <b>Текст размышления</b>\n{escape(row['text'])}"
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"approve:{row['id']}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{row['id']}"),
        ]])
        await callback.message.answer(header)
        for chunk in split_text(body):
            await callback.message.answer(chunk, reply_markup=markup if chunk == split_text(body)[-1] else None)


@dp.callback_query(F.data.startswith("approve:"))
async def approve(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        reflection_id = int(callback.data.split(":")[1])
    except ValueError:
        await callback.answer("Некорректный ID", show_alert=True)
        return
    conn = db()
    row = conn.execute("SELECT telegram_id, status FROM reflections WHERE id=?", (reflection_id,)).fetchone()
    if not row or row["status"] != "pending":
        conn.close()
        await callback.answer("Уже обработано или не найдено.", show_alert=True)
        return
    conn.execute("UPDATE reflections SET status='published', published_at=? WHERE id=?", (now(), reflection_id))
    conn.commit()
    conn.close()
    await callback.answer("Опубликовано.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    try:
        await callback.bot.send_message(row["telegram_id"], "✅ Твоё размышление одобрено и опубликовано анонимно в разделе «Что думают другие».")
    except Exception:
        logging.exception("Could not notify reflection author")


@dp.callback_query(F.data.startswith("reject:"))
async def reject(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        reflection_id = int(callback.data.split(":")[1])
    except ValueError:
        await callback.answer("Некорректный ID", show_alert=True)
        return
    conn = db()
    row = conn.execute("SELECT telegram_id, status FROM reflections WHERE id=?", (reflection_id,)).fetchone()
    if not row or row["status"] != "pending":
        conn.close()
        await callback.answer("Уже обработано или не найдено.", show_alert=True)
        return
    conn.execute("UPDATE reflections SET status='rejected' WHERE id=?", (reflection_id,))
    conn.commit()
    conn.close()
    await callback.answer("Отклонено.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    try:
        await callback.bot.send_message(row["telegram_id"], "❌ Твоё размышление не прошло модерацию. Ты можешь отправить новый вариант через «Моё размышление».")
    except Exception:
        logging.exception("Could not notify reflection author")


# -------------------- Admin: history --------------------

@dp.callback_query(F.data == "adm_history")
async def adm_history(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer("📜 <b>Библия — это история</b>\n\nДобавляй события вручную. Бот ничего сам не придумывает.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить событие", callback_data="hist_add")],
        [InlineKeyboardButton(text="📋 Управление событиями", callback_data="hist_list")],
        [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin_home")],
    ]))


@dp.callback_query(F.data == "hist_add")
async def hist_add(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminHistoryState.waiting_title)
    await callback.message.answer("Название события:")


@dp.message(AdminHistoryState.waiting_title)
async def hist_title(message: Message, state: FSMContext):
    value, error = validate_text(message.text, "Название события", 200)
    if error:
        await message.answer(error)
        return
    await state.update_data(title=value)
    await state.set_state(AdminHistoryState.waiting_description)
    await message.answer("Описание события:")


@dp.message(AdminHistoryState.waiting_description)
async def hist_description(message: Message, state: FSMContext):
    value, error = validate_text(message.text, "Описание события", 3500)
    if error:
        await message.answer(error)
        return
    await state.update_data(description=value)
    await state.set_state(AdminHistoryState.waiting_reference)
    await message.answer("Место Писания. Если не нужно — отправьте «-».")


@dp.message(AdminHistoryState.waiting_reference)
async def hist_reference(message: Message, state: FSMContext):
    data = await state.get_data()
    reference = (message.text or "").strip()
    if reference == "":
        await message.answer("Введите место Писания или «-».")
        return
    if reference != "-" and len(reference) > 300:
        await message.answer("Место Писания слишком длинное. Максимум — 300 символов.")
        return
    reference = "" if reference == "-" else reference
    conn = db()
    max_position = conn.execute("SELECT COALESCE(MAX(position), 0) FROM history_events").fetchone()[0]
    conn.execute("INSERT INTO history_events(title, description, reference, position, created_at) VALUES (?, ?, ?, ?, ?)", (data["title"], data["description"], reference, max_position + 1, now()))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer("✅ Событие добавлено.", reply_markup=admin_menu_for(message.from_user.id))


@dp.callback_query(F.data == "hist_list")
async def hist_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    conn = db()
    rows = conn.execute("SELECT id, title, position FROM history_events ORDER BY position, id").fetchall()
    conn.close()
    if not rows:
        await callback.message.answer("Событий пока нет.")
        return
    buttons = [[InlineKeyboardButton(text=f"✏️ {r['position']}. {r['title']}", callback_data=f"hist_edit:{r['id']}")] for r in rows]
    buttons += [[InlineKeyboardButton(text=f"🗑 Удалить #{r['id']}", callback_data=f"hist_del:{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton(text="⬅️ История", callback_data="adm_history")])
    await callback.message.answer("📋 <b>Управление событиями</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("hist_edit:"))
async def hist_edit(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    event_id = int(callback.data.split(":")[1])
    conn = db()
    row = conn.execute("SELECT * FROM history_events WHERE id=?", (event_id,)).fetchone()
    conn.close()
    await callback.answer()
    if not row:
        await callback.message.answer("Событие не найдено.")
        return
    await state.update_data(event_id=event_id)
    await state.set_state(AdminHistoryEditState.waiting_title)
    await callback.message.answer(f"Текущее название:\n{escape(row['title'])}\n\nВведите новое название:")


@dp.message(AdminHistoryEditState.waiting_title)
async def hist_edit_title(message: Message, state: FSMContext):
    value, error = validate_text(message.text, "Название события", 200)
    if error:
        await message.answer(error)
        return
    await state.update_data(title=value)
    await state.set_state(AdminHistoryEditState.waiting_description)
    await message.answer("Введите новое описание:")


@dp.message(AdminHistoryEditState.waiting_description)
async def hist_edit_description(message: Message, state: FSMContext):
    value, error = validate_text(message.text, "Описание события", 3500)
    if error:
        await message.answer(error)
        return
    await state.update_data(description=value)
    await state.set_state(AdminHistoryEditState.waiting_reference)
    await message.answer("Новое место Писания или «-»:")


@dp.message(AdminHistoryEditState.waiting_reference)
async def hist_edit_reference(message: Message, state: FSMContext):
    data = await state.get_data()
    reference = (message.text or "").strip()
    if not reference:
        await message.answer("Введите место Писания или «-».")
        return
    if reference != "-" and len(reference) > 300:
        await message.answer("Место Писания слишком длинное. Максимум — 300 символов.")
        return
    reference = "" if reference == "-" else reference
    conn = db()
    conn.execute("UPDATE history_events SET title=?, description=?, reference=? WHERE id=?", (data["title"], data["description"], reference, data["event_id"]))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer("✅ Событие изменено.", reply_markup=admin_menu_for(message.from_user.id))


@dp.callback_query(F.data.startswith("hist_del:"))
async def hist_del(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    event_id = int(callback.data.split(":")[1])
    conn = db()
    conn.execute("DELETE FROM history_events WHERE id=?", (event_id,))
    conn.commit()
    conn.close()
    await callback.answer("Событие удалено.")
    await callback.message.edit_reply_markup(reply_markup=None)


# -------------------- Admin: stats/admins --------------------

@dp.callback_query(F.data == "adm_stats")
async def adm_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    conn = db()
    users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    reflections = conn.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM reflections WHERE status='pending'").fetchone()[0]
    published = conn.execute("SELECT COUNT(*) FROM reflections WHERE status='published'").fetchone()[0]
    readings = conn.execute("SELECT COUNT(*) FROM daily_reading").fetchone()[0]
    conn.close()
    await callback.message.answer(f"📊 <b>Статистика</b>\n\n👥 Пользователей: {users}\n📖 Дней чтения задано: {readings}\n💭 Размышлений: {reflections}\n⏳ На модерации: {pending}\n🌍 Опубликовано: {published}")


@dp.callback_query(F.data == "adm_admins")
async def adm_admins(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только главный администратор.", show_alert=True)
        return
    await callback.answer()
    conn = db()
    rows = conn.execute("SELECT telegram_id, added_at FROM admins ORDER BY added_at").fetchall()
    conn.close()
    text = "👑 <b>Администраторы</b>\n\n"
    for r in rows:
        role = "Главный администратор" if r["telegram_id"] == OWNER_ID else "Дополнительный администратор"
        text += f"• <code>{r['telegram_id']}</code> — {role}\n"
    await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="admin_add")],
        [InlineKeyboardButton(text="➖ Удалить админа", callback_data="admin_remove")],
        [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin_home")],
    ]))


@dp.callback_query(F.data == "admin_add")
async def admin_add(callback: CallbackQuery, state: FSMContext):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только главный администратор.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminAddState.waiting_id)
    await callback.message.answer("Введите Telegram ID нового администратора.\nНапример: <code>123456789</code>")


@dp.message(AdminAddState.waiting_id)
async def admin_add_save(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("ID должен состоять только из цифр.")
        return
    admin_id = int(raw)
    if admin_id == OWNER_ID:
        await state.clear()
        await message.answer("Этот ID уже является главным администратором.")
        return
    conn = db()
    before = conn.execute("SELECT 1 FROM admins WHERE telegram_id=?", (admin_id,)).fetchone()
    conn.execute("INSERT OR IGNORE INTO admins(telegram_id, added_by, added_at) VALUES (?, ?, ?)", (admin_id, message.from_user.id, now()))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(f"{'ℹ️ Администратор уже был добавлен.' if before else '✅ Администратор <code>'+str(admin_id)+'</code> добавлен.'}", reply_markup=owner_admin_keyboard())


@dp.callback_query(F.data == "admin_remove")
async def admin_remove(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только главный администратор.", show_alert=True)
        return
    conn = db()
    rows = conn.execute("SELECT telegram_id FROM admins WHERE telegram_id != ? ORDER BY telegram_id", (OWNER_ID,)).fetchall()
    conn.close()
    await callback.answer()
    if not rows:
        await callback.message.answer("Дополнительных администраторов нет.")
        return
    buttons = [[InlineKeyboardButton(text=f"❌ Удалить {r['telegram_id']}", callback_data=f"admin_del:{r['telegram_id']}")] for r in rows]
    buttons.append([InlineKeyboardButton(text="⬅️ Администраторы", callback_data="adm_admins")])
    await callback.message.answer("Выберите администратора для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("admin_del:"))
async def admin_del(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только главный администратор.", show_alert=True)
        return
    admin_id = int(callback.data.split(":")[1])
    if admin_id == OWNER_ID:
        await callback.answer("Главного администратора удалить нельзя.", show_alert=True)
        return
    conn = db()
    conn.execute("DELETE FROM admins WHERE telegram_id=?", (admin_id,))
    conn.commit()
    conn.close()
    await callback.answer("Администратор удалён.")
    await callback.message.edit_reply_markup(reply_markup=None)


@dp.callback_query(F.data == "admin_home")
async def admin_home(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    await safe_edit(callback, "⚙️ <b>Панель администратора</b>", admin_menu_for(callback.from_user.id))


# -------------------- Admin: edit published reflection --------------------

@dp.callback_query(F.data.startswith("edit_req_view:"))
async def edit_req_view(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    request_id = int(callback.data.split(":")[1])
    conn = db()
    row = conn.execute("""
        SELECT er.*, r.text AS reflection_text, r.status AS reflection_status
        FROM edit_requests er JOIN reflections r ON r.id=er.reflection_id
        WHERE er.id=?
    """, (request_id,)).fetchone()
    conn.close()
    await callback.answer()
    if not row:
        await callback.message.answer("Запрос не найден.")
        return
    if row["status"] != "pending":
        await callback.message.answer("Этот запрос уже обработан.")
        return
    await callback.message.answer(
        f"✏️ <b>Запрос #{request_id}</b>\n\n"
        f"Опубликовано:\n{escape(row['reflection_text'])}\n\n"
        f"Просьба:\n{escape(row['text'])}\n\n"
        f"ID автора: <code>{row['telegram_id']}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"edit_req_apply:{request_id}")],
            [InlineKeyboardButton(text="❌ Отклонить просьбу", callback_data=f"edit_req_reject:{request_id}")],
        ])
    )


@dp.callback_query(F.data.startswith("edit_req_apply:"))
async def edit_req_apply(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    request_id = int(callback.data.split(":")[1])
    conn = db()
    row = conn.execute("""
        SELECT er.id, er.reflection_id, er.telegram_id, er.status, r.text, r.status AS reflection_status
        FROM edit_requests er JOIN reflections r ON r.id=er.reflection_id
        WHERE er.id=?
    """, (request_id,)).fetchone()
    conn.close()
    await callback.answer()
    if not row or row["status"] != "pending" or row["reflection_status"] != "published":
        await callback.message.answer("Запрос уже обработан или публикация недоступна.")
        return
    await state.update_data(request_id=request_id, reflection_id=row["reflection_id"], telegram_id=row["telegram_id"])
    await state.set_state(AdminEditReflectionState.waiting_text)
    await callback.message.answer("✏️ Введите новый текст опубликованного размышления целиком.\n\nТекущий текст:\n" + escape(row["text"]))


@dp.message(AdminEditReflectionState.waiting_text)
async def admin_edit_reflection_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    new_text, error = validate_text(message.text, "Новый текст размышления")
    if error:
        await message.answer(error)
        return
    conn = db()
    row = conn.execute("SELECT telegram_id, reflection_id FROM edit_requests WHERE id=? AND status='pending'", (data["request_id"],)).fetchone()
    if not row:
        conn.close()
        await state.clear()
        await message.answer("Запрос уже обработан.")
        return
    conn.execute("UPDATE reflections SET text=? WHERE id=? AND status='published'", (new_text, data["reflection_id"]))
    conn.execute("UPDATE edit_requests SET status='done' WHERE id=?", (data["request_id"],))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer("✅ Опубликованный текст изменён.", reply_markup=admin_menu_for(message.from_user.id))
    try:
        await message.bot.send_message(row["telegram_id"], "✏️ Администратор изменил твоё опубликованное размышление по твоей просьбе.")
    except Exception:
        logging.exception("Could not notify user about edit")


@dp.callback_query(F.data.startswith("edit_req_reject:"))
async def edit_req_reject(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    request_id = int(callback.data.split(":")[1])
    conn = db()
    cur = conn.execute("UPDATE edit_requests SET status='rejected' WHERE id=? AND status='pending'", (request_id,))
    conn.commit()
    row = conn.execute("SELECT telegram_id FROM edit_requests WHERE id=?", (request_id,)).fetchone()
    conn.close()
    await callback.answer("Просьба отклонена." if cur.rowcount else "Запрос уже обработан.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if row:
        try:
            await callback.bot.send_message(row["telegram_id"], "ℹ️ Администратор рассмотрел просьбу об изменении, но не внёс изменения.")
        except Exception:
            logging.exception("Could not notify user about edit rejection")


@dp.callback_query(F.data == "notify_now")
async def notify_now(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer("Рассылка запускается…")
    count = await broadcast_today_reading(callback.bot, force=True)
    await callback.message.answer(f"🔔 Уведомление отправлено. Успешно: {count} пользователей.")


# -------------------- Notifications --------------------

async def broadcast_today_reading(bot: Bot, force=False):
    reading = get_today_reading()
    if not reading:
        return 0

    conn = db()
    already = conn.execute("SELECT 1 FROM notification_log WHERE reading_date=?", (reading["reading_date"],)).fetchone()
    users = conn.execute("SELECT telegram_id FROM users").fetchall()
    if already and not force:
        conn.close()
        return 0
    conn.close()

    text = (
        "🔔 <b>Чтение на сегодня</b>\n\n"
        f"📖 <b>{escape(reading['title'])}</b>\n"
        f"📚 {escape(reading['reference'])}\n\n"
        f"❓ <b>Вопрос дня</b>\n{escape(reading['question'])}"
    )
    buttons = []
    if WEBAPP_URL:
        buttons.append(InlineKeyboardButton(text="✨ Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL)))
    buttons.append(InlineKeyboardButton(text="📖 Открыть чтение", callback_data="today"))
    markup = InlineKeyboardMarkup(inline_keyboard=[buttons])
    success = 0
    for user in users:
        try:
            await bot.send_message(user["telegram_id"], text, reply_markup=markup)
            success += 1
        except Exception as exc:
            logging.info("Notification to %s failed: %s", user["telegram_id"], exc)

    conn = db()
    conn.execute("INSERT OR REPLACE INTO notification_log(reading_date, sent_at) VALUES (?, ?)", (reading["reading_date"], now()))
    conn.commit()
    conn.close()
    return success


async def notification_loop(bot: Bot):
    logging.info("Daily notification loop started: %s %s", BOT_TIMEZONE, NOTIFICATION_TIME)
    while True:
        try:
            current = datetime.now(LOCAL_TZ)
            target = current.replace(hour=NOTIFY_HOUR, minute=NOTIFY_MINUTE, second=0, microsecond=0)
            if target <= current:
                target += timedelta(days=1)
            await asyncio.sleep(max(1, (target - current).total_seconds()))
            await broadcast_today_reading(bot, force=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Daily notification loop failed")
            await asyncio.sleep(60)


# -------------------- Mini App API --------------------

def validate_webapp_init_data(init_data: str):
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return None
        auth_date = int(pairs.get("auth_date", "0"))
        if abs(int(datetime.now(timezone.utc).timestamp()) - auth_date) > 86400:
            return None
        user = json.loads(pairs.get("user", "{}"))
        if not user.get("id"):
            return None
        return user
    except Exception:
        logging.exception("Mini App initData validation failed")
        return None


async def api_user(request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user = validate_webapp_init_data(init_data)
    if not user:
        raise web.HTTPUnauthorized(text="Invalid Telegram initData")
    register_user(int(user["id"]))
    return user


def row_to_dict(row):
    return dict(row) if row else None


def miniapp_state(user_id):
    reading = get_today_reading()
    conn = db()
    views = conn.execute("SELECT 1 FROM reading_views WHERE telegram_id=? AND reading_date=?", (user_id, today())).fetchone()
    days = conn.execute("SELECT COUNT(*) FROM reading_views WHERE telegram_id=?", (user_id,)).fetchone()[0]
    sent = conn.execute("SELECT COUNT(*) FROM reflections WHERE telegram_id=?", (user_id,)).fetchone()[0]
    published = conn.execute("SELECT COUNT(*) FROM reflections WHERE telegram_id=? AND status='published'", (user_id,)).fetchone()[0]
    my_reflection = None
    if reading:
        my_reflection = conn.execute("SELECT id, text, status, created_at, published_at FROM reflections WHERE telegram_id=? AND reading_id=?", (user_id, reading["id"])).fetchone()
    others = []
    if reading:
        others = conn.execute("SELECT id, text, published_at FROM reflections WHERE reading_id=? AND status='published' ORDER BY published_at DESC LIMIT 20", (reading["id"],)).fetchall()
    history = conn.execute("SELECT id, title, description, reference FROM history_events ORDER BY position, id").fetchall()
    conn.close()
    return {
        "reading": row_to_dict(reading),
        "viewed_today": bool(views),
        "progress": {"days": days, "reflections": sent, "published": published},
        "my_reflection": row_to_dict(my_reflection),
        "others": [row_to_dict(r) for r in others],
        "history": [row_to_dict(r) for r in history],
    }


async def api_state(request):
    user = await api_user(request)
    return web.json_response(miniapp_state(int(user["id"])), dumps=lambda x: json.dumps(x, ensure_ascii=False))


async def api_mark_read(request):
    user = await api_user(request)
    reading = get_today_reading()
    if reading:
        mark_reading_viewed(int(user["id"]), reading["reading_date"])
    return web.json_response({"ok": True})


async def api_submit_reflection(request):
    user = await api_user(request)
    payload = await request.json()
    text, error = validate_text(payload.get("text"), "Размышление", 4000)
    if error:
        return web.json_response({"ok": False, "error": error}, status=400)
    reading = get_today_reading()
    if not reading:
        return web.json_response({"ok": False, "error": "Сегодняшнее чтение ещё не задано."}, status=400)
    uid = int(user["id"])
    conn = db()
    existing = conn.execute("SELECT id, status FROM reflections WHERE telegram_id=? AND reading_id=?", (uid, reading["id"])).fetchone()
    if existing and existing["status"] in ("pending", "published"):
        conn.close()
        return web.json_response({"ok": False, "error": "На сегодня у тебя уже есть размышление."}, status=409)
    if existing and existing["status"] == "rejected":
        conn.execute("UPDATE reflections SET text=?, status='pending', created_at=?, published_at=NULL WHERE id=?", (text, now(), existing["id"]))
        reflection_id = existing["id"]
    else:
        cur = conn.execute("INSERT INTO reflections(telegram_id, reading_id, text, status, created_at) VALUES (?, ?, ?, 'pending', ?)", (uid, reading["id"], text, now()))
        reflection_id = cur.lastrowid
    conn.commit()
    conn.close()
    asyncio.create_task(notify_admins_about_reflection(request.app["bot"], reflection_id))
    return web.json_response({"ok": True, "status": "pending"})


async def api_edit_request(request):
    user = await api_user(request)
    payload = await request.json()
    text, error = validate_text(payload.get("text"), "Просьба", 2000)
    if error:
        return web.json_response({"ok": False, "error": error}, status=400)
    reflection_id = int(payload.get("reflection_id", 0))
    uid = int(user["id"])
    conn = db()
    reflection = conn.execute("SELECT * FROM reflections WHERE id=? AND telegram_id=? AND status='published'", (reflection_id, uid)).fetchone()
    pending = conn.execute("SELECT 1 FROM edit_requests WHERE reflection_id=? AND status='pending'", (reflection_id,)).fetchone()
    if not reflection or pending:
        conn.close()
        return web.json_response({"ok": False, "error": "Запрос уже существует или публикация недоступна."}, status=409)
    cur = conn.execute("INSERT INTO edit_requests(reflection_id, telegram_id, text, status, created_at) VALUES (?, ?, ?, 'pending', ?)", (reflection_id, uid, text, now()))
    request_id = cur.lastrowid
    admins = conn.execute("SELECT telegram_id FROM admins").fetchall()
    conn.commit()
    conn.close()
    for admin in admins:
        try:
            await request.app["bot"].send_message(admin["telegram_id"], f"✏️ <b>Новая просьба изменить размышление</b>\n\n{escape(text)}\n\nID автора: <code>{uid}</code>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📩 Открыть запрос", callback_data=f"edit_req_view:{request_id}")]]))
        except Exception:
            logging.exception("Could not notify admin about Mini App edit request")
    return web.json_response({"ok": True})


async def health(request):
    return web.json_response({"ok": True, "service": "bible-na-kazhdyy-den"})


def create_web_app(bot):
    app = web.Application()
    app["bot"] = bot
    web_dir = os.path.join(os.path.dirname(__file__), "webapp")
    app.router.add_get("/", lambda request: web.FileResponse(os.path.join(web_dir, "index.html")))
    app.router.add_get("/style.css", lambda request: web.FileResponse(os.path.join(web_dir, "style.css")))
    app.router.add_get("/app.js", lambda request: web.FileResponse(os.path.join(web_dir, "app.js")))
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/mark-read", api_mark_read)
    app.router.add_post("/api/reflections", api_submit_reflection)
    app.router.add_post("/api/edit-request", api_edit_request)
    return app


async def start_web_server(bot):
    app = create_web_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("Mini App server started on 0.0.0.0:%s", PORT)
    return runner


async def configure_menu_button(bot):
    if not WEBAPP_URL:
        logging.info("WEBAPP_URL is not set; Mini App menu button is not configured")
        return
    try:
        from aiogram.types import MenuButtonWebApp
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL)))
        logging.info("Telegram menu button configured: %s", WEBAPP_URL)
    except Exception:
        logging.exception("Could not configure Telegram Mini App menu button")


async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notification_task = asyncio.create_task(notification_loop(bot))
    web_runner = await start_web_server(bot)
    await configure_menu_button(bot)
    try:
        await dp.start_polling(bot)
    finally:
        notification_task.cancel()
        try:
            await notification_task
        except asyncio.CancelledError:
            pass
        await web_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
