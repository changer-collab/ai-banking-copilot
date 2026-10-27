FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# 国内服务器直连 pypi.org 常超时；构建时可用镜像源：
#   PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple docker compose build
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} PIP_DEFAULT_TIMEOUT=60
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir -e .
COPY tests ./tests
COPY docs ./docs
COPY deploy ./deploy
EXPOSE 8000
CMD ["python", "-m", "ai_banking_copilot"]
