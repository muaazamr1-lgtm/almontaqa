FROM python:3.12-slim
WORKDIR /app
COPY server.py stats.py index.html requirements.txt favicon.ico icon.svg favicon-96x96.png apple-touch-icon.png apple-touch-icon-180.png icon-192.png icon-512.png og.png site.webmanifest ./
RUN pip install --no-cache-dir -r requirements.txt
ENV PORT=8080 DB_PATH=/data/almontaqa.sqlite3
EXPOSE 8080
CMD ["python", "server.py"]
