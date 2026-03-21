# ───────────────────────────
# WorkIQ Teams Relay Bot
# ───────────────────────────
FROM python:3.12-slim

# 1. Working directory
WORKDIR /app

# 2. Runtime env flags
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=3978

# 3. OS build tools (gcc needed for some native deps)
RUN apt-get update && \
    apt-get install -y gcc && \
    rm -rf /var/lib/apt/lists/*

# 4. Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 5. Copy application code
COPY . .

# 6. Expose port
EXPOSE 3978

# 7. Non-root user
RUN adduser --disabled-password --gecos '' appuser && \
    chown -R appuser:appuser /app
USER appuser

# 8. Entrypoint
CMD ["python", "app.py"]
