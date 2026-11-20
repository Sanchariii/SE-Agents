FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg for audio/video; libgl1 + libglib for OpenCV; LibreOffice Impress and
# fonts for max mode's slide renders.
# Both are only needed by app/genai/ingestion/local — drop them once video
# ingestion moves to Azure Video Indexer.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libgl1 libglib2.0-0 \
        libreoffice-impress fontconfig fonts-crosextra-carlito fonts-liberation2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY company-ppt-templet/ ./company-ppt-templet/

# Max mode renders slides with headless LibreOffice; without Barlow it would
# substitute a wider font and the visual review would judge the wrong wrapping.
COPY company-ppt-templet/fonts/barlow/ /usr/local/share/fonts/barlow/
RUN fc-cache -f
COPY app ./app

# Never run as root.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /srv
USER appuser

ENV APP_TYPE=hosted
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health').read()"

CMD ["uvicorn", "app.core.services.server:app", "--host", "0.0.0.0", "--port", "8000"]
