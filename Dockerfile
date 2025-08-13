# Use a slim Python image
FROM python:3.11-slim

# Install system deps + FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Create app dir
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the bot code
COPY bot.py /app/bot.py

# Env (optional: you can override in Render dashboard)
ENV SESSION_NAME=krishnavi_session

# Start the bot
CMD ["python", "bot.py"]
