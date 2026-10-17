import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, FSInputFile
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ADMIN_IDS = [int(x) for x in (os.getenv("ADMIN_IDS", "") or "").replace(" ", "").split(",") if x.strip().isdigit()]
if not ADMIN_IDS and ADMIN_ID:
    ADMIN_IDS = [ADMIN_ID]

# Главный админ — тот, кто может назначать/снимать операторов.
# По умолчанию — первый из ADMIN_IDS (или ADMIN_ID).
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", "0") or 0)
if not SUPER_ADMIN_ID and ADMIN_IDS:
    SUPER_ADMIN_ID = ADMIN_IDS[0]

SITE_URL = os.getenv("SITE_URL", "https://nebula-kungur.netlify.app").strip()
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/anonkiychat_bot").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "nebula_secret").strip()
WEBHOOK_PORT = int(os.getenv("PORT", os.getenv("WEBHOOK_PORT", "8080")))

BASE_DIR = Path(__file__).resolve().parent
IMG_DIR = BASE_DIR / "img"

BRAND_PHOTO    = Path(os.getenv("BRAND_PHOTO",    str(IMG_DIR / "brand.jpg")))
OPERATOR_PHOTO = Path(os.getenv("OPERATOR_PHOTO", str(IMG_DIR / "operator.jpg")))
ORDER_PHOTO    = Path(os.getenv("ORDER_PHOTO",    str(IMG_DIR / "order.jpg")))

