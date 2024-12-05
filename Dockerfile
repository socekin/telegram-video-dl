FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 复制项目文件
COPY requirements.txt .
COPY bot.py .
COPY .env .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 运行机器人
CMD ["python", "bot.py"]
