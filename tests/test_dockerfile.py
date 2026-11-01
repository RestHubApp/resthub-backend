"""La imagen declara su propio sondeo de salud (`Dockerfile`)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.conftest import API_PREFIX

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def test_dockerfile_healthcheck_probes_health_without_curl() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    pattern = r"^HEALTHCHECK (?P<options>.*?) CMD (?P<command>\[.*\])$"
    match = re.search(pattern, dockerfile, re.MULTILINE)

    # Sin HEALTHCHECK, Swarm da la tarea por buena apenas arranca y una migración
    # rota deja el API caído en vez de mantener el despliegue anterior.
    assert match is not None, "Falta el HEALTHCHECK en el Dockerfile"
    command = json.loads(match.group("command"))
    assert command[0] == "python"
    assert f"{API_PREFIX}/health" in command[-1]
    # La imagen slim no trae curl ni wget: un sondeo con ellos nunca pasaría.
    assert not {"curl", "wget"} & set(" ".join(command).split())
    # El margen de arranque cubre `alembic upgrade head`.
    assert "--start-period=" in match.group("options")
