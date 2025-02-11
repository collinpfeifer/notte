import base64
import json
from dataclasses import dataclass, field
from typing import TypeVar

import tiktoken
from litellm import (
    AllMessageValues,
    ChatCompletionAssistantMessage,
    ChatCompletionAssistantToolCall,
    ChatCompletionImageObject,
    ChatCompletionSystemMessage,
    ChatCompletionTextObject,
    ChatCompletionToolMessage,
    ChatCompletionUserMessage,
    ModelResponse,
    OpenAIMessageContent,
)
from pydantic import BaseModel

from notte.errors.llm import LLMParsingError
from notte.llms.engine import StructuredContent

# Define valid message roles


@dataclass
class CachedMessage:
    """Message with cached token count"""

    message: AllMessageValues
    token_count: int


T = TypeVar("T", bound=BaseModel)


@dataclass
class Conversation:
    """Manages conversation history and message extraction"""

    history: list[CachedMessage] = field(default_factory=list)
    json_extractor: StructuredContent = field(default_factory=lambda: StructuredContent(inner_tag="json"))
    autosize: bool = False
    max_tokens: int = 64000
    encoding: tiktoken.Encoding = tiktoken.get_encoding("cl100k_base")
    _total_tokens: int = field(default=0, init=False)
    convert_tools_to_assistant: bool = False

    def count_tokens(self, content: OpenAIMessageContent) -> int:
        """Count the number of tokens in a text string"""
        content_str = json.dumps(content)
        return len(self.encoding.encode(content_str))

    def total_tokens(self) -> int:
        """Get total tokens in conversation history"""
        return self._total_tokens

    def would_exceed_token_limit(self, content: str) -> bool:
        """Check if adding content would exceed token limit"""
        if not self.autosize:
            return False
        return (self._total_tokens + self.count_tokens(content)) > self.max_tokens

    def trim_history_to_fit(self, new_content: OpenAIMessageContent) -> None:
        """Trim history to make room for new content while preserving system messages"""
        if not self.autosize:
            return

        # Always keep system messages
        system_messages = [msg for msg in self.history if msg.message["role"] == "system"]
        other_messages = [msg for msg in self.history if msg.message["role"] != "system"]

        new_content_tokens = self.count_tokens(new_content)
        system_tokens = sum(msg.token_count for msg in system_messages)
        available_tokens = self.max_tokens - system_tokens - new_content_tokens

        # Remove oldest non-system messages until we have room
        current_tokens = sum(msg.token_count for msg in other_messages)
        while other_messages and current_tokens > available_tokens:
            removed = other_messages.pop(0)
            current_tokens -= removed.token_count

        self.history = system_messages + other_messages
        self._total_tokens = sum(msg.token_count for msg in self.history)

    def _add_message(self, msg: AllMessageValues) -> None:
        """Internal helper to add a message with token counting"""
        content: OpenAIMessageContent = msg["content"]  # type: ignore
        token_count = self.count_tokens(content)
        if self.autosize:
            self.trim_history_to_fit(content)
        cached_msg = CachedMessage(message=msg, token_count=token_count)
        self.history.append(cached_msg)
        self._total_tokens += token_count

    def add_system_message(self, content: str) -> None:
        """Add a system message to the conversation"""
        self._add_message(ChatCompletionSystemMessage(role="system", content=content))

    def add_user_message(self, content: OpenAIMessageContent, image: bytes | None = None) -> OpenAIMessageContent:
        """Add a user message to the conversation"""
        _content: OpenAIMessageContent = content
        if image is not None and isinstance(content, str):
            image_str = base64.b64encode(image).decode("utf-8")
            _content = [
                ChatCompletionTextObject(type="text", text=content),
                ChatCompletionImageObject(type="image_url", image_url={"url": f"data:image/png;base64,{image_str}"}),
            ]
        self._add_message(ChatCompletionUserMessage(role="user", content=_content))
        return _content

    def add_assistant_message(self, content: str) -> None:
        """Add an assistant message to the conversation"""
        self._add_message(ChatCompletionAssistantMessage(role="assistant", content=content))

    def add_tool_message(self, parsed_content: BaseModel, tool_id: str) -> None:
        """Add a tool message to the conversation"""
        content: str = str(parsed_content.model_dump(mode="json", exclude_unset=True))  # type: ignore
        if not self.convert_tools_to_assistant:
            self._add_message(
                ChatCompletionToolMessage(
                    role="tool",
                    content=content,
                    tool_call_id=tool_id,
                )
            )
        else:
            # Optional, convert tools to assistant role
            self._add_message(
                ChatCompletionAssistantMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ChatCompletionAssistantToolCall(
                            id=tool_id,
                            type="function",
                            function={
                                "arguments": content,
                                "name": parsed_content.__class__.__name__,
                            },
                        )
                    ],
                )
            )

    def parse_structured_response(self, response: ModelResponse | str, model: type[T]) -> T:
        """Parse a structured response from the LLM into a Pydantic model

        Args:
            response: The LLM model response
            model: The Pydantic model class to parse into

        Returns:
            Instance of the specified Pydantic model

        Raises:
            LLMParsingError: If response cannot be parsed into the model
        """
        if isinstance(response, str):
            return model.model_validate(response)
        if not response.choices:
            raise LLMParsingError("No choices in LLM response")

        choice = response.choices[0]
        # Extract content from either streaming or non-streaming response
        content = None
        if isinstance(choice, dict):
            message = choice.get("message", {})
            if isinstance(message, dict):
                content = message.get("content")
        else:
            content = getattr(choice, "text", None)

        if not content:
            raise LLMParsingError("No content in LLM response message")

        try:
            extracted = self.json_extractor.extract(content)
            return model.model_validate_json(extracted)
        except (json.JSONDecodeError, ValueError) as e:
            raise LLMParsingError(f"Failed to parse response into {model.__name__}: {str(e)}")

    def messages(self) -> list[AllMessageValues]:
        """Get messages in LiteLLM format

        Returns:
            List of messages formatted for LiteLLM

        Note:
            This converts our internal message format to litellm's format.
            litellm only supports 'assistant' role, so we map all roles to that.
        """
        return [msg.message for msg in self.history]

    def reset(self) -> None:
        """Clear all messages from the conversation"""
        self.history.clear()
        self._total_tokens = 0
