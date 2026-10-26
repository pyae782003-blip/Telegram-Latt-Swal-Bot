import sqlite3
import os
import shutil
import logging
import contextvars
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Context variable to automatically track and scope the active store across calls
current_store_id_ctx: contextvars.ContextVar[int] = contextvars.ContextVar("db_store_id_ctx", default=1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

def _resolve_db_file() -> str:
    """
    Resolves SQLite database path with persistent cloud volume auto-detection.
    Supports Fly.io (/data), Koyeb (/data, /mnt/data), Render (/var/data), Railway (/data),
    custom DATABASE_PATH / SQLITE_DB_PATH / DATABASE_URL, or local fallback.
    """
    env_path = (os.getenv("DATABASE_PATH") or os.getenv("SQLITE_DB_PATH") or "").strip()
    is_persistent = False

    if env_path:
        # If env_path is a relative file name like 'store.db', check if persistent volumes exist
        if not os.path.isabs(env_path):
            if os.path.isdir("/data") and os.access("/data", os.W_OK):
                db_path = os.path.join("/data", os.path.basename(env_path))
                is_persistent = True
            elif os.path.isdir("/var/data") and os.access("/var/data", os.W_OK):
                db_path = os.path.join("/var/data", os.path.basename(env_path))
                is_persistent = True
            elif os.path.isdir("/mnt/data") and os.access("/mnt/data", os.W_OK):
                db_path = os.path.join("/mnt/data", os.path.basename(env_path))
                is_persistent = True
            else:
                db_path = os.path.abspath(env_path)
        else:
            db_path = env_path
            is_persistent = any(db_path.startswith(d) for d in ("/data", "/var/data", "/mnt/data"))
    else:
        db_url = (os.getenv("DATABASE_URL") or "").strip()
        if db_url.startswith("sqlite:///"):
            db_path = db_url.replace("sqlite:///", "")
        elif db_url.startswith("sqlite://"):
            db_path = db_url.replace("sqlite://", "")
        else:
            # Auto-detect standard cloud persistent volume mount points
            if os.path.isdir("/data") and os.access("/data", os.W_OK):
                db_path = "/data/store.db"
                is_persistent = True
            elif os.path.isdir("/var/data") and os.access("/var/data", os.W_OK):
                db_path = "/var/data/store.db"
                is_persistent = True
            elif os.path.isdir("/mnt/data") and os.access("/mnt/data", os.W_OK):
                db_path = "/mnt/data/store.db"
                is_persistent = True
            else:
                db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store.db")

    parent_dir = os.path.dirname(os.path.abspath(db_path))
    if parent_dir and not os.path.exists(parent_dir):
        try:
            os.makedirs(parent_dir, exist_ok=True)
            logger.info(f"📁 Created database directory: {parent_dir}")
        except Exception as e:
            logger.warning(f"⚠️ Could not create database directory {parent_dir}: {e}")

    # Auto-seed from local template if persistent volume database does not exist yet
    local_template = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store.db")
    if db_path != local_template and not os.path.exists(db_path) and os.path.exists(local_template):
        try:
            shutil.copy2(local_template, db_path)
            logger.info(f"📋 Seeded fresh persistent database at '{db_path}' from repository template '{local_template}'")
        except Exception as se:
            logger.warning(f"⚠️ Could not seed database to '{db_path}': {se}")

    logger.info(f"📁 [Database] SQLite DB File: {db_path} (Persistent Storage: {is_persistent})")
    return db_path

DB_FILE = _resolve_db_file()


def get_postgres_url() -> Optional[str]:
    """Retrieves and standardizes cloud PostgreSQL / Supabase connection URL from environment variables."""
    url = (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or os.getenv("SUPABASE_DB_URL") or "").strip()
    if url.startswith("postgres://") or url.startswith("postgresql://"):
        # Fix legacy postgres:// dialect prefix for psycopg2 if needed
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        # Automatically append sslmode=require for cloud-hosted managed databases like Supabase, Neon, Render
        if "sslmode=" not in url and any(h in url for h in ("supabase.com", "supabase.co", "neon.tech", "pooler.supabase.com", "fly.dev", "koyeb")):
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}sslmode=require"
        return url
    return None


def is_postgres() -> bool:
    """Returns True if a cloud PostgreSQL or Supabase database URL is configured."""
    return get_postgres_url() is not None


class PostgresCursorWrapper:
    """Wraps psycopg2 cursor to provide transparent SQLite-to-PostgreSQL syntax translation and dict-like row access."""
    def __init__(self, raw_cursor, conn_wrapper):
        self.cursor = raw_cursor
        self.conn_wrapper = conn_wrapper
        self.lastrowid = None
        self._last_result = None

    def _adapt_sql(self, sql: str) -> str:
        s = sql.strip()
        # 1. Translate SQLite INSERT OR IGNORE INTO to PostgreSQL ON CONFLICT DO NOTHING
        if re.search(r"INSERT\s+OR\s+IGNORE\s+INTO", s, re.IGNORECASE):
            s = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", s, flags=re.IGNORECASE)
            if not re.search(r"ON\s+CONFLICT", s, re.IGNORECASE):
                s = s.rstrip(";") + " ON CONFLICT DO NOTHING"

        # 2. Translate SQLite strftime to PostgreSQL date formatting
        if "strftime('%Y-%m'," in s:
            s = re.sub(r"strftime\('%Y-%m',\s*([^)]+)\)", r"substr(CAST(\1 AS TEXT), 1, 7)", s)

        # 3. Replace SQLite '?' parameter placeholders with PostgreSQL '%s'
        parts = s.split("'")
        for i in range(0, len(parts), 2):
            parts[i] = parts[i].replace("?", "%s")
        s = "'".join(parts)
        return s

    def execute(self, sql: str, params=None):
        adapted_sql = self._adapt_sql(sql)
        # Capture lastrowid for tables with auto-increment 'id' primary keys
        is_insert = bool(re.search(r"^\s*INSERT\s+INTO\s+(products|stock|orders|stores|store_requests)\b", adapted_sql, re.IGNORECASE))
        has_returning = "RETURNING" in adapted_sql.upper()
        if is_insert and not has_returning:
            adapted_sql = adapted_sql.rstrip(";") + " RETURNING id"

        try:
            if params is not None:
                self.cursor.execute(adapted_sql, tuple(params))
            else:
                self.cursor.execute(adapted_sql)

            if is_insert and not has_returning:
                try:
                    res = self.cursor.fetchone()
                    if res:
                        self.lastrowid = res.get("id") if isinstance(res, dict) else res[0]
                        self._last_result = res
                except Exception:
                    pass
        except Exception as e:
            # Fallback if RETURNING id is incompatible
            if is_insert and not has_returning:
                orig_adapted = self._adapt_sql(sql)
                if params is not None:
                    self.cursor.execute(orig_adapted, tuple(params))
                else:
                    self.cursor.execute(orig_adapted)
            else:
                raise e
        return self

    def executemany(self, sql: str, seq_of_params):
        adapted_sql = self._adapt_sql(sql)
        self.cursor.executemany(adapted_sql, seq_of_params)
        return self

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        rows = self.cursor.fetchall()
        return [dict(r) for r in rows]

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def __iter__(self):
        for r in self.cursor:
            yield dict(r)

    def close(self):
        self.cursor.close()


class PostgresConnectionWrapper:
    """Wraps psycopg2 connection with transaction management and dict cursor factory."""
    def __init__(self, raw_conn):
        self.conn = raw_conn

    def cursor(self):
        import psycopg2.extras
        raw_cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return PostgresCursorWrapper(raw_cur, self)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

    def execute(self, sql: str, params=None):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()


def get_connection():
    """
    Returns a unified database connection:
    - If DATABASE_URL / POSTGRES_URL / SUPABASE_DB_URL is set, connects to PostgreSQL / Supabase cloud database.
    - Otherwise, falls back to local/persistent SQLite store.db with WAL mode.
    """
    pg_url = get_postgres_url()
    if pg_url:
        try:
            import psycopg2
            raw_conn = psycopg2.connect(pg_url)
            return PostgresConnectionWrapper(raw_conn)
        except Exception as e:
            logger.error(f"❌ [Database] PostgreSQL connection error: {e}. Falling back to SQLite...")

    # Local SQLite Fallback
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=15000;")
    conn.row_factory = sqlite3.Row
    return conn


