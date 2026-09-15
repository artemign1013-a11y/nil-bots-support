import asyncio
import os
import json
import datetime
import firebase_admin
from firebase_admin import credentials, firestore
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN, ADMIN_ID

# ============================================
# ПРОВЕРКИ
# ============================================
if not BOT_TOKEN:
    raise SystemExit("❌ Переменная BOT_TOKEN не задана!")
if not ADMIN_ID:
    raise SystemExit("❌ Переменная ADMIN_ID не задана!")

# ============================================
# FIREBASE
# ============================================
firebase_key_json = os.environ.get("FIREBASE_KEY_JSON")
if firebase_key_json:
    cred = credentials.Certificate(json.loads(firebase_key_json))
else:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    cred = credentials.Certificate(os.path.join(current_dir, "firebase-key.json"))

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)
db = firestore.client()

# ============================================
# БОТ
# ============================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


class TicketState(StatesGroup):
    waiting_message = State()


# ============================================
# КЛАВИАТУРЫ И ТЕКСТЫ
# ============================================
def get_status_text(status: str) -> str:
    return {
        "new": "🟡 Новый",
        "working": "🔵 В работе",
        "resolved": "🟢 Решён",
        "closed": "⚫ Закрыт"
    }.get(status, "❓")


def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать в поддержку", callback_data="new_ticket")],
        [InlineKeyboardButton(text="📋 Мои тикеты", callback_data="my_tickets")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")]
    ])


def ticket_kb(ticket_id: str, status: str):
    buttons = []
    if status in ("new", "working"):
        buttons.append([InlineKeyboardButton(text="✍️ Написать ещё", callback_data=f"reply_{ticket_id}")])
        buttons.append([InlineKeyboardButton(text="🔒 Закрыть тикет", callback_data=f"close_{ticket_id}")])
    buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ============================================
# РАБОТА С FIREBASE
# ============================================
def find_active_ticket(user_id: int):
    try:
        docs = db.collection("tickets")\
            .where("user_id", "==", user_id)\
            .where("status", "in", ["new", "working"])\
            .limit(1)\
            .stream()
        for t in docs:
            return {"id": t.id, **t.to_dict()}
    except Exception as e:
        print(f"Ошибка поиска тикета: {e}")
    return None


def create_ticket(user_id, username, full_name, message_text) -> str:
    ref = db.collection("tickets").document()
    now = datetime.datetime.now().isoformat()
    ref.set({
        "user_id": user_id,
        "username": username or "Без username",
        "full_name": full_name,
        "message": message_text,
        "status": "new",
        "created_at": now,
        "updated_at": now,
        "messages": [{"from": "client", "text": message_text, "time": now}]
    })
    return ref.id


def add_message_to_ticket(ticket_id: str, from_who: str, text: str):
    ref = db.collection("tickets").document(ticket_id)
    doc = ref.get()
    if not doc.exists:
        return
    ticket = doc.to_dict()
    messages = ticket.get("messages", [])
    messages.append({"from": from_who, "text": text, "time": datetime.datetime.now().isoformat()})
    ref.update({"messages": messages, "updated_at": datetime.datetime.now().isoformat()})


def get_user_tickets(user_id: int, limit: int = 10):
    try:
        docs = db.collection("tickets")\
            .where("user_id", "==", user_id)\
            .stream()
        tickets = [{"id": t.id, **t.to_dict()} for t in docs]
        tickets.sort(key=lambda t: t.get("created_at", ""), reverse=True)
        return tickets[:limit]
    except Exception as e:
        print(f"Ошибка получения тикетов: {e}")
        return []


def get_ticket_status(ticket_id: str) -> str:
    try:
        doc = db.collection("tickets").document(ticket_id).get()
        if doc.exists:
            return doc.to_dict().get("status", "new")
    except Exception:
        pass
    return "new"


# ============================================
# ХЕНДЛЕРЫ
# ============================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    active = find_active_ticket(message.from_user.id)
    text = (
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
        "Я — бот технической поддержки <b>Nil Bots</b>.\n\n"
        "• ✍️ Напиши вопрос или сообщи о проблеме\n"
        "• 📋 Смотри историю своих обращений\n"
        "• 💬 Получай ответы от разработчика прямо здесь\n\n"
    )
    if active:
        text += f"🔔 Активный тикет: <b>#{active['id'][:6].upper()}</b> ({get_status_text(active['status'])})\n"
    await message.answer(text, reply_markup=main_kb(), parse_mode="HTML")


