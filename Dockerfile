# Multi-stage Dockerfile for Indian Stock Market Intelligence Platform

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for lxml and PyPDF2
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libxml2-dev libxslt1-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy backend requirements & install
COPY backend/requirements.txt ./backend/
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy full application code
COPY backend/ ./backend/
COPY frontend/dist ./frontend/dist

WORKDIR /app/backend

EXPOSE 8000

CMD ["python", "run.py"]
