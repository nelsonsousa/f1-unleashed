#!/bin/bash

# Formula 1 Live Timing API Service Script
# Usage: ./f1unleashed.sh {start|stop|restart|status}

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$APP_DIR/.server.pid"
LOG_FILE="$APP_DIR/server.log"
VENV_DIR="$APP_DIR/venv"

# Local per-checkout instance overrides (gitignored): PORT, F1_DATA_HOME, etc.
# `set -a` auto-exports sourced vars so F1_DATA_HOME reaches the app process.
if [ -f "$APP_DIR/instance.env" ]; then
    set -a; . "$APP_DIR/instance.env"; set +a
fi

HOST="${HOST:-0.0.0.0}"
# Exported so the app process (live session monitor) can address its own API.
export PORT="${PORT:-1950}"

start() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p "$PID" > /dev/null 2>&1; then
            echo "Server is already running (PID: $PID)"
            return 1
        else
            rm -f "$PID_FILE"
        fi
    fi

    echo "Starting server..."
    cd "$APP_DIR"
    source "$VENV_DIR/bin/activate"
    nohup "$VENV_DIR/bin/python" -m uvicorn app.main:app --host "$HOST" --port "$PORT" > "$LOG_FILE" 2>&1 &
    PID=$!
    echo $PID > "$PID_FILE"
    sleep 1

    if ps -p "$PID" > /dev/null 2>&1; then
        echo "Server started (PID: $PID)"
        echo "Listening on http://$HOST:$PORT"
        echo "Log file: $LOG_FILE"
    else
        echo "Failed to start server. Check $LOG_FILE for details."
        rm -f "$PID_FILE"
        return 1
    fi
}

stop() {
    if [ ! -f "$PID_FILE" ]; then
        echo "Server is not running (no PID file)"
        return 1
    fi

    PID=$(cat "$PID_FILE")
    if ! ps -p "$PID" > /dev/null 2>&1; then
        echo "Server is not running (stale PID file)"
        rm -f "$PID_FILE"
        return 1
    fi

    echo "Stopping server (PID: $PID)..."
    kill "$PID"

    # Wait for graceful shutdown
    for i in {1..10}; do
        if ! ps -p "$PID" > /dev/null 2>&1; then
            echo "Server stopped"
            rm -f "$PID_FILE"
            return 0
        fi
        sleep 1
    done

    # Force kill if still running
    echo "Force killing server..."
    kill -9 "$PID" 2>/dev/null
    rm -f "$PID_FILE"
    echo "Server stopped"
}

restart() {
    stop
    sleep 1
    start
}

status() {
    if [ ! -f "$PID_FILE" ]; then
        echo "Server is not running"
        return 1
    fi

    PID=$(cat "$PID_FILE")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "Server is running (PID: $PID)"
        echo "Listening on http://$HOST:$PORT"

        # Check if actually responding
        if command -v curl > /dev/null 2>&1; then
            if curl -s "http://localhost:$PORT/health" > /dev/null 2>&1; then
                echo "Health check: OK"
            else
                echo "Health check: Not responding"
            fi
        fi
        return 0
    else
        echo "Server is not running (stale PID file)"
        rm -f "$PID_FILE"
        return 1
    fi
}

case "$1" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        restart
        ;;
    status)
        status
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
