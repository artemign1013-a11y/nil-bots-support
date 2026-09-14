import os
 import json
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore

import config

# ===== ИНИЦИАЛИЗАЦИЯ FIREBASE =====
cred_dict = json.loads(os.environ.get("FIREBASE_KEY_JSON"))
cred = credentials.Certificate(cred_dict)
firebase_admin.initialize_app(cred, config.FIREBASE_CONFIG)
db = firestore.client()

# ===== ИНИЦИАЛИЗАЦИЯ БОТА =====
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# ===== СОСТОЯНИЯ =====
class SupportStates(StatesGroup):
    waiting_message = State()

# ===== КЛАВИАТУРЫ =====
def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Задать вопрос", callback_data="new_ticket")],
        [InlineKeyboardButton(text="📋 Мои тикеты", callback_data="my_tickets")],
        [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")],
    ])

def ticket_actions(ticket_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Написать ещё", callback_data=f"reply_{ticket_id}")],
        [InlineKeyboardButton(text="✅ Закрыть тикет", callback_data=f"close_{ticket_id}")],
    ])

# ===== КОМАНДА /start =====
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "Я — бот технической поддержки **Nil Bots**.\n"
        "Задай свой вопрос, и я передам его разработчику.",
        reply_markup=main_keyboard(),
        parse_mode="Markdown"
    )

# ===== СОЗДАНИЕ ТИКЕТА =====
@dp.callback_query(F.data == "new_ticket")
async def new_ticket(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "️ Напиши свой вопрос или опиши проблему.\n"
        "Я передам его разработчику, и он ответит тебе.",
        reply_markup=None
    )
    await state.set_state(SupportStates.waiting_message)
    await callback.answer()

@dp.message(SupportStates.waiting_message)
async def receive_message(message: Message, state: FSMContext):
    # Создаём тикет в Firebase
    ticket_ref = db.collection("tickets").document()
    ticket_data = {
        "id": ticket_ref.id,
        "user_id": message.from_user.id,
        "username": message.from_user.username or "Без username",
        "full_name": message.from_user.full_name,
        "message": message.text,
        "status": "new",
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "messages": [{
            "from": "client",
            "text": message.text,
            "time": datetime.now().isoformat()
        }]
    }
    ticket_ref.set(ticket_data)
    
    await state.clear()
    await message.answer(
        f"✅ Тикет #{ticket_ref.id[:6].upper()} создан!\n\n"
        f"Твой вопрос:\n_{message.text}_\n\n"
        "Я передам его разработчику. Он ответит тебе здесь.",
        reply_markup=ticket_actions(ticket_ref.id),
        parse_mode="Markdown"
    )
    
    # Уведомление админу
    await bot.send_message(
        config.ADMIN_ID,
        f"🆕 Новый тикет #{ticket_ref.id[:6].upper()}\n\n"
        f"👤 {message.from_user.full_name} (@{message.from_user.username})\n"
        f"💬 {message.text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Ответить", callback_data=f"admin_reply_{ticket_ref.id}")],
            [InlineKeyboardButton(text="Открыть все тикеты", callback_data="admin_tickets")]
        ])
    )

# ===== МОИ ТИКЕТЫ =====
@dp.callback_query(F.data == "my_tickets")
async def my_tickets(callback: CallbackQuery):
    tickets = db.collection("tickets").where("user_id", "==", callback.from_user.id).stream()
    ticket_list = list(tickets)
    
    if not ticket_list:
        await callback.message.edit_text("📭 У тебя пока нет тикетов.", reply_markup=main_keyboard())
        await callback.answer()
        return
    
    text = "📋 **Твои тикеты:**\n\n"
    for t in ticket_list:
        data = t.to_dict()
        status_emoji = {"new": "🆕", "working": "⚙️", "resolved": "✅", "closed": "🔒"}[data["status"]]
        text += f"{status_emoji} #{t.id[:6].upper()} — {data['message'][:50]}...\n"
    
    await callback.message.edit_text(text, reply_markup=main_keyboard(), parse_mode="Markdown")
    await callback.answer()

