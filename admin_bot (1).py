"""
Админ-бот для управления ScriptAI.

Переменные окружения:
  ADMIN_BOT_TOKEN  — токен этого бота
  MAIN_BOT_TOKEN   — токен основного бота (для уведомлений пользователям)
  DATABASE_URL     — та же строка что у основного бота
  ADMIN_IDS        — Telegram ID администраторов через запятую
"""

import asyncio
import logging
import os
import asyncpg
from datetime import datetime, date, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
DATABASE_URL    = os.getenv("DATABASE_URL")
USD_TO_RUB      = 86.0  # фиксированный курс — держать синхронно с основным ботом

# Себестоимость моделей ($ за 1M токенов: input, output)
MODEL_COSTS_USD = {
    "google/gemini-2.5-flash-lite":  (0.10,  0.40),
    "x-ai/grok-4.1-fast":            (0.20,  0.50),
    "anthropic/claude-haiku-4.5":    (1.00,  5.00),
    "openai/gpt-5.1":                (1.25, 10.00),
    "anthropic/claude-sonnet-4.6":   (3.00, 15.00),
}
# Средний расход токенов на 1 минуту сценария
AVG_INPUT_TOKENS_PER_MIN  = 600
AVG_OUTPUT_TOKENS_PER_MIN = 475

_raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[int] = {int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()}

if not ADMIN_BOT_TOKEN:
    raise RuntimeError("Не задан ADMIN_BOT_TOKEN")
if not ADMIN_IDS:
    raise RuntimeError("Не задан ADMIN_IDS")

bot      = Bot(token=ADMIN_BOT_TOKEN)
main_bot = Bot(token=os.getenv("MAIN_BOT_TOKEN", ADMIN_BOT_TOKEN))
dp       = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)

db_pool: asyncpg.Pool | None = None


@dp.startup()
async def on_startup():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, ssl="prefer")
    logging.info(f"Админ-бот готов | Админы: {ADMIN_IDS}")


@dp.shutdown()
async def on_shutdown():
    if db_pool:
        await db_pool.close()


def _now() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def rub_to_usd(rub: float) -> str:
    return f"~${rub / USD_TO_RUB:.2f}"


def estimate_api_cost_rub(model_id: str, duration_min: float) -> float:
    """Оценочная себестоимость генерации в рублях."""
    if model_id not in MODEL_COSTS_USD:
        return 0.0
    input_cost, output_cost = MODEL_COSTS_USD[model_id]
    input_usd  = (AVG_INPUT_TOKENS_PER_MIN  * duration_min / 1_000_000) * input_cost
    output_usd = (AVG_OUTPUT_TOKENS_PER_MIN * duration_min / 1_000_000) * output_cost
    return (input_usd + output_usd) * USD_TO_RUB


async def resolve_user(query: str) -> dict | None:
    query = query.strip().lstrip("@")
    async with db_pool.acquire() as conn:
        if query.lstrip("-").isdigit():
            row = await conn.fetchrow("SELECT * FROM settings WHERE user_id=$1", int(query))
        else:
            row = await conn.fetchrow(
                "SELECT * FROM settings WHERE lower(username)=$1", query.lower()
            )
    return dict(row) if row else None


async def admin_only(message: types.Message) -> bool:
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return False
    return True


def get_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💳 Пополнить",   callback_data="cmd_topup"),
            InlineKeyboardButton(text="➖ Списать",      callback_data="cmd_deduct"),
        ],
        [
            InlineKeyboardButton(text="🔍 Проверить",   callback_data="cmd_check"),
            InlineKeyboardButton(text="📋 История",      callback_data="cmd_history"),
        ],
        [
            InlineKeyboardButton(text="🔴 Бан",          callback_data="cmd_ban"),
            InlineKeyboardButton(text="🟢 Разбан",       callback_data="cmd_unban"),
        ],
        [
            InlineKeyboardButton(text="🔎 Поиск",        callback_data="cmd_search"),
        ],
        [
            InlineKeyboardButton(text="📊 Статистика",   callback_data="cmd_stats"),
            InlineKeyboardButton(text="💵 Выручка",      callback_data="cmd_revenue"),
        ],
        [
            InlineKeyboardButton(text="🏆 Топ",          callback_data="cmd_top"),
        ],
        [
            InlineKeyboardButton(text="📢 Рассылка",     callback_data="cmd_broadcast"),
            InlineKeyboardButton(text="🎁 Начислить всем", callback_data="cmd_give_all"),
        ],
    ])

