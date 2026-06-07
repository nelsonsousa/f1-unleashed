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

    def emit(self, topic: str, data: Any, clock_time: datetime) -> None:
        """Emit a message to all handlers for the topic."""
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

    def clear(self) -> None:
        """Remove all handlers."""
        self._handlers.clear()

    @property
    def topic_count(self) -> int:
        """Number of subscribed topics (excluding wildcard)."""
        return len([t for t in self._handlers if t != '*'])
