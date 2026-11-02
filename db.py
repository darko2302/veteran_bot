import asyncpg
import os

pool: asyncpg.Pool | None = None

async def init_db():
    global pool
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("Задай DATABASE_URL")

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5)

    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS appeals (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                category_key TEXT NOT NULL,
                subject TEXT,
                conversation_message_id BIGINT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS group_messages (
                chat_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                appeal_id INTEGER NOT NULL REFERENCES appeals(id),
                user_id BIGINT NOT NULL,
                source_message_id BIGINT NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS group_messages_appeal_source
                ON group_messages (appeal_id, source_message_id, chat_id);
            CREATE TABLE IF NOT EXISTS private_messages (
                user_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                appeal_id INTEGER NOT NULL REFERENCES appeals(id),
                PRIMARY KEY (user_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id BIGINT PRIMARY KEY,
                blocked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS group_cards (
                chat_id BIGINT NOT NULL,
                appeal_id INTEGER NOT NULL REFERENCES appeals(id),
                message_id BIGINT NOT NULL,
                base_text TEXT NOT NULL,
                PRIMARY KEY (chat_id, appeal_id)
            );
            CREATE TABLE IF NOT EXISTS appeal_messages (
                id SERIAL PRIMARY KEY,
                appeal_id INTEGER NOT NULL REFERENCES appeals(id),
                sender_role TEXT NOT NULL,
                sender_id BIGINT NOT NULL,
                sender_label TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS active_operator_msgs (
                appeal_id INTEGER PRIMARY KEY,
                user_msg_id BIGINT
            );
            CREATE TABLE IF NOT EXISTS active_user_msgs (
                appeal_id INTEGER NOT NULL,
                chat_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                PRIMARY KEY (appeal_id, chat_id)
            );
        """)

async def close_db():
    global pool
    if pool:
        await pool.close()

async def register_group_message(chat_id, message_id, appeal_id, user_id, source_message_id):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO group_messages (chat_id, message_id, appeal_id, user_id, source_message_id)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (chat_id, message_id) DO UPDATE SET
                appeal_id = EXCLUDED.appeal_id,
                user_id = EXCLUDED.user_id,
                source_message_id = EXCLUDED.source_message_id
        """, chat_id, message_id, appeal_id, user_id, source_message_id)

async def register_group_card(chat_id, appeal_id, message_id, base_text):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO group_cards (chat_id, appeal_id, message_id, base_text)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (chat_id, appeal_id) DO UPDATE SET
                message_id = EXCLUDED.message_id,
                base_text = EXCLUDED.base_text
        """, chat_id, appeal_id, message_id, base_text)

async def get_group_card_message_id(chat_id, appeal_id):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT message_id FROM group_cards
            WHERE chat_id = $1 AND appeal_id = $2
        """, chat_id, appeal_id)
    return row["message_id"] if row else None

async def register_private_message(user_id, message_id, appeal_id):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO private_messages (user_id, message_id, appeal_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, message_id) DO UPDATE SET
                appeal_id = EXCLUDED.appeal_id
        """, user_id, message_id, appeal_id)

async def is_user_blocked(user_id):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM blocked_users WHERE user_id = $1", user_id)
    return row is not None

async def set_user_blocked(user_id, blocked):
    async with pool.acquire() as conn:
        if blocked:
            await conn.execute("""
                INSERT INTO blocked_users (user_id) VALUES ($1)
                ON CONFLICT (user_id) DO NOTHING
            """, user_id)
        else:
            await conn.execute("DELETE FROM blocked_users WHERE user_id = $1", user_id)

async def get_open_appeals(user_id):
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT id, category_key, conversation_message_id
            FROM appeals
            WHERE user_id = $1 AND status = 'open'
            ORDER BY id DESC
        """, user_id)

async def get_all_user_appeals(user_id):
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT id, category_key, conversation_message_id, status, created_at
            FROM appeals
            WHERE user_id = $1
            ORDER BY id DESC
        """, user_id)

async def get_appeal_by_id(appeal_id):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM appeals WHERE id = $1", appeal_id)

async def get_appeal_messages(appeal_id):
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT sender_role, sender_label, content
            FROM appeal_messages
            WHERE appeal_id = $1
            ORDER BY id
        """, appeal_id)

async def get_group_message(chat_id, message_id):
    async with pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT gm.*, a.status, a.category_key, a.conversation_message_id
            FROM group_messages AS gm
            JOIN appeals AS a ON a.id = gm.appeal_id
            WHERE gm.chat_id = $1 AND gm.message_id = $2
        """, chat_id, message_id)

async def get_private_message(user_id, message_id):
    async with pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT a.id, pm.appeal_id, a.user_id, a.category_key,
                   a.conversation_message_id, a.status
            FROM private_messages AS pm
            JOIN appeals AS a ON a.id = pm.appeal_id
            WHERE pm.user_id = $1 AND pm.message_id = $2
        """, user_id, message_id)

async def create_appeal(user_id, category_key, conversation_message_id):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO appeals (user_id, category_key, conversation_message_id)
            VALUES ($1, $2, $3)
            RETURNING id
        """, user_id, category_key, conversation_message_id)
    return row["id"]

async def append_message_to_group_cards_db(appeal_id, sender_role, sender_id, sender_label, content):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO appeal_messages (appeal_id, sender_role, sender_id, sender_label, content)
            VALUES ($1, $2, $3, $4, $5)
        """, appeal_id, sender_role, sender_id, sender_label, content)

        entries = await conn.fetch("""
            SELECT sender_role, sender_label, content
            FROM appeal_messages
            WHERE appeal_id = $1
            ORDER BY id
        """, appeal_id)

        cards = await conn.fetch("""
            SELECT chat_id, message_id, base_text
            FROM group_cards
            WHERE appeal_id = $1
        """, appeal_id)

        appeal = await conn.fetchrow("SELECT user_id, status FROM appeals WHERE id = $1", appeal_id)

    return entries, cards, appeal

async def close_appeal_db(appeal_id):
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE appeals SET status = 'closed'
            WHERE id = $1 AND status = 'open'
        """, appeal_id)
    return result == "UPDATE 1"

# ==================== АКТИВНІ ПОВІДОМЛЕННЯ ====================

async def get_active_operator_user_msg(appeal_id):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_msg_id FROM active_operator_msgs WHERE appeal_id = $1", appeal_id)
    return row["user_msg_id"] if row else None

async def set_active_operator_user_msg(appeal_id, user_msg_id):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO active_operator_msgs (appeal_id, user_msg_id) 
            VALUES ($1, $2) 
            ON CONFLICT (appeal_id) DO UPDATE SET user_msg_id = EXCLUDED.user_msg_id
        """, appeal_id, user_msg_id)

async def get_active_user_group_msg(appeal_id, chat_id):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT message_id FROM active_user_msgs WHERE appeal_id = $1 AND chat_id = $2", appeal_id, chat_id)
    return row["message_id"] if row else None

async def set_active_user_group_msg(appeal_id, chat_id, message_id):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO active_user_msgs (appeal_id, chat_id, message_id) 
            VALUES ($1, $2, $3) 
            ON CONFLICT (appeal_id, chat_id) DO UPDATE SET message_id = EXCLUDED.message_id
        """, appeal_id, chat_id, message_id)