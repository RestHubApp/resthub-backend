"""Actualiza la matriz de 35 rutas y 22 diálogos desde corridas reales."""

import json
from pathlib import Path

RAIZ = Path("/mnt/c/Users/JeffryRU/Downloads/RestHub_Pruebas/09-caos")
reportes = RAIZ / "reportes"
archivo = RAIZ / "caos.md"
filas = json.loads((reportes / "04-matriz-final.json").read_text())
for fila in filas:
    if fila["elemento"] in ("ruta:/acceso", "ruta:/plataforma/acceso"):
        fila["estado"] = "no verificado"
        fila["detalle"] = "Acceso público: se requiere probar el envío POST, no la navegación GET."
adicionales = ["04b-observaciones.json", "04c-observaciones.json", "04d-observaciones.json"]
for nombre in adicionales:
    datos = json.loads((reportes / nombre).read_text())
    for dato in datos.get("rutas_corregidas", []):
        for fila in filas:
            if fila["elemento"] == f"ruta:{dato['ruta']}" and fila["falla"] == dato["falla"]:
                fila["estado"] = "error recuperable" if dato["cumple"] else "no verificado"
                fila["detalle"] = f"Revisión: {nombre}; {dato['alertas']}"
                fila["reintentar"] = dato["reintentar"]
                fila["cargando"] = dato["cargando"]
(reportes / "04-matriz-compuesta.json").write_text(json.dumps(filas, ensure_ascii=False, indent=2))
estados = {}
for fila in filas:
    estados.setdefault(fila["elemento"], {})[fila["falla"]] = fila["estado"]
tabla = ["| Ruta o diálogo | 500 | 409 | 503 | Timeout |", "|---|:---:|:---:|:---:|:---:|"]


def marca(resultados: dict[str, str], falla: str) -> str:
    return "✓" if resultados.get(falla) == "error recuperable" else "—"


for elemento, resultados in estados.items():
    tabla.append(
        f"| `{elemento}` | {marca(resultados, '500')} | {marca(resultados, '409')} | "
        f"{marca(resultados, '503')} | {marca(resultados, 'timeout')} |"
    )
texto = archivo.read_text()
if "<!-- MATRIZ -->" in texto:
    texto = texto.replace("<!-- MATRIZ -->", "\n".join(tabla))
else:
    inicio = texto.index("| Ruta o diálogo | 500 |")
    fin = texto.index("### CC-05.", inicio)
    texto = texto[:inicio] + "\n".join(tabla) + "\n\n" + texto[fin:]
archivo.write_text(texto)
print(sum(f["estado"] == "error recuperable" for f in filas), "/", len(filas))
