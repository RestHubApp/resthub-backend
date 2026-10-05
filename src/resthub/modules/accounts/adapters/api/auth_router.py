"""Adaptador de entrada HTTP para el acceso y la sesión propia.

Traduce credenciales a un token y errores de dominio a códigos de estado. No
decide nada: la regla de quién puede entrar vive en el caso de uso.

También deja el rastro de seguridad del acceso en los logs: ingresos, ingresos
fallidos y cambios de contraseña. Vive acá y no en los casos de uso porque
escribir un log es tecnología, y el dominio no la conoce.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import (
    UNAUTHENTICATED_HEADERS,
    PrincipalDep,
    SessionDep,
    TokenServiceDep,
    client_address,
    unauthenticated,
)
from resthub.core.login_throttle import LoginThrottle, get_login_throttle
from resthub.core.logs import get_logger, mask_email
from resthub.modules.accounts.adapters.api.dependencies import (
    ClockDep,
    PasswordHasherDep,
    PlatformAdminDirectoryDep,
    PreviewCodesDep,
    RestaurantDirectoryDep,
    UserRepositoryDep,
)
from resthub.modules.accounts.adapters.api.schemas import (
    AcceptTermsRequest,
    AccessTokenResponse,
    ChangeOwnPasswordRequest,
    LoginRequest,
    PreviewExchangeRequest,
    SessionResponse,
)
from resthub.modules.accounts.domain.exceptions import (
    InactiveAccount,
    InactiveRestaurant,
    InvalidCredentials,
    InvalidPreviewCode,
    OutdatedTerms,
    PreviewSessionRestricted,
    UserNotFound,
    WeakPassword,
    WrongCurrentPassword,
)
from resthub.modules.accounts.use_cases.accept_terms import AcceptTerms, AcceptTermsCommand
from resthub.modules.accounts.use_cases.authenticate_user import (
    AuthenticateUser,
    AuthenticateUserCommand,
)
from resthub.modules.accounts.use_cases.change_own_password import (
    ChangeOwnPassword,
    ChangeOwnPasswordCommand,
)
from resthub.modules.accounts.use_cases.preview import ExchangePreviewCode
from resthub.modules.accounts.use_cases.read_session import ReadCurrentSession

router = APIRouter()
logger = get_logger("resthub.auth")
ThrottleDep = Annotated[LoginThrottle, Depends(get_login_throttle)]


@router.post("/login", response_model=AccessTokenResponse, summary="Obtener un token de acceso")
async def login(
    payload: LoginRequest,
    request: Request,
    throttle: ThrottleDep,
    users: UserRepositoryDep,
    restaurants: RestaurantDirectoryDep,
    hasher: PasswordHasherDep,
    tokens: TokenServiceDep,
    activity: ActivityRecorderDep,
) -> AccessTokenResponse:
    email, address = str(payload.email), client_address(request)
    wait = throttle.retry_after(email, address)
    if wait:
        logger.warning("auth.login_throttled", email=mask_email(email), retry_after=wait)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Demasiados intentos fallidos. Vuelve a intentar en {wait // 60 + 1} minutos.",
            headers={"Retry-After": str(wait)},
        )
    use_case = AuthenticateUser(users, restaurants, hasher, tokens, activity)
    try:
        result = await use_case(
            AuthenticateUserCommand(email=str(payload.email), password=payload.password)
        )
    except InvalidCredentials as error:
        throttle.failed(email, address)
        # Aviso y no información: varios seguidos contra la misma cuenta son
        # la señal de un intento de adivinar la contraseña.
        logger.warning(
            "auth.login_failed", reason="invalid_credentials", email=mask_email(str(payload.email))
        )
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, str(error), headers=UNAUTHENTICATED_HEADERS
        ) from error
    except (InactiveAccount, InactiveRestaurant) as error:
        logger.warning(
            "auth.login_failed", reason=type(error).__name__, email=mask_email(str(payload.email))
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from error

    throttle.succeeded(email, address)
    user = result.session.user
    logger.info(
        "auth.login_succeeded",
        user_id=user.id,
        role_id=user.role.id,
        restaurant_id=user.restaurant_id,
    )
    return AccessTokenResponse.issued(
        result.session, result.token.value, result.token.expires_in_seconds
    )


@router.post(
    "/preview",
    response_model=AccessTokenResponse,
    summary="Canjear un código de vista previa por una sesión del local de muestra",
)
async def exchange_preview_code(
    payload: PreviewExchangeRequest,
    codes: PreviewCodesDep,
    users: UserRepositoryDep,
    restaurants: RestaurantDirectoryDep,
    admins: PlatformAdminDirectoryDep,
    tokens: TokenServiceDep,
    activity: ActivityRecorderDep,
    clock: ClockDep,
    session: SessionDep,
) -> AccessTokenResponse:
    """Lo llama la pestaña nueva que abre la administración del sistema.

    Sin autenticación previa: el código es la credencial. Sirve una sola vez y
    por 60 segundos; el token que entrega dura 30 minutos y no se renueva.
    """
    try:
        result = await ExchangePreviewCode(
            codes, users, restaurants, admins, tokens, activity, clock
        )(payload.code)
    except InvalidPreviewCode as error:
        logger.warning("auth.preview_rejected")
        raise unauthenticated(str(error)) from error

    # Confirmado antes de responder, no después: la sesión de la petición
    # confirma cuando la respuesta ya salió, y un token entregado con el código
    # todavía sin marcar usado dejaría una ventana para canjearlo otra vez.
    await session.commit()
    user = result.session.user
    logger.info(
        "auth.preview_started",
        user_id=user.id,
        role_id=user.role.id,
        restaurant_id=user.restaurant_id,
    )
    return AccessTokenResponse.issued(
        result.session, result.token.value, result.token.expires_in_seconds, preview=True
    )


@router.post(
    "/refresh", response_model=AccessTokenResponse, summary="Renovar el token de la sesión"
)
async def refresh_token(
    principal: PrincipalDep,
    users: UserRepositoryDep,
    restaurants: RestaurantDirectoryDep,
    tokens: TokenServiceDep,
) -> AccessTokenResponse:
    """Un token nuevo para una sesión que sigue válida.

    El celular del mesero lo pide antes de que venza el actual, así el turno
    no se corta cada hora. Una cuenta desactivada no llega acá: el principal
    ya se validó contra la base.

    Una vista previa no se renueva: vence a los 30 minutos y se vuelve a abrir
    desde la plataforma, que la deja otra vez en su bitácora.
    """
    if principal.preview:
        raise unauthenticated("La vista previa no se renueva: ábrela otra vez desde la plataforma.")
    try:
        session = await ReadCurrentSession(users, restaurants)(principal.user_id)
    except UserNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "La cuenta ya no existe.") from error
    token = tokens.issue(principal.user_id, principal.restaurant_id)
    return AccessTokenResponse.issued(session, token.value, token.expires_in_seconds)


@router.get("/me", response_model=SessionResponse, summary="Sesión de la cuenta que pregunta")
async def read_current_session(
    principal: PrincipalDep, users: UserRepositoryDep, restaurants: RestaurantDirectoryDep
) -> SessionResponse:
    # El principal solo trae identificadores, restaurante y permisos. El
    # perfil lo posee este módulo, así que acá sí se lee la entidad entera.
    try:
        session = await ReadCurrentSession(users, restaurants)(principal.user_id)
    except UserNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "La cuenta ya no existe.") from error
    return SessionResponse.from_session(session, preview=principal.preview)


@router.post(
    "/me/terms",
    response_model=SessionResponse,
    summary="Aceptar los términos de uso y la política de privacidad",
)
async def accept_terms(
    payload: AcceptTermsRequest,
    principal: PrincipalDep,
    users: UserRepositoryDep,
    restaurants: RestaurantDirectoryDep,
    activity: ActivityRecorderDep,
) -> SessionResponse:
    """Ley N.º 29733: la cuenta acepta la versión vigente antes de trabajar.

    Responde la sesión ya al día. Si la versión leída no es la vigente, 409:
    la interfaz vuelve a mostrar el texto nuevo.
    """
    try:
        await AcceptTerms(users, activity)(
            AcceptTermsCommand(
                user_id=principal.user_id, version=payload.version, preview=principal.preview
            )
        )
        session = await ReadCurrentSession(users, restaurants)(principal.user_id)
    except PreviewSessionRestricted as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from error
    except UserNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    except OutdatedTerms as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    logger.info("auth.terms_accepted", user_id=principal.user_id, version=payload.version)
    return SessionResponse.from_session(session, preview=principal.preview)


@router.post(
    "/me/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cambiar la contraseña propia",
)
async def change_own_password(
    payload: ChangeOwnPasswordRequest,
    principal: PrincipalDep,
    users: UserRepositoryDep,
    hasher: PasswordHasherDep,
    activity: ActivityRecorderDep,
) -> Response:
    try:
        await ChangeOwnPassword(users, hasher, activity)(
            ChangeOwnPasswordCommand(
                user_id=principal.user_id,
                current_password=payload.current_password,
                new_password=payload.new_password,
                preview=principal.preview,
            )
        )
    except PreviewSessionRestricted as error:
        # 403 y no 401: la credencial es válida, lo que no cabe es la acción.
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from error
    except UserNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    except WrongCurrentPassword as error:
        logger.warning("auth.password_change_rejected", user_id=principal.user_id)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    except WeakPassword as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    logger.info("auth.password_changed", user_id=principal.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
