#!/usr/bin/env python3
"""
Debug test to understand what's happening with our tests.
"""

from pathlib import Path
import sys
import os
import subprocess

sys.path.insert(0, str(Path(__file__).parent.parent))

from test_runner import XcodeMCPTestRunner

def debug_project_search():
    """Debug why project search is failing."""
    runner = XcodeMCPTestRunner()
    runner.setup()

    try:
        # Copy test projects. This script exercises get_xcode_projects (Spotlight
        # discovery), so wait for the copied bundles to be indexed.
        print("\n=== Copying projects ===")
        simple_app_path = runner.copy_project("SimpleApp", index_for_discovery=True)
        console_app_path = runner.copy_project("ConsoleApp", index_for_discovery=True)

        print(f"SimpleApp copied to: {simple_app_path}")
        print(f"ConsoleApp copied to: {console_app_path}")

        # Check what's actually in the working directory
        print(f"\n=== Working directory contents ===")
        for item in runner.working_dir.iterdir():
            print(f"  {item}")
            if item.is_dir():
                for subitem in item.iterdir():
                    print(f"    -> {subitem}")

        # Try to search for projects
        print(f"\n=== Searching for projects in {runner.working_dir} ===")
        result = runner.run_mcp_tool("get_xcode_projects", search_path=str(runner.working_dir))

        print(f"Success: {result.get('success')}")
        print(f"Result: {result.get('result')}")
        print(f"Error: {result.get('error')}")

        # Try using mdfind directly
        print(f"\n=== Direct mdfind test ===")
        try:
            mdfind_result = subprocess.run(
                ['mdfind', '-onlyin', str(runner.working_dir),
                 'kMDItemFSName == "*.xcodeproj"'],
                capture_output=True, text=True
            )
            print(f"mdfind return code: {mdfind_result.returncode}")
            print(f"mdfind stdout: {mdfind_result.stdout}")
            print(f"mdfind stderr: {mdfind_result.stderr}")
        except Exception as e:
            print(f"mdfind error: {e}")

        # Check if the .xcodeproj files actually exist
        print(f"\n=== Checking .xcodeproj existence ===")
        simple_xcodeproj = simple_app_path / "SimpleApp.xcodeproj"
        console_xcodeproj = console_app_path / "ConsoleApp.xcodeproj"

        print(f"SimpleApp.xcodeproj exists: {simple_xcodeproj.exists()}")
        print(f"ConsoleApp.xcodeproj exists: {console_xcodeproj.exists()}")

        if simple_xcodeproj.exists():
            print(f"  Contents: {list(simple_xcodeproj.iterdir())}")

    finally:
        runner.teardown()

def debug_path_validation():
    """Debug why path validation test is failing."""
    runner = XcodeMCPTestRunner()
    runner.setup()

    try:
        print("\n=== Testing path validation ===")

        # Test with non-existent path
        print("\n1. Testing non-existent path:")
        result = runner.run_mcp_tool(
            "get_project_hierarchy",
            project_path="/nonexistent/path/Project.xcodeproj"
        )
        print(f"  Success: {result.get('success')}")
        print(f"  Error: {result.get('error')}")
        print(f"  Error type: {result.get('error_type')}")

        # The test expects "does not exist" in the error
        if result.get('error'):
            print(f"  'does not exist' in error: {'does not exist' in result['error']}")

        # Test with invalid extension
        print("\n2. Testing invalid extension:")
        valid_dir = runner.working_dir / "test"
        valid_dir.mkdir(exist_ok=True)

        result = runner.run_mcp_tool(
            "get_project_hierarchy",
            project_path=str(valid_dir)
        )
        print(f"  Success: {result.get('success')}")
        print(f"  Error: {result.get('error')}")

        # Test with empty path
        print("\n3. Testing empty path:")
        result = runner.run_mcp_tool("get_project_hierarchy", project_path="")
        print(f"  Success: {result.get('success')}")
        print(f"  Error: {result.get('error')}")

    finally:
        runner.teardown()

if __name__ == "__main__":
    print("=" * 60)
    print("DEBUG TEST RUNNER")
    print("=" * 60)

    debug_project_search()
    debug_path_validation()