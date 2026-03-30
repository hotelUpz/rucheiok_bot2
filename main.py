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
from utils import deep_update

BASE_DIR = Path(__file__).resolve().parent
CFG_PATH = BASE_DIR / "cfg.json"

load_dotenv(BASE_DIR / ".env")
logger = UnifiedLogger("main")

def load_cfg(path: str | Path = CFG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg

async def _main():
    cfg = load_cfg()
    tg_enabled = cfg.get("tg", {}).get("enable", False)
    
    bot = TradingBot(cfg)
    tg_admin = None

    try:
        if tg_enabled:
            token = os.getenv("TELEGRAM_TOKEN") or cfg["tg"].get("token")
            chat_id = os.getenv("TELEGRAM_CHAT_ID") or cfg["tg"].get("chat_id")
            
            if not token or not chat_id:
                logger.error("Telegram включен, но token/chat_id не заданы.")
                sys.exit(1)
                
            tg_admin = AdminTgBot(token, chat_id, bot)
            logger.info("🤖 Запуск Telegram-админки... Ждем команды 'Старт'.")
            await tg_admin.dp.start_polling(tg_admin.bot, allowed_updates=["message"])
        else:
            logger.warning("TG отключен. Автостарт торговли...")
            await bot.start()
            while True:
                await asyncio.sleep(3600)
                
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.warning("\n🛑 Получен сигнал прерывания (Ctrl+C). Остановка...")
    finally:
        logger.info("🧹 Очистка ресурсов и закрытие сессий...")
        await bot.aclose()
        if tg_admin:
            await tg_admin.bot.session.close()
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