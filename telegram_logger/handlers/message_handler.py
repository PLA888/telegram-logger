import logging
import re
import pickle
from datetime import datetime
from typing import Union, Optional
from telethon import events
from telethon.tl.types import Message
from telegram_logger.handlers.base_handler import BaseHandler
from telegram_logger.utils.media import save_media_as_file
import os
from dotenv import load_dotenv

load_dotenv()
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "-1002268819123"))
IGNORED_IDS = {int(x.strip()) for x in os.getenv("IGNORED_IDS", "-10000").split(",")}

logger = logging.getLogger(__name__)


class NewMessageHandler(BaseHandler):
    def __init__(self, client, db, log_chat_id, ignored_ids, persist_times):
        super().__init__(client, db, log_chat_id, ignored_ids)
        self.persist_times = persist_times

    async def handle_new_message(
        self, event: events.NewMessage.Event # 明确处理 NewMessage 事件
    ) -> Optional[Message]:
        """处理新接收到的消息"""
        # 添加类型检查以确保事件类型正确（虽然注册时已指定）
        if not isinstance(event, events.NewMessage.Event):
            logger.warning(f"NewMessageHandler received non-NewMessage event: {type(event)}")
            # 根据 Telethon 的期望，可能返回 None 或不返回
            return None
        chat_id = event.chat_id
        from_id = self._get_sender_id(event.message)
        msg_id = event.message.id

        if await self._should_ignore_message(event, chat_id, from_id):
            return None

        message = await self._create_message_object(event)
        await self.db.save_message(message)
        return message

    async def _should_ignore_message(self, event, chat_id, from_id) -> bool:
        """判断是否应该忽略消息"""
        if await self._is_special_link_message(event, chat_id, from_id):
            return True
        return from_id in IGNORED_IDS or chat_id in IGNORED_IDS

    async def _is_special_link_message(self, event, chat_id, from_id) -> bool:
        """处理特殊消息链接"""
        if (
            chat_id == LOG_CHAT_ID
            and from_id == self.my_id
            and event.message.text
            and (
                re.match(
                    r"^(https:\/\/)?t\.me\/(?:c\/)?[\d\w]+\/[\d]+", event.message.text
                )
                or re.match(
                    r"^tg:\/\/openmessage\?user_id=\d+&message_id=\d+",
                    event.message.text,
                )
            )
        ):
            await self._save_restricted_messages(event.message.text)
            return True
        return False

    async def _create_message_object(self, event: events.NewMessage.Event) -> Message: # 类型提示更精确
        """创建消息对象"""
        noforwards = getattr(event.chat, "noforwards", False) or getattr(
            event.message, "noforwards", False
        )
        self_destructing = bool(
            getattr(getattr(event.message, "media", None), "ttl_seconds", False)
        )

        media = None
        if event.message.media or (noforwards or self_destructing):
            try:
                media_path = await save_media_as_file(self.client, event.message)
                media = pickle.dumps(event.message.media)
            except Exception as e:
                logger.error(f"保存媒体失败: {str(e)}")

        return Message(
            id=event.message.id,
            from_id=self._get_sender_id(event.message),
            chat_id=event.chat_id,
            msg_type=await self._get_chat_type(event),
            msg_text=event.message.message,
            media=media,
            noforwards=noforwards,
            self_destructing=self_destructing,
            created_time=datetime.now(), # 新消息的创建时间
            edited_time=None, # 新消息没有编辑时间
        )

    async def _save_restricted_messages(self, link: str):
        """保存受限消息"""
        # 实现类似原save_restricted_msg的功能
        pass
