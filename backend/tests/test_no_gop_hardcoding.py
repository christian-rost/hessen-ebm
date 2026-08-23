import ast
import re
from pathlib import Path


# Der Katalog fuehrt fuenfstellige GOPs; das Regelwerk normalisiert vierstellige
# Angaben per zfill auf fuenf Stellen. Beide Formen muessen deshalb erkannt werden.
GOP_LITERAL_RE = re.compile(r"(?<!\d)\d{5}[A-Z0-9*]*(?!\d)")
SHORT_GOP_LITERAL_RE = re.compile(r"^\s*\d{4}\s*$")
QUARTER_LITERAL_RE = re.compile(r"\s*(?:19|20)\d{2}/Q[1-4]\s*")
APP_DIR = Path(__file__).resolve().parents[1] / "app"

# Vierstellige Zahlen sind meist Puffergroessen oder Limits, keine GOPs.
ALLOWED_INT_LITERALS = {1024, 2000, 3000, 4096, 8000, 8192, 65536}


def _source_files() -> list[Path]:
    # rglob statt glob: ein spaeteres Unterpaket, z. B. app/export_profiles/,
    # darf nicht unbemerkt aus der Pruefung fallen.
    return sorted(APP_DIR.rglob("*.py"))


def _constants(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            yield node


def test_product_python_contains_no_gop_literals() -> None:
    violations: list[str] = []
    for path in _source_files():
        for node in _constants(path):
            if not isinstance(node.value, str):
                continue
            matches = sorted(set(GOP_LITERAL_RE.findall(node.value)))
            if matches:
                violations.append(f"{path.name}:{node.lineno}: {', '.join(matches)}")

    assert violations == [], (
        "GOP-Zuordnungen gehören in billing_rule_definitions.json oder den Quartalskatalog, "
        "nicht in den Python-Code:\n" + "\n".join(violations)
    )


def test_product_python_contains_no_short_gop_literals() -> None:
    """Vierstellige Strings werden von `_gop()` zu gueltigen GOPs aufgefuellt."""
    violations: list[str] = []
    for path in _source_files():
        for node in _constants(path):
            if isinstance(node.value, str) and SHORT_GOP_LITERAL_RE.match(node.value):
                violations.append(f"{path.name}:{node.lineno}: {node.value!r}")

    assert violations == [], (
        "Vierstellige Zahlenstrings werden zu GOPs normalisiert und gehören ins Regelwerk:\n"
        + "\n".join(violations)
    )


def test_product_python_contains_no_numeric_gop_literals() -> None:
    """Der Stringtest allein wuerde eine als Zahl notierte GOP durchlassen."""
    violations: list[str] = []
    for path in _source_files():
        for node in _constants(path):
            if not isinstance(node.value, int) or isinstance(node.value, bool):
                continue
            if 1000 <= node.value <= 99999 and node.value not in ALLOWED_INT_LITERALS:
                violations.append(f"{path.name}:{node.lineno}: {node.value}")

    assert violations == [], (
        "Zahlenliterale im GOP-Wertebereich gehören ins Regelwerk; technische Grenzwerte "
        "gehören in ALLOWED_INT_LITERALS:\n" + "\n".join(violations)
    )


def test_product_python_pins_no_billing_quarter() -> None:
    """Ein fest verdrahtetes Quartal wuerde still gegen den falschen Katalogstand rechnen."""
    violations: list[str] = []
    for path in _source_files():
        for node in _constants(path):
            if not isinstance(node.value, str) or not QUARTER_LITERAL_RE.fullmatch(node.value):
                continue
            violations.append(f"{path.name}:{node.lineno}: {node.value!r}")

    assert violations == [], (
        "Das Leistungsquartal kommt aus Behandlungsdatum, Fallkontext oder Katalogstand, "
        "nicht aus dem Code:\n" + "\n".join(violations)
    )
