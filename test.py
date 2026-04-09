import asyncio
import json
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# Импортируем твой класс WS
from API.PHEMEX.ws_private import PhemexPrivateWS

# Настраиваем изолированный логгер только для этого теста
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)
log_file = log_dir / "test_ws.log"

# Очищаем старый лог перед новым запуском
if log_file.exists():
    with open(log_file, "w") as f:
        f.write("")

logger = logging.getLogger("WS_TESTER")
logger.setLevel(logging.DEBUG)
fh = logging.FileHandler(log_file, encoding='utf-8')
fh.setFormatter(logging.Formatter('%(asctime)s | %(message)s'))
logger.addHandler(fh)

ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(ch)

async def main():
    load_dotenv()
    
    # Грузим ключи из твоего конфига
    with open("cfg.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
        
    api_key = os.getenv("API_KEY") or cfg.get("credentials", {}).get("api_key", "")
    api_secret = os.getenv("API_SECRET") or cfg.get("credentials", {}).get("api_secret", "")

    if not api_key or not api_secret:
        print("❌ Ключи API не найдены в .env или cfg.json!")
        return

    ws = PhemexPrivateWS(api_key, api_secret)

    async def on_message(data: dict):
        # Дампим чистый сырой JSON с отступами, чтобы было удобно читать
        raw_json = json.dumps(data, ensure_ascii=False)
        logger.info(raw_json)

    print("=======================================================")
    print("🟢 ПРОСЛУШКА PHEMEX PRIVATE WS ЗАПУЩЕНА")
    print(f"📁 Логи пишутся в файл: {log_file}")
    print("=======================================================")
    print("ТВОИ ДЕЙСТВИЯ НА БИРЖЕ:")
    print("1. Поставь лимитку далеко от рынка (не налитую).")
    print("2. Отмени эту лимитку.")
    print("3. Поставь лимитку так, чтобы налило ЧАСТИЧНО (если получится), затем отмени остаток.")
    print("4. Открой позицию рынком (Market).")
    print("5. Закрой половину позиции.")
    print("6. Закрой остаток позиции.")
    print("=======================================================\n")

    await ws.run(on_message)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Прослушка остановлена.")