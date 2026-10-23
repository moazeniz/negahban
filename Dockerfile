FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data TZ=Asia/Tehran
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY negahban ./negahban
COPY static ./static
COPY demo-shop ./demo-shop
RUN useradd -m -u 1000 negahban && mkdir -p /data && chown -R negahban /data /app
EXPOSE 8080
# the Liara disk is mounted at runtime (root-owned): fix ownership, then drop to the unprivileged user
CMD ["sh", "-c", "chown -R negahban:negahban /data 2>/dev/null; exec setpriv --reuid=1000 --regid=1000 --init-groups uvicorn negahban.main:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips '*' --no-access-log"]
