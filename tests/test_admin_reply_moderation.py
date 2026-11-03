from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from config import config
from database import models as db
from database.db_manager import DatabaseManager
from handlers import admin_handler
from services import moderation


@pytest_asyncio.fixture
async def conversation(tmp_path, monkeypatch):
    manager = DatabaseManager()
    monkeypatch.setattr(manager, 'db_path', str(tmp_path / 'reply.sqlite3'))
    await manager.initialize()
    await db.add_user(42, None, 'Test')
    await db.update_user_thread_id(42, 77)
    monkeypatch.setattr(config, 'ENABLE_AI_FILTER', True)
    monkeypatch.setattr(config, 'NEW_USER_STRICT_MESSAGES', 3)
    return manager


def make_message(message_id=1):
    return SimpleNamespace(
        message_id=message_id, chat_id=42, message_thread_id=77,
        text='hello', caption=None, entities=[], caption_entities=[],
        photo=[], sticker=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('size', [1, 2])
@pytest.mark.parametrize('failed', [False, True])
async def test_reply_bypasses_ai_only_after_success(conversation, monkeypatch, size, failed):
    updates = [SimpleNamespace(effective_message=make_message(i)) for i in range(size)]
    sender = AsyncMock(side_effect=RuntimeError('send failed') if failed else None)
    monkeypatch.setattr(admin_handler, '_send_reply_to_user', sender)
    monkeypatch.setattr(admin_handler, 'relay_media_group', sender)
    monkeypatch.setattr(moderation, 'quick_advertising_check', lambda text: None)
    analyze = AsyncMock(return_value={'status': 'passed'})
    monkeypatch.setattr(moderation.gemini_service, 'analyze_message', analyze)

    if failed:
        with pytest.raises(RuntimeError, match='send failed'):
            await admin_handler._handle_admin_messages(updates, SimpleNamespace(bot=None))
    else:
        await admin_handler._handle_admin_messages(updates, SimpleNamespace(bot=None))

    assert await db.has_admin_replied(42) is not failed
    result = await moderation.assess(make_message(), 42)
    assert result['status'] == ('passed' if failed else 'bypassed_after_admin_reply')
    assert analyze.await_count == (1 if failed else 0)


@pytest.mark.asyncio
async def test_reply_trust_survives_initialization_and_resets_with_topic(conversation):
    await db.mark_admin_replied(42, 77)
    await conversation.initialize()
    assert await db.has_admin_replied(42)
    await db.reset_user_relay_state(42)
    assert not await db.has_admin_replied(42)
    await db.update_user_thread_id(42, 88)
    await db.mark_admin_replied(42, 77)
    assert not await db.has_admin_replied(42)
    await db.mark_admin_replied(42, 88)
    assert await db.has_admin_replied(42)
    assert not await db.has_admin_replied(999)


@pytest.mark.asyncio
async def test_upgrade_adds_flag_without_trusting_old_users(conversation):
    async with conversation.get_connection() as connection:
        await connection.execute('ALTER TABLE users DROP COLUMN admin_replied')
        await connection.commit()
    await conversation.initialize()
    await conversation.initialize()
    user = await db.get_user(42)
    assert user['first_name'] == 'Test'
    assert user['thread_id'] == 77
    assert not await db.has_admin_replied(42)


@pytest.mark.asyncio
async def test_local_rules_still_apply_after_reply(conversation, monkeypatch):
    await db.mark_admin_replied(42, 77)
    verdict = {'status': 'review', 'reason': 'local advertising rule'}
    monkeypatch.setattr(moderation, 'quick_advertising_check', lambda text: verdict)
    analyze = AsyncMock()
    monkeypatch.setattr(moderation.gemini_service, 'analyze_message', analyze)
    assert await moderation.assess(make_message(), 42) == verdict
    analyze.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_allows_and_audits_reply_bypass(conversation, monkeypatch):
    await db.mark_admin_replied(42, 77)
    monkeypatch.setattr(moderation, 'quick_advertising_check', lambda text: None)
    message = make_message()
    assert await moderation.check_batch(
        [SimpleNamespace(effective_message=message)], SimpleNamespace(), 42,
    )
    events = await moderation.store.recent_events(42)
    assert events[0]['status'] == 'bypassed_after_admin_reply'
    assert await moderation.store.passed_message_count(42) == 0


@pytest.mark.asyncio
async def test_unauthorized_sender_cannot_trigger_reply(conversation, monkeypatch):
    message = make_message()
    message.is_topic_message = True
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=9))
    monkeypatch.setattr(db, 'is_admin', AsyncMock(return_value=False))
    enqueue = AsyncMock()
    monkeypatch.setattr(admin_handler, 'enqueue_message', enqueue)
    await admin_handler.handle_admin_reply(update, SimpleNamespace())
    enqueue.assert_not_awaited()
    assert not await db.has_admin_replied(42)
