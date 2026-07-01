FROM python:3.12-slim

WORKDIR /app
# Install the exact, hash-pinned dependency set CI tests against — the same
# supply-chain guarantee docs/SUPPLY_CHAIN.md advertises. Don't fall back to the
# floating requirements.txt ranges here.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY . .
# Install the package itself (no deps — already pinned above) so the `olympus`
# console script and package metadata (importlib.metadata version) exist, and
# `python -m olympus` works from any working directory.
RUN pip install --no-cache-dir --no-deps .

# Memory persists across container restarts via this volume.
VOLUME /app/memory

EXPOSE 8484

# Default: browser chat UI. Other entrypoints:
#   docker run ... python -m olympus telegram     (Telegram gateway)
#   docker run ... python -m olympus heartbeat    (autonomous loop)
CMD ["python", "-m", "olympus", "web", "--host", "0.0.0.0"]
