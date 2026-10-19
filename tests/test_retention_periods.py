"""Los plazos de conservación del Anexo J (RNFP-12), leídos de donde se aplican.

La telemetría (14 días) y el historial de accesos (90 días) tienen además sus
pruebas de purga (`test_telemetry_capture.py`, `test_access_history.py`). Los
respaldos los borra GitHub: el plazo vive en el workflow y se comprueba aquí.
"""

from __future__ import annotations

import re
from pathlib import Path

from resthub.core.config import Settings

ROOT = Path(__file__).resolve().parents[1]
BACKUP_WORKFLOW = ROOT / ".github" / "workflows" / "backup.yml"


def _step(workflow: str, name: str) -> str:
    """El texto de un paso del workflow, desde su `- name:` hasta el siguiente."""
    match = re.search(rf"- name: {re.escape(name)}\n(.*?)(?=\n      - |\Z)", workflow, re.DOTALL)
    assert match is not None, f"Falta el paso «{name}» en backup.yml"
    return match.group(1)


def test_backups_live_30_days_and_only_encrypted() -> None:
    workflow = BACKUP_WORKFLOW.read_text(encoding="utf-8")
    upload = _step(workflow, "Guardar el respaldo")
    encrypt = _step(workflow, "Cifrar el respaldo")

    assert re.search(r"^\s*retention-days: 30$", upload, re.MULTILINE)
    assert "age --encrypt" in encrypt
    # El volcado en claro se borra y no queda nada sin cifrar antes de subir.
    assert "shred --remove" in encrypt
    assert "! -name '*.age'" in encrypt
    assert workflow.index("Cifrar el respaldo") < workflow.index("Guardar el respaldo")


def test_the_other_periods_by_default() -> None:
    defaults = Settings.model_fields

    assert defaults["observability_retention_days"].default == 14
    assert defaults["access_history_retention_days"].default == 90
