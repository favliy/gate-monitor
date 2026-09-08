#!/bin/bash
# Gate.io Futures Monitor - 24/7 Runner
# Usage: bash run.sh [start|stop|restart|status]

DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DIR/monitor.pid"
LOG_FILE="$DIR/logs/monitor.log"

start() {
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        echo "Monitor is already running (PID: $(cat $PID_FILE))"
        return 1
    fi

    # Ensure Python and deps
    if ! command -v python3 &>/dev/null; then
        echo "ERROR: python3 not found. Install: apt install python3 python3-pip"
        return 1
    fi

    pip3 install -r "$DIR/requirements.txt" -q 2>/dev/null

    mkdir -p "$DIR/logs"

    echo "Starting Gate.io Futures Monitor..."
    cd "$DIR"
    nohup python3 main.py >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Started (PID: $!)"
}

stop() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 $PID 2>/dev/null; then
            kill $PID
            echo "Stopped (PID: $PID)"
        fi
        rm -f "$PID_FILE"
    else
        echo "Monitor not running"
    fi
}

status() {
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        echo "Running (PID: $(cat $PID_FILE))"
        echo "Recent logs:"
        tail -5 "$LOG_FILE" 2>/dev/null
    else
        echo "Not running"
    fi
}

case "${1:-start}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; sleep 1; start ;;
    status)  status ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" ;;
esac
