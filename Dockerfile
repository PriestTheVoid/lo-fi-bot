FROM python:3.12-slim

# Логи сразу в stdout (видно в панели), без .pyc и кэша pip
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Копируем только код — .env в образ не попадает (токен задаётся переменными окружения)
COPY bot.py ./

CMD ["python", "bot.py"]
