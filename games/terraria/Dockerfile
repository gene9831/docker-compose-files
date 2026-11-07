FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    MONO_IOMAP=all
RUN groupadd --gid 1000 terraria \
    && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /data terraria

WORKDIR /opt/terraria
COPY --chmod=0644 server.py .
USER 1000:1000
ENTRYPOINT ["python3", "/opt/terraria/server.py"]
