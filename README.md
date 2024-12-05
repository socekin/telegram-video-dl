# Telegram Video Downloader Bot

A Telegram bot that downloads videos from YouTube and Twitter.

## Features

- Download videos from YouTube and Twitter
- Automatically handles video size limitations
- Simple URL-based interface
- Supports video streaming

## Setup

1. Clone this repository
2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Create a `.env` file and add your Telegram bot token:
```
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
```

4. Run the bot:
```bash
python bot.py
```

## Usage

1. Start a chat with the bot
2. Send `/start` to get started
3. Simply paste a YouTube or Twitter video URL
4. Wait for the bot to process and send you the video

## Notes

- Video file size is limited to 50MB due to Telegram restrictions
- Some videos might not be available for download due to platform restrictions
