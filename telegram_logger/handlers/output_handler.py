import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Set, Union

from telethon import events
from telethon.errors import (ChannelPrivateError, ChatAdminRequiredError,
                             MessageIdInvalidError, UserIsBlockedError)
from telethon.tl.types import (DocumentAttributeFilename,
                               DocumentAttributeSticker, Message as TelethonMessage,
                               PeerChannel, PeerChat, PeerUser)

from ..data.database import DatabaseManager
from ..data.models import Message
from ..utils.media import retrieve_media_as_file
from ..utils.mentions import create_mention
from .base_handler import BaseHandler
from .log_sender import LogSender
from .media_handler import RestrictedMediaHandler
from .message_formatter import MessageFormatter

logger = logging.getLogger(__name__)

class OutputHandler(BaseHandler):
    """
    负责根据配置过滤事件、格式化消息、处理媒体并将其发送到日志频道的处理器。
    合并了原 EditDeleteHandler 和 ForwardHandler 的输出相关功能。
    监听 NewMessage, MessageEdited, MessageDeleted 事件。
    """

    def __init__(
        self,
        db: DatabaseManager,
        log_chat_id: int,
        ignored_ids: Set[int],
        forward_user_ids: Optional[List[int]] = None,
        forward_group_ids: Optional[List[int]] = None,
        deletion_rate_limit_threshold: int = 5,
        deletion_rate_limit_window: int = 10,   # 单位：秒
        deletion_pause_duration: int = 5,       # 单位：秒
        **kwargs: Dict[str, Any]
    ):
        """初始化 OutputHandler。"""
        super().__init__(None, db, log_chat_id, ignored_ids, **kwargs)
        self.forward_user_ids = set(forward_user_ids) if forward_user_ids else set()
        self.forward_group_ids = set(forward_group_ids) if forward_group_ids else set()

        # 删除事件的速率限制配置
        self.deletion_rate_limit_threshold = deletion_rate_limit_threshold
        self.deletion_rate_limit_window = timedelta(seconds=deletion_rate_limit_window)
        self.deletion_pause_duration = timedelta(seconds=deletion_pause_duration)
        self._deletion_timestamps: Deque[datetime] = deque()
        self._rate_limit_paused_until: Optional[datetime] = None

        # 辅助类的占位符，将在 set_client 中初始化
        self.log_sender: Optional[LogSender] = None
        self.formatter: Optional[MessageFormatter] = None
        self.restricted_media_handler: Optional[RestrictedMediaHandler] = None

        logger.info(
            f"OutputHandler 初始化完毕。转发用户: {self.forward_user_ids}, "
            f"群组: {self.forward_group_ids}, 忽略 ID: {self.ignored_ids}, "
            f"删除速率限制: {self.deletion_rate_limit_threshold} 事件 / "
            f"{self.deletion_rate_limit_window.total_seconds()} 秒, 暂停: "
            f"{self.deletion_pause_duration.total_seconds()} 秒"
        )

    def set_client(self, client):
        """设置客户端并初始化依赖客户端的辅助类。"""
        super().set_client(client)
        if self.client:
            if not self.log_chat_id:
                 logger.error("OutputHandler 无法初始化辅助类：log_chat_id 未设置。")
                 return

            self.log_sender = LogSender(self.client, self.log_chat_id)
            self.formatter = MessageFormatter(self.client)
            self.restricted_media_handler = RestrictedMediaHandler(self.client)
            logger.info("OutputHandler 的辅助类 (LogSender, MessageFormatter, RestrictedMediaHandler) 已初始化。")
        else:
            logger.warning("无法初始化 OutputHandler 辅助类：客户端为 None。")

    async def process(self, event: events.common.EventCommon) -> Optional[Message]:
        """
        处理传入的 Telegram 事件。
        根据事件类型调用相应的内部处理方法。
        此处理器不返回 Message 对象，而是执行发送操作。
        """
        if not self.client or not self.log_sender or not self.formatter or not self.restricted_media_handler:
            logger.error("OutputHandler 无法处理事件：客户端或辅助类尚未初始化。")
            return None

        try:
            if isinstance(event, events.NewMessage.Event):
                await self._process_new_message(event)
            elif isinstance(event, events.MessageEdited.Event):
                await self._process_edited_message(event)
            elif isinstance(event, events.MessageDeleted.Event):
                await self._process_deleted_message(event)
            else:
                logger.debug(f"OutputHandler 忽略事件类型: {type(event).__name__}")
            return None
        except Exception as e:
            event_type = type(event).__name__
            msg_id = getattr(event, 'message_id', None)
            if msg_id is None and hasattr(event, 'deleted_ids'):
                msg_id = event.deleted_ids
            if msg_id is None and hasattr(event, 'original_update'):
                 msg_id = getattr(getattr(event.original_update, 'message', None), 'id', '未知')

            logger.exception(f"OutputHandler 处理 {event_type} (相关消息ID: {msg_id}) 时发生严重错误: {e}")
            return None

    # --- 内部处理方法 ---

    async def _process_new_message(self, event: events.NewMessage.Event):
        """处理新消息事件。"""
        if not self._should_forward(event):
            logger.debug(f"新消息 {event.message.id} 不满足转发条件，已忽略。")
            return # 不符合转发规则

        logger.info(f"处理新消息: ChatID={event.chat_id}, MsgID={event.message.id}")
        # 使用 OutputHandler 内部的格式化方法
        formatted_text = await self._format_output_message("新消息", event.message)

        await self._send_message_with_media(formatted_text, event.message)

    async def _process_edited_message(self, event: events.MessageEdited.Event):
        """处理消息编辑事件。"""
        # 编辑事件也应用相同的转发规则
        if not self._should_forward(event):
            logger.debug(f"编辑消息 {event.message.id} 不满足转发条件，已忽略。")
            return

        logger.info(f"处理编辑消息: ChatID={event.chat_id}, MsgID={event.message.id}")
        formatted_text = await self._format_output_message("编辑消息", event.message)

        # 决定编辑事件是否需要重新发送媒体。
        # 通常，编辑只更新文本，为避免刷屏，仅发送更新后的文本日志。
        # 如果需要包含媒体，取消下面一行的注释，并确保 _send_message_with_media 能处理
        # await self._send_message_with_media(formatted_text, event.message)
        if self.log_sender:
            await self.log_sender.send_message(formatted_text, parse_mode="markdown")
        else:
            logger.error("LogSender 未初始化，无法发送编辑消息日志。")


    async def _process_deleted_message(self, event: events.MessageDeleted.Event):
        """处理消息删除事件。"""
        if not self._should_log_deletion(event):
            logger.debug(f"删除事件 (IDs: {event.deleted_ids}, Chat: {event.chat_id}) 不满足记录条件，已忽略。")
            return # 不符合记录删除的规则

        # 应用速率限制
        if not await self._apply_deletion_rate_limit():
            logger.warning(f"删除事件被速率限制: IDs={event.deleted_ids}, Chat: {event.chat_id}")
            return # 被速率限制

        deleted_ids = event.deleted_ids
        chat_id = event.chat_id # 可能为 None

        logger.info(f"处理删除消息: ChatID={chat_id}, MsgIDs={deleted_ids}")

        for msg_id in deleted_ids:
            # 从数据库检索原始消息，带重试逻辑
            original_message = await self._get_message_from_db_with_retry(msg_id, chat_id)

            if original_message:
                # 如果找到原始消息，格式化并发送日志
                formatted_text = await self._format_output_message("删除消息", original_message, is_deleted=True)

                # 决定是否在删除日志中包含原始媒体。
                # 为简化起见，默认只发送文本通知。
                # 如果需要发送媒体，需要修改这里的逻辑，并考虑媒体是否还可访问。
                # media_path = original_message.media_path if not original_message.is_restricted else None # 示例
                # await self.log_sender.send_message(formatted_text, file=media_path, parse_mode="markdown")
                if self.log_sender:
                    await self.log_sender.send_message(formatted_text, parse_mode="markdown")
                else:
                    logger.error("LogSender 未初始化，无法发送删除消息日志。")
            else:
                # 如果数据库中找不到原始消息，发送一条简化的删除日志
                logger.warning(f"无法从数据库检索到已删除消息 {msg_id} 的内容 (ChatID: {chat_id})。")
                mention = f"消息 ID `{msg_id}`"
                if chat_id and self.client:
                    try:
                        # 尝试创建聊天提及以提供上下文
                        chat_mention = await create_mention(self.client, chat_id, msg_id) # 使用 msg_id 尝试生成链接
                        mention = f"{chat_mention} 中的消息 ID `{msg_id}`"
                    except Exception as e:
                        logger.warning(f"为删除日志创建聊天 {chat_id} 提及失败: {e}")
                        mention = f"聊天 `{chat_id}` 中的消息 ID `{msg_id}`"

                formatted_text = f"🗑️ **删除消息 (内容未知)**\n\n{mention} 已被删除，但无法从数据库中检索到原始内容。"
                if self.log_sender:
                    await self.log_sender.send_message(formatted_text, parse_mode="markdown")
                else:
                    logger.error("LogSender 未初始化，无法发送内容未知的删除消息日志。")

    # --- 过滤与规则 ---

    def _should_forward(self, event: Union[events.NewMessage.Event, events.MessageEdited.Event]) -> bool:
        """检查新消息或编辑消息是否应根据规则转发到日志频道。"""
        message = event.message
        if not message:
            logger.warning(f"事件 {type(event).__name__} 没有有效的 message 对象，无法应用转发规则。")
            return False

        sender_id = self._get_sender_id(message) # 使用基类方法获取发送者 ID
        chat_id = message.chat_id

        # 规则 1: 检查是否在忽略列表中
        if sender_id in self.ignored_ids:
            logger.debug(f"忽略消息 {message.id}：发送者 {sender_id} 在忽略列表中。")
            return False
        # 对于群组/频道消息，也检查聊天 ID 是否在忽略列表
        if chat_id and chat_id in self.ignored_ids:
            logger.debug(f"忽略消息 {message.id}：聊天 {chat_id} 在忽略列表中。")
            return False

        # 规则 2: 检查是否满足转发条件
        # 注意：message.out 用于判断是否是自己发送的消息
        is_incoming_private = message.is_private and not message.out
        is_group = message.is_group # 包括普通群组和超级群组

        # 条件 A: 来自指定用户的私聊消息 (非自己发送的)
        if is_incoming_private and sender_id in self.forward_user_ids:
            logger.debug(f"转发私聊消息 {message.id}，来自用户 {sender_id}。")
            return True

        # 条件 B: 来自指定群组的消息
        if is_group and chat_id in self.forward_group_ids:
            logger.debug(f"转发群组消息 {message.id}，来自群组 {chat_id}。")
            return True

        # 如果以上条件都不满足
        logger.debug(f"不转发消息 {message.id}：不满足转发规则 (Sender: {sender_id}, Chat: {chat_id}, PrivateIn: {is_incoming_private}, Group: {is_group})。")
        return False

    def _should_log_deletion(self, event: events.MessageDeleted.Event) -> bool:
        """检查删除事件是否应记录日志。"""
        chat_id = event.chat_id # 删除事件可能没有 chat_id
        # 尝试从 peer 获取 chat_id (如果 event.chat_id 为 None)
        if chat_id is None and event.peer:
            if isinstance(event.peer, PeerChannel):
                chat_id = event.peer.channel_id
                # Telethon 通常返回正数 ID，但内部可能需要负数表示频道/群组
                if chat_id > 0: chat_id = int(f"-100{chat_id}")
            elif isinstance(event.peer, PeerChat):
                chat_id = -event.peer.chat_id # 普通群组 ID 为负数

        # 规则 1: 如果知道 chat_id 且在忽略列表，则忽略
        if chat_id and chat_id in self.ignored_ids:
             logger.debug(f"忽略删除事件 (IDs: {event.deleted_ids})：聊天 {chat_id} 在忽略列表中。")
             return False

        # 规则 2: 只记录发生在被转发群组中的删除事件
        if chat_id and chat_id in self.forward_group_ids:
            logger.debug(f"记录删除事件 (IDs: {event.deleted_ids})：发生在转发群组 {chat_id} 中。")
            return True

        # 规则 3: 如果 chat_id 未知（可能发生在私聊或旧事件），保守起见，默认记录
        # 速率限制将防止未知来源的删除事件刷屏
        if chat_id is None:
             logger.debug(f"记录删除事件 (IDs: {event.deleted_ids})：chat_id 未知，默认记录。")
             return True

        # 如果 chat_id 已知但不在转发群组列表中
        logger.debug(f"不记录删除事件 (IDs: {event.deleted_ids})：聊天 {chat_id} 不在转发群组列表中。")
        return False

    # --- 数据库交互 ---

    async def _get_message_from_db_with_retry(self, message_id: int, chat_id: Optional[int] = None) -> Optional[Message]:
        """
        从数据库检索消息，包含短暂重试以处理潜在的持久化延迟。
        如果提供了 chat_id，会进行验证。
        """
        retry_delay = 0.5 # 重试前的等待时间（秒）
        message = None
        try:
            # 第一次尝试
            message = self.db.get_message_by_id(message_id)
            if message and (chat_id is None or message.chat_id == chat_id):
                return message
            elif message: # 找到了但 chat_id 不匹配
                 logger.warning(f"数据库中找到消息 {message_id}，但其 chat_id ({message.chat_id}) 与事件 ({chat_id}) 不匹配。")
                 return None # 视为未找到

            # 如果第一次未找到，等待后重试
            logger.debug(f"消息 {message_id} 在数据库中首次未找到，将在 {retry_delay} 秒后重试。")
            await asyncio.sleep(retry_delay)
            message = self.db.get_message_by_id(message_id)

            if message and (chat_id is None or message.chat_id == chat_id):
                logger.info(f"消息 {message_id} 在重试后于数据库中找到。")
                return message
            elif message: # 重试后找到但 chat_id 不匹配
                 logger.warning(f"数据库中重试找到消息 {message_id}，但其 chat_id ({message.chat_id}) 与事件 ({chat_id}) 不匹配。")
                 return None # 视为未找到
            else:
                # 注意：这里改为 warning，因为消息可能确实不存在或已被清理
                logger.warning(f"消息 {message_id} 在重试后仍未在数据库中找到或 chat_id 不匹配。")
                return None

        except Exception as e:
            logger.error(f"从数据库检索消息 {message_id} 时发生错误: {e}", exc_info=True)
            return None

    # --- 速率限制 ---

    async def _apply_deletion_rate_limit(self) -> bool:
        """
        检查并应用删除事件的速率限制。
        返回 True 表示事件应继续处理，False 表示被限制。
        """
        now = datetime.now(timezone.utc)

        # 检查是否处于暂停状态
        if self._rate_limit_paused_until and now < self._rate_limit_paused_until:
            # 仍在暂停期内，限制事件
            logger.warning(f"删除日志记录因速率限制而暂停中，直到 {self._rate_limit_paused_until}")
            return False

        # 如果暂停时间已过，重置暂停状态
        if self._rate_limit_paused_until and now >= self._rate_limit_paused_until:
            logger.info("删除日志记录的速率限制暂停已结束。")
            self._rate_limit_paused_until = None

        # 清理时间窗口之外的旧时间戳
        cutoff = now - self.deletion_rate_limit_window
        while self._deletion_timestamps and self._deletion_timestamps[0] <= cutoff:
            self._deletion_timestamps.popleft()

        # 记录当前事件的时间戳 (将整个 MessageDeletedEvent 视为一次事件)
        self._deletion_timestamps.append(now)

        # 检查是否超过阈值
        if len(self._deletion_timestamps) > self.deletion_rate_limit_threshold:
            # 超过阈值，设置暂停时间
            self._rate_limit_paused_until = now + self.deletion_pause_duration
            logger.warning(
                f"删除事件速率限制触发！在过去 {self.deletion_rate_limit_window.total_seconds()} 秒内发生 "
                f"{len(self._deletion_timestamps)} 次删除事件 (阈值: {self.deletion_rate_limit_threshold})。"
                f"将暂停记录删除事件直到 {self._rate_limit_paused_until}。"
            )
            # 发送一次性的暂停通知到日志频道
            if self.log_sender:
                try:
                    await self.log_sender.send_message(
                        f"⚠️ **删除消息速率过快**\n"
                        f"检测到大量删除事件 (超过 {self.deletion_rate_limit_threshold} 条 / {self.deletion_rate_limit_window.total_seconds()} 秒)。\n"
                        f"将暂停记录删除事件 {self.deletion_pause_duration.total_seconds()} 秒以避免刷屏。",
                        parse_mode="markdown"
                    )
                except Exception as send_error:
                     logger.error(f"发送速率限制暂停通知失败: {send_error}")
            else:
                logger.error("LogSender 未初始化，无法发送速率限制暂停通知。")

            return False # 事件被限制

        # 未达到阈值，允许事件
        return True

    # --- 格式化与发送 ---

    async def _format_output_message(
        self,
        event_type: str, # "新消息", "编辑消息", "删除消息"
        message_data: Union[TelethonMessage, Message],
        is_deleted: bool = False
    ) -> str:
        """为发送到日志频道的消息格式化文本内容。"""
        # 断言确保 client 已设置
        if not self.client:
             logger.error("Client 未设置，无法格式化消息。")
             return "❌ 格式化错误：客户端未设置。"

        sender_mention = "未知用户"
        chat_mention = ""
        text_content = ""
        msg_id = 0
        date_str = "未知时间"
        edit_date_str = ""
        reply_to_str = ""
        chat_id_for_link = None # 用于构造回复链接

        try:
            if isinstance(message_data, TelethonMessage):
                # 处理来自事件的实时 Telethon Message 对象
                msg_id = message_data.id
                chat_id = message_data.chat_id
                chat_id_for_link = chat_id # 保存 chat_id 用于链接
                sender_id = self._get_sender_id(message_data)
                text_content = message_data.text or ""
                date = message_data.date
                edit_date = getattr(message_data, 'edit_date', None)
                reply_to_msg_id = message_data.reply_to_msg_id

                # 异步获取提及信息
                sender_mention = await create_mention(self.client, sender_id, msg_id)
                if chat_id and not message_data.is_private:
                    chat_mention = await create_mention(self.client, chat_id, msg_id) # 使用 msg_id 尝试生成链接

            elif isinstance(message_data, Message):
                # 处理从数据库检索的 Message 数据对象
                msg_id = message_data.id
                chat_id = message_data.chat_id
                chat_id_for_link = chat_id # 保存 chat_id 用于链接
                sender_id = message_data.from_id
                text_content = message_data.text
                date = message_data.date
                edit_date = message_data.edit_date
                reply_to_msg_id = message_data.reply_to_msg_id

                # 异步获取提及信息
                sender_mention = await create_mention(self.client, sender_id, msg_id)
                if chat_id and not message_data.is_private:
                    chat_mention = await create_mention(self.client, chat_id, msg_id)

            else:
                logger.error(f"无法格式化消息：无效的数据类型 {type(message_data)}")
                return f"❌ 格式化错误：无效的消息数据类型 {type(message_data)}"

            # 格式化日期和回复信息
            date_str = date.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') if date else "未知时间"
            if edit_date and not is_deleted:
                edit_date_str = f"\n**编辑于:** {edit_date.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
            if reply_to_msg_id:
                # 尝试为回复的消息创建链接 (如果 chat_id 已知)
                reply_link = ""
                if chat_id_for_link:
                    try:
                        # 简单的链接构造，适用于超级群组/频道
                        link_chat_id_str = str(abs(chat_id_for_link))
                        if link_chat_id_str.startswith('100'):
                            link_chat_id_str = link_chat_id_str[3:] # 移除 -100 前缀
                        # 对于普通群组，链接格式不同，这里简化处理，可能不总正确
                        if not str(chat_id_for_link).startswith('-100'):
                             # 普通群组链接通常不直接可用，这里仅显示 ID
                             reply_link = f" (普通群组)"
                        else:
                             reply_link = f" [原始消息](https://t.me/c/{link_chat_id_str}/{reply_to_msg_id})"
                    except Exception as link_err:
                        logger.warning(f"为回复消息 {reply_to_msg_id} (Chat: {chat_id_for_link}) 创建链接失败: {link_err}")
                        pass # 链接构造失败就算了
                reply_to_str = f"\n**回复:** `{reply_to_msg_id}`{reply_link}"

            # 截断过长的消息文本
            if len(text_content) > 3500: # Telegram 消息长度限制约为 4096，留些余地
                text_content = text_content[:3500] + "... (消息过长截断)"

            # 构建最终的格式化字符串
            header = ""
            if event_type == "新消息":
                header = f"✉️ **新消息** {chat_mention}\n**来自:** {sender_mention}"
            elif event_type == "编辑消息":
                header = f"✏️ **编辑消息** {chat_mention}\n**来自:** {sender_mention}"
            elif event_type == "删除消息":
                header = f"🗑️ **删除消息** {chat_mention}\n**来自:** {sender_mention}"

            # 添加媒体指示器（如果适用）
            media_indicator = ""
            if isinstance(message_data, TelethonMessage) and message_data.media:
                 media_type = type(message_data.media).__name__
                 # 尝试获取文件名
                 filename = ""
                 if hasattr(message_data.media, 'attributes'):
                     for attr in message_data.media.attributes:
                         if isinstance(attr, DocumentAttributeFilename):
                             filename = f" ({attr.file_name})"
                             break
                 media_indicator = f"\n**媒体:** {media_type}{filename}"
            elif isinstance(message_data, Message) and message_data.media_type:
                 # 数据库中只存了类型名，没有文件名
                 media_indicator = f"\n**媒体:** {message_data.media_type}"


            footer = f"\n**消息 ID:** `{msg_id}`{reply_to_str}\n**时间:** {date_str}{edit_date_str}{media_indicator}"

            # 移除可能存在的 Markdown 格式冲突字符，例如在 text_content 中
            # 简单的清理，可能需要更复杂的处理
            text_content = text_content.replace('*', '\\*').replace('_', '\\_').replace('`', '\\`')

            return f"{header}\n\n{text_content}\n{footer}"

        except Exception as e:
            # 尝试获取 msg_id 用于日志
            error_msg_id = getattr(message_data, 'id', '未知')
            logger.error(f"格式化消息 (ID: {error_msg_id}) 时发生错误: {e}", exc_info=True)
            return f"❌ 格式化消息时出错 (ID: {error_msg_id})。"


    async def _send_message_with_media(self, text: str, message: TelethonMessage):
        """处理带媒体的消息发送，包括普通、受限和贴纸。"""
        # 断言确保辅助类已设置
        if not self.log_sender or not self.restricted_media_handler or not self.client:
             logger.error("OutputHandler 辅助类未完全初始化，无法发送带媒体的消息。")
             # 尝试发送纯文本作为回退
             if self.log_sender:
                 await self.log_sender.send_message(
                     f"⚠️ **媒体发送失败 (初始化错误)** ⚠️\n\n{text}",
                     parse_mode="markdown"
                 )
             return

        media_file_path: Optional[str] = None
        media_context = None # 用于管理 retrieve_media_as_file 的上下文

        try:
            if not message.media:
                # 没有媒体，直接发送文本
                logger.debug(f"消息 {message.id} 无媒体，仅发送文本。")
                await self.log_sender.send_message(text, parse_mode="markdown")
                return

            # --- 处理有媒体的情况 ---
            # 检查是否是贴纸 (使用 Telethon 的属性)
            is_sticker = any(isinstance(attr, DocumentAttributeSticker) for attr in getattr(message.media, 'attributes', []))
            is_restricted = getattr(message, 'noforwards', False)

            # 1. 处理贴纸
            if is_sticker:
                logger.debug(f"消息 {message.id} 是贴纸。")
                # 尝试从数据库获取已保存的贴纸文件路径
                db_message = self.db.get_message_by_id(message.id)
                if db_message and db_message.media_path:
                    try:
                        # 使用 retrieve_media_as_file 获取文件路径
                        media_context = retrieve_media_as_file(db_message.media_path, db_message.is_restricted)
                        media_file_path = media_context.__enter__() # 手动进入上下文
                        # 直接使用 client 发送贴纸文件，保留 caption 和 reply_to
                        await self.client.send_file(
                            self.log_chat_id,
                            media_file_path,
                            caption=text, # 将格式化文本作为 caption
                            parse_mode="markdown",
                            reply_to=message.reply_to_msg_id # 保留回复上下文
                        )
                        logger.info(f"贴纸消息 {message.id} 已使用数据库文件发送到日志频道。")
                        return # 发送成功
                    except FileNotFoundError:
                         logger.error(f"数据库记录的贴纸文件 {db_message.media_path} 未找到。")
                         # 继续尝试动态下载
                    except Exception as sticker_send_err:
                         logger.error(f"发送已保存的贴纸文件 {message.id} (路径: {db_message.media_path}) 失败: {sticker_send_err}", exc_info=True)
                         # 发送失败，降级为仅发送文本
                    finally:
                        if media_context: # 确保退出上下文
                            try: media_context.__exit__(None, None, None)
                            except Exception as cm_exit_e: logger.error(f"退出贴纸媒体上下文时出错: {cm_exit_e}")
                            media_context = None # 重置
                else:
                    logger.warning(f"无法从数据库找到贴纸 {message.id} 的文件路径，尝试动态下载发送。")

                # 尝试动态下载并发送贴纸（作为备选或首选）
                try:
                    # 使用 RestrictedMediaHandler 下载（即使它可能不是受限的，它应该也能处理）
                    async with self.restricted_media_handler.prepare_media(message) as prepared_media_path:
                        if prepared_media_path:
                            await self.client.send_file(
                                self.log_chat_id,
                                prepared_media_path,
                                caption=text,
                                parse_mode="markdown",
                                reply_to=message.reply_to_msg_id
                            )
                            logger.info(f"动态下载并发送了贴纸 {message.id}。")
                            return # 发送成功
                        else:
                            logger.error(f"使用 RestrictedMediaHandler 下载贴纸 {message.id} 失败，未返回路径。")
                except Exception as sticker_dl_err:
                    logger.error(f"动态下载并发送贴纸 {message.id} 时出错: {sticker_dl_err}", exc_info=True)

                # 如果贴纸发送失败，则降级到下面发送纯文本

            # 2. 处理受限媒体 (非贴纸)
            elif is_restricted:
                logger.debug(f"消息 {message.id} 包含受限媒体，使用 RestrictedMediaHandler 处理。")
                try:
                    async with self.restricted_media_handler.prepare_media(message) as prepared_media_path:
                        if prepared_media_path:
                            # 使用 LogSender 发送解密后的文件
                            await self.log_sender.send_message(text, file=prepared_media_path, parse_mode="markdown")
                            logger.info(f"受限媒体消息 {message.id} 已处理并发送。")
                            return # 发送成功
                        else:
                            logger.warning(f"RestrictedMediaHandler 未能准备好受限媒体 {message.id}。")
                except Exception as restricted_err:
                    logger.error(f"处理受限媒体 {message.id} 时出错: {restricted_err}", exc_info=True)
                # 如果处理失败，降级到下面发送纯文本

            # 3. 处理普通媒体 (非贴纸，非受限)
            else:
                logger.debug(f"消息 {message.id} 包含普通媒体，尝试从数据库检索。")
                db_message = self.db.get_message_by_id(message.id)
                if db_message and db_message.media_path:
                    try:
                        media_context = retrieve_media_as_file(db_message.media_path, is_restricted=False)
                        media_file_path = media_context.__enter__() # 手动进入上下文
                        # 使用 LogSender 发送普通媒体文件
                        await self.log_sender.send_message(text, file=media_file_path, parse_mode="markdown")
                        logger.info(f"普通媒体消息 {message.id} 已使用数据库文件发送。")
                        return # 发送成功
                    except FileNotFoundError:
                         logger.error(f"数据库记录的普通媒体文件 {db_message.media_path} 未找到。")
                         # 降级处理
                    except Exception as normal_media_err:
                        logger.error(f"发送普通媒体文件 {message.id} (路径: {db_message.media_path}) 失败: {normal_media_err}", exc_info=True)
                        # 降级处理
                    finally:
                        if media_context: # 确保退出上下文
                            try: media_context.__exit__(None, None, None)
                            except Exception as cm_exit_e: logger.error(f"退出普通媒体上下文时出错: {cm_exit_e}")
                            media_context = None # 重置
                else:
                     logger.warning(f"无法从数据库找到普通媒体 {message.id} 的文件路径。")
                # 如果找不到文件或发送失败，降级到下面发送纯文本

            # --- 降级处理：仅发送文本 ---
            logger.warning(f"消息 {message.id} 的媒体处理失败或未处理，仅发送文本信息。")
            await self.log_sender.send_message(
                f"⚠️ **媒体可能未发送** ⚠️\n\n{text}\n\n(原始媒体未能成功处理或发送)",
                parse_mode="markdown"
            )

        except Exception as e:
            logger.critical(f"发送带媒体的消息 {message.id} 时发生严重错误: {e}", exc_info=True)
            # 尝试发送最终的回退消息
            if self.log_sender:
                try:
                    await self.log_sender.send_message(
                        f"❌ **发送消息时发生严重错误** ❌\n\n"
                        f"尝试处理消息 ID `{message.id}` 时遇到意外问题。\n"
                        f"错误: {type(e).__name__}: {e}\n\n"
                        f"原始文本内容 (可能不完整):\n{text[:500]}...", # 只显示部分文本
                        parse_mode="markdown"
                    )
                except Exception as fallback_err:
                    logger.critical(f"发送最终错误回退消息也失败 (消息 ID: {message.id}): {fallback_err}")
        finally:
            # 确保手动管理的上下文被退出 (再次检查以防万一)
            if media_context:
                try:
                    media_context.__exit__(None, None, None)
                except Exception as cm_exit_e:
                    logger.error(f"在 finally 块中退出媒体文件上下文管理器时出错: {cm_exit_e}", exc_info=True)
