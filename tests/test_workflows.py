"""Los workflows de GitHub Actions corren con el token de solo lectura."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
CHECKOUT = re.compile(r"^(?P<indent> *)- uses: actions/checkout@\S+.*$", re.MULTILINE)


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS.glob("*.yml")), ids=lambda path: path.name)
def test_workflow_declares_its_permissions(workflow: Path) -> None:
    # Sin `permissions:`, el token recibe los permisos por omisión del repositorio o de la
    # organización, que pueden incluir escritura.
    assert re.search(r"^permissions:", workflow.read_text(encoding="utf-8"), re.MULTILINE)


# Los que no empujan: CI y el respaldo diario, que además tiene la URL de la base.
@pytest.mark.parametrize("name", ["ci.yml", "backup.yml"])
def test_checkouts_do_not_persist_the_token(name: str) -> None:
    ci = (WORKFLOWS / name).read_text(encoding="utf-8")
    checkouts = list(CHECKOUT.finditer(ci))

    assert checkouts
    for checkout in checkouts:
        # El bloque del paso: hasta el siguiente paso o el siguiente job.
        rest = ci[checkout.end() :]
        end = re.search(rf"^{checkout.group('indent')}- |^  \S", rest, re.MULTILINE)
        step = rest[: end.start() if end else len(rest)]
        # Ningún job de estos empuja: el token no se deja en .git/config, donde lo leería
        # cualquier paquete que traigan `npx jscpd`, `uvx semgrep` o `uv sync`.
        assert "persist-credentials: false" in step, checkout.group(0)
