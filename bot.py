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

# ============================================
# ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ============================================
BOT_TOKEN = os.environ.get("SUPPORT_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

if not BOT_TOKEN:
    raise SystemExit("❌ ОШИБКА: Переменная SUPPORT_BOT_TOKEN или BOT_TOKEN не задана!")
if not ADMIN_ID:
    raise SystemExit("❌ ОШИБКА: Переменная ADMIN_ID не задана!")

# ============================================
# FIREBASE ИНИЦИАЛИЗАЦИЯ
# ============================================
firebase_key_json = os.environ.get("FIREBASE_KEY_JSON")
if firebase_key_json:
    cred_dict = json.loads(firebase_key_json)
    cred = credentials.Certificate(cred_dict)
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
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# Словарь для хранения активного тикета пользователя
# user_id -> ticket_id
active_tickets: dict[int, str] = {}


# ============================================
# СОСТОЯНИЯ
# ============================================
class TicketState(StatesGroup):
    waiting_message = State()


# ============================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================
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


def get_status_text(status: str) -> str:
    statuses = {
        "new": "🟡 Новый",
        "working": "🔵 В работе",
        "resolved": "🟢 Решён",
        "closed": "⚫ Закрыт"
    }
    return statuses.get(status, "❓")


def find_active_ticket(user_id: int) -> dict | None:
    """Находит активный (не закрытый) тикет пользователя"""
    try:
        tickets_ref = db.collection("tickets")\
            .where("user_id", "==", user_id)\
            .where("status", "in", ["new", "working"])\
            .limit(1)\
            .stream()
        for ticket in tickets_ref:
            return {"id": ticket.id, **ticket.to_dict()}
    except Exception as e:
        print(f"Ошибка поиска тикета: {e}")
    return None


def create_ticket(user_id: int, username: str, full_name: str, message: str) -> str:
    """Создаёт новый тикет в Firebase"""
    ticket_ref = db.collection("tickets").document()
    now = datetime.datetime.now().isoformat()
    ticket_data = {
        "user_id": user_id,
        "username": username or "Без username",
        "full_name": full_name,
        "message": message,
        "status": "new",
        "created_at": now,
        "updated_at": now,
        "messages": [{
            "from": "client",
            "text": message,
            "time": now
        }]
    }
    ticket_ref.set(ticket_data)
    return ticket_ref.id


def add_message_to_ticket(ticket_id: str, from_who: str, text: str):
    """Добавляет сообщение в тикет"""
    ticket_ref = db.collection("tickets").document(ticket_id)
    ticket = ticket_ref.get().to_dict()
    if not ticket:
        return
    
    messages = ticket.get("messages", [])
    messages.append({
        "from": from_who,
        "text": text,
        "time": datetime.datetime.now().isoformat()
    })
    
    ticket_ref.update({
        "messages": messages,
        "updated_at": datetime.datetime.now().isoformat()
    })


def close_ticket(ticket_id: str):
    """Закрывает тикет"""
    db.collection("tickets").document(ticket_id).update({
        "status": "closed",
        "updated_at": datetime.datetime.now().isoformat()
    })


def get_user_tickets(user_id: int, limit: int = 10) -> list:
    """Получает последние тикеты пользователя"""
    try:
        tickets_ref = db.collection("tickets")\
            .where("user_id", "==", user_id)\
            .order_by("created_at", direction=firestore.Query.DESCENDING)\
            .limit(limit)\
            .stream()
        return [{"id": t.id, **t.to_dict()} for t in tickets_ref]
    except Exception as e:
        print(f"Ошибка получения тикетов: {e}")
        return []


# ============================================
# /start
# ============================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    
    # Проверяем активный тикет
    active = find_active_ticket(message.from_user.id)
    
    text = (
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
        "Я — бот технической поддержки <b>Nil Bots</b>.\n\n"
        "Здесь ты можешь:\n"
        "• ✍️ Написать вопрос или сообщить о проблеме\n"
        "• 📋 Посмотреть историю своих обращений\n"
        "• 💬 Получить ответ от разработчика\n\n"
    )
    
    if active:
        text += f"🔔 <b>У тебя есть активный тикет:</b> #{active['id'][:6].upper()}\nСтатус: {get_status_text(active['status'])}\n\n"
    
    await message.answer(text, reply_markup=main_kb(), parse_mode="HTML")


# ============================================
# НОВЫЙ ТИКЕТ
# ============================================
@router.callback_query(F.data == "new_ticket")
async def new_ticket(call: CallbackQuery, state: FSMContext):
    await call.answer()
    
    # Проверяем активный тикет
    active = find_active_ticket(call.from_user.id)
    if active:
        await call.message.edit_text(
            f"⚠️ У тебя уже есть активный тикет <b>#{active['id'][:6].upper()}</b>.\n\n"
            "Ты можешь написать в него ещё одно сообщение или закрыть его.",
            reply_markup=ticket_kb(active['id'], active['status']),
            parse_mode="HTML"
        )
        return
    
    await call.message.edit_text(
        "✍️ <b>Опиши свою проблему или задай вопрос:</b>\n\n"
        "Напиши текстовое сообщение — я передам его разработчику.\n\n"
        "<i>(Чтобы отменить — нажми /cancel)</i>",
        parse_mode="HTML"
    )
    await state.set_state(TicketState.waiting_message)


@router.message(TicketState.waiting_message, F.text)
async def receive_message(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=main_kb())
        return
    
    # Создаём тикет
    ticket_id = create_ticket(
        user_id=message.from_user.id,
        username=message.from_user.username or "",
        full_name=message.from_user.full_name,
        message=message.text
    )
    
    active_tickets[message.from_user.id] = ticket_id
    await state.clear()
    
    # Уведомляем клиента
    await message.answer(
        f"✅ <b>Тикет создан!</b>\n\n"
        f"🆔 Номер: <code>{ticket_id[:6].upper()}</code>\n"
        f"📝 Статус: {get_status_text('new')}\n\n"
        "Я передал твой вопрос разработчику. Он ответит прямо здесь, в этом чате.",
        reply_markup=ticket_kb(ticket_id, "new"),
        parse_mode="HTML"
    )
    
    # Уведомляем админа
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🎫 <b>Новый тикет #{ticket_id[:6].upper()}</b>\n\n"
            f"👤 {message.from_user.full_name}\n"
            f"📱 @{message.from_user.username or 'без username'}\n"
            f"🆔 ID: <code>{message.from_user.id}</code>\n\n"
            f"💬 <b>Сообщение:</b>\n{message.text}\n\n"
            f"🔗 Ответить можно на сайте:\nhttps://nil-bots-site-with-bot.vercel.app",
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"Не удалось уведомить админа: {e}")


