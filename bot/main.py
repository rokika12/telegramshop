import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from dotenv import load_dotenv

from bot.database import Store
from bot.handlers import admin_router, notify_expired_orders, poll_khqr_payments, user_router
from bot.payments import DEFAULT_GATEWAY_URL, DEFAULT_PROFILE_ID

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ACCOUNT_DATA_KEY = os.getenv("ACCOUNT_DATA_KEY", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot_data.sqlite3").strip()
KHQRCC_PROFILE_ID = os.getenv("KHQRCC_PROFILE_ID", DEFAULT_PROFILE_ID).strip()
KHQRCC_SECRET_KEY = os.getenv("KHQRCC_SECRET_KEY", "").strip()
PAYMENTS_ENABLED = os.getenv("PAYMENTS_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
_ADMIN_CONTACT_USER_ID = os.getenv("ADMIN_CONTACT_USER_ID", "").strip()
try:
    ADMIN_CONTACT_USER_ID = int(_ADMIN_CONTACT_USER_ID) if _ADMIN_CONTACT_USER_ID else None
except ValueError as error:
    raise RuntimeError("ADMIN_CONTACT_USER_ID must be a numeric Telegram user ID.") from error
if ADMIN_CONTACT_USER_ID is not None and ADMIN_CONTACT_USER_ID <= 0:
    raise RuntimeError("ADMIN_CONTACT_USER_ID must be a positive Telegram user ID.")
KHQRCC_GATEWAY_URL = os.getenv("KHQRCC_GATEWAY_URL", DEFAULT_GATEWAY_URL).strip()
KHQRCC_SUCCESS_URL = os.getenv(
    "KHQRCC_SUCCESS_URL",
    "https://t.me/rovistarshopBot",
).strip()
KHQRCC_VERIFY_URL = os.getenv(
    "KHQRCC_VERIFY_URL",
    f"https://khqr.cc/api/{KHQRCC_PROFILE_ID}/payment-gateway/v1/payments/check-transv2-khqrcc",
).strip()


def _load_admin_ids() -> frozenset[int]:
    values = os.getenv("ADMIN_USER_IDS", "")
    try:
        admin_ids = frozenset(int(value.strip()) for value in values.split(",") if value.strip())
    except ValueError as error:
        raise RuntimeError("ADMIN_USER_IDS must be comma-separated numeric Telegram IDs.") from error
    if not admin_ids:
        raise RuntimeError("Set at least one numeric Telegram user ID in ADMIN_USER_IDS.")
    return admin_ids


async def _order_expiry_loop(bot: Bot, store: Store) -> None:
    while True:
        await asyncio.sleep(15)
        try:
            await notify_expired_orders(bot, store)
        except Exception:
            logging.exception("Order expiration check failed")


async def _khqr_poll_loop(
    bot: Bot,
    store: Store,
    payment_config: dict[str, str],
    admin_ids: frozenset[int],
) -> None:
    while True:
        try:
            await poll_khqr_payments(bot, store, payment_config, admin_ids)
        except Exception:
            logging.exception("KHQR payment verification failed")
        await asyncio.sleep(3)


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env before starting the bot.")
    if not ACCOUNT_DATA_KEY:
        raise RuntimeError(
            "Set ACCOUNT_DATA_KEY in .env. Generate one with "
            "Fernet.generate_key() as described in README.md."
        )

    admin_ids = _load_admin_ids()
    if not PAYMENTS_ENABLED:
        logging.warning("KHQR checkout is disabled because the Direct QR API is not reachable from this machine.")
    elif not KHQRCC_SECRET_KEY or KHQRCC_SECRET_KEY.lower().startswith(("your_", "replace_", "placeholder")):
        logging.warning("KHQRCC_SECRET_KEY is not configured; checkout buttons will not create payment orders.")
    store = Store(Path(DATABASE_PATH), ACCOUNT_DATA_KEY)
    await store.initialize()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(
        storage=MemoryStorage(),
        events_isolation=SimpleEventIsolation(),
    )
    dispatcher["store"] = store
    dispatcher["admin_ids"] = admin_ids
    dispatcher["admin_contact_user_id"] = ADMIN_CONTACT_USER_ID
    payment_config = {
        "profile_id": KHQRCC_PROFILE_ID,
        "secret_key": KHQRCC_SECRET_KEY,
        "gateway_url": KHQRCC_GATEWAY_URL,
        "success_url": KHQRCC_SUCCESS_URL,
        "verify_url": KHQRCC_VERIFY_URL,
        "enabled": "true" if PAYMENTS_ENABLED else "false",
    }
    dispatcher["payment_config"] = payment_config
    dispatcher.include_router(admin_router)
    dispatcher.include_router(user_router)

    expiry_task = asyncio.create_task(_order_expiry_loop(bot, store))
    payment_task = asyncio.create_task(_khqr_poll_loop(bot, store, payment_config, admin_ids))
    try:
        await dispatcher.start_polling(bot)
    finally:
        expiry_task.cancel()
        payment_task.cancel()
        await asyncio.gather(expiry_task, payment_task, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
