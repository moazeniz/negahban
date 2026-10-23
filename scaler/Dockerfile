# meow-lb: load balancer + deterministic replica controller for stateless Liara apps.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PORT=8080
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt && useradd --uid 10001 --create-home app
COPY meowlb ./meowlb
COPY tests ./tests
# Unit tests run at build time (fake backends and a fake Liara API; no network).
RUN python -m unittest discover -s tests -q && rm -rf tests && python -m compileall -q meowlb
USER 10001
EXPOSE 8080
CMD ["python", "-m", "meowlb.app"]
