FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖（web3.py 需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源代码
COPY src/ ./src/

# 创建非 root 用户运行
RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app
USER botuser

# 默认启动命令
CMD ["python", "-u", "src/bot.py"]