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