@router.callback_query(F.data == "new_ticket")
async def new_ticket(call: CallbackQuery, state: FSMContext):
    await call.answer()
    active = find_active_ticket(call.from_user.id)
    if active:
        await call.message.edit_text(
            f"⚠️ У тебя уже есть активный тикет <b>#{active['id'][:6].upper()}</b>.\nНапиши в него или закрой его.",
            reply_markup=ticket_kb(active['id'], active['status']),
            parse_mode="HTML"
        )
        return
    await call.message.edit_text(
        "✍️ <b>Опиши свою проблему или задай вопрос:</b>\n\n<i>(отмена — /cancel)</i>",
        parse_mode="HTML"
    )
    await state.set_state(TicketState.waiting_message)


@router.callback_query(F.data.startswith("reply_"))
async def reply_to_ticket(call: CallbackQuery, state: FSMContext):
    await call.answer()
    ticket_id = call.data.replace("reply_", "")
    await state.update_data(ticket_id=ticket_id)
    await call.message.edit_text(
        f"✍️ <b>Напиши сообщение в тикет #{ticket_id[:6].upper()}:</b>\n\n<i>(отмена — /cancel)</i>",
        parse_mode="HTML"
    )
    await state.set_state(TicketState.waiting_message)


@router.message(TicketState.waiting_message, F.text)
async def receive_message(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=main_kb())
        return

    data = await state.get_data()
    target_ticket = data.get("ticket_id")
    await state.clear()

    is_new = False
    if target_ticket:
        add_message_to_ticket(target_ticket, "client", message.text)
        ticket_id = target_ticket
    else:
        active = find_active_ticket(message.from_user.id)
        if active:
            add_message_to_ticket(active["id"], "client", message.text)
            ticket_id = active["id"]
        else:
            ticket_id = create_ticket(
                message.from_user.id,
                message.from_user.username or "",
                message.from_user.full_name,
                message.text
            )
            is_new = True

    status = get_ticket_status(ticket_id)

    if is_new:
        await message.answer(
            f"✅ <b>Тикет создан!</b>\n\n🆔 Номер: <code>{ticket_id[:6].upper()}</code>\n\n"
            "Разработчик ответит тебе прямо в этот чат.",
            reply_markup=ticket_kb(ticket_id, status),
            parse_mode="HTML"
        )
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🎫 <b>Новый тикет #{ticket_id[:6].upper()}</b>\n\n"
                f"👤 {message.from_user.full_name} (@{message.from_user.username or '—'})\n"
                f"🆔 <code>{message.from_user.id}</code>\n\n💬 {message.text}\n\n"
                f"🔗 Ответить: https://nil-bots-site-with-bot.vercel.app",
                parse_mode="HTML"
            )
        except Exception as e:
            print(f"Не удалось уведомить админа: {e}")
    else:
        await message.answer(
            f"✅ Сообщение добавлено в тикет <b>#{ticket_id[:6].upper()}</b>",
            reply_markup=ticket_kb(ticket_id, status),
            parse_mode="HTML"
        )
        try:
            await bot.send_message(
                ADMIN_ID,
                f"💬 <b>Сообщение в тикете #{ticket_id[:6].upper()}</b>\n\n"
                f"👤 {message.from_user.full_name}\n\n{message.text}",
                parse_mode="HTML"
            )
        except Exception:
            pass


@router.message(Command("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отменено.", reply_markup=main_kb())


@router.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    active = find_active_ticket(message.from_user.id)
    if active:
        add_message_to_ticket(active['id'], "client", message.text)
        await message.answer(
            f"✅ Добавлено в тикет <b>#{active['id'][:6].upper()}</b>",
            reply_markup=ticket_kb(active['id'], active['status']),
            parse_mode="HTML"
        )
        try:
            await bot.send_message(
                ADMIN_ID,
                f"💬 <b>Сообщение в тикете #{active['id'][:6].upper()}</b>\n\n"
                f"👤 {message.from_user.full_name}\n\n{message.text}",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return
    await message.answer(
        "🤔 У тебя нет активного тикета. Создать новый?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Создать тикет", callback_data="new_ticket")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_main")]
        ])
    )


@router.callback_query(F.data == "my_tickets")
async def my_tickets(call: CallbackQuery):
    await call.answer()
    tickets = get_user_tickets(call.from_user.id)
    if not tickets:
        await call.message.edit_text("📭 <b>Тикетов пока нет.</b>", reply_markup=main_kb(), parse_mode="HTML")
        return
    text = "📋 <b>Твои тикеты:</b>\n\n"
    buttons = []
    for t in tickets:
        text += f"• <b>#{t['id'][:6].upper()}</b> — {get_status_text(t.get('status'))} ({t.get('created_at', '')[:10]})\n"
        buttons.append([InlineKeyboardButton(text=f"📄 #{t['id'][:6].upper()}", callback_data=f"view_{t['id']}")])
    buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_main")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


