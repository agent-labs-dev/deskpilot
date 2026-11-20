"""The AUR package files `scripts/render_aur.py` writes for a release."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("render_aur", ROOT / "scripts" / "render_aur.py")
assert SPEC and SPEC.loader
render_aur = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render_aur)

SHA = "a" * 64


def test_srcinfo_lists_what_the_pkgbuild_declares() -> None:
    build = render_aur.pkgbuild("1.2.3", SHA)
    info = render_aur.srcinfo("1.2.3", SHA)
    assert "pkgver=1.2.3" in build and "\tpkgver = 1.2.3" in info
    assert f"sha256sums=('{SHA}')" in build and f"\tsha256sums = {SHA}" in info
    for field, items in (
        ("depends", render_aur.DEPENDS),
        ("makedepends", render_aur.MAKEDEPENDS),
        ("optdepends", render_aur.OPTDEPENDS),
    ):
        quoted = " ".join(f"'{item}'" for item in items)
        assert f"\n{field}=({quoted})\n" in build
        assert [line.split(" = ", 1)[1] for line in info.splitlines() if line.startswith(f"\t{field} = ")] == [*items]


def test_no_value_breaks_the_shell_quoting() -> None:
    values = (*render_aur.DEPENDS, *render_aur.MAKEDEPENDS, *render_aur.OPTDEPENDS)
    assert not any("'" in value for value in values)
    assert '"' not in render_aur.DESCRIPTION and "$" not in render_aur.DESCRIPTION


def test_every_python_dependency_is_an_arch_package() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    names = {requirement.split(">")[0].split("=")[0].strip() for requirement in project["dependencies"]}
    assert {f"python-{name}" for name in names} <= set(render_aur.DEPENDS)
