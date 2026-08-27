#!/usr/bin/env python3
"""Reject MCP tool calls carrying arguments that no tool parameter declares.

FastMCP validates every tool call against a pydantic model generated from the
tool's signature. Pydantic defaults to ``extra="ignore"``, so a key matching no
parameter is silently discarded and the parameter keeps its default -- the call
then succeeds while doing something other than what the caller asked for. A
misspelled optional argument is indistinguishable from omitting it.
"""

from pydantic import ConfigDict, ValidationError

from drews_xcode_mcp.exceptions import XCodeMCPError


def enforce_strict_tool_arguments() -> None:
    """
    Make unknown tool-call arguments a validation error rather than a silent no-op.

    Must run before any tool is registered: FastMCP builds each tool's argument
    model when the tool is registered, and a pydantic model inherits its config
    from its base at class-creation time.

    Raises:
        XCodeMCPError: If the FastMCP internals this depends on have moved, or if
            the resulting models still accept unknown keys. Losing the check is
            never allowed to pass unnoticed.
    """
    try:
        from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, func_metadata
    except ImportError as e:
        raise XCodeMCPError(
            "Cannot enforce strict tool arguments: "
            f"mcp.server.fastmcp.utilities.func_metadata is unavailable ({e}). "
            "The installed 'mcp' package changed its internals; update "
            "enforce_strict_tool_arguments() to match."
        ) from e

    existing_config = getattr(ArgModelBase, "model_config", None)
    if existing_config is None:
        raise XCodeMCPError(
            "Cannot enforce strict tool arguments: ArgModelBase has no model_config. "
            "The installed 'mcp' package changed its internals; update "
            "enforce_strict_tool_arguments() to match."
        )

    ArgModelBase.model_config = ConfigDict(**{**existing_config, "extra": "forbid"})

    _verify_unknown_arguments_are_rejected(func_metadata)


def _verify_unknown_arguments_are_rejected(func_metadata) -> None:
    """
    Prove the config change reaches a freshly generated argument model.

    Guards against a future pydantic or mcp release that builds argument models
    without inheriting ArgModelBase's config, which would leave unknown keys
    silently ignored again.
    """
    def probe(declared_parameter: str = "") -> str:
        return declared_parameter

    arg_model = func_metadata(probe).arg_model
    try:
        arg_model.model_validate({"undeclared_parameter": "x"})
    except ValidationError:
        return

    raise XCodeMCPError(
        "Cannot enforce strict tool arguments: generated argument models still "
        "accept undeclared keys after setting extra='forbid' on ArgModelBase. "
        "The installed 'mcp' or 'pydantic' package changed how argument models "
        "are built; update enforce_strict_tool_arguments() to match."
    )