@router.callback_query(F.data.startswith("view_"))
async def view_ticket(call: CallbackQuery):
    await call.answer()
    ticket_id = call.data.replace("view_", "")
    doc = db.collection("tickets").document(ticket_id).get()
    if not doc.exists or doc.to_dict().get("user_id") != call.from_user.id:
        await call.message.edit_text("❌ Тикет не найден.", reply_markup=main_kb())
        return
    ticket = doc.to_dict()
    text = f"🎫 <b>Тикет #{ticket_id[:6].upper()}</b>\n📊 {get_status_text(ticket.get('status'))}\n\n"
    for msg in ticket.get("messages", [])[-10:]:
        who = "👤 Ты" if msg["from"] == "client" else "🛠️ Поддержка"
        text += f"<b>{who}:</b> {msg['text'][:300]}\n\n"
    await call.message.edit_text(text, reply_markup=ticket_kb(ticket_id, ticket.get("status", "new")), parse_mode="HTML")


@router.callback_query(F.data.startswith("close_"))
async def close_ticket_handler(call: CallbackQuery):
    await call.answer()
    ticket_id = call.data.replace("close_", "")
    db.collection("tickets").document(ticket_id).update({
        "status": "closed",
        "updated_at": datetime.datetime.now().isoformat()
    })
    last_status[ticket_id] = "closed"  # чтобы listener не слал дубль
    await call.message.edit_text(
        f"🔒 <b>Тикет #{ticket_id[:6].upper()} закрыт.</b> Спасибо!",
        reply_markup=main_kb(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "help")
async def help_handler(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        "❓ <b>Как пользоваться:</b>\n\n"
        "1️⃣ Нажми «Написать в поддержку»\n2️⃣ Опиши проблему\n3️⃣ Жди ответ в этом чате\n\n"
        "⏱ Среднее время ответа: 1-2 часа",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_main")]
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "back_main")
async def back_main(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await call.message.edit_text("🏠 <b>Главное меню</b>", reply_markup=main_kb(), parse_mode="HTML")


# ============================================
# LISTENER: on_snapshot (ЭКОНОМИТ ЛИМИТЫ FIREBASE)
# ============================================
last_counts: dict = {}
last_status: dict = {}
MAIN_LOOP = None


def _firestore_listener(snapshot, changes, read_time):
    """Firebase сам пушит изменения — мы НЕ опрашиваем базу каждые 3 секунды"""
    for change in changes:
        doc_id = change.document.id
        data = change.document.to_dict()
        if MAIN_LOOP:
            MAIN_LOOP.call_soon_threadsafe(
                MAIN_LOOP.create_task, process_change(doc_id, data)
            )


async def process_change(doc_id: str, ticket: dict):
    user_id = ticket.get("user_id")
    messages = ticket.get("messages", [])
    status = ticket.get("status", "new")

    # первая встреча — просто запоминаем, без спама
    if doc_id not in last_counts:
        last_counts[doc_id] = len(messages)
        last_status[doc_id] = status
        return

    # новые сообщения от админа (ответил с сайта) → шлём клиенту
    if len(messages) > last_counts[doc_id]:
        for msg in messages[last_counts[doc_id]:]:
            if msg.get("from") == "admin" and user_id:
                try:
                    await bot.send_message(
                        user_id,
                        f"🛠️ <b>Ответ поддержки</b> (тикет #{doc_id[:6].upper()}):\n\n"
                        f"{msg['text']}\n\n"
                        f"💬 <i>Чтобы мы увидели твоё сообщение — ответь прямо на это сообщение!</i>",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    print(f"Ошибка отправки ответа: {e}")
        last_counts[doc_id] = len(messages)

    # смена статуса → уведомление клиенту
    if status != last_status.get(doc_id):
        last_status[doc_id] = status
        if user_id:
            extra = ""
            if status == "closed":
                extra = "\n\nСпасибо за обращение! Если вопрос снова появится — создай новый тикет."
            elif status == "resolved":
                extra = "\n\nЕсли всё хорошо — можешь закрыть тикет. Если нет — напиши в него ещё."
            try:
                await bot.send_message(
                    user_id,
                    f"📊 <b>Статус тикета #{doc_id[:6].upper()} изменился</b>\n\n"
                    f"Новый статус: {get_status_text(status)}{extra}",
                    parse_mode="HTML",
                )
            except Exception as e:
                print(f"Ошибка уведомления о статусе: {e}")


def start_firestore_listener():
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_event_loop()
    q = db.collection("tickets").where(
        "status", "in", ["new", "working", "resolved", "closed"]
    )
    q.on_snapshot(_firestore_listener)
    print("👂 Listener (on_snapshot) запущен — экономим лимиты Firebase")


# ============================================
# ЗАПУСК
# ============================================
async def main():
    print("🎫 Бот поддержки запущен!")
    print(f"👑 Admin ID: {ADMIN_ID}")
    start_firestore_listener()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
