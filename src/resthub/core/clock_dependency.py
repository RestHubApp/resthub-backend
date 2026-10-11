"""Dependencia de FastAPI del `Clock`: un solo punto que las pruebas reemplazan."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from resthub.core.clock import Clock, SystemClock


def get_clock() -> Clock:
    return SystemClock()


ClockDep = Annotated[Clock, Depends(get_clock)]
