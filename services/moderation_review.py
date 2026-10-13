import json
import logging
from types import SimpleNamespace

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from database import models as db
from database import moderation_store as store
from services.media_group_buffer import enqueue_serialized
from services.snapshot_sender import send_snapshot

logger = logging.getLogger(__name__)


def review_keyboard(review):
    review_id = review['id']
    buttons = [InlineKeyboardButton('确认广告', callback_data=f'review:reject:{review_id}'),
               InlineKeyboardButton('拉黑用户', callback_data=f'review:block:{review_id}')]
    if review['status'] != 'uncertain':
        buttons.insert(0, InlineKeyboardButton('允许发送（人工审核）', callback_data=f'review:allow:{review_id}'))
    else:
        buttons.insert(0, InlineKeyboardButton('确认已发送', callback_data=f'review:sent:{review_id}'))
    return InlineKeyboardMarkup([buttons, [InlineKeyboardButton('查看原始内容', callback_data=f'review:preview:{review_id}')]])


def review_text(review):
    verdict = json.loads(review['verdict'])
    updates = json.loads(review['snapshot'])
    message = updates[0].get('message') or updates[0].get('edited_message') or {}
    text = message.get('text') or message.get('caption') or '[媒体消息]'
    return (f"审核 #{review['id']} · 用户 {review['user_id']}\n"
            f"状态：{review['status']} · 操作：{review['operation']}\n"
            f"原因：{verdict.get('reason', '')}\n"
            f"风险分：{verdict.get('risk_score', '无')} · 服务：{verdict.get('provider', '无')}\n"
            f"内容（预览）：{text[:800]}\n"
            "放行仅针对保存的这一版本，不给予永久审查豁免。")


async def notify_review(context, review_id):
    review = await store.get_review(review_id)
    # Private notices keep unreviewed advertisements out of the normal relay forum.
    for admin_id in config.ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=review_text(review),
                                           reply_markup=review_keyboard(review),
                                           disable_web_page_preview=True)
        except Exception:
            logger.warning('无法私聊通知管理员 %s，审核 #%s 可通过 /review 查看', admin_id, review_id)


async def show_reviews(update, context):
    if not await db.is_admin(update.effective_user.id):
        await update.effective_message.reply_text('您没有权限。')
        return
    before = None
    if context.args:
        try:
            before = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text('用法：/review [上一页最后一个审核编号]')
            return
    reviews = await store.list_reviews(before)
    if not reviews:
        await update.effective_message.reply_text('没有待处理记录。')
        return
    for review in reviews:
        await update.effective_message.reply_text(review_text(review), reply_markup=review_keyboard(review),
                                                  disable_web_page_preview=True)
    await update.effective_message.reply_text(f"查看下一页：/review {reviews[-1]['id']}")


async def show_audit(update, context):
    if not await db.is_admin(update.effective_user.id):
        await update.effective_message.reply_text('您没有权限。')
        return
    try:
        if context.args:
            user_id = int(context.args[0])
        else:
            user = await db.get_user_by_thread_id(update.effective_message.message_thread_id)
            user_id = user['user_id']
    except (ValueError, TypeError, KeyError, AttributeError):
        await update.effective_message.reply_text('用法：/audit <用户ID>，或在对应话题中发送 /audit')
        return
    user = await db.get_user(user_id) or {}
    events = await store.recent_events(user_id)
    lines = [f'用户 {user_id}', f"验证时间（UTC）：{user.get('verified_at') or '未知（旧记录或未验证）'}",
             f"验证方式：{user.get('verification_method') or '未知'}"]
    latest = await store.latest_verification(user_id)
    if latest:
        source_label = {'ai': 'AI 生成', 'local': '本地题库'}.get(latest['source'], '未知')
        lines.append(f"题目来源：{source_label}（{latest['event']}）")
        if latest.get('provider'):
            lines.append(f"出题服务/模型：{latest['provider']} / {latest.get('model') or '-'}")
        if latest.get('fallback_reason'):
            lines.append(f"本地题库回退原因：{latest['fallback_reason']}")
    else:
        lines.append('题目来源：未知（更新前的记录无法追溯）')
    reviewed_count = await store.passed_message_count(user_id)
    lines.append(f"本轮 AI 审核通过条数：{reviewed_count}/{config.NEW_USER_STRICT_MESSAGES}")
    for event in events:
        lines.append(f"\n{event['created_at']} 消息 {event['message_id']}\n"
                     f"{event['stage']} / {event['status']} / {event.get('risk_score')}\n"
                     f"{event.get('provider') or '-'} / {event.get('model') or '-'}\n"
                     f"{(event.get('reason') or '')[:180]} {event.get('error') or ''}")
    await update.effective_message.reply_text('\n'.join(lines)[:4000])


