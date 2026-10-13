from aiogram.enums import ButtonStyle
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from bot.text_format import plain_label

# Replace each value with the sticker's custom_emoji_id to change its button icon.
CUSTOM_EMOJI_IDS = {
    "welcome": "5413554183502572090",
    "new_stock": "6267117038808870117",
    "catalog": "5445260044398524944",
    "guide": "5258328383183396223",
    "cart": "5447319442562251569",
    "wallet": "5444960062407732826",
    "add": "5258108352008823107",
    "product": "5258134813302332906",
    "account": "5316727448644103237",
    "support": "5258337316715373336",
    "chat": "5447510826304959724",
    "profile_mail": "5445163772706582819",
    "profile_water": "5444979432710242887",
    "profile_wallet": "5444903695256941915",
    "profile_money": "5444860552310457690",
    "wallet_topup": "5444860552310457690",
    "pay_aba": "6339110599179373483",
    "pay_khqr": "6032693626394382504",
}

MENU_CATALOG = "បញ្ជាទិញទំនិញ"
MENU_ORDERS = "ការបញ្ជាទិញរបស់ខ្ញុំ"
MENU_PROFILE = "គណនី"
MENU_WALLET = "បញ្ចូលប្រាក់"
MENU_SUPPORT = "ជំនួយ"
MENU_GUIDE = "វិធីប្រើប្រាស់"
MENU_ADMIN = "ផ្ទាំងគ្រប់គ្រង"
ADMIN_ADD_PRODUCT = "បន្ថែមទំនិញចូល stock"
ADMIN_MANAGE_ACCOUNTS = "គ្រប់គ្រង account"
ADMIN_DELETE_PRODUCT = "លុបទំនិញ"
ADMIN_PENDING_ORDERS = "Order កំពុងរង់ចាំ"
ADMIN_BACK = "ត្រឡប់ទៅម៉ឺនុយ"
ADMIN_PHOTOS_DONE = "បញ្ចប់ការបញ្ចូលរូប"
ADMIN_KEY_MEDIA_DONE = "បញ្ចប់ការបញ្ចូលរូប/វីដេអូ"
ADMIN_KEY_STOCK_DONE = "បញ្ចប់ស្តុកជម្រើសនេះ"
ADMIN_KEY_OPTION_ADD = "បន្ថែមជម្រើសតម្លៃ"
ADMIN_KEY_OPTIONS_DONE = "បញ្ចប់ និងផ្សាយទំនិញ"
ADMIN_DESCRIPTION_SKIP = "រំលងការពិពណ៌នា"
ADMIN_CREDENTIAL_SKIP = "រំលង"
ADMIN_DELIVERY_SKIP = "រំលងឯកសារ/Link"
ADMIN_PUBLISH = "បញ្ជាក់ និងប្រកាស stock"
ADMIN_CANCEL = "បោះបង់"


def button(
    text: str,
    callback_data: str,
    *,
    style: ButtonStyle = ButtonStyle.PRIMARY,
    icon: str | None = None,
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        style=style,
        icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get(icon) if icon else None,
    )


def reply_button(text: str, *, style: ButtonStyle = ButtonStyle.PRIMARY, icon: str | None = None) -> KeyboardButton:
    return KeyboardButton(
        text=text,
        style=style,
        icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get(icon) if icon else None,
    )


def _short_label(text: str, limit: int) -> str:
    label = plain_label(text)
    return f"{label[:limit]}{'…' if len(label) > limit else ''}"


