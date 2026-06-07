"""
F1 Live Timing SignalR Client.

Connects to F1's live timing SignalR service to receive real-time
timing data during sessions. Data is cached to disk and can be
forwarded to subscribers.
"""

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import requests
from signalrcore.hub_connection_builder import HubConnectionBuilder
from signalrcore.messages.completion_message import CompletionMessage

logger = logging.getLogger(__name__)


# SignalR endpoints
SIGNALR_CONNECTION_URL = "wss://livetiming.formula1.com/signalrcore"
SIGNALR_NEGOTIATE_URL = "https://livetiming.formula1.com/signalrcore/negotiate"

# Topics to subscribe to
LIVE_TOPICS = [
    "Heartbeat",
    "AudioStreams",
    "ContentStreams",
    "DriverList",
    "ExtrapolatedClock",
    "RaceControlMessages",
    "SessionInfo",
    "SessionData",
    "SessionStatus",
    "TeamRadio",
    "TimingAppData",
    "TimingData",
    "TimingDataF1",
    "TimingStats",
    "TrackStatus",
    "WeatherData",
    "WeatherDataSeries",
    "Position.z",
    "CarData.z",
    "TopThree",
    "LapCount",
    "ChampionshipPrediction",
    "PitLaneTimeCollection",
    "PitStopSeries",
    "PitStop",
    "CurrentTyres",
    "TyreStintSeries",
    "LapSeries",
    "DriverTracker",
    "DriverRaceInfo",
    "OvertakeSeries",
    "ArchiveStatus",
    "TlaRcm",
    "RcmSeries",
]