# ============================================
# ОТВЕТ В АКТИВНЫЙ ТИКЕТ
# ============================================
@router.callback_query(F.data.startswith("reply_"))
async def reply_to_ticket(call: CallbackQuery, state: FSMContext):
    await call.answer()
    ticket_id = call.data.replace("reply_", "")
    
    await call.message.edit_text(
        f"✍️ <b>Напиши сообщение в тикет #{ticket_id[:6].upper()}:</b>\n\n"
        "<i>(Чтобы отменить — нажми /cancel)</i>",
        parse_mode="HTML"
    )
    await state.update_data(ticket_id=ticket_id)
    await state.set_state(TicketState.waiting_message)


@router.message(Command("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отменено.", reply_markup=main_kb())


# ============================================
# ДОПОЛНИТЕЛЬНОЕ СООБЩЕНИЕ В СУЩЕСТВУЮЩИЙ ТИКЕТ
# ============================================
@router.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    # Если есть активный тикет — добавляем сообщение в него
    active = find_active_ticket(message.from_user.id)
    if active:
        add_message_to_ticket(active['id'], "client", message.text)
        
        await message.answer(
            f"✅ Сообщение добавлено в тикет <b>#{active['id'][:6].upper()}</b>",
            reply_markup=ticket_kb(active['id'], active['status']),
            parse_mode="HTML"
        )
        
        # Уведомляем админа
        try:
            await bot.send_message(
                ADMIN_ID,
                f"💬 <b>Новое сообщение в тикете #{active['id'][:6].upper()}</b>\n\n"
                f"👤 {message.from_user.full_name} (@{message.from_user.username or 'без username'})\n\n"
                f"{message.text}\n\n"
                f"🔗 Ответить: https://nil-bots-site-with-bot.vercel.app",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return
    
    # Нет активного тикета — предлагаем создать
    await message.answer(
        "🤔 У тебя нет активного тикета. Хочешь создать новый?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Создать тикет", callback_data="new_ticket")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_main")]
        ])
    )


