"""
In-process pub/sub message bus for a single session.

Same pattern as the JS messageBus: handlers receive (data, clock_time).
"""

import logging
from datetime import datetime
from typing import Any, Callable

logger = logging.getLogger(__name__)

Handler = Callable[[Any, datetime], None]


class SessionMessageBus:
    """In-process pub/sub for a single session."""

    def __init__(self):
        self._handlers: dict[str, list[Handler]] = {}
        self._persist_sink: Callable[[str, Any, datetime], None] | None = None

    def set_persist_sink(self, sink: Callable[[str, Any, datetime], None]) -> None:
        """Register the single sink that persists emits to the DB. It is
        invoked (topic, data, clock_time) for every emit with persist=True."""
        self._persist_sink = sink

    def on(self, topic: str, handler: Handler) -> None:
        """Subscribe to a topic."""
        if topic not in self._handlers:
            self._handlers[topic] = []
        self._handlers[topic].append(handler)

    def off(self, topic: str, handler: Handler) -> None:
        """Unsubscribe from a topic."""
        if topic in self._handlers:
            try:
                self._handlers[topic].remove(handler)
            except ValueError:
                pass

    def emit(self, topic: str, data: Any, clock_time: datetime,
             persist: bool = True) -> None:
        """Emit a message to all handlers for the topic.

        persist=False emits to subscribers as normal but skips the DB persist
        sink — for high-rate, live-only topics (e.g. liveTelemetry) that the
        client consumes live and never needs to replay/rebuild on seek.
        """
        if topic in self._handlers:
            for handler in self._handlers[topic]:
                try:
                    handler(data, clock_time)
                except Exception:
                    logger.exception(f"Error in handler for topic '{topic}'")

        # Wildcard subscribers get (topic, data, clock_time)
        if '*' in self._handlers:
            for handler in self._handlers['*']:
                try:
                    handler(topic, data, clock_time)
                except Exception:
                    logger.exception(f"Error in wildcard handler for topic '{topic}'")

        if persist and self._persist_sink is not None:
            self._persist_sink(topic, data, clock_time)

    def has_subscriber(self, topic: str) -> bool:
        """True if a specific (non-wildcard) handler is registered for topic.

        Used for topic discovery: a raw F1 topic with no specific subscriber
        is captured but not processed by any processor.
        """
        return bool(self._handlers.get(topic))

    def clear(self) -> None:
        """Remove all handlers."""
        self._handlers.clear()

    @property
    def topic_count(self) -> int:
        """Number of subscribed topics (excluding wildcard)."""
        return len([t for t in self._handlers if t != '*'])
