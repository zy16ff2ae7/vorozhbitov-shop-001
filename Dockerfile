FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY bot.py catalog.json payments.py ./
COPY miniapp ./miniapp
RUN mkdir -p /app/data && useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080
CMD ["python", "bot.py"]
