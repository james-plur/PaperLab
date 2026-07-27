FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
WORKDIR /app
RUN python -m venv /opt/venv
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[backup]"
COPY topics.example.yaml ./topics.example.yaml
EXPOSE 8765
CMD ["paperlab-publish", "--config", "/app/topics.yaml", "web", "serve", "--host", "0.0.0.0", "--port", "8765"]
