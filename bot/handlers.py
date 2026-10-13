import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp
from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InputMediaPhoto, Message, ReplyKeyboardRemove

from bot.database import ORDER_RESERVATION_MINUTES, Store
from bot.keyboards import (
    CUSTOM_EMOJI_IDS,
    ADMIN_ADD_PRODUCT,
    ADMIN_BACK,
    ADMIN_CANCEL,
    ADMIN_DESCRIPTION_SKIP,
    ADMIN_CREDENTIAL_SKIP,
    ADMIN_DELIVERY_SKIP,
    ADMIN_DELETE_PRODUCT,
    ADMIN_MANAGE_ACCOUNTS,
    ADMIN_PENDING_ORDERS,
    ADMIN_PHOTOS_DONE,
    ADMIN_KEY_MEDIA_DONE,
    ADMIN_KEY_OPTION_ADD,
    ADMIN_KEY_OPTIONS_DONE,
    ADMIN_KEY_STOCK_DONE,
    ADMIN_PUBLISH,
    MENU_ADMIN,
    MENU_CATALOG,
    MENU_GUIDE,
    MENU_ORDERS,
    MENU_PROFILE,
    MENU_WALLET,
    MENU_SUPPORT,
    admin_hide_confirmation,
    admin_account_actions,
    admin_accounts_keyboard,
    admin_contact_keyboard,
    admin_key_catalog_keyboard,
    admin_menu,
    admin_products_keyboard,
    button,
    confirm_product,
    credential_skip,
    delivery_skip,
    payment_qr_keyboard,
    description_skip,
    games_keyboard,
    key_catalog_keyboard,
    key_item_options_keyboard,
    key_item_stock_announcement,
    key_media_done,
    key_option_decisions,
    key_stock_done,
    main_menu,
    pending_order_actions,
    pending_orders_list,
    photo_done,
    payment_method_keyboard,
    product_actions,
    products_keyboard,
    stock_announcement,
    wallet_custom_amount_keyboard,
    wallet_topup_keyboard,
    wallet_topup_payment_keyboard,
)
from bot.payments import (
    PAYMENT_CHECK_WINDOW_SECONDS,
    download_direct_qr_image,
    render_payment_card_png,
    request_direct_qr,
    verify_transaction,
)
from bot.text_format import html_label

logger = logging.getLogger(__name__)
user_router = Router(name="user")
admin_router = Router(name="admin")
reported_amount_mismatch_orders: set[int] = set()
ADMIN_CONTACT_USERNAME = "hongzeze6"
sensitive_message_cleanup_tasks: set[asyncio.Task] = set()


class AdminProductForm(StatesGroup):
    game = State()
    level = State()
    photos = State()
    name = State()
    description = State()
    price = State()
    email = State()
    password = State()
    license_key = State()
    delivery = State()
    confirm = State()


class KeyCatalogForm(StatesGroup):
    name = State()
    media = State()
    description = State()
    option_label = State()
    option_price = State()
    stock = State()
    option_decision = State()
    confirm = State()


class AdminCredentialForm(StatesGroup):
    email = State()
    password = State()


class WalletTopupForm(StatesGroup):
    amount = State()


def _is_admin(user_id: int, admin_ids: frozenset[int]) -> bool:
    return user_id in admin_ids


def _price(price_cents: int) -> str:
    return f"${price_cents / 100:.2f}"


def _parse_wallet_amount(text: str) -> int | None:
    value = text.strip()
    if value.upper().endswith("USD"):
        value = value[:-3].strip()
    if value.startswith("$"):
        value = value[1:].strip()
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")):
            return None
        cents = int(amount * 100)
    except (InvalidOperation, ValueError, OverflowError):
        return None
    return cents if 100 <= cents <= 100_000 else None


def _cambodia_time(timestamp: str) -> str:
    expires_at = datetime.fromisoformat(timestamp).astimezone(timezone(timedelta(hours=7)))
    return expires_at.strftime("%H:%M:%S")


def _short_email(email: str) -> str:
    local, separator, domain = email.partition("@")
    if not separator or len(local) < 3:
        return "***"
    return f"{local[:2]}***@{domain}"


def _html_field(value: str, value_html: str | None) -> str:
    return html_label(value, value_html)


