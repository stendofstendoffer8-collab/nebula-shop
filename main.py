import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
bot = Bot(token=BOT_TOKEN)
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
SITE_URL = os.getenv("SITE_URL", "https://nebula-kungur.netlify.app").strip()
SUPPORT_URL = os.getenv("SUPPORT_URL", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "nebula_secret").strip()
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080") or 8080)

DB_PATH = Path(__file__).parent / "orders.db"

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

# ---------- Бот ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Состояние «жду номер заказа» для каждого пользователя
WAITING = set()

def fmt_order(o: dict) -> str:
    p = o["payload"]
    items = p.get("items", [])
    lines = []
    for it in items:
        line = f"• {it['name']} · {it['weight']} × {it['qty']} — {it['sum']} ₽"
        line += "  (Магнит)" if it.get("pick") == "magnet" else "  (Прикоп)"
        lines.append(line)
    created = time.strftime("%d.%m.%Y %H:%M", time.localtime(o["created_at"]))
    text = (
        f"🧾 Заказ <b>{o['num']}</b>\n"
        f"Дата: {created}\n"
        f"Город: {o.get('city','—')}\n"
        f"Сумма: <b>{o.get('total',0)} ₽</b>\n\n"
        f"Состав:\n" + ("\n".join(lines) if lines else "—")
    )
    return text

def main_menu_kb() -> InlineKeyboardMarkup:
    rows = []
    if SITE_URL:
        rows.append([InlineKeyboardButton(text="🌐 Открыть сайт", url=SITE_URL)])
    rows.append([InlineKeyboardButton(text="🧾 Проверить заказ", callback_data="check_order")])
    if SUPPORT_URL:
        rows.append([InlineKeyboardButton(text="💬 Поддержка", url=SUPPORT_URL)])
    rows.append([InlineKeyboardButton(text="📋 Последние заказы", callback_data="last_orders")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀ В меню", callback_data="back_menu")]
    ])

def welcome_text() -> str:
    site_line = f"🌐 Сайт: {SITE_URL}" if SITE_URL else "🌐 Сайт: (не задан)"
    support_line = f"💬 Поддержка: {SUPPORT_URL}" if SUPPORT_URL else "💬 Поддержка: напиши в этот чат"
    return (
        "👋 <b>NEBULA</b> — бот магазина.\n\n"
        f"{site_line}\n"
        f"{support_line}\n\n"
        "Что можно сделать:\n"
        "• проверить заказ по номеру <code>NEB-XXXXXX-YYYY</code>\n"
        "• посмотреть последние заказы\n"
        "• открыть сайт и оформить новый заказ"
    )

@dp.message(CommandStart())
async def cmd_start(msg: types.Message):
    # ?start=NEB-XXXXXX-YYYY  → сразу показываем заказ
    arg = ""
    if msg.text and " " in msg.text:
        arg = msg.text.split(" ", 1)[1].strip().upper()

    if arg.startswith("NEB-"):
        order = db_get(arg)
        if order:
            await msg.answer(fmt_order(order), reply_markup=back_kb())
        else:
            await msg.answer(
                f"❓ Заказ <b>{arg}</b> не найден.\n"
                f"Проверь номер или напиши в поддержку.",
                reply_markup=back_kb()
            )
        return

    await msg.answer(welcome_text(), reply_markup=main_menu_kb(), disable_web_page_preview=True)

@dp.callback_query(F.data == "back_menu")
async def cb_back(cb: types.CallbackQuery):
    WAITING.discard(cb.from_user.id)
    await cb.message.edit_text(welcome_text(), reply_markup=main_menu_kb(), disable_web_page_preview=True)
    await cb.answer()

@dp.callback_query(F.data == "check_order")
async def cb_check(cb: types.CallbackQuery):
    WAITING.add(cb.from_user.id)
    await cb.message.edit_text(
        "🧾 Пришли номер заказа вида <code>NEB-XXXXXX-YYYY</code>.\n"
        "Он есть на сайте после оплаты.",
        reply_markup=back_kb()
    )
    await cb.answer()

@dp.callback_query(F.data == "last_orders")
async def cb_last(cb: types.CallbackQuery):
    rows = db_last(5)
    if not rows:
        await cb.message.edit_text("Пока ни одного заказа.", reply_markup=back_kb())
        await cb.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=n, callback_data=f"lookup:{n}")] for n in rows
    ] + [[InlineKeyboardButton(text="◀ В меню", callback_data="back_menu")]])
    await cb.message.edit_text("Последние 5 заказов:", reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data.startswith("lookup:"))
async def cb_lookup(cb: types.CallbackQuery):
    num = cb.data.split(":", 1)[1]
    order = db_get(num)
    if order:
        await cb.message.edit_text(fmt_order(order), reply_markup=back_kb())
    else:
        await cb.message.edit_text(f"❓ Заказ <b>{num}</b> не найден.", reply_markup=back_kb())
    await cb.answer()

@dp.message(F.text.regexp(r"(?i)^NEB-[A-Z0-9]{6}-[A-Z0-9]{4}$"))
async def msg_lookup(msg: types.Message):
    WAITING.discard(msg.from_user.id)
    num = msg.text.strip().upper()
    order = db_get(num)
    if order:
        await msg.answer(fmt_order(order), reply_markup=back_kb())
    else:
        await msg.answer(f"❓ Заказ <b>{num}</b> не найден.", reply_markup=back_kb())

@dp.message(Command("last"))
async def cmd_last(msg: types.Message):
    rows = db_last(5)
    if not rows:
        await msg.answer("Пока ни одного заказа.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=n, callback_data=f"lookup:{n}")] for n in rows
    ])
    await msg.answer("Последние 5 заказов:", reply_markup=kb)

@dp.message()
async def msg_fallback(msg: types.Message):
    uid = msg.from_user.id
    if uid in WAITING:
        WAITING.discard(uid)
        num = (msg.text or "").strip().upper()
        order = db_get(num)
        if order:
            await msg.answer(fmt_order(order), reply_markup=back_kb())
        else:
            await msg.answer(
                f"❓ Заказ <b>{num}</b> не найден.\nПопробуй ещё раз или напиши в поддержку.",
                reply_markup=back_kb()
            )
        return
    await msg.answer(welcome_text(), reply_markup=main_menu_kb(), disable_web_page_preview=True)

# ---------- HTTP webhook от сайта ----------
def check_secret(request: web.Request) -> bool:
    return request.headers.get("X-Secret", "") == WEBHOOK_SECRET

async def handle_order(request: web.Request):
    if not check_secret(request):
        return web.json_response({"ok": False, "err": "bad secret"}, status=403)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "err": "bad json"}, status=400)

    num = str(data.get("num", "")).strip().upper()
    if not num.startswith("NEB-"):
        return web.json_response({"ok": False, "err": "bad num"}, status=400)

    order = {
        "num": num,
        "created_at": int(data.get("created_at") or time.time()),
        "user_id": int(data.get("user_id") or 0),
        "city": data.get("city", ""),
        "total": int(data.get("total") or 0),
        "items": data.get("items", []),
    }
    db_save(order)

    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, "🔔 Новый заказ\n\n" + fmt_order(order))
        except Exception as e:
            print("admin notify failed:", e)

    return web.json_response({"ok": True, "num": num})

async def handle_health(request: web.Request):
    return web.json_response({"ok": True, "time": int(time.time())})

def make_app():
    app = web.Application()
    app.router.add_post("/order", handle_order)
    app.router.add_get("/health", handle_health)
    return app

# ---------- Запуск ----------
async def main():
    db_init()
    print("bot starting…")
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    print(f"webhook on :{WEBHOOK_PORT}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())