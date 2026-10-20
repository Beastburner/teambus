ARG BASE=python:3.13-slim
FROM ${BASE}
ENV SANDBOX_DIR=/tmp/sandboxes PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8765 TEAMBUS_DB=/data/teambus.db TEAMBUS_TOKENS=/data/tokens.json
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py sandbox.py ./
COPY web ./web
RUN useradd -r -u 10001 teambus && mkdir /data && chown teambus /data
USER teambus
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ['PORT'])"
CMD ["python", "server.py"]
