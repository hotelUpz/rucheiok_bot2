# ============================================================
# FILE: CORE/executor.py
# ROLE: Шаблоны отправки ордеров на биржу, защита от гонок потоков...
# ============================================================
from __future__ import annotations

# import asyncio
# import time
import pytz
from typing import Dict, Any, TYPE_CHECKING, Optional
from c_log import UnifiedLogger
from decimal import Decimal, ROUND_DOWN
from consts import TIME_ZONE


logger = UnifiedLogger("bot")

from dotenv import load_dotenv    

load_dotenv()


logger = UnifiedLogger(name="bot")
TZ = pytz.timezone(TIME_ZONE)


def round_step(value: float, step: float) -> float:
    if not step or step <= 0:
        return value
    val_d = Decimal(str(value))
    step_d = Decimal(str(step))
    rounded = (val_d / step_d).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_d
    return float(rounded)


class OrderExecutor:
    """
    Прокладка между оркестратором и сетевым адаптером. Содержит шаблоны рутинных операций перед финальной отправкой запросов.
    """

    async def execute_cancel(symbol, pos_key, order_id) -> bool:
        """
        Роль: некая промежуточная прокладка между сетевым адаптером и инициирующей стороной.
        Ошибку неудачи отмены логируем (можно поднимать в тг, потом закоментирую если будет мулять)                
        """

    async def interf_bought(symbol, pos_key, qty, order_price) -> Optional[float]:
        """
        Роль: скупка маленьких ордеров.
        Неудачи логируем. Допустимые остатки для скупки переосмысливаются в оркестраторе.    
        Возврат -- фактически исполненное количество ордера float (чекаем после некой минимальной паузы sleep -- interference.interference_timeout_sec) | None     
        """

    async def execute_entry(symbol, pos_key, signal) -> bool: # можно добавить горячую ссылку на ActivePosition стату. (читать через локер) (либо прокинуть в класс через __init__)
        """
        1. Расчитывает количество ордера и цену постатовки лимитного ордера, согласно рискам и спецификации биржи.
        2. Ставит ордер через await. Дожидается ответа в течение entry_timeout_sec (тупо через asyncio.sleep)
        3. Интерпретируем Лимитный ордер.
            Если успех то А.):
                - Логирует результат, поднимает событие для отправки в тг.
                - (Не мутирует ActivePosition, это прерогатива ws_interpreter.py)
                - читаем данные из ActivePosition, если там поднят флаг частичного исполнения то отменяем остаток. Если нет, то нет.
                - после успешного ответа постановки лимитного ордера и выдержки sleep(entry_timeout_sec), отменяем остаток позиции по ордер id через await (да, именно так!).
                - Возвращаем True.
            Если неуспех Б.):
                - Ошибку логируем и поднимаем в тг (просто номер ошибки, можно и текст.). Саму монету заносим в отдельный карантин -- см настройки: entry.quarantine.
                - делаем ретрай повторную попытку постановки лимитного ордера в количестве entry.max_place_order_retries раз. (на практике будет всего одна попытка). (если max_place_order_retries = 1 то это на все попытки, включая первую, основную).
                - Возвращаем False.
                
        """

    async def execute_exit(symbol, pos_key, order_price, timeout_sec) -> bool: # можно добавить горячую ссылку на ActivePosition стату. (читать через локер) (либо прокинуть в класс через __init__)
        """
        1. Берет количество ордера. Цена постатовки лимитного ордера - order_price (детерминируем выше). Все округляем согласно спецификациям биржи.
        2. Ставит ордер через await. Дожидается ответа в течение timeout_sec (параметр детерминируем в орке) (тупо через asyncio.sleep)
        3. Интерпретируем Лимитный ордер.
            Если успех то А.):
                - Логирует результат, поднимает событие для отправки в тг.
                - (Не мутирует ActivePosition, это прерогатива ws_interpreter.py)
                - читаем данные из ActivePosition, если там поднят флаг частичного исполнения то отменяем остаток. Если нет, то нет.
                - после успешного ответа постановки лимитного ордера и выдержки sleep(entry_timeout_sec), отменяем остаток позиции по ордер id через await (да, именно так!).
                - Возвращаем True.
            Если неуспех Б.):
                - Ошибку логируем и поднимаем в тг (просто номер ошибки, можно и текст.).
                - делаем ретрай повторную попытку постановки лимитного ордера в количестве entry.max_place_order_retries раз. (на практике будет 1-2 попытки). (если max_place_order_retries = 1 то это на все попытки, включая первую, основную).
                - Возвращаем False.
                
        """
