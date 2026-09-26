"""Administración del sistema: el equipo de RestHub, que no pertenece a ningún restaurante.

Da de alta restaurantes y a su primer encargado, los activa o desactiva y deja
todo en su propia bitácora. Sus cuentas viven en `platform_admins`, no en
`users`, y sus tokens llevan otro alcance: no hay forma de que una cuenta de
plataforma opere como personal de un local ni al revés.

Lo que el alta de un restaurante tiene que hacer en `restaurants` y `accounts`
lo pide por el puerto `RestaurantProvisioning`; lo implementa la raíz de
composición (`wiring/restaurant_provisioning.py`).
"""