class F1SignalRClient:
    """
    SignalR client for F1 live timing data.

    This client connects to F1's SignalR service and:
    - Receives real-time timing messages
    - Caches messages to disk (live.jsonl format)
    - Forwards messages to registered callbacks

    The client runs in a separate thread since signalrcore is not async-native.
    """

    def __init__(
        self,
        cache_path: Path,
        message_callback: Optional[Callable[[dict], None]] = None,
        no_auth: bool = False,
        timeout: int = 120,
    ):
        """
        Initialize the SignalR client.

        Args:
            cache_path: Directory to store cached data
            message_callback: Optional callback for received messages
            no_auth: If True, connect without F1 authentication (limited data)
            timeout: Seconds without data before auto-disconnect
        """
        self.cache_path = Path(cache_path)
        self.cache_path.mkdir(parents=True, exist_ok=True)

        self._message_callback = message_callback
        self._no_auth = no_auth
        self._timeout = timeout

        self._connection: Optional[Any] = None
        self._is_connected = False
        self._is_running = False
        self._thread: Optional[threading.Thread] = None

        self._output_file = None
        self._subscribe_data: dict[str, Any] = {}
        self._session_start: Optional[datetime] = None
        self._t_last_message: float = 0
        self._message_count = 0

        # For async communication
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._message_queue: Optional[asyncio.Queue] = None

    def _get_auth_token(self) -> Optional[str]:
        """Get authentication token from f1auth.json."""
        from app.services.auth_service import auth_service
        token = auth_service._load_token()
        if token:
            logger.info("Using F1 auth token for SignalR connection")
        else:
            logger.warning("No F1 auth token found - CarData.z and Position.z will not be available")
        return token

    def _on_message(self, msg: list | CompletionMessage):
        """Handle incoming SignalR messages."""
        self._t_last_message = time.time()
        timestamp = datetime.utcnow()

        if self._session_start is None:
            self._session_start = timestamp

        try:
            if isinstance(msg, CompletionMessage):
                # Initial subscription response with current state
                for key, value in msg.result.items():
                    self._subscribe_data[key] = value
                    self._process_message(key, value, timestamp)

                # Write subscribe.json immediately so replay clients can use it
                subscribe_file = self.cache_path / "subscribe.json"
                try:
                    with open(subscribe_file, "w", encoding="utf-8") as f:
                        json.dump(self._subscribe_data, f, indent=2)
                except Exception as e:
                    logger.error(f"Failed to write subscribe.json: {e}")

            elif isinstance(msg, list):
                # Regular message: [topic, data, extra?]
                if len(msg) >= 2:
                    topic = msg[0]
                    data = msg[1]

                    # Parse data if it's a string
                    if isinstance(data, str):
                        try:
                            data = json.loads(data)
                        except json.JSONDecodeError:
                            pass  # Keep as string

                    self._process_message(topic, data, timestamp)

            else:
                logger.warning(f"Unknown message type: {type(msg)}")

        except Exception as e:
            logger.error(f"Error processing message: {e}")

    def _process_message(self, topic: str, data: Any, timestamp: datetime):
        """Process and store a single message."""
        offset = timestamp - self._session_start if self._session_start else timedelta(0)

        message = {
            "Type": topic,
            "Json": data,
            "DateTime": timestamp.isoformat(),
        }

        # Write to cache file
        if self._output_file:
            try:
                self._output_file.write(json.dumps(message) + "\n")
                self._output_file.flush()
            except Exception as e:
                logger.error(f"Error writing to cache: {e}")

        self._message_count += 1

        # Call the message callback
        if self._message_callback:
            try:
                self._message_callback({
                    "topic": topic,
                    "data": data,
                    "timestamp": timestamp,
                    "offset": offset,
                })
            except Exception as e:
                logger.error(f"Error in message callback: {e}")

        # Put in async queue if available
        if self._message_queue and self._loop:
            try:
                self._loop.call_soon_threadsafe(
                    self._message_queue.put_nowait,
                    {
                        "type": "timing",
                        "topic": topic,
                        "data": data,
                        "timestamp": timestamp.isoformat(),
                        "offset": offset.total_seconds(),
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to queue message: {e}")

    def _on_connect(self):
        """Handle connection established."""
        self._is_connected = True
        logger.info("SignalR connection established")

        if self._message_queue and self._loop:
            self._loop.call_soon_threadsafe(
                self._message_queue.put_nowait,
                {"type": "status", "status": "connected"}
            )

    def _on_close(self):
        """Handle connection closed."""
        self._is_connected = False
        logger.info("SignalR connection closed")

        if self._message_queue and self._loop:
            self._loop.call_soon_threadsafe(
                self._message_queue.put_nowait,
                {"type": "status", "status": "disconnected"}
            )

    def _on_error(self, error):
        """Handle connection error."""
        logger.error(f"SignalR connection error: {error}")

        if self._message_queue and self._loop:
            self._loop.call_soon_threadsafe(
                self._message_queue.put_nowait,
                {"type": "error", "message": str(error)}
            )

    def _run_connection(self):
        """Run the SignalR connection (in a separate thread)."""
        headers = {}

        try:
            # Pre-negotiate to get AWSALBCORS cookie
            logger.info("Pre-negotiating SignalR connection...")
            r = requests.options(SIGNALR_NEGOTIATE_URL, headers=headers, timeout=30)
            if "AWSALBCORS" in r.cookies:
                headers["Cookie"] = f"AWSALBCORS={r.cookies['AWSALBCORS']}"
                logger.info("Got AWSALBCORS cookie")
        except Exception as e:
            logger.warning(f"Pre-negotiate failed: {e}")

        # Configure connection options
        options = {
            "verify_ssl": True,
            "headers": headers,
        }

        # Add auth token factory if authentication is enabled
        if not self._no_auth:
            options["access_token_factory"] = self._get_auth_token

        # Build connection
        logger.info("Building SignalR connection...")
        self._connection = HubConnectionBuilder() \
            .with_url(SIGNALR_CONNECTION_URL, options=options) \
            .configure_logging(logging.WARNING) \
            .build()

        self._connection.on_open(self._on_connect)
        self._connection.on_close(self._on_close)
        self._connection.on_error(self._on_error)
        self._connection.on("feed", self._on_message)

        # Open cache file
        live_file = self.cache_path / "live.jsonl"
        self._output_file = open(live_file, "a", encoding="utf-8")

        # Start connection
        logger.info("Starting SignalR connection...")
        self._connection.start()

        # Wait for connection
        timeout_count = 0
        while not self._is_connected and timeout_count < 100:
            time.sleep(0.1)
            timeout_count += 1

        if not self._is_connected:
            raise ConnectionError("Failed to establish SignalR connection")

        # Subscribe to topics
        logger.info(f"Subscribing to {len(LIVE_TOPICS)} topics...")
        self._connection.send(
            "Subscribe",
            [LIVE_TOPICS],
            on_invocation=self._on_message
        )

        self._t_last_message = time.time()

        # Monitor connection
        while self._is_running and self._is_connected:
            time.sleep(1)

            # Check for timeout
            if self._timeout > 0 and time.time() - self._t_last_message > self._timeout:
                logger.warning(f"No data for {self._timeout}s, disconnecting...")
                break

        # Cleanup
        self._cleanup()

    def _cleanup(self):
        """Clean up SignalR connection resources."""
        if self._connection:
            try:
                self._connection.stop()
            except Exception:
                pass
            self._connection = None

        # Write end marker before closing the file
        if self._output_file:
            try:
                end_marker = {
                    "Type": "_SessionEnd",
                    "DateTime": datetime.utcnow().isoformat(),
                    "Json": {"MessageCount": self._message_count},
                }
                self._output_file.write(json.dumps(end_marker) + "\n")
                self._output_file.flush()
            except Exception as e:
                logger.warning(f"Failed to write end marker: {e}")
            try:
                self._output_file.close()
            except Exception:
                pass
            self._output_file = None

        # Save subscribe data
        if self._subscribe_data:
            subscribe_file = self.cache_path / "subscribe.json"
            try:
                with open(subscribe_file, "w", encoding="utf-8") as f:
                    json.dump(self._subscribe_data, f, indent=2)
                logger.info(f"Saved subscribe data to {subscribe_file}")
            except Exception as e:
                logger.error(f"Failed to save subscribe data: {e}")

        self._is_connected = False
        logger.info(f"SignalR client stopped. Received {self._message_count} messages.")

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> asyncio.Queue:
        """
        Start the SignalR client.

        Args:
            loop: Optional asyncio event loop for async message delivery

        Returns:
            asyncio.Queue that will receive messages
        """
        if self._is_running:
            raise RuntimeError("Client is already running")

        self._is_running = True
        self._loop = loop or asyncio.get_event_loop()
        self._message_queue = asyncio.Queue()

        self._thread = threading.Thread(target=self._run_connection, daemon=True)
        self._thread.start()

        return self._message_queue

    def stop(self):
        """Stop the SignalR client."""
        self._is_running = False

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    @property
    def is_connected(self) -> bool:
        """Check if currently connected."""
        return self._is_connected

    @property
    def message_count(self) -> int:
        """Get the number of messages received."""
        return self._message_count

    @property
    def subscribe_data(self) -> dict[str, Any]:
        """Get the subscription data (initial state)."""
        return self._subscribe_data
