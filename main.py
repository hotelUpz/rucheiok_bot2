from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

from CORE.orchestrator import TradingBot
from CORE.lvg_setter import GlobalLeverageSetter
from TG.admin import AdminTgBot
from c_log import UnifiedLogger
from utils import load_json

BASE_DIR = Path(__file__).resolve().parent
CFG_PATH = BASE_DIR / "cfg.json"
CACHE_PATH = BASE_DIR / "leverage_cache.json"

load_dotenv(BASE_DIR / ".env")
logger = UnifiedLogger("main")


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
    cfg = load_json(filepath=CFG_PATH, default={})
    tg_enabled = cfg.get("tg", {}).get("enable", False)
    
    bot = TradingBot(cfg)
    tasks = []

    try:
        # Извлекаем параметры для глобальной настройки плечей
        # Приоритет: секция "phemex" -> секция "credentials" / "risk"
        phemex_cfg = cfg.get("phemex", {})
        api_key = os.getenv("API_KEY") or phemex_cfg.get("api_key") or cfg.get("credentials", {}).get("api_key", "")
        api_secret = os.getenv("API_SECRET") or phemex_cfg.get("api_secret") or cfg.get("credentials", {}).get("api_secret", "")
        
        risk_cfg = cfg.get("risk", {})
        leverage_cfg = risk_cfg.get("leverage", {})
        
        leverage_val = phemex_cfg.get("leverage") or leverage_cfg.get("val")
        raw_margin = phemex_cfg.get("margin_mode") or leverage_cfg.get("margin_mode", 2)
        
        # Маппинг для красивого лога и корректной передачи в setter
        # 1 / "cross" -> Cross, 2 / "isolated" -> Isolated
        if str(raw_margin).lower() in ("1", "cross"):
            margin_mode_str = "Cross"
            raw_margin = 1
        else:
            margin_mode_str = "Isolated"
            raw_margin = 2
        
        set_previous = leverage_cfg.get("set_previous", False)
        use_cache = leverage_cfg.get("used_by_cache", False)
        delay_sec = leverage_cfg.get("delay_sec", 0.3)
        
        if set_previous:
            # 1. Запуск глобальной конфигурации
            logger.info(f"⚙️ Запуск глобальной конфигурации (Leverage: {leverage_val}x, Margin: {margin_mode_str})...")
            setter = GlobalLeverageSetter(
                api_key=api_key,
                api_secret=api_secret,
                leverage_val=leverage_val,
                margin_mode=raw_margin,
                black_list=bot.black_list,
                use_cache=use_cache,
                cache_path=CACHE_PATH,
                delay_sec=delay_sec
            )
            await setter.apply()
            print("lev set succ")
        
        else:
            logger.info("⚙️ Скип установки (Leverage & Margin), так как set_previous == false")

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

            # await bot.start() # -- форсированный запуск в обход админа.
        else:
            logger.warning("TG отключен. Автостарт торговли...")
            await bot.start()

        # Фикс #4 TECH_DEBT: не привязываем жизнь бота к TG-задаче.
        # Задачи уже запущены как create_task — просто ждём вечно.
        stop_event = asyncio.Event()
        await stop_event.wait()
                
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.warning("\n🛑 Получен сигнал прерывания. Остановка...")
    finally:
        logger.info("🧹 Очистка ресурсов...")
        await bot.aclose()  # aclose() вызывает stop() внутри — двойной вызов был лишним
        
        for t in tasks:
            t.cancel()
        
        await asyncio.sleep(0.5) 
        logger.info("✅ Программа безопасно завершена.")

if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass

# # chmod 600 ssh_key.txt
# # eval "$(ssh-agent -s)" 
# # ssh-add ssh_key.txt
# # source .ssh-autostart.sh
# # git push --set-upstream origin master
# # git config --global push.autoSetupRemote true
# # ssh -T git@github.com 
# # git log -1

# # git add .
# # git commit -m "plh37" 
# # git push

# # pip install anthropic
# # npm install -g @anthropic-ai/claude-code

# # export ANTHROPIC_API_KEY=...
# # claude