def main_menu(*, include_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [reply_button(MENU_CATALOG, style=ButtonStyle.SUCCESS, icon="cart")],
        [
            reply_button(MENU_ORDERS, icon="product"),
            reply_button(MENU_PROFILE, icon="account"),
        ],
        [reply_button(MENU_WALLET, style=ButtonStyle.PRIMARY, icon="wallet_topup")],
        [
            reply_button(MENU_SUPPORT, style=ButtonStyle.SUCCESS, icon="support"),
            reply_button(MENU_GUIDE, icon="guide"),
        ],
    ]
    if include_admin:
        rows.append([reply_button(MENU_ADMIN, style=ButtonStyle.PRIMARY, icon="add")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="ជ្រើសរើសមុខងារ",
    )


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [reply_button(ADMIN_ADD_PRODUCT, style=ButtonStyle.SUCCESS, icon="add")],
            [reply_button(ADMIN_MANAGE_ACCOUNTS, icon="account")],
            [
                reply_button(ADMIN_DELETE_PRODUCT, style=ButtonStyle.DANGER, icon="product"),
                reply_button(ADMIN_PENDING_ORDERS, icon="cart"),
            ],
            [reply_button(ADMIN_BACK, icon="guide")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="មុខងារគ្រប់គ្រង",
    )


def games_keyboard(games: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            button(
                f"{plain_label(game['name'])} · {game['product_count']} ទំនិញ",
                f"game:{game['id']}",
                style=ButtonStyle.SUCCESS,
                icon="cart",
            )
        ]
        for game in games
    ]
    rows.append([button("ត្រឡប់ទៅម៉ឺនុយ", "menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def levels_keyboard(levels: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            button(
                f"{plain_label(level['name'])} · {level['product_count']} ទំនិញ",
                f"level:{level['id']}",
                style=ButtonStyle.SUCCESS,
                icon="account",
            )
        ]
        for level in levels
    ]
    rows.append([button("ត្រឡប់ទៅ game", "catalog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def products_keyboard(products: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            button(
                (
                    f"{_short_label(product['name'], 35)} · "
                    f"${product['price_cents'] / 100:.2f} · "
                    f"{'SOLD OUT' if product['status'] == 'sold' else 'កំពុងកក់' if product['status'] == 'reserved' else 'មានក្នុង stock'}"
                ),
                f"product:{product['id']}",
                style=ButtonStyle.SUCCESS if product["status"] == "available" else ButtonStyle.DANGER,
                icon="product",
            )
        ]
        for product in products
    ]
    level_id = products[0]["level_id"] if products else 0
    rows.append([button("ត្រឡប់ទៅ level", f"level:{level_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def key_catalog_keyboard(items: list[dict], games: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            button(
                f"{_short_label(item['name'], 32)} · {item['stock_count']} ស្តុក",
                f"keyitem:{item['id']}",
                style=ButtonStyle.SUCCESS,
                icon="product",
            )
        ]
        for item in items
    ]
    rows.extend(
        [
            button(
                f"{plain_label(game['name'])} · {game['product_count']} ទំនិញចាស់",
                f"game:{game['id']}",
                style=ButtonStyle.PRIMARY,
                icon="cart",
            )
        ]
        for game in games
    )
    rows.append([button("ត្រឡប់ទៅម៉ឺនុយ", "menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def key_item_options_keyboard(options: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for option in options:
        stock_count = int(option["stock_count"] or 0)
        label = (
            f"{_short_label(option['label'], 28)} · ${option['price_cents'] / 100:.2f} · ស្តុក {stock_count}"
            if stock_count
            else f"{_short_label(option['label'], 28)} · អស់ស្តុក"
        )
        rows.append(
            [
                button(
                    label,
                    f"keyoption:{option['id']}",
                    style=ButtonStyle.SUCCESS if stock_count else ButtonStyle.DANGER,
                    icon="cart" if stock_count else "product",
                )
            ]
        )
    rows.append([button("ត្រឡប់ទៅបញ្ជីទំនិញ", "catalog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def key_item_stock_announcement(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("មើលជម្រើស និងទិញ", f"keyitem:{item_id}", style=ButtonStyle.SUCCESS, icon="new_stock")]
        ]
    )


def product_actions(product_id: int, status: str = "available") -> InlineKeyboardMarkup:
    rows = []
    if status == "available":
        rows.append([button("ទិញឥឡូវនេះ", f"buy:{product_id}", style=ButtonStyle.SUCCESS, icon="cart")])
    elif status == "sold":
        rows.append([button("បានលក់រួចហើយ", f"sold:{product_id}", style=ButtonStyle.DANGER, icon="product")])
    else:
        rows.append([button("កំពុងរង់ចាំការបង់ប្រាក់", f"reserved:{product_id}", style=ButtonStyle.DANGER)])
    rows.append([button("ត្រឡប់ទៅជ្រើសរើស Game", "catalog")])
    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def stock_announcement(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("មើលព័ត៌មាន និងទិញឥឡូវនេះ", f"product:{product_id}", style=ButtonStyle.SUCCESS, icon="new_stock")]
        ]
    )


def payment_method_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("ABA Mobile", f"pay:{product_id}:aba", style=ButtonStyle.SUCCESS, icon="pay_aba"),
                button("កាបូបប្រាក់", f"pay:{product_id}:wallet", style=ButtonStyle.PRIMARY, icon="wallet_topup"),
            ]
        ]
    )


def payment_qr_keyboard(order_id: int, payment_method: str = "aba") -> InlineKeyboardMarkup:
    rows = []
    if payment_method == "aba":
        rows.append(
            [
                InlineKeyboardButton(
                    text="បើក ABA Mobile",
                    url="https://aba.onelink.me/oRF8",
                    style=ButtonStyle.SUCCESS,
                    icon_custom_emoji_id=CUSTOM_EMOJI_IDS["pay_aba"],
                )
            ]
        )
    rows.append([button("បោះបង់ការកក់", f"cancel_order:{order_id}", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wallet_topup_keyboard() -> InlineKeyboardMarkup:
    amounts = (100, 200, 500, 1000, 2000, 5000)
    rows = [
        [
            button(
                f"${amount // 100}",
                f"wallet:topup:{amount}",
                style=ButtonStyle.SUCCESS,
                icon="wallet_topup",
            )
            for amount in amounts[offset : offset + 3]
        ]
        for offset in range(0, len(amounts), 3)
    ]
    rows.append([button("ចំនួនផ្សេង", "wallet:custom", style=ButtonStyle.PRIMARY, icon="wallet_topup")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wallet_custom_amount_keyboard(amount_cents: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    f"បន្តបង់ ${amount_cents / 100:.2f}",
                    f"wallet:topup:{amount_cents}",
                    style=ButtonStyle.SUCCESS,
                    icon="wallet_topup",
                )
            ]
        ]
    )


def wallet_topup_payment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="បើក ABA Mobile", url="https://aba.onelink.me/oRF8", style=ButtonStyle.SUCCESS)]
        ]
    )


