import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from config import config
from database import models as db


logger = logging.getLogger(__name__)


class TopicDeletedError(Exception):
    """The stored topic was deleted; the sender must verify again."""



def build_direct_contact_url(username: str | None) -> str | None:
    if not username:
        return None
    return f"tg://resolve?domain={username}"


def build_user_info_markdown(user) -> str:
    name_parts = [part.strip() for part in [user.first_name or "", user.last_name or ""] if part and part.strip()]
    display_name = " ".join(name_parts) if name_parts else str(user.id)
    username = f"@{user.username}" if user.username else "未设置"
    first_contact = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    escaped_display_name = escape_markdown(display_name, version=2)
    escaped_username = escape_markdown(username, version=2)
    escaped_first_contact = escape_markdown(first_contact, version=2)

    return (
        "*用户信息*\n\n"
        f"*名称:* {escaped_display_name}\n"
        f"*TG ID:* `{user.id}`\n"
        f"*用户名:* {escaped_username}\n"
        f"*首次联系:* {escaped_first_contact}"
    )


async def get_or_create_thread(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[int, bool]:
    user = update.effective_user
    user_data = await db.get_user(user.id)

    if user_data and user_data.get('thread_id'):
        existing_thread = user_data['thread_id']
        # Validate the destination before sending: stale topic ids must never
        # be silently routed to the General topic. This also reopens a closed topic.
        try:
            await context.bot.reopen_forum_topic(
                chat_id=config.FORUM_GROUP_ID, message_thread_id=existing_thread)
            return existing_thread, False
        except BadRequest as error:
            reason = str(error).lower()
            if 'not modified' in reason or 'not_modified' in reason:
                return existing_thread, False
            if any(word in reason for word in ('thread not found', 'topic not found',
                                               'thread is invalid', 'thread_id_invalid',
                                               'topic_id_invalid', 'topic_deleted')):
                raise TopicDeletedError(existing_thread) from error
            else:
                logger.warning('无法确认话题 %s 状态，停止发送: %s', existing_thread, reason)
                return None, False
        except Exception:
            logger.exception('无法确认话题 %s 状态，停止发送', existing_thread)
            return None, False

    topic_name = f"{user.first_name} (ID: {user.id})"
    try:
        topic = await context.bot.create_forum_topic(
            chat_id=config.FORUM_GROUP_ID,
            name=topic_name
        )
    except Exception:
        logger.exception("创建用户 %s 的话题失败", user.id)
        return None, False

    thread_id = topic.message_thread_id
    thread_saved = False
    try:
        await db.update_user_thread_id(user.id, thread_id)
        thread_saved = True
    except Exception:
        logger.exception("保存用户 %s 的话题映射失败", user.id)
        try:
            saved_user = await db.get_user(user.id)
            thread_saved = bool(
                saved_user and saved_user.get("thread_id") == thread_id
            )
        except Exception:
            logger.exception("无法确认用户 %s 的话题映射是否已保存", user.id)

    if not thread_saved:
        try:
            await context.bot.delete_forum_topic(
                chat_id=config.FORUM_GROUP_ID,
                message_thread_id=thread_id,
            )
        except Exception:
            logger.exception("删除未落库的话题 %s 失败", thread_id)
            try:
                await context.bot.close_forum_topic(
                    chat_id=config.FORUM_GROUP_ID,
                    message_thread_id=thread_id,
                )
            except Exception:
                logger.exception("关闭未落库的话题 %s 失败", thread_id)
        return None, False

    try:
        await send_user_info_card(update, context, thread_id)
    except Exception:
        logger.exception("发送用户 %s 的信息卡片失败", user.id)

    return thread_id, True


async def build_user_info_card_keyboard(
    user_id: int,
    username: str | None = None
) -> InlineKeyboardMarkup:
    is_blocked, _ = await db.is_blacklisted(user_id)
    is_exempted = await db.is_exempted(user_id)

    if username is None:
        user_data = await db.get_user(user_id)
        username = user_data.get("username") if user_data else None

    row = [
        InlineKeyboardButton(
            "解除封禁" if is_blocked else "封禁",
            callback_data=f"usercard_block_{user_id}"
        ),
        InlineKeyboardButton(
            "取消豁免" if is_exempted else "豁免",
            callback_data=f"usercard_exempt_{user_id}"
        ),
    ]

    direct_contact_url = build_direct_contact_url(username)
    if direct_contact_url:
        row.append(InlineKeyboardButton("直接联系", url=direct_contact_url))

    return InlineKeyboardMarkup([row])


async def send_user_info_card(update: Update, context: ContextTypes.DEFAULT_TYPE, thread_id: int):
    user = update.effective_user
    reply_markup = await build_user_info_card_keyboard(user.id, username=user.username)
    info_text = build_user_info_markdown(user)
    user_data = await db.get_user(user.id) or {}
    verified_at = user_data.get('verified_at') or '未知（旧记录或未验证）'
    method = user_data.get('verification_method') or '未知'
    info_text += ("\n*验证时间 UTC:* " + escape_markdown(verified_at, version=2) +
                  "\n*验证方式:* " + escape_markdown(method, version=2) +
                  "\n审查记录：/audit")

    photos = None
    try:
        photos = await context.bot.get_user_profile_photos(user.id, limit=1)
    except Exception as e:
        print(f"获取用户头像失败: {e}")

    if photos and photos.total_count > 0:
        try:
            await context.bot.send_photo(
                chat_id=config.FORUM_GROUP_ID,
                photo=photos.photos[0][0].file_id,
                caption=info_text,
                message_thread_id=thread_id,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=reply_markup
            )
            return
        except Exception as e:
            print(f"发送头像卡片失败，改用文本卡片: {e}")

    await context.bot.send_message(
        chat_id=config.FORUM_GROUP_ID,
        text=info_text,
        message_thread_id=thread_id,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=reply_markup
    )
