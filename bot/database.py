import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from cryptography.fernet import Fernet

ORDER_RESERVATION_MINUTES = 3


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _row_to_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Store:
    def __init__(self, path: str | Path, encryption_key: str) -> None:
        self.path = str(path)
        self._cipher = Fernet(encryption_key.encode("ascii"))

    async def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_name TEXT NOT NULL DEFAULT '',
                    username TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS games (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    name_html TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS levels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id INTEGER NOT NULL REFERENCES games(id),
                    name TEXT NOT NULL COLLATE NOCASE,
                    name_html TEXT NOT NULL DEFAULT '',
                    UNIQUE(game_id, name)
                );
                CREATE TABLE IF NOT EXISTS key_catalog_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    name_html TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    description_html TEXT NOT NULL DEFAULT '',
                    media_json TEXT NOT NULL,
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    is_listed INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS key_catalog_options (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES key_catalog_items(id),
                    label TEXT NOT NULL,
                    label_html TEXT NOT NULL DEFAULT '',
                    price_cents INTEGER NOT NULL CHECK(price_cents > 0),
                    created_at TEXT NOT NULL,
                    is_listed INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id INTEGER NOT NULL REFERENCES games(id),
                    level_id INTEGER NOT NULL REFERENCES levels(id),
                    name TEXT NOT NULL,
                    name_html TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    description_html TEXT NOT NULL DEFAULT '',
                    price_cents INTEGER NOT NULL CHECK(price_cents > 0),
                    photo_file_ids TEXT NOT NULL,
                    account_email_encrypted TEXT NOT NULL,
                    account_password_encrypted TEXT NOT NULL,
                    license_key_encrypted TEXT NOT NULL DEFAULT '',
                    delivery_file_id TEXT NOT NULL DEFAULT '',
                    delivery_url TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'available'
                        CHECK(status IN ('available', 'reserved', 'sold')),
                    reserved_order_id INTEGER,
                    is_listed INTEGER NOT NULL DEFAULT 1,
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS products_by_level_status
                    ON products(level_id, status);
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    user_id INTEGER NOT NULL REFERENCES users(user_id),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'paid', 'cancelled', 'expired')),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    payment_tx_id TEXT UNIQUE
                );
                CREATE TABLE IF NOT EXISTS wallet_accounts (
                    user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
                    balance_cents INTEGER NOT NULL DEFAULT 0 CHECK(balance_cents >= 0)
                );
                CREATE TABLE IF NOT EXISTS wallet_topups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(user_id),
                    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                    credit_cents INTEGER NOT NULL CHECK(credit_cents >= amount_cents),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'paid', 'expired', 'cancelled')),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    payment_tx_id TEXT UNIQUE
                );
                CREATE INDEX IF NOT EXISTS orders_by_status_expiration
                    ON orders(status, expires_at);
                CREATE INDEX IF NOT EXISTS wallet_topups_by_status_expiration
                    ON wallet_topups(status, expires_at);
                """
            )
            product_columns = {
                row[1] for row in await (await db.execute("PRAGMA table_info(products)")).fetchall()
            }
            if "name_html" not in product_columns:
                await db.execute("ALTER TABLE products ADD COLUMN name_html TEXT NOT NULL DEFAULT ''")
            if "description" not in product_columns:
                await db.execute("ALTER TABLE products ADD COLUMN description TEXT NOT NULL DEFAULT ''")
            if "description_html" not in product_columns:
                await db.execute("ALTER TABLE products ADD COLUMN description_html TEXT NOT NULL DEFAULT ''")
            if "is_listed" not in product_columns:
                await db.execute("ALTER TABLE products ADD COLUMN is_listed INTEGER NOT NULL DEFAULT 1")
            if "delivery_file_id" not in product_columns:
                await db.execute("ALTER TABLE products ADD COLUMN delivery_file_id TEXT NOT NULL DEFAULT ''")
            if "delivery_url" not in product_columns:
                await db.execute("ALTER TABLE products ADD COLUMN delivery_url TEXT NOT NULL DEFAULT ''")
            if "license_key_encrypted" not in product_columns:
                await db.execute("ALTER TABLE products ADD COLUMN license_key_encrypted TEXT NOT NULL DEFAULT ''")
            if "catalog_option_id" not in product_columns:
                await db.execute(
                    "ALTER TABLE products ADD COLUMN catalog_option_id INTEGER REFERENCES key_catalog_options(id)"
                )
            game_columns = {
                row[1] for row in await (await db.execute("PRAGMA table_info(games)")).fetchall()
            }
            if "name_html" not in game_columns:
                await db.execute("ALTER TABLE games ADD COLUMN name_html TEXT NOT NULL DEFAULT ''")
            level_columns = {
                row[1] for row in await (await db.execute("PRAGMA table_info(levels)")).fetchall()
            }
            if "name_html" not in level_columns:
                await db.execute("ALTER TABLE levels ADD COLUMN name_html TEXT NOT NULL DEFAULT ''")
            order_columns = {
                row[1] for row in await (await db.execute("PRAGMA table_info(orders)")).fetchall()
            }
            if "payment_tx_id" not in order_columns:
                await db.execute("ALTER TABLE orders ADD COLUMN payment_tx_id TEXT")
            await db.commit()

    async def create_key_catalog_item(
        self,
        *,
        name: str,
        name_html: str,
        description: str,
        description_html: str,
        media: list[dict[str, str]],
        options: list[dict[str, Any]],
        created_by: int,
    ) -> int:
        if not name.strip() or not media or not options:
            raise ValueError("A key listing requires a name, media and at least one option.")
        for option in options:
            if not str(option.get("label", "")).strip() or int(option.get("price_cents", 0)) <= 0:
                raise ValueError("Each option requires a label and positive price.")
            stock = option.get("stock", [])
            if not stock:
                raise ValueError("Each option requires at least one key or file in stock.")
            for entry in stock:
                has_key = bool(str(entry.get("license_key", "")).strip())
                has_file = bool(str(entry.get("delivery_file_id", "")).strip())
                if has_key == has_file:
                    raise ValueError("Each stock entry must contain exactly one key or file.")

        now = _timestamp(_utc_now())
        empty_encrypted = self._cipher.encrypt(b"").decode("ascii")
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """INSERT INTO key_catalog_items(
                       name, name_html, description, description_html, media_json, created_by, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    name.strip(), name_html, description.strip(), description_html,
                    json.dumps(media, ensure_ascii=False), created_by, now,
                ),
            )
            item_id = int(cursor.lastrowid)
            await db.execute("INSERT INTO games(name) VALUES ('Digital Keys') ON CONFLICT(name) DO NOTHING")
            cursor = await db.execute("SELECT id FROM games WHERE name = 'Digital Keys' COLLATE NOCASE")
            game_id = int((await cursor.fetchone())[0])
            await db.execute(
                "INSERT INTO levels(game_id, name) VALUES (?, 'Key Stock') ON CONFLICT(game_id, name) DO NOTHING",
                (game_id,),
            )
            cursor = await db.execute(
                "SELECT id FROM levels WHERE game_id = ? AND name = 'Key Stock' COLLATE NOCASE",
                (game_id,),
            )
            level_id = int((await cursor.fetchone())[0])

            for option in options:
                cursor = await db.execute(
                    """INSERT INTO key_catalog_options(item_id, label, label_html, price_cents, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (item_id, str(option["label"]).strip(), str(option.get("label_html", "")), int(option["price_cents"]), now),
                )
                option_id = int(cursor.lastrowid)
                for entry in option["stock"]:
                    license_key = str(entry.get("license_key", "")).strip()
                    encrypted_key = self._cipher.encrypt(license_key.encode("utf-8")).decode("ascii")
                    await db.execute(
                        """INSERT INTO products(
                               game_id, level_id, name, name_html, description, description_html,
                               price_cents, photo_file_ids, account_email_encrypted,
                               account_password_encrypted, license_key_encrypted, delivery_file_id,
                               catalog_option_id, created_by, created_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            game_id, level_id, name.strip(), name_html, description.strip(), description_html,
                            int(option["price_cents"]), empty_encrypted, empty_encrypted, encrypted_key,
                            str(entry.get("delivery_file_id", "")).strip(), option_id, created_by, now,
                        ),
                    )
            await db.commit()
            return item_id

    async def key_catalog_items(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT i.id, i.name, i.name_html, i.description, i.description_html, i.media_json,
                          COUNT(DISTINCT o.id) AS option_count,
                          SUM(CASE WHEN p.status = 'available' THEN 1 ELSE 0 END) AS stock_count
                   FROM key_catalog_items AS i
                   JOIN key_catalog_options AS o ON o.item_id = i.id AND o.is_listed = 1
                   LEFT JOIN products AS p ON p.catalog_option_id = o.id AND p.is_listed = 1
                   WHERE i.is_listed = 1
                   GROUP BY i.id ORDER BY i.id DESC"""
            )
            items = [dict(row) for row in await cursor.fetchall()]
            for item in items:
                item["media"] = json.loads(item.pop("media_json"))
            return items

    async def key_catalog_item(self, item_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT id, name, name_html, description, description_html, media_json
                   FROM key_catalog_items WHERE id = ? AND is_listed = 1""",
                (item_id,),
            )
            item = _row_to_dict(await cursor.fetchone())
            if item:
                item["media"] = json.loads(item.pop("media_json"))
            return item

    async def key_catalog_options(self, item_id: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT o.id, o.item_id, o.label, o.label_html, o.price_cents,
                          SUM(CASE WHEN p.status = 'available' AND p.is_listed = 1 THEN 1 ELSE 0 END) AS stock_count
                   FROM key_catalog_options AS o
                   JOIN key_catalog_items AS i ON i.id = o.item_id AND i.is_listed = 1
                   LEFT JOIN products AS p ON p.catalog_option_id = o.id
                   WHERE o.item_id = ? AND o.is_listed = 1
                   GROUP BY o.id ORDER BY o.id""",
                (item_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def available_key_stock_product(self, option_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT p.id, p.price_cents, o.label, o.label_html, i.name
                   FROM products AS p
                   JOIN key_catalog_options AS o ON o.id = p.catalog_option_id AND o.is_listed = 1
                   JOIN key_catalog_items AS i ON i.id = o.item_id AND i.is_listed = 1
                   WHERE o.id = ? AND p.status = 'available' AND p.is_listed = 1
                   ORDER BY p.id LIMIT 1""",
                (option_id,),
            )
            return _row_to_dict(await cursor.fetchone())

    async def register_user(self, user_id: int, first_name: str, username: str | None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO users(user_id, first_name, username, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       first_name=excluded.first_name,
                       username=excluded.username""",
                (user_id, first_name, username, _timestamp(_utc_now())),
            )
            await db.execute(
                "INSERT INTO wallet_accounts(user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )
            await db.commit()

    async def user_ids(self) -> list[int]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT user_id FROM users ORDER BY user_id")
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def create_product(
        self,
        *,
        game: str,
        level: str,
        name: str,
        price_cents: int,
        photo_file_ids: list[str],
        account_email: str,
        account_password: str,
        license_key: str = "",
        created_by: int,
        game_html: str = "",
        level_html: str = "",
        name_html: str = "",
        description: str = "",
        description_html: str = "",
        delivery_file_id: str = "",
        delivery_url: str = "",
    ) -> int:
        if not photo_file_ids:
            raise ValueError("A product must have at least one photo.")
        if price_cents <= 0:
            raise ValueError("Price must be greater than zero.")

        email_encrypted = self._cipher.encrypt(account_email.encode("utf-8")).decode("ascii")
        password_encrypted = self._cipher.encrypt(account_password.encode("utf-8")).decode("ascii")
        license_key_encrypted = self._cipher.encrypt(license_key.encode("utf-8")).decode("ascii")
        now = _timestamp(_utc_now())

        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """INSERT INTO games(name, name_html) VALUES (?, ?)
                   ON CONFLICT(name) DO UPDATE SET name_html =
                       CASE WHEN excluded.name_html != '' THEN excluded.name_html ELSE games.name_html END""",
                (game.strip(), game_html),
            )
            cursor = await db.execute("SELECT id FROM games WHERE name = ? COLLATE NOCASE", (game.strip(),))
            game_row = await cursor.fetchone()
            assert game_row is not None
            game_id = int(game_row[0])
            await db.execute(
                """INSERT INTO levels(game_id, name, name_html) VALUES (?, ?, ?)
                   ON CONFLICT(game_id, name) DO UPDATE SET name_html =
                       CASE WHEN excluded.name_html != '' THEN excluded.name_html ELSE levels.name_html END""",
                (game_id, level.strip(), level_html),
            )
            cursor = await db.execute(
                "SELECT id FROM levels WHERE game_id = ? AND name = ? COLLATE NOCASE",
                (game_id, level.strip()),
            )
            level_row = await cursor.fetchone()
            assert level_row is not None
            cursor = await db.execute(
                """INSERT INTO products(
                       game_id, level_id, name, name_html, description, description_html,
                       price_cents, photo_file_ids,
                       account_email_encrypted, account_password_encrypted, license_key_encrypted,
                       delivery_file_id, delivery_url, created_by, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    game_id,
                    int(level_row[0]),
                    name.strip(),
                    name_html,
                    description.strip(),
                    description_html,
                    price_cents,
                    json.dumps(photo_file_ids),
                    email_encrypted,
                    password_encrypted,
                    license_key_encrypted,
                    delivery_file_id.strip(),
                    delivery_url.strip(),
                    created_by,
                    now,
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def games_with_stock(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                     """SELECT g.id, g.name, g.name_html, COUNT(p.id) AS product_count,
                                  SUM(CASE WHEN p.status = 'available' THEN 1 ELSE 0 END) AS available_count
                   FROM games AS g
                         JOIN products AS p ON p.game_id = g.id AND p.is_listed = 1
                         AND p.catalog_option_id IS NULL
                   GROUP BY g.id, g.name ORDER BY g.name COLLATE NOCASE"""
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def levels_with_stock(self, game_id: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                     """SELECT l.id, l.name, l.name_html, COUNT(p.id) AS product_count,
                                  SUM(CASE WHEN p.status = 'available' THEN 1 ELSE 0 END) AS available_count
                   FROM levels AS l
                         JOIN products AS p ON p.level_id = l.id AND p.is_listed = 1
                         AND p.catalog_option_id IS NULL
                   WHERE l.game_id = ?
                   GROUP BY l.id, l.name ORDER BY l.name COLLATE NOCASE""",
                (game_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def products_for_game(self, game_id: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT p.id, p.game_id, p.level_id, p.name, p.name_html,
                          p.description, p.description_html, p.price_cents,
                          p.photo_file_ids, p.status,
                          g.name AS game_name, g.name_html AS game_name_html,
                          l.name AS level_name, l.name_html AS level_name_html
                   FROM products AS p
                   JOIN games AS g ON g.id = p.game_id
                   JOIN levels AS l ON l.id = p.level_id
                   WHERE p.game_id = ? AND p.is_listed = 1 AND p.catalog_option_id IS NULL
                   ORDER BY l.name COLLATE NOCASE, p.id DESC""",
                (game_id,),
            )
            products = [dict(row) for row in await cursor.fetchall()]
            for product in products:
                product["photo_file_ids"] = json.loads(product["photo_file_ids"])
            return products

    async def products_for_level(self, level_id: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT p.id, p.game_id, p.level_id, p.name, p.name_html, p.description, p.description_html,
                          p.price_cents, p.photo_file_ids, p.status,
                          g.name AS game_name, g.name_html AS game_name_html,
                          l.name AS level_name, l.name_html AS level_name_html
                   FROM products AS p
                   JOIN games AS g ON g.id = p.game_id
                   JOIN levels AS l ON l.id = p.level_id
                   WHERE p.level_id = ? AND p.is_listed = 1 AND p.catalog_option_id IS NULL
                   ORDER BY p.id DESC""",
                (level_id,),
            )
            products = [dict(row) for row in await cursor.fetchall()]
            for product in products:
                product["photo_file_ids"] = json.loads(product["photo_file_ids"])
            return products

    async def available_product(self, product_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT p.id, p.level_id, p.name, p.name_html, p.description, p.description_html,
                          p.price_cents, p.photo_file_ids, p.status,
                          g.name AS game_name, g.name_html AS game_name_html,
                          l.name AS level_name, l.name_html AS level_name_html
                   FROM products AS p
                   JOIN games AS g ON g.id = p.game_id
                   JOIN levels AS l ON l.id = p.level_id
                   WHERE p.id = ? AND p.is_listed = 1 AND p.catalog_option_id IS NULL""",
                (product_id,),
            )
            product = _row_to_dict(await cursor.fetchone())
            if product:
                product["photo_file_ids"] = json.loads(product["photo_file_ids"])
            return product

    async def admin_products(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT p.id, p.name, p.name_html, p.price_cents, p.status, p.is_listed,
                          g.name AS game_name, g.name_html AS game_name_html,
                          l.name AS level_name, l.name_html AS level_name_html
                   FROM products AS p
                   JOIN games AS g ON g.id = p.game_id
                   JOIN levels AS l ON l.id = p.level_id
                   WHERE p.catalog_option_id IS NULL
                   ORDER BY p.id DESC"""
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def product_email_preview(self, product_id: int) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT account_email_encrypted FROM products WHERE id = ?",
                (product_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        email = self._cipher.decrypt(row[0].encode("ascii")).decode("utf-8")
        local, separator, domain = email.partition("@")
        if not separator or not domain:
            return "***"
        return f"{local[:2]}***@{domain}"

    async def admin_product_credentials(self, product_id: int) -> dict[str, str] | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """SELECT account_email_encrypted, account_password_encrypted
                   FROM products
                   WHERE id = ? AND status = 'available' AND reserved_order_id IS NULL""",
                (product_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "email": self._cipher.decrypt(row[0].encode("ascii")).decode("utf-8"),
            "password": self._cipher.decrypt(row[1].encode("ascii")).decode("utf-8"),
        }

    async def update_product_email(self, product_id: int, email: str) -> bool:
        encrypted = self._cipher.encrypt(email.encode("utf-8")).decode("ascii")
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """UPDATE products SET account_email_encrypted = ?
                   WHERE id = ? AND status = 'available' AND reserved_order_id IS NULL""",
                (encrypted, product_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def update_product_password(self, product_id: int, password: str) -> bool:
        encrypted = self._cipher.encrypt(password.encode("utf-8")).decode("ascii")
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """UPDATE products SET account_password_encrypted = ?
                   WHERE id = ? AND status = 'available' AND reserved_order_id IS NULL""",
                (encrypted, product_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def hide_product(self, product_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """UPDATE products SET is_listed = 0 WHERE id = ? AND catalog_option_id IS NULL
                   AND is_listed = 1 AND status != 'reserved'""",
                (product_id,),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def admin_key_catalog_items(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT i.id, i.name, i.is_listed,
                          SUM(CASE WHEN p.status = 'reserved' THEN 1 ELSE 0 END) AS reserved_count,
                          SUM(CASE WHEN p.status = 'available' THEN 1 ELSE 0 END) AS stock_count
                   FROM key_catalog_items AS i
                   LEFT JOIN key_catalog_options AS o ON o.item_id = i.id
                   LEFT JOIN products AS p ON p.catalog_option_id = o.id AND p.is_listed = 1
                   GROUP BY i.id ORDER BY i.id DESC"""
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def hide_key_catalog_item(self, item_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """SELECT COUNT(*) FROM products AS p
                   JOIN key_catalog_options AS o ON o.id = p.catalog_option_id
                   WHERE o.item_id = ? AND p.status = 'reserved'""",
                (item_id,),
            )
            if int((await cursor.fetchone())[0]) > 0:
                await db.rollback()
                return False
            cursor = await db.execute(
                "UPDATE key_catalog_items SET is_listed = 0 WHERE id = ? AND is_listed = 1",
                (item_id,),
            )
            if cursor.rowcount == 1:
                await db.execute(
                    "UPDATE products SET is_listed = 0 WHERE catalog_option_id IN "
                    "(SELECT id FROM key_catalog_options WHERE item_id = ?) AND status = 'available'",
                    (item_id,),
                )
            await db.commit()
            return cursor.rowcount == 1

    async def set_payment_transaction(self, order_id: int, transaction_id: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """UPDATE orders SET payment_tx_id = ?
                   WHERE id = ? AND status = 'pending' AND expires_at > ?""",
                (transaction_id, order_id, _timestamp(_utc_now())),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def wallet_balance_cents(self, user_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT balance_cents FROM wallet_accounts WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def create_wallet_topup(
        self,
        user_id: int,
        amount_cents: int,
        credit_cents: int,
    ) -> dict[str, Any]:
        if amount_cents <= 0 or credit_cents < amount_cents:
            raise ValueError("Invalid wallet top-up amount")
        now = _utc_now()
        expires_at = now + timedelta(minutes=ORDER_RESERVATION_MINUTES)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """INSERT INTO wallet_topups(user_id, amount_cents, credit_cents, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, amount_cents, credit_cents, _timestamp(now), _timestamp(expires_at)),
            )
            topup_id = int(cursor.lastrowid)
            await db.commit()
            return {
                "id": topup_id,
                "user_id": user_id,
                "amount_cents": amount_cents,
                "credit_cents": credit_cents,
                "expires_at": _timestamp(expires_at),
            }

    async def set_wallet_topup_transaction(self, topup_id: int, transaction_id: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """UPDATE wallet_topups SET payment_tx_id = ?
                   WHERE id = ? AND status = 'pending' AND expires_at > ?""",
                (transaction_id, topup_id, _timestamp(_utc_now())),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def pending_wallet_topups(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                  """SELECT t.id, t.user_id, t.amount_cents, t.credit_cents, t.payment_tx_id, t.expires_at,
                         u.first_name, u.username
                     FROM wallet_topups AS t
                     JOIN users AS u ON u.user_id = t.user_id
                     WHERE t.status = 'pending' AND t.payment_tx_id IS NOT NULL AND t.expires_at > ?""",
                (_timestamp(_utc_now()),),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def complete_wallet_topup(
        self,
        topup_id: int,
        verified_amount_cents: int,
    ) -> dict[str, Any] | None:
        now = _timestamp(_utc_now())
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                  """SELECT t.id, t.user_id, t.amount_cents, t.credit_cents,
                         u.first_name, u.username
                     FROM wallet_topups AS t
                     JOIN users AS u ON u.user_id = t.user_id
                     WHERE t.id = ? AND t.status = 'pending' AND t.expires_at > ?""",
                (topup_id, now),
            )
            topup = _row_to_dict(await cursor.fetchone())
            if topup is None or int(topup["amount_cents"]) != verified_amount_cents:
                await db.rollback()
                return None
            await db.execute(
                """INSERT INTO wallet_accounts(user_id, balance_cents) VALUES (?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET balance_cents = balance_cents + excluded.balance_cents""",
                (topup["user_id"], topup["credit_cents"]),
            )
            await db.execute("UPDATE wallet_topups SET status = 'paid' WHERE id = ?", (topup_id,))
            cursor = await db.execute(
                "SELECT balance_cents FROM wallet_accounts WHERE user_id = ?",
                (topup["user_id"],),
            )
            balance_row = await cursor.fetchone()
            await db.commit()
            topup["balance_cents"] = int(balance_row[0])
            return topup

    async def pending_payment_orders(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT o.id, o.user_id, o.payment_tx_id, o.created_at, o.expires_at,
                          p.price_cents, p.name
                   FROM orders AS o
                   JOIN products AS p ON p.id = o.product_id
                   WHERE o.status = 'pending' AND o.payment_tx_id IS NOT NULL
                     AND o.expires_at > ?""",
                (_timestamp(_utc_now()),),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def create_order(self, product_id: int, user_id: int) -> dict[str, Any] | None:
        now = _utc_now()
        expires_at = now + timedelta(minutes=ORDER_RESERVATION_MINUTES)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """UPDATE products SET status = 'reserved'
                   WHERE id = ? AND status = 'available' AND is_listed = 1""",
                (product_id,),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None
            cursor = await db.execute(
                """INSERT INTO orders(product_id, user_id, created_at, expires_at)
                   VALUES (?, ?, ?, ?)""",
                (product_id, user_id, _timestamp(now), _timestamp(expires_at)),
            )
            order_id = int(cursor.lastrowid)
            await db.execute(
                "UPDATE products SET reserved_order_id = ? WHERE id = ?",
                (order_id, product_id),
            )
            await db.commit()
            order_cursor = await db.execute(
                    """SELECT order_row.id, order_row.user_id, order_row.expires_at, p.name, p.name_html, p.price_cents,
                             COALESCE(i.name, g.name) AS game_name,
                             COALESCE(i.name_html, g.name_html) AS game_name_html,
                             COALESCE(kco.label, l.name) AS level_name,
                             COALESCE(kco.label_html, l.name_html) AS level_name_html
                     FROM orders AS order_row
                     JOIN products AS p ON p.id = order_row.product_id
                   JOIN games AS g ON g.id = p.game_id
                   JOIN levels AS l ON l.id = p.level_id
                     LEFT JOIN key_catalog_options AS kco ON kco.id = p.catalog_option_id
                     LEFT JOIN key_catalog_items AS i ON i.id = kco.item_id
                     WHERE order_row.id = ?""",
                (order_id,),
            )
            return _row_to_dict(await order_cursor.fetchone())

    async def pay_product_with_wallet(self, product_id: int, user_id: int) -> dict[str, Any]:
        now = _utc_now()
        timestamp = _timestamp(now)
        expires_at = _timestamp(now + timedelta(minutes=ORDER_RESERVATION_MINUTES))
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """SELECT p.id, p.price_cents, p.name, p.name_html,
                             COALESCE(i.name, g.name) AS game_name,
                             COALESCE(i.name_html, g.name_html) AS game_name_html,
                             COALESCE(kco.label, l.name) AS level_name,
                             COALESCE(kco.label_html, l.name_html) AS level_name_html
                   FROM products AS p
                   JOIN games AS g ON g.id = p.game_id
                   JOIN levels AS l ON l.id = p.level_id
                         LEFT JOIN key_catalog_options AS kco ON kco.id = p.catalog_option_id
                         LEFT JOIN key_catalog_items AS i ON i.id = kco.item_id
                   WHERE p.id = ? AND p.status = 'available' AND p.is_listed = 1""",
                (product_id,),
            )
            product = _row_to_dict(await cursor.fetchone())
            if product is None:
                await db.rollback()
                return {"status": "unavailable"}

            cursor = await db.execute(
                "SELECT balance_cents FROM wallet_accounts WHERE user_id = ?",
                (user_id,),
            )
            wallet = await cursor.fetchone()
            balance_cents = int(wallet[0]) if wallet else 0
            price_cents = int(product["price_cents"])
            if balance_cents < price_cents:
                await db.rollback()
                return {
                    "status": "insufficient",
                    "balance_cents": balance_cents,
                    "price_cents": price_cents,
                }

            cursor = await db.execute(
                """UPDATE products SET status = 'reserved'
                   WHERE id = ? AND status = 'available' AND is_listed = 1""",
                (product_id,),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return {"status": "unavailable"}

            cursor = await db.execute(
                """INSERT INTO orders(product_id, user_id, status, created_at, expires_at)
                   VALUES (?, ?, 'paid', ?, ?)""",
                (product_id, user_id, timestamp, expires_at),
            )
            order_id = int(cursor.lastrowid)
            await db.execute(
                """UPDATE products SET status = 'sold', reserved_order_id = NULL
                   WHERE id = ?""",
                (product_id,),
            )
            await db.execute(
                "UPDATE wallet_accounts SET balance_cents = balance_cents - ? WHERE user_id = ?",
                (price_cents, user_id),
            )
            await db.commit()

            cursor = await db.execute(
                """SELECT o.id, o.user_id, o.product_id, p.name, p.name_html, p.price_cents,
                          p.account_email_encrypted, p.account_password_encrypted,
                          p.license_key_encrypted,
                          p.delivery_file_id, p.delivery_url,
                             COALESCE(i.name, g.name) AS game_name,
                             COALESCE(i.name_html, g.name_html) AS game_name_html,
                             COALESCE(kco.label, l.name) AS level_name,
                             COALESCE(kco.label_html, l.name_html) AS level_name_html
                   FROM orders AS o
                   JOIN products AS p ON p.id = o.product_id
                   JOIN games AS g ON g.id = p.game_id
                   JOIN levels AS l ON l.id = p.level_id
                         LEFT JOIN key_catalog_options AS kco ON kco.id = p.catalog_option_id
                         LEFT JOIN key_catalog_items AS i ON i.id = kco.item_id
                   WHERE o.id = ?""",
                (order_id,),
            )
            order = _row_to_dict(await cursor.fetchone())
            cursor = await db.execute(
                "SELECT balance_cents FROM wallet_accounts WHERE user_id = ?",
                (user_id,),
            )
            balance = await cursor.fetchone()

        assert order is not None and balance is not None
        order["account_email"] = self._cipher.decrypt(
            order.pop("account_email_encrypted").encode("ascii")
        ).decode("utf-8")
        order["account_password"] = self._cipher.decrypt(
            order.pop("account_password_encrypted").encode("ascii")
        ).decode("utf-8")
        encrypted_key = order.pop("license_key_encrypted", "")
        order["license_key"] = (
            self._cipher.decrypt(encrypted_key.encode("ascii")).decode("utf-8")
            if encrypted_key
            else ""
        )
        order["status"] = "paid"
        order["balance_cents"] = int(balance[0])
        return order

    async def pending_orders(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                  """SELECT o.id, o.user_id, o.expires_at, p.name, p.name_html, p.price_cents,
                         COALESCE(i.name, g.name) AS game_name,
                         COALESCE(i.name_html, g.name_html) AS game_name_html,
                         COALESCE(kco.label, l.name) AS level_name,
                         COALESCE(kco.label_html, l.name_html) AS level_name_html
                   FROM orders AS o
                   JOIN products AS p ON p.id = o.product_id
                   JOIN games AS g ON g.id = p.game_id
                   JOIN levels AS l ON l.id = p.level_id
                     LEFT JOIN key_catalog_options AS kco ON kco.id = p.catalog_option_id
                     LEFT JOIN key_catalog_items AS i ON i.id = kco.item_id
                   WHERE o.status = 'pending' AND o.expires_at > ?
                   ORDER BY o.id""",
                (_timestamp(_utc_now()),),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def complete_paid_order(
        self,
        order_id: int,
        verified_amount_cents: int,
    ) -> dict[str, Any] | None:
        now = _timestamp(_utc_now())
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """SELECT o.id, o.user_id, o.product_id, p.name, p.name_html, p.price_cents,
                          p.account_email_encrypted, p.account_password_encrypted,
                          p.license_key_encrypted,
                          p.delivery_file_id, p.delivery_url,
                             COALESCE(i.name, g.name) AS game_name,
                             COALESCE(i.name_html, g.name_html) AS game_name_html,
                             COALESCE(kco.label, l.name) AS level_name,
                             COALESCE(kco.label_html, l.name_html) AS level_name_html
                   FROM orders AS o
                   JOIN products AS p ON p.id = o.product_id
                   JOIN games AS g ON g.id = p.game_id
                   JOIN levels AS l ON l.id = p.level_id
                         LEFT JOIN key_catalog_options AS kco ON kco.id = p.catalog_option_id
                         LEFT JOIN key_catalog_items AS i ON i.id = kco.item_id
                   WHERE o.id = ? AND o.status = 'pending' AND o.expires_at > ?
                     AND p.status = 'reserved' AND p.reserved_order_id = o.id""",
                (order_id, now),
            )
            order = _row_to_dict(await cursor.fetchone())
            if order is None or int(order["price_cents"]) != verified_amount_cents:
                await db.rollback()
                return None
            await db.execute("UPDATE orders SET status = 'paid' WHERE id = ?", (order_id,))
            await db.execute(
                """UPDATE products SET status = 'sold', reserved_order_id = NULL
                   WHERE id = ? AND reserved_order_id = ?""",
                (order["product_id"], order_id),
            )
            await db.commit()

        order["account_email"] = self._cipher.decrypt(
            order.pop("account_email_encrypted").encode("ascii")
        ).decode("utf-8")
        order["account_password"] = self._cipher.decrypt(
            order.pop("account_password_encrypted").encode("ascii")
        ).decode("utf-8")
        encrypted_key = order.pop("license_key_encrypted", "")
        order["license_key"] = (
            self._cipher.decrypt(encrypted_key.encode("ascii")).decode("utf-8")
            if encrypted_key
            else ""
        )
        return order

    async def cancel_order(self, order_id: int, user_id: int | None = None) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """SELECT o.id, o.user_id, o.product_id, p.name
                   FROM orders AS o JOIN products AS p ON p.id = o.product_id
                         WHERE o.id = ? AND o.status = 'pending'
                            AND (? IS NULL OR o.user_id = ?)""",
                     (order_id, user_id, user_id),
            )
            order = _row_to_dict(await cursor.fetchone())
            if order is None:
                await db.rollback()
                return None
            await db.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,))
            await db.execute(
                """UPDATE products SET status = 'available', reserved_order_id = NULL
                   WHERE id = ? AND reserved_order_id = ?""",
                (order["product_id"], order_id),
            )
            await db.commit()
            return order

    async def expire_orders(self) -> list[dict[str, Any]]:
        now = _timestamp(_utc_now())
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """SELECT o.id, o.user_id, o.product_id, p.name
                   FROM orders AS o JOIN products AS p ON p.id = o.product_id
                   WHERE o.status = 'pending' AND o.expires_at <= ?""",
                (now,),
            )
            expired = [dict(row) for row in await cursor.fetchall()]
            for order in expired:
                await db.execute("UPDATE orders SET status = 'expired' WHERE id = ?", (order["id"],))
                await db.execute(
                    """UPDATE products SET status = 'available', reserved_order_id = NULL
                       WHERE id = ? AND reserved_order_id = ?""",
                    (order["product_id"], order["id"]),
                )
            await db.commit()
            return expired

    async def order_history(self, user_id: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT o.id, o.status, o.created_at, p.name, p.name_html, p.price_cents,
                             COALESCE(i.name, g.name) AS game_name,
                             COALESCE(i.name_html, g.name_html) AS game_name_html,
                             COALESCE(kco.label, l.name) AS level_name,
                             COALESCE(kco.label_html, l.name_html) AS level_name_html
                   FROM orders AS o
                   JOIN products AS p ON p.id = o.product_id
                   JOIN games AS g ON g.id = p.game_id
                   JOIN levels AS l ON l.id = p.level_id
                         LEFT JOIN key_catalog_options AS kco ON kco.id = p.catalog_option_id
                         LEFT JOIN key_catalog_items AS i ON i.id = kco.item_id
                   WHERE o.user_id = ? ORDER BY o.id DESC LIMIT 20""",
                (user_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]
