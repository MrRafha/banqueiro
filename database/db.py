import aiosqlite
import os
import time as _time
from contextlib import asynccontextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "velhocovilbot.db")

# ─── Simple TTL in-memory cache ───────────────────────────────────────────────
# Avoids opening a new SQLite connection on every voice-state-update / reaction.
_CACHE: dict = {}
_CACHE_TTL = 5.0  # seconds
_FLOAT_EPSILON = 1e-6


def _cache_get(key):
    entry = _CACHE.get(key)
    if entry and _time.monotonic() - entry[0] < _CACHE_TTL:
        return entry[1]
    return None


def _cache_set(key, value):
    _CACHE[key] = (_time.monotonic(), value)


def _cache_del(*keys):
    for k in keys:
        _CACHE.pop(k, None)


@asynccontextmanager
async def get_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        # WAL mode and foreign keys are set once in init_db() and persist;
        # re-setting them on every connection open adds unnecessary lock overhead.
        await conn.execute("PRAGMA foreign_keys=ON")
        yield conn


async def init_db():
    async with get_db() as db:
        # Set WAL once — the mode persists in the DB file across connections.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id              TEXT PRIMARY KEY,
                admin_role_id         TEXT,
                cc_role_id            TEXT,
                seller_role_id        TEXT,
                member_role_id        TEXT,
                tax_rate              REAL DEFAULT 0,
                guild_balance         REAL DEFAULT 0,
                event_category_id     TEXT,
                create_event_channel_id TEXT,
                log_channel_id        TEXT
            );

            CREATE TABLE IF NOT EXISTS events (
                id                TEXT PRIMARY KEY,
                guild_id          TEXT NOT NULL,
                event_name        TEXT,
                announce_msg_id   TEXT,
                voice_channel_id  TEXT,
                text_channel_id   TEXT,
                status            TEXT DEFAULT 'active',
                started_at        REAL,
                ended_at          REAL,
                silver_amount     REAL,
                FOREIGN KEY(guild_id) REFERENCES guild_config(guild_id)
            );

            CREATE TABLE IF NOT EXISTS event_participants (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id         TEXT NOT NULL,
                user_id          TEXT NOT NULL,
                join_time        REAL,
                total_seconds    REAL DEFAULT 0,
                participation_pct REAL DEFAULT 0,
                manual_pct       INTEGER DEFAULT 0,
                UNIQUE(event_id, user_id),
                FOREIGN KEY(event_id) REFERENCES events(id)
            );

            CREATE TABLE IF NOT EXISTS user_balances (
                user_id   TEXT NOT NULL,
                guild_id  TEXT NOT NULL,
                balance   REAL DEFAULT 0,
                PRIMARY KEY (user_id, guild_id)
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT,
                guild_id   TEXT NOT NULL,
                amount     REAL NOT NULL,
                type       TEXT NOT NULL,
                event_id   TEXT,
                note       TEXT,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS raids (
                message_id     TEXT PRIMARY KEY,
                guild_id       TEXT NOT NULL,
                tipo           TEXT NOT NULL,
                titulo         TEXT NOT NULL,
                descricao      TEXT NOT NULL,
                data           TEXT NOT NULL,
                horario        TEXT NOT NULL,
                selected_roles TEXT NOT NULL
            );
        """)
        # Migrate: add log_channel_id if it doesn't exist yet
        try:
            await db.execute("ALTER TABLE guild_config ADD COLUMN log_channel_id TEXT")
            await db.commit()
        except Exception:
            pass  # column already exists

        # Migrate: add event_name to events if it doesn't exist yet
        try:
            await db.execute("ALTER TABLE events ADD COLUMN event_name TEXT")
            await db.commit()
        except Exception:
            pass  # column already exists

        # Migrate: create raids table if it doesn't exist yet (added later)
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS raids (
                    message_id     TEXT PRIMARY KEY,
                    guild_id       TEXT NOT NULL,
                    tipo           TEXT NOT NULL,
                    titulo         TEXT NOT NULL,
                    descricao      TEXT NOT NULL,
                    data           TEXT NOT NULL,
                    horario        TEXT NOT NULL,
                    selected_roles TEXT NOT NULL
                )
            """)
            await db.commit()
        except Exception:
            pass  # table already exists