# ---------------------------------------------------------------------------
# СОСТОЯНИЯ
# ---------------------------------------------------------------------------
class TopupState(StatesGroup):
    waiting_user_id = State()
    waiting_amount  = State()
    waiting_comment = State()

class DeductState(StatesGroup):
    waiting_user_id = State()
    waiting_amount  = State()
    waiting_comment = State()

class CheckState(StatesGroup):
    waiting_user_id = State()

class HistoryState(StatesGroup):
    waiting_user_id = State()

class BanState(StatesGroup):
    waiting_user_id = State()

class UnbanState(StatesGroup):
    waiting_user_id = State()

class SearchState(StatesGroup):
    waiting_user_id = State()

class BroadcastState(StatesGroup):
    waiting_text = State()

class GiveAllState(StatesGroup):
    waiting_amount  = State()
    waiting_comment = State()

# ---------------------------------------------------------------------------
# СТАРТ
# ---------------------------------------------------------------------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    if not await admin_only(message):
        return
    await message.answer(
        "👨‍💼 <b>Админ-панель ScriptAI</b>\n\nВыбери действие:",
        reply_markup=get_admin_kb(),
        parse_mode="HTML",
    )


@dp.message(Command("menu"))
async def menu_cmd(message: types.Message):
    if not await admin_only(message):
        return
    await message.answer(
        "👨‍💼 <b>Меню</b>",
        reply_markup=get_admin_kb(),
        parse_mode="HTML",
    )


# Обработчик всех кнопок главного меню
@dp.callback_query(F.data.startswith("cmd_"))
async def menu_button(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа.", show_alert=True)
        return

    await call.answer()
    cmd = call.data.replace("cmd_", "")

    # Одношаговые команды — выполняем сразу
    if cmd == "stats":
        await stats_cmd(call.message)
        return
    if cmd == "revenue":
        await revenue_cmd(call.message)
        return
    if cmd == "top":
        await top_cmd(call.message)
        return

    # Многошаговые — запрашиваем ввод
    prompts = {
        "topup":     ("💳 <b>Пополнение</b>\n\nВведи ID или @username:", TopupState.waiting_user_id),
        "deduct":    ("➖ <b>Списание</b>\n\nВведи ID или @username:", DeductState.waiting_user_id),
        "check":     ("🔍 Введи ID или @username:", CheckState.waiting_user_id),
        "history":   ("📋 Введи ID или @username:", HistoryState.waiting_user_id),
        "ban":       ("🔴 Введи ID или @username для бана:", BanState.waiting_user_id),
        "unban":     ("🟢 Введи ID или @username для разбана:", UnbanState.waiting_user_id),
        "search":    ("🔎 Введи ID или @username:", SearchState.waiting_user_id),
        "broadcast": ("📢 Введи текст рассылки:", BroadcastState.waiting_text),
        "give_all":  ("🎁 Сколько кредитов начислить всем?", GiveAllState.waiting_amount),
    }

    if cmd in prompts:
        prompt, next_state = prompts[cmd]
        await call.message.answer(prompt, parse_mode="HTML")
        await state.set_state(next_state)

# ---------------------------------------------------------------------------
# СТАТИСТИКА
# ---------------------------------------------------------------------------
@dp.message(Command("stats"))
async def stats_cmd(message: types.Message):
    if not await admin_only(message):
        return

    today    = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=7)).isoformat()

    async with db_pool.acquire() as conn:
        total_users   = await conn.fetchval("SELECT COUNT(*) FROM settings")
        total_scripts = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status='Completed'")
        scripts_today = await conn.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE status='Completed' AND created_at >= $1", today
        )
        scripts_week  = await conn.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE status='Completed' AND created_at >= $1", week_ago
        )
        tasks_today   = await conn.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE created_at >= $1", today
        )
        errors_today  = await conn.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE status LIKE 'Error%' AND created_at >= $1", today
        )
        total_credits = await conn.fetchval("SELECT COALESCE(SUM(credits), 0) FROM settings")
        model_rows    = await conn.fetch(
            "SELECT model, COUNT(*) as cnt FROM tasks WHERE status='Completed' "
            "GROUP BY model ORDER BY cnt DESC LIMIT 5"
        )

    model_lines = "\n".join(
        f"  {i+1}. {r['model']}: {r['cnt']} сц." for i, r in enumerate(model_rows)
    ) or "  нет данных"

    await message.answer(
        f"📊 <b>Статистика ScriptAI</b>\n\n"
        f"👥 Пользователей: <b>{total_users}</b>\n\n"
        f"🎬 <b>Сценарии:</b>\n"
        f"  Всего: <b>{total_scripts}</b>\n"
        f"  Сегодня: <b>{scripts_today}</b> (задач: {tasks_today})\n"
        f"  За 7 дней: <b>{scripts_week}</b>\n"
        f"  Ошибок сегодня: <b>{errors_today}</b>\n\n"
        f"🤖 <b>Популярные модели:</b>\n{model_lines}\n\n"
        f"💰 Кредитов на балансах: <b>{float(total_credits):.1f}₽</b>",
        parse_mode="HTML",
    )

