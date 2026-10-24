FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

# CPU-only PyTorch first (the default wheel pulls multi-GB CUDA libraries)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

RUN useradd -m appuser \
    && mkdir -p /data/uploads /app/logs /home/appuser/.cache/whisper \
    && chown -R appuser:appuser /data /app/logs /home/appuser
USER appuser

ENV UPLOAD_DIR=/data/uploads LOG_DIR=/app/logs PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
