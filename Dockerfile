FROM python:3.12-slim

WORKDIR /srv/app

# pip 镜像源构建参数（本机网络受限时：--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple）
ARG PIP_INDEX_URL=https://pypi.org/simple

# 代码先拷贝，再 pip install .（setuptools 需要真实目录结构才能发现包；
# 顺序反了会装成空包，app/ui 不进 site-packages，运行时依赖 cwd 的隐式行为）
COPY pyproject.toml ./
COPY app ./app
COPY ui ./ui
COPY ui_entrypoint.sh /usr/local/bin/ui_entrypoint.sh
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" . \
    && chmod +x /usr/local/bin/ui_entrypoint.sh

EXPOSE 8000 8501

# 默认启动 API；ui 容器由 compose 覆盖 command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
