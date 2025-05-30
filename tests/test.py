import os
import re
import logging

# import tempfile  # 不再需要显式下载到临时文件
from dotenv import load_dotenv
from telethon import TelegramClient, events, errors
from telethon.tl.types import (
    InputMediaDocument,
    InputDocument,
    InputMediaPhoto,
    InputPhoto,
)

# --- 配置 ---
# 将基础日志级别设置为 DEBUG，并为 telethon logger 也设置 DEBUG
# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s') # 原配置
logging.basicConfig(
    level=logging.INFO,  # 设置为 DEBUG
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)  # 添加 logger 名称

# 可选：如果你只想看 Telethon 的 DEBUG 日志，而保持你自己的代码为 INFO
# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
# logging.getLogger('telethon').setLevel(logging.DEBUG)

load_dotenv()

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_NAME = os.getenv("SESSION_NAME", "media_downloader_test")
LOG_CHAT_ID = os.getenv("LOG_CHAT_ID")  # 从 .env 读取目标 LOG_CHAT_ID

if not all([API_ID, API_HASH, LOG_CHAT_ID]):
    logging.error("请确保 .env 文件中设置了 API_ID, API_HASH, 和 LOG_CHAT_ID")
    exit(1)

try:
    # 尝试将 LOG_CHAT_ID 转换为整数，如果失败则保持为字符串 (username)
    LOG_CHAT_ID = int(LOG_CHAT_ID)
    logging.info(f"日志频道 ID '{LOG_CHAT_ID}' 将作为数字 ID 处理。")
except ValueError:
    logging.info(f"日志频道 ID '{LOG_CHAT_ID}' 不是数字，将作为 username 处理。")

# 正则表达式匹配 Telegram 消息链接
# 支持 t.me/username/123 和 t.me/c/123456789/123 格式
link_pattern = re.compile(r"https://t\.me/(\w+|c/\d+)/(\d+)")

# --- Telethon 客户端 ---
# 使用 system_version='4.16.30-vxCUSTOM' 可能有助于处理某些限制，但通常不需要
client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)

# 新增：从 .env 读取测试链接
TEST_MESSAGE_LINK = os.getenv("TEST_MESSAGE_LINK")