def admin_contact_keyboard(username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="ចូលឆាតជាមួយ Admin",
                    url=f"https://t.me/{username.lstrip('@')}",
                    style=ButtonStyle.DANGER,
                    icon_custom_emoji_id=CUSTOM_EMOJI_IDS["support"],
                )
            ]
        ]
    )


def photo_done() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[reply_button("បញ្ចប់ការបញ្ចូលរូប", style=ButtonStyle.SUCCESS, icon="guide")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def key_media_done() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[reply_button(ADMIN_KEY_MEDIA_DONE, style=ButtonStyle.SUCCESS, icon="guide")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def key_stock_done() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[reply_button(ADMIN_KEY_STOCK_DONE, style=ButtonStyle.SUCCESS, icon="guide")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def key_option_decisions() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [reply_button(ADMIN_KEY_OPTION_ADD, style=ButtonStyle.PRIMARY, icon="add")],
            [reply_button(ADMIN_KEY_OPTIONS_DONE, style=ButtonStyle.SUCCESS, icon="new_stock")],
            [reply_button(ADMIN_CANCEL, style=ButtonStyle.DANGER)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def description_skip() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[reply_button(ADMIN_DESCRIPTION_SKIP, icon="guide")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def credential_skip() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[reply_button(ADMIN_CREDENTIAL_SKIP, style=ButtonStyle.PRIMARY)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def delivery_skip() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[reply_button(ADMIN_DELIVERY_SKIP, style=ButtonStyle.PRIMARY)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def confirm_product() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [reply_button("បញ្ជាក់ និងប្រកាស stock", style=ButtonStyle.SUCCESS, icon="new_stock")],
            [reply_button("បោះបង់", style=ButtonStyle.DANGER)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_products_keyboard(products: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            button(
                f"លុប · #{product['id']} · {_short_label(product['name'], 30)}",
                f"admin:select_hide:{product['id']}",
                style=ButtonStyle.DANGER,
                icon="product",
            )
        ]
        for product in products
        if product["is_listed"] and product["status"] != "reserved"
    ]
    rows.append([button("ត្រឡប់ទៅ admin menu", "admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_key_catalog_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            button(
                f"លាក់ · #{item['id']} · {_short_label(item['name'], 30)}",
                f"admin:key_hide:{item['id']}",
                style=ButtonStyle.DANGER,
                icon="product",
            )
        ]
        for item in items
        if item["is_listed"]
    ]
    rows.append([button("ត្រឡប់ទៅផ្ទាំង admin", "admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_accounts_keyboard(products: list[dict]) -> InlineKeyboardMarkup:
    status_labels = {
        "available": "មានក្នុង stock",
        "reserved": "កំពុងកក់",
        "sold": "បានលក់",
    }
    rows = [
        [
            button(
                f"#{product['id']} · {_short_label(product['name'], 30)} · "
                f"${product['price_cents'] / 100:.2f} · "
                f"{status_labels.get(product['status'], product['status'])}",
                f"admin:account:{product['id']}",
                icon="account",
            )
        ]
        for product in products
    ]
    rows.append([button("ត្រឡប់ទៅផ្ទាំង admin", "admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_account_actions(product_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status == "available":
        rows.extend(
            [
                [button("មើល Gmail / password", f"admin:view_credentials:{product_id}", icon="account")],
                [button("ប្តូរ Gmail", f"admin:edit_email:{product_id}", icon="account")],
                [button("ប្តូរ password", f"admin:edit_password:{product_id}", icon="wallet")],
            ]
        )
    rows.append([button("ត្រឡប់ទៅបញ្ជី account", "admin:manage_accounts")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_hide_confirmation(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("បញ្ជាក់លុបចេញពីបញ្ជី", f"admin:hide:{product_id}", style=ButtonStyle.DANGER)],
            [button("បោះបង់", "admin:delete_list")],
        ]
    )


def pending_order_actions(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("ពិនិត្យស្ថានភាព KHQR ម្ដងទៀត", f"admin:check:{order_id}", style=ButtonStyle.SUCCESS, icon="wallet")],
            [button("បដិសេធ និងដោះ stock", f"admin:cancel_order:{order_id}", style=ButtonStyle.DANGER)],
        ]
    )


def pending_orders_list(orders: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            button(
                f"Order #{order['id']} · {order['name']} · ${order['price_cents'] / 100:.2f}",
                f"admin:order:{order['id']}",
                icon="product",
            )
        ]
        for order in orders
    ]
    rows.append([button("ត្រឡប់ទៅផ្ទាំង admin", "admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
