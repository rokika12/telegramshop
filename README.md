# ROKIRA Telegram Bot

A Telegram-only game-key and digital-product shop built with aiogram 3. The customer and admin menus are persistent reply keyboards displayed at the bottom of Telegram, not inline keyboards attached to the welcome message. Admin menu keys are shown only to IDs in the allowlist.

## Setup on Windows

Requirements: Python 3.10 or newer.

1. Create a bot with BotFather.
2. Create the Python environment and install dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

3. Generate a new encryption key for account credentials:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

4. Copy `.env.example` to `.env`. Set `TELEGRAM_BOT_TOKEN`, put the generated value in `ACCOUNT_DATA_KEY`, and copy the actual `KHQRCC_SECRET_KEY` from your KHQRcc merchant dashboard. The supplied profile ID and admin IDs are prefilled; adjust the admin list if needed. Keep all keys private.
5. Start the bot:

```powershell
python -m bot.main
```

## Customer menu and catalog

`/start` shows the customer menu. The catalog lists key products alongside any legacy products. A key listing has a name, photo/video promo media, description, and up to ten named price options (for example, `1 day` or `1 month`). Each option has its own stock. Admins add stock one item at a time as a key message or Telegram document; each submitted key/document is one saleable unit.

A customer opens a key listing, reviews its media and description, then selects an option button to see its exact price and choose wallet or ABA Mobile QR payment. The bot reserves one stock unit for that option. QR orders stay pending for three minutes in Cambodia time until the provider verifies the transaction and exact amount. After verified QR payment or a completed wallet charge, the bot sends the encrypted key or attached document. Keys are encrypted in SQLite and are never sent based on a redirect or user claim.

## KHQR verification

The checkout request is signed with `sha1(secret + transaction_id + amount + success_url + remark)`. The bot polls the supplied Verify V2 endpoint every three seconds for the three-minute checkout window, using `sha1(secret + transaction_id)`. It only fulfills an order if the provider returns `responseCode: 0`, `data.status: success`, and an amount exactly matching the order in USD cents. Mismatches are not fulfilled and admins are notified.

`PAYMENTS_ENABLED` defaults to `false`. The merchant profile must have the KHQRcc ABA Access Plan enabled and its managed checkout page must display a QR for the order amount. Set it to `true` only after that is confirmed; this enables checkout and Verify V2 polling.

The profile ID from the provided integration notes is in `.env.example`. `KHQRCC_SECRET_KEY` is deliberately a placeholder; payment cannot start until you enter the merchant secret. The default success URL opens the bot again and is only a return destination; the backend Verify V2 response is the payment authority. Set `KHQRCC_SUCCESS_URL` to another HTTPS destination if your merchant setup requires it. If KHQR configuration is missing, the bot refuses to reserve the account and alerts the customer.

## Admin dashboard

Only Telegram user IDs in `ADMIN_USER_IDS` can access `/admin` or the dashboard key shown on their `/start` keyboard. In private chat, admins can:

- Add a key listing: enter its name, add one or more promo photos/videos, enter an optional formatted description, then create one or more named price options. For each option, enter the price and submit each saleable key or Telegram document one at a time; the stock count increases with every item. Review the option prices and stock counts, then confirm to publish and broadcast the listing.
- Legacy account management remains available for older account listings. Admins can select an available legacy account to reveal a masked Gmail preview, reveal the full Gmail/password in an allowlisted admin's private chat, or replace either value. Revealed credential messages are protected from forwarding/saving and auto-delete after 60 seconds. Sold or reserved accounts cannot be viewed or edited.
- Manage accounts: select an account to see a masked Gmail preview, reveal the full Gmail/password in an allowlisted admin's private chat, or replace either value. Revealed credential messages are protected from forwarding/saving and auto-delete after 60 seconds. Sold or reserved accounts cannot be viewed or edited.
- Delete a product: select a listed product and confirm. This hides it from customer listings while retaining order history. A currently reserved product must first have its order cancelled or expire.
- View pending orders, recheck KHQR, or cancel an order to release the account.

Admin form inputs are sent in private chat. The bot attempts to delete submitted keys and legacy account credentials after receiving them. Keys and credentials are Fernet-encrypted before SQLite storage. Admin credential reveal is restricted to private chat and available legacy accounts; keys are sent to customers only after verified payment or a successful wallet purchase.

The support card links to `@hongzeze6`. Set optional `ADMIN_CONTACT_USER_ID` to that user's numeric Telegram ID to show their current profile photo; this value does not grant admin permissions. Without it, the contact link still works but the card has no profile photo.

## Emoji, colors, and message formatting

The bottom reply buttons use the provided custom emoji IDs via `icon_custom_emoji_id`, with green/blue styles. Sold items use red buttons. Ordinary Unicode emoji are removed from product labels; custom emoji IDs remain available for button and message icons. Rich descriptions preserve Telegram formatting and custom emoji while removing ordinary emoji. Change icon IDs in `bot/keyboards.py`.

## Data and secrets

`bot_data.sqlite3` stores user IDs, product and option metadata, order history, media file IDs, payment transaction IDs, and encrypted keys/credentials. `.env` and database files are ignored by Git. Back up the Fernet key separately from the database; losing it makes stored keys and credentials unrecoverable. Never commit or share the bot token, KHQR merchant secret, account passwords, product keys, or Fernet key.
