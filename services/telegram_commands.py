import logging
from typing import Iterable, Sequence

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
)
from telegram.ext import Application

from config import config

logger = logging.getLogger(__name__)

CommandSpec = tuple[str, str]


COMMON_PRIVATE_COMMANDS: tuple[CommandSpec, ...] = (
    ("start", "启动机器人"),
    ("getid", "查看当前 ID"),
)

ADMIN_PRIVATE_COMMANDS: tuple[CommandSpec, ...] = (
    ("help", "查看帮助说明"),
    ("block", "拉黑用户"),
    ("unblock", "解除拉黑"),
    ("panel", "打开管理面板"),
    ("blacklist", "查看黑名单"),
    ("stats", "查看统计信息"),
    ("view_filtered", "查看拦截消息"),
    ("autoreply", "管理自动回复"),
    ("exempt", "管理用户豁免"),
    ("review", "查看待审核消息"),
    ("audit", "查看验证和审查记录"),
)


COMMON_GROUP_COMMANDS: tuple[CommandSpec, ...] = (
    ("getid", "查看当前群组 ID"),
)

ADMIN_GROUP_COMMANDS: tuple[CommandSpec, ...] = (
    ("block", "拉黑用户"),
    ("unblock", "解除拉黑"),
    ("panel", "打开管理面板"),
    ("blacklist", "查看黑名单"),
    ("stats", "查看统计信息"),
    ("view_filtered", "查看拦截消息"),
    ("autoreply", "管理自动回复"),
    ("exempt", "管理用户豁免"),
)


def _to_bot_commands(command_specs: Sequence[CommandSpec]) -> list[BotCommand]:
    return [BotCommand(command, description) for command, description in command_specs]


def _has_admin_features() -> bool:
    return bool(config.FORUM_GROUP_ID and config.ADMIN_IDS)


def _extend_commands(
    commands: list[CommandSpec],
    extra_commands: Iterable[CommandSpec],
) -> list[CommandSpec]:
    commands.extend(extra_commands)
    return commands


def get_private_chat_commands() -> list[BotCommand]:
    commands = list(COMMON_PRIVATE_COMMANDS)
    if _has_admin_features():
        _extend_commands(commands, ADMIN_PRIVATE_COMMANDS)
    return _to_bot_commands(commands)


def get_group_chat_commands() -> list[BotCommand]:
    commands = list(COMMON_GROUP_COMMANDS)
    if _has_admin_features():
        _extend_commands(commands, ADMIN_GROUP_COMMANDS)
    return _to_bot_commands(commands)


async def register_bot_commands(app: Application) -> None:
    private_commands = get_private_chat_commands()
    group_commands = get_group_chat_commands()

    await app.bot.set_my_commands(
        private_commands,
        scope=BotCommandScopeAllPrivateChats(),
    )
    await app.bot.set_my_commands(
        group_commands,
        scope=BotCommandScopeAllGroupChats(),
    )

    logger.info(
        "已同步 Telegram 命令菜单: 私聊 %s 条, 群聊 %s 条",
        len(private_commands),
        len(group_commands),
    )
