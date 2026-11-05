FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 todo \
    && mkdir -p /app/database /app/backups \
    && chown -R todo:todo /app

COPY deploy/entrypoint.sh /usr/local/bin/todo-entrypoint
RUN chmod 0755 /usr/local/bin/todo-entrypoint

USER todo

RUN python -m compileall -q app.py cli.py database graph harness legacy ops prompts tools

ENTRYPOINT ["/usr/local/bin/todo-entrypoint"]
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
