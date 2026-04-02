from __future__ import annotations

import asyncio
import os
import sys
import json
from pathlib import Path
from dotenv import load_dotenv

from CORE.bot import TradingBot
from CORE.leverage_setter import GlobalLeverageSetter
from TG.admin import AdminTgBot
from c_log import UnifiedLogger

BASE_DIR = Path(__file__).resolve().parent
CFG_PATH = BASE_DIR / "cfg.json"
CACHE_PATH = BASE_DIR / "leverage_cache.json"

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
            await tg_admin.dp.start_polling(
                tg_admin.bot, 
                allowed_updates=["message"],
                skip_updates=True,
                handle_as_tasks=True
            )
            logger.error("⚠️ Поллинг завершился штатно (неожиданно)")
        
        except asyncio.CancelledError:
            logger.info("Stopping TG supervisor...")
            break
            
        except Exception as e:
            logger.error(f"💥 Критическая ошибка TG Polling: {e}")
            logger.info(f"Перезапуск через {retry_pause} сек...")
        
        await tg_admin.reset_session()
        await asyncio.sleep(retry_pause)

async def _main():
    cfg = load_cfg()
    tg_enabled = cfg.get("tg", {}).get("enable", False)
    
    bot = TradingBot(cfg)
    tasks = []

    try:
        # Извлекаем параметры для глобальной настройки плечей
        api_key = os.getenv("API_KEY") or cfg.get("credentials", {}).get("api_key", "")
        api_secret = os.getenv("API_SECRET") or cfg.get("credentials", {}).get("api_secret", "")
        
        risk_cfg = cfg.get("risk", {})
        leverage_cfg = risk_cfg.get("leverage", {})
        
        # Парсим новую структуру (словарь или число)
        if isinstance(leverage_cfg, dict):
            leverage_val = leverage_cfg.get("val")
            use_cache = leverage_cfg.get("used_by_cache", False)
            margin_mode = leverage_cfg.get("margin_mode", 2)
            delay_sec = leverage_cfg.get("delay_sec", 0.3)
        else:
            raise TypeError(f"Expected dict for leverage_cfg, got {type(leverage_cfg).__name__}")
            
        # 1. Запуск глобальной конфигурации
        logger.info("⚙️ Запуск глобальной конфигурации параметров (Leverage & Margin)...")
        setter = GlobalLeverageSetter(
            api_key=api_key,
            api_secret=api_secret,
            leverage_val=leverage_val,
            margin_mode=margin_mode,
            black_list=bot.black_list,
            use_cache=use_cache,
            cache_path=CACHE_PATH,
            delay_sec=delay_sec
        )
        await setter.apply()

        print("lev set succ")
        return

        # 2. Инициализация TG и Торговли
        if tg_enabled:
            token = os.getenv("TELEGRAM_TOKEN") or cfg["tg"].get("token")
            chat_id = os.getenv("TELEGRAM_CHAT_ID") or cfg["tg"].get("chat_id")
            
            if not token or not chat_id:
                logger.error("Telegram включен, но token/chat_id не заданы.")
                sys.exit(1)
            
            tg_admin = AdminTgBot(token, chat_id, bot)
            tg_task = asyncio.create_task(polling_supervisor(tg_admin))
            tasks.append(tg_task)
        else:
            logger.warning("TG отключен. Автостарт торговли...")
            await bot.start()

        if tasks:
            await asyncio.gather(*tasks)
        else:
            while True: await asyncio.sleep(3600)
                
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.warning("\n🛑 Получен сигнал прерывания. Остановка...")
    finally:
        logger.info("🧹 Очистка ресурсов...")
        await bot.stop()
        await bot.aclose()
        
        for t in tasks:
            t.cancel()
        
        await asyncio.sleep(0.5) 
        logger.info("✅ Программа безопасно завершена.")

if __name__ == "__main__":
    try:
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
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