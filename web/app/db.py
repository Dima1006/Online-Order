import os
import sqlite3
import time
from pathlib import Path

from . import config as cfg
from .utils import normalize

DB_PATH = os.getenv("DATABASE_PATH", str(Path(__file__).resolve().parent.parent / "data" / "app.db"))
SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT,                       -- NULL for Google-only accounts
    google_sub TEXT UNIQUE,
    name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    email_verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS venues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    city TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    accepting_orders INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | suspended
    open_time TEXT NOT NULL DEFAULT '00:00',  -- Europe/Warsaw; open == close means "open all day"
    close_time TEXT NOT NULL DEFAULT '00:00',
    prep_minutes INTEGER NOT NULL DEFAULT 5,  -- earliest arrival a customer can choose
    max_active INTEGER NOT NULL DEFAULT 15,   -- the venue stops taking orders above this many active ones
    paused_until TEXT,                        -- UTC; short "we are swamped" pause
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_id INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_id INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_available INTEGER NOT NULL DEFAULT 1
);

-- One row per size/option. Price is stored in grosze (1 zł = 100).
CREATE TABLE IF NOT EXISTS variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '',
    price INTEGER NOT NULL
);

-- No-show warnings are counted per venue: one venue cannot lock a customer out of all the others.
CREATE TABLE IF NOT EXISTS venue_customer_flags (
    venue_id INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    warnings INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (venue_id, user_id)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_id INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    customer_name TEXT NOT NULL,              -- snapshot, so profile edits never rewrite history
    phone TEXT NOT NULL DEFAULT '',
    arrival_minutes INTEGER NOT NULL,
    arrive_at TEXT NOT NULL,                  -- UTC moment the customer promised to arrive
    note TEXT NOT NULL DEFAULT '',
    total INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',       -- new | preparing | ready | completed | declined | no_show | cancelled | expired
    decline_reason TEXT NOT NULL DEFAULT '',
    submit_key TEXT UNIQUE,                   -- one per order form: a double click cannot create two orders
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Snapshot of what was ordered, so later menu edits never change old orders.
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    variant_label TEXT NOT NULL DEFAULT '',
    price INTEGER NOT NULL,
    qty INTEGER NOT NULL
);

-- Telegram: one-time codes that connect a chat to a venue, the connected chats,
-- and the messages we sent (so they can be updated when the status changes elsewhere).
CREATE TABLE IF NOT EXISTS tg_link_codes (
    code TEXT PRIMARY KEY,
    venue_id INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tg_chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_id INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    chat_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (venue_id, chat_id)
);
CREATE TABLE IF NOT EXISTS tg_messages (
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (order_id, chat_id)
);

CREATE INDEX IF NOT EXISTS idx_items_venue ON items(venue_id);
CREATE INDEX IF NOT EXISTS idx_variants_item ON variants(item_id);
CREATE INDEX IF NOT EXISTS idx_orders_venue_created ON orders(venue_id, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_venue_status ON orders(venue_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_user_status ON orders(user_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_status_arrive ON orders(status, arrive_at);
CREATE INDEX IF NOT EXISTS idx_venues_status ON venues(status);
"""


class _QueryBudget:
    """SQLite progress handler: aborts one statement that runs longer than QUERY_TIMEOUT_S.
    It is called every few thousand VM steps *while a query runs*; a long pause between calls
    means a new statement started, so the clock restarts."""

    def __init__(self):
        self.start = 0.0
        self.last = 0.0

    def __call__(self):
        now = time.monotonic()
        if now - self.last > 0.5:
            self.start = now
        self.last = now
        return 1 if now - self.start > cfg.QUERY_TIMEOUT_S else 0


def connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.create_function("norm", 1, lambda s: normalize(s or ""), deterministic=True)
    conn.set_progress_handler(_QueryBudget(), 20_000)
    return conn


def init_db() -> None:
    conn = connect()
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_venues = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='venues'").fetchone()
        if has_venues and version < SCHEMA_VERSION:
            raise RuntimeError(
                f"{DB_PATH} was created by an older version of this app (schema {version}, now {SCHEMA_VERSION}). "
                "It only held demo data: delete the file and start again.")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()
