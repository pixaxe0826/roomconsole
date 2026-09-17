FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HUB_DATA_DIR=/app/data HUB_PORT=8088
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && groupadd --gid 10001 hub && useradd --uid 10001 --gid 10001 --create-home hub
COPY app ./app
COPY web ./web
COPY widgets ./widgets
COPY run.py ./run.py
RUN mkdir -p /app/data && chown -R 10001:10001 /app
USER 10001:10001
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/healthz',timeout=3)"
CMD ["python","run.py"]