def _init_postgres_db(conn) -> None:
    """Initializes PostgreSQL schema with BIGINT IDs, SERIAL primary keys, and seed data."""
    logger.info("🐘 [Database] Initializing PostgreSQL / Supabase schema and seeding tables...")
    cursor = conn.cursor()

    # Users table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance BIGINT DEFAULT 0,
            language TEXT DEFAULT 'my',
            referred_by BIGINT,
            referral_earnings BIGINT DEFAULT 0,
            referral_reward_paid INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Admins table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Products table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            price BIGINT NOT NULL,
            category TEXT DEFAULT 'Digital',
            store_id INTEGER DEFAULT 1
        )
        """
    )

    # Stock table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS stock (
            id SERIAL PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES products(id),
            item_data TEXT NOT NULL,
            is_sold INTEGER DEFAULT 0,
            sold_to_user_id BIGINT,
            sold_at TIMESTAMP,
            store_id INTEGER DEFAULT 1
        )
        """
    )

    # Orders table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            product_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            price BIGINT NOT NULL,
            delivered_data TEXT NOT NULL,
            purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            store_id INTEGER DEFAULT 1
        )
        """
    )

    # Settings table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )

    # Stores table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS stores (
            id SERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL UNIQUE,
            store_name TEXT NOT NULL,
            bot_token TEXT DEFAULT '',
            bot_username TEXT DEFAULT '',
            license_status TEXT DEFAULT 'active',
            license_expiry TEXT NOT NULL,
            plan TEXT DEFAULT 'Pro Business',
            thb_exchange_rate INTEGER DEFAULT 135,
            kpay_phone TEXT DEFAULT '09758511925',
            kpay_name TEXT DEFAULT 'Pyae Phyo Kyaw',
            wpay_phone TEXT DEFAULT '09779737113',
            wpay_name TEXT DEFAULT 'Sandar Kyaw',
            thai_bank_info TEXT DEFAULT 'Kasikorn Bank (KBank) - 123-4-56789-0 (Pyae Phyo Kyaw)',
            promptpay_phone TEXT DEFAULT '09758511925',
            promptpay_name TEXT DEFAULT 'Pyae Phyo Kyaw',
            topup_instructions TEXT DEFAULT '',
            qr_file_id TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Store requests table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS store_requests (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            username TEXT,
            first_name TEXT,
            store_name TEXT NOT NULL,
            plan TEXT NOT NULL,
            slip_file_id TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Store users association table (with strictly isolated per-store balance)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS store_users (
            store_id INTEGER NOT NULL,
            user_id BIGINT NOT NULL,
            balance BIGINT DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (store_id, user_id)
        )
        """
    )
    cursor.execute("ALTER TABLE store_users ADD COLUMN IF NOT EXISTS balance BIGINT DEFAULT 0")

    # Seed Store #1 (Master Super Admin Store) if none exists
    cursor.execute("SELECT count(*) as count FROM stores WHERE id = 1")
    row = cursor.fetchone()
    if not row or row.get("count", 0) == 0:
        env_token = clean_bot_token(os.getenv("TELEGRAM_BOT_TOKEN", "8780863025:AAEy4_1JSDv1nxzaWYCvvE5-n4DGFmWkaPQ"))
        env_super_id_str = os.getenv("SAAS_SUPER_ADMIN_IDS", "1920334929").split(",")[0].strip()
        env_super_id = int(env_super_id_str) if env_super_id_str.isdigit() else 1920334929
        env_store_name = os.getenv("STORE_NAME", "Latt Swal Store")
        env_bot_username = os.getenv("BOT_USERNAME", "@latt_swalshop_bot")
        env_kpay_phone = os.getenv("KPAY_PHONE", "09758511925")
        env_kpay_name = os.getenv("KPAY_NAME", "Pyae Phyo Kyaw")
        env_wpay_phone = os.getenv("WPAY_PHONE", "09779737113")
        env_wpay_name = os.getenv("WPAY_NAME", "Sandar Kyaw")
        env_thai_bank = os.getenv("THAI_BANK_INFO", "Kasikorn Bank (KBank) - 123-4-56789-0 (Pyae Phyo Kyaw)")
        env_promptpay_phone = os.getenv("PROMPTPAY_PHONE", "09758511925")
        env_promptpay_name = os.getenv("PROMPTPAY_NAME", "Pyae Phyo Kyaw")

        cursor.execute(
            """
            INSERT INTO stores (
                id, owner_id, store_name, bot_token, bot_username,
                license_status, license_expiry, plan, thb_exchange_rate,
                kpay_phone, kpay_name, wpay_phone, wpay_name,
                thai_bank_info, promptpay_phone, promptpay_name
            ) VALUES (
                1, %s, %s, %s, %s,
                'active', '2099-12-31 23:59:59', 'Master Enterprise', 135,
                %s, %s, %s, %s,
                %s, %s, %s
            ) ON CONFLICT (id) DO NOTHING
            """,
            (
                env_super_id, env_store_name, env_token, env_bot_username,
                env_kpay_phone, env_kpay_name, env_wpay_phone, env_wpay_name,
                env_thai_bank, env_promptpay_phone, env_promptpay_name
            )
        )
        cursor.execute("INSERT INTO admins (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (env_super_id,))

    # Seed default settings
    default_settings = {
        "store_name": os.getenv("STORE_NAME", "Latt Swal Store"),
        "admin_username": os.getenv("ADMIN_USERNAME", "latt_swal2026"),
        "thb_exchange_rate": os.getenv("THB_EXCHANGE_RATE", "135"),
        "kpay_phone": os.getenv("KPAY_PHONE", "09758511925"),
        "kpay_name": os.getenv("KPAY_NAME", "Pyae Phyo Kyaw"),
        "wpay_phone": os.getenv("WPAY_PHONE", "09779737113"),
        "wpay_name": os.getenv("WPAY_NAME", "Sandar Kyaw"),
        "thai_bank_info": os.getenv("THAI_BANK_INFO", "Kasikorn Bank (KBank) - 123-4-56789-0 (Pyae Phyo Kyaw)"),
        "promptpay_phone": os.getenv("PROMPTPAY_PHONE", "09758511925"),
        "promptpay_name": os.getenv("PROMPTPAY_NAME", "Pyae Phyo Kyaw"),
        "license_status": "active",
        "license_expiry": "2026-12-31 23:59:59",
        "license_plan": "SaaS Monthly Pro",
        "license_key": "SAAS-LATT-SWAL-PRO-2026",
        "license_client_name": "Latt Swal Digital Shop",
    }
    for k, v in default_settings.items():
        cursor.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (k, v))

    # Seed products if empty
    cursor.execute("SELECT count(*) as count FROM products")
    p_row = cursor.fetchone()
    if not p_row or p_row.get("count", 0) == 0:
        sample_products = [
            ("🎬 Netflix Premium (1 Month)", "4K Ultra HD • Private Profile • 1 Month Warranty\nEmail & Password login format.", 8500, "Streaming"),
            ("🎵 Spotify Premium (1 Month)", "Ad-free • High Quality Audio • Offline Downloads\nIndividual Account Invite Link.", 5000, "Music"),
            ("🎨 Canva Pro (Lifetime Invite)", "Brand Kits • Unlimited Cloud Storage • Premium Assets\nTeam invite link.", 4000, "Design"),
            ("⚡ Telegram Premium (3 Months)", "4GB Uploads • Faster Download • Animated Emojis\nGift code redeem link.", 28000, "Telegram"),
            ("🛡️ ExpressVPN Key (1 Month)", "Fast & Secure • Multi-device support\nActivation Code.", 6000, "VPN"),
        ]
        cursor.executemany("INSERT INTO products (name, description, price, category) VALUES (%s, %s, %s, %s)", sample_products)

        stock_items = [
            (1, "Email: user101@streammail.com\nPass: Netfl!x#2026\nProfile: PIN 1234"),
            (1, "Email: user102@streammail.com\nPass: Netfl!x#2026\nProfile: PIN 5678"),
            (2, "Invite Link: https://www.spotify.com/invite/sample-token-abc123"),
            (3, "Invite Link: https://www.canva.com/brand/join?token=canva-pro-sample-789"),
            (4, "Gift Link: https://t.me/giftcode/SAMPLE-TG-PREM-CODE-XYZ"),
            (5, "Activation Key: EVPN-9982-1143-6672-ABCD"),
        ]
        cursor.executemany("INSERT INTO stock (product_id, item_data) VALUES (%s, %s)", stock_items)

    conn.commit()

    # Automatic one-time migration from local SQLite if Postgres database has no extra stores
    if os.path.exists(DB_FILE):
        _auto_migrate_sqlite_to_postgres(DB_FILE, conn)


