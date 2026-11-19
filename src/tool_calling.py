"""
OpenAI function calling (`tools` / `tool_choice`) on top of the Claude Agent SDK.

Claude Code cannot hand tool calls of the *client* back to the caller. So the request is
turned into a structured-output task: Claude must answer with JSON that matches a schema
built from the client's function definitions, and that JSON is translated into the
OpenAI `tool_calls` format. Claude's own built-in tools (Bash, Read, ...) stay disabled.
"""

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

ToolChoice = Union[str, Dict[str, Any], None]


class ToolCallingError(ValueError):
    """The tool definitions or tool_choice of a request are unusable."""


@dataclass
class ToolSpec:
    tools: List[Dict[str, Any]]
    forced_name: Optional[str]  # tool_choice names one function
    required: bool  # at least one tool call must be returned
    output_schema: Dict[str, Any]
    instructions: str

    @property
    def names(self) -> List[str]:
        return [t["function"]["name"] for t in self.tools]


def build_tool_spec(
    tools: Optional[List[Dict[str, Any]]], tool_choice: ToolChoice
) -> Optional[ToolSpec]:
    """Return the spec for a request, or None if the request does not use tools."""
    if not tools or tool_choice == "none":
        return None

    forced_name = None
    required = False
    if isinstance(tool_choice, dict):
        forced_name = (tool_choice.get("function") or {}).get("name")
        if not forced_name:
            raise ToolCallingError("tool_choice must name a function: {type, function: {name}}")
        required = True
    elif tool_choice == "required":
        required = True
    elif tool_choice not in (None, "auto"):
        raise ToolCallingError(f"Unsupported tool_choice: {tool_choice!r}")

    for tool in tools:
        if tool.get("type") != "function" or not (tool.get("function") or {}).get("name"):
            raise ToolCallingError("Only tools of type 'function' with a name are supported")

    selected = [t for t in tools if t["function"]["name"] == forced_name] if forced_name else tools
    if not selected:
        raise ToolCallingError(f"tool_choice names unknown function '{forced_name}'")

    return ToolSpec(
        tools=selected,
        forced_name=forced_name,
        required=required,
        output_schema=_build_output_schema(selected, required, single=bool(forced_name)),
        instructions=_build_instructions(selected, required),
    )


def _build_output_schema(
    tools: List[Dict[str, Any]], required: bool, single: bool
) -> Dict[str, Any]:
    variants = []
    root_defs: Dict[str, Dict[str, Any]] = {"$defs": {}, "definitions": {}}
    for tool in tools:
        function = tool["function"]
        params = json.loads(json.dumps(function.get("parameters") or {"type": "object"}))
        # "$ref": "#/$defs/X" is resolved from the schema root, so shared definitions of
        # every function move to the root of the combined schema.
        for key in ("$defs", "definitions"):
            root_defs[key].update(params.pop(key, {}))
        variants.append(
            {
                "type": "object",
                "properties": {"name": {"const": function["name"]}, "arguments": params},
                "required": ["name", "arguments"],
            }
        )

    items = variants[0] if len(variants) == 1 else {"anyOf": variants}
    calls: Dict[str, Any] = {"type": "array", "items": items}
    if required:
        calls["minItems"] = 1
    if single:
        calls["maxItems"] = 1

    schema: Dict[str, Any] = {
        "type": "object",
        "properties": {"tool_calls": calls},
        "required": ["tool_calls"] if required else [],
    }
    if not required:
        schema["properties"]["content"] = {"type": "string"}
    for key, value in root_defs.items():
        if value:
            schema[key] = value
    return schema


def _build_instructions(tools: List[Dict[str, Any]], required: bool) -> str:
    lines = [
        "You have access to the following functions. You cannot run them yourself: to use",
        "one, put a call into your structured answer; the caller executes it and sends",
        "the result back as a message with role 'tool'.",
        "Deliver your final answer ONLY by calling the StructuredOutput tool with the",
        "object described below. Do not write the answer as plain text.",
        "",
    ]
    for tool in tools:
        function = tool["function"]
        lines.append(f"- {function['name']}: {function.get('description', '').strip()}")
    lines.append("")
    if required:
        lines.append('Answer with {"tool_calls": [{"name": ..., "arguments": {...}}]}.')
    else:
        lines.append(
            'To call functions answer with {"tool_calls": [{"name": ..., "arguments": {...}}]}. '
            'If no function is needed, answer with {"content": "<your reply>"} instead.'
        )
    lines.append("The arguments must follow the function's parameter schema exactly.")
    return "\n".join(lines)


def _extract_json(text: str) -> Any:
    """Parse JSON from a model reply, also when wrapped in a code fence or prose."""
    text = text.strip()
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def parse_response(
    structured_output: Any,
    text: Optional[str],
    spec: ToolSpec,
    assistant_texts: Optional[List[str]] = None,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Translate Claude's answer into (content, tool_calls) of an OpenAI message.

    Uses the SDK's structured output if present, otherwise tries to read JSON from the
    text. Plain text without any call is returned as content when calls are optional.
    """
    data = structured_output if isinstance(structured_output, dict) else None
    if data is None:
        # Newest first: the model may have written the JSON as plain text instead of
        # calling the StructuredOutput tool (the CLI then asks again and may give up).
        for candidate in [*reversed(assistant_texts or []), text or ""]:
            parsed = _extract_json(candidate) if candidate else None
            if isinstance(parsed, dict) and ("tool_calls" in parsed or "content" in parsed):
                data = parsed
                break

    raw_calls = (data or {}).get("tool_calls") or []
    tool_calls = []
    for call in raw_calls:
        name = call.get("name") if isinstance(call, dict) else None
        if name not in spec.names:
            raise ToolCallingError(f"Claude called an unknown function: {name!r}")
        arguments = call.get("arguments", {})
        tool_calls.append(
            {
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
                },
            }
        )

    if tool_calls:
        return None, tool_calls
    if spec.required:
        raise ToolCallingError("Claude returned no tool call although one is required")
    content = (data or {}).get("content") if data else None
    return (content if isinstance(content, str) else text or ""), []
