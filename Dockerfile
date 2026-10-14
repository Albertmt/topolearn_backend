# Small, stateless image: the SQLite DB is baked in, so every instance is self-sufficient.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts
COPY data ./data
# Use a prebuilt data/topolearn.db if present, otherwise build from the JSON sources.
RUN [ -f data/topolearn.db ] || python scripts/build_db.py
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers ${WORKERS:-2}"]
