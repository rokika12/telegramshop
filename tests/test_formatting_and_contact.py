import asyncio
import re
import unittest
from types import SimpleNamespace

from emoji import replace_emoji

from bot.handlers import _profile_text, _send_admin_contact
from bot.keyboards import CUSTOM_EMOJI_IDS, games_keyboard
from bot.text_format import html_label, plain_label


class LabelFormattingTests(unittest.TestCase):
    def test_profile_text_uses_custom_emoji_and_safe_separate_lines(self) -> None:
        rendered = _profile_text("Rovistar & <b>Shop</b>", "ROVISTARCAMBO", 8607930927)

        self.assertEqual(len(rendered.splitlines()), 6)
        self.assertIn("Rovistar &amp; &lt;b&gt;Shop&lt;/b&gt;", rendered)
        self.assertIn("<code>@ROVISTARCAMBO</code>", rendered)
        self.assertIn("<code>8607930927</code>", rendered)
        for emoji_id in (
            "5445163772706582819",
            "5444979432710242887",
            "5444903695256941915",
            "5444860552310457690",
        ):
            self.assertIn(f'<tg-emoji emoji-id="{emoji_id}">', rendered)

    def test_plain_label_removes_emoji_and_collapses_spacing(self) -> None:
        self.assertEqual(plain_label("Free Fire ✅ · 1 account"), "Free Fire · 1 account")

    def test_html_label_removes_regular_emoji_but_keeps_custom_tag(self) -> None:
        rendered = html_label(
            "Name ✅",
            'Name ✅ <tg-emoji emoji-id="123">🔥</tg-emoji> 🌟',
        )
        outside_custom = re.sub(r"<tg-emoji\b[^>]*>.*?</tg-emoji>", "", rendered, flags=re.S)
        self.assertEqual(replace_emoji(outside_custom, replace=""), outside_custom)
        self.assertIn('<tg-emoji emoji-id="123">🔥</tg-emoji>', rendered)

    def test_game_button_uses_clean_label_and_custom_icon(self) -> None:
        button = games_keyboard([{"id": 1, "name": "Free Fire ✅", "product_count": 1}]).inline_keyboard[0][0]
        self.assertNotIn("✅", button.text)
        self.assertEqual(button.icon_custom_emoji_id, CUSTOM_EMOJI_IDS["cart"])


class AdminContactTests(unittest.IsolatedAsyncioTestCase):
    async def test_contact_card_uses_current_photo_and_requested_username(self) -> None:
        class BotDouble:
            def __init__(self) -> None:
                self.photo_request_user_id = None
                self.sent_photo = None

            async def get_user_profile_photos(self, user_id: int, limit: int):
                self.photo_request_user_id = user_id
                return SimpleNamespace(photos=[[SimpleNamespace(file_id="latest-photo")]])

            async def get_chat(self, user_id: int):
                raise AssertionError("configured contact ID should not need admin permissions")

            async def send_photo(self, **kwargs):
                self.sent_photo = kwargs

            async def send_message(self, *args, **kwargs):
                raise AssertionError("a current profile photo is available")

        bot = BotDouble()
        await _send_admin_contact(bot, 10, frozenset(), contact_user_id=20)
        self.assertEqual(bot.photo_request_user_id, 20)
        self.assertEqual(bot.sent_photo["photo"], "latest-photo")
        button = bot.sent_photo["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.url, "https://t.me/hongzeze6")
        self.assertTrue(str(button.style).lower().endswith("danger"))


if __name__ == "__main__":
    unittest.main()