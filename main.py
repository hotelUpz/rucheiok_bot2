from __future__ import annotations

import asyncio
import os
import sys
import json
from pathlib import Path
from dotenv import load_dotenv

from CORE.bot import TradingBot
from TG.admin import AdminTgBot
from c_log import UnifiedLogger

BASE_DIR = Path(__file__).resolve().parent
CFG_PATH = BASE_DIR / "cfg.json"

load_dotenv(BASE_DIR / ".env")
logger = UnifiedLogger("main")

def load_cfg(path: str | Path = CFG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg

async def polling_supervisor(tg_admin: AdminTgBot):
    """Следит за тем, чтобы Telegram бот всегда был онлайн"""
    logger.info("🤖 Запуск супервизора Telegram...")
    
    retry_pause = 5.0  # Пауза перед рестартом при ошибке
    
    while True:
        try:
            # skip_updates=True полезен, чтобы бот не захлебнулся 
            # старыми командами после долгого оффлайна
            await tg_admin.dp.start_polling(
                tg_admin.bot, 
                allowed_updates=["message"],
                skip_updates=True,
                handle_as_tasks=True # Важно для параллельной обработки
            )
            logger.error("⚠️ Поллинг завершился штатно (неожиданно)")
        
        except asyncio.CancelledError:
            # Сюда попадаем при выключении программы (Ctrl+C)
            logger.info("Stopping TG supervisor...")
            break
            
        except Exception as e:
            logger.error(f"💥 Критическая ошибка TG Polling: {e}")
            logger.info(f"Перезапуск через {retry_pause} сек...")
        
        # В любой непонятной ситуации — сбрасываем сессию и ждем
        await tg_admin.reset_session()
        await asyncio.sleep(retry_pause)

async def _main():
    cfg = load_cfg()
    tg_enabled = cfg.get("tg", {}).get("enable", False)
    
    bot = TradingBot(cfg)
    tasks = []

    try:
        if tg_enabled:
            token = os.getenv("TELEGRAM_TOKEN") or cfg["tg"].get("token")
            chat_id = os.getenv("TELEGRAM_CHAT_ID") or cfg["tg"].get("chat_id")
            
            if not token or not chat_id:
                logger.error("Telegram включен, но token/chat_id не заданы.")
                sys.exit(1)
            
            tg_admin = AdminTgBot(token, chat_id, bot)
            
            # Создаем задачу для ТГ, которая будет крутиться в фоне
            tg_task = asyncio.create_task(polling_supervisor(tg_admin))
            tasks.append(tg_task)
        else:
            # Если ТГ нет, просто запускаем торговлю сразу
            logger.warning("TG отключен. Автостарт торговли...")
            await bot.start()

        # Держим main живым, пока работают задачи
        # Если добавите другие фоновые задачи (например, веб-сервер), 
        # добавьте их в tasks.
        if tasks:
            await asyncio.gather(*tasks)
        else:
            # Бесконечный цикл для режима без ТГ
            while True: await asyncio.sleep(3600)
                
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.warning("\n🛑 Получен сигнал прерывания. Остановка...")
    finally:
        logger.info("🧹 Очистка ресурсов...")
        # Останавливаем торговые процессы
        await bot.stop()
        await bot.aclose()
        
        # Отменяем все фоновые задачи (включая супервизор ТГ)
        for t in tasks:
            t.cancel()
        
        await asyncio.sleep(0.5) 
        logger.info("✅ Программа безопасно завершена.")

if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass

# chmod 600 ssh_key.txt
# eval "$(ssh-agent -s)"
# ssh-add ssh_key.txt
# source .ssh-autostart.sh
# git push --set-upstream origin master
# git config --global push.autoSetupRemote true
# ssh -T git@github.com
# git log -1