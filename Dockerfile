# Multi-stage Dockerfile for Indian Stock Market Intelligence Platform

# Stage 1: Build React Frontend
FROM node:20-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: Python Backend & Static Server
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for lxml and PyPDF2
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libxml2-dev libxslt1-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy backend requirements & install
COPY backend/requirements.txt ./backend/
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy backend code and built frontend dist from Stage 1
COPY backend/ ./backend/
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# Operational scripts. They are run with `docker exec` against the live
# container, so they have to be in the image — the repo checkout on the host is
# not what the container sees, and these need the installed dependencies and the
# mounted database.
COPY ops/ ./ops/

WORKDIR /app/backend

EXPOSE 8000

CMD ["python", "run.py"]
