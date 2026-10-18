FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN useradd -m -u 1000 app && mkdir -p /app/data /app/logs && chown -R app:app /app
USER app
EXPOSE 8000
CMD ["python", "-m", "dgis", "ui"]
