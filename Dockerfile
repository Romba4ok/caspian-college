FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080
WORKDIR /app

COPY requirements.txt requirements-hosting.txt ./
RUN pip install --no-cache-dir -r requirements-hosting.txt
COPY app ./app
COPY migrations ./migrations
COPY templates ./templates
COPY static ./static
COPY deploy ./deploy

EXPOSE 8080
CMD ["sh", "deploy/start.sh"]