# как видишь, все стало просто как в автомате Калашникова.
# Смотри некий пример постановки ордеров из другого бота:

# def round_step(value: float, step: float) -> float:
#     if not step or step <= 0:
#         return value
#     val_d = Decimal(str(value))
#     step_d = Decimal(str(step))
#     rounded = (val_d / step_d).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_d
#     return float(rounded)


# class OpenInterestDefender:
#     def __init__(self, config: dict):
#         self.config = config

#         self.pos_mode = "hedged"
#         self.api_key = os.getenv("api_key") or ""
#         self.api_secret = os.getenv("api_secret") or ""

#         self.pos_side = self.config.get("pos_side", "").upper() or "LONG"
#         self.leverage = self.config.get("leverage", 10)
#         self.margin_amount = self.config.get("margin_amount", 3.5)
#         self.indentation_pct = self.config.get("order_indentation_pct", 25)

#         self.client = PhemexPrivateClient(self.api_key, self.api_secret)

#     async def is_oil(self, symbol: str, price: float, sym_info: "SymbolInfo"):
#         if not price:
#             logger.warning(f"[{symbol}] Пропуск: нет цены.")
#             return "ERR_NO_PRICE"

#         tick_size = sym_info.tick_size
#         lot_size = sym_info.lot_size

#         if tick_size is None or lot_size is None:
#             logger.warning(
#                 f"[{symbol}] Пропуск: отсутствует спецификация "
#                 f"tick_size={tick_size}, lot_size={lot_size}"
#             )
#             return "ERR_NO_SPEC"

#         if self.pos_side == "LONG":
#             order_price = price * (1 - self.indentation_pct / 100.0)
#             side = "Buy"
#         else:
#             order_price = price * (1 + self.indentation_pct / 100.0)
#             side = "Sell"
            
#         phemex_pos_side = self.pos_side.capitalize()
#         order_price = round_step(order_price, tick_size)

#         if order_price <= 0:
#             logger.warning(f"[{symbol}] Пропуск: некорректная цена ордера {order_price}")
#             return "ERR_BAD_PRICE"

#         notional_value = self.margin_amount * self.leverage
#         actual_notional = max(6.0, notional_value)

#         qty = max(lot_size, actual_notional / order_price)
#         qty = round_step(qty, lot_size)

#         if qty <= 0:
#             logger.warning(f"[{symbol}] Пропуск: некорректный qty {qty}")
#             return "ERR_BAD_QTY"

#         logger.debug(f"[{symbol}] OIL Подготовка ордера: side={side}, pos_side={phemex_pos_side}, qty={qty}, price={order_price}")

#         try:
#             resp = await self.client.place_order(symbol, side, qty, order_price, phemex_pos_side)
#             code = resp.get("code", -1)

#             if code == 0:
#                 data_dict = resp.get("data") or {}
#                 order_id = data_dict.get("orderID") or data_dict.get("orderId")

#                 if order_id:
#                     cancel_resp = await self.client.cancel_order(symbol, order_id, phemex_pos_side)
#                     if cancel_resp.get("code", -1) == 0:
#                         logger.info(f"[{symbol}] 🗑️ Ордер {order_id} моментально отменен.")
#                     else:
#                         logger.error(f"[{symbol}] ❌ ОШИБКА ОТМЕНЫ ОРДЕРА! {cancel_resp}")
#                 else:
#                     logger.error(f"[{symbol}] code=0, но order_id не найден в ответе: {resp}")

#                 logger.info(f"[{symbol}] OIL: False.")
#                 return False

#             if code == 11150:
#                 logger.info(f"[{symbol}] OIL: True.")
#                 return True

#             return f"ERR_{code}"

#         except asyncio.TimeoutError:
#             logger.warning(f"[{symbol}] Таймаут ожидания ответа от биржи!")
#             return "ERR_TIMEOUT"

#         except Exception as e:
#             logger.error(f"[{symbol}] Исключение при отправке/отмене: {e}")
#             return "ERR_EXCEPTION"