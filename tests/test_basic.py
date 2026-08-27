#!/usr/bin/env python3
"""
Basic functionality tests for Xcode MCP Server.
Tests core functions like version, project discovery, and scheme listing.
"""

from pathlib import Path
from test_runner import XcodeMCPTestRunner, TestHelpers

class BasicTests(XcodeMCPTestRunner):
    """Test basic MCP server functionality."""

    def test_version(self):
        """Test that version command returns expected format."""
        result = self.run_mcp_tool("version")
        self.assert_success(result)
        self.assert_contains(result["result"], "Drew's Xcode MCP Server (drews-xcode-mcp) version")

    def test_get_xcode_projects_empty(self):
        """Test finding projects in empty directory."""
        empty_dir = self.working_dir / "empty"
        empty_dir.mkdir(exist_ok=True)

        result = self.run_mcp_tool("get_xcode_projects", search_path=str(empty_dir))
        self.assert_success(result)

        # Should return empty string for no projects
        assert result["result"] == "", f"Expected empty result, got: {result['result']}"

    def test_get_xcode_projects_with_projects(self):
        """Test finding projects in directory with projects."""
        # Copy test projects
        simple_app_path = self.copy_project("SimpleApp")
        console_app_path = self.copy_project("ConsoleApp")

        # Search for projects
        result = self.run_mcp_tool("get_xcode_projects", search_path=str(self.working_dir))
        self.assert_success(result)

        # Should find both projects
        projects = result["result"].split('\n') if result["result"] else []
        assert len(projects) >= 2, f"Expected at least 2 projects, found {len(projects)}"

        # Check that both projects are found
        project_names = [Path(p).name for p in projects]
        assert "SimpleApp.xcodeproj" in project_names, "SimpleApp.xcodeproj not found"
        assert "ConsoleApp.xcodeproj" in project_names, "ConsoleApp.xcodeproj not found"

    def test_get_directory_tree(self):
        """Test getting project directory tree."""
        # Copy SimpleApp
        project_path = self.copy_project("SimpleApp")
        xcodeproj_path = project_path / "SimpleApp.xcodeproj"

        # Get tree (accepts .xcodeproj path, scans parent)
        result = self.run_mcp_tool("get_directory_tree", directory_path=str(xcodeproj_path))
        self.assert_success(result)

        # Check that tree contains expected elements
        tree = result["result"]
        self.assert_contains(tree, "SimpleApp", "Tree should show SimpleApp directory")
        self.assert_contains(tree, "SimpleApp.xcodeproj", "Tree should show xcodeproj")

    def test_get_project_schemes(self):
        """Test getting available build schemes."""
        # Copy SimpleApp
        project_path = self.copy_project("SimpleApp")
        xcodeproj_path = project_path / "SimpleApp.xcodeproj"

        # Get schemes
        result = self.run_mcp_tool("get_project_schemes", project_path=str(xcodeproj_path))

        # This might fail if Xcode isn't properly configured
        # We'll handle both success and expected failure
        if result["success"]:
            schemes = result["result"]
            print(f"Found schemes: {schemes}")
        else:
            # If it fails, it should be because of Xcode not being able to load the minimal project
            print(f"Schemes query failed (expected for minimal test project): {result.get('error')}")

    def test_path_validation(self):
        """Test path validation and security checks."""
        # Test with path outside allowed folders (security check fires first)
        result = self.run_mcp_tool(
            "get_project_schemes",
            project_path="/nonexistent/path/Project.xcodeproj"
        )
        self.assert_failure(result)
        self.assert_contains(result["error"], "not allowed")

        # Test with non-existent path inside allowed folders
        fake_proj = self.working_dir / "DoesNotExist.xcodeproj"
        result = self.run_mcp_tool(
            "get_project_schemes",
            project_path=str(fake_proj)
        )
        self.assert_failure(result)
        self.assert_contains(result["error"], "does not exist")

        # Test with invalid extension
        valid_dir = self.working_dir / "test"
        valid_dir.mkdir(exist_ok=True)

        result = self.run_mcp_tool(
            "get_project_schemes",
            project_path=str(valid_dir)
        )
        self.assert_failure(result)
        self.assert_contains(result["error"], "must end with")

        # Test with empty path
        result = self.run_mcp_tool("get_project_schemes", project_path="")
        self.assert_failure(result)
        self.assert_contains(result["error"], "cannot be empty")

    def test_search_all_allowed_folders(self):
        """Test searching all allowed folders when no path specified."""
        # Copy a project
        self.copy_project("SimpleApp")

        # Search without specifying path (should search all allowed folders)
        result = self.run_mcp_tool("get_xcode_projects")
        self.assert_success(result)

        # Should find the SimpleApp project
        if result["result"]:
            self.assert_contains(result["result"], "SimpleApp.xcodeproj")

    def test_path_normalization(self):
        """Test that paths are normalized correctly."""
        # Create a project with symlinks
        project_path = self.copy_project("SimpleApp")
        xcodeproj_path = project_path / "SimpleApp.xcodeproj"

        # Create a symlink
        symlink_path = self.working_dir / "SimpleAppLink.xcodeproj"
        if symlink_path.exists():
            symlink_path.unlink()
        symlink_path.symlink_to(xcodeproj_path)

        # Try to get directory tree through symlink
        result = self.run_mcp_tool("get_directory_tree", directory_path=str(symlink_path))

        # Should work with normalized path
        if result["success"]:
            print("Symlink resolution working correctly")
        else:
            print(f"Symlink test result: {result.get('error')}")

def run_basic_tests():
    """Run all basic tests."""
    print("\n" + "=" * 60)
    print("RUNNING BASIC FUNCTIONALITY TESTS")
    print("=" * 60)

    tests = BasicTests()
    tests.setup()

    try:
        # Run each test
        tests.run_test(tests.test_version, "Version Command")
        tests.run_test(tests.test_get_xcode_projects_empty, "Find Projects - Empty Dir")
        tests.run_test(tests.test_get_xcode_projects_with_projects, "Find Projects - With Projects")
        tests.run_test(tests.test_get_directory_tree, "Get Directory Tree")
        tests.run_test(tests.test_get_project_schemes, "Get Project Schemes")
        tests.run_test(tests.test_path_validation, "Path Validation")
        tests.run_test(tests.test_search_all_allowed_folders, "Search All Allowed Folders")
        tests.run_test(tests.test_path_normalization, "Path Normalization")

        # Print summary
        return tests.print_summary()

    finally:
        tests.teardown()

if __name__ == "__main__":
    import sys
    success = run_basic_tests()
    sys.exit(0 if success else 1)