# ---------------------------------------------------------------------------
# ВЫРУЧКА И ЧИСТАЯ ПРИБЫЛЬ
# ---------------------------------------------------------------------------
@dp.message(Command("revenue"))
async def revenue_cmd(message: types.Message):
    if not await admin_only(message):
        return

    today     = date.today().isoformat()
    week_ago  = (date.today() - timedelta(days=7)).isoformat()
    month_ago = (date.today() - timedelta(days=30)).isoformat()

    async with db_pool.acquire() as conn:
        rev_total = await conn.fetchval(
            "SELECT COALESCE(SUM(ABS(amount)), 0) FROM transactions "
            "WHERE amount < 0 AND description LIKE '🎬%'"
        )
        rev_today = await conn.fetchval(
            "SELECT COALESCE(SUM(ABS(amount)), 0) FROM transactions "
            "WHERE amount < 0 AND description LIKE '🎬%' AND created_at >= $1", today
        )
        rev_week  = await conn.fetchval(
            "SELECT COALESCE(SUM(ABS(amount)), 0) FROM transactions "
            "WHERE amount < 0 AND description LIKE '🎬%' AND created_at >= $1", week_ago
        )
        rev_month = await conn.fetchval(
            "SELECT COALESCE(SUM(ABS(amount)), 0) FROM transactions "
            "WHERE amount < 0 AND description LIKE '🎬%' AND created_at >= $1", month_ago
        )
        topup_total = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions "
            "WHERE amount > 0 AND description LIKE '💳%'"
        )
        topup_month = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions "
            "WHERE amount > 0 AND description LIKE '💳%' AND created_at >= $1", month_ago
        )
        model_stats = await conn.fetch(
            "SELECT model, COUNT(*) as cnt FROM tasks WHERE status='Completed' GROUP BY model"
        )

    # Оценочная себестоимость (среднее 60 мин/сценарий)
    api_cost_rub = sum(
        estimate_api_cost_rub(r["model"], 60.0) * r["cnt"]
        for r in model_stats
    )

    rev_total   = float(rev_total)
    rev_today   = float(rev_today)
    rev_week    = float(rev_week)
    rev_month   = float(rev_month)
    topup_total = float(topup_total)
    topup_month = float(topup_month)

    net_profit = rev_total - api_cost_rub
    margin     = (net_profit / rev_total * 100) if rev_total > 0 else 0

    await message.answer(
        f"💵 <b>Выручка ScriptAI</b>\n\n"
        f"📈 <b>Списано за генерации:</b>\n"
        f"  Сегодня: <b>{rev_today:.1f}₽</b> ({rub_to_usd(rev_today)})\n"
        f"  7 дней: <b>{rev_week:.1f}₽</b> ({rub_to_usd(rev_week)})\n"
        f"  30 дней: <b>{rev_month:.1f}₽</b> ({rub_to_usd(rev_month)})\n"
        f"  Всего: <b>{rev_total:.1f}₽</b> ({rub_to_usd(rev_total)})\n\n"
        f"💳 <b>Пополнения (ручные):</b>\n"
        f"  30 дней: <b>{topup_month:.1f}₽</b> ({rub_to_usd(topup_month)})\n"
        f"  Всего: <b>{topup_total:.1f}₽</b> ({rub_to_usd(topup_total)})\n\n"
        f"🔧 <b>Оценочная себестоимость API:</b>\n"
        f"  ~<b>{api_cost_rub:.1f}₽</b> ({rub_to_usd(api_cost_rub)})\n"
        f"  <i>(при средней длине 60 мин/сценарий)</i>\n\n"
        f"💰 <b>Чистая прибыль (оценка):</b>\n"
        f"  ~<b>{net_profit:.1f}₽</b> ({rub_to_usd(net_profit)})\n"
        f"  Маржа: ~<b>{margin:.0f}%</b>",
        parse_mode="HTML",
    )

