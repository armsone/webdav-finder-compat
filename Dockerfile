# Production-suitable Dockerfile for WebDAV Compatibility Reverse Proxy
FROM python:3.11-slim

# Avoid writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEBDAV_LISTEN_HOST=0.0.0.0 \
    WEBDAV_LISTEN_PORT=8080 \
    WEBDAV_TLS_VERIFY=true \
    WEBDAV_PUBLIC_ORIGIN=https://nas.example.com:5006

WORKDIR /app

# Create non-root user
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /sbin/nologin -d /app appuser

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY gateway.py .

# Switch to non-root user
USER 10001:10001

EXPOSE 8080

ENTRYPOINT ["python", "gateway.py"]
