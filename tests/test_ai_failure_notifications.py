from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from services.ai_alerts import classify_error, notify_ai_failure
from config import config


def test_auth_error_is_recognized_through_wrapped_cause():
    inner = RuntimeError('secret must not be displayed')
    inner.status_code = 401
    wrapped = RuntimeError('provider failed')
    wrapped.__cause__ = inner
    kind, detail = classify_error(wrapped)
    assert kind == 'credentials'
    assert 'secret' not in detail


@pytest.mark.asyncio
async def test_alert_is_limited_per_admin(monkeypatch):
    monkeypatch.setattr(config, 'ADMIN_IDS', [11, 12])
    monkeypatch.setattr(config, 'AI_ALERT_INTERVAL_SECONDS', 3600)
    context = SimpleNamespace(bot_data={}, bot=SimpleNamespace(send_message=AsyncMock()))
    verdict = dict(error='credentials', error_detail='密钥无效')
    await notify_ai_failure(context, verdict)
    await notify_ai_failure(context, verdict)
    assert context.bot.send_message.await_count == 2
    assert {call.kwargs['chat_id'] for call in context.bot.send_message.await_args_list} == {11, 12}


@pytest.mark.asyncio
async def test_failed_private_alert_falls_back_to_forum(monkeypatch):
    monkeypatch.setattr(config, 'ADMIN_IDS', [11])
    monkeypatch.setattr(config, 'FORUM_GROUP_ID', -100123)
    sender = AsyncMock(side_effect=[RuntimeError('private chat blocked'), None])
    context = SimpleNamespace(bot_data={}, bot=SimpleNamespace(send_message=sender))
    await notify_ai_failure(context, dict(error='credentials', error_detail='密钥无效'))
    assert sender.await_args_list[-1].kwargs['chat_id'] == -100123
