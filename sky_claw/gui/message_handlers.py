"""Strategy pattern handlers for GUI queue messages."""

from __future__ import annotations
import abc
from typing import Any


class MessageHandlerStrategy(abc.ABC):
    @abc.abstractmethod
    def handle(self, gui: Any, data: Any) -> None:
        pass


class ResponseHandler(MessageHandlerStrategy):
    def handle(self, gui: Any, data: Any) -> None:
        gui.append_chat_message(str(data), is_user=False)


class ModlistHandler(MessageHandlerStrategy):
    def handle(self, gui: Any, data: Any) -> None:
        gui.update_mod_list(data)


class SuccessHandler(MessageHandlerStrategy):
    def handle(self, gui: Any, data: Any) -> None:
        gui.append_chat_message(str(data), is_user=False, style="success")


class ErrorHandler(MessageHandlerStrategy):
    def handle(self, gui: Any, data: Any) -> None:
        gui.append_chat_message(str(data), is_user=False, style="error")