# ---------------------------------------------------------------------------
# ТОП ПОЛЬЗОВАТЕЛЕЙ
# ---------------------------------------------------------------------------
@dp.message(Command("top"))
async def top_cmd(message: types.Message):
    if not await admin_only(message):
        return

    async with db_pool.acquire() as conn:
        top_spend = await conn.fetch(
            "SELECT t.user_id, s.username, COALESCE(SUM(ABS(t.amount)), 0) as total "
            "FROM transactions t LEFT JOIN settings s ON t.user_id=s.user_id "
            "WHERE t.amount < 0 AND t.description LIKE '🎬%' "
            "GROUP BY t.user_id, s.username ORDER BY total DESC LIMIT 5"
        )
        top_scripts = await conn.fetch(
            "SELECT t.user_id, s.username, COUNT(*) as cnt "
            "FROM tasks t LEFT JOIN settings s ON t.user_id=s.user_id "
            "WHERE t.status='Completed' "
            "GROUP BY t.user_id, s.username ORDER BY cnt DESC LIMIT 5"
        )
        top_balance = await conn.fetch(
            "SELECT user_id, username, credits FROM settings ORDER BY credits DESC LIMIT 5"
        )

    def fmt(row):
        un = f"@{row['username']}" if row.get("username") else f"<code>{row['user_id']}</code>"
        return un

    spend_lines = "\n".join(
        f"  {i+1}. {fmt(r)}: {float(r['total']):.1f}₽" for i, r in enumerate(top_spend)
    ) or "  нет данных"

    scripts_lines = "\n".join(
        f"  {i+1}. {fmt(r)}: {r['cnt']} сц." for i, r in enumerate(top_scripts)
    ) or "  нет данных"

    balance_lines = "\n".join(
        f"  {i+1}. {fmt(r)}: {float(r['credits']):.1f}₽" for i, r in enumerate(top_balance)
    ) or "  нет данных"

    await message.answer(
        f"🏆 <b>Топ пользователей</b>\n\n"
        f"💸 <b>По расходам:</b>\n{spend_lines}\n\n"
        f"🎬 <b>По сценариям:</b>\n{scripts_lines}\n\n"
        f"💰 <b>По балансу:</b>\n{balance_lines}",
        parse_mode="HTML",
    )

