# Política de seguridad

Cómo reportar una vulnerabilidad de RestHub y en qué plazo se corrige, tanto en el
código propio como en las dependencias (OWASP ASVS 5.0, V15.1.1 y V15.2.1). La lista
de chequeo completa está en [`docs/asvs-5.0.md`](docs/asvs-5.0.md).

## Reportar una vulnerabilidad

- **En privado, nunca en un issue público ni en un PR.** Usa el reporte privado de
  GitHub: pestaña *Security* del repositorio → *Report a vulnerability* (crea un
  *security advisory* privado que solo ve el equipo). Si la opción no aparece,
  escribe a un miembro del equipo por un canal privado y pídele que abra el advisory.
- Incluye qué endpoint o archivo, cómo reproducirlo, qué datos o cuentas quedan
  expuestos y desde qué versión (`CHANGELOG.md` o la etiqueta `vX.Y.Z`).
- No pruebes contra producción con datos reales ni intentes leer datos de otro
  restaurante: usa un entorno local (`README.md`, «Correr en local») o el local de
  muestra.
- El equipo confirma la recepción en **2 días hábiles**, asigna la severidad y avisa
  cuándo sale la corrección. Se publica el advisory cuando ya hay versión corregida.

## Severidad

La severidad sale del puntaje CVSS v3.1 o v4.0 (el del advisory de la dependencia o
el que asigne el equipo a un hallazgo propio). El equipo puede subirla si el fallo
expone datos personales (Ley N.º 29733), credenciales SOL, el certificado digital o
cruza la frontera entre restaurantes, aunque el puntaje diga otra cosa.

| Severidad | CVSS | Ejemplos en RestHub |
|---|---|---|
| Crítica | 9,0 – 10,0 | Leer o escribir datos de otro restaurante, saltar el login o la verificación en dos pasos, ejecutar código en el servidor, fuga de `JWT_SECRET_KEY` o `DATA_ENCRYPTION_KEY`. |
| Alta | 7,0 – 8,9 | Escalar permisos dentro del local, leer datos personales en claro, robo de sesión. |
| Media | 4,0 – 6,9 | Una defensa en profundidad que falta (cabecera, límite), una fuga de información menor. |
| Baja | 0,1 – 3,9 | Endurecimiento, detalles sin un ataque práctico. |

## Plazos de remediación

Valen igual para el código propio y para las dependencias (PyPI en este repositorio,
npm en el frontend). Corren desde que el equipo conoce el problema: el reporte, el
advisory o el fallo del CI. «Remediar» es que la corrección esté en producción, o que
quede una mitigación probada y una excepción registrada (ver abajo).

| Severidad | Plazo |
|---|---|
| Crítica | 72 horas |
| Alta | 7 días |
| Media | 30 días |
| Baja | 90 días |

Fuera de las alertas, las dependencias se mantienen al día: los PR de Dependabot se
revisan en la semana en que se abren, y una versión mayor pendiente no pasa de 90
días sin decisión (actualizar o dejar escrito por qué no).

## Cómo se revisan las dependencias

- **En cada push y PR** (`.github/workflows/ci.yml`, job `seguridad`): `pip-audit
  --strict` sobre todo `uv.lock` (`uv export --locked --all-groups`), Bandit,
  Semgrep y gitleaks. Una vulnerabilidad conocida bloquea la integración.
- **Cada semana**, aunque nadie toque el código
  (`.github/workflows/auditoria-dependencias.yml`, lunes 08:13 UTC y a mano desde
  Actions): el mismo `pip-audit`. Una vulnerabilidad publicada después del último
  push hace fallar la corrida, y GitHub avisa del fallo a quien tenga activadas las
  notificaciones de Actions. Ese aviso es el inicio del plazo.
- **Dependabot** (`.github/dependabot.yml`): PR semanales de `uv` y mensuales de las
  acciones de GitHub, agrupados, contra `develop`. El `cooldown` de 7 días deja pasar
  una semana desde que se publica una versión antes de proponerla, para no traer una
  versión retirada o comprometida. Ese plazo es para las actualizaciones de versión;
  una corrección de seguridad no espera la semana: se actualiza a mano con
  `uv lock --upgrade-package <paquete>` dentro del plazo de su severidad.
- **Frontend**: `pnpm audit --audit-level high` en su CI y su propio Dependabot
  (repositorio `resthub-frontend`). Los plazos de esta página valen igual.
- **SBOM**: cada release adjunta `sbom.cdx.json` (CycloneDX, generado de `uv.lock`
  por `.github/workflows/release.yml`). Sirve para saber, ante un advisory nuevo, qué
  versión publicada trae el paquete afectado.

Al triar una alerta: confirmar que el paquete y la versión están en `uv.lock`, ver si
la función vulnerable se usa (y en qué grupo: producción o solo desarrollo), asignar
la severidad y abrir el PR. Una dependencia que solo está en un grupo de desarrollo
(`dev`, `fuzz`, `chaos`, `carbon`…) no llega a la imagen, pero sigue el mismo plazo:
corre en el CI con acceso al repositorio.

## Excepciones

Cuando no se puede corregir a tiempo (no hay versión arreglada, o el arreglo rompe
algo), la excepción queda escrita aquí, con fecha de vencimiento, y no solo en el
workflow:

1. Agregar una fila a la tabla de abajo, en el mismo PR que la introduce.
2. Si `pip-audit` tiene que ignorar el aviso, agregar `--ignore-vuln <ID>` en
   **los dos** workflows (`ci.yml` y `auditoria-dependencias.yml`) con un comentario
   `# Excepción hasta AAAA-MM-DD, ver SECURITY.md`.
3. Al vencer, se renueva con un motivo nuevo (otra fila) o se corrige. Una excepción
   vencida cuenta como una vulnerabilidad abierta con el plazo ya cumplido.

El vencimiento no pasa del plazo de la severidad contado desde la fecha de alta, salvo
que no exista una versión corregida; en ese caso, 30 días y se vuelve a revisar.

| ID | Paquete | Severidad | Motivo y mitigación | Alta | Vence | Responsable |
|---|---|---|---|---|---|---|
| — | — | — | Hoy no hay excepciones en el backend. | — | — | — |
