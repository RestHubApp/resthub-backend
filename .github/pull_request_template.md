## Qué cambia

<!-- Qué hace este PR y por qué. Enlaza el requisito (RFS-xx, RNFS-xx) o el issue. -->

## Cómo se probó

<!-- Pruebas nuevas o cambiadas, y lo que corriste en local. -->

- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run lint-imports && uv run mypy && uv run deptry .`
- [ ] `uv run pytest -q -n auto --cov=resthub`

## Privacidad (PI-04)

- [ ] Ningún log, mensaje de error ni respuesta nueva imprime datos personales en claro
      (correo, teléfono, documento, dirección, IP completa, contraseñas, tokens).
- [ ] Cada campo nuevo con datos personales está clasificado y, si corresponde, se guarda
      cifrado (`EncryptedText`) o con su huella (SHA-256/HMAC) en vez del dato.
- [ ] Nada se copió de producción: los datos de prueba son inventados.
- [ ] Si el cambio guarda datos personales nuevos, tiene plazo de conservación y purga.

## Seguridad

- [ ] Las consultas nuevas de negocio filtran por el `restaurant_id` del token.
- [ ] Los endpoints nuevos exigen su permiso (`require_permission`) y prueban quién recibe 403/404.
- [ ] Los cambios de esquema traen su migración reversible.
