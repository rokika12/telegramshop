# ROKIRA Project Notes

- This workspace contains a Telegram bot only; do not add a Mini App or frontend unless requested.
- Use aiogram 3 and keep bot messages and button labels in Khmer.
- Read `TELEGRAM_BOT_TOKEN` from `.env`; never commit or print secrets.
- Keep the main customer/admin menus as Telegram reply keyboards at the bottom of the chat. Use inline keyboards only for catalog/product/payment actions.
- Keep ordinary emoji out of button text. Put custom IDs in `icon_custom_emoji_id` and HTML custom emoji in messages.
- Never mark an order paid or reveal account credentials to customers without a successful KHQR Verify V2 response and an exact amount match.
- Admins in `ADMIN_USER_IDS` may reveal stored Gmail/password only in private chat for available, unreserved products; protect the message and auto-delete it after 60 seconds. Never reveal credentials for sold/reserved products or to non-admins.
- Run the bot with `python -m bot.main` after installing `requirements.txt`.