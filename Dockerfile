FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system appuser \
    && useradd --system --gid appuser --home-dir /app --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --requirement requirements.txt

COPY --chown=appuser:appuser app.py database.py statement_import.py ./
COPY --chown=appuser:appuser static/ ./static/
COPY --chown=appuser:appuser strategies/ ./strategies/
COPY --chown=appuser:appuser templates/ ./templates/

RUN mkdir --parents /app/data/statements \
    && chown --recursive appuser:appuser /app/data

USER appuser

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--preload", "--access-logfile", "-", "app:app"]
