FROM python:3.12-slim

WORKDIR /app

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p logs

# Force remove old modules
RUN rm -f monitor/reporter.py monitor/trading_signal.py monitor/paper_trader.py
RUN find . -name "*.pyc" -delete

ENV PORT=8080
EXPOSE 8080

CMD ["python", "main.py"]
