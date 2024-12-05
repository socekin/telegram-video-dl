import os
import logging
import re
import requests
import subprocess
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import tempfile
import json
import glob
import math
import asyncio
import time

# Load environment variables
load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ALLOWED_USERS = [int(id.strip()) for id in os.getenv('ALLOWED_TELEGRAM_USER_IDS', '').split(',') if id.strip()]

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 设置 gallery-dl 的日志级别
gallery_dl_logger = logging.getLogger('gallery_dl')
gallery_dl_logger.setLevel(logging.WARNING)

# URL patterns
TWITTER_PATTERN = r'(?:https?:\/\/)?(?:www\.)?(?:twitter\.com|x\.com)\/\w+\/status\/(\d+)'

# 文件分片大小 (45MB，为元数据预留空间)
CHUNK_SIZE = 45 * 1024 * 1024
MAX_TELEGRAM_SIZE = 48 * 1024 * 1024  # Telegram 文件大小限制

# 创建 gallery-dl 配置文件
def create_gallery_dl_config():
    config = {
        "extractor": {
            "twitter": {
                "cookies": {
                    "auth_token": os.getenv('TWITTER_AUTH_TOKEN', '')
                }
            }
        }
    }
    
    config_dir = os.path.expanduser('~/.config/gallery-dl')
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, 'config.json')
    
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=4)
    
    logger.info(f"已创建 gallery-dl 配置文件: {config_path}")

async def check_user(update: Update) -> bool:
    """检查用户是否有权限使用机器人"""
    user_id = update.effective_user.id
    if not ALLOWED_USERS or user_id in ALLOWED_USERS:
        return True
    await update.message.reply_text('抱歉，您没有权限使用此机器人。')
    return False

async def split_video(file_path: str, chunk_size: int = CHUNK_SIZE) -> list:
    """将视频文件分割成多个小于50MB的部分"""
    file_size = os.path.getsize(file_path)
    if file_size <= chunk_size:
        return [file_path]
    
    # 创建临时目录存放分片
    temp_dir = os.path.dirname(file_path)
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    
    # 使用 ffmpeg 分割视频
    duration_cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
    duration = float(subprocess.check_output(duration_cmd).decode().strip())
    
    # 计算每个分片的时长
    chunk_count = math.ceil(file_size / chunk_size)
    segment_duration = duration / chunk_count
    
    # 使用更可靠的分片参数
    split_cmd = [
        'ffmpeg', '-i', file_path,
        '-c', 'copy',  # 复制编码，不重新编码
        '-f', 'segment',
        '-segment_time', str(segment_duration),
        '-reset_timestamps', '1',
        '-segment_format', 'mp4',
        '-max_muxing_queue_size', '1024',
        os.path.join(temp_dir, f'{base_name}_%03d.mp4')
    ]
    
    subprocess.run(split_cmd, check=True, capture_output=True)
    
    # 返回所有分片文件的路径
    return sorted(glob.glob(os.path.join(temp_dir, f'{base_name}_*.mp4')))

async def merge_video_parts(parts: list, output_path: str) -> str:
    """合并视频分片"""
    if len(parts) == 1:
        return parts[0]
        
    # 创建合并文件列表
    list_file = os.path.join(os.path.dirname(output_path), "files.txt")
    with open(list_file, "w") as f:
        for part in parts:
            f.write(f"file '{part}'\n")
    
    # 使用 FFmpeg 合并视频
    merge_cmd = [
        'ffmpeg', '-f', 'concat',
        '-safe', '0',
        '-i', list_file,
        '-c', 'copy',
        output_path
    ]
    
    subprocess.run(merge_cmd, check=True, capture_output=True)
    os.remove(list_file)  # 清理临时文件
    return output_path

