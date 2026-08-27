#!/usr/bin/env python3
"""
Test runner for Xcode MCP Server.
This module provides the core test infrastructure and utilities.
"""

import os
import sys
import subprocess
import json
import shutil
import pathlib
import time
from pathlib import Path
from typing import Dict, Any, Optional, List
import tempfile

# Add parent directory to path to import the MCP server
sys.path.insert(0, str(Path(__file__).parent.parent))

class XcodeMCPTestRunner:
    """Base class for running tests against the Xcode MCP Server."""

    def __init__(self):
        self.repo_root = Path(__file__).parent.parent
        self.templates_dir = self.repo_root / "test_projects" / "templates"
        self.working_dir = self.repo_root / "test_projects" / "working"
        self.server_process = None
        self.test_results = []

        # Ensure working directory exists
        self.working_dir.mkdir(parents=True, exist_ok=True)

    def setup(self):
        """Set up the test environment."""
        print("Setting up test environment...")

        # Clean working directory
        if self.working_dir.exists():
            shutil.rmtree(self.working_dir)
        self.working_dir.mkdir(parents=True, exist_ok=True)

        # Set environment variables
        os.environ["XCODEMCP_ALLOWED_FOLDERS"] = str(self.working_dir)
        print(f"Set allowed folders to: {self.working_dir}")

        # Set the global variable in the security module where it's actually used
        from drews_xcode_mcp.security import set_allowed_folders
        set_allowed_folders({str(self.working_dir)})
        print(f"Set ALLOWED_FOLDERS to: {self.working_dir}")

    def teardown(self):
        """Clean up the test environment."""
        print("Cleaning up test environment...")

        # Stop server if running
        if self.server_process:
            self.server_process.terminate()
            self.server_process = None

    def copy_project(self, project_name: str) -> Path:
        """Copy a template project to the working directory."""
        template_path = self.templates_dir / project_name
        working_path = self.working_dir / project_name

        if not template_path.exists():
            raise FileNotFoundError(f"Template project not found: {template_path}")

        # Remove if already exists
        if working_path.exists():
            shutil.rmtree(working_path)

        # Copy the template
        shutil.copytree(template_path, working_path)
        print(f"Copied {project_name} to working directory")

        self.wait_for_spotlight_index(working_path)

        return working_path

    def wait_for_spotlight_index(self, project_dir: Path, timeout_seconds: float = 60.0):
        """
        Block until Spotlight can find the project bundles just copied into project_dir.

        get_xcode_projects locates projects with `mdfind`, which only sees files
        Spotlight has indexed. A freshly copied tree is not indexed yet, so a test
        that copies and immediately searches races the indexer and intermittently
        reports the project missing. `mdimport` requests an immediate index; the
        poll confirms it actually landed before any test queries for it.

        Args:
            project_dir: Directory just populated with a copied template project.
            timeout_seconds: How long to wait before giving up.

        Raises:
            TimeoutError: If Spotlight never reports the bundles, so the cause is
                named rather than surfacing later as a missing-project assertion.
        """
        # Only top-level bundles. An .xcodeproj contains its own
        # project.xcworkspace, which lives inside the bundle and is never indexed
        # as a separate Spotlight item, so waiting on it would always time out.
        bundle_suffixes = (".xcodeproj", ".xcworkspace")
        expected = sorted(
            path for path in project_dir.rglob("*")
            if path.suffix in bundle_suffixes
            and not any(parent.suffix in bundle_suffixes for parent in path.parents)
        )
        if not expected:
            return

        subprocess.run(["mdimport", str(project_dir)], capture_output=True, timeout=60)

        # mdfind reports resolved paths and one result per line. Compare resolved
        # paths so reaching the repo through a symlink still matches, and split on
        # lines so a path containing a space stays one result.
        expected_resolved = {str(path.resolve()) for path in expected}

        deadline = time.monotonic() + timeout_seconds
        while True:
            found = {
                str(pathlib.Path(line).resolve())
                for line in subprocess.run(
                    ["mdfind", "-onlyin", str(project_dir),
                     'kMDItemFSName == "*.xcodeproj" || kMDItemFSName == "*.xcworkspace"'],
                    capture_output=True, text=True, timeout=30,
                ).stdout.splitlines() if line.strip()
            }
            missing = sorted(expected_resolved - found)
            if not missing:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Spotlight did not index {[pathlib.Path(m).name for m in missing]} in "
                    f"{project_dir} within {timeout_seconds:.0f}s. Tests that rely on "
                    "mdfind cannot run until it does."
                )
            time.sleep(0.25)

    def run_mcp_tool(self, tool_name: str, **params) -> Dict[str, Any]:
        """
        Run an MCP tool and return the result.

        Looks up the tool function from the FastMCP registry and calls it directly.
        """
        from drews_xcode_mcp.server import mcp
        import drews_xcode_mcp.tools  # noqa: F401 — triggers @mcp.tool() registration

        tool = mcp._tool_manager._tools.get(tool_name)
        if not tool:
            raise ValueError(f"Tool not found: {tool_name}")

        try:
            result = tool.fn(**params)
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "error": str(e), "error_type": type(e).__name__}

    def assert_success(self, result: Dict[str, Any], message: str = ""):
        """Assert that an MCP tool call was successful."""
        if not result.get("success"):
            error_msg = f"Tool call failed: {result.get('error', 'Unknown error')}"
            if message:
                error_msg = f"{message}: {error_msg}"
            raise AssertionError(error_msg)

    def assert_failure(self, result: Dict[str, Any], expected_error: str = None):
        """Assert that an MCP tool call failed as expected."""
        if result.get("success"):
            raise AssertionError(f"Expected failure but got success: {result.get('result')}")

        if expected_error and expected_error not in result.get("error", ""):
            raise AssertionError(
                f"Expected error containing '{expected_error}' "
                f"but got: {result.get('error')}"
            )

    def assert_contains(self, text: str, substring: str, message: str = ""):
        """Assert that text contains a substring."""
        if substring not in text:
            error_msg = f"Expected text to contain '{substring}'"
            if message:
                error_msg = f"{message}: {error_msg}"
            raise AssertionError(error_msg)

    def assert_not_contains(self, text: str, substring: str, message: str = ""):
        """Assert that text does not contain a substring."""
        if substring in text:
            error_msg = f"Expected text not to contain '{substring}'"
            if message:
                error_msg = f"{message}: {error_msg}"
            raise AssertionError(error_msg)

    def run_test(self, test_func, test_name: str = None):
        """Run a single test function and record the result."""
        if not test_name:
            test_name = test_func.__name__

        print(f"\nRunning test: {test_name}")
        try:
            test_func()
            self.test_results.append({"name": test_name, "passed": True})
            print(f"✅ {test_name} passed")
            return True
        except Exception as e:
            self.test_results.append({
                "name": test_name,
                "passed": False,
                "error": str(e)
            })
            print(f"❌ {test_name} failed: {e}")
            return False

    def print_summary(self):
        """Print a summary of test results."""
        print("\n" + "=" * 60)
        print("TEST SUMMARY")
        print("=" * 60)

        passed = sum(1 for r in self.test_results if r["passed"])
        total = len(self.test_results)

        print(f"\nTotal tests: {total}")
        print(f"Passed: {passed}")
        print(f"Failed: {total - passed}")

        if total - passed > 0:
            print("\nFailed tests:")
            for result in self.test_results:
                if not result["passed"]:
                    print(f"  - {result['name']}: {result.get('error', 'Unknown error')}")

        print("\n" + "=" * 60)
        return passed == total

    def wait_for_xcode(self, project_path: Path, timeout: int = 10):
        """Wait for Xcode to open and load a project."""
        print(f"Waiting for Xcode to load {project_path.name}...")
        time.sleep(2)  # Give Xcode time to start
        return True

