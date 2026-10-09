import ast
from pathlib import Path

CORE = Path(__file__).resolve().parents[2] / "app" / "core"
ALLOWED_TOP = {"app.core", "app.db", "app.ai.client"}


def _module_level_app_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for (
        node
    ) in tree.body:  # solo nivel de modulo: los imports perezosos dentro de funciones se permiten
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("app"))
    return found


def test_core_does_not_import_domain_packages_at_module_level():
    bad = {}
    for py in CORE.glob("*.py"):
        for mod in _module_level_app_imports(py):
            if not any(mod == a or mod.startswith(a + ".") for a in ALLOWED_TOP):
                bad[py.name] = mod
    assert not bad, bad
