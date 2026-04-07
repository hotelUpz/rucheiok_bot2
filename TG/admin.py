# ============================================================
# FILE: TG/admin.py
# ROLE: Интерактивная панель управления (aiogram 3.x)
# ============================================================
from __future__ import annotations

import re
import os
from pathlib import Path
import json
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, StateFilter
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from c_log import UnifiedLogger
from utils import get_config_summary
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from CORE.orchestrator import TradingBot

CFG_PATH = Path(__file__).resolve().parent.parent / "cfg.json"
TEMP_CFG_PATH = Path(__file__).resolve().parent.parent / "cfg.tmp.json"

logger = UnifiedLogger("tg")

class BotStates(StatesGroup):
    waiting_for_list = State()
    waiting_for_config = State()

class AdminTgBot:
    def __init__(self, token: str, chat_id: str, trading_bot: "TradingBot"):
        self.bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
        self.dp = Dispatcher()
        self.chat_id = str(chat_id)
        self.tb = trading_bot

        self.kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="▶️ Старт"), KeyboardButton(text="⏹ Стоп")],
                [KeyboardButton(text="📝 Установить BlackList"), KeyboardButton(text="⚙️ Установить настройки")],
                [KeyboardButton(text="📊 Статус")]
            ],
            resize_keyboard=True
        )
        self._register_handlers()

    async def reset_session(self):
        """Безопасное пересоздание сессии при сетевых сбоях"""
        try:
            if self.bot.session and not self.bot.session.closed:
                await self.bot.session.close()
        except Exception as e:
            logger.error(f"Ошибка при закрытии сессии TG: {e}")
        
        # Создаем новую сессию
        self.bot.session = AiohttpSession()
        logger.warning("🔄 Сессия Telegram API перезапущена")

    def _register_handlers(self):
        @self.dp.message(Command("start"), StateFilter("*"))
        async def cmd_start(msg: Message, state: FSMContext):
            if str(msg.from_user.id) != self.chat_id: return
            try:
                await state.clear()
                await msg.answer("🤖 <b>Панель управления Rucheiok Bot</b>", reply_markup=self.kb)
            except Exception as e:
                logger.error(f"Ошибка в cmd_start: {e}")

        @self.dp.message(F.text == "▶️ Старт", StateFilter("*"))
        async def btn_start(msg: Message, state: FSMContext):
            if str(msg.from_user.id) != self.chat_id: return
            try:
                await state.clear()
                if getattr(self.tb, '_is_running', False):
                    await msg.answer("⚠️ Бот уже запущен и торгует.")
                    return
                await msg.answer("🚀 Запускаю торговые процессы...")
                await self.tb.start()
            except Exception as e:
                logger.error(f"Ошибка при старте бота: {e}")
                await msg.answer("❌ Ошибка при запуске. Проверьте логи.")

        @self.dp.message(F.text == "⏹ Стоп", StateFilter("*"))
        async def btn_stop(msg: Message, state: FSMContext):
            if str(msg.from_user.id) != self.chat_id: return
            try:
                await state.clear()
                if not getattr(self.tb, '_is_running', False):
                    await msg.answer("⚠️ Бот и так остановлен.")
                    return
                await msg.answer("🛑 Останавливаю процессы... (ордера остаются на бирже)")
                await self.tb.stop()
                await msg.answer("✅ Бот остановлен.")
            except Exception as e:
                logger.error(f"Ошибка при остановке бота: {e}")
                await msg.answer("❌ Ошибка при остановке. Проверьте логи.")

        @self.dp.message(F.text == "📊 Статус", StateFilter("*"))
        async def btn_status(msg: Message, state: FSMContext):
            if str(msg.from_user.id) != self.chat_id: return
            try:
                await state.clear()
                state_text = "🟢 ЗАПУЩЕН" if getattr(self.tb, '_is_running', False) else "🔴 ОСТАНОВЛЕН"
                pos_count = len(self.tb.state.active_positions)
                
                text = (
                    f"<b>Статус:</b> {state_text}\n"
                    f"<b>Позиций:</b> {pos_count} / {self.tb.max_active_positions}\n"
                    f"<b>Монет с лоссами:</b> {len(self.tb.state.consecutive_fails)}\n"
                    f"<b>Монет в карантине:</b> {len(self.tb.state.quarantine_until)}\n"
                )
                
                if pos_count > 0:
                    text += "\n<b>Сделки:</b>\n"
                    for sym, pos in self.tb.state.active_positions.items():
                        text += f"• #{sym} ({pos.side}) | Вход: {pos.entry_price}\n"
                
                text += f"\n{get_config_summary(self.tb.cfg)}"
                await msg.answer(text)
            except Exception as e:
                logger.error(f"Ошибка вывода статуса: {e}")
                await msg.answer("❌ Произошла ошибка при формировании статуса.")

        @self.dp.message(F.text == "📝 Установить BlackList", StateFilter("*"))
        async def btn_bl(msg: Message, state: FSMContext):
            if str(msg.from_user.id) != self.chat_id: return
            try:
                bl_text = " ".join(self.tb.black_list) if self.tb.black_list else "ПУСТО"
                text = (
                    f"🚫 <b>Текущий BlackList:</b>\n"
                    f"<blockquote>{bl_text}</blockquote>\n\n"
                    f"✏️ <b>УСТАНОВКА НОВОГО СПИСКА:</b>\n"
                    f"Отправьте мне список монет через пробел.\n"
                    f"🗑 <i>Чтобы очистить список, отправьте <code>0</code> или <code>пусто</code>.</i>\n\n"
                    f"❌ <i>Для отмены нажмите любую кнопку меню.</i>"
                )
                await msg.answer(text)
                await state.set_state(BotStates.waiting_for_list)
            except Exception as e:
                logger.error(f"Ошибка при запросе BlackList: {e}")
                await msg.answer("❌ Произошла ошибка. Попробуйте еще раз.")

        @self.dp.message(StateFilter(BotStates.waiting_for_list))
        async def process_bl(msg: Message, state: FSMContext):
            if str(msg.from_user.id) != self.chat_id: return
            try:
                # Защита от стикеров/картинок
                if not msg.text:
                    await msg.answer("⚠️ Пожалуйста, отправьте список текстом.")
                    return

                text = msg.text.strip().upper()
                
                if text in ["▶️ СТАРТ", "⏹ СТОП", "📊 СТАТУС", "📝 УСТАНОВИТЬ BLACKLIST", "⚙️ УСТАНОВИТЬ НАСТРОЙКИ"]:
                    await state.clear()
                    await msg.answer("⚠️ <b>Ввод отменен.</b> Нажмите нужную кнопку меню еще раз.")
                    return
                
                if text in ("0", "ПУСТО", "EMPTY", "NONE", "CLEAR", "ОЧИСТИТЬ"):
                    symbols = []
                else:
                    symbols = [s.strip() for s in re.split(r'[\s,;]+', text) if s.strip()]
                
                success, reply_msg = self.tb.set_blacklist(symbols)
                if success:
                    bl_text = " ".join(self.tb.black_list) if self.tb.black_list else "ПУСТО"
                    reply_msg += f"\n\n📋 <b>Новый BlackList установлен:</b>\n<blockquote>{bl_text}</blockquote>"
                
                await msg.answer(reply_msg)
                await state.clear()
            except Exception as e:
                logger.error(f"Ошибка при обработке нового BlackList: {e}")
                await msg.answer("❌ Произошла ошибка при сохранении. Попробуйте снова.")
                await state.clear()

        # ---------- БЛОК ЗАГРУЗКИ КОНФИГОВ ----------
        @self.dp.message(F.text == "⚙️ Установить настройки", StateFilter("*"))
        async def btn_config(msg: Message, state: FSMContext):
            if str(msg.from_user.id) != self.chat_id: return
            try:
                text = (
                    "⚙️ <b>ЗАГРУЗКА НАСТРОЕК</b>\n\n"
                    "Отправьте мне файл конфигурации <code>.json</code>.\n"
                    "⚠️ <i>Этот файл <b>ПОЛНОСТЬЮ ПЕРЕЗАПИШЕТ</b> текущие настройки бота (cfg.json).</i>\n\n"
                    "❌ <i>Для отмены нажмите любую кнопку в меню.</i>"
                )
                await msg.answer(text)
                await state.set_state(BotStates.waiting_for_config)
            except Exception as e:
                logger.error(f"Ошибка при вызове меню настроек: {e}")

        @self.dp.message(StateFilter(BotStates.waiting_for_config), F.document)
        async def process_config_file(msg: Message, state: FSMContext):
            if str(msg.from_user.id) != self.chat_id: return
            file_path = TEMP_CFG_PATH
            try:
                doc = msg.document
                if not doc.file_name.endswith('.json'):
                    await msg.answer("❌ <b>Ошибка:</b> Файл должен иметь расширение .json")
                    return
                    
                await self.bot.download(doc, destination=file_path)
                
                with open(file_path, "r", encoding="utf-8") as f:
                    json.load(f) # Просто валидация
                
                # Сохраняем физически ПОВЕРХ основного конфига
                os.replace(file_path, CFG_PATH)
                
                # ГЛАВНЫЙ ФИКС: ПРИМЕНЯЕМ В ОПЕРАТИВНУЮ ПАМЯТЬ
                success, reload_msg = self.tb.cfg_manager.reload_config()
                
                if success:
                    await msg.answer("✅ <b>Настройки загружены и применены «НА ЛЕТУ»!</b>\n\nНажмите <b>📊 Статус</b>, чтобы убедиться.\n<i>(Останавливать бота не нужно)</i>")
                else:
                    await msg.answer(f"⚠️ Файл сохранен, но применить на лету не вышло: <code>{reload_msg}</code>\nПотребуется перезапуск скрипта.")
                
                await state.clear()
                
            except json.JSONDecodeError as e:
                if os.path.exists(file_path): os.remove(file_path)
                await msg.answer(f"❌ <b>Ошибка:</b> Неверный формат JSON.\n\nПодробности: <code>{e}</code>")
            except Exception as e:
                if os.path.exists(file_path): os.remove(file_path)
                await msg.answer(f"❌ <b>Системная ошибка:</b> <code>{e}</code>")
                logger.error(f"Config upload error: {e}")

        @self.dp.message(StateFilter(BotStates.waiting_for_config))
        async def process_config_invalid(msg: Message, state: FSMContext):
            if str(msg.from_user.id) != self.chat_id: return
            try:
                # Защита от NoneType если прислали фото/стикер
                if msg.text and msg.text in ["▶️ Старт", "⏹ Стоп", "📊 Статус", "📝 Установить BlackList", "⚙️ Установить настройки"]:
                    await state.clear()
                    await msg.answer("⚠️ <b>Ожидание файла отменено.</b>")
                    return
                await msg.answer("⚠️ Я жду файл с расширением <b>.json</b>.\nДля отмены нажмите любую кнопку меню.")
            except Exception as e:
                logger.error(f"Ошибка в process_config_invalid: {e}")