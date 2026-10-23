FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE {{port}}
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port {{port}} --proxy-headers --workers ${WEB_CONCURRENCY:-1}"]
