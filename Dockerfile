FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY smalltimetrader ./smalltimetrader
RUN pip install --no-cache-dir . && useradd --create-home trader && mkdir -p /app/data && chown trader:trader /app/data
USER trader
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "smalltimetrader.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
