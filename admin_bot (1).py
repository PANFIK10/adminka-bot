"""
Админ-бот для ручного управления кредитами пользователей.
Работает с той же БД что и основной бот.

Переменные окружения:
  ADMIN_BOT_TOKEN  — токен этого бота (отдельный от основного)
  DATABASE_URL     — та же строка подключения что у основного бота
  ADMIN_IDS        — список Telegram ID администраторов через запятую
                     например: 123456789,987654321
"""

import asyncio
import logging
import os
import asyncpg
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
DATABASE_URL    = os.getenv("DATABASE_URL")

# Список разрешённых admin user_id (через запятую в env)
_raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[int] = {int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()}

if not ADMIN_BOT_TOKEN:
    raise RuntimeError("Не задан ADMIN_BOT_TOKEN")
if not ADMIN_IDS:
    raise RuntimeError("Не задан ADMIN_IDS — укажи свой Telegram ID")

bot       = Bot(token=ADMIN_BOT_TOKEN)
main_bot  = Bot(token=os.getenv("MAIN_BOT_TOKEN", ADMIN_BOT_TOKEN))
dp        = Dispatcher(storage=MemoryStorage())
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


async def resolve_user(query: str) -> dict | None:
    """
    Ищет пользователя по ID или @username.
    Возвращает строку из БД или None если не найден.
    """
    query = query.strip().lstrip("@")
    async with db_pool.acquire() as conn:
        # Если это число — ищем по user_id
        if query.lstrip("-").isdigit():
            row = await conn.fetchrow(
                "SELECT * FROM settings WHERE user_id=$1", int(query)
            )
        else:
            # Иначе ищем по username (без @, без учёта регистра)
            row = await conn.fetchrow(
                "SELECT * FROM settings WHERE lower(username)=$1", query.lower()
            )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# СОСТОЯНИЯ
# ---------------------------------------------------------------------------
class TopupState(StatesGroup):
    waiting_user_id = State()
    waiting_amount  = State()
    waiting_comment = State()


class CheckState(StatesGroup):
    waiting_user_id = State()


# ---------------------------------------------------------------------------
# GUARD — только для админов
# ---------------------------------------------------------------------------
async def admin_only(message: types.Message) -> bool:
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return False
    return True


