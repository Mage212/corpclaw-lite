from pathlib import Path

from corpclaw_lite.extensions.skills.loader import SkillLoader
from corpclaw_lite.extensions.skills.registry import SkillRegistry
from corpclaw_lite.users.models import User


def test_skill_loader(tmp_path: Path) -> None:
    skill_file = tmp_path / "test_skill.md"
    skill_file.write_text(
        "---\n"
        "id: my_test\n"
        "description: test desc\n"
        "allowed_for: [marketing]\n"
        "---\n"
        "Here are instructions."
    )

    skill = SkillLoader.load_from_file(skill_file)
    assert skill is not None
    assert skill.id == "my_test"
    assert skill.description == "test desc"
    assert skill.allowed_for == ["marketing"]
    assert skill.instructions == "Here are instructions."
    assert skill.version == "1.0.0"


def test_skill_registry(tmp_path: Path) -> None:
    skill_file1 = tmp_path / "skill1.md"
    skill_file1.write_text("---\nid: s1\nallowed_for: ['*']\n---\ntext1")

    skill_file2 = tmp_path / "skill2.md"
    skill_file2.write_text("---\nid: s2\nallowed_for: [dev]\n---\ntext2")

    registry = SkillRegistry()
    registry.load_directory(tmp_path)

    assert len(registry.list_all()) == 2

    user_marketing = User(id=1, name="Mark", department="marketing")
    user_dev = User(id=2, name="Dave", department="dev")

    # Marketing sees only s1 (*)
    allowed_m = registry.get_allowed_skills(user_marketing)
    assert len(allowed_m) == 1
    assert allowed_m[0].id == "s1"

    # Dev sees s1 (*) and s2 (dev)
    allowed_d = registry.get_allowed_skills(user_dev)
    assert len(allowed_d) == 2


def test_skill_registry_overlay_replace(tmp_path: Path) -> None:
    """Overlay dir overrides a default skill by id when allow_replace=True."""
    default_dir = tmp_path / "default"
    overlay_dir = tmp_path / "overlay"
    default_dir.mkdir()
    overlay_dir.mkdir()

    (default_dir / "shared.md").write_text("---\nid: shared\n---\nfrom default")
    (overlay_dir / "shared.md").write_text("---\nid: shared\n---\nfrom overlay")
    (overlay_dir / "extra.md").write_text("---\nid: extra\n---\noverlay-only")

    registry = SkillRegistry()
    registry.load_directory(default_dir)
    registry.load_directory(overlay_dir, allow_replace=True)

    skills = {s.id: s for s in registry.list_all()}
    assert set(skills) == {"shared", "extra"}
    assert skills["shared"].instructions == "from overlay"
    assert skills["extra"].instructions == "overlay-only"


def test_skill_registry_overlay_without_replace_raises(tmp_path: Path) -> None:
    """Without allow_replace, a duplicate id from a second dir raises ValueError."""
    default_dir = tmp_path / "default"
    overlay_dir = tmp_path / "overlay"
    default_dir.mkdir()
    overlay_dir.mkdir()
    (default_dir / "shared.md").write_text("---\nid: shared\n---\nfrom default")
    (overlay_dir / "shared.md").write_text("---\nid: shared\n---\nfrom overlay")

    registry = SkillRegistry()
    registry.load_directory(default_dir)
    import pytest

    with pytest.raises(ValueError, match="already registered"):
        registry.load_directory(overlay_dir)
