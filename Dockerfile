# Slim production image. Serves the addon with Granian (ASGI) on 7003.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/addon

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 7003

CMD ["granian", "--interface", "asgi", "--host", "0.0.0.0", "--port", "7003", "app.main:app"]