def _custom_emoji(name: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{CUSTOM_EMOJI_IDS[name]}">{fallback}</tg-emoji>'


def _profile_text(full_name: str, username: str | None, user_id: int) -> str:
    display_name = escape(full_name)
    display_username = f"@{escape(username)}" if username else "មិនបានកំណត់"
    return (
        f"{_custom_emoji('profile_mail', '📬')} <b>គណនី Telegram</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{_custom_emoji('profile_water', '💦')} ឈ្មោះ: <b>{display_name}</b>\n"
        f"{_custom_emoji('profile_wallet', '👛')} Username: <code>{display_username}</code>\n"
        f"{_custom_emoji('profile_money', '💰')} ID: <code>{user_id}</code>\n"
        "━━━━━━━━━━━━━━━━━━"
    )


def _welcome_text() -> str:
    # Telegram's tg-emoji HTML tag requires a valid fallback emoji inside the tag.
    return (
        f"<tg-emoji emoji-id=\"{CUSTOM_EMOJI_IDS['welcome']}\">👋</tg-emoji> "
        "<b>សូមស្វាគមន៍មកកាន់ ROKIRA</b>\n"
        "សេវាកម្មទិញផលិតផលហ្គេម និង Key ឌីជីថល ដែលមានព័ត៌មានច្បាស់លាស់។\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>មុខងាររបស់ហាង</b>\n"
        "├ បញ្ជាទិញផលិតផលហ្គេម និង Key ឌីជីថល\n"
        "├ មើលប្រវត្តិបញ្ជាទិញ\n"
        "└ សួរជំនួយពី admin\n\n"
        f"<tg-emoji emoji-id=\"{CUSTOM_EMOJI_IDS['chat']}\">💬</tg-emoji> "
        "សូមជ្រើសរើសមុខងារពីម៉ឺនុយខាងក្រោម។"
    )


async def _send_product_card(
    bot: Bot,
    chat_id: int,
    store: Store,
    product_id: int,
) -> None:
    product = await store.available_product(product_id)
    if product is None:
        await bot.send_message(chat_id, "សូមអភ័យទោស ទំនិញនេះត្រូវបានកក់ ឬលក់រួចហើយ។")
        return

    available = product["status"] == "available"
    description_block = (
        f"{_html_field(product.get('description', ''), product.get('description_html'))}\n"
        if product.get("description")
        else ""
    )
    details = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{_custom_emoji('product', '🎁')} <b>ព័ត៌មានផលិតផល</b>\n"
        f"├ ឈ្មោះ: <b>{_html_field(product['name'], product.get('name_html'))}</b>\n"
        f"├ Game: <b>{_html_field(product['game_name'], product.get('game_name_html'))}</b>\n"
        f"├ Level: <b>{_html_field(product['level_name'], product.get('level_name_html'))}</b>\n"
        f"├ តម្លៃ: <b>{_price(product['price_cents'])}</b>\n"
        f"{description_block}"
        f"└ ស្ថានភាព: <b>{'មានក្នុង stock' if available else 'បានលក់រួច' if product['status'] == 'sold' else 'កំពុងកក់'}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Key, ព័ត៌មានចូល ឬឯកសារ/link នឹងផ្ញើជូនក្រោយ Verify ការបង់ប្រាក់ជោគជ័យ។"
    )
    markup = product_actions(product["id"], product["status"])
    photo_file_ids = product["photo_file_ids"]
    if len(photo_file_ids) == 1:
        await bot.send_photo(
            chat_id=chat_id,
            photo=photo_file_ids[0],
            caption=details,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
        return

    for offset in range(0, len(photo_file_ids), 10):
        chunk = photo_file_ids[offset : offset + 10]
        if len(chunk) == 1:
            await bot.send_photo(chat_id=chat_id, photo=chunk[0])
        else:
            await bot.send_media_group(
                chat_id=chat_id,
                media=[InputMediaPhoto(media=file_id) for file_id in chunk],
            )
    await bot.send_message(chat_id, details, parse_mode=ParseMode.HTML, reply_markup=markup)


async def _broadcast_new_product(bot: Bot, store: Store, product_id: int) -> tuple[int, int]:
    product = await store.available_product(product_id)
    if product is None:
        return 0, 0

    text = (
        f"<tg-emoji emoji-id=\"{CUSTOM_EMOJI_IDS['new_stock']}\">🎉</tg-emoji> "
        "<b>មានផលិតផលថ្មីចូល stock</b>\n"
        f"Game: <b>{_html_field(product['game_name'], product.get('game_name_html'))}</b>\n"
        f"Level: <b>{_html_field(product['level_name'], product.get('level_name_html'))}</b>\n"
        f"ឈ្មោះ: <b>{_html_field(product['name'], product.get('name_html'))}</b>\n"
        f"តម្លៃ: <b>{_price(product['price_cents'])}</b>"
        + (
            f"\n\n{_html_field(product['description'], product.get('description_html'))}"
            if product.get("description")
            else ""
        )
    )
    user_ids = await store.user_ids()
    sent = 0
    failed = 0
    for user_id in user_ids:
        try:
            await bot.send_photo(
                chat_id=user_id,
                photo=product["photo_file_ids"][0],
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=stock_announcement(product_id),
            )
            sent += 1
        except TelegramAPIError as error:
            failed += 1
            logger.info("Stock announcement not delivered to user %s: %s", user_id, error)
        await asyncio.sleep(0.04)
    return sent, failed


async def _broadcast_new_key_item(
    bot: Bot,
    store: Store,
    item_id: int,
    admin_ids: frozenset[int],
) -> tuple[int, int]:
    item = await store.key_catalog_item(item_id)
    if item is None:
        return 0, 0
    options = await store.key_catalog_options(item_id)
    available_options = [option for option in options if int(option["stock_count"] or 0) > 0]
    if not available_options:
        return 0, 0
    price_values = [int(option["price_cents"]) for option in available_options]
    price_text = (
        _price(min(price_values))
        if min(price_values) == max(price_values)
        else f"{_price(min(price_values))} - {_price(max(price_values))}"
    )
    text = (
        f"<tg-emoji emoji-id=\"{CUSTOM_EMOJI_IDS['new_stock']}\">🎉</tg-emoji> "
        "<b>មាន Key ថ្មីចូលស្តុក</b>\n"
        f"ឈ្មោះ: <b>{_html_field(item['name'], item.get('name_html'))}</b>\n"
        f"តម្លៃចាប់ពី: <b>{price_text}</b>\n"
        + (f"\n{_html_field(item['description'], item.get('description_html'))}" if item.get("description") else "")
    )
    media = item["media"][0]
    sender = bot.send_video if media["type"] == "video" else bot.send_photo
    user_ids = await store.user_ids()
    sent = 0
    failed = 0
    for user_id in user_ids:
        try:
            await sender(
                chat_id=user_id,
                **{"video" if media["type"] == "video" else "photo": media["file_id"]},
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=key_item_stock_announcement(item_id),
            )
            sent += 1
        except TelegramAPIError as error:
            failed += 1
            logger.info("Key stock announcement not delivered to user %s: %s", user_id, error)
        await asyncio.sleep(0.04)
    return sent, failed


async def _notify_admins(
    bot: Bot,
    admin_ids: frozenset[int],
    text: str,
    reply_markup=None,
) -> None:
    for admin_id in admin_ids:
        try:
            await bot.send_message(
                admin_id,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        except TelegramAPIError as error:
            logger.warning("Could not notify admin %s: %s", admin_id, error)


@user_router.message(CommandStart())
async def start(message: Message, store: Store, admin_ids: frozenset[int]) -> None:
    if message.from_user is None:
        return
    await store.register_user(
        message.from_user.id,
        message.from_user.first_name,
        message.from_user.username,
    )
    await message.answer(
        _welcome_text(),
        reply_markup=main_menu(include_admin=_is_admin(message.from_user.id, admin_ids)),
    )


@user_router.message(F.text == MENU_CATALOG)
async def show_games_from_menu(message: Message, store: Store) -> None:
    if message.from_user is None:
        return
    key_items = await store.key_catalog_items()
    games = await store.games_with_stock()
    if not games and not key_items:
        await message.answer("ពេលនេះមិនទាន់មានទំនិញក្នុង stock ទេ។ សូមត្រឡប់មកពិនិត្យម្ដងទៀត។")
        return
    await message.answer("សូមជ្រើសរើស Key ឬ game៖", reply_markup=key_catalog_keyboard(key_items, games))


@user_router.message(F.text == MENU_ORDERS)
async def show_orders_from_menu(message: Message, store: Store) -> None:
    if message.from_user is None:
        return
    orders = await store.order_history(message.from_user.id)
    if not orders:
        await message.answer("អ្នកមិនទាន់មានប្រវត្តិបញ្ជាទិញទេ។")
        return
    statuses = {
        "pending": "កំពុងរង់ចាំការបង់ប្រាក់",
        "paid": "បានបង់ប្រាក់",
        "cancelled": "បានបោះបង់",
        "expired": "ផុតកំណត់",
    }
    lines = ["━━━━━━━━━━━━━━━━━━━━", "<b>ការបញ្ជាទិញរបស់អ្នក</b>"]
    for order in orders:
        lines.append(
            f"#{order['id']} · {_html_field(order['name'], order.get('name_html'))} · {_price(order['price_cents'])} · "
            f"{statuses.get(order['status'], order['status'])}"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@user_router.message(F.text == MENU_PROFILE)
async def show_profile_from_menu(message: Message) -> None:
    if message.from_user is None:
        return
    await message.answer(
        _profile_text(message.from_user.full_name, message.from_user.username, message.from_user.id),
        parse_mode=ParseMode.HTML,
    )


@user_router.message(F.text == MENU_WALLET)
async def show_wallet_from_menu(message: Message, store: Store) -> None:
    if message.from_user is None:
        return
    balance = await store.wallet_balance_cents(message.from_user.id)
    await message.answer(
        f"{_custom_emoji('wallet_topup', '💰')} <b>កាបូបប្រាក់</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"សមតុល្យ: <b>{_price(balance)}</b>\n"
        f"{_custom_emoji('new_stock', '🎁')} បញ្ចូលចាប់ពី $5 ទទួលបាន Bonus 5%\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "សូមជ្រើសរើសចំនួនប្រាក់៖",
        parse_mode=ParseMode.HTML,
        reply_markup=wallet_topup_keyboard(),
    )


@user_router.callback_query(F.data == "wallet:custom")
async def request_custom_wallet_amount(query: CallbackQuery, state: FSMContext) -> None:
    if not isinstance(query.message, Message) or query.message.chat.type != ChatType.PRIVATE:
        await query.answer("សូមបញ្ចូលប្រាក់ក្នុង private chat ជាមួយ bot។", show_alert=True)
        return
    await state.clear()
    await state.set_state(WalletTopupForm.amount)
    await query.answer()
    await query.message.answer("សូមវាយចំនួនប្រាក់ជា USD ចាប់ពី $1 ដល់ $1,000 (ឧទាហរណ៍ 7.50)៖")


@user_router.message(WalletTopupForm.amount, F.text)
async def receive_custom_wallet_amount(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != ChatType.PRIVATE:
        return
    amount_cents = _parse_wallet_amount(message.text or "")
    if amount_cents is None:
        await message.answer("ចំនួនមិនត្រឹមត្រូវទេ។ សូមបញ្ចូល $1.00 ដល់ $1,000.00 ដោយមានមិនលើស 2 ខ្ទង់ទសភាគ។")
        return
    await state.clear()
    await message.answer(
        f"បញ្ជាក់ចំនួនបញ្ចូលប្រាក់: <b>{_price(amount_cents)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=wallet_custom_amount_keyboard(amount_cents),
    )


@user_router.callback_query(F.data.startswith("wallet:topup:"))
async def create_wallet_topup(
    query: CallbackQuery,
    bot: Bot,
    store: Store,
    admin_ids: frozenset[int],
    payment_config: dict[str, str],
) -> None:
    if not isinstance(query.message, Message) or query.message.chat.type != ChatType.PRIVATE:
        await query.answer("សូមបញ្ចូលប្រាក់ក្នុង private chat ជាមួយ bot។", show_alert=True)
        return
    try:
        amount_cents = int((query.data or "").rsplit(":", 1)[1])
    except ValueError:
        await query.answer("ចំនួនប្រាក់មិនត្រឹមត្រូវទេ។", show_alert=True)
        return
    if not 100 <= amount_cents <= 100_000:
        await query.answer("ចំនួនប្រាក់មិនស្ថិតក្នុងចន្លោះដែលអនុញ្ញាតទេ។", show_alert=True)
        return
    secret_key = payment_config.get("secret_key", "").strip()
    if payment_config.get("enabled", "false").lower() != "true" or not secret_key:
        await query.answer("ការបង់ KHQR មិនទាន់បើកទេ។ សូមទាក់ទង Admin។", show_alert=True)
        return

    bonus_cents = (amount_cents * 5 + 50) // 100 if amount_cents >= 500 else 0
    await store.register_user(
        query.from_user.id,
        query.from_user.first_name,
        query.from_user.username,
    )
    topup = await store.create_wallet_topup(
        query.from_user.id,
        amount_cents,
        amount_cents + bonus_cents,
    )
    transaction_id = f"ROKIRA_TOPUP_{topup['id']}_{uuid4().hex[:8]}"
    try:
        direct_qr = await request_direct_qr(
            profile_id=payment_config["profile_id"],
            secret_key=secret_key,
            transaction_id=transaction_id,
            amount_cents=amount_cents,
            success_url=payment_config["success_url"],
            remark=f"ROKIRA wallet top-up #{topup['id']}",
        )
        qr_png = (
            render_payment_card_png(direct_qr.content, amount_cents, transaction_id)
            if direct_qr.content
            else await download_direct_qr_image(direct_qr.image_url)
        )
    except (ValueError, aiohttp.ClientError, TimeoutError) as error:
        logger.warning("Could not create KHQR for wallet top-up %s: %s", topup["id"], error)
        await query.answer("មិនអាចបង្កើត KHQR បានទេ។ សូមសាកម្ដងទៀត។", show_alert=True)
        await _notify_admins(
            bot,
            admin_ids,
            f"មិនអាចបង្កើត KHQR សម្រាប់ការបញ្ចូលប្រាក់ #{topup['id']} បានទេ។",
        )
        return

    if not await store.set_wallet_topup_transaction(topup["id"], transaction_id):
        await query.answer("សំណើផុតកំណត់។ សូមសាកម្ដងទៀត។", show_alert=True)
        return
    await query.answer("បានបង្កើត QR សម្រាប់បញ្ចូលប្រាក់។")
    await query.message.answer_photo(
        photo=BufferedInputFile(qr_png, filename=f"rokira-topup-{topup['id']}.png"),
        caption=(
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"{_custom_emoji('wallet_topup', '💰')} <b>បញ្ចូលប្រាក់ទៅកាបូប</b>\n"
            f"ចំនួនបង់: <b>{_price(amount_cents)}</b>\n"
            f"Bonus: <b>{_price(bonus_cents)}</b>\n"
            f"នឹងបញ្ចូលសរុប: <b>{_price(topup['credit_cents'])}</b>\n"
            f"ផុតកំណត់: {_cambodia_time(topup['expires_at'])} (ម៉ោងកម្ពុជា)\n"
            "សមតុល្យនឹងកើនឡើងក្រោយ KHQR Verify បញ្ជាក់ការបង់ និងចំនួនប្រាក់ត្រូវគ្នាប៉ុណ្ណោះ។\n"
            "━━━━━━━━━━━━━━━━━━━━"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=wallet_topup_payment_keyboard(),
    )


async def _admin_profile_photo_id(
    bot: Bot,
    admin_ids: frozenset[int],
    username: str,
    contact_user_id: int | None = None,
) -> str | None:
    if contact_user_id is not None:
        try:
            photos = await bot.get_user_profile_photos(contact_user_id, limit=1)
        except TelegramAPIError:
            return None
        return photos.photos[0][-1].file_id if photos.photos and photos.photos[0] else None

    for admin_id in sorted(admin_ids):
        try:
            chat = await bot.get_chat(admin_id)
        except TelegramAPIError:
            continue
        if (chat.username or "").lstrip("@").casefold() != username.casefold():
            continue
        try:
            photos = await bot.get_user_profile_photos(admin_id, limit=1)
        except TelegramAPIError:
            return None
        return photos.photos[0][-1].file_id if photos.photos and photos.photos[0] else None
    return None


async def _send_admin_contact(
    bot: Bot,
    chat_id: int,
    admin_ids: frozenset[int],
    contact_user_id: int | None = None,
) -> None:
    username = ADMIN_CONTACT_USERNAME
    text = (
        f"{_custom_emoji('support', '☎️')} <b>ទាក់ទងទៅកាន់ Admin</b>\n"
        f"Username: <code>@{escape(username)}</code>\n"
        "ចុចប៊ូតុងខាងក្រោម ដើម្បីបើក private chat។"
    )
    markup = admin_contact_keyboard(username)
    photo_id = await _admin_profile_photo_id(bot, admin_ids, username, contact_user_id)
    if photo_id:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo_id,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            return
        except TelegramAPIError as error:
            logger.info("Could not send admin contact photo: %s", error)
    await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)


@user_router.message(F.text == MENU_SUPPORT)
async def show_support_from_menu(
    message: Message,
    bot: Bot,
    admin_ids: frozenset[int],
    admin_contact_user_id: int | None = None,
) -> None:
    await _send_admin_contact(bot, message.chat.id, admin_ids, admin_contact_user_id)


@user_router.message(F.text == MENU_GUIDE)
async def show_guide_from_menu(message: Message) -> None:
    await message.answer(
        "របៀបទិញ Key៖\n"
        "១. ចុច បញ្ជាទិញទំនិញ ហើយជ្រើស Key ដែលចង់បាន។\n"
        "២. មើលរូប/វីដេអូ និងពណ៌នា រួចជ្រើសប៊ូតុងកញ្ចប់និងតម្លៃ។\n"
        "៣. ជ្រើសកាត់ប្រាក់ពីកាបូប ឬបង់តាម ABA Mobile ដោយស្កេន QR។\n"
        "៤. Key ឬ file នឹងផ្ញើក្នុង chat ក្រោយប្រព័ន្ធ Verify ការបង់ប្រាក់ជោគជ័យប៉ុណ្ណោះ។"
    )


@user_router.message(F.text == MENU_ADMIN)
async def open_admin_from_menu(
    message: Message,
    state: FSMContext,
    admin_ids: frozenset[int],
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("សូមបើកផ្ទាំង admin ក្នុង private chat ជាមួយ bot។")
        return
    await state.clear()
    await message.answer("<b>ផ្ទាំងគ្រប់គ្រង ROKIRA</b>", parse_mode=ParseMode.HTML, reply_markup=admin_menu())


@user_router.callback_query(F.data == "catalog")
async def show_games(query: CallbackQuery, store: Store) -> None:
    await query.answer()
    key_items = await store.key_catalog_items()
    games = await store.games_with_stock()
    if isinstance(query.message, Message):
        if not games and not key_items:
            await query.message.answer("ពេលនេះមិនទាន់មានទំនិញក្នុង stock ទេ។ សូមត្រឡប់មកពិនិត្យម្ដងទៀត។")
            return
        await query.message.answer("សូមជ្រើសរើស Key ឬ game៖", reply_markup=key_catalog_keyboard(key_items, games))


@user_router.callback_query(F.data.startswith("keyitem:"))
async def show_key_catalog_item(query: CallbackQuery, bot: Bot, store: Store) -> None:
    if not isinstance(query.message, Message) or query.message.chat.type != ChatType.PRIVATE:
        await query.answer("សូមបើកការបញ្ជាទិញក្នុង private chat ជាមួយ bot។", show_alert=True)
        return
    try:
        item_id = int((query.data or "").partition(":")[2])
    except ValueError:
        await query.answer("ទំនិញមិនត្រឹមត្រូវទេ។", show_alert=True)
        return
    item = await store.key_catalog_item(item_id)
    if item is None:
        await query.answer("ទំនិញនេះមិនមានទៀតទេ។", show_alert=True)
        return
    await query.answer()
    for media in item["media"]:
        sender = bot.send_video if media["type"] == "video" else bot.send_photo
        await sender(query.message.chat.id, media["file_id"])
    options = await store.key_catalog_options(item_id)
    details = (
        f"<b>{_html_field(item['name'], item.get('name_html'))}</b>\n"
        + (f"{_html_field(item['description'], item.get('description_html'))}\n\n" if item.get("description") else "\n")
        + "ជ្រើសរើសកញ្ចប់ និងតម្លៃ៖"
    )
    await query.message.answer(
        details,
        parse_mode=ParseMode.HTML,
        reply_markup=key_item_options_keyboard(options),
    )


@user_router.callback_query(F.data.startswith("keyoption:"))
async def choose_key_option(query: CallbackQuery, store: Store) -> None:
    if not isinstance(query.message, Message) or query.message.chat.type != ChatType.PRIVATE:
        await query.answer("សូមបន្តក្នុង private chat ជាមួយ bot។", show_alert=True)
        return
    try:
        option_id = int((query.data or "").partition(":")[2])
    except ValueError:
        await query.answer("ជម្រើសមិនត្រឹមត្រូវទេ។", show_alert=True)
        return
    stock_product = await store.available_key_stock_product(option_id)
    if stock_product is None:
        await query.answer("ជម្រើសនេះអស់ស្តុកហើយ។", show_alert=True)
        return
    await query.answer()
    await query.message.answer(
        f"បានជ្រើស <b>{_html_field(stock_product['label'], stock_product.get('label_html'))}</b> · "
        f"<b>{_price(stock_product['price_cents'])}</b>\nសូមជ្រើសរើសវិធីបង់ប្រាក់៖",
        parse_mode=ParseMode.HTML,
        reply_markup=payment_method_keyboard(stock_product["id"]),
    )


@user_router.callback_query(F.data.startswith("game:"))
async def show_levels(query: CallbackQuery, bot: Bot, store: Store) -> None:
    if not isinstance(query.message, Message) or query.message.chat.type != ChatType.PRIVATE:
        await query.answer("សូមបើកការបញ្ជាទិញក្នុង private chat ជាមួយ bot។", show_alert=True)
        return
    await query.answer()
    try:
        game_id = int((query.data or "").partition(":")[2])
    except ValueError:
        return
    products = await store.products_for_game(game_id)
    if not products:
        await query.message.answer("Game នេះមិនមានទំនិញក្នុង stock ទេ។")
        return
    for product in products:
        await _send_product_card(bot, query.message.chat.id, store, product["id"])
        await asyncio.sleep(0.08)


@user_router.callback_query(F.data.startswith("level:"))
async def show_products(query: CallbackQuery, store: Store) -> None:
    await query.answer()
    try:
        level_id = int((query.data or "").partition(":")[2])
    except ValueError:
        return
    products = await store.products_for_level(level_id)
    if isinstance(query.message, Message):
        if not products:
            await query.message.answer("ទំនិញក្នុងប្រភេទនេះត្រូវបានលក់ ឬកក់អស់ហើយ។")
            return
        await query.message.answer("សូមជ្រើសរើសទំនិញដែលចង់មើល៖", reply_markup=products_keyboard(products))


@user_router.callback_query(F.data.startswith("product:"))
async def show_product(query: CallbackQuery, bot: Bot, store: Store) -> None:
    await query.answer()
    if not isinstance(query.message, Message):
        return
    if query.message.chat.type != ChatType.PRIVATE:
        await query.message.answer("សូមបើកការសន្ទនាផ្ទាល់ជាមួយ bot ដើម្បីមើលទំនិញ និងបញ្ជាទិញ។")
        return
    try:
        product_id = int((query.data or "").partition(":")[2])
    except ValueError:
        return
    await _send_product_card(bot, query.message.chat.id, store, product_id)


@user_router.callback_query(F.data.startswith("sold:"))
async def sold_product_notice(query: CallbackQuery) -> None:
    await query.answer("ទំនិញនេះបានលក់រួចហើយ។", show_alert=True)


@user_router.callback_query(F.data.startswith("reserved:"))
async def reserved_product_notice(query: CallbackQuery) -> None:
    await query.answer("ទំនិញនេះកំពុងត្រូវបានកក់។ សូមជ្រើសរើសទំនិញផ្សេង។", show_alert=True)


@user_router.callback_query(F.data.startswith("buy:"))
async def buy_product(query: CallbackQuery) -> None:
    if not isinstance(query.message, Message) or query.message.chat.type != ChatType.PRIVATE:
        await query.answer("សូមធ្វើការបញ្ជាទិញក្នុង private chat ជាមួយ bot។", show_alert=True)
        return
    try:
        product_id = int((query.data or "").partition(":")[2])
    except ValueError:
        await query.answer("សំណើមិនត្រឹមត្រូវ។", show_alert=True)
        return

    await query.answer()
    await query.message.answer(
        "សូមជ្រើសរើសវិធីបង់ប្រាក់៖ ABA Mobile ដោយស្កេន QR ឬកាត់ពីកាបូបប្រាក់ក្នុង bot។",
        reply_markup=payment_method_keyboard(product_id),
    )


def _payment_setup_error(payment_config: dict[str, str]) -> str | None:
    secret_key = payment_config.get("secret_key", "").strip()
    if payment_config.get("enabled", "true").lower() != "true":
        return "KHQR កំពុងផ្អាក ព្រោះ Direct QR API របស់ KHQRcc មិនអាចភ្ជាប់បាន។ សូមសាក network ដែលអាចបើក khqr.cc។"
    if not secret_key or secret_key.lower().startswith(("your_", "replace_", "placeholder")):
        return "KHQR មិនទាន់កំណត់ merchant secret ត្រឹមត្រូវទេ។ សូមទាក់ទង admin។"
    return None


@user_router.callback_query(F.data.startswith("pay:"))
async def choose_product_payment_method(
    query: CallbackQuery,
    bot: Bot,
    store: Store,
    admin_ids: frozenset[int],
    payment_config: dict[str, str],
) -> None:
    if not isinstance(query.message, Message) or query.message.chat.type != ChatType.PRIVATE:
        await query.answer("សូមបង់ប្រាក់ក្នុង private chat ជាមួយ bot។", show_alert=True)
        return
    try:
        _, raw_product_id, payment_method = (query.data or "").split(":")
        product_id = int(raw_product_id)
    except ValueError:
        await query.answer("សំណើបង់ប្រាក់មិនត្រឹមត្រូវ។", show_alert=True)
        return
    if payment_method == "khqr":
        payment_method = "aba"
    if payment_method not in {"aba", "wallet"}:
        await query.answer("វិធីបង់ប្រាក់នេះមិនមានទេ។", show_alert=True)
        return
    if payment_method == "wallet":
        paid_order = await store.pay_product_with_wallet(product_id, query.from_user.id)
        if paid_order["status"] == "unavailable":
            await query.answer("ទំនិញនេះត្រូវបានលក់ ឬកក់រួចហើយ។", show_alert=True)
            return
        if paid_order["status"] == "insufficient":
            shortfall = paid_order["price_cents"] - paid_order["balance_cents"]
            await query.answer("សមតុល្យកាបូបមិនគ្រប់គ្រាន់ទេ។", show_alert=True)
            await query.message.answer(
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"{_custom_emoji('wallet_topup', '💰')} <b>សមតុល្យមិនគ្រប់គ្រាន់</b>\n"
                f"សមតុល្យបច្ចុប្បន្ន: <b>{_price(paid_order['balance_cents'])}</b>\n"
                f"តម្លៃទំនិញ: <b>{_price(paid_order['price_cents'])}</b>\n"
                f"នៅខ្វះ: <b>{_price(shortfall)}</b>\n"
                "សូមបញ្ចូលប្រាក់ឱ្យគ្រប់ រួចត្រឡប់មកទិញម្ដងទៀត។\n"
                "━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.HTML,
                reply_markup=wallet_topup_keyboard(),
            )
            return
        await query.answer("បានកាត់ប្រាក់ពីកាបូប និងបង់ជោគជ័យ។")
        await _deliver_paid_account(
            bot,
            paid_order,
            admin_ids,
            payment_method="កាបូបប្រាក់ក្នុង bot",
        )
        return

    error = _payment_setup_error(payment_config)
    if error:
        await query.answer(error, show_alert=True)
        return
    await query.answer("កំពុងបង្កើត KHQR...")
    await _create_product_payment(
        query,
        bot,
        store,
        admin_ids,
        payment_config,
        product_id,
        payment_method,
    )


async def _create_product_payment(
    query: CallbackQuery,
    bot: Bot,
    store: Store,
    admin_ids: frozenset[int],
    payment_config: dict[str, str],
    product_id: int,
    payment_method: str,
) -> None:
    if not isinstance(query.message, Message):
        return
    secret_key = payment_config.get("secret_key", "").strip()
    order = await store.create_order(product_id, query.from_user.id)
    if order is None:
        await query.message.answer("ទំនិញនេះទើបត្រូវបានកក់ ឬលក់រួចហើយ។ សូមជ្រើសរើសទំនិញផ្សេង។")
        return

    transaction_id = f"ROKIRA_{order['id']}_{uuid4().hex[:8]}"
    success_url = payment_config["success_url"]
    remark = f"ROKIRA order {order['id']}"
    try:
        direct_qr = await request_direct_qr(
            profile_id=payment_config["profile_id"],
            secret_key=secret_key,
            transaction_id=transaction_id,
            amount_cents=order["price_cents"],
            success_url=success_url,
            remark=remark,
        )
        qr_png = (
            render_payment_card_png(direct_qr.content, order["price_cents"], transaction_id)
            if direct_qr.content
            else await download_direct_qr_image(direct_qr.image_url)
        )
    except (ValueError, aiohttp.ClientError, TimeoutError) as error:
        await store.cancel_order(order["id"])
        logger.warning("Could not create KHQR for order %s: %s", order["id"], error)
        await query.message.answer(
            "KHQRcc មិនបានផ្ញើ QR ត្រឹមត្រូវមកទេ។ Order ត្រូវបានបោះបង់ ហើយ account ត្រឡប់ទៅ stock វិញ។ សូមទាក់ទង admin។"
        )
        await _notify_admins(
            bot,
            admin_ids,
            f"មិនអាចបង្កើត KHQR សម្រាប់ order #{order['id']} បានទេ។ "
            f"មូលហេតុ: <code>{escape(str(error)[:180])}</code>",
        )
        return

    if not await store.set_payment_transaction(order["id"], transaction_id):
        await store.cancel_order(order["id"])
        await query.message.answer("Order ផុតកំណត់ពេលបង្កើត QR; account ត្រឡប់ទៅ stock វិញហើយ។ សូមសាកល្បងម្ដងទៀត។")
        return

    await query.message.answer_photo(
        photo=BufferedInputFile(qr_png, filename=f"rokira-khqr-{order['id']}.png"),
        caption=(
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{_custom_emoji('wallet', '💳')} <b>ស្កេន QR ដើម្បីបង់ប្រាក់ · Order #{order['id']}</b>\n"
        f"{_html_field(order['game_name'], order.get('game_name_html'))} · "
        f"{_html_field(order['level_name'], order.get('level_name_html'))}\n"
        f"{_html_field(order['name'], order.get('name_html'))} · <b>{_price(order['price_cents'])}</b>\n\n"
        f"កំណត់ពេលបង់៖ {ORDER_RESERVATION_MINUTES} នាទី · ផុតកំណត់ម៉ោង {_cambodia_time(order['expires_at'])} (ម៉ោងកម្ពុជា)\n"
        "ស្ថានភាព៖ មិនទាន់បានបញ្ជាក់ការបង់ប្រាក់ទេ។\n"
        f"វិធីបង់ដែលបានជ្រើស៖ <b>{'ABA Mobile' if payment_method == 'aba' else 'KHQR តាមធនាគារដែលគាំទ្រ'}</b>\n"
        "ស្កេន QR នេះពី app ធនាគារដែលបានជ្រើស។ Account នឹងផ្ញើក្រោយ Verify បញ្ជាក់ការបង់ប្រាក់ និង amount ត្រូវគ្នាប៉ុណ្ណោះ។\n"
        "━━━━━━━━━━━━━━━━━━━━"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=payment_qr_keyboard(order["id"], payment_method),
    )
    admin_text = (
        f"<b>មាន order ថ្មី #{order['id']}</b>\n"
        f"អតិថិជន: <code>{order['user_id']}</code>\n"
        f"{_html_field(order['game_name'], order.get('game_name_html'))} · "
        f"{_html_field(order['level_name'], order.get('level_name_html'))}\n"
        f"{_html_field(order['name'], order.get('name_html'))} · <b>{_price(order['price_cents'])}</b>\n"
        f"កក់ផុតកំណត់: <code>{escape(order['expires_at'])}</code>\n\n"
        f"Transaction: <code>{escape(transaction_id)}</code>\n"
        f"Payment method: {escape(payment_method)}\n"
        "Status: PENDING។ មិនទាន់បានបញ្ជាក់ថាបង់រួចទេ; Account នឹងមិនចេញរហូតដល់ Verify V2 បញ្ជាក់ payment និង amount ត្រូវគ្នា។"
    )
    await _notify_admins(bot, admin_ids, admin_text, pending_order_actions(order["id"]))


@user_router.callback_query(F.data.startswith("cancel_order:"))
async def cancel_own_order(query: CallbackQuery, bot: Bot, store: Store) -> None:
    try:
        order_id = int((query.data or "").partition(":")[2])
    except ValueError:
        await query.answer("Order មិនត្រឹមត្រូវទេ។", show_alert=True)
        return
    order = await store.cancel_order(order_id, user_id=query.from_user.id)
    if order is None:
        await query.answer("Order នេះមិនអាចបោះបង់បានទេ។", show_alert=True)
        return
    await query.answer("បានបោះបង់ការកក់")
    if isinstance(query.message, Message):
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.answer("បានបោះបង់ការកក់។ ទំនិញត្រឡប់ទៅ stock វិញហើយ។")


@user_router.callback_query(F.data == "my_orders")
async def show_my_orders(query: CallbackQuery, store: Store) -> None:
    await query.answer()
    orders = await store.order_history(query.from_user.id)
    if not isinstance(query.message, Message):
        return
    if not orders:
        await query.message.answer("អ្នកមិនទាន់មានប្រវត្តិបញ្ជាទិញទេ។")
        return
    statuses = {
        "pending": "កំពុងរង់ចាំការបង់ប្រាក់",
        "paid": "បានបង់ប្រាក់",
        "cancelled": "បានបោះបង់",
        "expired": "ផុតកំណត់",
    }
    lines = ["<b>ការបញ្ជាទិញរបស់អ្នក</b>"]
    for order in orders:
        lines.append(
            f"#{order['id']} · {_html_field(order['name'], order.get('name_html'))} · {_price(order['price_cents'])} · "
            f"{statuses.get(order['status'], order['status'])}"
        )
    await query.message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@user_router.callback_query(F.data == "profile")
async def show_profile(query: CallbackQuery) -> None:
    await query.answer()
    if isinstance(query.message, Message):
        await query.message.answer(
            _profile_text(query.from_user.full_name, query.from_user.username, query.from_user.id),
            parse_mode=ParseMode.HTML,
        )


@user_router.callback_query(F.data == "support")
async def show_support(
    query: CallbackQuery,
    admin_ids: frozenset[int],
    admin_contact_user_id: int | None = None,
) -> None:
    await query.answer()
    if isinstance(query.message, Message):
        await _send_admin_contact(query.bot, query.message.chat.id, admin_ids, admin_contact_user_id)


@user_router.callback_query(F.data == "guide")
async def show_guide(query: CallbackQuery) -> None:
    await query.answer()
    if isinstance(query.message, Message):
        await query.message.answer(
            "របៀបទិញ Key៖\n"
            "១. ចុច បញ្ជាទិញទំនិញ ហើយជ្រើសទំនិញ Key។\n"
            "២. មើលរូប/វីដេអូ និងពណ៌នា រួចជ្រើសប៊ូតុងកញ្ចប់និងតម្លៃ។\n"
            "៣. ជ្រើសកាត់ប្រាក់ពីកាបូប ឬបង់តាម ABA Mobile ដោយស្កេន QR។\n"
            "៤. Key ឬ file នឹងផ្ញើក្នុង chat ក្រោយ Verify ការបង់ប្រាក់ជោគជ័យប៉ុណ្ណោះ។"
        )


@admin_router.message(Command("admin"))
async def open_admin_panel(
    message: Message,
    state: FSMContext,
    admin_ids: frozenset[int],
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("សូមបើកផ្ទាំង admin ក្នុង private chat ជាមួយ bot។")
        return
    await state.clear()
    await message.answer("<b>ផ្ទាំងគ្រប់គ្រង ROKIRA</b>", parse_mode=ParseMode.HTML, reply_markup=admin_menu())


@admin_router.message(F.text == ADMIN_BACK)
async def return_to_customer_menu(
    message: Message,
    state: FSMContext,
    admin_ids: frozenset[int],
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    await state.clear()
    await message.answer(
        _welcome_text(),
        reply_markup=main_menu(include_admin=True),
    )


@admin_router.callback_query(F.data == "admin:home")
async def admin_home(query: CallbackQuery, state: FSMContext, admin_ids: frozenset[int]) -> None:
    if not _is_admin(query.from_user.id, admin_ids):
        await query.answer("មិនមានសិទ្ធិចូលប្រើទេ។", show_alert=True)
        return
    await state.clear()
    await query.answer()
    if isinstance(query.message, Message):
        await query.message.answer("<b>ផ្ទាំងគ្រប់គ្រង ROKIRA</b>", parse_mode=ParseMode.HTML, reply_markup=admin_menu())


def _admin_account_status(status: str) -> str:
    return {
        "available": "មានក្នុង stock",
        "reserved": "កំពុងកក់",
        "sold": "បានលក់រួច",
    }.get(status, status)


async def _send_admin_account_card(message: Message, product: dict, email_preview: str | None) -> None:
    text = (
        f"<b>គ្រប់គ្រង account #{product['id']}</b>\n"
        f"ឈ្មោះ: {_html_field(product['name'], product.get('name_html'))}\n"
        f"Game: {_html_field(product['game_name'], product.get('game_name_html'))}\n"
        f"Level: {_html_field(product['level_name'], product.get('level_name_html'))}\n"
        f"តម្លៃ: <b>{_price(product['price_cents'])}</b>\n"
        f"ស្ថានភាព: {_admin_account_status(product['status'])}\n\n"
        f"Gmail: <code>{escape(email_preview or '***')}</code>\n"
        "Password: រក្សាទុកជាសម្ងាត់\n"
        "Gmail បង្ហាញជាផ្នែកដើម្បីសម្គាល់ account; password ដើមមិនបង្ហាញក្នុង chat ទេ។ អាចប្តូរទាំងពីរបាន។"
    )
    await message.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=admin_account_actions(product["id"], product["status"]),
    )


async def _delete_sensitive_message_later(message: Message) -> None:
    await asyncio.sleep(60)
    try:
        await message.delete()
    except TelegramAPIError:
        pass


def _schedule_sensitive_message_cleanup(message: Message) -> None:
    task = asyncio.create_task(_delete_sensitive_message_later(message))
    sensitive_message_cleanup_tasks.add(task)
    task.add_done_callback(sensitive_message_cleanup_tasks.discard)


@admin_router.message(F.text == ADMIN_MANAGE_ACCOUNTS)
async def show_admin_accounts(message: Message, store: Store, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("សូមប្រើការគ្រប់គ្រង account ក្នុង private chat ជាមួយ bot។")
        return
    products = await store.admin_products()
    if not products:
        await message.answer("មិនទាន់មាន account ក្នុង database ទេ។", reply_markup=admin_menu())
        return
    await message.answer(
        "ជ្រើសរើស account ដែលចង់គ្រប់គ្រង៖",
        reply_markup=admin_accounts_keyboard(products),
    )


@admin_router.callback_query(F.data.startswith("admin:account:"))
async def show_admin_account(query: CallbackQuery, store: Store, admin_ids: frozenset[int]) -> None:
    if not _is_admin(query.from_user.id, admin_ids):
        await query.answer("មិនមានសិទ្ធិចូលប្រើទេ។", show_alert=True)
        return
    if not isinstance(query.message, Message) or query.message.chat.type != ChatType.PRIVATE:
        await query.answer("សូមប្រើការគ្រប់គ្រង account ក្នុង private chat។", show_alert=True)
        return
    try:
        product_id = int((query.data or "").rsplit(":", 1)[1])
    except ValueError:
        await query.answer("Account មិនត្រឹមត្រូវទេ។", show_alert=True)
        return
    product = next((item for item in await store.admin_products() if item["id"] == product_id), None)
    await query.answer()
    if product is None:
        await query.message.answer("រក account នេះមិនឃើញទេ។")
        return
    email_preview = await store.product_email_preview(product_id)
    await _send_admin_account_card(query.message, product, email_preview)


@admin_router.callback_query(F.data.startswith("admin:view_credentials:"))
async def reveal_admin_account_credentials(
    query: CallbackQuery,
    store: Store,
    admin_ids: frozenset[int],
) -> None:
    if not _is_admin(query.from_user.id, admin_ids):
        await query.answer("មិនមានសិទ្ធិចូលប្រើទេ។", show_alert=True)
        return
    if not isinstance(query.message, Message) or query.message.chat.type != ChatType.PRIVATE:
        await query.answer("ព័ត៌មាននេះអាចមើលបានតែក្នុង private chat របស់ admin។", show_alert=True)
        return
    try:
        product_id = int((query.data or "").rsplit(":", 1)[1])
    except ValueError:
        await query.answer("Account មិនត្រឹមត្រូវទេ។", show_alert=True)
        return

    credentials = await store.admin_product_credentials(product_id)
    await query.answer()
    if credentials is None:
        await query.message.answer("បង្ហាញ credential បានតែ account ដែលនៅមាន stock; account នេះត្រូវបានកក់ ឬលក់រួចហើយ។")
        return
    sensitive_message = await query.message.answer(
        f"<b>ព័ត៌មាន account #{product_id}</b>\n"
        f"Gmail: <code>{escape(credentials['email'])}</code>\n"
        f"Password: <code>{escape(credentials['password'])}</code>\n\n"
        "សារនេះការពារការបញ្ជូនបន្ត និងលុបដោយស្វ័យប្រវត្តិក្នុង 60 វិនាទី។",
        parse_mode=ParseMode.HTML,
        protect_content=True,
    )
    _schedule_sensitive_message_cleanup(sensitive_message)


async def _begin_credential_change(
    query: CallbackQuery,
    state: FSMContext,
    store: Store,
    admin_ids: frozenset[int],
    field: str,
) -> None:
    if not _is_admin(query.from_user.id, admin_ids):
        await query.answer("មិនមានសិទ្ធិចូលប្រើទេ។", show_alert=True)
        return
    if not isinstance(query.message, Message) or query.message.chat.type != ChatType.PRIVATE:
        await query.answer("សូមប្តូរព័ត៌មានក្នុង private chat។", show_alert=True)
        return
    try:
        product_id = int((query.data or "").rsplit(":", 1)[1])
    except ValueError:
        await query.answer("Account មិនត្រឹមត្រូវទេ។", show_alert=True)
        return
    product = next((item for item in await store.admin_products() if item["id"] == product_id), None)
    if product is None or product["status"] != "available":
        await query.answer("អាចប្តូរបានតែ account ដែលមិនទាន់លក់ ឬកំពុងកក់ទេ។", show_alert=True)
        return
    await state.clear()
    await state.update_data(credential_product_id=product_id)
    if field == "email":
        await state.set_state(AdminCredentialForm.email)
        prompt = "បញ្ចូល Gmail ថ្មី។ សារនេះនឹងត្រូវលុបចេញពី chat។"
    else:
        await state.set_state(AdminCredentialForm.password)
        prompt = "បញ្ចូល password ថ្មី។ សារនេះនឹងត្រូវលុបចេញពី chat។"
    await query.answer()
    await query.message.answer(prompt, reply_markup=ReplyKeyboardRemove())


@admin_router.callback_query(F.data.startswith("admin:edit_email:"))
async def begin_email_change(
    query: CallbackQuery,
    state: FSMContext,
    store: Store,
    admin_ids: frozenset[int],
) -> None:
    await _begin_credential_change(query, state, store, admin_ids, "email")


@admin_router.callback_query(F.data.startswith("admin:edit_password:"))
async def begin_password_change(
    query: CallbackQuery,
    state: FSMContext,
    store: Store,
    admin_ids: frozenset[int],
) -> None:
    await _begin_credential_change(query, state, store, admin_ids, "password")


@admin_router.message(AdminCredentialForm.email, F.text == ADMIN_CANCEL)
@admin_router.message(AdminCredentialForm.password, F.text == ADMIN_CANCEL)
async def cancel_credential_change(message: Message, state: FSMContext, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    await state.clear()
    await message.answer("បានបោះបង់ការប្តូរព័ត៌មាន។", reply_markup=admin_menu())


@admin_router.message(AdminCredentialForm.email, F.text)
async def receive_updated_email(
    message: Message,
    state: FSMContext,
    store: Store,
    admin_ids: frozenset[int],
) -> None:
    if (
        message.from_user is None
        or not _is_admin(message.from_user.id, admin_ids)
        or message.chat.type != ChatType.PRIVATE
    ):
        return
    email = (message.text or "").strip()
    if "@" not in email or len(email) > 254:
        await message.answer("Gmail មិនត្រឹមត្រូវទេ។ សូមបញ្ចូលម្ដងទៀត។")
        return
    data = await state.get_data()
    try:
        await message.delete()
    except TelegramAPIError:
        logger.info("Could not delete an admin credential update message")
    updated = await store.update_product_email(data.get("credential_product_id", 0), email)
    await state.clear()
    await message.answer(
        "បានប្តូរ Gmail និងរក្សាទុកដោយអ៊ិនគ្រីប។" if updated else "មិនអាចប្តូរបានទេ។ Account ត្រូវបានកក់ ឬលក់រួចហើយ។",
        reply_markup=admin_menu(),
    )


@admin_router.message(AdminCredentialForm.password, F.text)
async def receive_updated_password(
    message: Message,
    state: FSMContext,
    store: Store,
    admin_ids: frozenset[int],
) -> None:
    if (
        message.from_user is None
        or not _is_admin(message.from_user.id, admin_ids)
        or message.chat.type != ChatType.PRIVATE
    ):
        return
    password = (message.text or "").strip()
    if not password or len(password) > 200:
        await message.answer("Password មិនត្រឹមត្រូវទេ។ សូមបញ្ចូលម្ដងទៀត។")
        return
    data = await state.get_data()
    try:
        await message.delete()
    except TelegramAPIError:
        logger.info("Could not delete an admin credential update message")
    updated = await store.update_product_password(data.get("credential_product_id", 0), password)
    await state.clear()
    await message.answer(
        "បានប្តូរ password និងរក្សាទុកដោយអ៊ិនគ្រីប។" if updated else "មិនអាចប្តូរបានទេ។ Account ត្រូវបានកក់ ឬលក់រួចហើយ។",
        reply_markup=admin_menu(),
    )


@admin_router.message(F.text == ADMIN_ADD_PRODUCT)
async def begin_add_product(
    message: Message,
    state: FSMContext,
    admin_ids: frozenset[int],
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("សូមបើកផ្ទាំង admin ក្នុង private chat។")
        return
    await state.clear()
    await state.set_state(KeyCatalogForm.name)
    await message.answer("បញ្ចូលឈ្មោះទំនិញ/សេវា ឧទាហរណ៍៖ Roblox Premium", reply_markup=ReplyKeyboardRemove())


@admin_router.message(KeyCatalogForm.name, F.text)
async def receive_key_catalog_name(message: Message, state: FSMContext, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids) or message.chat.type != ChatType.PRIVATE:
        return
    name = (message.text or "").strip()
    if not 2 <= len(name) <= 80:
        await message.answer("ឈ្មោះទំនិញត្រូវមាន ២–៨០ តួអក្សរ។")
        return
    await state.update_data(
        key_name=name,
        key_name_html=(message.html_text or escape(name)).strip(),
        key_media=[],
        key_options=[],
    )
    await state.set_state(KeyCatalogForm.media)
    await message.answer(
        "ផ្ញើរូបភាព ឬវីដេអូផ្សព្វផ្សាយទំនិញ (យ៉ាងតិច ១)។ ពេលរួច សូមចុចប៊ូតុងបញ្ចប់។",
        reply_markup=key_media_done(),
    )


@admin_router.message(KeyCatalogForm.media, F.photo)
async def receive_key_catalog_photo(message: Message, state: FSMContext, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids) or message.chat.type != ChatType.PRIVATE:
        return
    data = await state.get_data()
    media: list[dict[str, str]] = data.get("key_media", [])
    media.append({"type": "photo", "file_id": message.photo[-1].file_id})
    await state.update_data(key_media=media)


@admin_router.message(KeyCatalogForm.media, F.video)
async def receive_key_catalog_video(message: Message, state: FSMContext, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids) or message.chat.type != ChatType.PRIVATE:
        return
    data = await state.get_data()
    media: list[dict[str, str]] = data.get("key_media", [])
    media.append({"type": "video", "file_id": message.video.file_id})
    await state.update_data(key_media=media)


@admin_router.message(KeyCatalogForm.media, F.text == ADMIN_KEY_MEDIA_DONE)
async def finish_key_catalog_media(message: Message, state: FSMContext, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids) or message.chat.type != ChatType.PRIVATE:
        return
    data = await state.get_data()
    if not data.get("key_media"):
        await message.answer("សូមផ្ញើយ៉ាងតិច ១ រូបភាព ឬវីដេអូសិន។")
        return
    await state.set_state(KeyCatalogForm.description)
    await message.answer(
        "សរសេរពណ៌នាផលិតផល ឬចុចរំលង។ អាចមាន bold/link និងកំណត់ត្រឹម ៦០០ តួអក្សរ។",
        reply_markup=description_skip(),
    )


async def _request_key_option_label(message: Message, state: FSMContext) -> None:
    await state.set_state(KeyCatalogForm.option_label)
    await message.answer("បញ្ចូលឈ្មោះប៊ូតុង/រយៈពេល ឧទាហរណ៍៖ 1 ថ្ងៃ ឬ 1 ខែ", reply_markup=ReplyKeyboardRemove())


@admin_router.message(KeyCatalogForm.description, F.text == ADMIN_DESCRIPTION_SKIP)
async def skip_key_catalog_description(message: Message, state: FSMContext, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    await state.update_data(key_description="", key_description_html="")
    await _request_key_option_label(message, state)


@admin_router.message(KeyCatalogForm.description, F.text)
async def receive_key_catalog_description(message: Message, state: FSMContext, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    description = (message.text or "").strip()
    if len(description.encode("utf-16-le")) // 2 > 600:
        await message.answer("ពណ៌នាវែងពេក។ សូមកំណត់ត្រឹម ៦០០ តួអក្សរ ឬចុចរំលង។")
        return
    await state.update_data(
        key_description=description,
        key_description_html=(message.html_text or escape(description)).strip(),
    )
    await _request_key_option_label(message, state)


@admin_router.message(KeyCatalogForm.option_label, F.text)
async def receive_key_option_label(message: Message, state: FSMContext, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    label = (message.text or "").strip()
    if not 1 <= len(label) <= 60:
        await message.answer("ឈ្មោះប៊ូតុងត្រូវមាន ១–៦០ តួអក្សរ។")
        return
    await state.update_data(key_option_label=label, key_option_label_html=(message.html_text or escape(label)).strip())
    await state.set_state(KeyCatalogForm.option_price)
    await message.answer("បញ្ចូលតម្លៃ USD សម្រាប់ប៊ូតុងនេះ ឧទាហរណ៍៖ 3.00", reply_markup=ReplyKeyboardRemove())


@admin_router.message(KeyCatalogForm.option_price, F.text)
async def receive_key_option_price(message: Message, state: FSMContext, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    raw_price = (message.text or "").strip().replace("$", "").replace("USD", "").strip()
    try:
        parsed_price = Decimal(raw_price)
        if not parsed_price.is_finite():
            raise InvalidOperation
        price_cents = int(parsed_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)
    except (InvalidOperation, ValueError, OverflowError):
        await message.answer("តម្លៃមិនត្រឹមត្រូវទេ។ សូមបញ្ចូល USD ឧទាហរណ៍ 3.00។")
        return
    if not 1 <= price_cents <= 100_000_000:
        await message.answer("តម្លៃត្រូវធំជាង 0 និងមិនលើស $1,000,000។")
        return
    await state.update_data(
        key_current_option={
            "label": (await state.get_data()).get("key_option_label", ""),
            "label_html": (await state.get_data()).get("key_option_label_html", ""),
            "price_cents": price_cents,
            "stock": [],
        }
    )
    await state.set_state(KeyCatalogForm.stock)
    await message.answer(
        "បញ្ចូល Key ជាសារ ឬផ្ញើ file/document មួយៗ។ អាចបញ្ចូលច្រើនរហូតដល់ចុចបញ្ចប់ស្តុកជម្រើសនេះ។\n"
        "ចំនួន Key/file ដែលបញ្ចូល គឺចំនួនស្តុករបស់ប៊ូតុងនេះ។",
        reply_markup=key_stock_done(),
    )


@admin_router.message(KeyCatalogForm.stock, F.text == ADMIN_KEY_STOCK_DONE)
async def finish_key_option_stock(message: Message, state: FSMContext, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids) or message.chat.type != ChatType.PRIVATE:
        return
    data = await state.get_data()
    option = data.get("key_current_option", {})
    if not option.get("stock"):
        await message.answer("ជម្រើសនេះមិនទាន់មាន Key ឬ file ក្នុងស្តុកទេ។")
        return
    options: list[dict] = data.get("key_options", [])
    options.append(option)
    await state.update_data(key_options=options, key_current_option={})
    await state.set_state(KeyCatalogForm.option_decision)
    await message.answer(
        f"បានបន្ថែម {len(option['stock'])} ក្នុងស្តុក សម្រាប់ {option['label']} · {_price(option['price_cents'])}។",
        reply_markup=key_option_decisions(),
    )


@admin_router.message(KeyCatalogForm.stock, F.document)
async def receive_key_stock_file(message: Message, state: FSMContext, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids) or message.chat.type != ChatType.PRIVATE:
        return
    data = await state.get_data()
    option = data.get("key_current_option", {})
    stock: list[dict[str, str]] = option.get("stock", [])
    stock.append({"delivery_file_id": message.document.file_id})
    option["stock"] = stock
    await state.update_data(key_current_option=option)
    try:
        await message.delete()
    except TelegramAPIError:
        logger.info("Could not delete an admin key delivery file message")
    await message.answer(f"បានបន្ថែម file ចូលស្តុក ({len(stock)})។ បន្តផ្ញើ ឬចុចបញ្ចប់ស្តុកជម្រើសនេះ។")


@admin_router.message(KeyCatalogForm.stock, F.text)
async def receive_key_stock_text(message: Message, state: FSMContext, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids) or message.chat.type != ChatType.PRIVATE:
        return
    key_value = (message.text or "").strip()
    if not key_value or len(key_value) > 500:
        await message.answer("Key មិនត្រឹមត្រូវទេ។ សូមបញ្ចូល Key មិនលើស ៥០០ តួអក្សរ ឬផ្ញើ file។")
        return
    data = await state.get_data()
    option = data.get("key_current_option", {})
    stock: list[dict[str, str]] = option.get("stock", [])
    stock.append({"license_key": key_value})
    option["stock"] = stock
    await state.update_data(key_current_option=option)
    try:
        await message.delete()
    except TelegramAPIError:
        logger.info("Could not delete an admin key message")
    await message.answer(f"បានបន្ថែម Key ចូលស្តុក ({len(stock)})។ បន្តផ្ញើ ឬចុចបញ្ចប់ស្តុកជម្រើសនេះ។")


@admin_router.message(KeyCatalogForm.option_decision, F.text == ADMIN_KEY_OPTION_ADD)
async def add_another_key_option(message: Message, state: FSMContext, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    if len((await state.get_data()).get("key_options", [])) >= 10:
        await message.answer("អាចបង្កើតបានច្រើនបំផុត ១០ ជម្រើសក្នុងទំនិញមួយ។")
        return
    await _request_key_option_label(message, state)


@admin_router.message(KeyCatalogForm.option_decision, F.text == ADMIN_KEY_OPTIONS_DONE)
async def review_key_catalog_item(message: Message, state: FSMContext, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    data = await state.get_data()
    option_lines = [
        f"• {_html_field(option['label'], option.get('label_html'))} · {_price(option['price_cents'])} · ស្តុក {len(option['stock'])}"
        for option in data.get("key_options", [])
    ]
    summary = (
        f"<b>{_html_field(data['key_name'], data.get('key_name_html'))}</b>\n"
        f"Media: {len(data.get('key_media', []))} (រូប/វីដេអូ)\n"
        + (f"{_html_field(data['key_description'], data.get('key_description_html'))}\n" if data.get("key_description") else "")
        + "\n<b>ប៊ូតុង និងតម្លៃ</b>\n"
        + "\n".join(option_lines)
        + "\n\nសូមពិនិត្យ រួចបញ្ជាក់ដើម្បីផ្សាយ។ Key នឹងផ្ញើក្រោយ Verify ការបង់ប្រាក់ប៉ុណ្ណោះ។"
    )
    await state.set_state(KeyCatalogForm.confirm)
    await message.answer(summary, parse_mode=ParseMode.HTML, reply_markup=confirm_product())


@admin_router.message(KeyCatalogForm.confirm, F.text == ADMIN_PUBLISH)
async def publish_key_catalog_item(
    message: Message,
    state: FSMContext,
    store: Store,
    bot: Bot,
    admin_ids: frozenset[int],
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids) or message.chat.type != ChatType.PRIVATE:
        return
    data = await state.get_data()
    try:
        item_id = await store.create_key_catalog_item(
            name=data["key_name"],
            name_html=data.get("key_name_html", ""),
            description=data.get("key_description", ""),
            description_html=data.get("key_description_html", ""),
            media=data["key_media"],
            options=data["key_options"],
            created_by=message.from_user.id,
        )
    except (KeyError, ValueError):
        await message.answer("ព័ត៌មានទំនិញមិនពេញលេញ ឬជម្រើសណាមួយគ្មានស្តុកទេ។ សូមចាប់ផ្ដើមម្ដងទៀត។")
        await state.clear()
        return
    await state.clear()
    sent, failed = await _broadcast_new_key_item(bot, store, item_id, admin_ids)
    await message.answer(
        f"បានផ្សាយទំនិញ Key #{item_id}។ ប្រកាសទៅអ្នកប្រើបាន {sent} នាក់; មិនអាចផ្ញើទៅ {failed} នាក់។",
        reply_markup=admin_menu(),
    )


@admin_router.message(KeyCatalogForm.confirm, F.text == ADMIN_CANCEL)
async def cancel_key_catalog_form(message: Message, state: FSMContext, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    await state.clear()
    await message.answer("បានបោះបង់ការបង្កើតទំនិញ។", reply_markup=admin_menu())


@admin_router.message(AdminProductForm.game, F.text)
async def receive_game(message: Message, state: FSMContext) -> None:
    game = (message.text or "").strip()
    if not 2 <= len(game) <= 40:
        await message.answer("ឈ្មោះ game ត្រូវមាន ២–៤០ តួអក្សរ។ សូមបញ្ចូលម្ដងទៀត។")
        return
    await state.update_data(game=game, game_html=(message.html_text or escape(game)).strip())
    await state.set_state(AdminProductForm.level)
    await message.answer(
        "បញ្ចូលប្រភេទផលិតផល ឬ Level។ ឧទាហរណ៍សម្រាប់ Roblox Key៖ Game Pass, VIP ឬ Robux",
        reply_markup=ReplyKeyboardRemove(),
    )


@admin_router.message(AdminProductForm.level, F.text)
async def receive_level(message: Message, state: FSMContext) -> None:
    level = (message.text or "").strip()
    if not 1 <= len(level) <= 40:
        await message.answer("Level ត្រូវមាន ១–៤០ តួអក្សរ។ សូមបញ្ចូលម្ដងទៀត។")
        return
    await state.update_data(
        level=level,
        level_html=(message.html_text or escape(level)).strip(),
        photo_file_ids=[],
    )
    await state.set_state(AdminProductForm.photos)
    await message.answer(
        "ផ្ញើរូបផលិតផលយ៉ាងតិច ១ សន្លឹក។ អាចផ្ញើជា album ឬផ្ញើម្តងមួយបាន។\n"
        "ពេលរួចរាល់ សូមចុចប៊ូតុងខាងក្រោម។",
        reply_markup=photo_done(),
    )


@admin_router.message(AdminProductForm.photos, F.photo)
async def receive_product_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    photos: list[str] = data.get("photo_file_ids", [])
    photo_id = message.photo[-1].file_id
    if photo_id not in photos:
        photos.append(photo_id)
        await state.update_data(photo_file_ids=photos)


@admin_router.message(AdminProductForm.photos, F.text == ADMIN_PHOTOS_DONE)
async def finish_product_photos(
    message: Message,
    state: FSMContext,
    admin_ids: frozenset[int],
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    data = await state.get_data()
    photo_count = len(data.get("photo_file_ids", []))
    if photo_count < 1:
        await message.answer("សូមផ្ញើយ៉ាងតិច ១ សន្លឹកសិន។")
        return
    await state.set_state(AdminProductForm.name)
    await message.answer("បញ្ចូលឈ្មោះផលិតផលដែលអ្នកទិញនឹងឃើញ។", reply_markup=ReplyKeyboardRemove())


@admin_router.message(AdminProductForm.name, F.text)
async def receive_product_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not 2 <= len(name) <= 80:
        await message.answer("ឈ្មោះ account ត្រូវមាន ២–៨០ តួអក្សរ។")
        return
    await state.update_data(name=name, name_html=(message.html_text or escape(name)).strip())
    await state.set_state(AdminProductForm.description)
    await message.answer(
        "សរសេរការពិពណ៌នាផ្សព្វផ្សាយដែលអតិថិជននឹងឃើញ។ អាចច្រើនបន្ទាត់ មាន bold, link និង custom emoji បាន។\n"
        "កំណត់ត្រឹម ៦០០ តួអក្សរ ឬចុចរំលង។",
        reply_markup=description_skip(),
    )


async def _request_product_price(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminProductForm.price)
    await message.answer("បញ្ចូលតម្លៃ USD ឧទាហរណ៍៖ 5.00", reply_markup=ReplyKeyboardRemove())


@admin_router.message(AdminProductForm.description, F.text == ADMIN_DESCRIPTION_SKIP)
async def skip_product_description(message: Message, state: FSMContext) -> None:
    await state.update_data(description="", description_html="")
    await _request_product_price(message, state)


@admin_router.message(AdminProductForm.description, F.text)
async def receive_product_description(message: Message, state: FSMContext) -> None:
    description = (message.text or "").strip()
    if len(description.encode("utf-16-le")) // 2 > 600:
        await message.answer("ការពិពណ៌នាវែងពេក។ សូមកំណត់ត្រឹម ៦០០ តួអក្សរ ឬចុចរំលង។")
        return
    await state.update_data(
        description=description,
        description_html=(message.html_text or escape(description)).strip(),
    )
    await _request_product_price(message, state)


@admin_router.message(AdminProductForm.price, F.text)
async def receive_product_price(message: Message, state: FSMContext) -> None:
    raw_price = (message.text or "").strip().replace("$", "").replace("USD", "").strip()
    try:
        parsed_price = Decimal(raw_price)
        if not parsed_price.is_finite():
            raise InvalidOperation
        amount = parsed_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        price_cents = int(amount * 100)
    except (InvalidOperation, ValueError, OverflowError):
        await message.answer("តម្លៃមិនត្រឹមត្រូវទេ។ សូមបញ្ចូលជាលេខ USD ឧទាហរណ៍ 5.00។")
        return
    if price_cents <= 0 or price_cents > 100_000_000:
        await message.answer("តម្លៃត្រូវធំជាង 0 និងមិនលើស $1,000,000។")
        return
    await state.update_data(price_cents=price_cents)
    await state.set_state(AdminProductForm.email)
    await message.answer(
        "បញ្ចូល Gmail/email សម្រាប់ account ដែលលក់ ឬចុចរំលង ប្រសិនបើមិនត្រូវការ។",
        reply_markup=credential_skip(),
    )


async def _request_product_password(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminProductForm.password)
    await message.answer(
        "បញ្ចូល password របស់ account។ សារដែលបញ្ចូលនឹងត្រូវលុបចេញពី chat ឬចុចរំលង ប្រសិនបើមិនមាន password។",
        reply_markup=credential_skip(),
    )


async def _request_product_delivery(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminProductForm.delivery)
    await message.answer(
        "ផ្ញើឯកសារដែលអតិថិជនត្រូវទាញយក ឬបញ្ចូល HTTPS link។ Bot នឹងផ្ញើវាក្រោយ Verify ការបង់ប្រាក់ជោគជ័យ។\n"
        "បើមិនមានឯកសារ ឬ link សូមចុចរំលង។",
        reply_markup=delivery_skip(),
    )


async def _request_product_key(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminProductForm.license_key)
    await message.answer(
        "បញ្ចូល Key មួយសម្រាប់ listing នេះ។ Key នឹងត្រូវអ៊ិនគ្រីប និងផ្ញើឲ្យភ្ញៀវក្រោយ Verify ការបង់ប្រាក់ជោគជ័យ។ "
        "ចុចរំលង ប្រសិនបើ listing នេះមិនមាន Key។",
        reply_markup=credential_skip(),
    )


async def _confirm_product_details(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    credentials = []
    if data.get("account_email"):
        credentials.append(f"Gmail: <code>{escape(_short_email(data['account_email']))}</code>")
    if data.get("account_password"):
        credentials.append("Password: រក្សាទុកដោយអ៊ិនគ្រីប។")
    if data.get("license_key"):
        credentials.append("Key: រក្សាទុកដោយអ៊ិនគ្រីប។")
    if not credentials:
        credentials.append("Gmail/password/Key: មិនប្រើ")
    delivery = (
        "ឯកសារផ្ញើក្រោយបង់: មានឯកសារ"
        if data.get("delivery_file_id")
        else "ឯកសារផ្ញើក្រោយបង់: មាន HTTPS link"
        if data.get("delivery_url")
        else "ឯកសារផ្ញើក្រោយបង់: មិនមាន"
    )
    summary = (
        "<b>ពិនិត្យព័ត៌មាន listing មុនដាក់ចូល stock</b>\n"
        f"Game: {_html_field(data['game'], data.get('game_html'))}\n"
        f"Level: {_html_field(data['level'], data.get('level_html'))}\n"
        f"ឈ្មោះ: {_html_field(data['name'], data.get('name_html'))}\n"
        + (
            f"\n{_html_field(data['description'], data.get('description_html'))}\n"
            if data.get("description")
            else ""
        )
        + f"តម្លៃ: <b>{_price(data['price_cents'])}</b>\n"
        + f"រូបភាព: {len(data['photo_file_ids'])} សន្លឹក\n"
        + "\n".join(credentials)
        + f"\n{delivery}\n\n"
        "បន្ទាប់ពីបញ្ជាក់ listing នឹងចូល stock។ Key, ឯកសារ/link និងព័ត៌មានចូលនឹងផ្ញើក្រោយ Verify បង់ប្រាក់ជោគជ័យ។"
    )
    await state.set_state(AdminProductForm.confirm)
    await message.answer(summary, parse_mode=ParseMode.HTML, reply_markup=confirm_product())


@admin_router.message(AdminProductForm.email, F.text == ADMIN_CREDENTIAL_SKIP)
async def skip_account_email(message: Message, state: FSMContext) -> None:
    await state.update_data(account_email="")
    await _request_product_password(message, state)


@admin_router.message(AdminProductForm.email, F.text)
async def receive_account_email(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip()
    if "@" not in email or len(email) > 254:
        await message.answer("Email មិនត្រឹមត្រូវទេ។ សូមពិនិត្យហើយបញ្ចូលម្ដងទៀត។")
        return
    await state.update_data(account_email=email)
    try:
        await message.delete()
    except TelegramAPIError:
        logger.info("Could not delete an admin credential message")
    await _request_product_password(message, state)


@admin_router.message(AdminProductForm.password, F.text == ADMIN_CREDENTIAL_SKIP)
async def skip_account_password(message: Message, state: FSMContext) -> None:
    await state.update_data(account_password="")
    await _request_product_key(message, state)


@admin_router.message(AdminProductForm.password, F.text)
async def receive_account_password(message: Message, state: FSMContext) -> None:
    password = (message.text or "").strip()
    if not password or len(password) > 200:
        await message.answer("Password មិនត្រឹមត្រូវទេ។ សូមបញ្ចូលម្ដងទៀត។")
        return
    await state.update_data(account_password=password)
    try:
        await message.delete()
    except TelegramAPIError:
        logger.info("Could not delete an admin credential message")
    await _request_product_key(message, state)


@admin_router.message(AdminProductForm.license_key, F.text == ADMIN_CREDENTIAL_SKIP)
async def skip_product_key(message: Message, state: FSMContext) -> None:
    await state.update_data(license_key="")
    await _request_product_delivery(message, state)


@admin_router.message(AdminProductForm.license_key, F.text)
async def receive_product_key(
    message: Message,
    state: FSMContext,
    admin_ids: frozenset[int],
) -> None:
    if (
        message.from_user is None
        or not _is_admin(message.from_user.id, admin_ids)
        or message.chat.type != ChatType.PRIVATE
    ):
        return
    license_key = (message.text or "").strip()
    if not license_key or len(license_key) > 500:
        await message.answer("Key មិនត្រឹមត្រូវទេ។ សូមបញ្ចូល Key មិនលើស ៥០០ តួអក្សរ ឬចុចរំលង។")
        return
    await state.update_data(license_key=license_key)
    try:
        await message.delete()
    except TelegramAPIError:
        logger.info("Could not delete an admin license key message")
    await _request_product_delivery(message, state)


@admin_router.message(AdminProductForm.delivery, F.document)
async def receive_product_delivery_file(
    message: Message,
    state: FSMContext,
    admin_ids: frozenset[int],
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    await state.update_data(delivery_file_id=message.document.file_id, delivery_url="")
    await _confirm_product_details(message, state)


@admin_router.message(AdminProductForm.delivery, F.text == ADMIN_DELIVERY_SKIP)
async def skip_product_delivery(
    message: Message,
    state: FSMContext,
    admin_ids: frozenset[int],
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    await state.update_data(delivery_file_id="", delivery_url="")
    await _confirm_product_details(message, state)


@admin_router.message(AdminProductForm.delivery, F.text)
async def receive_product_delivery_link(
    message: Message,
    state: FSMContext,
    admin_ids: frozenset[int],
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    delivery_url = (message.text or "").strip()
    try:
        parsed_url = urlsplit(delivery_url)
        valid_url = (
            parsed_url.scheme.lower() == "https"
            and bool(parsed_url.hostname)
            and parsed_url.username is None
            and parsed_url.password is None
            and len(delivery_url) <= 2048
        )
    except ValueError:
        valid_url = False
    if not valid_url:
        await message.answer("Link មិនត្រឹមត្រូវទេ។ សូមបញ្ចូល HTTPS link ដែលមានសុវត្ថិភាព ឬចុចរំលង។")
        return
    await state.update_data(delivery_file_id="", delivery_url=delivery_url)
    try:
        await message.delete()
    except TelegramAPIError:
        logger.info("Could not delete an admin delivery link message")
    await _confirm_product_details(message, state)


@admin_router.message(AdminProductForm.confirm, F.text == ADMIN_PUBLISH)
async def publish_product(
    message: Message,
    state: FSMContext,
    store: Store,
    bot: Bot,
    admin_ids: frozenset[int],
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    data = await state.get_data()
    try:
        product_id = await store.create_product(
            game=data["game"],
            level=data["level"],
            name=data["name"],
            price_cents=data["price_cents"],
            photo_file_ids=data["photo_file_ids"],
            account_email=data["account_email"],
            account_password=data["account_password"],
            license_key=data.get("license_key", ""),
            created_by=message.from_user.id,
            game_html=data.get("game_html", ""),
            level_html=data.get("level_html", ""),
            name_html=data.get("name_html", ""),
            description=data.get("description", ""),
            description_html=data.get("description_html", ""),
            delivery_file_id=data.get("delivery_file_id", ""),
            delivery_url=data.get("delivery_url", ""),
        )
    except (KeyError, ValueError):
        await message.answer("ព័ត៌មាន listing មិនពេញលេញទេ។ សូមចាប់ផ្ដើមម្ដងទៀត។")
        await state.clear()
        return

    await state.clear()
    sent, failed = await _broadcast_new_product(bot, store, product_id)
    await message.answer(
        f"បានបង្កើត listing #{product_id}។\n"
        f"ប្រកាសទៅអ្នកប្រើបាន {sent} នាក់; មិនអាចផ្ញើទៅ {failed} នាក់។",
        reply_markup=admin_menu(),
    )


@admin_router.message(AdminProductForm.confirm, F.text == ADMIN_CANCEL)
async def cancel_product_form(
    message: Message,
    state: FSMContext,
    admin_ids: frozenset[int],
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    await state.clear()
    await message.answer("បានបោះបង់។", reply_markup=admin_menu())


@admin_router.message(F.text == ADMIN_PENDING_ORDERS)
async def show_pending_orders(message: Message, store: Store, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("សូមប្រើផ្ទាំង admin ក្នុង private chat ជាមួយ bot។")
        return
    orders = await store.pending_orders()
    if not orders:
        await message.answer("មិនមាន order កំពុងរង់ចាំទេ។", reply_markup=admin_menu())
        return
    await message.answer("<b>ជ្រើសរើស order ដែលកំពុងរង់ចាំ</b>", parse_mode=ParseMode.HTML, reply_markup=pending_orders_list(orders))


@admin_router.message(F.text == ADMIN_DELETE_PRODUCT)
async def show_products_to_delete(message: Message, store: Store, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id, admin_ids):
        return
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("សូមប្រើផ្ទាំង admin ក្នុង private chat ជាមួយ bot។")
        return
    products = await store.admin_products()
    deletable = [product for product in products if product["is_listed"] and product["status"] != "reserved"]
    key_items = [item for item in await store.admin_key_catalog_items() if item["is_listed"]]
    if not deletable and not key_items:
        await message.answer("មិនមានទំនិញដែលអាចលុបបានទេ។", reply_markup=admin_menu())
        return
    await message.answer("ជ្រើសរើសទំនិញដែលចង់លាក់ពីបញ្ជីលក់៖")
    if deletable:
        await message.answer("ទំនិញចាស់៖", reply_markup=admin_products_keyboard(deletable))
    if key_items:
        await message.answer("Key និងជម្រើសតម្លៃ៖", reply_markup=admin_key_catalog_keyboard(key_items))


@admin_router.callback_query(F.data.startswith("admin:key_hide:"))
async def hide_key_catalog_listing(query: CallbackQuery, store: Store, admin_ids: frozenset[int]) -> None:
    if not _is_admin(query.from_user.id, admin_ids):
        await query.answer("មិនមានសិទ្ធិចូលប្រើទេ។", show_alert=True)
        return
    try:
        item_id = int((query.data or "").rsplit(":", 1)[1])
    except ValueError:
        await query.answer("ទំនិញមិនត្រឹមត្រូវទេ។", show_alert=True)
        return
    hidden = await store.hide_key_catalog_item(item_id)
    await query.answer(
        "បានលាក់ទំនិញ និងជម្រើសតម្លៃទាំងអស់។" if hidden else "ទំនិញកំពុងមាន order កក់ ឬត្រូវបានលាក់រួចហើយ។",
        show_alert=not hidden,
    )
    if hidden and isinstance(query.message, Message):
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.answer("បានលាក់ Key listing ពីបញ្ជីលក់។", reply_markup=admin_menu())


@admin_router.callback_query(F.data.startswith("admin:select_hide:"))
async def confirm_hide_product(query: CallbackQuery, store: Store, admin_ids: frozenset[int]) -> None:
    if not _is_admin(query.from_user.id, admin_ids):
        await query.answer("មិនមានសិទ្ធិចូលប្រើទេ។", show_alert=True)
        return
    try:
        product_id = int((query.data or "").rsplit(":", 1)[1])
    except ValueError:
        return
    product = await store.available_product(product_id)
    await query.answer()
    if product is None:
        await query.message.answer("ទំនិញនេះត្រូវបានលុបចេញ ឬមិនមានទៀតទេ។")
        return
    await query.message.answer(
        f"តើបញ្ជាក់លុប <b>{_html_field(product['name'], product.get('name_html'))}</b> ចេញពីបញ្ជីលក់ឬ?\n"
        "ប្រវត្តិ order ចាស់នឹងនៅរក្សាទុក។",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_hide_confirmation(product_id),
    )


@admin_router.callback_query(F.data.startswith("admin:hide:"))
async def hide_product(query: CallbackQuery, store: Store, admin_ids: frozenset[int]) -> None:
    if not _is_admin(query.from_user.id, admin_ids):
        await query.answer("មិនមានសិទ្ធិចូលប្រើទេ។", show_alert=True)
        return
    try:
        product_id = int((query.data or "").rsplit(":", 1)[1])
    except ValueError:
        return
    hidden = await store.hide_product(product_id)
    await query.answer("បានលុបចេញពីបញ្ជី" if hidden else "ទំនិញនេះកំពុងកក់ ឬត្រូវបានលុបរួចហើយ។", show_alert=not hidden)
    if hidden and isinstance(query.message, Message):
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.answer("បានលុបទំនិញចេញពីបញ្ជីលក់។", reply_markup=admin_menu())


@admin_router.callback_query(F.data == "admin:delete_list")
async def return_to_delete_list(query: CallbackQuery, store: Store, admin_ids: frozenset[int]) -> None:
    if not _is_admin(query.from_user.id, admin_ids):
        await query.answer("មិនមានសិទ្ធិចូលប្រើទេ។", show_alert=True)
        return
    products = await store.admin_products()
    deletable = [product for product in products if product["is_listed"] and product["status"] != "reserved"]
    key_items = [item for item in await store.admin_key_catalog_items() if item["is_listed"]]
    await query.answer()
    if isinstance(query.message, Message):
        if not deletable and not key_items:
            await query.message.answer("មិនមានទំនិញដែលអាចលុបបានទេ។", reply_markup=admin_menu())
            return
        await query.message.answer("ជ្រើសរើសទំនិញដែលចង់លាក់ពីបញ្ជីលក់៖")
        if deletable:
            await query.message.answer("ទំនិញចាស់៖", reply_markup=admin_products_keyboard(deletable))
        if key_items:
            await query.message.answer("Key និងជម្រើសតម្លៃ៖", reply_markup=admin_key_catalog_keyboard(key_items))


@admin_router.callback_query(F.data.startswith("admin:order:"))
async def show_admin_order(query: CallbackQuery, store: Store, admin_ids: frozenset[int]) -> None:
    if not _is_admin(query.from_user.id, admin_ids):
        await query.answer("មិនមានសិទ្ធិចូលប្រើទេ។", show_alert=True)
        return
    try:
        order_id = int((query.data or "").rsplit(":", 1)[1])
    except ValueError:
        return
    orders = await store.pending_orders()
    order = next((item for item in orders if item["id"] == order_id), None)
    await query.answer()
    if order is None:
        if isinstance(query.message, Message):
            await query.message.answer("Order នេះបានបញ្ចប់ ឬផុតកំណត់ហើយ។")
        return
    if isinstance(query.message, Message):
        await query.message.answer(
            f"<b>Order #{order['id']}</b>\n"
            f"អតិថិជន: <code>{order['user_id']}</code>\n"
            f"Game: {_html_field(order['game_name'], order.get('game_name_html'))} · "
            f"Level: {_html_field(order['level_name'], order.get('level_name_html'))}\n"
            f"Account: {_html_field(order['name'], order.get('name_html'))}\n"
            f"តម្លៃ: <b>{_price(order['price_cents'])}</b>\n"
            f"ផុតកំណត់: <code>{escape(order['expires_at'])}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=pending_order_actions(order_id),
        )


async def _deliver_paid_account(
    bot: Bot,
    order: dict,
    admin_ids: frozenset[int],
    payment_method: str = "ABA / KHQR ដែលបាន Verify",
) -> None:
    credential_lines = []
    if order.get("account_email"):
        credential_lines.append(f"├ Gmail: <code>{escape(order['account_email'])}</code>")
    if order.get("account_password"):
        credential_lines.append(f"└ Password: <code>{escape(order['account_password'])}</code>")
    credential_text = (
        f"{_custom_emoji('account', '👤')} <b>ព័ត៌មានចូល account</b>\n"
        + "\n".join(credential_lines)
        if credential_lines
        else ""
    )
    license_key = order.get("license_key", "")
    key_text = (
        f"{_custom_emoji('account', '🔑')} <b>Key</b>\n<code>{escape(license_key)}</code>\n"
        if license_key
        else ""
    )
    delivery_url = order.get("delivery_url", "")
    download_text = (
        f"\n{_custom_emoji('new_stock', '🎁')} ឯកសារ/Link ទាញយក: "
        f'<a href="{escape(delivery_url, quote=True)}">ចុចទីនេះ</a>\n'
        if delivery_url
        else ""
    )
    wallet_balance_text = (
        f"សមតុល្យកាបូបក្រោយបង់: <b>{_price(order['balance_cents'])}</b>\n"
        if "balance_cents" in order
        else ""
    )
    text = (
        f"{_custom_emoji('wallet', '💳')} <b>ការបង់ប្រាក់ជោគជ័យ</b>\n"
        f"<b>Order #{order['id']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{_custom_emoji('cart', '🛒')} <b>ព័ត៌មានការបញ្ជាទិញ</b>\n"
        f"├ ផលិតផល: <b>{_html_field(order['name'], order.get('name_html'))}</b>\n"
        f"├ Game: <b>{_html_field(order['game_name'], order.get('game_name_html'))}</b>\n"
        f"├ Level: <b>{_html_field(order['level_name'], order.get('level_name_html'))}</b>\n"
        f"└ តម្លៃ: <b>{_price(order['price_cents'])}</b>\n"
        f"វិធីបង់: <b>{escape(payment_method)}</b>\n"
        f"{wallet_balance_text}"
        f"━━━━━━━━━━━━━━━━━━━━\n{credential_text}{key_text}{download_text}"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "សូមរក្សាទុកព័ត៌មាន និងឯកសារនេះឱ្យបានសុវត្ថិភាព។"
    )
    try:
        await bot.send_message(order["user_id"], text, parse_mode=ParseMode.HTML)
    except TelegramAPIError as error:
        logger.warning("Could not deliver account for order %s: %s", order["id"], error)
        await _notify_admins(
            bot,
            admin_ids,
            f"ទូទាត់ order #{order['id']} ជោគជ័យ ប៉ុន្តែផ្ញើសារទៅអតិថិជនមិនបាន។\n"
            f"User ID: <code>{order['user_id']}</code>។ សូមពិនិត្យហើយទាក់ទងអតិថិជន។",
        )
        return

    delivery_file_id = order.get("delivery_file_id", "")
    if delivery_file_id:
        try:
            await bot.send_document(
                order["user_id"],
                delivery_file_id,
                caption=f"{_custom_emoji('new_stock', '🎁')} ឯកសារសម្រាប់ order #{order['id']}",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError as error:
            logger.warning("Could not deliver file for order %s: %s", order["id"], error)
            await _notify_admins(
                bot,
                admin_ids,
                f"បង់ order #{order['id']} ជោគជ័យ ប៉ុន្តែផ្ញើឯកសារមិនបាន។\n"
                f"User ID: <code>{order['user_id']}</code>។ សូមពិនិត្យ product file ហើយទាក់ទងអតិថិជន។",
            )
            return

    await _notify_admins(
        bot,
        admin_ids,
        f"ការទូទាត់ order #{order['id']} តាម {escape(payment_method)} ជោគជ័យ។ "
        "ទំនិញត្រូវបានផ្ញើទៅអតិថិជន និងសម្គាល់ថាលក់រួច។",
    )


async def poll_khqr_payments(
    bot: Bot,
    store: Store,
    payment_config: dict[str, str],
    admin_ids: frozenset[int],
) -> None:
    if payment_config.get("enabled", "true").lower() != "true":
        return
    secret_key = payment_config.get("secret_key", "").strip()
    if not secret_key or secret_key.lower().startswith(("your_", "replace_", "placeholder")):
        await asyncio.sleep(3)
        return

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        orders = await store.pending_payment_orders()
        for pending in orders:
            check = await verify_transaction(
                transaction_id=pending["payment_tx_id"],
                secret_key=secret_key,
                verify_url=payment_config["verify_url"],
                session=session,
            )
            if check.status != "success" or check.amount_cents is None:
                continue
            if check.amount_cents != pending["price_cents"]:
                logger.error("KHQR amount mismatch for order %s", pending["id"])
                if pending["id"] not in reported_amount_mismatch_orders:
                    reported_amount_mismatch_orders.add(pending["id"])
                    await _notify_admins(
                        bot,
                        admin_ids,
                        f"KHQR order #{pending['id']} មានចំនួនទឹកប្រាក់មិនត្រូវនឹងតម្លៃ order។ "
                        "មិនបានផ្ញើ account ទេ; សូមពិនិត្យ merchant dashboard។",
                    )
                continue
            paid_order = await store.complete_paid_order(
                pending["id"],
                verified_amount_cents=check.amount_cents,
            )
            if paid_order is not None:
                await _deliver_paid_account(bot, paid_order, admin_ids)

        topups = await store.pending_wallet_topups()
        for pending in topups:
            check = await verify_transaction(
                transaction_id=pending["payment_tx_id"],
                secret_key=secret_key,
                verify_url=payment_config["verify_url"],
                session=session,
            )
            if check.status != "success" or check.amount_cents is None:
                continue
            if check.amount_cents != pending["amount_cents"]:
                logger.error("KHQR amount mismatch for wallet top-up %s", pending["id"])
                continue
            credited = await store.complete_wallet_topup(
                pending["id"],
                verified_amount_cents=check.amount_cents,
            )
            if credited is None:
                continue
            try:
                username = f"@{escape(credited['username'])}" if credited.get("username") else "មិនមាន username"
                bonus_cents = credited["credit_cents"] - credited["amount_cents"]
                await bot.send_message(
                    credited["user_id"],
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"{_custom_emoji('wallet_topup', '💰')} <b>បញ្ចូលប្រាក់បានជោគជ័យ</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"សួស្តី <b>{escape(credited.get('first_name') or 'អតិថិជន')}</b>!\n"
                    f"{_custom_emoji('account', '👤')} យូសនាម: <code>{username}</code>\n"
                    f"{_custom_emoji('wallet', '💳')} ប្រាក់ដែលបានបង់: <b>{_price(credited['amount_cents'])}</b>\n"
                    f"{_custom_emoji('new_stock', '🎁')} Bonus: <b>{_price(bonus_cents)}</b>\n"
                    f"{_custom_emoji('wallet_topup', '💰')} បានបញ្ចូលកាបូប: <b>{_price(credited['credit_cents'])}</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"សមតុល្យថ្មី: <b>{_price(credited['balance_cents'])}</b>\n"
                    "សូមបើកម៉ឺនុយ <b>កាបូបប្រាក់</b> ដើម្បីពិនិត្យសមតុល្យ។\n"
                    "ការទូទាត់បានផ្ទៀងផ្ទាត់ដោយ KHQR រួចរាល់។",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramAPIError as error:
                logger.warning("Could not notify user after wallet top-up %s: %s", credited["id"], error)
            await _notify_admins(
                bot,
                admin_ids,
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"{_custom_emoji('wallet_topup', '💰')} <b>មានការបញ្ចូលប្រាក់ជោគជ័យ</b> · #{credited['id']}\n"
                f"ឈ្មោះ: <b>{escape(credited.get('first_name') or 'មិនស្គាល់')}</b>\n"
                f"Username: <code>{'@' + escape(credited['username']) if credited.get('username') else 'មិនមាន'}</code>\n"
                f"User ID: <code>{credited['user_id']}</code>\n"
                f"បានបង់: <b>{_price(credited['amount_cents'])}</b> · Bonus: <b>{_price(bonus_cents)}</b>\n"
                f"ចូលកាបូប: <b>{_price(credited['credit_cents'])}</b> · សមតុល្យ: <b>{_price(credited['balance_cents'])}</b>\n"
                "KHQR Verify V2: <b>ជោគជ័យ</b>\n"
                "━━━━━━━━━━━━━━━━━━━━",
            )


@admin_router.callback_query(F.data.startswith("admin:check:"))
async def check_order_payment(query: CallbackQuery, store: Store, payment_config: dict[str, str], admin_ids: frozenset[int]) -> None:
    if not _is_admin(query.from_user.id, admin_ids):
        await query.answer("មិនមានសិទ្ធិចូលប្រើទេ។", show_alert=True)
        return
    try:
        order_id = int((query.data or "").rsplit(":", 1)[1])
    except ValueError:
        return
    pending = next((order for order in await store.pending_payment_orders() if order["id"] == order_id), None)
    if pending is None:
        await query.answer("Order មិនទាន់មានការទូទាត់កំពុងរង់ចាំទេ។", show_alert=True)
        return
    result = await verify_transaction(
        transaction_id=pending["payment_tx_id"],
        secret_key=payment_config.get("secret_key", ""),
        verify_url=payment_config["verify_url"],
    )
    if result.status == "success" and result.amount_cents == pending["price_cents"]:
        order = await store.complete_paid_order(order_id, verified_amount_cents=result.amount_cents)
        if order is not None:
            await _deliver_paid_account(query.bot, order, admin_ids)
            await query.answer("បានផ្ទៀងផ្ទាត់ការបង់ប្រាក់ជោគជ័យ")
            return
    await query.answer("មិនទាន់ឃើញ payment ជោគជ័យទេ។", show_alert=True)


@admin_router.callback_query(F.data.startswith("admin:cancel_order:"))
async def cancel_pending_order(
    query: CallbackQuery,
    store: Store,
    bot: Bot,
    admin_ids: frozenset[int],
) -> None:
    if not _is_admin(query.from_user.id, admin_ids):
        await query.answer("មិនមានសិទ្ធិចូលប្រើទេ។", show_alert=True)
        return
    try:
        order_id = int((query.data or "").rsplit(":", 1)[1])
    except ValueError:
        return
    order = await store.cancel_order(order_id)
    if order is None:
        await query.answer("Order មិនអាចបោះបង់បានទេ។", show_alert=True)
        return
    await query.answer("បានបោះបង់ order និងដោះ stock")
    try:
        await bot.send_message(
            order["user_id"],
            f"ការបញ្ជាទិញ #{order_id} ({_html_field(order['name'], order.get('name_html'))}) ត្រូវបានបោះបង់។ Account បានត្រឡប់ទៅ stock វិញ។",
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError:
        pass
    if isinstance(query.message, Message):
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.answer(f"បានបោះបង់ order #{order_id} ហើយ។")


async def notify_expired_orders(bot: Bot, store: Store) -> None:
    expired = await store.expire_orders()
    for order in expired:
        try:
            await bot.send_message(
                order["user_id"],
                f"ការកក់ #{order['id']} ({_html_field(order['name'], order.get('name_html'))}) បានផុតកំណត់។ Account ត្រឡប់ទៅ stock វិញហើយ។",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            pass