# ---------------------------------------------------------------------------
# ИСТОРИЯ (через состояние)
# ---------------------------------------------------------------------------
@dp.message(Command("history"))
async def history_cmd_entry(message: types.Message, state: FSMContext):
    if not await admin_only(message):
        return
    await message.answer("📋 Введи ID или @username [кол-во]:")
    await state.set_state(HistoryState.waiting_user_id)


@dp.message(HistoryState.waiting_user_id)
async def history_user(message: types.Message, state: FSMContext):
    parts = message.text.split(maxsplit=1)
    found = await resolve_user(parts[0])
    if not found:
        await message.answer("❌ Не найден.")
        await state.clear()
        return

    limit   = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 15
    user_id = found["user_id"]
    uname   = f"@{found['username']}" if found.get("username") else str(user_id)

    async with db_pool.acquire() as conn:
        txs = await conn.fetch(
            "SELECT amount, description, created_at FROM transactions "
            "WHERE user_id=$1 ORDER BY id DESC LIMIT $2",
            user_id, limit,
        )

    if not txs:
        await message.answer(f"Транзакций для {uname} нет.")
    else:
        text = f"📋 <b>История {uname} (последние {limit}):</b>\n\n"
        for tx in txs:
            sign = "+" if tx["amount"] > 0 else ""
            text += f"{sign}{float(tx['amount']):.1f} — {tx['description']} <i>({tx['created_at']})</i>\n"
        await message.answer(text, parse_mode="HTML")

    await state.clear()
    await message.answer("Меню:", reply_markup=get_admin_kb())


# ---------------------------------------------------------------------------
# БАН / РАЗБАН (через состояние)
# ---------------------------------------------------------------------------
@dp.message(Command("ban"))
async def ban_cmd_entry(message: types.Message, state: FSMContext):
    if not await admin_only(message):
        return
    await message.answer("🔴 Введи ID или @username для бана:")
    await state.set_state(BanState.waiting_user_id)


@dp.message(BanState.waiting_user_id)
async def ban_user(message: types.Message, state: FSMContext):
    found = await resolve_user(message.text)
    if not found:
        await message.answer("❌ Не найден.")
        await state.clear()
        return

    user_id = found["user_id"]
    async with db_pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE settings ADD COLUMN IF NOT EXISTS banned BOOLEAN DEFAULT FALSE"
        )
        await conn.execute("UPDATE settings SET banned=TRUE WHERE user_id=$1", user_id)

    uname = f"@{found['username']}" if found.get("username") else str(user_id)
    await message.answer(f"🔴 <b>{uname}</b> заблокирован.", parse_mode="HTML")
    try:
        await main_bot.send_message(user_id, "⛔ Ваш аккаунт заблокирован. Свяжитесь с поддержкой.")
    except Exception:
        pass
    await state.clear()
    await message.answer("Меню:", reply_markup=get_admin_kb())


@dp.message(Command("unban"))
async def unban_cmd_entry(message: types.Message, state: FSMContext):
    if not await admin_only(message):
        return
    await message.answer("🟢 Введи ID или @username для разбана:")
    await state.set_state(UnbanState.waiting_user_id)


@dp.message(UnbanState.waiting_user_id)
async def unban_user(message: types.Message, state: FSMContext):
    found = await resolve_user(message.text)
    if not found:
        await message.answer("❌ Не найден.")
        await state.clear()
        return

    user_id = found["user_id"]
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE settings SET banned=FALSE WHERE user_id=$1", user_id)

    uname = f"@{found['username']}" if found.get("username") else str(user_id)
    await message.answer(f"🟢 <b>{uname}</b> разблокирован.", parse_mode="HTML")
    try:
        await main_bot.send_message(user_id, "✅ Аккаунт разблокирован. Добро пожаловать!")
    except Exception:
        pass
    await state.clear()
    await message.answer("Меню:", reply_markup=get_admin_kb())


