import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not OWNER_ID_RAW.isdigit():
    raise RuntimeError("OWNER_ID must be a numeric Telegram ID")

OWNER_ID = int(OWNER_ID_RAW)

os.makedirs("data", exist_ok=True)
DB_PATH = "data/bot.sqlite3"

logging.basicConfig(level=logging.INFO)


# -------------------- Database --------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
    """)
    conn.execute(
        "INSERT OR IGNORE INTO admins(telegram_id, added_by, added_at) VALUES (?, ?, ?)",
        (OWNER_ID, OWNER_ID, now()),
    )
    conn.commit()
    conn.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today():
    return datetime.now().date().isoformat()


def register_user(user_id):
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users(telegram_id, created_at) VALUES (?, ?)",
        (user_id, now()),
    )
    conn.commit()
    conn.close()


def is_admin(user_id):
    conn = db()
    row = conn.execute(
        "SELECT 1 FROM admins WHERE telegram_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return row is not None


def is_owner(user_id):
    return user_id == OWNER_ID


def get_today_reading():
    conn = db()
    row = conn.execute(
        "SELECT * FROM daily_reading WHERE reading_date=?", (today(),)
    ).fetchone()
    conn.close()
    return row


def get_reading_by_id(reading_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM daily_reading WHERE id=?", (reading_id,)
    ).fetchone()
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


def get_reflection(reflection_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM reflections WHERE id=?", (reflection_id,)
    ).fetchone()
    conn.close()
    return row


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


# -------------------- Keyboards --------------------

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Что читаем сегодня", callback_data="today")],
        [InlineKeyboardButton(text="💭 Моё размышление", callback_data="my_reflection")],
        [InlineKeyboardButton(text="👥 Что думают другие", callback_data="others")],
        [InlineKeyboardButton(text="📜 Библия — это история", callback_data="history")],
        [InlineKeyboardButton(text="📊 Мой прогресс", callback_data="progress")],
    ])


def admin_keyboard():
    rows = [
        [InlineKeyboardButton(text="📖 Сегодня", callback_data="adm_today")],
        [InlineKeyboardButton(text="🛡 Модерация", callback_data="adm_moderation")],
        [InlineKeyboardButton(text="📜 История", callback_data="adm_history")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
    ]
    if is_owner_cached_placeholder:
        pass
    return InlineKeyboardMarkup(inline_keyboard=rows)


def owner_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Сегодня", callback_data="adm_today")],
        [InlineKeyboardButton(text="🛡 Модерация", callback_data="adm_moderation")],
        [InlineKeyboardButton(text="📜 История", callback_data="adm_history")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="👑 Администраторы", callback_data="adm_admins")],
    ])


# This is intentionally unused; it keeps keyboard construction simple.
is_owner_cached_placeholder = False


# -------------------- FSM --------------------

class ReflectionState(StatesGroup):
    waiting_text = State()


class EditRequestState(StatesGroup):
    waiting_text = State()


class AdminReadingState(StatesGroup):
    waiting_title = State()
    waiting_reference = State()
    waiting_description = State()
    waiting_question = State()


class AdminHistoryState(StatesGroup):
    waiting_title = State()
    waiting_description = State()
    waiting_reference = State()


class AdminAddState(StatesGroup):
    waiting_id = State()


# -------------------- Helpers --------------------

async def safe_edit(callback: CallbackQuery, text: str, markup=None):
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)


def admin_menu_for(user_id):
    return owner_admin_keyboard() if is_owner(user_id) else admin_keyboard()


def today_text():
    reading = get_today_reading()
    if not reading:
        return (
            "📖 <b>Сегодняшнее чтение</b>\n\n"
            "Администратор ещё не добавил чтение на сегодня."
        )
    description = reading["description"] or "Дополнительной мысли нет."
    return (
        f"📖 <b>{escape(reading['title'])}</b>\n\n"
        f"📚 {escape(reading['reference'])}\n\n"
        f"{escape(description)}\n\n"
        f"❓ <b>Вопрос дня</b>\n{escape(reading['question'])}"
    )


def escape(text):
    # Telegram HTML escaping for user/admin-entered text.
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# -------------------- User handlers --------------------

dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message):
    register_user(message.from_user.id)
    await message.answer(
        "📖 <b>Добро пожаловать</b>\n\n"
        "Здесь можно читать заданный на сегодня отрывок Библии, "
        "размышлять над ним и анонимно читать мысли других.",
        reply_markup=main_keyboard(),
    )


@dp.callback_query(F.data == "today")
async def today_handler(callback: CallbackQuery):
    await callback.answer()
    await safe_edit(
        callback,
        today_text(),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💭 Написать размышление", callback_data="write_reflection")],
            [InlineKeyboardButton(text="👥 Что думают другие", callback_data="others")],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="home")],
        ]),
    )


@dp.callback_query(F.data == "write_reflection")
async def write_reflection(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    reading = get_today_reading()
    if not reading:
        await callback.message.answer("На сегодня чтение ещё не задано.")
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

    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши размышление обычным текстом.")
        return
    if len(text) > 4000:
        await message.answer("Размышление слишком длинное. Максимум — 4000 символов.")
        return

    conn = db()
    try:
        conn.execute(
            """INSERT INTO reflections
               (telegram_id, reading_id, text, status, created_at)
               VALUES (?, ?, ?, 'pending', ?)""",
            (message.from_user.id, reading["id"], text, now()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        await message.answer(
            "Ты уже отправлял размышление по сегодняшнему чтению.\n"
            "Если его нужно изменить, дождись публикации и попроси администратора внести изменение."
        )
        conn.close()
        await state.clear()
        return
    conn.close()
    await state.clear()

    await message.answer(
        "✅ Размышление отправлено на модерацию.\n\n"
        "Если администратор одобрит его, оно появится в разделе "
        "«Что думают другие» анонимно."
    )

    await notify_admins_about_reflection(message.bot, message.from_user.id)


async def notify_admins_about_reflection(bot: Bot, user_id: int):
    rows = get_pending_reflections()
    if not rows:
        return
    row = rows[-1]
    conn = db()
    admins = conn.execute("SELECT telegram_id FROM admins").fetchall()
    conn.close()

    text = (
        "🛡 <b>Новое размышление</b>\n\n"
        f"📖 {escape(row['title'])}\n"
        f"📚 {escape(row['reference'])}\n\n"
        f"💭 {escape(row['text'])}\n\n"
        f"🔐 Telegram ID автора: <code>{user_id}</code>"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"approve:{row['id']}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{row['id']}"),
        ]
    ])
    for admin in admins:
        try:
            await bot.send_message(admin["telegram_id"], text, reply_markup=markup)
        except Exception:
            logging.exception("Could not notify admin %s", admin["telegram_id"])


@dp.callback_query(F.data == "others")
async def others_handler(callback: CallbackQuery):
    await callback.answer()
    reading = get_today_reading()
    if not reading:
        await safe_edit(callback, "Сегодняшнее чтение ещё не задано.",
                        InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
                        ]))
        return

    rows = get_published_reflections(reading["id"])
    if not rows:
        text = (
            f"👥 <b>Что думают другие</b>\n\n"
            f"По теме «{escape(reading['title'])}» пока нет опубликованных размышлений."
        )
    else:
        parts = [f"👥 <b>Что думают другие</b>\n\n"
                 f"Опубликовано размышлений: {len(rows)}\n"]
        for i, row in enumerate(rows[:10], 1):
            parts.append(f"\n<b>💭 Размышление {i}</b>\n{escape(row['text'])}")
        text = "".join(parts)

    await safe_edit(callback, text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="home")]
    ]))


@dp.callback_query(F.data == "my_reflection")
async def my_reflection_handler(callback: CallbackQuery):
    await callback.answer()
    reading = get_today_reading()
    if not reading:
        await safe_edit(callback, "Сегодняшнее чтение ещё не задано.",
                        InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
                        ]))
        return

    conn = db()
    row = conn.execute(
        "SELECT * FROM reflections WHERE telegram_id=? AND reading_id=?",
        (callback.from_user.id, reading["id"]),
    ).fetchone()
    conn.close()

    if not row:
        text = "💭 Сегодня ты ещё не отправлял размышление."
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Написать", callback_data="write_reflection")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")],
        ])
    else:
        status = {
            "pending": "⏳ На модерации",
            "published": "✅ Опубликовано анонимно",
            "rejected": "❌ Отклонено",
        }.get(row["status"], row["status"])
        text = f"💭 <b>Твоё сегодняшнее размышление</b>\n\n{escape(row['text'])}\n\n<b>Статус:</b> {status}"
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
        ])
        if row["status"] == "published":
            markup.inline_keyboard.insert(
                0, [InlineKeyboardButton(text="✏️ Попросить изменить", callback_data=f"edit_request:{row['id']}")]
            )

    await safe_edit(callback, text, markup)


@dp.callback_query(F.data.startswith("edit_request:"))
async def edit_request_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    reflection_id = int(callback.data.split(":")[1])
    row = get_reflection(reflection_id)
    if not row or row["telegram_id"] != callback.from_user.id or row["status"] != "published":
        await callback.message.answer("Эту публикацию нельзя изменить через этот запрос.")
        return
    await state.update_data(reflection_id=reflection_id)
    await state.set_state(EditRequestState.waiting_text)
    await callback.message.answer(
        "✏️ Напиши, что именно нужно изменить.\n\n"
        "Это будет отправлено администратору как просьба об изменении."
    )


@dp.message(EditRequestState.waiting_text)
async def edit_request_save(message: Message, state: FSMContext):
    data = await state.get_data()
    reflection_id = data["reflection_id"]
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши просьбу обычным текстом.")
        return

    conn = db()
    conn.execute(
        """INSERT INTO edit_requests
           (reflection_id, telegram_id, text, status, created_at)
           VALUES (?, ?, ?, 'pending', ?)""",
        (reflection_id, message.from_user.id, text, now()),
    )
    conn.commit()
    request_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    await state.clear()

    await message.answer("✅ Просьба отправлена администратору.")

    conn = db()
    admins = conn.execute("SELECT telegram_id FROM admins").fetchall()
    reflection = get_reflection(reflection_id)
    conn.close()

    admin_text = (
        "✏️ <b>Просьба изменить публикацию</b>\n\n"
        f"💭 Опубликованный текст:\n{escape(reflection['text'])}\n\n"
        f"📝 Просьба:\n{escape(text)}\n\n"
        f"🔐 ID автора: <code>{message.from_user.id}</code>"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📩 Открыть запрос", callback_data=f"edit_req_view:{request_id}")]
    ])
    for admin in admins:
        try:
            await message.bot.send_message(admin["telegram_id"], admin_text, reply_markup=markup)
        except Exception:
            logging.exception("Could not notify admin")


@dp.callback_query(F.data == "history")
async def history_handler(callback: CallbackQuery):
    await callback.answer()
    conn = db()
    rows = conn.execute(
        "SELECT * FROM history_events ORDER BY position, id"
    ).fetchall()
    conn.close()

    if not rows:
        text = "📜 <b>Библия — это история</b>\n\nАдминистратор ещё не добавил события."
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
        ])
    else:
        buttons = []
        for row in rows:
            buttons.append([InlineKeyboardButton(
                text=f"📜 {row['title']}",
                callback_data=f"hist:{row['id']}"
            )])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="home")])
        text = "📜 <b>Библия — это история</b>\n\nВыбери событие:"
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    await safe_edit(callback, text, markup)


@dp.callback_query(F.data.startswith("hist:"))
async def history_event_handler(callback: CallbackQuery):
    await callback.answer()
    event_id = int(callback.data.split(":")[1])
    conn = db()
    row = conn.execute("SELECT * FROM history_events WHERE id=?", (event_id,)).fetchone()
    conn.close()
    if not row:
        await callback.message.answer("Событие не найдено.")
        return

    reference = f"\n\n📖 <b>Место Писания:</b> {escape(row['reference'])}" if row["reference"] else ""
    text = f"📜 <b>{escape(row['title'])}</b>\n\n{escape(row['description'])}{reference}"
    await safe_edit(callback, text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К истории", callback_data="history")]
    ]))


@dp.callback_query(F.data == "progress")
async def progress_handler(callback: CallbackQuery):
    await callback.answer()
    conn = db()
    total = conn.execute(
        "SELECT COUNT(*) FROM reflections WHERE telegram_id=?",
        (callback.from_user.id,)
    ).fetchone()[0]
    published = conn.execute(
        "SELECT COUNT(*) FROM reflections WHERE telegram_id=? AND status='published'",
        (callback.from_user.id,)
    ).fetchone()[0]
    conn.close()
    await safe_edit(
        callback,
        f"📊 <b>Мой прогресс</b>\n\n"
        f"💭 Размышлений отправлено: {total}\n"
        f"🌍 Опубликовано: {published}",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
        ]),
    )


@dp.callback_query(F.data == "home")
async def home_handler(callback: CallbackQuery):
    await callback.answer()
    await safe_edit(callback, "📖 <b>Главное меню</b>", main_keyboard())


# -------------------- Admin --------------------

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
    await callback.message.answer(
        "📖 <b>Управление сегодняшним чтением</b>\n\n"
        "Нажми кнопку, чтобы заново задать сегодняшнее чтение.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Задать сегодня", callback_data="set_today")],
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
    await callback.message.answer("Введите название чтения, например: «Притча о сеятеле».")


@dp.message(AdminReadingState.waiting_title)
async def admin_title(message: Message, state: FSMContext):
    await state.update_data(title=(message.text or "").strip())
    await state.set_state(AdminReadingState.waiting_reference)
    await message.answer("Введите место Писания, например: «Матфея 13:1–23».")


@dp.message(AdminReadingState.waiting_reference)
async def admin_reference(message: Message, state: FSMContext):
    await state.update_data(reference=(message.text or "").strip())
    await state.set_state(AdminReadingState.waiting_description)
    await message.answer("Введите дополнительную мысль/контекст. Если не нужен — отправьте «-».")


@dp.message(AdminReadingState.waiting_description)
async def admin_description(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    await state.update_data(description="" if value == "-" else value)
    await state.set_state(AdminReadingState.waiting_question)
    await message.answer("Введите вопрос дня.")


@dp.message(AdminReadingState.waiting_question)
async def admin_question(message: Message, state: FSMContext):
    data = await state.get_data()
    question = (message.text or "").strip()
    if not question:
        await message.answer("Вопрос не должен быть пустым.")
        return

    conn = db()
    conn.execute(
        """INSERT INTO daily_reading
           (reading_date, title, reference, description, question, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(reading_date) DO UPDATE SET
             title=excluded.title,
             reference=excluded.reference,
             description=excluded.description,
             question=excluded.question,
             created_by=excluded.created_by,
             created_at=excluded.created_at""",
        (today(), data["title"], data["reference"], data["description"], question,
         message.from_user.id, now()),
    )
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer("✅ Сегодняшнее чтение и вопрос дня сохранены.", reply_markup=admin_menu_for(message.from_user.id))


@dp.callback_query(F.data == "adm_moderation")
async def adm_moderation(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    rows = get_pending_reflections()
    if not rows:
        await callback.message.answer("🛡 На модерации ничего нет.")
        return
    await callback.message.answer(f"🛡 На модерации: {len(rows)}")
    for row in rows:
        text = (
            f"📝 <b>Размышление #{row['id']}</b>\n\n"
            f"📖 {escape(row['title'])}\n"
            f"📚 {escape(row['reference'])}\n\n"
            f"💭 {escape(row['text'])}\n\n"
            f"🔐 ID автора: <code>{row['telegram_id']}</code>"
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"approve:{row['id']}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{row['id']}"),
        ]])
        await callback.message.answer(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("approve:"))
async def approve(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    reflection_id = int(callback.data.split(":")[1])
    conn = db()
    conn.execute(
        "UPDATE reflections SET status='published', published_at=? WHERE id=? AND status='pending'",
        (now(), reflection_id),
    )
    conn.commit()
    changed = conn.total_changes
    conn.close()
    await callback.answer("Опубликовано." if changed else "Уже обработано.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.callback_query(F.data.startswith("reject:"))
async def reject(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    reflection_id = int(callback.data.split(":")[1])
    conn = db()
    conn.execute(
        "UPDATE reflections SET status='rejected' WHERE id=? AND status='pending'",
        (reflection_id,),
    )
    conn.commit()
    changed = conn.total_changes
    conn.close()
    await callback.answer("Отклонено." if changed else "Уже обработано.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.callback_query(F.data == "adm_history")
async def adm_history(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer(
        "📜 <b>Библия — это история</b>\n\n"
        "Добавляй события вручную. Бот ничего сам не придумывает.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить событие", callback_data="hist_add")],
            [InlineKeyboardButton(text="📋 Список событий", callback_data="hist_list")],
        ])
    )


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
    await state.update_data(title=(message.text or "").strip())
    await state.set_state(AdminHistoryState.waiting_description)
    await message.answer("Описание события:")


@dp.message(AdminHistoryState.waiting_description)
async def hist_description(message: Message, state: FSMContext):
    await state.update_data(description=(message.text or "").strip())
    await state.set_state(AdminHistoryState.waiting_reference)
    await message.answer("Место Писания. Если не нужно — отправьте «-».")


@dp.message(AdminHistoryState.waiting_reference)
async def hist_reference(message: Message, state: FSMContext):
    data = await state.get_data()
    reference = (message.text or "").strip()
    if reference == "-":
        reference = ""
    conn = db()
    max_position = conn.execute(
        "SELECT COALESCE(MAX(position), 0) FROM history_events"
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO history_events
           (title, description, reference, position, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (data["title"], data["description"], reference, max_position + 1, now()),
    )
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
    rows = conn.execute(
        "SELECT id, title, position FROM history_events ORDER BY position, id"
    ).fetchall()
    conn.close()
    if not rows:
        await callback.message.answer("Событий пока нет.")
        return
    text = "📋 <b>События</b>\n\n" + "\n".join(
        f"{r['position']}. {escape(r['title'])} — ID {r['id']}" for r in rows
    )
    await callback.message.answer(text)


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
    conn.close()
    await callback.message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: {users}\n"
        f"💭 Размышлений: {reflections}\n"
        f"⏳ На модерации: {pending}\n"
        f"🌍 Опубликовано: {published}"
    )


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
    await callback.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить админа", callback_data="admin_add")],
            [InlineKeyboardButton(text="➖ Удалить админа", callback_data="admin_remove")],
        ])
    )


@dp.callback_query(F.data == "admin_add")
async def admin_add(callback: CallbackQuery, state: FSMContext):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только главный администратор.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminAddState.waiting_id)
    await callback.message.answer(
        "Введите Telegram ID нового администратора.\n"
        "Например: <code>123456789</code>"
    )


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
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO admins(telegram_id, added_by, added_at) VALUES (?, ?, ?)",
        (admin_id, message.from_user.id, now()),
    )
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(
        f"✅ Администратор <code>{admin_id}</code> добавлен.",
        reply_markup=owner_admin_keyboard()
    )


@dp.callback_query(F.data == "admin_remove")
async def admin_remove(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только главный администратор.", show_alert=True)
        return
    conn = db()
    rows = conn.execute(
        "SELECT telegram_id FROM admins WHERE telegram_id != ? ORDER BY telegram_id",
        (OWNER_ID,)
    ).fetchall()
    conn.close()
    await callback.answer()
    if not rows:
        await callback.message.answer("Дополнительных администраторов нет.")
        return
    buttons = [
        [InlineKeyboardButton(
            text=f"❌ Удалить {r['telegram_id']}",
            callback_data=f"admin_del:{r['telegram_id']}"
        )]
        for r in rows
    ]
    await callback.message.answer(
        "Выберите администратора для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


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


@dp.callback_query(F.data.startswith("edit_req_view:"))
async def edit_req_view(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    request_id = int(callback.data.split(":")[1])
    conn = db()
    row = conn.execute("""
        SELECT er.*, r.text AS reflection_text
        FROM edit_requests er
        JOIN reflections r ON r.id=er.reflection_id
        WHERE er.id=?
    """, (request_id,)).fetchone()
    conn.close()
    await callback.answer()
    if not row:
        await callback.message.answer("Запрос не найден.")
        return
    await callback.message.answer(
        f"✏️ <b>Запрос #{request_id}</b>\n\n"
        f"Опубликовано:\n{escape(row['reflection_text'])}\n\n"
        f"Просьба:\n{escape(row['text'])}\n\n"
        f"ID автора: <code>{row['telegram_id']}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отметить обработанным", callback_data=f"edit_req_done:{request_id}")],
        ])
    )


@dp.callback_query(F.data.startswith("edit_req_done:"))
async def edit_req_done(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    request_id = int(callback.data.split(":")[1])
    conn = db()
    conn.execute("UPDATE edit_requests SET status='done' WHERE id=? AND status='pending'", (request_id,))
    conn.commit()
    conn.close()
    await callback.answer("Запрос отмечен обработанным.")
    await callback.message.edit_reply_markup(reply_markup=None)


async def main():
    init_db()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