# ─── guild_config ─────────────────────────────────────────────────────────────

async def get_guild_config(guild_id: int) -> aiosqlite.Row | None:
    key = f"guild_config_{guild_id}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM guild_config WHERE guild_id = ?", (str(guild_id),)
        ) as cur:
            row = await cur.fetchone()
    _cache_set(key, row)
    return row


async def upsert_guild_config(guild_id: int, **kwargs):
    async with get_db() as db:
        existing = None
        async with db.execute(
            "SELECT guild_id FROM guild_config WHERE guild_id = ?", (str(guild_id),)
        ) as cur:
            existing = await cur.fetchone()

        if existing:
            sets = ", ".join(f"{k} = ?" for k in kwargs)
            vals = list(kwargs.values()) + [str(guild_id)]
            await db.execute(f"UPDATE guild_config SET {sets} WHERE guild_id = ?", vals)
        else:
            kwargs["guild_id"] = str(guild_id)
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" for _ in kwargs)
            await db.execute(
                f"INSERT INTO guild_config ({cols}) VALUES ({placeholders})",
                list(kwargs.values()),
            )
        await db.commit()    # Invalidate cached config for this guild
    _cache_del(f"guild_config_{guild_id}")

# ─── events ───────────────────────────────────────────────────────────────────

async def create_event(event_id: str, guild_id: int, voice_channel_id: int,
                       announce_msg_id: int, started_at: float | None = None,
                       event_name: str = ""):
    async with get_db() as db:
        await db.execute(
            """INSERT INTO events (id, guild_id, event_name, voice_channel_id, announce_msg_id, started_at, status)
               VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
            (event_id, str(guild_id), event_name, str(voice_channel_id), str(announce_msg_id), started_at),
        )
        await db.commit()
    # New event → invalidate active-events cache
    _cache_del("all_active_events")


async def get_event(event_id: str) -> aiosqlite.Row | None:
    async with get_db() as db:
        async with db.execute("SELECT * FROM events WHERE id = ?", (event_id,)) as cur:
            return await cur.fetchone()


async def get_event_by_channel(channel_id: int, status: str | None = None) -> aiosqlite.Row | None:
    """Find an event by its voice OR text channel id. Optionally filter by status."""
    async with get_db() as db:
        if status:
            async with db.execute(
                "SELECT * FROM events WHERE (voice_channel_id = ? OR text_channel_id = ?) AND status = ?",
                (str(channel_id), str(channel_id), status),
            ) as cur:
                return await cur.fetchone()
        else:
            async with db.execute(
                "SELECT * FROM events WHERE voice_channel_id = ? OR text_channel_id = ?",
                (str(channel_id), str(channel_id)),
            ) as cur:
                return await cur.fetchone()


async def get_active_events(guild_id: int) -> list:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM events WHERE guild_id = ? AND status IN ('pending', 'active')",
            (str(guild_id),),
        ) as cur:
            return await cur.fetchall()


async def get_all_active_events() -> list:
    cached = _cache_get("all_active_events")
    if cached is not None:
        return cached
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM events WHERE status IN ('pending', 'active')"
        ) as cur:
            rows = await cur.fetchall()
    _cache_set("all_active_events", rows)
    return rows


async def update_event(event_id: str, **kwargs):
    async with get_db() as db:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [event_id]
        await db.execute(f"UPDATE events SET {sets} WHERE id = ?", vals)
        await db.commit()
    # Status may have changed → invalidate active-events cache
    _cache_del("all_active_events")


# ─── event_participants ───────────────────────────────────────────────────────

async def add_participant(event_id: str, user_id: int):
    """Insert participant if not already present."""
    async with get_db() as db:
        await db.execute(
            """INSERT OR IGNORE INTO event_participants (event_id, user_id)
               VALUES (?, ?)""",
            (event_id, str(user_id)),
        )
        await db.commit()


async def participant_join_voice(event_id: str, user_id: int, join_time: float):
    async with get_db() as db:
        await db.execute(
            "UPDATE event_participants SET join_time = ? WHERE event_id = ? AND user_id = ?",
            (join_time, event_id, str(user_id)),
        )
        await db.commit()


async def participant_leave_voice(event_id: str, user_id: int, leave_time: float):
    """Accumulate seconds and clear join_time."""
    async with get_db() as db:
        async with db.execute(
            "SELECT join_time, total_seconds FROM event_participants WHERE event_id = ? AND user_id = ?",
            (event_id, str(user_id)),
        ) as cur:
            row = await cur.fetchone()
        if row and row["join_time"] is not None:
            elapsed = leave_time - row["join_time"]
            new_total = (row["total_seconds"] or 0) + elapsed
            await db.execute(
                """UPDATE event_participants
                   SET total_seconds = ?, join_time = NULL
                   WHERE event_id = ? AND user_id = ?""",
                (new_total, event_id, str(user_id)),
            )
            await db.commit()


async def get_participants(event_id: str) -> list:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM event_participants WHERE event_id = ?", (event_id,)
        ) as cur:
            return await cur.fetchall()


async def upsert_participant(event_id: str, user_id: int, participation_pct: float,
                              manual_pct: bool = True):
    async with get_db() as db:
        await db.execute(
            """INSERT INTO event_participants (event_id, user_id, participation_pct, manual_pct)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(event_id, user_id)
               DO UPDATE SET participation_pct = excluded.participation_pct,
                             manual_pct = excluded.manual_pct""",
            (event_id, str(user_id), participation_pct, int(manual_pct)),
        )
        await db.commit()


async def remove_participant(event_id: str, user_id: int):
    async with get_db() as db:
        await db.execute(
            "DELETE FROM event_participants WHERE event_id = ? AND user_id = ?",
            (event_id, str(user_id)),
        )
        await db.commit()


async def finalize_participation_pcts(event_id: str, event_duration_seconds: float):
    """Calculate participation_pct for all non-manual participants."""
    if event_duration_seconds <= 0:
        return
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM event_participants WHERE event_id = ? AND manual_pct = 0",
            (event_id,),
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            pct = min(100.0, (row["total_seconds"] / event_duration_seconds) * 100)
            await db.execute(
                "UPDATE event_participants SET participation_pct = ? WHERE id = ?",
                (pct, row["id"]),
            )
        await db.commit()


# ─── user_balances ────────────────────────────────────────────────────────────

async def get_balance(user_id: int, guild_id: int) -> float:
    async with get_db() as db:
        async with db.execute(
            "SELECT balance FROM user_balances WHERE user_id = ? AND guild_id = ?",
            (str(user_id), str(guild_id)),
        ) as cur:
            row = await cur.fetchone()
        return row["balance"] if row else 0.0


async def get_all_balances(guild_id: int) -> list:
    """Return all non-zero balances for a guild, sorted highest first."""
    async with get_db() as db:
        async with db.execute(
            "SELECT user_id, balance FROM user_balances WHERE guild_id = ? AND balance > 0 ORDER BY balance DESC",
            (str(guild_id),),
        ) as cur:
            return await cur.fetchall()


async def normalize_guild_balances(guild_id: int) -> tuple[int, int, float]:
    """Normalize all balances for a guild.

    Rules:
    - Remove fractional part (round down/truncate) for positive balances.
    - Clamp tiny residuals and negative balances to 0.

    Returns: (changed_rows, zeroed_rows, total_fraction_removed)
    """
    changed_rows = 0
    zeroed_rows = 0
    removed_total = 0.0

    async with get_db() as db:
        async with db.execute(
            "SELECT user_id, balance FROM user_balances WHERE guild_id = ?",
            (str(guild_id),),
        ) as cur:
            rows = await cur.fetchall()

        for row in rows:
            current = float(row["balance"] or 0)
            if abs(current) <= _FLOAT_EPSILON:
                normalized = 0
            elif current < 0:
                normalized = 0
            else:
                normalized = int(current)

            if abs(current - normalized) > _FLOAT_EPSILON:
                changed_rows += 1
                if normalized == 0 and current > 0:
                    zeroed_rows += 1
                if current > normalized:
                    removed_total += (current - normalized)

                await db.execute(
                    "UPDATE user_balances SET balance = ? WHERE user_id = ? AND guild_id = ?",
                    (float(normalized), str(row["user_id"]), str(guild_id)),
                )

        await db.commit()

    return changed_rows, zeroed_rows, removed_total


async def add_balance(user_id: int, guild_id: int, amount: float):
    async with get_db() as db:
        await db.execute(
            """INSERT INTO user_balances (user_id, guild_id, balance) VALUES (?, ?, ?)
               ON CONFLICT(user_id, guild_id) DO UPDATE SET balance = balance + excluded.balance""",
            (str(user_id), str(guild_id), amount),
        )
        await db.commit()


async def set_balance(user_id: int, guild_id: int, amount: float):
    async with get_db() as db:
        await db.execute(
            """INSERT INTO user_balances (user_id, guild_id, balance) VALUES (?, ?, ?)
               ON CONFLICT(user_id, guild_id) DO UPDATE SET balance = excluded.balance""",
            (str(user_id), str(guild_id), amount),
        )
        await db.commit()


async def atomic_subtract_balance(user_id: int, guild_id: int, amount: float) -> bool:
    """Atomically subtract amount from user balance with float-tolerance.

    This avoids false "insufficient funds" caused by tiny float precision errors
    (for example: stored 0.379999999 vs requested 0.38).
    """
    async with get_db() as db:
        cur = await db.execute(
            """UPDATE user_balances
               SET balance = CASE
                   WHEN ABS(balance - ?) <= ? THEN 0
                   ELSE balance - ?
               END
               WHERE user_id = ?
                 AND guild_id = ?
                 AND balance >= (? - ?)""",
            (amount, _FLOAT_EPSILON, amount, str(user_id), str(guild_id), amount, _FLOAT_EPSILON),
        )
        await db.commit()
        return cur.rowcount == 1


# ─── guild balance helpers ────────────────────────────────────────────────────

async def add_guild_balance(guild_id: int, amount: float):
    """Ensure the guild row exists, then atomically add to guild_balance."""
    async with get_db() as db:
        # Ensure the row exists without touching any other column
        await db.execute(
            "INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)",
            (str(guild_id),),
        )
        await db.execute(
            "UPDATE guild_config SET guild_balance = COALESCE(guild_balance, 0) + ? WHERE guild_id = ?",
            (amount, str(guild_id)),
        )
        await db.commit()


async def subtract_guild_balance(guild_id: int, amount: float) -> bool:
    """Atomically subtract amount from guild balance with float-tolerance."""
    async with get_db() as db:
        cur = await db.execute(
            """UPDATE guild_config
               SET guild_balance = CASE
                   WHEN ABS(COALESCE(guild_balance, 0) - ?) <= ? THEN 0
                   ELSE COALESCE(guild_balance, 0) - ?
               END
               WHERE guild_id = ?
                 AND COALESCE(guild_balance, 0) >= (? - ?)""",
            (amount, _FLOAT_EPSILON, amount, str(guild_id), amount, _FLOAT_EPSILON),
        )
        await db.commit()
        return cur.rowcount == 1


# ─── transactions ─────────────────────────────────────────────────────────────

async def record_transaction(guild_id: int, amount: float, type_: str,
                              user_id: int | None = None, event_id: str | None = None,
                              note: str | None = None):
    import time
    async with get_db() as db:
        await db.execute(
            """INSERT INTO transactions (user_id, guild_id, amount, type, event_id, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(user_id) if user_id else None, str(guild_id), amount,
             type_, event_id, note, time.time()),
        )
        await db.commit()