def _auto_migrate_sqlite_to_postgres(sqlite_path: str, pg_conn) -> None:
    """Migrates existing tenant stores from local SQLite into fresh PostgreSQL database."""
    try:
        pg_cursor = pg_conn.cursor()
        pg_cursor.execute("SELECT count(*) as count FROM stores")
        res = pg_cursor.fetchone()
        pg_store_count = res.get("count", 0) if res else 0

        # Only run migration if PostgreSQL has at most 1 store (just initialized master store)
        if pg_store_count <= 1:
            logger.info("📦 [Database Migration] Checking for existing tenant stores in local SQLite to import...")
            import sqlite3 as local_sql
            with local_sql.connect(sqlite_path) as s_conn:
                s_conn.row_factory = local_sql.Row
                s_cur = s_conn.cursor()
                s_cur.execute("SELECT * FROM stores WHERE id > 1")
                extra_stores = s_cur.fetchall()
                if extra_stores:
                    logger.info(f"🚚 [Database Migration] Importing {len(extra_stores)} tenant store(s) from SQLite to PostgreSQL...")
                    for es in extra_stores:
                        es_d = dict(es)
                        pg_cursor.execute(
                            """
                            INSERT INTO stores (
                                id, owner_id, store_name, bot_token, bot_username, license_status, license_expiry,
                                plan, thb_exchange_rate, kpay_phone, kpay_name, wpay_phone, wpay_name,
                                thai_bank_info, promptpay_phone, promptpay_name, topup_instructions, qr_file_id
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s
                            ) ON CONFLICT (id) DO UPDATE SET
                                bot_token = EXCLUDED.bot_token,
                                bot_username = EXCLUDED.bot_username
                            """,
                            (
                                es_d["id"], es_d["owner_id"], es_d["store_name"], es_d.get("bot_token", ""),
                                es_d.get("bot_username", ""), es_d.get("license_status", "active"), es_d.get("license_expiry", "2026-12-31"),
                                es_d.get("plan", "Pro Business"), es_d.get("thb_exchange_rate", 135), es_d.get("kpay_phone", ""),
                                es_d.get("kpay_name", ""), es_d.get("wpay_phone", ""), es_d.get("wpay_name", ""),
                                es_d.get("thai_bank_info", ""), es_d.get("promptpay_phone", ""), es_d.get("promptpay_name", ""),
                                es_d.get("topup_instructions", ""), es_d.get("qr_file_id", "")
                            )
                        )
                        pg_cursor.execute("INSERT INTO admins (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (es_d["owner_id"],))

                    pg_conn.commit()
                    logger.info("✅ [Database Migration] Successfully migrated all tenant stores to PostgreSQL!")
    except Exception as me:
        logger.warning(f"⚠️ [Database Migration] Notice during SQLite-to-PostgreSQL migration: {me}")


def init_db() -> None:
    """Initializes tables and seeds initial products and stock if empty."""
    if is_postgres():
        with get_connection() as conn:
            _init_postgres_db(conn)
        return

    # SQLite Database Initialization
    with get_connection() as conn:
        cursor = conn.cursor()

        # Users table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance INTEGER DEFAULT 0,
                language TEXT DEFAULT 'my',
                referred_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Migrate existing users table if needed
        cursor.execute("PRAGMA table_info(users)")
        existing_cols = [c["name"] for c in cursor.fetchall()]
        if "language" not in existing_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'my'")
        if "referred_by" not in existing_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
        if "referral_earnings" not in existing_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN referral_earnings INTEGER DEFAULT 0")
        if "referral_reward_paid" not in existing_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN referral_reward_paid INTEGER DEFAULT 0")

        # Admins table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Products table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                price INTEGER NOT NULL,
                category TEXT DEFAULT 'Digital'
            )
            """
        )

        # Stock table (credentials, accounts, keys, activation links)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS stock (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                item_data TEXT NOT NULL,
                is_sold INTEGER DEFAULT 0,
                sold_to_user_id INTEGER,
                sold_at TIMESTAMP,
                FOREIGN KEY (product_id) REFERENCES products(id)
            )
            """
        )

        # Orders / Purchase history table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                price INTEGER NOT NULL,
                delivered_data TEXT NOT NULL,
                purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
            """
        )

        # Settings table (for currency exchange rate, payment details, store configs)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

        # Multi-tenant Stores Table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS stores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL UNIQUE,
                store_name TEXT NOT NULL,
                bot_token TEXT DEFAULT '',
                bot_username TEXT DEFAULT '',
                license_status TEXT DEFAULT 'active',
                license_expiry TEXT NOT NULL,
                plan TEXT DEFAULT 'Pro Business',
                thb_exchange_rate INTEGER DEFAULT 135,
                kpay_phone TEXT DEFAULT '09758511925',
                kpay_name TEXT DEFAULT 'Pyae Phyo Kyaw',
                wpay_phone TEXT DEFAULT '09779737113',
                wpay_name TEXT DEFAULT 'Sandar Kyaw',
                thai_bank_info TEXT DEFAULT 'Kasikorn Bank (KBank) - 123-4-56789-0 (Pyae Phyo Kyaw)',
                promptpay_phone TEXT DEFAULT '09758511925',
                promptpay_name TEXT DEFAULT 'Pyae Phyo Kyaw',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Store Rental / Purchase Applications Table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS store_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                store_name TEXT NOT NULL,
                plan TEXT NOT NULL,
                slip_file_id TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Store Users Association Table (for strict multi-tenant customer and balance isolation)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS store_users (
                store_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                balance INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (store_id, user_id)
            )
            """
        )
        cursor.execute("PRAGMA table_info(store_users)")
        su_cols = [c["name"] for c in cursor.fetchall()]
        if "balance" not in su_cols:
            cursor.execute("ALTER TABLE store_users ADD COLUMN balance INTEGER DEFAULT 0")
            cursor.execute("UPDATE store_users SET balance = (SELECT balance FROM users WHERE users.user_id = store_users.user_id) WHERE store_id = 1")
            conn.commit()

        # Migration: Add store_id column to products, stock, orders if not present
        for tbl in ['products', 'stock', 'orders']:
            cursor.execute(f"PRAGMA table_info({tbl})")
            cols = [c["name"] for c in cursor.fetchall()]
            if "store_id" not in cols:
                cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN store_id INTEGER DEFAULT 1")

        # Migration: Add topup_instructions and qr_file_id columns to stores if not present
        cursor.execute("PRAGMA table_info(stores)")
        store_cols = [c["name"] for c in cursor.fetchall()]
        if "topup_instructions" not in store_cols:
            cursor.execute("ALTER TABLE stores ADD COLUMN topup_instructions TEXT DEFAULT ''")
        if "qr_file_id" not in store_cols:
            cursor.execute("ALTER TABLE stores ADD COLUMN qr_file_id TEXT DEFAULT ''")

        # Seed Store #1 (Master Super Admin Store) if none exists
        cursor.execute("SELECT COUNT(*) as count FROM stores WHERE id = 1")
        if cursor.fetchone()["count"] == 0:
            env_token = os.getenv("TELEGRAM_BOT_TOKEN", "8780863025:AAEy4_1JSDv1nxzaWYCvvE5-n4DGFmWkaPQ").strip()
            env_super_id_str = os.getenv("SAAS_SUPER_ADMIN_IDS", "1920334929").split(",")[0].strip()
            env_super_id = int(env_super_id_str) if env_super_id_str.isdigit() else 1920334929
            env_store_name = os.getenv("STORE_NAME", "Latt Swal Store")
            env_bot_username = os.getenv("BOT_USERNAME", "@latt_swalshop_bot")
            env_kpay_phone = os.getenv("KPAY_PHONE", "09758511925")
            env_kpay_name = os.getenv("KPAY_NAME", "Pyae Phyo Kyaw")
            env_wpay_phone = os.getenv("WPAY_PHONE", "09779737113")
            env_wpay_name = os.getenv("WPAY_NAME", "Sandar Kyaw")
            env_thai_bank = os.getenv("THAI_BANK_INFO", "Kasikorn Bank (KBank) - 123-4-56789-0 (Pyae Phyo Kyaw)")
            env_promptpay_phone = os.getenv("PROMPTPAY_PHONE", "09758511925")
            env_promptpay_name = os.getenv("PROMPTPAY_NAME", "Pyae Phyo Kyaw")

            cursor.execute(
                """
                INSERT INTO stores (
                    id, owner_id, store_name, bot_token, bot_username,
                    license_status, license_expiry, plan, thb_exchange_rate,
                    kpay_phone, kpay_name, wpay_phone, wpay_name,
                    thai_bank_info, promptpay_phone, promptpay_name
                ) VALUES (
                    1, ?, ?, ?, ?,
                    'active', '2099-12-31 23:59:59', 'Master Enterprise', 135,
                    ?, ?, ?, ?,
                    ?, ?, ?
                )
                """,
                (
                    env_super_id, env_store_name, env_token, env_bot_username,
                    env_kpay_phone, env_kpay_name, env_wpay_phone, env_wpay_name,
                    env_thai_bank, env_promptpay_phone, env_promptpay_name
                )
            )

        default_settings = {
            "store_name": os.getenv("STORE_NAME", "Latt Swal Store"),
            "admin_username": os.getenv("ADMIN_USERNAME", "latt_swal2026"),
            "thb_exchange_rate": os.getenv("THB_EXCHANGE_RATE", "135"),
            "kpay_phone": os.getenv("KPAY_PHONE", "09758511925"),
            "kpay_name": os.getenv("KPAY_NAME", "Pyae Phyo Kyaw"),
            "wpay_phone": os.getenv("WPAY_PHONE", "09779737113"),
            "wpay_name": os.getenv("WPAY_NAME", "Sandar Kyaw"),
            "thai_bank_info": os.getenv("THAI_BANK_INFO", "Kasikorn Bank (KBank) - 123-4-56789-0 (Pyae Phyo Kyaw)"),
            "promptpay_phone": os.getenv("PROMPTPAY_PHONE", "09758511925"),
            "promptpay_name": os.getenv("PROMPTPAY_NAME", "Pyae Phyo Kyaw"),
            "license_status": "active",
            "license_expiry": "2026-12-31 23:59:59",
            "license_plan": "SaaS Monthly Pro",
            "license_key": "SAAS-LATT-SWAL-PRO-2026",
            "license_client_name": "Latt Swal Digital Shop",
        }
        for k, v in default_settings.items():
            cursor.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )

        # Seed initial products if none exist
        cursor.execute("SELECT COUNT(*) as count FROM products")
        if cursor.fetchone()["count"] == 0:
            sample_products = [
                (
                    "🎬 Netflix Premium (1 Month)",
                    "4K Ultra HD • Private Profile • 1 Month Warranty\nEmail & Password login format.",
                    8500,
                    "Streaming",
                ),
                (
                    "🎵 Spotify Premium (1 Month)",
                    "Ad-free • High Quality Audio • Offline Downloads\nIndividual Account Invite Link.",
                    5000,
                    "Music",
                ),
                (
                    "🎨 Canva Pro (Lifetime Invite)",
                    "Brand Kits • Unlimited Cloud Storage • Premium Assets\nTeam invite link.",
                    4000,
                    "Design",
                ),
                (
                    "⚡ Telegram Premium (3 Months)",
                    "4GB Uploads • Faster Download • Animated Emojis\nGift code redeem link.",
                    28000,
                    "Telegram",
                ),
                (
                    "🛡️ ExpressVPN Key (1 Month)",
                    "Fast & Secure • Multi-device support\nActivation Code.",
                    6000,
                    "VPN",
                ),
            ]

            cursor.executemany(
                "INSERT INTO products (name, description, price, category) VALUES (?, ?, ?, ?)",
                sample_products,
            )

            # Seed sample stock for each product
            stock_items = [
                # Netflix
                (1, "Email: user101@streammail.com\nPass: Netfl!x#2026\nProfile: PIN 1234"),
                (1, "Email: user102@streammail.com\nPass: Netfl!x#2026\nProfile: PIN 5678"),
                (1, "Email: user103@streammail.com\nPass: Netfl!x#2026\nProfile: PIN 9988"),
                # Spotify
                (2, "Invite Link: https://www.spotify.com/invite/sample-token-abc123"),
                (2, "Invite Link: https://www.spotify.com/invite/sample-token-def456"),
                # Canva
                (3, "Invite Link: https://www.canva.com/brand/join?token=canva-pro-sample-789"),
                (3, "Invite Link: https://www.canva.com/brand/join?token=canva-pro-sample-999"),
                # Telegram Premium
                (4, "Gift Link: https://t.me/giftcode/SAMPLE-TG-PREM-CODE-XYZ"),
                # ExpressVPN
                (5, "Activation Key: EVPN-9982-1143-6672-ABCD"),
                (5, "Activation Key: EVPN-7721-3312-9901-WXYZ"),
            ]

            cursor.executemany(
                "INSERT INTO stock (product_id, item_data) VALUES (?, ?)",
                stock_items,
            )

        conn.commit()


