"""
Parameter validation and mapping utilities for OpenAI to Claude Code SDK conversion.
"""

import logging
from typing import Dict, Any
from src.models import ChatCompletionRequest
from src.constants import CLAUDE_MODELS

logger = logging.getLogger(__name__)


class ParameterValidator:
    """Validates and maps OpenAI Chat Completions parameters to Claude Code SDK options."""

    # Use models from constants (single source of truth)
    SUPPORTED_MODELS = set(CLAUDE_MODELS)

    # Valid permission modes for Claude Code SDK
    VALID_PERMISSION_MODES = {"default", "acceptEdits", "bypassPermissions", "plan"}

    # Upper bound for X-Claude-Max-Turns
    MAX_TURNS_LIMIT = 50

    @classmethod
    def validate_model(cls, model: str) -> bool:
        """Validate that the model is supported by Claude Code SDK."""
        if model not in cls.SUPPORTED_MODELS:
            logger.warning(
                f"Model '{model}' is not in the known supported models list. It will still be attempted but may fail. Supported models: {sorted(cls.SUPPORTED_MODELS)}"
            )
            # Return True anyway to allow graceful degradation
        return True

    @classmethod
    def extract_claude_headers(cls, headers: Dict[str, str]) -> Dict[str, Any]:
        """
        Extract Claude-Code-specific parameters from custom HTTP headers.

        This allows clients to pass SDK-specific options via headers:
        - X-Claude-Max-Turns: 5
        - X-Claude-Allowed-Tools: tool1,tool2,tool3
        - X-Claude-Permission-Mode: acceptEdits
        """
        claude_options = {}

        # Extract max_turns
        if "x-claude-max-turns" in headers:
            try:
                requested = int(headers["x-claude-max-turns"])
                # Bound agent loops so one header cannot burn through the quota.
                claude_options["max_turns"] = min(max(requested, 1), cls.MAX_TURNS_LIMIT)
            except ValueError:
                logger.warning(
                    f"Invalid X-Claude-Max-Turns header: {headers['x-claude-max-turns']}"
                )

        # Extract allowed tools
        if "x-claude-allowed-tools" in headers:
            tools = [tool.strip() for tool in headers["x-claude-allowed-tools"].split(",")]
            if tools:
                claude_options["allowed_tools"] = tools

        # Extract disallowed tools
        if "x-claude-disallowed-tools" in headers:
            tools = [tool.strip() for tool in headers["x-claude-disallowed-tools"].split(",")]
            if tools:
                claude_options["disallowed_tools"] = tools

        # Extract permission mode
        if "x-claude-permission-mode" in headers:
            claude_options["permission_mode"] = headers["x-claude-permission-mode"]

        # Extract max thinking tokens
        if "x-claude-max-thinking-tokens" in headers:
            try:
                claude_options["max_thinking_tokens"] = int(headers["x-claude-max-thinking-tokens"])
            except ValueError:
                logger.warning(
                    f"Invalid X-Claude-Max-Thinking-Tokens header: {headers['x-claude-max-thinking-tokens']}"
                )

        return claude_options


class CompatibilityReporter:
    """Reports on OpenAI API compatibility and suggests alternatives."""

    @classmethod
    def generate_compatibility_report(cls, request: ChatCompletionRequest) -> Dict[str, Any]:
        """Generate a detailed compatibility report for the request."""
        report = {
            "supported_parameters": [],
            "unsupported_parameters": [],
            "warnings": [],
            "suggestions": [],
        }

        # Check supported parameters
        if request.model:
            report["supported_parameters"].append("model")
        if request.messages:
            report["supported_parameters"].append("messages")
        if request.stream is not None:
            report["supported_parameters"].append("stream")
        if request.user:
            report["supported_parameters"].append("user (for logging)")

        # Check unsupported parameters with suggestions
        if request.temperature != 1.0:
            report["unsupported_parameters"].append("temperature")
            report["suggestions"].append(
                "Claude Code SDK does not support temperature control. Consider using different models for varied response styles (e.g., claude-haiku-4-5 for more focused responses)."
            )

        if request.top_p != 1.0:
            report["unsupported_parameters"].append("top_p")
            report["suggestions"].append(
                "Claude Code SDK does not support top_p. This parameter will be ignored."
            )

        if request.max_tokens:
            report["unsupported_parameters"].append("max_tokens")
            report["suggestions"].append(
                "Use max_turns parameter instead to limit conversation length, or use max_thinking_tokens to limit internal reasoning."
            )

        if request.n > 1:
            report["unsupported_parameters"].append("n")
            report["suggestions"].append(
                "Claude Code SDK only supports single responses (n=1). For multiple variations, make separate API calls."
            )

        if request.stop:
            report["unsupported_parameters"].append("stop")
            report["suggestions"].append(
                "Stop sequences are not supported. Consider post-processing responses or using max_turns to limit output."
            )

        if request.presence_penalty != 0 or request.frequency_penalty != 0:
            report["unsupported_parameters"].extend(["presence_penalty", "frequency_penalty"])
            report["suggestions"].append(
                "Penalty parameters are not supported. Consider using different system prompts to encourage varied responses."
            )

        if request.logit_bias:
            report["unsupported_parameters"].append("logit_bias")
            report["suggestions"].append(
                "Logit bias is not supported. Consider using system prompts to guide response style."
            )

        return report