# ===== ПОМОЩЬ =====
@dp.callback_query(F.data == "help")
async def help_cmd(callback: CallbackQuery):
    await callback.message.edit_text(
        "ℹ️ **Как это работает:**\n\n"
        "1️⃣ Нажми «Задать вопрос»\n"
        "2️ Опиши свою проблему\n"
        "3️⃣ Получи номер тикета\n"
        "4️ Дождись ответа разработчика\n\n"
        "Обычно отвечаем в течение 2 часов!",
        reply_markup=main_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

# ===== АДМИН: ОТВЕТ НА ТИКЕТ =====
@dp.callback_query(F.data.startswith("admin_reply_"))
async def admin_reply(callback: CallbackQuery):
    ticket_id = callback.data.replace("admin_reply_", "")
    ticket = db.collection("tickets").document(ticket_id).get()
    
    if not ticket.exists:
        await callback.answer("❌ Тикет не найден", show_alert=True)
        return
    
    data = ticket.to_dict()
    await callback.message.edit_text(
        f" Тикет #{ticket_id[:6].upper()}\n"
        f"👤 {data['full_name']} (@{data['username']})\n"
        f"💬 {data['message']}\n\n"
        "Напиши ответ:",
        reply_markup=None
    )
    
    # Сохраняем ID тикета для следующего сообщения
    await dp.storage.set_data(chat_id=callback.from_user.id, key="replying_to", value=ticket_id)
    await callback.answer()

@dp.message(F.chat.id == config.ADMIN_ID)
async def admin_message(message: Message):
    """Обработка ответов админа"""
    replying_to = await dp.storage.get_data(chat_id=message.chat.id, key="replying_to")
    
    if not replying_to:
        # Если не в режиме ответа — показываем список тикетов
        await show_admin_tickets(message)
        return
    
    # Отправляем ответ клиенту
    ticket = db.collection("tickets").document(replying_to).get()
    if ticket.exists:
        data = ticket.to_dict()
        await bot.send_message(
            data["user_id"],
            f" **Ответ от поддержки** (тикет #{replying_to[:6].upper()}):\n\n"
            f"{message.text}",
            parse_mode="Markdown"
        )
        
        # Обновляем тикет
        db.collection("tickets").document(replying_to).update({
            "status": "working",
            "updated_at": datetime.now().isoformat(),
            "messages": data["messages"] + [{
                "from": "admin",
                "text": message.text,
                "time": datetime.now().isoformat()
            }]
        })
        
        await message.answer("✅ Ответ отправлен клиенту!")
    
    await dp.storage.set_data(chat_id=message.chat.id, key="replying_to", value=None)

# ===== АДМИН: СПИСОК ТИКЕТОВ =====
async def show_admin_tickets(message: Message):
    tickets = db.collection("tickets").order_by("created_at", direction="DESCENDING").limit(20).stream()
    ticket_list = list(tickets)
    
    if not ticket_list:
        await message.answer("📭 Тикетов пока нет.")
        return
    
    text = "📋 **Последние тикеты:**\n\n"
    keyboard = []
    
    for t in ticket_list:
        data = t.to_dict()
        status_emoji = {"new": "🆕", "working": "⚙️", "resolved": "✅", "closed": "🔒"}[data["status"]]
        text += f"{status_emoji} #{t.id[:6].upper()} — {data['full_name']}\n"
        keyboard.append([InlineKeyboardButton(
            text=f"{status_emoji} #{t.id[:6].upper()} — {data['full_name']}",
            callback_data=f"admin_view_{t.id}"
        )])
    
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@dp.callback_query(F.data == "admin_tickets")
async def admin_tickets_list(callback: CallbackQuery):
    await show_admin_tickets(callback.message)
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_view_"))
async def admin_view_ticket(callback: CallbackQuery):
    ticket_id = callback.data.replace("admin_view_", "")
    ticket = db.collection("tickets").document(ticket_id).get()
    
    if not ticket.exists:
        await callback.answer("❌ Тикет не найден", show_alert=True)
        return
    
    data = ticket.to_dict()
    status_emoji = {"new": "🆕", "working": "️", "resolved": "✅", "closed": "🔒"}[data["status"]]
    
    text = f"{status_emoji} **Тикет #{ticket_id[:6].upper()}**\n\n"
    text += f"👤 {data['full_name']} (@{data['username']})\n"
    text += f"📅 {datetime.fromisoformat(data['created_at']).strftime('%d.%m.%Y %H:%M')}\n\n"
    text += f" **Сообщения:**\n\n"
    
    for msg in data["messages"]:
        sender = "👤 Клиент" if msg["from"] == "client" else "️ Ты"
        text += f"{sender}:\n{msg['text']}\n\n"
    
    keyboard = [
        [InlineKeyboardButton(text="✏️ Ответить", callback_data=f"admin_reply_{ticket_id}")],
        [InlineKeyboardButton(text="✅ Решён", callback_data=f"admin_resolve_{ticket_id}")],
        [InlineKeyboardButton(text=" Закрыть", callback_data=f"admin_close_{ticket_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_tickets")]
    ]
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_resolve_"))
async def admin_resolve(callback: CallbackQuery):
    ticket_id = callback.data.replace("admin_resolve_", "")
    db.collection("tickets").document(ticket_id).update({"status": "resolved"})
    await callback.answer("✅ Статус изменён на «Решён»")
    await callback.message.edit_text("✅ Тикет отмечен как решённый.", reply_markup=None)

@dp.callback_query(F.data.startswith("admin_close_"))
async def admin_close(callback: CallbackQuery):
    ticket_id = callback.data.replace("admin_close_", "")
    db.collection("tickets").document(ticket_id).update({"status": "closed"})
    await callback.answer("🔒 Тикет закрыт")
    await callback.message.edit_text("🔒 Тикет закрыт.", reply_markup=None)

# ===== ЗАПУСК =====
async def main():
    print("🤖 Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