async def handle_review_callback(query, context):
    if not await db.is_admin(query.from_user.id):
        await query.answer('您没有权限。', show_alert=True)
        return
    try:
        _, action, review_id_text = query.data.split(':')
        review_id = int(review_id_text)
    except (ValueError, AttributeError):
        return
    review = await store.get_review(review_id)
    if not review:
        await query.message.reply_text('审核记录不存在。')
        return
    if action == 'preview':
        for raw in json.loads(review['snapshot']):
            message = Update.de_json(raw, context.bot).effective_message
            try:
                await send_snapshot(context.bot, message, query.from_user.id)
            except Exception:
                await query.message.reply_text('该内容无法显示，文字摘要见审核记录。')
        return
    if action not in {'allow', 'reject', 'block', 'sent'}:
        return

    async def process():
        current = await store.get_review(review_id)
        if current['status'] in {'allowed', 'sending', 'superseded', 'confirmed_ad'}:
            await query.message.reply_text('该记录已处理或已失效，请刷新 /review。')
            return
        if action in {'reject', 'block', 'sent'}:
            if action == 'sent' and current['status'] != 'uncertain':
                return
            if action == 'block':
                await db.add_to_blacklist(current['user_id'], '管理员确认广告', query.from_user.id, permanent=True)
            await store.resolve_review(review_id, 'allowed' if action == 'sent' else 'confirmed_ad',
                                       query.from_user.id, action)
        else:
            blocked, _ = await db.is_blacklisted(current['user_id'])
            if blocked:
                await query.message.reply_text('该用户已被封禁，请先解封再处理。')
                return
            if not await store.claim_review(review_id, query.from_user.id):
                await query.message.reply_text('正在处理或发送结果未确认，请核对话题后处理。')
                return
            updates = [Update.de_json(raw, context.bot) for raw in json.loads(current['snapshot'])]
            user_context = SimpleNamespace(bot=context.bot, application=context.application,
                                           bot_data=context.bot_data, user_data={}, args=[])
            try:
                if current['operation'] == 'edit':
                    from services.message_relay import sync_edited_message
                    success = await sync_edited_message(context.bot, updates[0].effective_message,
                                                       current['user_id'], 'user')
                else:
                    from handlers.user_handler import _forward_verified_messages
                    success = await _forward_verified_messages(updates, user_context,
                                                               current['user_id'], moderation_approved=True)
                if not success:
                    raise RuntimeError('Delivery not confirmed')
                for item in updates:
                    await store.audit(current['user_id'], item.effective_message,
                                      {'status': 'manual_allowed', 'reason': f'管理员 {query.from_user.id} 人工放行'})
                await store.resolve_review(review_id, 'allowed', query.from_user.id, '人工放行成功')
            except Exception:
                logger.exception('审核 #%s 放行未确认', review_id)
                await store.resolve_review(review_id, 'uncertain', query.from_user.id, '发送结果未确认，请核对话题')
                await query.message.reply_text('发送结果未确认，为避免重复发送，请先核对话题。')
                return
        await query.edit_message_text(f'审核 #{review_id} 已处理：{action}', reply_markup=None)

    accepted = enqueue_serialized(context, review['user_id'], process)
    if not accepted:
        await query.message.reply_text('队列繁忙，请稍后再试。')