async def process_message_link(link: str):
    """处理单个 Telegram 消息链接，下载媒体并发送到 LOG_CHAT_ID"""
    match = link_pattern.search(link)

    if not match:
        logging.error(f"提供的链接格式无效: {link}")
        return False  # 表示处理失败

    identifier = match.group(1)  # username or c/channel_id
    message_id = int(match.group(2))

    logging.info(f"开始处理链接: identifier={identifier}, message_id={message_id}")

    # downloaded_file_path = None  # 不再需要
    success = False  # 标记处理是否成功
    try:
        # 1. 获取源 Chat Entity
        logging.info(f"尝试获取源实体: {identifier}")
        source_entity = await client.get_entity(identifier)
        source_entity_title = getattr(
            source_entity, "title", getattr(source_entity, "username", identifier)
        )
        logging.info(f"成功获取源实体: {source_entity_title}")

        # 2. 获取源消息
        logging.info(f"尝试获取消息 ID: {message_id} 从 {source_entity_title}")
        source_messages = await client.get_messages(source_entity, ids=message_id)

        if not source_messages:
            logging.warning(f"找不到消息 ID {message_id} 在 {source_entity_title}")
            # await event.reply(f"错误：在源 '{source_entity_title}' 中找不到消息 ID {message_id}。") # 不再回复事件
            return False

        source_message = (
            source_messages  # get_messages with single ID returns the message itself
        )

        # 3. 检查是否有媒体
        if not source_message.media:
            logging.info(f"消息 {message_id} 不包含媒体文件。")
            # await event.reply(f"提示：链接指向的消息 {message_id} 不包含媒体文件。") # 不再回复事件
            return False  # 可以认为这不是我们想要处理的情况

        media_type = type(source_message.media).__name__
        logging.info(f"消息 {message_id} 包含媒体，类型: {media_type}")

        # 4. 获取目标 Chat Entity (LOG_CHAT_ID)
        logging.info(f"尝试获取目标日志实体: {LOG_CHAT_ID}")
        try:
            target_entity = await client.get_entity(LOG_CHAT_ID)
            target_entity_title = getattr(
                target_entity, "title", getattr(target_entity, "username", LOG_CHAT_ID)
            )
            logging.info(f"成功获取目标日志实体: {target_entity_title}")
        except (ValueError, TypeError) as e:
            logging.error(f"无法解析配置的 LOG_CHAT_ID '{LOG_CHAT_ID}': {e}")
            # await event.reply(f"错误：配置的 LOG_CHAT_ID ('{LOG_CHAT_ID}') 无效或无法访问。请检查 .env 文件。") # 不再回复事件
            return False
        except Exception as e:
            logging.error(f"获取目标日志实体时发生意外错误: {e}")
            # await event.reply(f"错误：无法访问目标日志频道 '{LOG_CHAT_ID}'。") # 不再回复事件
            return False

        # 5. 直接尝试发送媒体对象，而不是下载
        logging.info(f"尝试直接发送消息 {message_id} 的媒体到 {target_entity_title}")
        try:
            caption = (
                f"媒体来源: {source_entity_title} (消息ID: {message_id})\n"
                f"原始链接: https://t.me/{identifier}/{message_id}"
            )

            logging.info(
                f"准备发送媒体 - 类型: {type(source_message.media)}, 大小: {getattr(source_message.media, 'size', '未知')} bytes"
            )

            # 获取媒体文件ID并发送
            logging.info(f"获取媒体文件信息 - 媒体类型: {type(source_message.media).__name__}")
            if hasattr(source_message.media, "document"):
                file_id = source_message.media.document.id
                access_hash = source_message.media.document.access_hash
                logging.info(f"文档文件信息 - ID: {file_id}, 访问哈希: {access_hash}")
                file_reference = source_message.media.document.file_reference
                file = InputMediaDocument(
                    id=InputDocument(
                        id=file_id,
                        access_hash=access_hash,
                        file_reference=file_reference,
                    )
                )
            elif hasattr(source_message.media, "photo"):
                file_id = source_message.media.photo.id
                access_hash = source_message.media.photo.access_hash
                logging.info(f"图片文件信息 - ID: {file_id}, 访问哈希: {access_hash}")
                file_reference = source_message.media.photo.file_reference
                file = InputMediaPhoto(
                    id=InputPhoto(
                        id=file_id,
                        access_hash=access_hash,
                        file_reference=file_reference,
                    )
                )
            else:
                raise ValueError("不支持的媒体类型")

            # 使用文件ID发送媒体
            await client.send_file(
                target_entity,
                file=file,
                caption=caption,
            )
            logging.info(f"成功发送媒体到 {target_entity_title}")
        except Exception as e:
            logging.error(
                f"发送媒体时发生错误 (消息ID: {message_id}, 目标: {target_entity_title})。"
                f"媒体类型: {type(source_message.media).__name__}, 错误详情: {str(e)}"
            )
            raise  # 重新抛出异常以便外层捕获处理

        logging.info(f"成功将媒体从消息 {message_id} 发送到 {target_entity_title}")
        # await event.reply(f"成功将链接指向的媒体发送到日志频道 '{target_entity_title}'。") # 不再回复
        success = True  # 标记成功

    except errors.FloodWaitError as e:
        logging.error(
            f"触发 Telegram Flood Wait: 需等待 {e.seconds} 秒 (错误详情: {str(e)})"
        )
    except errors.ChannelPrivateError:
        logging.error(
            f"无法访问私有频道/群组 '{identifier}' (消息ID: {message_id})。错误详情: 该频道/群组是私有的，需要邀请才能加入。"
        )
    except errors.ChatForbiddenError:
        logging.error(
            f"访问被禁止的频道/群组 '{identifier}' (消息ID: {message_id})。错误详情: 您已被禁止访问或该频道/群组不存在。"
        )
    except errors.ChatAdminRequiredError as e:
        logging.error(
            f"权限不足，无法向目标频道 '{LOG_CHAT_ID}' 发送消息 (消息ID: {message_id})。"
            f"错误详情: {str(e)}。需要管理员权限或发送媒体权限。"
        )
    except errors.UserNotParticipantError:
        logging.error(
            f"账户未加入源频道/群组 '{identifier}' (消息ID: {message_id})。错误详情: 需要先加入该频道/群组才能访问消息。"
        )
    except errors.MediaUnavailableError as e:
        logging.error(
            f"无法访问或发送来自消息 {message_id} 的媒体 (频道: {identifier})。"
            f"错误详情: {str(e)}。可能原因: 1) 源频道开启了严格的内容保护 2) 媒体已过期/删除 3) 没有下载权限"
        )
    except (ValueError, TypeError) as e:
        logging.error(
            f"无效的标识符格式 '{identifier}' 或 '{LOG_CHAT_ID}' (消息ID: {message_id})。"
            f"错误详情: {str(e)}。请检查: 1) 链接格式是否正确 2) LOG_CHAT_ID 是否有效"
        )
    except Exception as e:
        logging.exception(
            f"处理链接时发生未预期的错误 (消息ID: {message_id}, 频道: {identifier})。"
            f"错误类型: {type(e).__name__}, 错误详情: {str(e)}"
        )
        # await event.reply(f"处理链接时发生未知错误: {e}") # 不再回复
    finally:
        # 不再需要清理临时文件
        # if downloaded_file_path and os.path.exists(downloaded_file_path):
        #     try:
        #         os.remove(downloaded_file_path)
        #         logging.info(f"已删除临时文件: {downloaded_file_path}")
        #     except OSError as e:
        #         logging.error(f"删除临时文件 {downloaded_file_path} 时出错: {e}")
        return success  # 返回处理结果