# ============================================
# МОИ ТИКЕТЫ
# ============================================
@router.callback_query(F.data == "my_tickets")
async def my_tickets(call: CallbackQuery):
    await call.answer()
    tickets = get_user_tickets(call.from_user.id)
    
    if not tickets:
        await call.message.edit_text(
            "📭 <b>У тебя пока нет тикетов.</b>",
            reply_markup=main_kb(),
            parse_mode="HTML"
        )
        return
    
    text = "📋 <b>Твои тикеты:</b>\n\n"
    buttons = []
    for t in tickets:
        short_id = t['id'][:6].upper()
        status = get_status_text(t['status'])
        date = t.get('created_at', '')[:10]
        text += f"• <b>#{short_id}</b> — {status} ({date})\n"
        buttons.append([InlineKeyboardButton(text=f"📄 #{short_id}", callback_data=f"view_{t['id']}")])
    
    buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_main")])
    
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


@router.callback_query(F.data.startswith("view_"))
async def view_ticket(call: CallbackQuery):
    await call.answer()
    ticket_id = call.data.replace("view_", "")
    
    try:
        ticket_doc = db.collection("tickets").document(ticket_id).get()
        if not ticket_doc.exists:
            await call.message.edit_text("❌ Тикет не найден.", reply_markup=main_kb())
            return
        
        ticket = ticket_doc.to_dict()
        
        # Проверяем, что это тикет текущего пользователя
        if ticket.get("user_id") != call.from_user.id:
            await call.message.edit_text("❌ У тебя нет доступа к этому тикету.", reply_markup=main_kb())
            return
        
        text = f"🎫 <b>Тикет #{ticket_id[:6].upper()}</b>\n\n"
        text += f"📊 Статус: {get_status_text(ticket.get('status', 'new'))}\n"
        text += f"📅 Создан: {ticket.get('created_at', '')[:16].replace('T', ' ')}\n\n"
        
        messages = ticket.get("messages", [])
        if messages:
            text += "<b>История сообщений:</b>\n\n"
            for msg in messages[-10:]:  # Последние 10 сообщений
                if msg["from"] == "client":
                    text += f"👤 <b>Ты:</b> {msg['text'][:200]}\n\n"
                else:
                    text += f"🛠️ <b>Поддержка:</b> {msg['text'][:200]}\n\n"
        
        await call.message.edit_text(
            text,
            reply_markup=ticket_kb(ticket_id, ticket.get('status', 'new')),
            parse_mode="HTML"
        )
    except Exception as e:
        await call.message.edit_text(f"❌ Ошибка: {e}", reply_markup=main_kb())


# ============================================
# ЗАКРЫТЬ ТИКЕТ
# ============================================
@router.callback_query(F.data.startswith("close_"))
async def close_ticket_handler(call: CallbackQuery):
    await call.answer()
    ticket_id = call.data.replace("close_", "")
    close_ticket(ticket_id)
    
    await call.message.edit_text(
        f"🔒 <b>Тикет #{ticket_id[:6].upper()} закрыт.</b>\n\n"
        "Спасибо за обращение!",
        reply_markup=main_kb(),
        parse_mode="HTML"
    )


