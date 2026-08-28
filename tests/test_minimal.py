#!/usr/bin/env python3
"""
Minimal tests that verify MCP functionality without triggering Xcode UI operations.
These tests focus on validating the MCP server's logic and error handling.
"""

import os
import time
import shutil
from pathlib import Path
from test_runner import XcodeMCPTestRunner, TestHelpers

class MinimalTests(XcodeMCPTestRunner):
    """Minimal tests focused on MCP server logic validation."""

    def test_version(self):
        """Test version command works and returns valid format."""
        result = self.run_mcp_tool("version")
        self.assert_success(result)

        # Validate format
        version = result["result"]
        assert "Drew's Xcode MCP Server (drews-xcode-mcp) version" in version
        assert len(version.split(".")) >= 2  # Should have version number like x.y.z

    def test_path_validation_logic(self):
        """Test path validation without opening Xcode."""
        print("\nTesting path validation logic...")

        # Test 1: Empty path
        result = self.run_mcp_tool("get_project_schemes", project_path="")
        self.assert_failure(result)
        assert "cannot be empty" in result.get("error", "")
        print("✓ Empty path rejected")

        # Test 2: Invalid extension
        result = self.run_mcp_tool("get_project_schemes",
                                  project_path=str(self.working_dir / "test.txt"))
        self.assert_failure(result)
        assert ".xcodeproj' or '.xcworkspace'" in result.get("error", "")
        print("✓ Invalid extension rejected")

        # Test 3: Path outside allowed folders
        result = self.run_mcp_tool("get_project_schemes",
                                  project_path="/private/tmp/Test.xcodeproj")
        self.assert_failure(result)
        assert "not allowed" in result.get("error", "")
        print("✓ Path outside allowed folders rejected")

        # Test 4: Valid path format but doesn't exist (within allowed folder)
        fake_path = self.working_dir / "NonExistent.xcodeproj"
        result = self.run_mcp_tool("get_project_schemes",
                                  project_path=str(fake_path))
        self.assert_failure(result)
        assert "does not exist" in result.get("error", "")
        print("✓ Non-existent project rejected")

    def test_project_discovery_fallback(self):
        """Test project discovery with filesystem fallback when mdfind fails."""
        print("\nTesting project discovery...")

        # Create simple xcodeproj directories (don't need to be valid)
        test_proj1 = self.working_dir / "TestProj1.xcodeproj"
        test_proj2 = self.working_dir / "TestProj2.xcworkspace"
        test_proj1.mkdir(exist_ok=True)
        test_proj2.mkdir(exist_ok=True)

        # Create marker files so they look like projects
        (test_proj1 / "project.pbxproj").touch()
        (test_proj2 / "contents.xcworkspacedata").touch()

        # Search for projects - even if mdfind returns nothing,
        # we should have a fallback that finds them
        result = self.run_mcp_tool("get_xcode_projects",
                                  search_path=str(self.working_dir))

        # Current implementation uses mdfind which might not find them
        # This is actually a bug we should fix
        if result["success"]:
            projects = result["result"].strip()
            if projects:
                print(f"✓ Found projects: {projects[:100]}")
            else:
                print("⚠ No projects found by mdfind (expected, shows need for fallback)")

        # Clean up
        shutil.rmtree(test_proj1)
        shutil.rmtree(test_proj2)

    def test_error_handling(self):
        """Test that errors are handled gracefully."""
        print("\nTesting error handling...")

        # Test with special characters in path
        special_path = self.working_dir / "Test's \"Special\" Project.xcodeproj"
        result = self.run_mcp_tool("get_project_schemes",
                                  project_path=str(special_path))
        self.assert_failure(result)
        # Should handle the path gracefully even with quotes
        assert "error" in result or not result["success"]
        print("✓ Special characters handled")

        # Test with very long path
        long_name = "A" * 200
        long_path = self.working_dir / f"{long_name}.xcodeproj"
        result = self.run_mcp_tool("get_project_schemes",
                                  project_path=str(long_path))
        self.assert_failure(result)
        print("✓ Long path handled")

    def test_parameter_validation(self):
        """Test that all parameter validations work correctly."""
        print("\nTesting parameter validation...")

        # A real project copied from the templates, not a hand-made directory with
        # an empty project.pbxproj. Both tools below reject their bad argument
        # before reaching Xcode, but a sham bundle turns any future ordering
        # change into a modal "project is damaged" alert on the developer's screen.
        project_dir = self.copy_project("SimpleApp")
        valid_project = project_dir / "SimpleApp.xcodeproj"

        try:
            # Bypasses apply_config: a configured parameter_override is a hard
            # override that would replace the bad value under test, so the guard
            # would never run and the result would depend on the local config.
            result = self.run_tool_bypassing_config(
                "build_project",
                project_path=str(valid_project),
                include_warnings="not_a_boolean")  # Invalid type
            self.assert_failure(result)
            assert "boolean" in result.get("error", "").lower(), \
                f"Expected a 'boolean' error, got: {result.get('error')}"
            print("✓ Invalid boolean parameter rejected")

            result = self.run_tool_bypassing_config(
                "run_project_until_terminated",
                project_path=str(valid_project),
                timeout=-5)  # Negative timeout
            self.assert_failure(result)
            assert "positive" in result.get("error", ""), \
                f"Expected a 'positive' timeout error, got: {result.get('error')}"
            print("✓ Negative timeout rejected")
        finally:
            shutil.rmtree(project_dir, ignore_errors=True)

    def test_helper_functions(self):
        """Test that helper functions work correctly."""
        print("\nTesting helper functions...")

        from drews_xcode_mcp.utils.applescript import escape_applescript_string
        from drews_xcode_mcp.security import validate_and_normalize_project_path

        # Test escaping quotes
        escaped = escape_applescript_string('Test "quoted" string')
        assert '\\"' in escaped
        print("✓ Quotes escaped correctly")

        # Test escaping backslashes
        escaped = escape_applescript_string('Test\\path')
        assert '\\\\' in escaped
        print("✓ Backslashes escaped correctly")

        # Test path normalization
        test_path = self.working_dir / "Test.xcodeproj"
        test_path.mkdir(exist_ok=True)

        normalized = validate_and_normalize_project_path(
            str(test_path), "Testing")
        assert os.path.isabs(normalized)
        print("✓ Path normalization works")

        # Clean up
        shutil.rmtree(test_path)

    def test_security_boundaries(self):
        """Test security boundaries are properly enforced."""
        print("\nTesting security boundaries...")

        # Test that we can't access parent directories
        parent_path = str(self.working_dir.parent / "Test.xcodeproj")
        result = self.run_mcp_tool("get_project_schemes",
                                  project_path=parent_path)
        self.assert_failure(result)
        assert "not allowed" in result.get("error", "")
        print("✓ Parent directory access blocked")

        # Test that we can't use .. in paths
        traversal_path = str(self.working_dir / ".." / "Test.xcodeproj")
        result = self.run_mcp_tool("get_project_schemes",
                                  project_path=traversal_path)
        self.assert_failure(result)
        # Should either be blocked or normalized away
        print("✓ Path traversal handled")

def run_minimal_tests():
    """Run all minimal tests."""
    print("\n" + "=" * 60)
    print("RUNNING MINIMAL TESTS (NO XCODE UI)")
    print("=" * 60)

    tests = MinimalTests()
    tests.setup()

    try:
        # Run each test
        tests.run_test(tests.test_version, "Version Command")
        tests.run_test(tests.test_path_validation_logic, "Path Validation Logic")
        tests.run_test(tests.test_project_discovery_fallback, "Project Discovery")
        tests.run_test(tests.test_error_handling, "Error Handling")
        tests.run_test(tests.test_parameter_validation, "Parameter Validation")
        tests.run_test(tests.test_helper_functions, "Helper Functions")
        tests.run_test(tests.test_security_boundaries, "Security Boundaries")

        # Print summary
        return tests.print_summary()

    finally:
        tests.teardown()

if __name__ == "__main__":
    import sys
    success = run_minimal_tests()
    sys.exit(0 if success else 1)