import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    BOT_ID = None
    BOT_USERNAME = None
    FORUM_GROUP_ID = int(os.getenv('FORUM_GROUP_ID') or 0)
    ADMIN_IDS = [int(id) for id in os.getenv('ADMIN_IDS', '').split(',') if id]
    
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
    GEMINI_BASE_URL = (os.getenv('GEMINI_BASE_URL') or '').strip() or None
    
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
    OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1')
    
    ENABLE_AI_FILTER = os.getenv('ENABLE_AI_FILTER', 'true').lower() == 'true'
    AI_CONFIDENCE_THRESHOLD = int(os.getenv('AI_CONFIDENCE_THRESHOLD', '70'))
    
    AI_REVIEW_THRESHOLD = int(os.getenv('AI_REVIEW_THRESHOLD', '40'))
    AI_NEW_USER_REVIEW_THRESHOLD = int(os.getenv('AI_NEW_USER_REVIEW_THRESHOLD', '25'))
    AI_TIMEOUT_SECONDS = int(os.getenv('AI_TIMEOUT_SECONDS', '20'))
    AI_RETRY_ATTEMPTS = int(os.getenv('AI_RETRY_ATTEMPTS', '2'))
    AI_ALERT_INTERVAL_SECONDS = int(os.getenv('AI_ALERT_INTERVAL_SECONDS', '3600'))
    NEW_USER_STRICT_MESSAGES = int(os.getenv('NEW_USER_STRICT_MESSAGES', '3'))

    VERIFICATION_ENABLED = os.getenv('VERIFICATION_ENABLED', 'true').lower() == 'true'
    AUTO_UNBLOCK_ENABLED = os.getenv('AUTO_UNBLOCK_ENABLED', 'true').lower() == 'true'
    
    DATABASE_PATH = os.getenv('DATABASE_PATH', './data/bot.db')
    
    MAX_WORKERS = int(os.getenv('MAX_WORKERS', '5'))
    QUEUE_TIMEOUT = int(os.getenv('QUEUE_TIMEOUT', '30'))
    
    VERIFICATION_TIMEOUT = int(os.getenv('VERIFICATION_TIMEOUT', '300'))
    MAX_VERIFICATION_ATTEMPTS = int(os.getenv('MAX_VERIFICATION_ATTEMPTS', '3'))
    
    MAX_MESSAGES_PER_MINUTE = int(os.getenv('MAX_MESSAGES_PER_MINUTE', '30'))

    @classmethod
    def validate(cls):
        if not 0 <= cls.AI_NEW_USER_REVIEW_THRESHOLD <= cls.AI_REVIEW_THRESHOLD < cls.AI_CONFIDENCE_THRESHOLD <= 100:
            raise ValueError("审查阈值需满足 0 <= 新用户待审核阈值 <= 待审核阈值 < 拦截阈值 <= 100")
        if not 1 <= cls.AI_RETRY_ATTEMPTS <= 3 or cls.AI_TIMEOUT_SECONDS <= 0:
            raise ValueError("AI 重试次数应为 1～3，超时必须大于 0")
        if cls.AI_ALERT_INTERVAL_SECONDS <= 0:
            raise ValueError('AI 异常通知间隔必须大于 0')
        if cls.NEW_USER_STRICT_MESSAGES < 0 or cls.MAX_MESSAGES_PER_MINUTE <= 0:
            raise ValueError("新用户严格审查条数不得为负，速率限制必须大于 0")
        if not cls.BOT_TOKEN:
            raise ValueError("BOT_TOKEN未设置")
        if not cls.FORUM_GROUP_ID or not cls.ADMIN_IDS:
            print("警告: FORUM_GROUP_ID 或 ADMIN_IDS 未设置。只有 /getid 功能可用。")

config = Config()
