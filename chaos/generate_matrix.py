"""Actualiza la matriz de 35 rutas y 22 diálogos desde corridas reales."""

import json
from pathlib import Path

ROOT = Path("/mnt/c/Users/JeffryRU/Downloads/RestHub_Pruebas/09-caos")
reports = ROOT / "reportes"
file_path = ROOT / "caos.md"
rows = json.loads((reports / "04-matriz-final.json").read_text())
for row in rows:
    if row["elemento"] in ("ruta:/acceso", "ruta:/plataforma/acceso"):
        row["estado"] = "no verificado"
        row["detalle"] = "Acceso público: se requiere probar el envío POST, no la navegación GET."
extra_files = ["04b-observaciones.json", "04c-observaciones.json", "04d-observaciones.json"]
for name in extra_files:
    data = json.loads((reports / name).read_text())
    for entry in data.get("rutas_corregidas", []):
        for row in rows:
            if row["elemento"] == f"ruta:{entry['ruta']}" and row["falla"] == entry["falla"]:
                row["estado"] = "error recuperable" if entry["cumple"] else "no verificado"
                row["detalle"] = f"Revisión: {name}; {entry['alertas']}"
                row["reintentar"] = entry["reintentar"]
                row["cargando"] = entry["cargando"]
(reports / "04-matriz-compuesta.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
states = {}
for row in rows:
    states.setdefault(row["elemento"], {})[row["falla"]] = row["estado"]
table = ["| Ruta o diálogo | 500 | 409 | 503 | Timeout |", "|---|:---:|:---:|:---:|:---:|"]


def mark(results: dict[str, str], fault: str) -> str:
    return "✓" if results.get(fault) == "error recuperable" else "—"


for element, results in states.items():
    table.append(
        f"| `{element}` | {mark(results, '500')} | {mark(results, '409')} | "
        f"{mark(results, '503')} | {mark(results, 'timeout')} |"
    )
text = file_path.read_text()
if "<!-- MATRIZ -->" in text:
    text = text.replace("<!-- MATRIZ -->", "\n".join(table))
else:
    start = text.index("| Ruta o diálogo | 500 |")
    end = text.index("### CC-05.", start)
    text = text[:start] + "\n".join(table) + "\n\n" + text[end:]
file_path.write_text(text)
print(sum(f["estado"] == "error recuperable" for f in rows), "/", len(rows))