class TestHelpers:
    """Helper functions for testing."""

    @staticmethod
    def create_test_file(project_path: Path, filename: str, content: str):
        """Create a test file in a project."""
        file_path = project_path / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return file_path

    @staticmethod
    def modify_file(file_path: Path, old_text: str, new_text: str):
        """Modify a file by replacing text."""
        content = file_path.read_text()
        content = content.replace(old_text, new_text)
        file_path.write_text(content)

    @staticmethod
    def file_contains(file_path: Path, text: str) -> bool:
        """Check if a file contains specific text."""
        if not file_path.exists():
            return False
        return text in file_path.read_text()

    @staticmethod
    def count_lines(text: str) -> int:
        """Count lines in text."""
        return len(text.strip().split('\n')) if text.strip() else 0

    @staticmethod
    def extract_errors(output: str) -> List[str]:
        """Extract error lines from build output."""
        lines = output.split('\n')
        return [line for line in lines if 'error' in line.lower()]

    @staticmethod
    def extract_warnings(output: str) -> List[str]:
        """Extract warning lines from build output."""
        lines = output.split('\n')
        return [line for line in lines if 'warning' in line.lower()]

if __name__ == "__main__":
    # Simple test of the test runner
    runner = XcodeMCPTestRunner()
    try:
        runner.setup()

        # Test getting version
        result = runner.run_mcp_tool("version")
        print(f"Version result: {result}")

        runner.assert_success(result)
        runner.assert_contains(result["result"], "Xcode MCP Server")

        print("\n✅ Test runner is working!")

    finally:
        runner.teardown()