# ---------------------------------------------------------------------------
# КОМАНДЫ
# ---------------------------------------------------------------------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    if not await admin_only(message):
        return
    await message.answer(
        "👨‍💼 <b>Админ-панель кредитов</b>\n\n"
        "Доступные команды:\n\n"
        "/topup — пополнить баланс пользователю\n"
        "/deduct — списать кредиты у пользователя\n"
        "/check — проверить баланс пользователя\n"
        "/history — история транзакций пользователя\n"
        "/broadcast — разослать сообщение всем пользователям\n\n"
        "<i>Везде можно вводить ID или @username</i>",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# ПОПОЛНЕНИЕ
# ---------------------------------------------------------------------------
@dp.message(Command("topup"))
async def topup_start(message: types.Message, state: FSMContext):
    if not await admin_only(message):
        return
    await message.answer(
        "💳 <b>Пополнение баланса</b>\n\n"
        "Введи ID или @username пользователя:",
        parse_mode="HTML",
    )
    await state.set_state(TopupState.waiting_user_id)


@dp.message(TopupState.waiting_user_id)
async def topup_user_id(message: types.Message, state: FSMContext):
    found = await resolve_user(message.text)
    if not found:
        await message.answer("❌ Пользователь не найден. Попробуй другой ID или @username.")
        return
    uname_display = f"@{found['username']}" if found.get("username") else str(found["user_id"])
    await state.update_data(target_user_id=found["user_id"], uname_display=uname_display)
    await message.answer(
        f"✅ Найден: <b>{uname_display}</b> (ID: <code>{found['user_id']}</code>)\n"
        f"💰 Текущий баланс: {float(found['credits'] or 0):.1f} кред.\n\n"
        "Сколько кредитов начислить?",
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
    await message.answer("Комментарий к операции (или /skip):")
    await state.set_state(TopupState.waiting_comment)


@dp.message(TopupState.waiting_comment)
async def topup_comment(message: types.Message, state: FSMContext):
    data           = await state.get_data()
    user_id        = data["target_user_id"]
    amount         = data["amount"]
    uname_display  = data.get("uname_display", str(user_id))
    comment        = message.text if message.text != "/skip" else "💳 Ручное пополнение администратором"

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT credits FROM settings WHERE user_id=$1", user_id)
        if not row:
            await message.answer(f"❌ Пользователь {user_id} не найден в БД.")
            await state.clear()
            return

        await conn.execute(
            "UPDATE settings SET credits = credits + $1 WHERE user_id=$2",
            amount, user_id,
        )
        await conn.execute(
            "INSERT INTO transactions (user_id, amount, description, created_at) "
            "VALUES ($1,$2,$3,$4)",
            user_id, amount, comment, _now(),
        )
        new_bal = await conn.fetchval("SELECT credits FROM settings WHERE user_id=$1", user_id)

    await message.answer(
        f"✅ <b>Начислено</b>\n\n"
        f"👤 Пользователь: <b>{uname_display}</b> (<code>{user_id}</code>)\n"
        f"➕ Кредитов: <b>+{amount:.1f}</b>\n"
        f"💰 Новый баланс: <b>{float(new_bal):.1f}</b>\n"
        f"📝 Комментарий: {comment}",
        parse_mode="HTML",
    )

    # Уведомляем самого пользователя
    try:
        await main_bot.send_message(
            user_id,
            f"💰 На ваш баланс начислено <b>{amount:.1f} кредитов</b>\n"
            f"📝 {comment}\n"
            f"Текущий баланс: <b>{float(new_bal):.1f} кред.</b>",
            parse_mode="HTML",
        )
    except Exception:
        await message.answer("⚠️ Не удалось уведомить пользователя (бот заблокирован?)")

    await state.clear()


# ---------------------------------------------------------------------------
# СПИСАНИЕ
# ---------------------------------------------------------------------------
@dp.message(Command("deduct"))
async def deduct_start(message: types.Message, state: FSMContext):
    if not await admin_only(message):
        return
    await message.answer(
        "➖ <b>Списание кредитов</b>\n\nВведи ID или @username пользователя:",
        parse_mode="HTML",
    )
    # Используем те же состояния что и topup, но с флагом
    await state.update_data(is_deduct=True)
    await state.set_state(TopupState.waiting_user_id)


# ---------------------------------------------------------------------------
# ПРОВЕРКА БАЛАНСА
# ---------------------------------------------------------------------------
@dp.message(Command("check"))
async def check_start(message: types.Message, state: FSMContext):
    if not await admin_only(message):
        return
    await message.answer("🔍 Введи ID или @username пользователя:")
    await state.set_state(CheckState.waiting_user_id)


@dp.message(CheckState.waiting_user_id)
async def check_user(message: types.Message, state: FSMContext):
    found = await resolve_user(message.text)
    if not found:
        await message.answer("❌ Пользователь не найден.")
        await state.clear()
        return

    user_id = found["user_id"]
    async with db_pool.acquire() as conn:
        txs = await conn.fetch(
            "SELECT amount, description, created_at FROM transactions "
            "WHERE user_id=$1 ORDER BY id DESC LIMIT 5",
            user_id,
        )

    credits       = float(found["credits"] or 0)
    model         = found["model_name"] or "—"
    ref           = found["referral_code"] or "—"
    uname_display = f"@{found['username']}" if found.get("username") else str(user_id)

    text = (
        f"👤 <b>{uname_display}</b> (ID: <code>{user_id}</code>)\n\n"
        f"💰 Баланс: <b>{credits:.1f} кред.</b>\n"
        f"🤖 Модель: {model}\n"
        f"🔗 Реф. код: <code>{ref}</code>\n\n"
        f"📋 <b>Последние операции:</b>\n"
    )
    for tx in txs:
        sign = "+" if tx["amount"] > 0 else ""
        text += f"  {sign}{float(tx['amount']):.1f} — {tx['description']} ({tx['created_at']})\n"
    if not txs:
        text += "  <i>Операций нет</i>"

    await message.answer(text, parse_mode="HTML")
    await state.clear()


# ---------------------------------------------------------------------------
# ИСТОРИЯ ТРАНЗАКЦИЙ
# ---------------------------------------------------------------------------
@dp.message(Command("history"))
async def history_cmd(message: types.Message):
    if not await admin_only(message):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /history <id или @username> [количество]")
        return

    found = await resolve_user(parts[1])
    if not found:
        await message.answer("❌ Пользователь не найден.")
        return

    user_id = found["user_id"]
    limit   = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 15
    uname_display = f"@{found['username']}" if found.get("username") else str(user_id)

    async with db_pool.acquire() as conn:
        txs = await conn.fetch(
            "SELECT amount, description, created_at FROM transactions "
            "WHERE user_id=$1 ORDER BY id DESC LIMIT $2",
            user_id, limit,
        )

    if not txs:
        await message.answer(f"Транзакций для {uname_display} не найдено.")
        return

    text = f"📋 <b>История {uname_display} (последние {limit}):</b>\n\n"
    for tx in txs:
        sign = "+" if tx["amount"] > 0 else ""
        text += f"{sign}{float(tx['amount']):.1f} — {tx['description']} <i>({tx['created_at']})</i>\n"

    await message.answer(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# РАССЫЛКА
# ---------------------------------------------------------------------------
@dp.message(Command("broadcast"))
async def broadcast_cmd(message: types.Message):
    if not await admin_only(message):
        return
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.answer("Использование: /broadcast <текст сообщения>")
        return

    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM settings")

    sent = 0
    failed = 0
    for row in users:
        try:
            await bot.send_message(row["user_id"], text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)  # защита от flood
        except Exception:
            failed += 1

    await message.answer(
        f"📢 <b>Рассылка завершена</b>\n"
        f"✅ Доставлено: {sent}\n"
        f"❌ Ошибок: {failed}",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------------------
async def main():
    print("Админ-бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
