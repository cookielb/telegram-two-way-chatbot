import asyncio
import logging

from config import config
from database import models as db
from database import moderation_store as store
from services.gemini_service import gemini_service
from services.ai_alerts import notify_ai_failure, classify_error
from services.moderation_rules import message_text, quick_advertising_check
from utils.media_converter import sticker_to_image

logger = logging.getLogger(__name__)


async def assess(message, user_id, *, precheck=False):
    if await db.is_exempted(user_id):
        return {'status': 'skipped', 'is_spam': False, 'reason': '管理员设置的内容审查豁免'}
    if not config.ENABLE_AI_FILTER:
        return {'status': 'skipped', 'is_spam': False, 'reason': '管理员关闭了 AI 审查'}
    local = quick_advertising_check(message_text(message))
    if local:
        return local
    if precheck:
        return {'status': 'precheck_passed', 'reason': '快速广告规则未命中，不等于 AI 审查通过'}
    # Only the first N delivered messages start AI moderation. Later messages
    # remain subject to blacklist, rate limits and local safety checks.
    if await store.passed_message_count(user_id) >= config.NEW_USER_STRICT_MESSAGES:
        return {'status': 'bypassed_after_initial_review', 'is_spam': False,
                'category': 'safe', 'reason': '用户已完成前几条消息审查，按配置跳过 AI 审核'}

    try:
        image_bytes = None
        if message.photo:
            photo_file = await asyncio.wait_for(message.photo[-1].get_file(), config.AI_TIMEOUT_SECONDS)
            image_bytes = await asyncio.wait_for(photo_file.download_as_bytearray(), config.AI_TIMEOUT_SECONDS)
        elif message.sticker and not message.sticker.is_animated and not message.sticker.is_video:
            sticker_file = await asyncio.wait_for(message.sticker.get_file(), config.AI_TIMEOUT_SECONDS)
            sticker_bytes = await asyncio.wait_for(sticker_file.download_as_bytearray(), config.AI_TIMEOUT_SECONDS)
            image_bytes = await sticker_to_image(sticker_bytes)
        # No claim that video/audio/documents are scanned: quarantine them for human review.
        unsupported = any(getattr(message, kind, None) for kind in
                          ('video', 'animation', 'video_note', 'voice', 'audio', 'document', 'story'))
        unsupported = unsupported or bool(message.sticker and
                                           (message.sticker.is_animated or message.sticker.is_video))
        if unsupported or (not message_text(message).strip() and not image_bytes):
            return {'status': 'review', 'is_spam': False, 'category': 'uncertain',
                    'reason': '该媒体内容尚无法完整自动审查，需要人工审核'}
        strict = await store.passed_message_count(user_id) < config.NEW_USER_STRICT_MESSAGES
        return await gemini_service.analyze_message(message, image_bytes, strict=strict)
    except Exception as error:
        logger.warning('内容审查预处理失败: %s', type(error).__name__)
        kind, detail = classify_error(error)
        return {'status': 'error', 'is_spam': False, 'reason': '审查异常，直接放行并通知管理员',
                'error': kind, 'error_detail': detail}


async def check_batch(updates, context, user_id, *, operation='new', precheck=False):
    from services.moderation_review import notify_review
    messages = [update.effective_message for update in updates]
    for message in messages:
        verdict = await assess(message, user_id, precheck=precheck)
        if verdict['status'] == 'error':
            verdict = {**verdict, 'status': 'error_allowed'}
            try:
                await notify_ai_failure(context, verdict)
            except Exception:
                logger.exception('AI 异常通知失败，继续转发消息')
        await store.audit(user_id, message, verdict, 'precheck' if precheck else 'delivery')
        if verdict['status'] not in {'passed', 'skipped', 'precheck_passed', 'error_allowed', 'bypassed_after_initial_review'}:
            review_id = await store.hold(updates, user_id, verdict, operation)
            if verdict['status'] == 'blocked':
                await db.save_filtered_message(user_id, message.message_id,
                                               message.text or message.caption,
                                               verdict.get('reason', '广告内容'))
            try:
                await messages[0].reply_text(
                    '消息已被拦截，未传递给管理员。' if verdict['status'] == 'blocked' else
                    '消息已保存，等待管理员审核；请勿重复发送。')
            except Exception:
                logger.exception('通知用户审查结果失败，消息已保存')
            await notify_review(context, review_id)
            return False
    return True
