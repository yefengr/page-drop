FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY service/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY service/app /app/app

RUN groupadd --gid 10001 pagedrop \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin pagedrop \
    && mkdir -p /data \
    && chown 10001:10001 /data \
    && chmod 0700 /data

USER 10001:10001

EXPOSE 18090

CMD ["python", "-m", "uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "18090", "--workers", "1", "--no-server-header", "--timeout-keep-alive", "15"]
