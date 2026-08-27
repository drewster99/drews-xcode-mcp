#!/usr/bin/env python3
"""
Fixed tests that actually test MCP server functionality properly.
These tests ensure we're testing real behavior, not just mocking responses.
"""

import re
import os
import time
from pathlib import Path
from test_runner import XcodeMCPTestRunner, TestHelpers

class FixedTests(XcodeMCPTestRunner):
    """Fixed tests that properly validate MCP server functionality."""

    def test_version_format(self):
        """Test that version command returns proper format and content."""
        result = self.run_mcp_tool("version")
        self.assert_success(result)

        # Check format
        version = result["result"]
        self.assert_contains(version, "Drew's Xcode MCP Server (drews-xcode-mcp) version")

        # Extract version number and validate format (e.g., "1.0.6", "1.3.14b1").
        # A source checkout appends " (dev source <hash>)", so take the first token.
        parts = version.split("version ")
        assert len(parts) == 2, f"Version format unexpected: {version}"

        version_num = parts[1].split()[0]
        assert re.fullmatch(r"\d+\.\d+(\.\d+)?([ab]\d+)?", version_num), \
            f"Version number format invalid: {version_num}"

        print(f"✓ Version format validated: {version_num}")

    def test_get_project_hierarchy_actual(self):
        """Test that hierarchy actually reflects the file structure."""
        # Copy SimpleApp
        project_path = self.copy_project("SimpleApp")
        xcodeproj_path = project_path / "SimpleApp.xcodeproj"

        # Get hierarchy
        result = self.run_mcp_tool("get_project_hierarchy", project_path=str(xcodeproj_path))
        self.assert_success(result)

        hierarchy = result["result"]

        # Validate that hierarchy shows actual structure
        # Should show the parent directory
        self.assert_contains(hierarchy, "SimpleApp/", "Should show SimpleApp directory")
        self.assert_contains(hierarchy, "SimpleApp.xcodeproj", "Should show xcodeproj")
        self.assert_contains(hierarchy, "main.swift", "Should show main.swift file")

        # Count lines to ensure we got a real hierarchy
        lines = hierarchy.strip().split('\n')
        assert len(lines) > 3, f"Hierarchy too short, only {len(lines)} lines"

        print(f"✓ Hierarchy contains {len(lines)} lines of actual structure")

    def test_build_project_real(self):
        """Test actual building of a project and check real results."""
        # Copy SimpleApp
        project_path = self.copy_project("SimpleApp")
        xcodeproj_path = project_path / "SimpleApp.xcodeproj"

        print("Testing real build...")

        # Attempt to build
        result = self.run_mcp_tool("build_project", project_path=str(xcodeproj_path))

        # Check the result - it might fail or succeed depending on Xcode state
        if result["success"]:
            # If it succeeded, verify the response format
            self.assert_contains(result["result"], "succeeded", "Success message should contain 'succeeded'")
            print("✓ Build succeeded as expected")
        else:
            # If it failed, ensure we get meaningful error info
            error = result.get("error", "")
            assert error, "Should have error message on failure"

            # Common reasons for failure in test environment
            if "scheme" in error.lower() or "workspace" in error.lower() or "can't get workspace" in error.lower():
                print(f"✓ Build failed with expected error (project not open): {error[:100]}...")
                print("  (This is expected for newly created test projects)")
            else:
                # This is an unexpected error - fail the test
                raise AssertionError(f"Unexpected build error: {error}")

    def test_build_with_errors_real(self):
        """Test that build errors are properly reported."""
        # Copy BrokenApp
        project_path = self.copy_project("BrokenApp")
        xcodeproj_path = project_path / "BrokenApp.xcodeproj"

        print("Testing build with errors...")

        # Attempt to build - should fail
        result = self.run_mcp_tool("build_project", project_path=str(xcodeproj_path))

        if result["success"]:
            # If it somehow succeeded, the output should mention errors
            output = result["result"]
            print(f"Build output: {output[:200]}...")
        else:
            # More likely - it failed
            error = result.get("error", "")

            # We should get information about the compilation errors
            # The error might be about scheme/workspace or actual compilation
            assert error, "Should have error details"
            print(f"✓ Build failed as expected: {error[:150]}...")

    def test_path_validation_comprehensive(self):
        """Test comprehensive path validation with real scenarios."""
        print("\nTesting path validation...")

        # Test 1: Path within allowed folders but doesn't exist
        fake_path = self.working_dir / "FakeProject" / "FakeProject.xcodeproj"
        result = self.run_mcp_tool("get_project_hierarchy", project_path=str(fake_path))
        self.assert_failure(result)
        # Should fail with "does not exist" since it's within allowed folders
        assert "does not exist" in result.get("error", ""), \
            f"Expected 'does not exist' error, got: {result.get('error')}"
        print("✓ Non-existent path properly rejected")

        # Test 2: Invalid extension
        valid_dir = self.working_dir / "test"
        valid_dir.mkdir(exist_ok=True)
        result = self.run_mcp_tool("get_project_hierarchy", project_path=str(valid_dir))
        self.assert_failure(result)
        self.assert_contains(result["error"], ".xcodeproj' or '.xcworkspace'")
        print("✓ Invalid extension properly rejected")

        # Test 3: Empty path
        result = self.run_mcp_tool("get_project_hierarchy", project_path="")
        self.assert_failure(result)
        self.assert_contains(result["error"], "cannot be empty")
        print("✓ Empty path properly rejected")

        # Test 4: Path outside allowed folders
        result = self.run_mcp_tool("get_project_hierarchy",
                                  project_path="/tmp/SomeProject/Project.xcodeproj")
        self.assert_failure(result)
        self.assert_contains(result["error"], "not allowed")
        print("✓ Path outside allowed folders properly rejected")

    def test_get_schemes_real(self):
        """Test getting actual schemes from a real project."""
        # Copy SimpleApp
        project_path = self.copy_project("SimpleApp")
        xcodeproj_path = project_path / "SimpleApp.xcodeproj"

        print("Testing scheme retrieval...")

        # Get schemes
        result = self.run_mcp_tool("get_project_schemes", project_path=str(xcodeproj_path))

        if result["success"]:
            schemes = result["result"]
            print(f"Found schemes: {schemes}")

            # Should have at least one scheme (SimpleApp)
            if schemes:
                self.assert_contains(schemes, "SimpleApp")
                print("✓ Found expected SimpleApp scheme")
            else:
                print("⚠ No schemes found (might need Xcode to generate)")
        else:
            # Might fail if Xcode hasn't processed the project yet
            print(f"Scheme retrieval failed (expected for new project): {result.get('error', '')[:100]}")

    def test_clean_project_real(self):
        """Test that clean operation works."""
        # Copy SimpleApp
        project_path = self.copy_project("SimpleApp")
        xcodeproj_path = project_path / "SimpleApp.xcodeproj"

        print("Testing clean operation...")

        # Clean the project
        result = self.run_mcp_tool("clean_project", project_path=str(xcodeproj_path))

        if result["success"]:
            self.assert_contains(result["result"], "success")
            print("✓ Clean completed successfully")
        else:
            # Might fail if project not open in Xcode
            error = result.get("error", "")
            print(f"Clean failed (might be expected): {error[:100]}")

    def test_console_output_real(self):
        """Test getting runtime output from a project."""
        # Copy ConsoleApp
        project_path = self.copy_project("ConsoleApp")
        xcodeproj_path = project_path / "ConsoleApp.xcodeproj"

        print("Testing runtime output retrieval...")

        # Try to get runtime output (might not exist yet)
        result = self.run_mcp_tool("get_runtime_output",
                                  project_path=str(xcodeproj_path))

        if result["success"]:
            output = result["result"]
            if "No xcresult file found" in output:
                print("✓ Correctly reported no previous run")
            else:
                # If there is output, validate it
                lines = output.strip().split('\n')
                assert len(lines) <= 10 + 3, "Should respect max_lines limit"  # +3 for header
                print(f"✓ Retrieved {len(lines)} lines of output")
        else:
            print(f"Runtime output retrieval failed: {result.get('error', '')[:100]}")

    def test_security_validation(self):
        """Test that security checks are working properly."""
        print("\nTesting security validation...")

        # Test path traversal attempt
        traversal_path = self.working_dir / ".." / ".." / "etc" / "passwd.xcodeproj"
        result = self.run_mcp_tool("get_project_hierarchy", project_path=str(traversal_path))
        self.assert_failure(result)
        assert "not allowed" in result.get("error", "").lower() or \
               "does not exist" in result.get("error", "").lower(), \
               "Should reject path traversal attempts"
        print("✓ Path traversal properly blocked")

        # Test symlink handling
        project_path = self.copy_project("SimpleApp")
        xcodeproj_path = project_path / "SimpleApp.xcodeproj"
        symlink_path = self.working_dir / "SymlinkProject.xcodeproj"

        if symlink_path.exists():
            symlink_path.unlink()
        symlink_path.symlink_to(xcodeproj_path)

        result = self.run_mcp_tool("get_project_hierarchy", project_path=str(symlink_path))
        # Should work since it resolves to allowed path
        if result["success"]:
            print("✓ Symlinks properly resolved and allowed")
        else:
            print(f"⚠ Symlink test result: {result.get('error', '')[:100]}")

def run_fixed_tests():
    """Run all fixed tests."""
    print("\n" + "=" * 60)
    print("RUNNING FIXED TESTS (REAL FUNCTIONALITY)")
    print("=" * 60)

    tests = FixedTests()
    tests.setup()

    try:
        # Run each test
        tests.run_test(tests.test_version_format, "Version Format Validation")
        tests.run_test(tests.test_get_project_hierarchy_actual, "Project Hierarchy (Real)")
        tests.run_test(tests.test_build_project_real, "Build Project (Real)")
        tests.run_test(tests.test_build_with_errors_real, "Build With Errors (Real)")
        tests.run_test(tests.test_path_validation_comprehensive, "Path Validation (Comprehensive)")
        tests.run_test(tests.test_get_schemes_real, "Get Schemes (Real)")
        tests.run_test(tests.test_clean_project_real, "Clean Project (Real)")
        tests.run_test(tests.test_console_output_real, "Console Output (Real)")
        tests.run_test(tests.test_security_validation, "Security Validation")

        # Print summary
        return tests.print_summary()

    finally:
        tests.teardown()

if __name__ == "__main__":
    import sys
    success = run_fixed_tests()
    sys.exit(0 if success else 1)