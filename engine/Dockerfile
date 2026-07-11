# Timesheet Intelligence engine — container image for Render / Cloud Run / any Docker host.
# Bundles the two system binaries the pipeline shells out to:
#   • tesseract-ocr  — local OCR for scanned PDFs / images
#   • libreoffice     — converts .xlsx/.xls/.csv/.docx → PDF (preview + extraction)
FROM python:3.12-slim

# System deps. libreoffice-calc + writer cover spreadsheets and Word docs and
# pull in libreoffice-core automatically; fonts make the PDF renders legible.
RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr \
      libreoffice-calc libreoffice-writer \
      fonts-dejavu fonts-liberation \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first so this layer caches across code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY tsengine ./tsengine
COPY config ./config

ENV PYTHONUNBUFFERED=1 \
    TSE_OUTPUT_DIR=/tmp/ts_output \
    TSE_USE_LOCAL_OCR=true

EXPOSE 8078
# Render/Cloud Run inject $PORT; bind 0.0.0.0 so the service is reachable.
CMD ["sh", "-c", "uvicorn tsengine.api.app:app --host 0.0.0.0 --port ${PORT:-8078}"]