async def batch_credit_event(
    event_id: str,
    guild_id: int,
    splits: list[dict],
    guild_cut: float,
    short_id: str,
):
    """Credit all event participants and the guild in ONE DB connection/transaction.
    Each split dict must have 'user_id' and 'amount'.
    """
    import time
    now = time.time()
    async with get_db() as db:
        for s in splits:
            if s["amount"] > 0:
                await db.execute(
                    """INSERT INTO user_balances (user_id, guild_id, balance) VALUES (?, ?, ?)
                       ON CONFLICT(user_id, guild_id) DO UPDATE SET balance = balance + excluded.balance""",
                    (str(s["user_id"]), str(guild_id), s["amount"]),
                )
                await db.execute(
                    """INSERT INTO transactions
                           (user_id, guild_id, amount, type, event_id, note, created_at)
                       VALUES (?, ?, ?, 'deposit', ?, ?, ?)""",
                    (str(s["user_id"]), str(guild_id), s["amount"],
                     event_id, f"Split evento {short_id}", now),
                )
        if guild_cut > 0:
            await db.execute(
                "INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)",
                (str(guild_id),),
            )
            await db.execute(
                "UPDATE guild_config SET guild_balance = COALESCE(guild_balance, 0) + ? WHERE guild_id = ?",
                (guild_cut, str(guild_id)),
            )
            await db.execute(
                """INSERT INTO transactions
                       (user_id, guild_id, amount, type, event_id, note, created_at)
                   VALUES (NULL, ?, ?, 'guild_deposit', ?, ?, ?)""",
                (str(guild_id), guild_cut, event_id, f"Taxa evento {short_id}", now),
            )
        await db.commit()


