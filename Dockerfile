# Optional: only needed on hosts that cannot apt-install system packages (e.g. Render's native Python runtime).
# It is the same app plus the Tesseract OCR engine used for scanned PDFs and images.
FROM python:3.11-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python static/fonts/download_fonts.py || true
ENV PORT=10000
CMD gunicorn app:app --bind 0.0.0.0:$PORT --timeout 300 --workers 2