# ---------------------------------------------------------------------------
# ПОИСК (через состояние)
# ---------------------------------------------------------------------------
@dp.message(Command("search"))
async def search_cmd_entry(message: types.Message, state: FSMContext):
    if not await admin_only(message):
        return
    await message.answer("🔎 Введи ID или @username:")
    await state.set_state(SearchState.waiting_user_id)


@dp.message(SearchState.waiting_user_id)
async def search_user(message: types.Message, state: FSMContext):
    found = await resolve_user(message.text)
    if not found:
        await message.answer("❌ Не найден.")
        await state.clear()
        return

    user_id = found["user_id"]
    async with db_pool.acquire() as conn:
        scripts = await conn.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE user_id=$1 AND status='Completed'", user_id
        )
        spent = await conn.fetchval(
            "SELECT COALESCE(SUM(ABS(amount)), 0) FROM transactions "
            "WHERE user_id=$1 AND amount < 0 AND description LIKE '🎬%'", user_id
        )

    uname  = f"@{found['username']}" if found.get("username") else "—"
    banned = "🔴 Заблокирован" if found.get("banned") else "🟢 Активен"

    await message.answer(
        f"👤 <b>Пользователь</b>\n\n"
        f"ID: <code>{user_id}</code>\n"
        f"Username: {uname}\n"
        f"Статус: {banned}\n"
        f"Модель: {found.get('model_name') or '—'}\n"
        f"Баланс: <b>{float(found['credits'] or 0):.1f}₽</b>\n"
        f"Сценариев: <b>{scripts}</b>\n"
        f"Потрачено: <b>{float(spent):.1f}₽</b>\n"
        f"Реф. код: <code>{found.get('referral_code') or '—'}</code>",
        parse_mode="HTML",
    )
    await state.clear()
    await message.answer("Меню:", reply_markup=get_admin_kb())


# ---------------------------------------------------------------------------
# РАССЫЛКА (через состояние)
# ---------------------------------------------------------------------------
@dp.message(Command("broadcast"))
async def broadcast_cmd_entry(message: types.Message, state: FSMContext):
    if not await admin_only(message):
        return
    await message.answer("📢 Введи текст рассылки:")
    await state.set_state(BroadcastState.waiting_text)


