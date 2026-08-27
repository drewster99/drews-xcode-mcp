#!/usr/bin/env python3
"""Publish each tool's parameter documentation into its MCP input schema.

Every tool documents its parameters in a Google-style ``Args:`` block, but
FastMCP copies only the whole docstring into the tool description; the generated
JSON Schema gets nothing per parameter beyond an auto-derived title such as
"Udid". A caller reading the structured schema therefore cannot tell what an
argument means, which invites guessed or invented argument names.

Restating each description in a ``Field(description=...)`` would fix the schema
but leave two copies of the same sentence to drift apart. This module keeps the
docstring as the single source and copies it into the schema at import time.
"""

import inspect
import re
from typing import Annotated, Any, Callable, get_args, get_origin

from pydantic import Field
from pydantic.fields import FieldInfo

from drews_xcode_mcp.exceptions import XCodeMCPError

_ARGS_SECTION_HEADING = "Args:"
_PARAMETER_LINE = re.compile(r"^(\w+)\s*:\s*(.*)$")


def parse_documented_parameters(docstring: str) -> dict[str, str]:
    """
    Extract parameter descriptions from a Google-style ``Args:`` block.

    Continuation lines (indented further than the parameter name) are folded into
    the preceding description. The block ends at the first line indented less than
    the parameter names, which is how ``Returns:`` and ``Raises:`` terminate it
    regardless of the indentation style a given docstring uses.

    Args:
        docstring: A docstring already normalized by `inspect.getdoc`.

    Returns:
        Parameter name mapped to its description, empty if there is no Args block.
    """
    if not docstring:
        return {}

    lines = docstring.splitlines()
    heading_index = next(
        (i for i, line in enumerate(lines) if line.strip() == _ARGS_SECTION_HEADING),
        None,
    )
    if heading_index is None:
        return {}

    body = lines[heading_index + 1:]
    parameter_indent = next(
        (len(line) - len(line.lstrip()) for line in body if line.strip()),
        None,
    )
    if parameter_indent is None:
        return {}

    description_parts: dict[str, list[str]] = {}
    current_parameter = None

    for line in body:
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip())
        if indent < parameter_indent:
            break

        if indent == parameter_indent:
            match = _PARAMETER_LINE.match(line.strip())
            if not match:
                break
            current_parameter = match.group(1)
            description_parts[current_parameter] = [match.group(2).strip()]
        elif current_parameter is not None:
            description_parts[current_parameter].append(line.strip())

    return {
        name: " ".join(part for part in parts if part).strip()
        for name, parts in description_parts.items()
    }


def describe_parameters_from_docstring(func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Copy the docstring's parameter descriptions into the MCP input schema.

    Apply below ``@mcp.tool()`` so the annotations are in place before FastMCP
    builds the tool's schema. The descriptions are written onto the undecorated
    function, which is the object `inspect.signature` resolves through the
    `functools.wraps` chain that `apply_config` establishes.

    A parameter already carrying an explicit `Field(description=...)` is left
    alone, so a tool can still override the docstring for one argument.

    Args:
        func: The tool function, optionally already wrapped by `apply_config`.

    Returns:
        The same callable, with parameter descriptions attached to its signature.

    Raises:
        XCodeMCPError: If any parameter is absent from the docstring's Args block,
            so an undocumented parameter fails at import rather than shipping a
            schema that cannot explain itself.
    """
    documented_function = inspect.unwrap(func)

    descriptions = parse_documented_parameters(inspect.getdoc(documented_function) or "")
    signature = inspect.signature(documented_function)

    undocumented = [
        name for name in signature.parameters if not descriptions.get(name)
    ]
    if undocumented:
        raise XCodeMCPError(
            f"Tool '{documented_function.__name__}' has parameters missing from its "
            f"docstring Args: block: {', '.join(undocumented)}. Document them there; "
            "that block is what the MCP input schema publishes."
        )

    annotations = dict(getattr(documented_function, "__annotations__", {}))
    for name in signature.parameters:
        annotation = annotations.get(name, str)
        if _already_describes_itself(annotation):
            continue
        annotations[name] = Annotated[annotation, Field(description=descriptions[name])]

    documented_function.__annotations__ = annotations
    return func


def _already_describes_itself(annotation: Any) -> bool:
    """Report whether an annotation carries its own non-empty Field description."""
    if get_origin(annotation) is not Annotated:
        return False
    return any(
        isinstance(metadata, FieldInfo) and metadata.description
        for metadata in get_args(annotation)[1:]
    )
