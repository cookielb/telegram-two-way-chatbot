"""Report degraded AI filtering without blocking message delivery."""
import asyncio
import logging
import time

from config import config

logger = logging.getLogger(__name__)


def classify_error(error):
    """Inspect structured SDK codes and wrapped causes; never expose raw errors or keys."""
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, 'status_code', None) or getattr(current, 'code', None)
        name = type(current).__name__
        if code in (401, 403, '401', '403') or name in {'AuthenticationError', 'PermissionDeniedError'}:
            return 'credentials', 'AI 密钥无效、已过期或没有所需权限，请检查密钥和模型权限。'
        if code in (429, '429') or name == 'RateLimitError':
            return 'quota', 'AI 额度不足或请求受限，请检查余额、配额和调用频率。'
        if isinstance(current, (TimeoutError, asyncio.TimeoutError)) or name == 'APITimeoutError':
            return 'timeout', 'AI 请求超时，请检查网络或服务状态。'
        if code in (404, '404'):
            return 'model', 'HTTP 404：模型或接口路径不存在，请检查所选模型和 BASE_URL。'
        if code in (400, '400'):
            return 'request', 'HTTP 400：请求参数、密钥或模型能力不匹配，请检查接口配置。'
        if isinstance(current, ValueError):
            return 'format', '模型返回空内容、无效 JSON 或不符合审查字段要求；本次未完成有效审查。'
        if name in {'ConnectError', 'NetworkError', 'APIConnectionError'}:
            return 'network', 'AI 接口网络连接失败，请检查 VPS 到接口的网络连接。'
        current = current.__cause__ or current.__context__
    return 'unavailable', 'AI 审查接口不可用或返回格式错误，请检查服务和模型配置。'


async def notify_ai_failure(context, verdict):
    state = context.bot_data.setdefault('ai_failure_alerts', {})
    lock = state.setdefault('lock', asyncio.Lock())
    async with lock:
        now = time.monotonic()
        sent = state.setdefault('sent', {})
        text = ('⚠️ AI 内容审查异常\n\n' + verdict.get('error_detail', 'AI 审查暂时不可用。') +
                '\n\n当前采用异常直接放行：消息继续转发，未完成 AI 审查。'
                '\n本地广告规则、黑名单、人机验证和媒体待审核规则仍有效。'
                '\n可用 /audit 用户ID 查看异常放行记录。')
        delivered = False
        for admin_id in config.ADMIN_IDS:
            if now - sent.get(admin_id, float('-inf')) < config.AI_ALERT_INTERVAL_SECONDS:
                delivered = True
                continue
            try:
                await context.bot.send_message(chat_id=admin_id, text=text,
                                               connect_timeout=5, read_timeout=5,
                                               write_timeout=5, pool_timeout=5)
                sent[admin_id] = now
                delivered = True
            except Exception:
                logger.warning('无法私聊通知管理员 %s，请管理员先发送 /start', admin_id)
        # If private notices cannot be delivered, use the configured management group.
        if not delivered and config.FORUM_GROUP_ID:
            fallback_key = 'forum'
            if now - sent.get(fallback_key, float('-inf')) >= config.AI_ALERT_INTERVAL_SECONDS:
                try:
                    await context.bot.send_message(chat_id=config.FORUM_GROUP_ID, text=text,
                                                   connect_timeout=5, read_timeout=5,
                                                   write_timeout=5, pool_timeout=5)
                    sent[fallback_key] = now
                except Exception:
                    logger.error('AI 异常通知发送失败；原因类型：%s', verdict.get('error', 'unknown'))
