import ast
import re
from pathlib import Path


GOP_LITERAL_RE = re.compile(r"(?<!\d)\d{5}[A-Z0-9*]*(?!\d)")
APP_DIR = Path(__file__).resolve().parents[1] / "app"


def test_product_python_contains_no_gop_literals() -> None:
    violations: list[str] = []
    for path in sorted(APP_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            matches = sorted(set(GOP_LITERAL_RE.findall(node.value)))
            if matches:
                violations.append(f"{path.name}:{node.lineno}: {', '.join(matches)}")

    assert violations == [], (
        "GOP-Zuordnungen gehören in billing_rule_definitions.json oder den Quartalskatalog, "
        "nicht in den Python-Code:\n" + "\n".join(violations)
    )
