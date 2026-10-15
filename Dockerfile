# Small, stateless API image. All data (concepts and user data) lives in Postgres
# (Neon); the server only needs DATABASE_URL, so instances can come and go.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# Files keep their permissions from the machine that deployed them (which may be
# owner-only), so make the code readable before switching to the unprivileged user.
RUN chmod -R a+rX /srv && useradd --system --no-create-home api
USER api
EXPOSE 8080
# exec: uvicorn becomes PID 1 and gets Cloud Run's SIGTERM for a clean shutdown.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers ${WORKERS:-2}"]
