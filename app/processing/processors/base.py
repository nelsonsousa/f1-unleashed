"""
Base processor interface.

Each processor subscribes to specific raw F1 topics on the SessionMessageBus,
maintains its own state, and emits display-ready messages back to the bus.
"""

from abc import ABC, abstractmethod
from typing import Any

from app.processing.message_bus import SessionMessageBus


class Processor(ABC):
    """Base class for all timing data processors."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        self._bus = bus
        self._session_type = session_type
        self._skip_animations = False

    @property
    def skip_animations(self) -> bool:
        """When True, suppress transient visual state (e.g. highlights)."""
        return self._skip_animations

    @skip_animations.setter
    def skip_animations(self, value: bool) -> None:
        self._skip_animations = value

    @abstractmethod
    def subscribe(self) -> None:
        """Subscribe to raw topics on the message bus.

        Called once during initialization. Subclasses should call
        self._bus.on('TopicName', self._handle_topic) here.
        """

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return current state for snapshot storage."""

    @abstractmethod
    def restore(self, state: dict[str, Any]) -> None:
        """Restore state from a snapshot."""

    @abstractmethod
    def reset(self) -> None:
        """Reset to initial state (for backward seeks past all snapshots)."""