async def get_transactions(guild_id: int, user_id: int | None = None,
                            limit: int = 10, offset: int = 0) -> list:
    """Fetch recent transactions for a user (or all guild transactions if user_id is None)."""
    async with get_db() as db:
        if user_id is not None:
            async with db.execute(
                """SELECT * FROM transactions WHERE guild_id = ? AND user_id = ?
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (str(guild_id), str(user_id), limit, offset),
            ) as cur:
                return await cur.fetchall()
        else:
            async with db.execute(
                """SELECT * FROM transactions WHERE guild_id = ?
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (str(guild_id), limit, offset),
            ) as cur:
                return await cur.fetchall()


async def get_event_history(guild_id: int, user_id: int) -> list:
    """Return all events a user participated in, joined with event data."""
    async with get_db() as db:
        async with db.execute(
            """SELECT e.id, e.started_at, e.ended_at, e.silver_amount, e.status,
                      ep.participation_pct, ep.total_seconds
               FROM event_participants ep
               JOIN events e ON ep.event_id = e.id
               WHERE e.guild_id = ? AND ep.user_id = ?
               ORDER BY e.started_at DESC""",
            (str(guild_id), str(user_id)),
        ) as cur:
            return await cur.fetchall()


async def recover_active_events() -> list:
    """Return all active events with participants still marked as in-voice (join_time != NULL).
    Used on bot restart to reset their join_time to now."""
    async with get_db() as db:
        async with db.execute(
            """SELECT ep.event_id, ep.user_id
               FROM event_participants ep
               JOIN events e ON ep.event_id = e.id
               WHERE e.status = 'active' AND ep.join_time IS NOT NULL"""
        ) as cur:
            return await cur.fetchall()


# ─── raids ────────────────────────────────────────────────────────────────────

async def create_raid(message_id: int, guild_id: int, tipo: str, titulo: str,
                      descricao: str, data: str, horario: str,
                      selected_roles_json: str):
    async with get_db() as db:
        await db.execute(
            """INSERT INTO raids (message_id, guild_id, tipo, titulo, descricao, data, horario, selected_roles)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(message_id), str(guild_id), tipo, titulo, descricao, data, horario, selected_roles_json),
        )
        await db.commit()


async def get_raid_by_message(message_id: int) -> aiosqlite.Row | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM raids WHERE message_id = ?", (str(message_id),)
        ) as cur:
            return await cur.fetchone()


async def delete_raid(message_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM raids WHERE message_id = ?", (str(message_id),))
        await db.commit()
