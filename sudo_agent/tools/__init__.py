"""Importing this package registers every tool. The groups are the specialist
"agents" under the one supervisor: computer, browser, terminal, files, research, system."""
from . import agent_tools, browser, desktop, documents, explorer, files, my_browser, research, system, terminal  # noqa: F401
from .base import REGISTRY, ToolContext, ToolResult, execute_tool  # noqa: F401
