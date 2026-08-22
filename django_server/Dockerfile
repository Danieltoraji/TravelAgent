# TravelAgent Django 单用户 Demo 镜像
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 安装依赖
COPY requirements.txt /tmp/requirements.txt
COPY django_server/requirements.txt /tmp/django-requirements.txt
RUN pip install --no-cache-dir -r /tmp/django-requirements.txt -r /tmp/requirements.txt

# 复制 B 侧核心代码 + Django 工程
COPY . /app

WORKDIR /app/django_server

EXPOSE 8000

CMD ["gunicorn", "travelagent.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120"]
