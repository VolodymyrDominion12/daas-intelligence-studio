FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn

COPY server.py /app/server.py

ENV HOST=0.0.0.0 PORT=8765

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3)"

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8765"]
