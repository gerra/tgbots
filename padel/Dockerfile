FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py club.json ./

# Long polling: no ports. Pass TELEGRAM_BOT_TOKEN with --env-file .env.
CMD ["python", "bot.py"]
