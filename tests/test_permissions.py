from pathlib import Path

from corpclaw_lite.departments.manager import DepartmentManager
from corpclaw_lite.departments.permissions import PermissionChecker
from corpclaw_lite.users.models import User


def test_permission_checker(tmp_path: Path) -> None:
    yaml_file = tmp_path / "departments.yaml"
    yaml_file.write_text(
        "departments:\n"
        "  marketing:\n"
        "    allowed_tools: [read_file, write_file]\n"
        "    allowed_skills: [content_writer]\n"
        "    allowed_plugins: [excel]\n"
        "    allowed_subagents: [research]\n"
        "    allowed_mcp: ['*']\n"
        "  admin:\n"
        "    allowed_tools: ['*']\n"
    )

    manager = DepartmentManager()
    manager.load_file(yaml_file)

    checker = PermissionChecker(manager)

    u_marketing = User(id=1, name="M", department="marketing")
    u_admin = User(id=2, name="A", department="admin")
    u_unknown = User(id=3, name="U", department="unknown")

    # Tool checks
    assert checker.can_use_tool(u_marketing, "read_file") is True
    assert checker.can_use_tool(u_marketing, "exec_script") is False
    assert checker.can_use_tool(u_admin, "exec_script") is True
    assert checker.can_use_tool(u_unknown, "read_file") is False

    # Other items
    assert checker.can_use_skill(u_marketing, "content_writer") is True
    assert checker.can_use_plugin(u_marketing, "excel") is True
    assert checker.can_dispatch_subagent(u_marketing, "research") is True
    assert checker.can_use_mcp(u_marketing, "any_server") is True


def test_department_budget_keys_from_yaml(tmp_path: Path) -> None:
    """DepartmentManager reads max_iterations and max_time_ms (not max_steps/max_wall_time_ms)."""
    yaml_file = tmp_path / "departments.yaml"
    yaml_file.write_text(
        "departments:\n"
        "  marketing:\n"
        "    description: Marketing team\n"
        "    allowed_tools: [read_file]\n"
        "    budget:\n"
        "      max_iterations: 7\n"
        "      max_tool_calls: 14\n"
        "      max_time_ms: 30000\n"
    )
    manager = DepartmentManager()
    manager.load_file(yaml_file)

    dept = manager.get_department("marketing")
    assert dept is not None
    assert dept.budget.max_iterations == 7
    assert dept.budget.max_tool_calls == 14
    assert dept.budget.max_time_ms == 30000


def test_department_name_from_description(tmp_path: Path) -> None:
    """DepartmentConfig.name is read from the 'description' YAML key."""
    yaml_file = tmp_path / "departments.yaml"
    yaml_file.write_text(
        "departments:\n  hr:\n    description: Human Resources team\n    allowed_tools: ['*']\n"
    )
    manager = DepartmentManager()
    manager.load_file(yaml_file)

    dept = manager.get_department("hr")
    assert dept is not None
    assert dept.name == "Human Resources team"
