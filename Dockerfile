FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY bot.py catalog.json payments.py payment_store.py commerce_store.py commerce_bot.py operations_store.py operations_bot.py finance_store.py finance_bot.py discovery_store.py discovery_bot.py growth_store.py growth_bot.py alerts_store.py alerts_bot.py ./
COPY miniapp ./miniapp
RUN mkdir -p /app/data && useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080
CMD ["python", "bot.py"]
