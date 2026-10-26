FROM python:3.11-slim

# Prevent python from buffering stdout/stderr and writing pyc files
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies (curl for health checks, sqlite3 tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Create directories for persistent SQLite data volumes
# /data is standard for Fly.io, Koyeb, Railway
# /var/data is standard for Render persistent disks
RUN mkdir -p /data /var/data

# Default persistent database location
ENV DATABASE_PATH=/data/store.db

# Run the Telegram bot application
CMD ["python", "main.py"]
