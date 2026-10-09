FROM python:3.13-alpine

WORKDIR /app
COPY server.py docs.html ./
# Writable place for an optional DATA_FILE; a named volume mounted here inherits the owner.
RUN mkdir /data && chown nobody /data

ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
USER nobody

HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ['PORT'])"

CMD ["python", "server.py"]
