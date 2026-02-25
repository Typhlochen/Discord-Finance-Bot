import asyncpg
import os


async def create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(os.environ["DATABASE_URL"])


async def init_db(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS debts (
                id          SERIAL PRIMARY KEY,
                creditor_id BIGINT          NOT NULL,
                debtor_id   BIGINT          NOT NULL,
                amount      DECIMAL(12, 2)  NOT NULL CHECK (amount > 0),
                note        TEXT,
                created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_requests (
                message_id  BIGINT          PRIMARY KEY,
                channel_id  BIGINT          NOT NULL,
                creditor_id BIGINT          NOT NULL,
                debtor_id   BIGINT          NOT NULL,
                amount      DECIMAL(12, 2)  NOT NULL CHECK (amount > 0),
                note        TEXT,
                expires_at  TIMESTAMPTZ     NOT NULL,
                reminded    BOOLEAN         NOT NULL DEFAULT FALSE,
                created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
            )
        """)
        # Safe migrations for tables that already exist without these columns
        await conn.execute("""
            ALTER TABLE pending_requests
                ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS reminded   BOOLEAN NOT NULL DEFAULT FALSE
        """)


# ---------- pending requests ----------

async def add_pending_request(
    pool: asyncpg.Pool,
    *,
    message_id: int,
    channel_id: int,
    creditor_id: int,
    debtor_id: int,
    amount: float,
    note: str | None,
    expires_at,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pending_requests
                (message_id, channel_id, creditor_id, debtor_id, amount, note, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            message_id, channel_id, creditor_id, debtor_id, amount, note, expires_at,
        )


async def get_pending_request(
    pool: asyncpg.Pool, message_id: int
) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM pending_requests WHERE message_id = $1", message_id
        )


async def delete_pending_request(pool: asyncpg.Pool, message_id: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM pending_requests WHERE message_id = $1", message_id
        )


async def get_requests_to_remind(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Requests expiring within 1 hour that haven't been reminded yet."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT * FROM pending_requests
            WHERE reminded = FALSE
              AND expires_at <= NOW() + INTERVAL '1 hour'
              AND expires_at > NOW()
            """
        )


async def mark_reminded(pool: asyncpg.Pool, message_id: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE pending_requests SET reminded = TRUE WHERE message_id = $1",
            message_id,
        )


async def get_expired_requests(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Requests whose expiration time has passed."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM pending_requests WHERE expires_at <= NOW()"
        )


# ---------- write operations ----------

async def add_debt(
    pool: asyncpg.Pool,
    *,
    creditor_id: int,
    debtor_id: int,
    amount: float,
    note: str | None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO debts (creditor_id, debtor_id, amount, note)
            VALUES ($1, $2, $3, $4)
            """,
            creditor_id,
            debtor_id,
            amount,
            note,
        )


async def apply_payment(
    pool: asyncpg.Pool,
    *,
    creditor_id: int,
    debtor_id: int,
    amount: float,
) -> float:
    """
    Reduce existing debt entries (oldest first) by `amount`.
    Returns the remaining payment after all matching debts are cleared
    (0.0 means the payment was fully absorbed; > 0 means overpayment).
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT id, amount FROM debts
                WHERE creditor_id = $1 AND debtor_id = $2
                ORDER BY created_at ASC
                FOR UPDATE
                """,
                creditor_id,
                debtor_id,
            )

            remaining = amount
            for row in rows:
                if remaining <= 0:
                    break
                if remaining >= row["amount"]:
                    remaining -= row["amount"]
                    await conn.execute("DELETE FROM debts WHERE id = $1", row["id"])
                else:
                    await conn.execute(
                        "UPDATE debts SET amount = amount - $1 WHERE id = $2",
                        remaining,
                        row["id"],
                    )
                    remaining = 0

            return remaining


# ---------- read operations ----------

async def get_owed_to_user(pool: asyncpg.Pool, creditor_id: int) -> list[asyncpg.Record]:
    """Return rows of (debtor_id, total) showing who owes the user."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT debtor_id, SUM(amount) AS total
            FROM debts
            WHERE creditor_id = $1
            GROUP BY debtor_id
            ORDER BY total DESC
            """,
            creditor_id,
        )


async def get_owed_by_user(pool: asyncpg.Pool, debtor_id: int) -> list[asyncpg.Record]:
    """Return rows of (creditor_id, total) showing who the user owes."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT creditor_id, SUM(amount) AS total
            FROM debts
            WHERE debtor_id = $1
            GROUP BY creditor_id
            ORDER BY total DESC
            """,
            debtor_id,
        )


async def get_balance_between(
    pool: asyncpg.Pool, user_a: int, user_b: int
) -> float:
    """Net amount user_b owes user_a (negative = user_a owes user_b)."""
    async with pool.acquire() as conn:
        owed_to_a = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM debts WHERE creditor_id=$1 AND debtor_id=$2",
            user_a, user_b,
        )
        owed_to_b = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM debts WHERE creditor_id=$1 AND debtor_id=$2",
            user_b, user_a,
        )
        return float(owed_to_a) - float(owed_to_b)
