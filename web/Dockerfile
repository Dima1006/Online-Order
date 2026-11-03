FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -r -u 10001 app && mkdir /data && chown app /data
COPY app ./app
USER app
ENV DATABASE_PATH=/data/app.db
VOLUME /data
EXPOSE 8000
# ONE worker on purpose: live updates and rate limits live in memory.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--ws-max-size", "1024"]
