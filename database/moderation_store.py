"""Persistent moderation audit and review queue, independent of Telegram runtime state."""
import hashlib
import json
from .db_manager import db_manager


async def audit(user_id, message, verdict, stage='delivery'):
    async with db_manager.get_connection() as connection:
        await connection.execute('''
            INSERT INTO moderation_events
            (user_id, chat_id, message_id, stage, status, risk_score, category, reason, provider, model, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, message.chat_id, message.message_id, stage, verdict['status'],
              verdict.get('risk_score'), verdict.get('category'), verdict.get('reason'),
              verdict.get('provider'), verdict.get('model'), verdict.get('error')))
        await connection.commit()


async def recent_events(user_id, limit=10):
    async with db_manager.get_connection() as connection:
        cursor = await connection.execute(
            'SELECT * FROM moderation_events WHERE user_id = ? ORDER BY id DESC LIMIT ?',
            (user_id, limit))
        return [dict(zip([col[0] for col in cursor.description], row)) for row in await cursor.fetchall()]


async def passed_message_count(user_id):
    async with db_manager.get_connection() as connection:
        cursor = await connection.execute('''SELECT COUNT(DISTINCT message_id)
            FROM moderation_events WHERE user_id = ? AND stage = 'delivery' AND status = 'passed'
            AND id > COALESCE((SELECT ai_review_reset_id FROM users WHERE user_id = ?), 0)
        ''', (user_id, user_id))
        return (await cursor.fetchone())[0]


async def hold(updates, user_id, verdict, operation='new'):
    snapshot = json.dumps([update.to_dict() for update in updates], ensure_ascii=False, sort_keys=True)
    fingerprint = hashlib.sha256((str(user_id) + operation + snapshot).encode()).hexdigest()
    async with db_manager.get_connection() as connection:
        await connection.execute('''INSERT OR IGNORE INTO moderation_reviews
            (fingerprint, user_id, operation, snapshot, status, verdict) VALUES (?, ?, ?, ?, ?, ?)
        ''', (fingerprint, user_id, operation, snapshot,
              'rejected' if verdict['status'] == 'blocked' else 'held',
              json.dumps(verdict, ensure_ascii=False)))
        cursor = await connection.execute('SELECT id FROM moderation_reviews WHERE fingerprint = ?', (fingerprint,))
        review_id = (await cursor.fetchone())[0]
        await connection.commit()
        return review_id


async def get_review(review_id):
    async with db_manager.get_connection() as connection:
        cursor = await connection.execute('SELECT * FROM moderation_reviews WHERE id = ?', (review_id,))
        row = await cursor.fetchone()
        return dict(zip([col[0] for col in cursor.description], row)) if row else None


async def list_reviews(before=None, limit=5):
    async with db_manager.get_connection() as connection:
        cursor = await connection.execute('''SELECT * FROM moderation_reviews
            WHERE status IN ('held', 'rejected', 'uncertain') AND (? IS NULL OR id < ?)
            ORDER BY id DESC LIMIT ?''', (before, before, limit))
        return [dict(zip([col[0] for col in cursor.description], row)) for row in await cursor.fetchall()]


async def claim_review(review_id, admin_id):
    async with db_manager.get_connection() as connection:
        cursor = await connection.execute('''UPDATE moderation_reviews
            SET status = 'sending', reviewed_by = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status IN ('held', 'rejected')''', (admin_id, review_id))
        await connection.commit()
        return cursor.rowcount == 1


async def resolve_review(review_id, status, admin_id, resolution):
    async with db_manager.get_connection() as connection:
        await connection.execute('''UPDATE moderation_reviews SET status = ?, reviewed_by = ?,
            resolution = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?''',
            (status, admin_id, resolution, review_id))
        await connection.commit()


async def record_verification(user_id, challenge, event):
    async with db_manager.get_connection() as connection:
        await connection.execute('''INSERT INTO verification_events
            (user_id, source, provider, model, fallback_reason, event) VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, challenge.get('source', 'unknown'), challenge.get('provider'),
              challenge.get('model'), challenge.get('fallback_reason'), event))
        await connection.commit()


async def latest_verification(user_id):
    async with db_manager.get_connection() as connection:
        cursor = await connection.execute(
            'SELECT * FROM verification_events WHERE user_id = ? ORDER BY id DESC LIMIT 1', (user_id,))
        row = await cursor.fetchone()
        return dict(zip([col[0] for col in cursor.description], row)) if row else None