@dp.message(BroadcastState.waiting_text)
async def broadcast_text(message: types.Message, state: FSMContext):
    text = message.text
    async with db_pool.acquire() as conn:
        users = await conn.fetch(
            "SELECT user_id FROM settings WHERE NOT COALESCE(banned, FALSE)"
        )

    sent = failed = 0
    for row in users:
        try:
            await main_bot.send_message(row["user_id"], text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await message.answer(
        f"📢 <b>Рассылка завершена</b>\n✅ {sent} | ❌ {failed}",
        parse_mode="HTML",
    )
    await state.clear()
    await message.answer("Меню:", reply_markup=get_admin_kb())

# ---------------------------------------------------------------------------
# НАЧИСЛИТЬ ВСЕМ
# ---------------------------------------------------------------------------
@dp.message(Command("give_all"))
async def give_all_start(message: types.Message, state: FSMContext):
    if not await admin_only(message):
        return
    await message.answer("🎁 Сколько кредитов начислить всем?")
    await state.set_state(GiveAllState.waiting_amount)


@dp.message(GiveAllState.waiting_amount)
async def give_all_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введи положительное число")
        return
    await state.update_data(amount=amount)
    await message.answer("Комментарий (или /skip):")
    await state.set_state(GiveAllState.waiting_comment)


@dp.message(GiveAllState.waiting_comment)
async def give_all_comment(message: types.Message, state: FSMContext):
    data    = await state.get_data()
    amount  = data["amount"]
    comment = message.text if message.text != "/skip" else "🎁 Бонус от администратора"
    now     = _now()

    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM settings WHERE NOT COALESCE(banned, FALSE)")
        await conn.execute(
            "UPDATE settings SET credits = credits + $1 WHERE NOT COALESCE(banned, FALSE)",
            amount,
        )
        for row in users:
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, description, created_at) "
                "VALUES ($1,$2,$3,$4)",
                row["user_id"], amount, comment, now,
            )

    await message.answer(
        f"✅ <b>Начислено {amount:.1f}₽</b> всем {len(users)} пользователям.\n📝 {comment}",
        parse_mode="HTML",
    )

    sent = 0
    for row in users:
        try:
            await main_bot.send_message(
                row["user_id"],
                f"🎁 Вам начислено <b>{amount:.1f} кредитов</b>!\n📝 {comment}",
                parse_mode="HTML",
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await message.answer(f"📢 Уведомлено: {sent}/{len(users)}")
    await state.clear()

# ---------------------------------------------------------------------------
# ПОПОЛНЕНИЕ
# ---------------------------------------------------------------------------
@dp.message(Command("topup"))
async def topup_start(message: types.Message, state: FSMContext):
    if not await admin_only(message):
        return
    await message.answer("💳 Введи ID или @username:")
    await state.set_state(TopupState.waiting_user_id)


@dp.message(TopupState.waiting_user_id)
async def topup_user_id(message: types.Message, state: FSMContext):
    found = await resolve_user(message.text)
    if not found:
        await message.answer("❌ Не найден.")
        return
    uname = f"@{found['username']}" if found.get("username") else str(found["user_id"])
    await state.update_data(target_user_id=found["user_id"], uname_display=uname)
    await message.answer(
        f"✅ <b>{uname}</b> | Баланс: {float(found['credits'] or 0):.1f}₽\n\nСколько начислить?",
        parse_mode="HTML",
    )
    await state.set_state(TopupState.waiting_amount)


@dp.message(TopupState.waiting_amount)
async def topup_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введи положительное число")
        return
    await state.update_data(amount=amount)
    await message.answer("Комментарий (или /skip):")
    await state.set_state(TopupState.waiting_comment)


@dp.message(TopupState.waiting_comment)
async def topup_comment(message: types.Message, state: FSMContext):
    data    = await state.get_data()
    user_id = data["target_user_id"]
    amount  = data["amount"]
    uname   = data.get("uname_display", str(user_id))
    comment = message.text if message.text != "/skip" else "💳 Ручное пополнение администратором"

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT credits FROM settings WHERE user_id=$1", user_id)
        if not row:
            await message.answer(f"❌ Пользователь {user_id} не найден.")
            await state.clear()
            return
        await conn.execute(
            "UPDATE settings SET credits = credits + $1 WHERE user_id=$2", amount, user_id,
        )
        await conn.execute(
            "INSERT INTO transactions (user_id, amount, description, created_at) VALUES ($1,$2,$3,$4)",
            user_id, amount, comment, _now(),
        )
        new_bal = await conn.fetchval("SELECT credits FROM settings WHERE user_id=$1", user_id)

    await message.answer(
        f"✅ <b>Начислено</b>\n"
        f"👤 {uname} (<code>{user_id}</code>)\n"
        f"➕ +{amount:.1f}₽ | Баланс: {float(new_bal):.1f}₽\n"
        f"📝 {comment}",
        parse_mode="HTML",
    )
    try:
        await main_bot.send_message(
            user_id,
            f"💰 Начислено <b>{amount:.1f} кредитов</b>\n📝 {comment}\n"
            f"Баланс: <b>{float(new_bal):.1f}₽</b>",
            parse_mode="HTML",
        )
    except Exception:
        await message.answer("⚠️ Не удалось уведомить пользователя.")
    await state.clear()

# ---------------------------------------------------------------------------
# СПИСАНИЕ
# ---------------------------------------------------------------------------
@dp.message(Command("deduct"))
async def deduct_start(message: types.Message, state: FSMContext):
    if not await admin_only(message):
        return
    await message.answer("➖ Введи ID или @username:")
    await state.set_state(DeductState.waiting_user_id)


@dp.message(DeductState.waiting_user_id)
async def deduct_user_id(message: types.Message, state: FSMContext):
    found = await resolve_user(message.text)
    if not found:
        await message.answer("❌ Не найден.")
        return
    uname = f"@{found['username']}" if found.get("username") else str(found["user_id"])
    await state.update_data(target_user_id=found["user_id"], uname_display=uname)
    await message.answer(
        f"✅ <b>{uname}</b> | Баланс: {float(found['credits'] or 0):.1f}₽\n\nСколько списать?",
        parse_mode="HTML",
    )
    await state.set_state(DeductState.waiting_amount)


@dp.message(DeductState.waiting_amount)
async def deduct_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введи положительное число")
        return
    await state.update_data(amount=amount)
    await message.answer("Комментарий (или /skip):")
    await state.set_state(DeductState.waiting_comment)


@dp.message(DeductState.waiting_comment)
async def deduct_comment(message: types.Message, state: FSMContext):
    data    = await state.get_data()
    user_id = data["target_user_id"]
    amount  = data["amount"]
    uname   = data.get("uname_display", str(user_id))
    comment = message.text if message.text != "/skip" else "➖ Ручное списание администратором"

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE settings SET credits = GREATEST(credits - $1, 0) WHERE user_id=$2",
            amount, user_id,
        )
        await conn.execute(
            "INSERT INTO transactions (user_id, amount, description, created_at) VALUES ($1,$2,$3,$4)",
            user_id, -amount, comment, _now(),
        )
        new_bal = await conn.fetchval("SELECT credits FROM settings WHERE user_id=$1", user_id)

    await message.answer(
        f"✅ <b>Списано</b>\n"
        f"👤 {uname} (<code>{user_id}</code>)\n"
        f"➖ -{amount:.1f}₽ | Баланс: {float(new_bal):.1f}₽\n"
        f"📝 {comment}",
        parse_mode="HTML",
    )
    await state.clear()