async def download_twitter_video(url: str, temp_dir: str, status_message) -> str:
    """使用 gallery-dl 下载 Twitter 视频"""
    try:
        await status_message.edit_text('🔍 正在从 Twitter 获取视频信息...')
        logger.info(f"开始下载 Twitter 视频: {url}")
        
        # 运行 gallery-dl 命令下载视频
        cmd = ['gallery-dl', '-D', temp_dir, '--verbose', url]
        logger.info(f"执行命令: {' '.join(cmd)}")
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        last_update = time.time()
        downloading_started = False
        
        # 创建异步任务来读取输出
        async def read_output():
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                log_line = line.decode().strip()
                if log_line:
                    if '[debug]' in log_line.lower():
                        logger.debug(f"gallery-dl: {log_line}")
                    elif '[error]' in log_line.lower():
                        logger.error(f"gallery-dl: {log_line}")
                    elif '[warning]' in log_line.lower():
                        logger.warning(f"gallery-dl: {log_line}")
                    else:
                        logger.info(f"gallery-dl: {log_line}")
                    
        async def read_error():
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                log_line = line.decode().strip()
                if log_line:
                    if '[debug]' in log_line.lower():
                        logger.debug(f"gallery-dl stderr: {log_line}")
                    else:
                        logger.error(f"gallery-dl stderr: {log_line}")
        
        # 启动输出读取任务
        output_task = asyncio.create_task(read_output())
        error_task = asyncio.create_task(read_error())
        
        # 等待下载完成，设置超时时间为5分钟
        try:
            await asyncio.wait_for(process.wait(), timeout=300)
        except asyncio.TimeoutError:
            process.terminate()
            raise Exception("下载超时，请稍后重试")
        finally:
            # 确保读取任务完成
            await output_task
            await error_task
        
        if process.returncode != 0:
            stderr = await process.stderr.read()
            error_msg = stderr.decode().strip()
            logger.error(f"gallery-dl 返回错误码 {process.returncode}: {error_msg}")
            raise Exception(f"下载失败: {error_msg}")
        
        # 查找下载的视频文件
        video_files = glob.glob(os.path.join(temp_dir, '*.mp4'))
        logger.info(f"在目录 {temp_dir} 中找到的视频文件: {video_files}")
        
        if not video_files:
            # 检查其他可能的视频格式
            for ext in ['*.mkv', '*.webm', '*.mov']:
                other_files = glob.glob(os.path.join(temp_dir, ext))
                if other_files:
                    video_files = other_files
                    break
            
            if not video_files:
                logger.error(f"在目录 {temp_dir} 中未找到视频文件")
                # 列出目录中的所有文件以进行调试
                all_files = os.listdir(temp_dir)
                logger.info(f"目录中的所有文件: {all_files}")
                raise Exception("未找到下载的视频文件")
        
        # 返回最新下载的视频文件
        newest_video = max(video_files, key=os.path.getctime)
        logger.info(f"选择的视频文件: {newest_video}")
        return newest_video
        
    except Exception as e:
        logger.error(f"Twitter video download error: {str(e)}")
        raise

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Download video from Twitter."""
    if not await check_user(update):
        return
    
    url = update.message.text.strip()
    status_message = await update.message.reply_text('正在处理您的请求，请稍候...')
    logger.info(f"收到下载请求: {url}")
    
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            logger.info(f"创建临时目录: {temp_dir}")
            video_path = None
            
            # 检查是否是 Twitter 链接
            if re.search(TWITTER_PATTERN, url):
                logger.info("检测到 Twitter 链接")
                video_path = await download_twitter_video(url, temp_dir, status_message)
            else:
                logger.warning(f"无效的链接: {url}")
                await status_message.edit_text('请发送有效的 Twitter 视频链接。')
                return
            
            if not video_path or not os.path.exists(video_path):
                logger.error(f"视频路径无效: {video_path}")
                raise Exception("视频下载失败")
            
            await status_message.edit_text('正在处理视频...')
            file_size = os.path.getsize(video_path)
            logger.info(f"视频文件大小: {file_size} 字节")
            
            if file_size > MAX_TELEGRAM_SIZE:
                logger.info("视频文件超过大小限制，开始分割")
                await status_message.edit_text('视频较大，正在分割...')
                video_parts = await split_video(video_path)
                logger.info(f"分割后的视频部分: {video_parts}")
                total_parts = len(video_parts)
                
                # 尝试合并较小的分片
                merged_parts = []
                current_size = 0
                temp_parts = []
                
                for part in video_parts:
                    part_size = os.path.getsize(part)
                    if current_size + part_size <= MAX_TELEGRAM_SIZE:
                        temp_parts.append(part)
                        current_size += part_size
                    else:
                        if len(temp_parts) > 1:
                            # 合并这些部分
                            merged_path = os.path.join(temp_dir, f'merged_{len(merged_parts)}.mp4')
                            await merge_video_parts(temp_parts, merged_path)
                            merged_parts.append(merged_path)
                        else:
                            merged_parts.extend(temp_parts)
                        temp_parts = [part]
                        current_size = part_size
                
                # 处理最后的部分
                if temp_parts:
                    if len(temp_parts) > 1:
                        merged_path = os.path.join(temp_dir, f'merged_{len(merged_parts)}.mp4')
                        await merge_video_parts(temp_parts, merged_path)
                        merged_parts.append(merged_path)
                    else:
                        merged_parts.extend(temp_parts)
                
                # 发送合并后的视频片段
                total_merged_parts = len(merged_parts)
                await status_message.edit_text(f'开始发送视频 (共 {total_merged_parts} 个部分)...')
                
                for i, part in enumerate(merged_parts, 1):
                    try:
                        with open(part, 'rb') as video:
                            await update.message.reply_video(
                                video=video,
                                caption=f'视频部分 {i}/{total_merged_parts}',
                                read_timeout=60,
                                write_timeout=60,
                                connect_timeout=60,
                                pool_timeout=60,
                            )
                        await asyncio.sleep(2)  # 等待一下，避免发送太快
                    except Exception as e:
                        logger.error(f"Error sending video part {i}: {str(e)}")
                        await update.message.reply_text(f'发送视频部分 {i} 失败，请重试')
                
                await status_message.edit_text('视频发送完成！')
            else:
                # 直接发送视频
                await status_message.edit_text('正在发送视频...')
                with open(video_path, 'rb') as video:
                    await update.message.reply_video(
                        video=video,
                        caption='您的视频已准备就绪',
                        read_timeout=60,
                        write_timeout=60,
                        connect_timeout=60,
                        pool_timeout=60,
                    )
                await status_message.edit_text('视频发送完成！')
            
    except Exception as e:
        logger.error(f"Error downloading video: {str(e)}")
        await status_message.edit_text(f'下载失败：{str(e)}')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /start is issued."""
    if not await check_user(update):
        return
    await update.message.reply_text(
        '欢迎使用视频下载机器人！\n'
        '请发送 Twitter 视频链接，我会帮你下载视频。\n'
        '支持的格式：\n'
        '- Twitter: https://twitter.com/user/status/...\n'
        '注意：大文件会自动分片发送'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /help is issued."""
    if not await check_user(update):
        return
    await update.message.reply_text(
        '使用说明：\n'
        '1. 直接发送 Twitter 视频链接即可下载\n'
        '2. 大文件会自动分片发送\n'
        '3. 支持 twitter.com 和 x.com 域名'
    )

def main():
    """Start the bot."""
    # 创建 gallery-dl 配置
    create_gallery_dl_config()
    
    # Create the Application
    application = Application.builder().token(TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_video))

    # Run the bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
