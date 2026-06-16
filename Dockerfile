FROM python:3.12-slim

WORKDIR /app

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p logs

# Nuclear cleanup at build time
RUN rm -f monitor/reporter.py monitor/trading_signal.py monitor/paper_trader.py 2>/dev/null || true
RUN find . -name "*.pyc" -delete 2>/dev/null || true
RUN find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# Make entrypoint executable
RUN chmod +x /app/docker-entrypoint.sh

ENV PORT=8080
EXPOSE 8080

# Entrypoint cleans old modules EVERY container start
ENTRYPOINT ["/app/docker-entrypoint.sh"]