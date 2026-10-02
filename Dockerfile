FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir -e .
COPY tests ./tests
COPY docs ./docs
COPY deploy ./deploy
EXPOSE 8000
CMD ["python", "-m", "ai_banking_copilot"]