async def main():
    """主函数，启动客户端，处理配置的链接，然后退出"""
    logging.info("媒体下载转发脚本（单次运行模式）启动...")

    if not TEST_MESSAGE_LINK:
        logging.error("错误：未在 .env 文件中配置 TEST_MESSAGE_LINK。")
        return

    logging.info(f"目标处理链接: {TEST_MESSAGE_LINK}")
    logging.info(f"媒体将发送到日志频道: {LOG_CHAT_ID}")

    async with client:  # 使用 async with 确保客户端正确关闭
        logging.info("客户端连接中...")
        # start() 会自动处理登录
        await client.start()
        logging.info("客户端已连接并登录。")

        # 预先检查是否能访问 LOG_CHAT_ID
        try:
            await client.get_entity(LOG_CHAT_ID)
            logging.info(f"成功验证可以访问目标日志频道: {LOG_CHAT_ID}")
        except Exception as e:
            logging.error(f"无法访问配置的目标 LOG_CHAT_ID ('{LOG_CHAT_ID}'): {e}")
            logging.error(
                "请检查 LOG_CHAT_ID 是否正确，以及该账号是否有权访问。脚本将退出。"
            )
            return  # 无法访问目标，直接退出

        # 处理配置的链接
        logging.info(f"开始处理配置的链接: {TEST_MESSAGE_LINK}")
        success = await process_message_link(TEST_MESSAGE_LINK)

        if success:
            logging.info("链接处理成功完成。")
        else:
            logging.error("链接处理失败。请检查日志获取详细信息。")

    logging.info("脚本执行完毕，客户端已断开连接。")


if __name__ == "__main__":
    # 使用 client.loop.run_until_complete 运行 main coroutine
    client.loop.run_until_complete(main())
