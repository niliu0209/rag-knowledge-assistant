FROM python:3.12-slim

WORKDIR /srv/app

# 依赖先拷贝，利用构建缓存
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# 应用代码（.dockerignore 排除 dev-docs/data/.env/.venv 等）
COPY app ./app
COPY ui ./ui
COPY ui_entrypoint.sh /usr/local/bin/ui_entrypoint.sh
RUN chmod +x /usr/local/bin/ui_entrypoint.sh

EXPOSE 8000 8501

# 默认启动 API；ui 容器由 compose 覆盖 command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
