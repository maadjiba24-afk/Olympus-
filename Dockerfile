# Base pinned by DIGEST, not by the mutable `python:3.12-slim` tag — a tag can
# be repointed at a different image under a build that never changed. The tag is
# recorded here so the next bump is a deliberate edit rather than archaeology:
#
#   python:3.12-slim  (digest below resolved 2026-08-16)
#
# To bump: docker pull python:3.12-slim && \
#          docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
FROM python@sha256:dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65

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

# Non-root, with an EXPLICIT FIXED UID/GID. The numeric id is what the filesystem
# and any Docker volume actually record — a name alone is not enough, because the
# name resolves only inside this image while the volume keeps the number.
#
# ORDER IS LOAD-BEARING. Docker seeds a NEW named volume from the image's content
# AND from the ownership/mode of the mount point, so /app/memory must already be
# owned by this user at image-build time. chown after the volume exists is too
# late; chown after USER cannot run at all. Get it wrong and the volume is
# created root-owned, the non-root process cannot write, and every durable store
# (W1-1/W1-1b) fails at once — quietly, because the container still starts.
RUN groupadd --gid 10001 olympus \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin olympus \
    && mkdir -p /app/memory \
    && chown -R olympus:olympus /app/memory \
    && chmod 0700 /app/memory

# Declared AFTER the ownership/mode change so a new volume inherits both the
# fixed UID/GID and owner-only permissions.
VOLUME /app/memory

USER 10001:10001

EXPOSE 8484

# Liveness via the interpreter that is already in the image.
#
# python:3.12-slim ships NEITHER curl NOR wget. The usual
# `CMD curl -f http://localhost:8484/healthz` does not fail loudly here: the
# command is simply not found, the check exits non-zero, and the container is
# marked unhealthy FOREVER. Composed with the `condition: service_healthy` gates
# in deploy/docker-compose.yml that means the stack NEVER STARTS, with nothing
# in the logs explaining why — two individually-correct changes that break only
# in combination. Installing curl to ask a question the interpreter can already
# answer would be a runtime dependency added for nothing.
#
# --start-period is not cosmetic: failures inside it do not count against
# --retries, so a slow first boot cannot mark the container unhealthy and wedge
# the dependent services.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8484/healthz', timeout=3).status == 200 else 1)"]

# Default: browser chat UI. Other entrypoints:
#   docker run ... python -m olympus telegram     (Telegram gateway)
#   docker run ... python -m olympus heartbeat    (autonomous loop)
CMD ["python", "-m", "olympus", "web", "--host", "0.0.0.0"]
