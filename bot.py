import logging
import asyncio
from telegram import Update
from telegram.ext import Application
from config import config
from handlers import register_handlers
from database.db_manager import DatabaseManager
from services.telegram_commands import register_bot_commands

async def post_init(app: Application):
    config.BOT_ID = app.bot.id
    config.BOT_USERNAME = app.bot.username
    print(f"Bot ID: {config.BOT_ID} 已设置")
    print(f"Bot Username: {config.BOT_USERNAME} 已设置")
    await register_bot_commands(app)
    if config.ENABLE_AI_FILTER:
        from services.ai_service import ai_service
        if await ai_service.get_provider() is None:
            logging.error('AI 审查已启用但当前服务没有密钥；用户消息将直接放行，并在收到消息时通知管理员。请在 /panel 配置 AI 服务。')

def main():
    config.validate()
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )

    # Python 3.14 不再为主线程隐式创建事件循环，显式设置可兼容 PTB 的 run_polling。
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    db_manager = DatabaseManager(config.DATABASE_PATH)
    loop.run_until_complete(db_manager.initialize())

    app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).build()

    register_handlers(app)

    config.validate()

    logging.info("Bot启动中...")
    app.run_polling(allowed_updates=[
        Update.MESSAGE,
        Update.EDITED_MESSAGE,
        Update.CALLBACK_QUERY,
    ])

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Bot已停止")
    except Exception as e:
        logging.error(f"启动失败: {e}")
        import traceback
        traceback.print_exc()
