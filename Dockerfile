FROM python:3.12-slim

WORKDIR /app

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p logs

# Force clean old modules at build
RUN rm -f monitor/reporter.py monitor/trading_signal.py monitor/paper_trader.py 2>/dev/null; find . -name "*.pyc" -delete; find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; echo "build clean done"

ENV PORT=8080
EXPOSE 8080

# Start: clean first, then run
CMD rm -f /app/monitor/reporter.py /app/monitor/trading_signal.py /app/monitor/paper_trader.py 2>/dev/null; find /app -name "*.pyc" -delete 2>/dev/null; find /app -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; echo "[startup] old modules cleaned"; exec python /app/main.py