# ---------------------------------------------------------------------------
# ПРОВЕРКА БАЛАНСА
# ---------------------------------------------------------------------------
@dp.message(Command("check"))
async def check_start(message: types.Message, state: FSMContext):
    if not await admin_only(message):
        return
    await message.answer("🔍 Введи ID или @username:")
    await state.set_state(CheckState.waiting_user_id)


@dp.message(CheckState.waiting_user_id)
async def check_user(message: types.Message, state: FSMContext):
    found = await resolve_user(message.text)
    if not found:
        await message.answer("❌ Не найден.")
        await state.clear()
        return

    user_id = found["user_id"]
    async with db_pool.acquire() as conn:
        txs = await conn.fetch(
            "SELECT amount, description, created_at FROM transactions "
            "WHERE user_id=$1 ORDER BY id DESC LIMIT 7",
            user_id,
        )
        scripts = await conn.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE user_id=$1 AND status='Completed'", user_id
        )

    uname   = f"@{found['username']}" if found.get("username") else "—"
    credits = float(found["credits"] or 0)
    banned  = "🔴" if found.get("banned") else "🟢"

    text = (
        f"{banned} <b>{uname}</b> (<code>{user_id}</code>)\n"
        f"💰 Баланс: <b>{credits:.1f}₽</b>\n"
        f"🎬 Сценариев: <b>{scripts}</b>\n"
        f"🤖 Модель: {found.get('model_name') or '—'}\n"
        f"🔗 Реф: <code>{found.get('referral_code') or '—'}</code>\n\n"
        f"📋 <b>Последние операции:</b>\n"
    )
    for tx in txs:
        sign = "+" if tx["amount"] > 0 else ""
        text += f"  {sign}{float(tx['amount']):.1f} — {tx['description']} <i>({tx['created_at']})</i>\n"
    if not txs:
        text += "  <i>Нет операций</i>"

    await message.answer(text, parse_mode="HTML")
    await state.clear()

# ---------------------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------------------
async def main():
    print("Админ-бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
