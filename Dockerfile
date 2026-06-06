FROM python:3.12-slim

WORKDIR /app

# 使用阿里云 Debian 镜像（国内加速）
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# 使用清华 PyPI 镜像
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --trusted-host pypi.tuna.tsinghua.edu.cn \
    -r requirements.txt

COPY mini_agent.py .

# 创建数据目录
RUN mkdir -p data/sessions data/checker/references

EXPOSE 5000

# 默认启动 Web 服务
CMD ["python", "mini_agent.py"]