# ============================================
# ПОМОЩЬ
# ============================================
@router.callback_query(F.data == "help")
async def help_handler(call: CallbackQuery):
    await call.answer()
    text = (
        "❓ <b>Как пользоваться ботом:</b>\n\n"
        "1️⃣ Нажми «✍️ Написать в поддержку»\n"
        "2️⃣ Опиши свою проблему\n"
        "3️⃣ Дождись ответа от разработчика\n"
        "4️⃣ Можешь писать дополнительные сообщения в тот же тикет\n\n"
        "⏱ <b>Среднее время ответа:</b> 1-2 часа\n\n"
        "💡 <b>Совет:</b> Описывай проблему максимально подробно — "
        "так я смогу быстрее помочь!"
    )
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_main")]
        ]),
        parse_mode="HTML"
    )


# ============================================
# ГЛАВНОЕ МЕНЮ
# ============================================
@router.callback_query(F.data == "back_main")
async def back_main(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    
    active = find_active_ticket(call.from_user.id)
    text = f"🏠 <b>Главное меню</b>\n\n"
    if active:
        text += f"🔔 Активный тикет: <b>#{active['id'][:6].upper()}</b>\n"
    
    await call.message.edit_text(text, reply_markup=main_kb(), parse_mode="HTML")


# ============================================
# REAL-TIME СЛУШАНИЕ FIREBASE ДЛЯ ОТВЕТОВ АДМИНА
# ============================================
async def listen_for_replies():
    """
    Слушает Firebase и отправляет ответы админа клиентам через Telegram.
    Это нужно, потому что сайт не может напрямую писать в Telegram.
    """
    print("👂 Запущен listener ответов от админа...")
    
    # Хранит последнее известное количество сообщений в тикетах
    last_counts: dict[str, int] = {}
    
    while True:
        try:
            # Получаем все активные тикеты
            tickets = db.collection("tickets")\
                .where("status", "in", ["new", "working", "resolved"])\
                .stream()
            
            for ticket_doc in tickets:
                ticket_id = ticket_doc.id
                ticket = ticket_doc.to_dict()
                
                messages = ticket.get("messages", [])
                current_count = len(messages)
                last_count = last_counts.get(ticket_id, 0)
                
                # Если появились новые сообщения
                if current_count > last_count:
                    # Проверяем только новые сообщения
                    for msg in messages[last_count:]:
                        # Если это ответ админа — отправляем клиенту
                        if msg.get("from") == "admin":
                            user_id = ticket.get("user_id")
                            if user_id:
                                try:
                                    await bot.send_message(
                                        user_id,
                                        f"🛠️ <b>Ответ от поддержки</b> (тикет #{ticket_id[:6].upper()}):\n\n{msg['text']}",
                                        parse_mode="HTML"
                                    )
                                except Exception as e:
                                    print(f"Не удалось отправить ответ пользователю {user_id}: {e}")
                    
                    last_counts[ticket_id] = current_count
                
                # Очистка старых записей
                if len(last_counts) > 1000:
                    last_counts = {k: v for k, v in last_counts.items() if v > 0}
            
            await asyncio.sleep(3)  # Проверяем каждые 3 секунды
            
        except Exception as e:
            print(f"Ошибка в listener: {e}")
            await asyncio.sleep(5)


# ============================================
# ЗАПУСК
# ============================================
async def main():
    print("=" * 50)
    print("🎫 Nil Bots — Бот поддержки запущен!")
    print(f"👑 Admin ID: {ADMIN_ID}")
    print(f"🔗 Firebase: подключён")
    print("=" * 50)
    
    # Запускаем listener параллельно с polling
    listener_task = asyncio.create_task(listen_for_replies())
    
    try:
        await dp.start_polling(bot)
    finally:
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
