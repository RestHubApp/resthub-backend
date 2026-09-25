"""Módulos de dominio.

Viven bajo un paquete común para que los contratos de arquitectura puedan
nombrarlos con un comodín exacto. Con los módulos colgando de la raíz, junto a
`core` y a `main`, la regla de independencia tenía que listar excepciones para
no acusar al núcleo compartido ni a la raíz de composición. Acá `resthub.modules.*`
son todos los módulos de dominio y nada más.
"""
