import asyncio
import logging
import re
import secrets
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes
from services.verification import verify_answer
from database import models as db
from services.thread_manager import build_user_info_card_keyboard
from services.media_group_buffer import enqueue_serialized
from .user_handler import _forward_verified_messages
from config import config

PANEL_CACHE_KEY = "panel_reference_cache"
AI_MODELS_PER_PAGE = 5

logger = logging.getLogger(__name__)


def _cache_panel_reference(application, kind, payload):
    token = secrets.token_hex(6)
    cache = application.bot_data.setdefault(PANEL_CACHE_KEY, {})
    if len(cache) >= 500:
        cache.clear()
    cache[token] = (kind, payload)
    return token


def _resolve_panel_reference(application, token, expected_kind):
    cache = application.bot_data.get(PANEL_CACHE_KEY, {})
    value = cache.get(token)
    if not value:
        return None
    kind, payload = value
    if kind != expected_kind:
        return None
    return payload


async def _refresh_usercard_keyboard(query, target_user_id: int):
    keyboard = await build_user_info_card_keyboard(target_user_id)

    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except BadRequest as exc:
        if "message is not modified" not in exc.message.lower():
            raise


def _build_ai_model_selection_view(application, provider_type: str, feature_type: str, models: list, page: int = 1):
    total = len(models)
    total_pages = max(1, (total + AI_MODELS_PER_PAGE - 1) // AI_MODELS_PER_PAGE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * AI_MODELS_PER_PAGE
    page_models = models[start:start + AI_MODELS_PER_PAGE]

    feature_name_map = {
        'filter': '内容审查',
        'verification': '验证码生成',
        'autoreply': '自动回复'
    }
    feature_name = feature_name_map.get(feature_type, feature_type)

    keyboard = []
    for model in page_models:
        token = _cache_panel_reference(
            application,
            "ai_model",
            {
                "provider_type": provider_type,
                "feature_type": feature_type,
                "model_name": model,
            },
        )
        keyboard.append([InlineKeyboardButton(model, callback_data=f"setmref:{token}")])

    nav_buttons = []
    callback_prefix = f"ai_select_model_{provider_type}_{feature_type}"
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("上一页", callback_data=f"{callback_prefix}_{page - 1}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("下一页", callback_data=f"{callback_prefix}_{page + 1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("返回上一级", callback_data=f"ai_config_models_{provider_type}")])

    message = (
        f"请选择 {provider_type.upper()} {feature_name} 模型:\n"
        f"第 {page}/{total_pages} 页，共 {total} 个模型"
    )
    return message, InlineKeyboardMarkup(keyboard)


def _copy_pending_batches(context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending_updates")
    legacy_pending = context.user_data.get("pending_update")
    if not pending:
        return [[legacy_pending]] if legacy_pending else []
    if isinstance(pending[0], (list, tuple)):
        batches = [list(batch) for batch in pending]
    else:
        batches = [list(pending)]
    if legacy_pending:
        batches.insert(0, [legacy_pending])
    return batches


async def _forward_pending_batches(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> bool:
    batches = _copy_pending_batches(context)
    if not batches:
        return False

    context.user_data.pop("pending_update", None)
    context.user_data["pending_updates"] = batches
    while batches:
        active_batches = batches
        outcome = await _forward_verified_messages(
            batches[0],
            context,
            user_id,
        )
        if outcome is False:
            if context.user_data.get("pending_updates") is not active_batches:
                replacement = _copy_pending_batches(context)
                context.user_data["pending_updates"] = replacement + batches[1:]
            return False
        batches = batches[1:]
        if batches:
            context.user_data["pending_updates"] = batches
        else:
            context.user_data.pop("pending_updates", None)
    return True


async def _process_verification_callback(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    challenge_id: str,
    option_index: int,
):
    result = await verify_answer(user_id, challenge_id, option_index)
    success, message, is_banned, new_question = result[:4]
    ignored = len(result) > 4 and result[4]

    if ignored:
        return

    if is_banned:
        await query.edit_message_text(text=message, reply_markup=None)
        return

    if new_question:
        new_question_text, new_keyboard = new_question
        await query.edit_message_text(
            text=f"{message}\n\n{new_question_text}",
            reply_markup=new_keyboard,
        )
        return

    try:
        await query.edit_message_text(text=message)
    except TelegramError:
        logger.exception("无法更新验证结果消息，继续处理待转发消息")
    if success:
        had_pending = bool(_copy_pending_batches(context))
        forwarded = await _forward_pending_batches(context, user_id)
        if not had_pending and not forwarded:
            await query.message.reply_text("现在您可以发送消息了！")
        elif had_pending and not forwarded:
            user_data = await db.get_user(user_id)
            if user_data and user_data.get("is_verified"):
                await query.message.reply_text(
                    "验证已完成，但待发送消息暂时传递失败；消息已保留，"
                    "您下次发送消息时会自动重试。"
                )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = query.from_user.id
    
    if data.startswith('review:'):
        from services.moderation_review import handle_review_callback
        await handle_review_callback(query, context)
        return

    if data == 'panel_reviews':
        if not await db.is_admin(user_id):
            return
        from services.moderation_review import show_reviews
        from types import SimpleNamespace
        await show_reviews(SimpleNamespace(effective_user=query.from_user, effective_message=query.message),
                           SimpleNamespace(args=[]))
        return

    if data.startswith("verify:"):
        try:
            _, challenge_id, option_index_text = data.split(":", 2)
            option_index = int(option_index_text)
        except (TypeError, ValueError):
            return

        async def process_verification():
            try:
                return await _process_verification_callback(
                    query,
                    context,
                    user_id,
                    challenge_id,
                    option_index,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("后台处理用户 %s 的验证回调失败", user_id)
                try:
                    await query.message.reply_text(
                        "验证处理暂时失败，请重新点击当前验证按钮。"
                    )
                except TelegramError:
                    logger.exception("无法通知用户验证回调处理失败")
                return None

        if hasattr(context, "application") and hasattr(context, "bot_data"):
            accepted = enqueue_serialized(
                context,
                user_id,
                process_verification,
            )
            if not accepted:
                await query.message.reply_text(
                    "当前消息队列繁忙，请稍后重新点击验证按钮。"
                )
        else:
            await process_verification()
        return
    
    if data == "panel_back":
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        total_users = await db.get_total_users_count()
        blocked_users = await db.get_blocked_users_count()
        exempted_users = await db.get_exemptions_count()
        is_enabled = await db.get_autoreply_enabled()
        
        message = (
            f"管理面板\n\n"
            f"统计信息:\n\n"
            f"总用户数: {total_users}\n"
            f"黑名单用户数: {blocked_users}\n"
            f"豁免用户数: {exempted_users}\n"
            f"自动回复状态: {'已启用' if is_enabled else '已禁用'}\n\n"
            f"请选择要查看的功能："
        )
        
        keyboard = [
            [InlineKeyboardButton("黑名单管理", callback_data="panel_blacklist_page_1"), InlineKeyboardButton("所有用户信息", callback_data="panel_stats")],
            [InlineKeyboardButton("被过滤消息", callback_data="panel_filtered_page_1"), InlineKeyboardButton("自动回复管理", callback_data="panel_autoreply")],
            [InlineKeyboardButton("豁免名单管理", callback_data="panel_exemptions_page_1")],
            [InlineKeyboardButton("AI 模型设置", callback_data="panel_ai_settings")],
            [InlineKeyboardButton("待审核消息", callback_data="panel_reviews")],
        ]
        
        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif data.startswith("panel_blacklist_page_"):
        from services import blacklist
        
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        try:
            page = int(data.split("_")[3])
        except (ValueError, IndexError):
            await query.answer("无效的页码。", show_alert=True)
            return
        
        message, keyboard = await blacklist.get_blacklist_keyboard(page=page)
        
        if keyboard:
            keyboard_buttons = list(keyboard.inline_keyboard)
            keyboard_buttons.append([InlineKeyboardButton("返回主面板", callback_data="panel_back")])
            keyboard = InlineKeyboardMarkup(keyboard_buttons)
        
        if keyboard:
            await query.edit_message_text(
                text=message,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        else:
            back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("返回主面板", callback_data="panel_back")]])
            await query.edit_message_text(text=message, reply_markup=back_keyboard)
    
    elif data == "panel_stats":
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        from services.blacklist import get_all_users_keyboard
        
        page = 1
        message, keyboard = await get_all_users_keyboard(
            page=page,
            callback_prefix="panel_stats_all_users_page_",
            back_callback="panel_back",
            back_text="返回主面板"
        )
        
        if keyboard:
            await query.edit_message_text(
                text=message,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        else:
            back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("返回主面板", callback_data="panel_back")]])
            await query.edit_message_text(text=message, reply_markup=back_keyboard, parse_mode='Markdown')
    
    elif data.startswith("panel_stats_all_users_page_"):
        from services.blacklist import get_all_users_keyboard
        
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        try:
            page = int(data.split("_")[5])
        except (ValueError, IndexError):
            await query.answer("无效的页码。", show_alert=True)
            return
        
        message, keyboard = await get_all_users_keyboard(
            page=page,
            callback_prefix="panel_stats_all_users_page_",
            back_callback="panel_back",
            back_text="返回主面板"
        )
        
        if keyboard:
            await query.edit_message_text(
                text=message,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        else:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("返回主面板", callback_data="panel_back")]])
            await query.edit_message_text(
                text=message,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
    
    elif data.startswith("panel_stats_blacklist_page_"):
        from services.blacklist import get_blacklist_keyboard_detailed
        
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        try:
            page = int(data.split("_")[4])
        except (ValueError, IndexError):
            await query.answer("无效的页码。", show_alert=True)
            return
        
        message, keyboard = await get_blacklist_keyboard_detailed(page=page)
        
        if keyboard:
            keyboard_buttons = [list(row) for row in keyboard.inline_keyboard]
            for i, row in enumerate(keyboard_buttons):
                for j, button in enumerate(row):
                    if button.callback_data == "stats_back_to_menu":
                        keyboard_buttons[i][j] = InlineKeyboardButton("返回主面板", callback_data="panel_back")
                        break
            keyboard = InlineKeyboardMarkup(keyboard_buttons)
        
        if keyboard:
            await query.edit_message_text(
                text=message,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        else:
            back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("返回主面板", callback_data="panel_back")]])
            await query.edit_message_text(text=message, reply_markup=back_keyboard, parse_mode='Markdown')
    
    elif data.startswith("panel_filtered_page_"):
        from .admin_handler import _format_filtered_messages, _get_filtered_messages_keyboard
        
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        try:
            page = int(data.split("_")[3])
        except (ValueError, IndexError):
            await query.answer("无效的页码。", show_alert=True)
            return
        
        MESSAGES_PER_PAGE = 5

        total_count = await db.get_filtered_messages_count()
        
        if total_count == 0:
            back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("返回主面板", callback_data="panel_back")]])
            await query.edit_message_text("没有找到被过滤的消息。", reply_markup=back_keyboard)
            return
        
        total_pages = (total_count + MESSAGES_PER_PAGE - 1) // MESSAGES_PER_PAGE

        if page < 1:
            page = 1
        elif page > total_pages:
            page = total_pages

        offset = (page - 1) * MESSAGES_PER_PAGE

        messages = await db.get_filtered_messages(MESSAGES_PER_PAGE, offset)
        
        if not messages:
            back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("返回主面板", callback_data="panel_back")]])
            await query.edit_message_text("没有找到被过滤的消息。", reply_markup=back_keyboard)
            return

        response = await _format_filtered_messages(messages, page, total_pages)

        keyboard = await _get_filtered_messages_keyboard(page, total_pages, callback_prefix="panel_filtered_page_")
        
        if keyboard:
            keyboard_buttons = [list(row) for row in keyboard.inline_keyboard]
            keyboard_buttons.append([InlineKeyboardButton("返回主面板", callback_data="panel_back")])
            keyboard = InlineKeyboardMarkup(keyboard_buttons)
        else:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("返回主面板", callback_data="panel_back")]])

        await query.edit_message_text(response, reply_markup=keyboard)
    
    elif data == "panel_autoreply":
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        is_enabled = await db.get_autoreply_enabled()
        status_text = "已启用" if is_enabled else "已禁用"
        
        message = (
            f"自动回复管理\n\n"
            f"当前状态: {status_text}\n\n"
            f"请选择操作："
        )
        
        keyboard = [
            [
                InlineKeyboardButton(
                    "关闭自动回复" if is_enabled else "开启自动回复",
                    callback_data="panel_autoreply_toggle"
                )
            ],
            [InlineKeyboardButton("管理知识库", callback_data="panel_autoreply_kb_list_page_1")],
            [InlineKeyboardButton("添加知识条目", callback_data="panel_autoreply_kb_add")],
            [InlineKeyboardButton("返回主面板", callback_data="panel_back")],
        ]
        
        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif data == "panel_ai_settings":
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
            
        async with db.db_manager.get_connection() as conn:
             cursor = await conn.execute("""
                SELECT key, value FROM settings 
                WHERE key IN (
                    'ai_provider', 
                    'gemini_model_filter', 'gemini_model_verification', 'gemini_model_autoreply',
                    'openai_model_filter', 'openai_model_verification', 'openai_model_autoreply'
                )
             """)
             settings = {row[0]: row[1] for row in await cursor.fetchall()}
             
        current_provider = settings.get('ai_provider', 'gemini')
        
        provider_name = "Gemini" if current_provider == 'gemini' else "OpenAI"
        
        message = (
            f"🤖 **AI 模型设置**\n\n"
            f"当前提供商: `{provider_name}`\n\n"
            f"**Gemini 模型**:\n"
            f"• 审查: `{settings.get('gemini_model_filter', 'N/A')}`\n"
            f"• 验证: `{settings.get('gemini_model_verification', 'N/A')}`\n"
            f"• 回复: `{settings.get('gemini_model_autoreply', 'N/A')}`\n\n"
            f"**OpenAI 模型**:\n"
            f"• 审查: `{settings.get('openai_model_filter', 'N/A')}`\n"
            f"• 验证: `{settings.get('openai_model_verification', 'N/A')}`\n"
            f"• 回复: `{settings.get('openai_model_autoreply', 'N/A')}`\n\n"
            f"请选择要配置的项目:"
        )
        
        keyboard = [
            [
                InlineKeyboardButton(f"{'✅ ' if current_provider == 'gemini' else ''}使用 Gemini", callback_data="ai_set_provider_gemini"),
                InlineKeyboardButton(f"{'✅ ' if current_provider == 'openai' else ''}使用 OpenAI", callback_data="ai_set_provider_openai")
            ],
            [
                InlineKeyboardButton("配置 Gemini 模型", callback_data="ai_config_models_gemini"),
                InlineKeyboardButton("配置 OpenAI 模型", callback_data="ai_config_models_openai")
            ],
            [InlineKeyboardButton("返回主面板", callback_data="panel_back")]
        ]
        
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    elif data.startswith("ai_set_provider_"):
        if not await db.is_admin(user_id): return
        
        new_provider = data.split("_")[3]
        async with db.db_manager.get_connection() as conn:
            await conn.execute("UPDATE settings SET value = ? WHERE key = 'ai_provider'", (new_provider,))
            await conn.commit()
            
        await query.answer(f"已切换 AI 提供商为 {new_provider.upper()}")
        
        async with db.db_manager.get_connection() as conn:
             cursor = await conn.execute("""
                SELECT key, value FROM settings 
                WHERE key IN (
                    'ai_provider', 
                    'gemini_model_filter', 'gemini_model_verification', 'gemini_model_autoreply',
                    'openai_model_filter', 'openai_model_verification', 'openai_model_autoreply'
                )
             """)
             settings = {row[0]: row[1] for row in await cursor.fetchall()}
             
        current_provider = settings.get('ai_provider', 'gemini')
        provider_name = "Gemini" if current_provider == 'gemini' else "OpenAI"
        
        message = (
            f"🤖 **AI 模型设置**\n\n"
            f"当前提供商: `{provider_name}`\n\n"
            f"**Gemini 模型**:\n"
            f"• 审查: `{settings.get('gemini_model_filter', 'N/A')}`\n"
            f"• 验证: `{settings.get('gemini_model_verification', 'N/A')}`\n"
            f"• 回复: `{settings.get('gemini_model_autoreply', 'N/A')}`\n\n"
            f"**OpenAI 模型**:\n"
            f"• 审查: `{settings.get('openai_model_filter', 'N/A')}`\n"
            f"• 验证: `{settings.get('openai_model_verification', 'N/A')}`\n"
            f"• 回复: `{settings.get('openai_model_autoreply', 'N/A')}`\n\n"
            f"请选择要配置的项目:"
        )
        
        keyboard = [
            [
                InlineKeyboardButton(f"{'✅ ' if current_provider == 'gemini' else ''}使用 Gemini", callback_data="ai_set_provider_gemini"),
                InlineKeyboardButton(f"{'✅ ' if current_provider == 'openai' else ''}使用 OpenAI", callback_data="ai_set_provider_openai")
            ],
            [
                InlineKeyboardButton("配置 Gemini 模型", callback_data="ai_config_models_gemini"),
                InlineKeyboardButton("配置 OpenAI 模型", callback_data="ai_config_models_openai")
            ],
            [InlineKeyboardButton("返回主面板", callback_data="panel_back")]
        ]
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    elif data.startswith("ai_config_models_"):
        if not await db.is_admin(user_id): return
        
        provider_type = data.split("_")[3]
        
        message = f"请选择要配置的 {provider_type.upper()} 功能模型:"
        
        keyboard = [
            [InlineKeyboardButton("内容审查模型", callback_data=f"ai_select_model_{provider_type}_filter")],
            [InlineKeyboardButton("验证码生成模型", callback_data=f"ai_select_model_{provider_type}_verification")],
            [InlineKeyboardButton("自动回复模型", callback_data=f"ai_select_model_{provider_type}_autoreply")],
            [InlineKeyboardButton("返回设置", callback_data="panel_ai_settings")]
        ]
        
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("ai_select_model_"):
        if not await db.is_admin(user_id): return
        
        parts = data.split("_")
        provider_type = parts[3]
        feature_type = parts[4]
        page = 1
        if len(parts) > 5:
            try:
                page = int(parts[5])
            except ValueError:
                page = 1
        
        from services.ai_service import ai_service
        
        await query.answer("正在获取模型列表...", show_alert=False)
        
        try:
            models = await ai_service.get_available_models(provider_type)
        except Exception as e:
            await query.answer(f"获取模型失败: {e}", show_alert=True)
            return

        if not models:
             await query.answer("未能获取到模型列表，请检查 API Key 配置。", show_alert=True)
             return
        
        message, keyboard = _build_ai_model_selection_view(
            context.application,
            provider_type,
            feature_type,
            models,
            page,
        )
        await query.edit_message_text(message, reply_markup=keyboard)
        return

    elif data.startswith("setmref:"):
        if not await db.is_admin(user_id): return

        token = data.split(":", 1)[1]
        payload = _resolve_panel_reference(context.application, token, "ai_model")
        if not payload:
            await query.answer("模型选择已过期，请重新打开模型列表。", show_alert=True)
            return

        provider_type = payload["provider_type"]
        feature_type = payload["feature_type"]
        model_name = payload["model_name"]
        setting_key = f"{provider_type}_model_{feature_type}"

        async with db.db_manager.get_connection() as conn:
            await conn.execute("UPDATE settings SET value = ? WHERE key = ?", (model_name, setting_key))
            await conn.commit()

        await query.answer(f"已设置 {provider_type.upper()} {feature_type} 模型为 {model_name}")

        message = f"请选择要配置的 {provider_type.upper()} 功能模型:"
        keyboard = [
            [InlineKeyboardButton("内容审查模型", callback_data=f"ai_select_model_{provider_type}_filter")],
            [InlineKeyboardButton("验证码生成模型", callback_data=f"ai_select_model_{provider_type}_verification")],
            [InlineKeyboardButton("自动回复模型", callback_data=f"ai_select_model_{provider_type}_autoreply")],
            [InlineKeyboardButton("返回设置", callback_data="panel_ai_settings")]
        ]
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("setm:"):
        if not await db.is_admin(user_id): return
        
        try:
            _, p_code, f_code, model_name = data.split(":", 3)
        except ValueError:
            await query.answer("无效的请求数据", show_alert=True)
            return
            
        p_map = {'g': 'gemini', 'o': 'openai'}
        f_map = {'f': 'filter', 'v': 'verification', 'a': 'autoreply'}
        
        provider_type = p_map.get(p_code, 'gemini')
        feature_type = f_map.get(f_code, 'filter')
        
        setting_key = f"{provider_type}_model_{feature_type}"
        
        async with db.db_manager.get_connection() as conn:
            await conn.execute("UPDATE settings SET value = ? WHERE key = ?", (model_name, setting_key))
            await conn.commit()
            
        await query.answer(f"已设置 {provider_type.upper()} {feature_type} 模型为 {model_name}")
        
        message = f"请选择要配置的 {provider_type.upper()} 功能模型:"
        keyboard = [
            [InlineKeyboardButton("内容审查模型", callback_data=f"ai_select_model_{provider_type}_filter")],
            [InlineKeyboardButton("验证码生成模型", callback_data=f"ai_select_model_{provider_type}_verification")],
            [InlineKeyboardButton("自动回复模型", callback_data=f"ai_select_model_{provider_type}_autoreply")],
            [InlineKeyboardButton("返回设置", callback_data="panel_ai_settings")]
        ]
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))

    
    elif data == "panel_autoreply_toggle":
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        is_enabled = await db.get_autoreply_enabled()
        await db.set_autoreply_enabled(not is_enabled)
        new_status = "已启用" if not is_enabled else "已禁用"
        await query.answer(f"自动回复已{new_status}", show_alert=True)
        
        is_enabled = await db.get_autoreply_enabled()
        status_text = "已启用" if is_enabled else "已禁用"
        
        message = (
            f"自动回复管理\n\n"
            f"当前状态: {status_text}\n\n"
            f"请选择操作："
        )
        
        keyboard = [
            [
                InlineKeyboardButton(
                    "关闭自动回复" if is_enabled else "开启自动回复",
                    callback_data="panel_autoreply_toggle"
                )
            ],
            [InlineKeyboardButton("管理知识库", callback_data="panel_autoreply_kb_list_page_1")],
            [InlineKeyboardButton("添加知识条目", callback_data="panel_autoreply_kb_add")],
            [InlineKeyboardButton("返回主面板", callback_data="panel_back")],
        ]
        
        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif data.startswith("panel_autoreply_kb_list_page_"):
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        try:
            page = int(data.split("_")[5])
        except (ValueError, IndexError):
            page = 1
        
        entries = await db.get_all_knowledge_entries()
        if not entries:
            back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("返回主面板", callback_data="panel_back")]])
            await query.edit_message_text("知识库为空", reply_markup=back_keyboard)
            return
        
        MESSAGES_PER_PAGE = 5
        total_pages = (len(entries) + MESSAGES_PER_PAGE - 1) // MESSAGES_PER_PAGE
        if page < 1:
            page = 1
        elif page > total_pages:
            page = total_pages
        
        start_idx = (page - 1) * MESSAGES_PER_PAGE
        end_idx = start_idx + MESSAGES_PER_PAGE
        page_entries = entries[start_idx:end_idx]
        
        message = f"知识库条目 (第 {page}/{total_pages} 页)\n\n"
        keyboard = []
        
        for entry in page_entries:
            title = entry['title'][:30] + "..." if len(entry['title']) > 30 else entry['title']
            keyboard.append([
                InlineKeyboardButton(
                    f"{title}",
                    callback_data=f"panel_autoreply_kb_view_{entry['id']}"
                )
            ])
            keyboard.append([
                InlineKeyboardButton(
                    "编辑",
                    callback_data=f"panel_autoreply_kb_edit_{entry['id']}"
                ),
                InlineKeyboardButton(
                    "删除",
                    callback_data=f"panel_autoreply_kb_delete_{entry['id']}"
                )
            ])
        
        nav_buttons = []
        if page > 1:
            nav_buttons.append(InlineKeyboardButton("上一页", callback_data=f"panel_autoreply_kb_list_page_{page-1}"))
        if page < total_pages:
            nav_buttons.append(InlineKeyboardButton("下一页", callback_data=f"panel_autoreply_kb_list_page_{page+1}"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        keyboard.append([InlineKeyboardButton("返回主面板", callback_data="panel_back")])
        
        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif data.startswith("panel_autoreply_kb_view_"):
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        try:
            entry_id = int(data.split("_")[4])
        except (ValueError, IndexError):
            await query.answer("无效的条目ID", show_alert=True)
            return
        
        entry = await db.get_knowledge_entry(entry_id)
        if not entry:
            await query.answer("条目不存在", show_alert=True)
            return
        
        message = (
            f"知识条目详情\n\n"
            f"ID: {entry['id']}\n"
            f"标题: {entry['title']}\n"
            f"内容: {entry['content']}\n\n"
            f"创建时间: {entry['created_at']}\n"
            f"更新时间: {entry['updated_at']}"
        )
        
        keyboard = [
            [
                InlineKeyboardButton("编辑", callback_data=f"panel_autoreply_kb_edit_{entry_id}"),
                InlineKeyboardButton("删除", callback_data=f"panel_autoreply_kb_delete_{entry_id}")
            ],
            [InlineKeyboardButton("返回列表", callback_data="panel_autoreply_kb_list_page_1")],
            [InlineKeyboardButton("返回主面板", callback_data="panel_back")],
        ]
        
        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif data.startswith("panel_autoreply_kb_edit_"):
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        try:
            entry_id = int(data.split("_")[4])
        except (ValueError, IndexError):
            await query.answer("无效的条目ID", show_alert=True)
            return
        
        entry = await db.get_knowledge_entry(entry_id)
        if not entry:
            await query.answer("条目不存在", show_alert=True)
            return
        
        back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("返回主面板", callback_data="panel_back")]])
        await query.edit_message_text(
            f"编辑知识条目\n\n"
            f"ID: {entry['id']}\n"
            f"标题: {entry['title']}\n"
            f"内容: {entry['content']}\n\n"
            f"请使用以下格式发送编辑命令：\n"
            f"`/autoreply edit {entry_id} <新标题> <新内容>`\n\n"
            f"示例：\n"
            f"`/autoreply edit {entry_id} 新标题 新内容`",
            parse_mode='Markdown',
            reply_markup=back_keyboard
        )
    
    elif data.startswith("panel_autoreply_kb_delete_"):
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        try:
            entry_id = int(data.split("_")[4])
        except (ValueError, IndexError):
            await query.answer("无效的条目ID", show_alert=True)
            return
        
        entry = await db.get_knowledge_entry(entry_id)
        if not entry:
            await query.answer("条目不存在", show_alert=True)
            return
        
        await db.delete_knowledge_entry(entry_id)
        await query.answer(f"已删除: {entry['title']}", show_alert=True)
        
        entries = await db.get_all_knowledge_entries()
        if not entries:
            back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("返回主面板", callback_data="panel_back")]])
            await query.edit_message_text("知识库为空", reply_markup=back_keyboard)
            return
        
        page = 1
        MESSAGES_PER_PAGE = 5
        total_pages = (len(entries) + MESSAGES_PER_PAGE - 1) // MESSAGES_PER_PAGE
        
        start_idx = (page - 1) * MESSAGES_PER_PAGE
        end_idx = start_idx + MESSAGES_PER_PAGE
        page_entries = entries[start_idx:end_idx]
        
        message = f"知识库条目 (第 {page}/{total_pages} 页)\n\n"
        keyboard = []
        
        for entry in page_entries:
            title = entry['title'][:30] + "..." if len(entry['title']) > 30 else entry['title']
            keyboard.append([
                InlineKeyboardButton(
                    f"{title}",
                    callback_data=f"panel_autoreply_kb_view_{entry['id']}"
                )
            ])
            keyboard.append([
                InlineKeyboardButton(
                    "编辑",
                    callback_data=f"panel_autoreply_kb_edit_{entry['id']}"
                ),
                InlineKeyboardButton(
                    "删除",
                    callback_data=f"panel_autoreply_kb_delete_{entry['id']}"
                )
            ])
        
        nav_buttons = []
        if page < total_pages:
            nav_buttons.append(InlineKeyboardButton("下一页", callback_data=f"panel_autoreply_kb_list_page_{page+1}"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        keyboard.append([InlineKeyboardButton("返回主面板", callback_data="panel_back")])
        
        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif data == "panel_autoreply_kb_add":
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("返回主面板", callback_data="panel_back")]])
        await query.edit_message_text(
            "添加知识条目\n\n"
            "请使用以下格式发送新条目：\n"
            "`/autoreply add <标题> <内容>`\n\n"
            "示例：\n"
            "`/autoreply add 常见问题 这是问题的答案`",
            parse_mode='Markdown',
            reply_markup=back_keyboard
        )
    
    elif data.startswith("usercard_block_"):
        from services.blacklist import block_user, unblock_user

        if not await db.is_admin(user_id):
            return

        try:
            user_id_to_block = int(data.split("_")[2])
        except (ValueError, IndexError):
            return

        is_blocked, _ = await db.is_blacklisted(user_id_to_block)
        if is_blocked:
            await unblock_user(user_id_to_block)
        else:
            await block_user(
                user_id_to_block,
                "用户信息卡片快捷封禁",
                user_id,
                permanent=True
            )

        await _refresh_usercard_keyboard(query, user_id_to_block)

    elif data.startswith("usercard_exempt_"):
        if not await db.is_admin(user_id):
            return

        try:
            user_id_to_exempt = int(data.split("_")[2])
        except (ValueError, IndexError):
            return

        is_exempted = await db.is_exempted(user_id_to_exempt)
        if is_exempted:
            await db.remove_exemption(user_id_to_exempt)
        else:
            await db.add_exemption(
                user_id_to_exempt,
                is_permanent=True,
                exempted_by=user_id,
                reason="用户信息卡片快捷豁免"
            )

        await _refresh_usercard_keyboard(query, user_id_to_exempt)

    elif data.startswith("unblock:"):
        from services.blacklist import verify_unblock_answer
        try:
            _, challenge_id, index = data.split(':')
            message, success = await verify_unblock_answer(user_id, challenge_id, int(index))
        except ValueError:
            return
        
        await query.edit_message_text(text=message, reply_markup=None)
        
    elif data.startswith("admin_unblock_"):
        from services import blacklist
        
        user_id_to_unblock = int(data.split("_")[2])
        
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
            
        response = await blacklist.unblock_user(user_id_to_unblock)
        await query.answer(response, show_alert=True)

        current_page = 1
        message_text = query.message.text or ""
        reply_markup_str = str(query.message.reply_markup) if query.message.reply_markup else ""
        
        is_panel = "panel_blacklist" in reply_markup_str or "panel_stats_blacklist" in reply_markup_str
        is_stats_page = "黑名单用户列表" in message_text or "stats_list_blacklist" in reply_markup_str
        
        if "第" in message_text and "/" in message_text:
            try:
                match = re.search(r'第\s*(\d+)/', message_text)
                if match:
                    current_page = int(match.group(1))
            except:
                pass
        
        if is_panel:
            message, keyboard = await blacklist.get_blacklist_keyboard(page=current_page)
            if keyboard:
                keyboard_buttons = [list(row) for row in keyboard.inline_keyboard]
                keyboard_buttons.append([InlineKeyboardButton("返回主面板", callback_data="panel_back")])
                keyboard = InlineKeyboardMarkup(keyboard_buttons)
            else:
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("返回主面板", callback_data="panel_back")]])
            await query.edit_message_text(
                text=message,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        elif is_stats_page:
            message, keyboard = await blacklist.get_blacklist_keyboard_detailed(page=current_page)
            if keyboard:
                keyboard_buttons = [list(row) for row in keyboard.inline_keyboard]
                for i, row in enumerate(keyboard_buttons):
                    for j, button in enumerate(row):
                        if button.callback_data == "stats_back_to_menu":
                            keyboard_buttons[i][j] = InlineKeyboardButton("返回主面板", callback_data="panel_back")
                            break
                keyboard = InlineKeyboardMarkup(keyboard_buttons)
            else:
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("返回主面板", callback_data="panel_back")]])
            await query.edit_message_text(
                text=message,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        else:
            message, keyboard = await blacklist.get_blacklist_keyboard(page=current_page)
            if keyboard:
                await query.edit_message_text(
                    text=message,
                    reply_markup=keyboard,
                    parse_mode='Markdown'
                )
            else:
                await query.edit_message_text(text=message)
    
    elif data.startswith("blacklist_page_"):
        from services import blacklist
        
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        try:
            page = int(data.split("_")[2])
        except (ValueError, IndexError):
            await query.answer("无效的页码。", show_alert=True)
            return
        
        message, keyboard = await blacklist.get_blacklist_keyboard(page=page)
        if keyboard:
            await query.edit_message_text(
                text=message,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(text=message)
    
    elif data.startswith("filtered_page_"):
        from .admin_handler import _format_filtered_messages, _get_filtered_messages_keyboard
        
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        try:
            page = int(data.split("_")[2])
        except (ValueError, IndexError):
            await query.answer("无效的页码。", show_alert=True)
            return
        
        MESSAGES_PER_PAGE = 5

        total_count = await db.get_filtered_messages_count()
        
        if total_count == 0:
            await query.edit_message_text("没有找到被过滤的消息。")
            return
        
        total_pages = (total_count + MESSAGES_PER_PAGE - 1) // MESSAGES_PER_PAGE

        if page < 1:
            page = 1
        elif page > total_pages:
            page = total_pages

        offset = (page - 1) * MESSAGES_PER_PAGE

        messages = await db.get_filtered_messages(MESSAGES_PER_PAGE, offset)
        
        if not messages:
            await query.edit_message_text("没有找到被过滤的消息。")
            return

        response = await _format_filtered_messages(messages, page, total_pages)

        keyboard = await _get_filtered_messages_keyboard(page, total_pages)

        if keyboard:
            await query.edit_message_text(response, reply_markup=keyboard)
        else:
            await query.edit_message_text(response)
    
    elif data.startswith("panel_exemptions_page_"):
        from services import blacklist
        
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        try:
            page = int(data.split("_")[3])
        except (ValueError, IndexError):
            await query.answer("无效的页码。", show_alert=True)
            return
        
        message, keyboard = await blacklist.get_exemptions_keyboard(page=page)
        
        if keyboard:
            keyboard_buttons = [list(row) for row in keyboard.inline_keyboard]
            keyboard_buttons.append([InlineKeyboardButton("返回主面板", callback_data="panel_back")])
            keyboard = InlineKeyboardMarkup(keyboard_buttons)
        else:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("返回主面板", callback_data="panel_back")]])
        
        if keyboard:
            await query.edit_message_text(
                text=message,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(text=message)
    
    elif data.startswith("admin_remove_exemption_"):
        from services import blacklist
        
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        try:
            user_id_to_remove = int(data.split("_")[3])
        except (ValueError, IndexError):
            await query.answer("无效的用户ID。", show_alert=True)
            return
        
        await db.remove_exemption(user_id_to_remove)
        await query.answer(f"已移除用户 {user_id_to_remove} 的豁免", show_alert=True)
        
        current_page = 1
        message_text = query.message.text or ""
        if "第" in message_text and "/" in message_text:
            try:
                match = re.search(r'第\s*(\d+)/', message_text)
                if match:
                    current_page = int(match.group(1))
            except:
                pass
        
        message, keyboard = await blacklist.get_exemptions_keyboard(page=current_page)
        
        if keyboard:
            keyboard_buttons = [list(row) for row in keyboard.inline_keyboard]
            keyboard_buttons.append([InlineKeyboardButton("返回主面板", callback_data="panel_back")])
            keyboard = InlineKeyboardMarkup(keyboard_buttons)
        else:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("返回主面板", callback_data="panel_back")]])
        
        if keyboard:
            await query.edit_message_text(
                text=message,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(text=message)
    
    elif data.startswith("stats_list_all_users_page_"):
        from services.blacklist import get_all_users_keyboard
        
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        try:
            page = int(data.split("_")[5])
        except (ValueError, IndexError):
            await query.answer("无效的页码。", show_alert=True)
            return
        
        message, keyboard = await get_all_users_keyboard(page=page)
        if keyboard:
            await query.edit_message_text(
                text=message,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(text=message, parse_mode='Markdown')
    
    elif data.startswith("stats_list_blacklist_page_"):
        from services.blacklist import get_blacklist_keyboard_detailed
        
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        try:
            page = int(data.split("_")[4])
        except (ValueError, IndexError):
            await query.answer("无效的页码。", show_alert=True)
            return
        
        message, keyboard = await get_blacklist_keyboard_detailed(page=page)
        if keyboard:
            await query.edit_message_text(
                text=message,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(text=message, parse_mode='Markdown')
    
    elif data == "stats_back_to_menu":
        from .command_handler import stats
        
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        total_users = await db.get_total_users_count()
        blocked_users = await db.get_blocked_users_count()
        
        stats_message = (
            f"机器人统计数据\n"
            f"---------------------\n"
            f"总用户数: {total_users}\n"
            f"黑名单用户数: {blocked_users}\n\n"
            f"请选择要查看的列表："
        )
        
        keyboard = [
            [InlineKeyboardButton("所有用户列表", callback_data="stats_list_all_users_page_1")],
            [InlineKeyboardButton("黑名单用户列表", callback_data="stats_list_blacklist_page_1")]
        ]
        
        await query.edit_message_text(
            text=stats_message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif data.startswith("autoreply_"):
        if not await db.is_admin(user_id):
            await query.answer("抱歉，您没有权限执行此操作。", show_alert=True)
            return
        
        if data == "autoreply_toggle":
            is_enabled = await db.get_autoreply_enabled()
            await db.set_autoreply_enabled(not is_enabled)
            new_status = "已启用" if not is_enabled else "已禁用"
            await query.answer(f"自动回复已{new_status}", show_alert=True)
            
            is_enabled = await db.get_autoreply_enabled()
            status_text = "已启用" if is_enabled else "已禁用"
            
            message = (
                f"自动回复管理\n\n"
                f"当前状态: {status_text}\n\n"
                f"请选择操作："
            )
            
            keyboard = [
                [
                    InlineKeyboardButton(
                        "关闭自动回复" if is_enabled else "开启自动回复",
                        callback_data="autoreply_toggle"
                    )
                ],
                [InlineKeyboardButton("管理知识库", callback_data="autoreply_kb_list_page_1")],
                [InlineKeyboardButton("添加知识条目", callback_data="autoreply_kb_add")],
            ]
            
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
        
        elif data.startswith("autoreply_kb_list_page_"):
            try:
                page = int(data.split("_")[4])
            except (ValueError, IndexError):
                page = 1
            
            entries = await db.get_all_knowledge_entries()
            if not entries:
                await query.edit_message_text("知识库为空")
                return
            
            MESSAGES_PER_PAGE = 5
            total_pages = (len(entries) + MESSAGES_PER_PAGE - 1) // MESSAGES_PER_PAGE
            if page < 1:
                page = 1
            elif page > total_pages:
                page = total_pages
            
            start_idx = (page - 1) * MESSAGES_PER_PAGE
            end_idx = start_idx + MESSAGES_PER_PAGE
            page_entries = entries[start_idx:end_idx]
            
            message = f"知识库条目 (第 {page}/{total_pages} 页)\n\n"
            keyboard = []
            
            for entry in page_entries:
                title = entry['title'][:30] + "..." if len(entry['title']) > 30 else entry['title']
                keyboard.append([
                    InlineKeyboardButton(
                        f"{title}",
                        callback_data=f"autoreply_kb_view_{entry['id']}"
                    )
                ])
                keyboard.append([
                    InlineKeyboardButton(
                        "编辑",
                        callback_data=f"autoreply_kb_edit_{entry['id']}"
                    ),
                    InlineKeyboardButton(
                        "删除",
                        callback_data=f"autoreply_kb_delete_{entry['id']}"
                    )
                ])
            
            nav_buttons = []
            if page > 1:
                nav_buttons.append(InlineKeyboardButton("上一页", callback_data=f"autoreply_kb_list_page_{page-1}"))
            if page < total_pages:
                nav_buttons.append(InlineKeyboardButton("下一页", callback_data=f"autoreply_kb_list_page_{page+1}"))
            if nav_buttons:
                keyboard.append(nav_buttons)
            
            keyboard.append([InlineKeyboardButton("返回", callback_data="autoreply_back")])
            
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
        
        elif data.startswith("autoreply_kb_view_"):
            try:
                entry_id = int(data.split("_")[3])
            except (ValueError, IndexError):
                await query.answer("无效的条目ID", show_alert=True)
                return
            
            entry = await db.get_knowledge_entry(entry_id)
            if not entry:
                await query.answer("条目不存在", show_alert=True)
                return
            
            message = (
                f"知识条目详情\n\n"
                f"ID: {entry['id']}\n"
                f"标题: {entry['title']}\n"
                f"内容: {entry['content']}\n\n"
                f"创建时间: {entry['created_at']}\n"
                f"更新时间: {entry['updated_at']}"
            )
            
            keyboard = [
                [
                    InlineKeyboardButton("编辑", callback_data=f"autoreply_kb_edit_{entry_id}"),
                    InlineKeyboardButton("删除", callback_data=f"autoreply_kb_delete_{entry_id}")
                ],
                [InlineKeyboardButton("返回列表", callback_data="autoreply_kb_list_page_1")]
            ]
            
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
        
        elif data.startswith("autoreply_kb_edit_"):
            try:
                entry_id = int(data.split("_")[3])
            except (ValueError, IndexError):
                await query.answer("无效的条目ID", show_alert=True)
                return
            
            entry = await db.get_knowledge_entry(entry_id)
            if not entry:
                await query.answer("条目不存在", show_alert=True)
                return
            
            await query.edit_message_text(
                f"编辑知识条目\n\n"
                f"ID: {entry['id']}\n"
                f"标题: {entry['title']}\n"
                f"内容: {entry['content']}\n\n"
                f"请使用以下格式发送编辑命令：\n"
                f"`/autoreply edit {entry_id} <新标题> <新内容>`\n\n"
                f"示例：\n"
                f"`/autoreply edit {entry_id} 新标题 新内容`",
                parse_mode='Markdown'
            )
        
        elif data.startswith("autoreply_kb_delete_"):
            try:
                entry_id = int(data.split("_")[3])
            except (ValueError, IndexError):
                await query.answer("无效的条目ID", show_alert=True)
                return
            
            entry = await db.get_knowledge_entry(entry_id)
            if not entry:
                await query.answer("条目不存在", show_alert=True)
                return
            
            await db.delete_knowledge_entry(entry_id)
            await query.answer(f"已删除: {entry['title']}", show_alert=True)
            
            entries = await db.get_all_knowledge_entries()
            if not entries:
                await query.edit_message_text("知识库为空")
                return
            
            page = 1
            MESSAGES_PER_PAGE = 5
            total_pages = (len(entries) + MESSAGES_PER_PAGE - 1) // MESSAGES_PER_PAGE
            
            start_idx = (page - 1) * MESSAGES_PER_PAGE
            end_idx = start_idx + MESSAGES_PER_PAGE
            page_entries = entries[start_idx:end_idx]
            
            message = f"知识库条目 (第 {page}/{total_pages} 页)\n\n"
            keyboard = []
            
            for entry in page_entries:
                title = entry['title'][:30] + "..." if len(entry['title']) > 30 else entry['title']
                keyboard.append([
                    InlineKeyboardButton(
                        f"{title}",
                        callback_data=f"autoreply_kb_view_{entry['id']}"
                    )
                ])
                keyboard.append([
                    InlineKeyboardButton(
                        "编辑",
                        callback_data=f"autoreply_kb_edit_{entry['id']}"
                    ),
                    InlineKeyboardButton(
                        "删除",
                        callback_data=f"autoreply_kb_delete_{entry['id']}"
                    )
                ])
            
            nav_buttons = []
            if page < total_pages:
                nav_buttons.append(InlineKeyboardButton("下一页", callback_data=f"autoreply_kb_list_page_{page+1}"))
            if nav_buttons:
                keyboard.append(nav_buttons)
            
            keyboard.append([InlineKeyboardButton("返回", callback_data="autoreply_back")])
            
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
        
        elif data == "autoreply_back":
            is_enabled = await db.get_autoreply_enabled()
            status_text = "已启用" if is_enabled else "已禁用"
            
            message = (
                f"自动回复管理\n\n"
                f"当前状态: {status_text}\n\n"
                f"请选择操作："
            )
            
            keyboard = [
                [
                    InlineKeyboardButton(
                        "关闭自动回复" if is_enabled else "开启自动回复",
                        callback_data="autoreply_toggle"
                    )
                ],
                [InlineKeyboardButton("管理知识库", callback_data="autoreply_kb_list_page_1")],
                [InlineKeyboardButton("添加知识条目", callback_data="autoreply_kb_add")],
            ]
            
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
        
        elif data == "autoreply_kb_add":
            await query.edit_message_text(
                "添加知识条目\n\n"
                "请使用以下格式发送新条目：\n"
                "`/autoreply add <标题> <内容>`\n\n"
                "示例：\n"
                "`/autoreply add 常见问题 这是问题的答案`",
                parse_mode='Markdown'
            )
