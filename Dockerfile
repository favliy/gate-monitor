FROM python:3.12-slim

WORKDIR /app

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/ /etc/localtime && echo  > /etc/timezone

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p logs

ENV PORT=8080

EXPOSE 8080

# 20260615232459
CMD ["python", "main.py"]
