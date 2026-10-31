FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATABASE_PATH=/data/wealth.db PORT=8080
WORKDIR /srv
RUN groupadd --gid 10001 wealth && useradd --uid 10001 --gid wealth --no-create-home wealth && mkdir /data && chown wealth:wealth /data
COPY --chown=wealth:wealth app /srv/app
USER wealth
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)"
CMD ["python", "-m", "app.server"]