DB_PATH = Path(os.getenv("DB_PATH", "/data/orders.db"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------- БД ----------
def db_init():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            num TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL,
            user_id INTEGER,
            city TEXT,
            total INTEGER,
            payload TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            order_num TEXT PRIMARY KEY,
            buyer_id INTEGER NOT NULL,
            operator_id INTEGER,
            status TEXT NOT NULL DEFAULT 'new',
            created_at INTEGER NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_num TEXT NOT NULL,
            buyer_id INTEGER NOT NULL,
            nick TEXT,
            city TEXT,
            stars INTEGER NOT NULL,
            text TEXT NOT NULL,
            ts INTEGER NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_num TEXT,
            buyer_id INTEGER NOT NULL,
            operator_id INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            created_at INTEGER NOT NULL,
            closed_at INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS operators (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER,
            added_at INTEGER NOT NULL
        )
    """)
    con.commit()
    con.close()

def db_save(order: dict):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT OR REPLACE INTO orders (num, created_at, user_id, city, total, payload) VALUES (?,?,?,?,?,?)",
        (
            order["num"],
            int(order.get("created_at") or time.time()),
            int(order.get("user_id") or 0),
            order.get("city", ""),
            int(order.get("total") or 0),
            json.dumps(order, ensure_ascii=False),
        ),
    )
    con.commit()
    con.close()

def db_get(num: str):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT num, created_at, user_id, city, total, payload FROM orders WHERE num=?",
        (num,),
    ).fetchone()
    con.close()
    if not row:
        return None
    return {
        "num": row[0],
        "created_at": row[1],
        "user_id": row[2],
        "city": row[3],
        "total": row[4],
        "payload": json.loads(row[5]),
    }

def db_last(limit=5):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT num FROM orders ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    con.close()
    return [r[0] for r in rows]

# ---------- Операторы ----------
def ops_add(user_id: int, added_by: int) -> bool:
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(
            "INSERT OR IGNORE INTO operators (user_id, added_by, added_at) VALUES (?,?,?)",
            (user_id, added_by, int(time.time())),
        )
        con.commit()
        ok = con.total_changes > 0
    except Exception:
        ok = False
    con.close()
    return ok

def ops_remove(user_id: int) -> bool:
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM operators WHERE user_id=?", (user_id,))
    ok = con.total_changes > 0
    con.commit()
    con.close()
    return ok

def ops_list():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT user_id, added_by, added_at FROM operators ORDER BY added_at").fetchall()
    con.close()
    return rows

def ops_exists(user_id: int) -> bool:
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT 1 FROM operators WHERE user_id=? LIMIT 1", (user_id,)).fetchone()
    con.close()
    return bool(row)

def is_admin(uid: int) -> bool:
    if uid == SUPER_ADMIN_ID:
        return True
    if uid in ADMIN_IDS:
        return True
    return ops_exists(uid)

def is_super(uid: int) -> bool:
    return uid == SUPER_ADMIN_ID

# ---------- ЧАТЫ ----------
def chat_create(order_num: str, buyer_id: int):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT OR IGNORE INTO chats (order_num, buyer_id, operator_id, status, created_at) VALUES (?,?,?,?,?)",
        (order_num, buyer_id, None, "new", int(time.time())),
    )
    con.commit()
    con.close()

def chat_get(order_num: str):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT order_num, buyer_id, operator_id, status FROM chats WHERE order_num=?",
        (order_num,),
    ).fetchone()
    con.close()
    if not row:
        return None
    return {"order_num": row[0], "buyer_id": row[1], "operator_id": row[2], "status": row[3]}

def chat_take(order_num: str, operator_id: int):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE chats SET operator_id=?, status='taken' WHERE order_num=? AND (operator_id IS NULL OR operator_id=?)",
        (operator_id, order_num, operator_id),
    )
    con.commit()
    con.close()

def chat_by_buyer(buyer_id: int):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT order_num, buyer_id, operator_id, status FROM chats WHERE buyer_id=? AND status IN ('new','taken') ORDER BY created_at DESC LIMIT 1",
        (buyer_id,),
    ).fetchone()
    con.close()
    if not row:
        return None
    return {"order_num": row[0], "buyer_id": row[1], "operator_id": row[2], "status": row[3]}

def chat_by_operator(operator_id: int):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT order_num, buyer_id, operator_id, status FROM chats WHERE operator_id=? AND status='taken' ORDER BY created_at DESC LIMIT 1",
        (operator_id,),
    ).fetchone()
    con.close()
    if not row:
        return None
    return {"order_num": row[0], "buyer_id": row[1], "operator_id": row[2], "status": row[3]}

# ---------- Отзывы ----------
def review_add(order_num: str, buyer_id: int, nick: str, city: str, stars: int, text: str):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO reviews (order_num, buyer_id, nick, city, stars, text, ts) VALUES (?,?,?,?,?,?,?)",
        (order_num, buyer_id, nick, city, stars, text, int(time.time())),
    )
    con.commit()
    con.close()

def review_has(order_num: str, buyer_id: int) -> bool:
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT 1 FROM reviews WHERE order_num=? AND buyer_id=? LIMIT 1",
        (order_num, buyer_id),
    ).fetchone()
    con.close()
    return bool(row)

def order_has_buyer(order_num: str, buyer_id: int) -> bool:
    o = db_get(order_num)
    if not o:
        return False
    if int(o.get("user_id") or 0) == buyer_id:
        return True
    ch = chat_get(order_num)
    return bool(ch and ch["buyer_id"] == buyer_id)

# ---------- Обращения ----------
def ticket_create(order_num: str, buyer_id: int) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "INSERT INTO tickets (order_num, buyer_id, operator_id, status, created_at) VALUES (?,?,?,?,?)",
        (order_num or "", buyer_id, None, "open", int(time.time())),
    )
    tid = cur.lastrowid
    con.commit()
    con.close()
    return tid

def ticket_get(tid: int):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT id, order_num, buyer_id, operator_id, status, created_at, closed_at FROM tickets WHERE id=?",
        (tid,),
    ).fetchone()
    con.close()
    if not row:
        return None
    return {
        "id": row[0], "order_num": row[1], "buyer_id": row[2],
        "operator_id": row[3], "status": row[4],
        "created_at": row[5], "closed_at": row[6],
    }

def ticket_take(tid: int, operator_id: int):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE tickets SET operator_id=?, status='taken' WHERE id=? AND (operator_id IS NULL OR operator_id=?)",
        (operator_id, tid, operator_id),
    )
    con.commit()
    con.close()

def ticket_close(tid: int):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE tickets SET status='closed', closed_at=? WHERE id=?",
        (int(time.time()), tid),
    )
    con.commit()
    con.close()

def ticket_by_buyer(buyer_id: int):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT id, order_num, buyer_id, operator_id, status, created_at, closed_at FROM tickets WHERE buyer_id=? AND status IN ('open','taken') ORDER BY created_at DESC LIMIT 1",
        (buyer_id,),
    ).fetchone()
    con.close()
    if not row:
        return None
    return {
        "id": row[0], "order_num": row[1], "buyer_id": row[2],
        "operator_id": row[3], "status": row[4],
        "created_at": row[5], "closed_at": row[6],
    }

def ticket_by_operator(operator_id: int):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT id, order_num, buyer_id, operator_id, status, created_at, closed_at FROM tickets WHERE operator_id=? AND status='taken' ORDER BY created_at DESC LIMIT 1",
        (operator_id,),
    ).fetchone()
    con.close()
    if not row:
        return None
    return {
        "id": row[0], "order_num": row[1], "buyer_id": row[2],
        "operator_id": row[3], "status": row[4],
        "created_at": row[5], "closed_at": row[6],
    }

def tickets_all(status_filter: str = "open"):
    con = sqlite3.connect(DB_PATH)
    if status_filter == "all":
        rows = con.execute(
            "SELECT id, order_num, buyer_id, operator_id, status, created_at FROM tickets ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id, order_num, buyer_id, operator_id, status, created_at FROM tickets WHERE status=? ORDER BY created_at DESC LIMIT 50",
            (status_filter,),
        ).fetchall()
    con.close()
    return rows

# ---------- Состояние ожидания ----------
WAITING = set()
WAITING_REVIEW = set()
WAITING_REVIEW_TEXT = {}
WAITING_OP_ADD = set()
WAITING_OP_REMOVE = set()

# ---------- Отправка с локальной картинкой ----------
async def send_photo_safe(target, photo_path: Path, caption: str, kb=None):
    try:
        if photo_path and photo_path.is_file():
            return await bot.send_photo(
                target,
                photo=FSInputFile(str(photo_path)),
                caption=caption,
                reply_markup=kb,
                parse_mode="HTML",
            )
        print(f"[send_photo] файл не найден: {photo_path}")
    except Exception as e:
        print(f"[send_photo] ошибка {photo_path}: {e}")
    return await bot.send_message(
        target, caption, reply_markup=kb,
        disable_web_page_preview=True, parse_mode="HTML",
    )

async def send_brand(target, text: str, kb=None):
    await send_photo_safe(target, BRAND_PHOTO, text, kb)

async def send_order(target, text: str, kb=None):
    photo = ORDER_PHOTO if ORDER_PHOTO.is_file() else BRAND_PHOTO
    await send_photo_safe(target, photo, text, kb)

async def send_operator(target, text: str, kb=None):
    photo = OPERATOR_PHOTO if OPERATOR_PHOTO.is_file() else BRAND_PHOTO
    await send_photo_safe(target, photo, text, kb)

# ---------- Форматирование ----------
def fmt_order(o: dict) -> str:
    p = o["payload"]
    items = p.get("items", [])
    lines = []
    for it in items:
        line = f"• <b>{it['name']}</b> · {it['weight']} × {it['qty']} — <b>{it['sum']} ₽</b>"
        line += "  <i>(Магнит)</i>" if it.get("pick") == "magnet" else "  <i>(Прикоп)</i>"
        lines.append(line)
    created = time.strftime("%d.%m.%Y %H:%M", time.localtime(o["created_at"]))
    return (
        f"🧾 <b>Заказ {o['num']}</b>\n"
        f"<i>Дата:</i> {created}\n"
        f"<i>Город:</i> {o.get('city','—')}\n"
        f"<i>Сумма:</i> <b>{o.get('total',0)} ₽</b>\n\n"
        f"<b>Состав:</b>\n" + ("\n".join(lines) if lines else "—")
    )

def order_items_text(o: dict) -> str:
    p = o["payload"]
    items = p.get("items", [])
    lines = []
    for idx, it in enumerate(items, 1):
        pick = "Магнит" if it.get("pick") == "magnet" else "Прикоп"
        lines.append(
            f"{idx}. <b>{it['name']}</b>\n"
            f"   {it['weight']} × {it['qty']} — <b>{it['sum']} ₽</b> · <i>{pick}</i>"
        )
    created = time.strftime("%d.%m.%Y %H:%M", time.localtime(o["created_at"]))
    return (
        f"🛍 <b>Ваш заказ {o['num']}</b>\n"
        f"<i>{created} · {o.get('city','—')}</i>\n\n"
        + ("\n\n".join(lines) if lines else "—")
        + f"\n\n💰 <b>Итого: {o.get('total',0)} ₽</b>"
    )

# ---------- Клавиатуры ----------
def main_menu_kb() -> InlineKeyboardMarkup:
    rows = []
    if SITE_URL:
        rows.append([InlineKeyboardButton(text="🌐 Открыть сайт", url=SITE_URL)])
    rows.append([InlineKeyboardButton(text="🧾 Проверить заказ", callback_data="check_order")])
    rows.append([InlineKeyboardButton(text="💬 Поддержка", callback_data="support")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_panel_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📋 Все обращения", callback_data="admin_tickets")],
        [InlineKeyboardButton(text="🧾 Последние заказы", callback_data="last_orders")],
    ]
    if is_super(uid):
        rows.append([InlineKeyboardButton(text="👥 Операторы", callback_data="ops_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def ops_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить оператора", callback_data="ops_add")],
        [InlineKeyboardButton(text="➖ Снять оператора", callback_data="ops_remove")],
        [InlineKeyboardButton(text="📋 Список операторов", callback_data="ops_list")],
        [InlineKeyboardButton(text="◀ В меню", callback_data="back_menu")],
    ])

def order_kb(num: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Оформить заказ", callback_data=f"order_confirm:{num}")],
        [InlineKeyboardButton(text="◀ В меню", callback_data="back_menu")],
    ])

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀ В меню", callback_data="back_menu")]
    ])

def admin_order_kb(num: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤝 Взять заказ", callback_data=f"take:{num}")],
    ])

def buyer_chat_kb(num: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать оператору", callback_data=f"to_op:{num}")],
        [InlineKeyboardButton(text="⭐ Оставить отзыв", callback_data=f"review:{num}")],
    ])

def op_chat_kb(num: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Написать покупателю", callback_data=f"to_buyer:{num}")],
        [InlineKeyboardButton(text="🔒 Закрыть заказ", callback_data=f"close:{num}")],
    ])

def support_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Открыть обращение", callback_data="ticket_open")],
        [InlineKeyboardButton(text="◀ В меню", callback_data="back_menu")],
    ])

def ticket_buyer_kb(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔒 Закрыть обращение", callback_data=f"tclose:{tid}")],
    ])

def ticket_operator_kb(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔒 Закрыть обращение", callback_data=f"tclose:{tid}")],
    ])

def admin_ticket_kb(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤝 Взять обращение", callback_data=f"ttake:{tid}")],
    ])

def admin_tickets_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Открытые", callback_data="tlist:open")],
        [InlineKeyboardButton(text="🟡 В работе", callback_data="tlist:taken")],
        [InlineKeyboardButton(text="⚪ Закрытые", callback_data="tlist:closed")],
        [InlineKeyboardButton(text="📋 Все", callback_data="tlist:all")],
        [InlineKeyboardButton(text="◀ В меню", callback_data="back_menu")],
    ])

def last_orders_kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=n, callback_data=f"lookup:{n}")] for n in rows
    ] + [[InlineKeyboardButton(text="◀ В меню", callback_data="back_menu")]])

# ---------- Тексты ----------
def welcome_text() -> str:
    site_line = f"🌐 <b>Сайт:</b> {SITE_URL}" if SITE_URL else "🌐 <b>Сайт:</b> —"
    support_line = f"💬 <b>Поддержка:</b> {SUPPORT_URL}" if SUPPORT_URL else "💬 <b>Поддержка:</b> этот чат"
    return (
        "🌌 <b>NEBULA</b>\n"
        "<i>магазин · витрина · города</i>\n"
        "────────────\n\n"
        f"{site_line}\n"
        f"{support_line}\n\n"
        "Что можно сделать:\n"
        "• проверить заказ по номеру <code>NEB-XXXXXX-YYYY</code>\n"
        "• открыть сайт и оформить новый заказ\n"
        "• написать в поддержку (скрины, PDF, чеки)"
    )

# ---------- Хендлеры пользователя ----------
@dp.message(CommandStart())
async def cmd_start(msg: types.Message):
    arg = ""
    if msg.text and " " in msg.text:
        arg = msg.text.split(" ", 1)[1].strip().upper()

    if arg.startswith("NEB-"):
        order = db_get(arg)
        if order:
            chat_create(order["num"], msg.from_user.id)
            await send_order(msg.chat.id, order_items_text(order), order_kb(order["num"]))
        else:
            await send_order(
                msg.chat.id,
                f"❓ Заказ <b>{arg}</b> не найден.\nПроверь номер или напиши в поддержку.",
                back_kb(),
            )
        return

    await send_brand(msg.chat.id, welcome_text(), main_menu_kb())
    if is_admin(msg.from_user.id):
        await bot.send_message(
            msg.chat.id,
            "🛠 <b>Панель оператора</b>",
            reply_markup=admin_panel_kb(msg.from_user.id),
            parse_mode="HTML",
        )

@dp.callback_query(F.data == "back_menu")
async def cb_back(cb: types.CallbackQuery):
    WAITING.discard(cb.from_user.id)
    WAITING_REVIEW.discard(cb.from_user.id)
    WAITING_OP_ADD.discard(cb.from_user.id)
    WAITING_OP_REMOVE.discard(cb.from_user.id)
    try:
        await cb.message.edit_text(welcome_text(), reply_markup=main_menu_kb(), disable_web_page_preview=True)
    except Exception:
        await send_brand(cb.message.chat.id, welcome_text(), main_menu_kb())
    await cb.answer()

@dp.callback_query(F.data == "check_order")
async def cb_check(cb: types.CallbackQuery):
    WAITING.add(cb.from_user.id)
    try:
        await cb.message.edit_text(
            "🧾 Пришли номер заказа вида <code>NEB-XXXXXX-YYYY</code>.\n"
            "Он показан на сайте после оформления.",
            reply_markup=back_kb(),
        )
    except Exception:
        await send_brand(cb.message.chat.id,
            "🧾 Пришли номер заказа вида <code>NEB-XXXXXX-YYYY</code>.", back_kb())
    await cb.answer()

# ---------- Управление операторами ----------
@dp.callback_query(F.data == "ops_menu")
async def cb_ops_menu(cb: types.CallbackQuery):
    if not is_super(cb.from_user.id):
        await cb.answer("Только главный админ", show_alert=True)
        return
    await cb.answer()
    try:
        await cb.message.edit_text(
            "👥 <b>Управление операторами</b>\n"
            "Операторы получают доступ к заказам и обращениям, но не могут назначать других.",
            reply_markup=ops_menu_kb(),
            parse_mode="HTML",
        )
    except Exception:
        await bot.send_message(cb.from_user.id, "👥 <b>Управление операторами</b>", reply_markup=ops_menu_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "ops_add")
async def cb_ops_add(cb: types.CallbackQuery):
    if not is_super(cb.from_user.id):
        await cb.answer("Только главный админ", show_alert=True)
        return
    WAITING_OP_ADD.add(cb.from_user.id)
    await cb.answer()
    await bot.send_message(
        cb.from_user.id,
        "➕ Отправь <b>ID пользователя</b>, которого нужно назначить оператором.\n"
        "Пример: <code>123456789</code>",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )

@dp.callback_query(F.data == "ops_remove")
async def cb_ops_remove(cb: types.CallbackQuery):
    if not is_super(cb.from_user.id):
        await cb.answer("Только главный админ", show_alert=True)
        return
    WAITING_OP_REMOVE.add(cb.from_user.id)
    await cb.answer()
    await bot.send_message(
        cb.from_user.id,
        "➖ Отправь <b>ID оператора</b>, которого нужно снять.\n"
        "Пример: <code>123456789</code>",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )

@dp.callback_query(F.data == "ops_list")
async def cb_ops_list(cb: types.CallbackQuery):
    if not is_super(cb.from_user.id):
        await cb.answer("Только главный админ", show_alert=True)
        return
    rows = ops_list()
    await cb.answer()
    if not rows:
        await cb.message.edit_text("Список операторов пуст.", reply_markup=ops_menu_kb())
        return
    lines = []
    for uid, by, ts in rows:
        when = time.strftime("%d.%m.%Y", time.localtime(ts))
        lines.append(f"• <code>{uid}</code> — добавил <code>{by}</code> · {when}")
    await cb.message.edit_text(
        "👥 <b>Операторы</b>\n\n" + "\n".join(lines),
        reply_markup=ops_menu_kb(),
        parse_mode="HTML",
    )

# ---------- Поддержка / обращения ----------
@dp.callback_query(F.data == "support")
async def cb_support(cb: types.CallbackQuery):
    t = ticket_by_buyer(cb.from_user.id)
    await cb.answer()
    if t and t["status"] in ("open", "taken"):
        await bot.send_message(
            cb.from_user.id,
            f"💬 У вас уже есть активное обращение <b>#{t['id']}</b>.\n"
            "Можете отправить сообщение или файл (фото/PDF/чек) — оператор увидит.",
            reply_markup=ticket_buyer_kb(t["id"]),
            parse_mode="HTML",
        )
        return
    await bot.send_message(
        cb.from_user.id,
        "💬 <b>Поддержка</b>\n"
        "Опишите проблему и при необходимости приложите скрин или PDF-чек.\n"
        "Можно отправлять фото и документы.",
        reply_markup=support_kb(),
        parse_mode="HTML",
    )

@dp.callback_query(F.data == "ticket_open")
async def cb_ticket_open(cb: types.CallbackQuery):
    t = ticket_by_buyer(cb.from_user.id)
    if t and t["status"] in ("open", "taken"):
        await cb.answer("Уже есть активное обращение", show_alert=True)
        return
    tid = ticket_create("", cb.from_user.id)
    await cb.answer("Обращение создано")
    for op in set(ADMIN_IDS + [SUPER_ADMIN_ID]):
        try:
            await bot.send_message(
                op,
                f"🆘 <b>Новое обращение #{tid}</b>\n"
                f"Покупатель: <code>{cb.from_user.id}</code>",
                reply_markup=admin_ticket_kb(tid),
                parse_mode="HTML",
            )
        except Exception:
            pass
    try:
        await cb.message.edit_text(
            f"✅ Обращение <b>#{tid}</b> создано.\n"
            "Опишите проблему и приложите скрин/PDF — оператор подключится.",
            reply_markup=ticket_buyer_kb(tid),
            parse_mode="HTML",
        )
    except Exception:
        pass

@dp.callback_query(F.data.startswith("ttake:"))
async def cb_ticket_take(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Недостаточно прав", show_alert=True)
        return
    tid = int(cb.data.split(":", 1)[1])
    t = ticket_get(tid)
    if not t:
        await cb.answer("Обращение не найдено", show_alert=True)
        return
    if t["operator_id"] and t["operator_id"] != cb.from_user.id:
        await cb.answer("Уже взято другим оператором", show_alert=True)
        return
    ticket_take(tid, cb.from_user.id)
    await cb.answer("Взято")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await bot.send_message(
        cb.from_user.id,
        f"🤝 Вы взяли обращение <b>#{tid}</b>.\nПокупатель: <code>{t['buyer_id']}</code>",
        reply_markup=ticket_operator_kb(tid),
        parse_mode="HTML",
    )
    try:
        await bot.send_message(
            t["buyer_id"],
            f"👤 Оператор подключился к обращению <b>#{tid}</b>. Пришлите скрины/PDF/чек.",
            reply_markup=ticket_buyer_kb(tid),
            parse_mode="HTML",
        )
    except Exception:
        pass

@dp.callback_query(F.data.startswith("tclose:"))
async def cb_ticket_close(cb: types.CallbackQuery):
    tid = int(cb.data.split(":", 1)[1])
    t = ticket_get(tid)
    if not t:
        await cb.answer("Обращение не найдено", show_alert=True)
        return
    if cb.from_user.id not in (t["buyer_id"], t["operator_id"]) and not is_admin(cb.from_user.id):
        await cb.answer("Недостаточно прав", show_alert=True)
        return
    ticket_close(tid)
    await cb.answer("Обращение закрыто")
    try:
        await cb.message.edit_text(f"🔒 Обращение <b>#{tid}</b> закрыто.", reply_markup=back_kb(), parse_mode="HTML")
    except Exception:
        pass
    for uid in {t["buyer_id"], t["operator_id"]}:
        if uid and uid != cb.from_user.id:
            try:
                await bot.send_message(uid, f"🔒 Обращение <b>#{tid}</b> закрыто.", parse_mode="HTML")
            except Exception:
                pass

@dp.callback_query(F.data == "admin_tickets")
async def cb_admin_tickets(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Недостаточно прав", show_alert=True)
        return
    await cb.answer()
    try:
        await cb.message.edit_text(
            "📋 <b>Все обращения</b>\nВыберите фильтр:",
            reply_markup=admin_tickets_kb(),
            parse_mode="HTML",
        )
    except Exception:
        await bot.send_message(cb.from_user.id, "📋 <b>Все обращения</b>", reply_markup=admin_tickets_kb(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("tlist:"))
async def cb_tickets_list(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Недостаточно прав", show_alert=True)
        return
    flt = cb.data.split(":", 1)[1]
    rows = tickets_all(flt)
    if not rows:
        await cb.answer("Пусто", show_alert=True)
        return
    lines = []
    for r in rows:
        tid, num, buyer, op, status, ts = r
        st = {"open": "🟢", "taken": "🟡", "closed": "⚪"}.get(status, "")
        lines.append(f"{st} #{tid} · {num or '—'} · buyer <code>{buyer}</code> · op {op or '—'}")
    await cb.message.edit_text(
        f"📋 <b>Обращения ({flt})</b>\n\n" + "\n".join(lines),
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await cb.answer()

# ---------- Заказы ----------
@dp.callback_query(F.data.startswith("order_confirm:"))
async def cb_order_confirm(cb: types.CallbackQuery):
    num = cb.data.split(":", 1)[1]
    order = db_get(num)
    if not order:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    chat_create(num, cb.from_user.id)
    await cb.answer("Заказ отправлен оператору")
    for op in set(ADMIN_IDS + [SUPER_ADMIN_ID]):
        try:
            await send_operator(
                op,
                f"🔔 <b>Новый заказ {num}</b>\n"
                f"Покупатель: <code>{cb.from_user.id}</code>\n"
                f"Город: {order.get('city','—')}\n"
                f"Сумма: <b>{order.get('total',0)} ₽</b>\n\n"
                + fmt_order(order),
                admin_order_kb(num),
            )
        except Exception as e:
            print("admin notify failed:", e)
    try:
        await cb.message.edit_text(
            f"⏳ Заказ <b>{num}</b> отправлен оператору.\n"
            "Оператор свяжется с тобой в этом чате.",
            reply_markup=buyer_chat_kb(num),
            parse_mode="HTML",
        )
    except Exception:
        pass

@dp.callback_query(F.data.startswith("take:"))
async def cb_take(cb: types.CallbackQuery):
    num = cb.data.split(":", 1)[1]
    if not is_admin(cb.from_user.id):
        await cb.answer("Недостаточно прав", show_alert=True)
        return
    ch = chat_get(num)
    if not ch:
        await cb.answer("Чат не найден", show_alert=True)
        return
    if ch["operator_id"] and ch["operator_id"] != cb.from_user.id:
        await cb.answer("Заказ уже взят другим оператором", show_alert=True)
        return
    chat_take(num, cb.from_user.id)
    order = db_get(num)
    await cb.answer("Заказ взят")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await send_operator(
        cb.from_user.id,
        f"🤝 Вы взяли заказ <b>{num}</b>.\n"
        f"Покупатель: <code>{ch['buyer_id']}</code>\n\n"
        + (fmt_order(order) if order else ""),
        op_chat_kb(num),
    )
    try:
        await send_operator(
            ch["buyer_id"],
            f"👤 Оператор подключился к заказу <b>{num}</b>.\n"
            "Напиши сюда, если есть вопросы.",
            buyer_chat_kb(num),
        )
    except Exception as e:
        print("notify buyer failed:", e)

@dp.callback_query(F.data.startswith("to_op:"))
async def cb_to_op(cb: types.CallbackQuery):
    num = cb.data.split(":", 1)[1]
    ch = chat_get(num)
    if not ch or not ch["operator_id"]:
        await cb.answer("Оператор ещё не подключился", show_alert=True)
        return
    await cb.answer("Напиши сообщение в этот чат — оно уйдёт оператору")

@dp.callback_query(F.data.startswith("to_buyer:"))
async def cb_to_buyer(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Недостаточно прав", show_alert=True)
        return
    await cb.answer("Напиши сообщение — оно уйдёт покупателю")

@dp.callback_query(F.data.startswith("close:"))
async def cb_close(cb: types.CallbackQuery):
    num = cb.data.split(":", 1)[1]
    if not is_admin(cb.from_user.id):
        await cb.answer("Недостаточно прав", show_alert=True)
        return
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE chats SET status='closed' WHERE order_num=?", (num,))
    con.commit()
    con.close()
    await cb.answer("Заказ закрыт")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

@dp.callback_query(F.data == "last_orders")
async def cb_last(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Недостаточно прав", show_alert=True)
        return
    rows = db_last(5)
    await cb.answer()
    if not rows:
        await cb.message.edit_text("Пока ни одного заказа.", reply_markup=back_kb())
        return
    await cb.message.edit_text("Последние 5 заказов:", reply_markup=last_orders_kb(rows))

@dp.callback_query(F.data.startswith("lookup:"))
async def cb_lookup(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Недостаточно прав", show_alert=True)
        return
    num = cb.data.split(":", 1)[1]
    order = db_get(num)
    if order:
        await cb.message.edit_text(fmt_order(order), reply_markup=back_kb())
    else:
        await cb.message.edit_text(f"❓ Заказ <b>{num}</b> не найден.", reply_markup=back_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("review:"))
async def cb_review(cb: types.CallbackQuery):
    num = cb.data.split(":", 1)[1]
    if not order_has_buyer(num, cb.from_user.id):
        await cb.answer("Это не ваш заказ", show_alert=True)
        return
    if review_has(num, cb.from_user.id):
        await cb.answer("Отзыв по этому заказу уже оставлен", show_alert=True)
        return
    WAITING_REVIEW_TEXT[num] = {"buyer": cb.from_user.id}
    await cb.answer()
    await bot.send_message(
        cb.from_user.id,
        "⭐ <b>Отзыв</b>\n"
        "Отправь оценку и текст в формате:\n"
        "<code>5 текст отзыва</code>  (от 1 до 5 звёзд)\n"
        "<i>За отзыв — скидка 15% на следующий заказ.</i>",
        parse_mode="HTML",
    )

@dp.message(F.text.regexp(r"(?i)^NEB-[A-Z0-9]{6}-[A-Z0-9]{4}$"))
async def msg_lookup(msg: types.Message):
    WAITING.discard(msg.from_user.id)
    num = msg.text.strip().upper()
    order = db_get(num)
    if order:
        chat_create(num, msg.from_user.id)
        await send_order(msg.chat.id, order_items_text(order), order_kb(num))
    else:
        await send_order(msg.chat.id, f"❓ Заказ <b>{num}</b> не найден.", back_kb())

@dp.message(Command("last"))
async def cmd_last(msg: types.Message):
    if not is_admin(msg.from_user.id):
        await msg.answer("Команда только для операторов.")
        return
    rows = db_last(5)
    if not rows:
        await msg.answer("Пока ни одного заказа.")
        return
    await msg.answer("Последние 5 заказов:", reply_markup=last_orders_kb(rows))

# ---------- Универсальный роутер ----------
@dp.message()
async def msg_router(msg: types.Message):
    uid = msg.from_user.id

    # --- назначение оператора ---
    if uid in WAITING_OP_ADD:
        WAITING_OP_ADD.discard(uid)
        if not is_super(uid):
            await msg.answer("Только главный админ.")
            return
        txt = (msg.text or "").strip()
        if not txt.isdigit():
            await msg.answer("Нужен числовой ID. Пример: <code>123456789</code>", parse_mode="HTML")
            return
        target = int(txt)
        if target == SUPER_ADMIN_ID:
            await msg.answer("Это главный админ, он уже имеет доступ.")
            return
        ok = ops_add(target, uid)
        if ok:
            await msg.answer(f"✅ Пользователь <code>{target}</code> назначен оператором.", parse_mode="HTML")
            try:
                await bot.send_message(
                    target,
                    "🛠 Вам выдан доступ оператора NEBULA.\n"
                    "Откройте /start — появится панель оператора.",
                )
            except Exception:
                pass
        else:
            await msg.answer(f"Пользователь <code>{target}</code> уже оператор.", parse_mode="HTML")
        return

    # --- снятие оператора ---
    if uid in WAITING_OP_REMOVE:
        WAITING_OP_REMOVE.discard(uid)
        if not is_super(uid):
            await msg.answer("Только главный админ.")
            return
        txt = (msg.text or "").strip()
        if not txt.isdigit():
            await msg.answer("Нужен числовой ID. Пример: <code>123456789</code>", parse_mode="HTML")
            return
        target = int(txt)
        if target == SUPER_ADMIN_ID:
            await msg.answer("Главного админа снять нельзя.")
            return
        ok = ops_remove(target)
        if ok:
            await msg.answer(f"✅ Пользователь <code>{target}</code> больше не оператор.", parse_mode="HTML")
            try:
                await bot.send_message(target, "🛠 Ваш доступ оператора NEBULA снят.")
            except Exception:
                pass
        else:
            await msg.answer(f"Пользователь <code>{target}</code> не найден в списке операторов.", parse_mode="HTML")
        return

    # --- ждём номер заказа ---
    if uid in WAITING:
        WAITING.discard(uid)
        num = (msg.text or "").strip().upper()
        order = db_get(num)
        if order:
            chat_create(num, uid)
            await send_order(msg.chat.id, order_items_text(order), order_kb(num))
        else:
            await send_order(msg.chat.id,
                f"❓ Заказ <b>{num}</b> не найден.\nПопробуй ещё раз или напиши в поддержку.",
                back_kb())
        return

    # --- отзыв ---
    for num, st in list(WAITING_REVIEW_TEXT.items()):
        if st.get("buyer") == uid:
            text = (msg.text or "").strip()
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not parts[0].isdigit() or not (1 <= int(parts[0]) <= 5):
                await msg.answer("Формат: <code>5 текст отзыва</code> (1–5 звёзд).", parse_mode="HTML")
                return
            stars = int(parts[0])
            body = parts[1].strip()
            if len(body) < 3:
                await msg.answer("Слишком короткий отзыв.")
                return
            o = db_get(num)
            nick = (msg.from_user.full_name or "anonymous")[:20]
            city = (o.get("city") if o else "") or "—"
            review_add(num, uid, nick, city, stars, body[:200])
            WAITING_REVIEW_TEXT.pop(num, None)
            await msg.answer(
                "✅ Отзыв сохранён.\n"
                "<i>Скидка 15% на следующий заказ закреплена за вашим заказом.</i>",
                parse_mode="HTML",
            )
            for op in set(ADMIN_IDS + [SUPER_ADMIN_ID]):
                try:
                    await bot.send_message(
                        op,
                        f"⭐ <b>Новый отзыв</b> по {num}\n"
                        f"Оценка: {'★'*stars}{'☆'*(5-stars)}\n"
                        f"{body[:200]}",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            return

    # --- вложения ---
    is_media = bool(msg.photo or msg.document or msg.video or msg.audio or msg.voice or msg.sticker)
    if is_media:
        t = ticket_by_buyer(uid)
        if t and t["operator_id"]:
            try:
                await bot.copy_message(chat_id=t["operator_id"], from_chat_id=msg.chat.id, message_id=msg.message_id)
                await bot.send_message(t["operator_id"], f"📎 Вложение по обращению <b>#{t['id']}</b> от <code>{uid}</code>", parse_mode="HTML")
                await msg.answer("📎 Отправлено оператору.")
            except Exception as e:
                await msg.answer(f"Не удалось отправить: {e}")
            return

        ch = chat_by_buyer(uid)
        if ch and ch["operator_id"]:
            try:
                await bot.copy_message(chat_id=ch["operator_id"], from_chat_id=msg.chat.id, message_id=msg.message_id)
                await msg.answer("📎 Отправлено оператору.")
            except Exception as e:
                await msg.answer(f"Не удалось отправить: {e}")
            return

        if is_admin(uid):
            to = ticket_by_operator(uid)
            if to:
                try:
                    await bot.copy_message(chat_id=to["buyer_id"], from_chat_id=msg.chat.id, message_id=msg.message_id)
                    await msg.answer("📎 Отправлено покупателю.")
                except Exception as e:
                    await msg.answer(f"Не удалось отправить: {e}")
                return
            ch2 = chat_by_operator(uid)
            if ch2:
                try:
                    await bot.copy_message(chat_id=ch2["buyer_id"], from_chat_id=msg.chat.id, message_id=msg.message_id)
                    await msg.answer("📎 Отправлено покупателю.")
                except Exception as e:
                    await msg.answer(f"Не удалось отправить: {e}")
                return

        await msg.answer("📎 Сначала откройте обращение в разделе «Поддержка».")
        return

    # --- оператор текстом ---
    if is_admin(uid):
        to = ticket_by_operator(uid)
        if to:
            try:
                await bot.send_message(to["buyer_id"], f"👤 <b>Оператор (обращение #{to['id']}):</b>\n{msg.text or ''}", parse_mode="HTML")
                await msg.answer("✉️ Отправлено покупателю.")
            except Exception as e:
                await msg.answer(f"Не удалось отправить: {e}")
            return
        ch = chat_by_operator(uid)
        if ch:
            try:
                await bot.send_message(ch["buyer_id"], f"👤 <b>Оператор:</b>\n{msg.text or ''}", parse_mode="HTML")
                await msg.answer("✉️ Отправлено покупателю.")
            except Exception as e:
                await msg.answer(f"Не удалось отправить: {e}")
            return
        await send_brand(msg.chat.id, welcome_text(), main_menu_kb())
        await bot.send_message(msg.chat.id, "🛠 <b>Панель оператора</b>", reply_markup=admin_panel_kb(uid), parse_mode="HTML")
        return

    # --- покупатель текстом ---
    t = ticket_by_buyer(uid)
    if t:
        if not t["operator_id"]:
            await msg.answer("⏳ Оператор ещё не подключился к обращению. Подожди немного.")
            return
        try:
            await bot.send_message(t["operator_id"], f"👤 <b>Покупатель (обращение #{t['id']}):</b>\n{msg.text or ''}", parse_mode="HTML")
            await msg.answer("✉️ Отправлено оператору.")
        except Exception as e:
            await msg.answer(f"Не удалось отправить: {e}")
        return

    ch = chat_by_buyer(uid)
    if ch:
        if not ch["operator_id"]:
            await msg.answer("⏳ Оператор ещё не подключился. Подожди немного.")
            return
        try:
            await bot.send_message(ch["operator_id"], f"👤 <b>Покупатель</b> ({ch['order_num']}):\n{msg.text or ''}", parse_mode="HTML")
            await msg.answer("✉️ Отправлено оператору.")
        except Exception as e:
            await msg.answer(f"Не удалось отправить: {e}")
        return

    await send_brand(msg.chat.id, welcome_text(), main_menu_kb())

# ---------- HTTP webhook ----------
def check_secret(request: web.Request) -> bool:
    return request.headers.get("X-Secret", "") == WEBHOOK_SECRET

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, X-Secret",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
}

async def handle_order(request: web.Request):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=CORS_HEADERS)
    if not check_secret(request):
        resp = web.json_response({"ok": False, "err": "bad secret"}, status=403)
        resp.headers.update(CORS_HEADERS)
        return resp
    try:
        data = await request.json()
    except Exception:
        resp = web.json_response({"ok": False, "err": "bad json"}, status=400)
        resp.headers.update(CORS_HEADERS)
        return resp

    num = str(data.get("num", "")).strip().upper()
    if not num.startswith("NEB-"):
        resp = web.json_response({"ok": False, "err": "bad num"}, status=400)
        resp.headers.update(CORS_HEADERS)
        return resp

    order = {
        "num": num,
        "created_at": int(data.get("created_at") or time.time()),
        "user_id": int(data.get("user_id") or 0),
        "city": data.get("city", ""),
        "total": int(data.get("total") or 0),
        "items": data.get("items", []),
    }
    db_save(order)
    print(f"[webhook] saved order {num}, total={order['total']}, items={len(order['items'])}")

    for op in set(ADMIN_IDS + [SUPER_ADMIN_ID]):
        try:
            await send_operator(
                op,
                f"🔔 <b>Новый заказ {num}</b>\n"
                f"Город: {order.get('city','—')}\n"
                f"Сумма: <b>{order.get('total',0)} ₽</b>\n\n"
                + fmt_order(order),
                admin_order_kb(num),
            )
        except Exception as e:
            print("admin notify failed:", e)

    resp = web.json_response({"ok": True, "num": num})
    resp.headers.update(CORS_HEADERS)
    return resp

async def handle_health(request: web.Request):
    return web.json_response({"ok": True, "time": int(time.time())})

def make_app():
    app = web.Application()
    app.router.add_route("OPTIONS", "/order", handle_order)
    app.router.add_post("/order", handle_order)
    app.router.add_get("/health", handle_health)
    return app

# ---------- Запуск ----------
import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main():
    logger.info("starting bot…")
    logger.info(f"BOT_TOKEN present: {bool(BOT_TOKEN)}")
    logger.info(f"ADMIN_IDS: {ADMIN_IDS}")
    logger.info(f"SUPER_ADMIN_ID: {SUPER_ADMIN_ID}")
    logger.info(f"IMG_DIR: {IMG_DIR}")
    logger.info(f"BRAND_PHOTO:    {BRAND_PHOTO} (exists={BRAND_PHOTO.is_file()})")
    logger.info(f"OPERATOR_PHOTO: {OPERATOR_PHOTO} (exists={OPERATOR_PHOTO.is_file()})")
    logger.info(f"ORDER_PHOTO:    {ORDER_PHOTO} (exists={ORDER_PHOTO.is_file()})")
    logger.info(f"DB_PATH: {DB_PATH}")

    db_init()
    logger.info("db initialized")

    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    logger.info(f"webhook on :{WEBHOOK_PORT}")

    try:
        me = await bot.get_me()
        logger.info(f"bot connected: @{me.username} (id={me.id})")
    except Exception as e:
        logger.error(f"bot connection failed: {e}")
        return

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.warning(f"delete_webhook: {e}")

    logger.info("starting polling…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
