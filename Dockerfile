FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Memory persists across container restarts via this volume.
VOLUME /app/memory

EXPOSE 8484

# Default: browser chat UI. Other entrypoints:
#   docker run ... python -m olympus telegram     (Telegram gateway)
#   docker run ... python -m olympus heartbeat    (autonomous loop)
CMD ["python", "-m", "olympus", "web", "--host", "0.0.0.0"]
