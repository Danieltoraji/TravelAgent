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

# workers 必须为 1：每用户运行时注册表与 sqlite 都按单进程设计，
# 多个 gunicorn worker = 会话串号/分裂状态（原单用户单例同此约束）。
# threads=8（gthread）：一个用户的 20-70s 规划不再阻塞其他用户，
# 同一用户的并发写由各自 runtime.lock 串行化（多用户改造 2026-09）。
# 启动前 migrate（多用户表：AuthToken/Trip）。
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn travelagent.wsgi:application --bind 0.0.0.0:8000 --workers 1 --threads 8 --timeout 120"]