# ==============================================================================
# Admin Functions
# ==============================================================================

def add_admin(user_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
        conn.commit()
        return True


def remove_admin(user_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        conn.commit()
        return True


def get_all_admin_ids() -> List[int]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM admins UNION SELECT owner_id as user_id FROM stores WHERE owner_id IS NOT NULL")
        return [row["user_id"] for row in cursor.fetchall()]


def is_admin(user_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None


# ==============================================================================
# User Functions
# ==============================================================================

def get_or_create_user(
    user_id: int,
    username: Optional[str],
    first_name: Optional[str],
    referrer_id: Optional[int] = None,
    store_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Retrieves an existing user or registers a new user with strictly isolated per-store balance (initial 0 MMK for new tenant stores)."""
    if store_id is None:
        store_id = current_store_id_ctx.get(1)

    with get_connection() as conn:
        cursor = conn.cursor()
        # 1. Ensure user record exists in users table (stores global identity: username, first_name, language)
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user_row = cursor.fetchone()
        if not user_row:
            ref_id = referrer_id if (referrer_id and referrer_id != user_id) else None
            cursor.execute(
                "INSERT INTO users (user_id, username, first_name, balance, language, referred_by, referral_earnings, referral_reward_paid) VALUES (?, ?, ?, 0, 'my', ?, 0, 0)",
                (user_id, username or "", first_name or "", ref_id),
            )
        else:
            cursor.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                (username or "", first_name or "", user_id),
            )

        # 2. Check per-store customer record in store_users table
        cursor.execute(
            "SELECT balance FROM store_users WHERE store_id = ? AND user_id = ?",
            (store_id, user_id),
        )
        su_row = cursor.fetchone()
        if not su_row:
            # Brand-new customer for THIS tenant bot: initial balance starts strictly at 0 MMK
            cursor.execute(
                "INSERT INTO store_users (store_id, user_id, balance) VALUES (?, ?, 0)",
                (store_id, user_id),
            )
            store_balance = 0
        else:
            store_balance = su_row["balance"] if su_row["balance"] is not None else 0

        conn.commit()

        # 3. Retrieve user record with strictly isolated store balance
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        res = dict(cursor.fetchone())
        res["balance"] = store_balance
        res["store_id"] = store_id
        return res


def get_user(user_id: int, store_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Retrieves user info with isolated store balance."""
    if store_id is None:
        store_id = current_store_id_ctx.get(1)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        cursor.execute(
            "SELECT balance FROM store_users WHERE store_id = ? AND user_id = ?",
            (store_id, user_id),
        )
        su_row = cursor.fetchone()
        if su_row and su_row["balance"] is not None:
            res["balance"] = su_row["balance"]
        else:
            if store_id == 1:
                res["balance"] = row["balance"] or 0
            else:
                res["balance"] = 0
        res["store_id"] = store_id
        return res


def get_user_by_username(username: str, store_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Retrieves user row by username (case-insensitive) with isolated store balance."""
    clean = username.lstrip("@").strip()
    if not clean:
        return None
    if store_id is None:
        store_id = current_store_id_ctx.get(1)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?) LIMIT 1", (clean,))
        row = cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        cursor.execute(
            "SELECT balance FROM store_users WHERE store_id = ? AND user_id = ?",
            (store_id, res["user_id"]),
        )
        su_row = cursor.fetchone()
        if su_row and su_row["balance"] is not None:
            res["balance"] = su_row["balance"]
        else:
            if store_id == 1:
                res["balance"] = row["balance"] or 0
            else:
                res["balance"] = 0
        res["store_id"] = store_id
        return res


def get_all_users(store_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Returns all users, or filtered strictly by store_id with their isolated store balance for store admins."""
    with get_connection() as conn:
        cursor = conn.cursor()
        if store_id and store_id != 1:
            cursor.execute(
                """
                SELECT DISTINCT u.user_id, u.username, u.first_name, COALESCE(su.balance, 0) as balance, u.language, u.referred_by, u.created_at
                FROM users u
                JOIN store_users su ON u.user_id = su.user_id AND su.store_id = ?
                ORDER BY su.created_at DESC
                """,
                (store_id,),
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        elif store_id == 1:
            cursor.execute(
                """
                SELECT u.user_id, u.username, u.first_name, COALESCE(su.balance, u.balance) as balance, u.language, u.referred_by, u.created_at
                FROM users u
                LEFT JOIN store_users su ON u.user_id = su.user_id AND su.store_id = 1
                ORDER BY u.created_at DESC
                """
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        else:
            cursor.execute("SELECT * FROM users ORDER BY created_at DESC")
            return [dict(row) for row in cursor.fetchall()]


def add_user_balance(user_id: int, amount: int, store_id: Optional[int] = None) -> int:
    """Adds balance to user for a specific store and returns the updated balance."""
    if store_id is None:
        store_id = current_store_id_ctx.get(1)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM store_users WHERE store_id = ? AND user_id = ?", (store_id, user_id))
        row = cursor.fetchone()
        if row is None:
            new_bal = max(0, amount)
            cursor.execute(
                "INSERT INTO store_users (store_id, user_id, balance) VALUES (?, ?, ?)",
                (store_id, user_id, new_bal),
            )
        else:
            curr_bal = row["balance"] or 0
            new_bal = curr_bal + amount
            cursor.execute(
                "UPDATE store_users SET balance = ? WHERE store_id = ? AND user_id = ?",
                (new_bal, store_id, user_id),
            )
        if store_id == 1:
            cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()
        return new_bal


def set_user_balance(user_id: int, new_balance: int, store_id: Optional[int] = None) -> int:
    """Sets a user's wallet balance directly to an exact amount for a specific store."""
    clean_balance = max(0, int(new_balance))
    if store_id is None:
        store_id = current_store_id_ctx.get(1)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM store_users WHERE store_id = ? AND user_id = ?", (store_id, user_id))
        if cursor.fetchone() is None:
            cursor.execute(
                "INSERT INTO store_users (store_id, user_id, balance) VALUES (?, ?, ?)",
                (store_id, user_id, clean_balance),
            )
        else:
            cursor.execute(
                "UPDATE store_users SET balance = ? WHERE store_id = ? AND user_id = ?",
                (clean_balance, store_id, user_id),
            )
        if store_id == 1:
            cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (clean_balance, user_id))
        conn.commit()
        return clean_balance


def deduct_user_balance(user_id: int, amount: int, store_id: Optional[int] = None) -> int:
    """Deducts an amount from user's wallet balance for a specific store without dropping below 0."""
    if store_id is None:
        store_id = current_store_id_ctx.get(1)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM store_users WHERE store_id = ? AND user_id = ?", (store_id, user_id))
        row = cursor.fetchone()
        if not row:
            return 0
        curr_bal = row["balance"] or 0
        new_bal = max(0, curr_bal - int(amount))
        cursor.execute(
            "UPDATE store_users SET balance = ? WHERE store_id = ? AND user_id = ?",
            (new_bal, store_id, user_id),
        )
        if store_id == 1:
            cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_bal, user_id))
        conn.commit()
        return new_bal



def is_user_in_store(user_id: int, store_id: int) -> bool:
    """Checks whether a user is linked to a specific store (registered, purchased, or store owner)."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT 1 FROM store_users WHERE store_id = ? AND user_id = ?
            UNION
            SELECT 1 FROM orders WHERE store_id = ? AND user_id = ?
            UNION
            SELECT 1 FROM stores WHERE id = ? AND owner_id = ?
            LIMIT 1
            """,
            (store_id, user_id, store_id, user_id, store_id, user_id),
        )
        row = cursor.fetchone()
        if row is not None:
            return True
        # For Store 1 fallback if store_users hasn't backfilled, check if user exists in users table
        if store_id == 1:
            cursor.execute("SELECT 1 FROM users WHERE user_id = ? LIMIT 1", (user_id,))
            return cursor.fetchone() is not None
        return False



# ==============================================================================
# Products & Stock Functions
# ==============================================================================

def get_products_with_stock(store_id: int = 1) -> List[Dict[str, Any]]:
    """Returns all products for a specific store with their current available stock count."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT p.id, p.name, p.description, p.price, p.category, p.store_id,
                   COUNT(CASE WHEN s.is_sold = 0 THEN 1 END) as stock_count
            FROM products p
            LEFT JOIN stock s ON p.id = s.product_id
            WHERE p.store_id = ?
            GROUP BY p.id
            ORDER BY p.id ASC
            """,
            (store_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


def get_product(product_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT p.id, p.name, p.description, p.price, p.category, p.store_id,
                   COUNT(CASE WHEN s.is_sold = 0 THEN 1 END) as stock_count
            FROM products p
            LEFT JOIN stock s ON p.id = s.product_id
            WHERE p.id = ?
            GROUP BY p.id
            """,
            (product_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def add_stock(product_id: int, item_data: str, store_id: int = 1) -> bool:
    """Adds a new stock credential or key for a product."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO stock (product_id, item_data, store_id) VALUES (?, ?, ?)",
            (product_id, item_data.strip(), store_id),
        )
        conn.commit()
        return True


def get_product_stock_items(product_id: int) -> List[Dict[str, Any]]:
    """Returns all stock credentials for a product (unsold first, then sold)."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, product_id, item_data, is_sold, sold_to_user_id, sold_at
            FROM stock
            WHERE product_id = ?
            ORDER BY is_sold ASC, id ASC
            """,
            (product_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


def get_all_available_stocks(store_id: int = 1) -> List[Dict[str, Any]]:
    """Returns all available unsold stock items with product names for a specific store."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT s.id, s.product_id, p.name as product_name, s.item_data
            FROM stock s
            JOIN products p ON s.product_id = p.id
            WHERE s.is_sold = 0 AND p.store_id = ?
            ORDER BY s.product_id ASC, s.id ASC
            """,
            (store_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


def get_stock_by_id(stock_id: int) -> Optional[Dict[str, Any]]:
    """Fetches a specific stock item with product name by ID."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT s.id, s.product_id, s.item_data, s.is_sold, s.sold_to_user_id, s.sold_at, p.name as product_name, p.store_id
            FROM stock s
            JOIN products p ON s.product_id = p.id
            WHERE s.id = ?
            """,
            (stock_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def update_stock_data(stock_id: int, new_item_data: str) -> bool:
    """Updates the credentials (mail/password/key) of an existing stock item."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE stock SET item_data = ? WHERE id = ?",
            (new_item_data.strip(), stock_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_stock(stock_id: int) -> bool:
    """Deletes a stock item if unsold."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM stock WHERE id = ? AND is_sold = 0", (stock_id,))
        conn.commit()
        return cursor.rowcount > 0


def add_product(name: str, description: str, price: int, category: str = "Digital", store_id: int = 1) -> int:
    """Adds a new product to the catalog scoped by store_id."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO products (name, description, price, category, store_id) VALUES (?, ?, ?, ?, ?)",
            (name, description, price, category, store_id),
        )
        conn.commit()
        return cursor.lastrowid


def delete_product(product_id: int) -> bool:
    """Deletes a product and its unsold stock from the database."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM stock WHERE product_id = ? AND is_sold = 0", (product_id,))
        cursor.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()
        return cursor.rowcount > 0


def update_product_price(product_id: int, new_price: int) -> bool:
    """Updates product price."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE products SET price = ? WHERE id = ?", (new_price, product_id))
        conn.commit()
        return cursor.rowcount > 0


def update_product_name(product_id: int, new_name: str) -> bool:
    """Updates product name."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE products SET name = ? WHERE id = ?", (new_name.strip(), product_id))
        conn.commit()
        return cursor.rowcount > 0


def purchase_product(
    user_id: int, product_id: int, store_id: Optional[int] = None
) -> Tuple[bool, str, Optional[str], int, str, int]:
    """
    Handles auto-delivery transaction atomically:
    1. Checks user balance for the specific store.
    2. Checks product stock.
    3. Deducts isolated store balance.
    4. Marks 1 stock item as sold.
    5. Saves order record.
    Returns: (success: bool, message: str, delivered_data: Optional[str], remaining_stock: int, product_name: str, price: int)
    """
    with get_connection() as conn:
        cursor = conn.cursor()

        # Fetch user
        cursor.execute("SELECT user_id, balance, referred_by, referral_reward_paid FROM users WHERE user_id = ?", (user_id,))
        user_row = cursor.fetchone()
        if not user_row:
            return False, "⚠️ အကောင့်ရှာမတွေ့ပါ /start ဖြင့် ပြန်လည်စတင်ပါ။", None, 0, "", 0

        # Fetch product
        cursor.execute("SELECT * FROM products WHERE id = ?", (product_id,))
        product_row = cursor.fetchone()
        if not product_row:
            return False, "⚠️ ပစ္စည်းရှာမတွေ့ပါ။", None, 0, "", 0

        price = product_row["price"]
        product_name = product_row["name"]
        prod_store_id = product_row["store_id"] if (product_row and "store_id" in product_row.keys()) else (store_id or current_store_id_ctx.get(1))

        # Check isolated balance for this specific store
        cursor.execute("SELECT balance FROM store_users WHERE store_id = ? AND user_id = ?", (prod_store_id, user_id))
        su_row = cursor.fetchone()
        if su_row and su_row["balance"] is not None:
            user_balance = su_row["balance"]
        else:
            if prod_store_id == 1:
                user_balance = user_row["balance"] or 0
            else:
                user_balance = 0

        # Check balance
        if user_balance < price:
            needed = price - user_balance
            return (
                False,
                f"⚠️ *လက်ကျန်ငွေ မလုံလောက်ပါ!*\n\n"
                f"📦 *ပစ္စည်း -* {product_name}\n"
                f"💰 *ကျသင့်ငွေ -* `{price:,} MMK`\n"
                f"💳 *လက်ကျန်ငွေ -* `{user_balance:,} MMK`\n"
                f"❗ *လိုအပ်သောငွေ -* `{needed:,} MMK`\n\n"
                f"ကျေးဇူးပြု၍ အောက်ပါ '💳 ငွေဖြည့်ရန်' ခလုတ်ကို နှိပ်ပြီး ငွေလွှဲပြေစာ ပေးပို့ဖြည့်သွင်းပါ။",
                None,
                0,
                product_name,
                price,
            )

        # Fetch one unsold stock item
        cursor.execute(
            """
            SELECT id, item_data FROM stock
            WHERE product_id = ? AND is_sold = 0
            LIMIT 1
            """,
            (product_id,),
        )
        stock_row = cursor.fetchone()
        if not stock_row:
            return (
                False,
                f"⚠️ ဝမ်းနည်းပါသည်၊ *{product_name}* ပစ္စည်းလက်ကျန် ကုန်သွားပါပြီ။ Admin မှ မကြာမီ ထပ်ဖြည့်ပေးပါမည်။",
                None,
                0,
                product_name,
                price,
            )

        stock_id = stock_row["id"]
        item_data = stock_row["item_data"]
        now = datetime.now()

        # Deduct balance from store_users
        cursor.execute(
            "UPDATE store_users SET balance = balance - ? WHERE store_id = ? AND user_id = ?",
            (price, prod_store_id, user_id),
        )
        if prod_store_id == 1:
            cursor.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ?",
                (price, user_id),
            )

        # Mark stock as sold
        cursor.execute(
            """
            UPDATE stock
            SET is_sold = 1, sold_to_user_id = ?, sold_at = ?
            WHERE id = ?
            """,
            (user_id, now, stock_id),
        )

        # Record order with product's store_id
        prod_store_id = product_row["store_id"] if (product_row and "store_id" in product_row.keys()) else 1
        cursor.execute(
            """
            INSERT INTO orders (user_id, product_id, product_name, price, delivered_data, purchased_at, store_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, product_id, product_name, price, item_data, now, prod_store_id),
        )

        # Check remaining stock count
        cursor.execute(
            "SELECT COUNT(*) as count FROM stock WHERE product_id = ? AND is_sold = 0",
            (product_id,),
        )
        remaining_stock = cursor.fetchone()["count"]

        # Award 1,000 MMK referral bonus if user was referred and hasn't triggered bonus yet
        referrer_id_to_notify = None
        referred_by = user_row["referred_by"]
        reward_paid = user_row["referral_reward_paid"] if "referral_reward_paid" in user_row.keys() else 0

        if referred_by and not reward_paid:
            ref_id = referred_by
            cursor.execute(
                "UPDATE users SET balance = balance + 1000, referral_earnings = referral_earnings + 1000 WHERE user_id = ?",
                (ref_id,),
            )
            cursor.execute(
                "UPDATE users SET referral_reward_paid = 1 WHERE user_id = ?",
                (user_id,),
            )
            referrer_id_to_notify = ref_id

        conn.commit()
        return True, "ဝယ်ယူမှုအောင်မြင်ပါသည်!", item_data, remaining_stock, product_name, price, referrer_id_to_notify


# ==============================================================================
# Orders & Stats Functions
# ==============================================================================

def get_user_orders(user_id: int, limit: int = 5, store_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Fetches recent purchase history for a user, optionally filtered by store_id."""
    with get_connection() as conn:
        cursor = conn.cursor()
        if store_id is not None:
            cursor.execute(
                """
                SELECT id, product_name, price, delivered_data, purchased_at
                FROM orders
                WHERE user_id = ? AND store_id = ?
                ORDER BY purchased_at DESC
                LIMIT ?
                """,
                (user_id, store_id, limit),
            )
        else:
            cursor.execute(
                """
                SELECT id, product_name, price, delivered_data, purchased_at
                FROM orders
                WHERE user_id = ?
                ORDER BY purchased_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
        return [dict(row) for row in cursor.fetchall()]


def get_recent_orders(limit: int = 10, store_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Fetches global recent orders with user details, optionally filtered by store_id."""
    with get_connection() as conn:
        cursor = conn.cursor()
        if store_id is not None:
            cursor.execute(
                """
                SELECT o.id, o.user_id, u.username, u.first_name, o.product_name, o.price, o.purchased_at
                FROM orders o
                LEFT JOIN users u ON o.user_id = u.user_id
                WHERE o.store_id = ?
                ORDER BY o.purchased_at DESC
                LIMIT ?
                """,
                (store_id, limit),
            )
        else:
            cursor.execute(
                """
                SELECT o.id, o.user_id, u.username, u.first_name, o.product_name, o.price, o.purchased_at
                FROM orders o
                LEFT JOIN users u ON o.user_id = u.user_id
                ORDER BY o.purchased_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [dict(row) for row in cursor.fetchall()]


def get_store_stats(store_id: int = 1) -> Dict[str, Any]:
    """Returns store dashboard statistics for the admin panel strictly scoped by store_id."""
    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT COUNT(DISTINCT user_id) as count FROM (
                SELECT user_id FROM store_users WHERE store_id = ?
                UNION
                SELECT user_id FROM orders WHERE store_id = ?
            )
            """,
            (store_id, store_id),
        )
        total_users = cursor.fetchone()["count"]
        if store_id == 1 and total_users == 0:
            cursor.execute("SELECT COUNT(*) as count FROM users")
            total_users = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count, COALESCE(SUM(price), 0) as revenue FROM orders WHERE store_id = ?", (store_id,))
        orders_row = cursor.fetchone()
        total_orders = orders_row["count"]
        total_revenue = orders_row["revenue"]

        cursor.execute("SELECT COUNT(*) as count FROM products WHERE store_id = ?", (store_id,))
        total_products = cursor.fetchone()["count"]

        cursor.execute("""
            SELECT COUNT(*) as count FROM stock s
            JOIN products p ON s.product_id = p.id
            WHERE s.is_sold = 0 AND p.store_id = ?
        """, (store_id,))
        total_stock = cursor.fetchone()["count"]

        return {
            "total_users": total_users,
            "total_orders": total_orders,
            "total_revenue": total_revenue,
            "total_products": total_products,
            "total_stock": total_stock,
        }


def get_monthly_sales_stats(store_id: int = 1) -> List[Dict[str, Any]]:
    """Returns monthly sales breakdown: month, order count, total revenue scoped by store_id."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT 
                COALESCE(strftime('%Y-%m', purchased_at), substr(purchased_at, 1, 7)) as month_str,
                COUNT(*) as order_count,
                COALESCE(SUM(price), 0) as total_sales
            FROM orders
            WHERE store_id = ?
            GROUP BY month_str
            ORDER BY month_str DESC
            """,
            (store_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


# ==============================================================================
# Settings & Exchange Rate Functions
# ==============================================================================

def get_setting(key: str, default: str = "") -> str:
    """Gets a setting value by key."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    """Sets or updates a setting value."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        conn.commit()


def get_store_setting(store_id: int, key: str, default: str = "") -> str:
    """Gets setting for a specific store, falling back to global setting or default."""
    val = get_setting(f"store_{store_id}_{key}", "")
    if val:
        return val
    return get_setting(key, default)


def set_store_setting(store_id: int, key: str, value: str) -> None:
    """Sets a store-specific setting."""
    set_setting(f"store_{store_id}_{key}", value)


def get_exchange_rate(store_id: int = 1) -> int:
    """Returns the current THB to MMK exchange rate for a store (Default: 135 MMK per 1 THB)."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT thb_exchange_rate FROM stores WHERE id = ?", (store_id,))
        row = cursor.fetchone()
        if row and row["thb_exchange_rate"]:
            return int(row["thb_exchange_rate"])
    val = get_setting("thb_exchange_rate", "135")
    try:
        return int(val)
    except ValueError:
        return 135


def set_exchange_rate(rate: int, store_id: int = 1) -> int:
    """Sets a new THB to MMK exchange rate for a store."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE stores SET thb_exchange_rate = ? WHERE id = ?", (rate, store_id))
        conn.commit()
    if store_id == 1:
        set_setting("thb_exchange_rate", str(rate))
    return rate


# ==============================================================================
# Language & Referral Functions
# ==============================================================================

def get_user_language(user_id: int) -> str:
    """Gets user language ('my' or 'en', defaults to 'my')."""
    user = get_user(user_id)
    if user and user.get("language"):
        return user["language"]
    return "my"


def set_user_language(user_id: int, lang: str) -> None:
    """Updates user language preference."""
    if lang not in ("my", "en"):
        lang = "my"
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET language = ? WHERE user_id = ?", (lang, user_id))
        conn.commit()


def get_referral_stats(user_id: int) -> Dict[str, Any]:
    """Returns referral stats: total invited count, total earnings, and list of referred users."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM users WHERE referred_by = ?", (user_id,))
        count = cursor.fetchone()["count"]

        cursor.execute("SELECT referral_earnings FROM users WHERE user_id = ?", (user_id,))
        user_row = cursor.fetchone()
        earnings = user_row["referral_earnings"] if (user_row and "referral_earnings" in user_row.keys()) else 0

        cursor.execute(
            "SELECT user_id, username, first_name, created_at FROM users WHERE referred_by = ? ORDER BY created_at DESC LIMIT 10",
            (user_id,),
        )
        invited_users = [dict(r) for r in cursor.fetchall()]

        return {
            "total_referred": count,
            "referral_earnings": earnings or 0,
            "invited_users": invited_users,
        }


# ==============================================================================
# SaaS Store Customization & Multi-tenant Licensing Functions
# ==============================================================================

def get_store_name(store_id: int = 1) -> str:
    """Gets the dynamically configured store name."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT store_name FROM stores WHERE id = ?", (store_id,))
        row = cursor.fetchone()
        if row and row["store_name"]:
            return row["store_name"]
    if store_id == 1:
        return get_setting("store_name", "Digital Store")
    return f"Store #{store_id}"


def set_store_name(name: str, store_id: int = 1) -> str:
    """Updates the store name dynamically in the database for the given store."""
    name = name.strip()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE stores SET store_name = ? WHERE id = ?", (name, store_id))
        conn.commit()
    if store_id == 1:
        set_setting("store_name", name)
    return name


def get_store_topup_instructions(store_id: int = 1) -> str:
    """Gets custom top-up instructions text for a store."""
    return get_store_setting(store_id, "topup_instructions", "")


def set_store_topup_instructions(store_id: int, text: str) -> None:
    """Sets custom top-up instructions text for a store."""
    set_store_setting(store_id, "topup_instructions", text.strip())


def get_store_qr_file(store_id: int = 1) -> str:
    """Gets the payment QR file_id or status ('none', 'removed', telegram file_id, or empty). Scoped strictly per store."""
    if store_id == 1:
        return get_setting("qr_file_id", "")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT qr_file_id FROM stores WHERE id = ?", (store_id,))
        row = cursor.fetchone()
        if row and row["qr_file_id"]:
            return row["qr_file_id"]
    return get_setting(f"store_{store_id}_qr_file_id", "")


def set_store_qr_file(store_id: int, file_id: str) -> None:
    """Sets payment QR file_id or marks as 'none'/'removed' strictly scoped per store."""
    clean_val = file_id.strip()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE stores SET qr_file_id = ? WHERE id = ?", (clean_val, store_id))
        conn.commit()
    set_setting(f"store_{store_id}_qr_file_id", clean_val)
    if store_id == 1:
        set_setting("qr_file_id", clean_val)


def get_admin_username() -> str:
    """Gets the configured admin contact username."""
    return get_setting("admin_username", "latt_swal2026")


def set_admin_username(username: str) -> str:
    """Updates the admin contact username dynamically."""
    username = username.strip().lstrip("@")
    set_setting("admin_username", username)
    return username


def get_license_info() -> Dict[str, Any]:
    """
    Fetches the global / default SaaS license status and subscription validity.
    """
    return get_store_license(1)


def set_license_expiry(expiry_date_str: str) -> Dict[str, Any]:
    """Sets a specific expiry date string for Store #1."""
    expiry_date_str = expiry_date_str.strip()
    if len(expiry_date_str) == 10:
        expiry_date_str += " 23:59:59"
    update_store(1, license_expiry=expiry_date_str, license_status="active")
    set_setting("license_expiry", expiry_date_str)
    set_setting("license_status", "active")
    return get_store_license(1)


def extend_license(days: int = 30) -> Dict[str, Any]:
    """Extends Store #1 license by X days."""
    res = extend_store_license(1, days)
    set_setting("license_expiry", res.get("expiry_full", ""))
    set_setting("license_status", "active")
    return res


def set_license_status(status: str) -> None:
    """Updates license status ('active', 'suspended', 'expired') for Store #1."""
    set_store_license_status(1, status)
    set_setting("license_status", status.strip().lower())


def kill_switch() -> Dict[str, Any]:
    """Instantly suspends Store #1."""
    set_store_license_status(1, "suspended")
    set_setting("license_status", "suspended")
    return get_store_license(1)


def activate_license(days: int = 30) -> Dict[str, Any]:
    """Reactivates or initializes Store #1."""
    return extend_store_license(1, days)


# ==============================================================================
# Multi-tenant Stores & Store Applications Management
# ==============================================================================

def get_store(store_id: int) -> Optional[Dict[str, Any]]:
    """Fetches store by ID with owner details."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT s.*, u.username as owner_username, u.first_name as owner_first_name
            FROM stores s
            LEFT JOIN users u ON s.owner_id = u.user_id
            WHERE s.id = ?
            """,
            (store_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_store_by_owner(owner_id: int) -> Optional[Dict[str, Any]]:
    """Fetches store by owner_id."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM stores WHERE owner_id = ?", (owner_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_store_by_token(token: str) -> Optional[Dict[str, Any]]:
    """Fetches store by bot_token."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM stores WHERE bot_token = ?", (token,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_store_by_bot_username(username: str) -> Optional[Dict[str, Any]]:
    """Fetches store by bot_username (@username or username)."""
    uname = username.strip().lstrip("@")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM stores WHERE REPLACE(bot_username, '@', '') = ? COLLATE NOCASE",
            (uname,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_user_store_id(user_id: int) -> Optional[int]:
    """Fetches the store_id the user is registered under in store_users table."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT store_id FROM store_users WHERE user_id = ? ORDER BY rowid DESC LIMIT 1",
            (user_id,),
        )
        row = cursor.fetchone()
        return row["store_id"] if row else None


def get_all_stores() -> List[Dict[str, Any]]:
    """Returns all tenant stores with owner details."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT s.*, u.username as owner_username, u.first_name as owner_first_name
            FROM stores s
            LEFT JOIN users u ON s.owner_id = u.user_id
            ORDER BY s.id ASC
            """
        )
        return [dict(row) for row in cursor.fetchall()]


def clean_bot_token(token: Optional[str]) -> str:
    """Cleans and sanitizes a Telegram bot token by stripping whitespace, newlines, tabs, and non-printable ASCII characters."""
    if not token:
        return ""
    import re
    s = str(token).strip().strip("'\"`")
    # Remove all whitespace characters (\r, \n, \t, spaces) and non-printable control characters
    s = re.sub(r"[\s\x00-\x1f\x7f-\x9f]", "", s)
    return s


def create_store(
    owner_id: int,
    store_name: str,
    plan: str = "Pro Business",
    days: int = 30,
    bot_token: str = "",
    bot_username: str = "",
) -> int:
    """Creates a new store or renews/updates existing store for the owner."""
    bot_token = clean_bot_token(bot_token)
    bot_username = bot_username.strip() if bot_username else ""
    expiry_dt = datetime.now() + timedelta(days=days)
    expiry_str = expiry_dt.strftime("%Y-%m-%d 23:59:59")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM stores WHERE owner_id = ?", (owner_id,))
        existing = cursor.fetchone()
        if existing:
            store_id = existing["id"]
            cursor.execute(
                """
                UPDATE stores
                SET store_name = ?, plan = ?, license_expiry = ?, license_status = 'active',
                    bot_token = CASE WHEN ? != '' THEN ? ELSE bot_token END,
                    bot_username = CASE WHEN ? != '' THEN ? ELSE bot_username END
                WHERE id = ?
                """,
                (store_name, plan, expiry_str, bot_token, bot_token, bot_username, bot_username, store_id),
            )
        else:
            cursor.execute(
                """
                INSERT INTO stores (owner_id, store_name, plan, license_expiry, license_status, bot_token, bot_username)
                VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (owner_id, store_name, plan, expiry_str, bot_token, bot_username),
            )
            store_id = cursor.lastrowid
        # Ensure owner is registered in admins table
        cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (owner_id,))
        conn.commit()
        logger.info(f"💾 [Database] Store #{store_id} saved for owner {owner_id} (name: '{store_name}', token: {'SET' if bot_token else 'EMPTY'})")
        return store_id


def update_store(store_id: int, **kwargs) -> bool:
    """Updates fields on a store record."""
    if not kwargs:
        return False
    if "bot_token" in kwargs and kwargs["bot_token"] is not None:
        kwargs["bot_token"] = clean_bot_token(kwargs["bot_token"])
    if "bot_username" in kwargs and kwargs["bot_username"] is not None:
        kwargs["bot_username"] = str(kwargs["bot_username"]).strip()
    fields = []
    values = []
    for k, v in kwargs.items():
        fields.append(f"{k} = ?")
        values.append(v)
    values.append(store_id)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE stores SET {', '.join(fields)} WHERE id = ?", tuple(values))
        conn.commit()
        success = cursor.rowcount > 0
        logger.info(f"💾 [Database] Store #{store_id} updated: {list(kwargs.keys())} (success={success})")
        return success


def get_store_license(store_id: int) -> Dict[str, Any]:
    """Checks license and validity for a specific store."""
    store = get_store(store_id)
    if not store:
        return {
            "is_valid": False,
            "status": "not_found",
            "expiry_str": "N/A",
            "expiry_full": "N/A",
            "days_remaining": 0,
            "plan": "None",
            "store_name": "Unknown",
            "owner_id": 0,
            "bot_token": "",
            "bot_username": "",
        }

    status = (store.get("license_status") or "active").strip().lower()
    expiry_str = (store.get("license_expiry") or "2026-12-31 23:59:59").strip()
    plan = store.get("plan") or "Pro Business"
    store_name = store.get("store_name") or (get_setting("store_name", "Digital Store") if store_id == 1 else f"Store #{store_id}")
    owner_id = store.get("owner_id") or 0

    expiry_dt = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%d %H:%M"):
        try:
            expiry_dt = datetime.strptime(expiry_str, fmt)
            break
        except ValueError:
            pass

    if not expiry_dt:
        expiry_dt = datetime.now() + timedelta(days=30)
        expiry_str = expiry_dt.strftime("%Y-%m-%d 23:59:59")
        update_store(store_id, license_expiry=expiry_str)

    now = datetime.now()
    time_diff = expiry_dt - now
    days_remaining = time_diff.days

    if status == "suspended":
        is_valid = False
        effective_status = "suspended"
    elif now > expiry_dt:
        is_valid = False
        effective_status = "expired"
        if status != "expired":
            update_store(store_id, license_status="expired")
    else:
        is_valid = (status == "active")
        effective_status = status if is_valid else "inactive"

    return {
        "is_valid": is_valid,
        "status": effective_status,
        "expiry_str": expiry_dt.strftime("%Y-%m-%d"),
        "expiry_full": expiry_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "days_remaining": max(0, days_remaining) if is_valid else 0,
        "plan": plan,
        "store_name": store_name,
        "owner_id": owner_id,
        "bot_token": store.get("bot_token") or "",
        "bot_username": store.get("bot_username") or "",
    }


def extend_store_license(store_id: int, days: int = 30) -> Dict[str, Any]:
    """Extends store license by X days from current expiry or now."""
    store = get_store(store_id)
    if not store:
        return {}
    now = datetime.now()
    current_expiry_dt = None
    expiry_val = store.get("license_expiry") or ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            current_expiry_dt = datetime.strptime(expiry_val, fmt)
            break
        except ValueError:
            pass

    if not current_expiry_dt or current_expiry_dt < now:
        base_dt = now
    else:
        base_dt = current_expiry_dt

    new_expiry_dt = base_dt + timedelta(days=days)
    new_expiry_str = new_expiry_dt.strftime("%Y-%m-%d 23:59:59")
    update_store(store_id, license_expiry=new_expiry_str, license_status="active")
    return get_store_license(store_id)


def set_store_license_status(store_id: int, status: str) -> None:
    """Updates license status ('active', 'suspended', 'expired') for a store."""
    update_store(store_id, license_status=status.strip().lower())


def create_store_request(
    user_id: int,
    username: Optional[str],
    first_name: Optional[str],
    store_name: str,
    plan: str,
    slip_file_id: Optional[str],
) -> int:
    """Records a new store onboarding application."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO store_requests (user_id, username, first_name, store_name, plan, slip_file_id, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (user_id, username, first_name, store_name, plan, slip_file_id),
        )
        conn.commit()
        return cursor.lastrowid


def get_store_request(req_id: int) -> Optional[Dict[str, Any]]:
    """Gets store request by ID."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM store_requests WHERE id = ?", (req_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_pending_store_requests() -> List[Dict[str, Any]]:
    """Gets all pending store applications."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM store_requests WHERE status = 'pending' ORDER BY created_at DESC")
        return [dict(row) for row in cursor.fetchall()]


def approve_store_request(req_id: int, days: int = 30) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Approves a store request, sets up/activates their store, adds admin access."""
    req = get_store_request(req_id)
    if not req:
        return False, None
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE store_requests SET status = 'approved' WHERE id = ?", (req_id,))
        conn.commit()

    store_id = create_store(
        owner_id=req["user_id"],
        store_name=req["store_name"],
        plan=req["plan"],
        days=days,
    )
    store = get_store(store_id)
    return True, store


def reject_store_request(req_id: int) -> bool:
    """Rejects a store request."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE store_requests SET status = 'rejected' WHERE id = ?", (req_id,))
        conn.commit()
        return cursor.rowcount > 0


def delete_store(store_id: int, remove_admin: bool = True) -> bool:
    """Deletes a client store, unlinks its products and stocks, and optionally cleans up the tenant admin."""
    if store_id == 1:
        return False  # Protect master store
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT owner_id FROM stores WHERE id = ?", (store_id,))
        row = cursor.fetchone()
        owner_id = row["owner_id"] if row else None

        cursor.execute("DELETE FROM stores WHERE id = ?", (store_id,))
        cursor.execute("DELETE FROM stock WHERE store_id = ?", (store_id,))
        cursor.execute("DELETE FROM products WHERE store_id = ?", (store_id,))
        cursor.execute("DELETE FROM store_users WHERE store_id = ?", (store_id,))

        if remove_admin and owner_id and owner_id != 1920334929:
            cursor.execute("SELECT COUNT(*) as count FROM stores WHERE owner_id = ?", (owner_id,))
            cnt_row = cursor.fetchone()
            if not cnt_row or cnt_row["count"] == 0:
                cursor.execute("DELETE FROM admins WHERE user_id = ?", (owner_id,))

        conn.commit()
        return True


def remove_tenant_admin(admin_id: int) -> bool:
    """Removes a tenant admin from admins table and cleans up their client stores."""
    if admin_id in (1920334929,):
        return False  # Protect super admin
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM admins WHERE user_id = ?", (admin_id,))
        cursor.execute("SELECT id FROM stores WHERE owner_id = ? AND id != 1", (admin_id,))
        store_rows = cursor.fetchall()
        for r in store_rows:
            s_id = r["id"]
            cursor.execute("DELETE FROM stores WHERE id = ?", (s_id,))
            cursor.execute("DELETE FROM stock WHERE store_id = ?", (s_id,))
            cursor.execute("DELETE FROM products WHERE store_id = ?", (s_id,))
            cursor.execute("DELETE FROM store_users WHERE store_id = ?", (s_id,))
        conn.commit()
        return True


def get_all_tenant_admins_with_stats() -> List[Dict[str, Any]]:
    """
    Returns all registered tenant stores and subscription admins along with their complete statistics.
    Ensures that every registered admin in `admins` table and every store in `stores` table is returned.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT s.*, 
                   u.username as owner_username, 
                   u.first_name as owner_first_name,
                   u.balance as owner_balance
            FROM stores s
            LEFT JOIN users u ON s.owner_id = u.user_id
            ORDER BY s.id ASC
            """
        )
        stores = [dict(r) for r in cursor.fetchall()]
        store_owners = {s["owner_id"] for s in stores if s.get("owner_id")}

        cursor.execute(
            """
            SELECT a.user_id, 
                   u.username as owner_username, 
                   u.first_name as owner_first_name,
                   u.balance as owner_balance
            FROM admins a
            LEFT JOIN users u ON a.user_id = u.user_id
            ORDER BY a.user_id ASC
            """
        )
        standalone_admins = []
        for r in cursor.fetchall():
            adm = dict(r)
            if adm["user_id"] not in store_owners and adm["user_id"] not in (1920334929,):
                standalone_admins.append(adm)

    results = []
    for s in stores:
        s_id = s["id"]
        lic = get_store_license(s_id)
        stats = get_store_stats(store_id=s_id)
        item = dict(s)
        item["license"] = lic
        item["stats"] = stats
        item["is_standalone"] = False
        results.append(item)

    for sa in standalone_admins:
        uid = sa["user_id"]
        results.append({
            "id": None,
            "owner_id": uid,
            "store_name": f"Pending Store (Admin #{uid})",
            "bot_token": "",
            "bot_username": "",
            "plan": "Standalone Admin",
            "license": {"is_valid": True, "status": "active", "days_remaining": 999, "expiry_str": "N/A"},
            "stats": {"total_users": 0, "total_orders": 0, "total_revenue": 0, "total_products": 0, "total_stock": 0},
            "owner_username": sa.get("owner_username"),
            "owner_first_name": sa.get("owner_first_name"),
            "is_standalone": True,
        })

    return results



