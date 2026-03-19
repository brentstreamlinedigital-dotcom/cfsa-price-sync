FROM python:3.12-slim

# Install system deps for Playwright and pdfplumber
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl ca-certificates \
    libglib2.0-0 libnss3 libnspr4 libdbus-1-3 \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
    libpangocairo-1.0-0 libx11-6 libx11-xcb1 libxcb1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY config/ config/
COPY src/ src/
COPY scrapers/ scrapers/

# Run as non-root
RUN useradd -m appuser && chown -R appuser /app
USER appuser

# Install Playwright browsers as appuser so they land in /home/appuser/.cache
RUN playwright install chromium

ENTRYPOINT ["python", "-m", "src.main"]
