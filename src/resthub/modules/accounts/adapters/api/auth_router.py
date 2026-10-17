"""Adaptador de entrada HTTP para el acceso y la sesión propia.

Traduce credenciales a un token y errores de dominio a códigos de estado. No
decide nada: la regla de quién puede entrar vive en el caso de uso.

También deja el rastro de seguridad del acceso en los logs: ingresos, ingresos
fallidos y cambios de contraseña. Vive acá y no en los casos de uso porque
escribir un log es tecnología, y el dominio no la conoce.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status

from resthub.core.access_history import (
    PLATFORM_ACCOUNT,
    STAFF_ACCOUNT,
    AccessEntry,
    AccessHistoryDep,
    AccessOutcome,
    LoginAttempt,
)
from resthub.core.activity import ActivityKind
from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import (
    UNAUTHENTICATED_HEADERS,
    PlatformTokenServiceDep,
    PrincipalDep,
    SessionSubjectDep,
    TemporaryPasswordPrincipalDep,
    TokenServiceDep,
    client_address,
    unauthenticated,
)
from resthub.core.clock_dependency import ClockDep
from resthub.core.database import SessionDep
from resthub.core.error_codes import ErrorCode
from resthub.core.errors import ApiError, api_error
from resthub.core.identity import PlatformClaims
from resthub.core.login_attempts import LoginGuardDep
from resthub.core.logs import get_logger, mask_email
from resthub.core.pagination import DEFAULT_PAGE_SIZE, MAX_OFFSET, MAX_PAGE_SIZE, PageRequest
from resthub.core.permissions import Permission
from resthub.core.realtime import ACCESS_ALERT_TOPIC, RealtimeEvent
from resthub.core.realtime_broker import EventPublisherDep
from resthub.core.request_context import PLATFORM, annotate_account
from resthub.core.session_cookie import (
    CSRF_COOKIE,
    PLATFORM_COOKIE,
    STAFF_COOKIE,
    attach_session,
    clear_session,
)
from resthub.modules.accounts.adapters.api.dependencies import (
    PasswordHasherDep,
    PlatformAdminDirectoryDep,
    PlatformSignInDep,
    PreviewCodesDep,
    RestaurantDirectoryDep,
    UserRepositoryDep,
)
from resthub.modules.accounts.adapters.api.schemas import (
    AcceptTermsRequest,
    AccessHistoryPageResponse,
    AccessTokenResponse,
    ChangeOwnPasswordRequest,
    LoginRequest,
    PlatformAdminAccessTokenResponse,
    PlatformAdminSessionResponse,
    PreviewExchangeRequest,
    SessionResponse,
    StaffAccessTokenResponse,
    StaffSessionResponse,
)
from resthub.modules.accounts.domain.exceptions import (
    InactiveRestaurantError,
    InvalidCredentialsError,
    InvalidPreviewCodeError,
    OutdatedTermsError,
    PreviewSessionRestrictedError,
    UserNotFoundError,
    WeakPasswordError,
    WrongCurrentPasswordError,
)
from resthub.modules.accounts.ports.platform_sign_in import PlatformAccount
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


@router.post(
    "/login",
    response_model=AccessTokenResponse,
    summary="Obtener un token de acceso (personal o administración del sistema)",
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    throttle: LoginGuardDep,
    users: UserRepositoryDep,
    restaurants: RestaurantDirectoryDep,
    hasher: PasswordHasherDep,
    tokens: TokenServiceDep,
    activity: ActivityRecorderDep,
    platform: PlatformSignInDep,
    history: AccessHistoryDep,
    events: EventPublisherDep,
    session: SessionDep,
) -> StaffAccessTokenResponse | PlatformAdminAccessTokenResponse:
    """Un solo acceso para las dos clases de cuenta.

    Prueba primero al personal de los restaurantes; si no hay una credencial
    válida del personal, a la administración del sistema. La respuesta trae
    `kind` (`staff` o `platform`) y la sesión que corresponde. Una credencial
    que no sirve en ninguna responde el mismo 401 y gasta el mismo contador de
    intentos, así que no revela de qué clase es el correo.
    """
    email, address = str(payload.email), client_address(request)

    async def remember(outcome: AccessOutcome) -> AccessEntry:
        # Un acceso correcto se anota en la sesión de la petición; un fallo, aparte.
        return await history.record(
            LoginAttempt(email, outcome, address, request.headers.get("user-agent", "")),
            session if outcome is AccessOutcome.SUCCEEDED else None,
        )

    # Reserva el intento antes del primer `await`: así cuenta aunque lleguen
    # muchos a la vez, y un acceso correcto lo libera al final.
    wait = await throttle.acquire(email, address)
    if wait:
        await remember(AccessOutcome.THROTTLED)
        logger.warning("auth.login_throttled", email=mask_email(email), retry_after=wait)
        raise ApiError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Demasiados intentos fallidos. Vuelve a intentar en {wait // 60 + 1} minutos.",
            ErrorCode.LOGIN_THROTTLED,
            {"Retry-After": str(wait)},
        )
    use_case = AuthenticateUser(users, restaurants, hasher, tokens, activity)
    try:
        result = await use_case(
            AuthenticateUserCommand(email=str(payload.email), password=payload.password)
        )
    except InvalidCredentialsError as error:
        # No es del personal: queda probar con la administración del sistema.
        signed_in = await platform.authenticate(email, payload.password)
        if signed_in is not None:
            await throttle.succeeded(email, address, session)
            await remember(AccessOutcome.SUCCEEDED)
            logger.info("platform.login_succeeded", admin_id=signed_in.account.id)
            return with_session_cookie(
                response,
                PlatformAdminAccessTokenResponse.issued(
                    signed_in.account, signed_in.token.value, signed_in.token.expires_in_seconds
                ),
            )
        await remember(AccessOutcome.FAILED)
        # Aviso y no información: varios seguidos contra la misma cuenta son
        # la señal de un intento de adivinar la contraseña.
        logger.warning(
            "auth.login_failed", reason="invalid_credentials", email=mask_email(str(payload.email))
        )
        raise api_error(
            status.HTTP_401_UNAUTHORIZED, error, headers=UNAUTHENTICATED_HEADERS
        ) from error
    except InactiveRestaurantError as error:
        await remember(AccessOutcome.BLOCKED)
        logger.warning(
            "auth.login_failed", reason=type(error).__name__, email=mask_email(str(payload.email))
        )
        raise api_error(status.HTTP_403_FORBIDDEN, error) from error

    await throttle.succeeded(email, address, session)
    user = result.session.user
    _alert_if_unfamiliar(events, await remember(AccessOutcome.SUCCEEDED))
    logger.info(
        "auth.login_succeeded",
        user_id=user.id,
        role_id=user.role.id,
        restaurant_id=user.restaurant_id,
    )
    return with_session_cookie(
        response,
        StaffAccessTokenResponse.issued(
            result.session, result.token.value, result.token.expires_in_seconds
        ),
    )


def with_session_cookie[Issued: StaffAccessTokenResponse | PlatformAdminAccessTokenResponse](
    response: Response, issued: Issued
) -> Issued:
    """Deja el token también en su cookie `HttpOnly` y suma el valor anti-CSRF (S2)."""
    cookie = PLATFORM_COOKIE if issued.kind == "platform" else STAFF_COOKIE
    issued.csrf_token = attach_session(response, cookie, issued.access_token, issued.expires_in)
    return issued


def _alert_if_unfamiliar(events: EventPublisherDep, entry: AccessEntry) -> None:
    """Dispositivo o país nuevo (RFS-16): avisa a la cuenta y a quien gestiona al personal."""
    if not (entry.new_device or entry.new_country) or entry.restaurant_id is None:
        return
    logger.warning(
        "auth.unfamiliar_access",
        user_id=entry.account_id,
        new_device=entry.new_device,
        new_country=entry.new_country,
    )
    events.publish(
        RealtimeEvent(
            restaurant_id=entry.restaurant_id,
            topic=ACCESS_ALERT_TOPIC,
            user_ids=frozenset({entry.account_id or 0}),
            permissions=frozenset({Permission.STAFF_MANAGE.value}),
            reference_id=entry.id,
        )
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cerrar la sesión y dejarlo en la bitácora",
)
async def logout(
    principal: TemporaryPasswordPrincipalDep, activity: ActivityRecorderDep, response: Response
) -> None:
    """Registra la salida del personal en la bitácora; responde 204.

    El token NO se invalida: la sesión se corta con `sessions_valid_since`, que
    es por cuenta y cerraría también las sesiones de los demás dispositivos, así
    que el token sigue vigente hasta vencer. La interfaz debe descartarlo. Una
    vista previa no deja asiento: sus entradas y salidas son de la plataforma.
    """
    if principal.preview:
        return
    clear_session(response)
    await activity.record(principal.restaurant_id, principal.user_id, ActivityKind.SIGNED_OUT)
    logger.info("auth.logout", user_id=principal.user_id, restaurant_id=principal.restaurant_id)


@router.post(
    "/preview",
    response_model=StaffAccessTokenResponse,
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
) -> StaffAccessTokenResponse:
    """Lo llama la pestaña nueva que abre la administración del sistema.

    Sin autenticación previa: el código es la credencial. Sirve una sola vez y
    por 60 segundos; el token que entrega dura 30 minutos y no se renueva.
    """
    try:
        result = await ExchangePreviewCode(
            codes=codes,
            users=users,
            restaurants=restaurants,
            admins=admins,
            tokens=tokens,
            activity=activity,
            clock=clock,
        )(payload.code)
    except InvalidPreviewCodeError as error:
        logger.warning("auth.preview_rejected")
        raise unauthenticated(str(error), error.code) from error

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
    return StaffAccessTokenResponse.issued(
        result.session, result.token.value, result.token.expires_in_seconds, preview=True
    )


@router.post(
    "/refresh", response_model=AccessTokenResponse, summary="Renovar el token de la sesión"
)
async def refresh_token(
    subject: SessionSubjectDep,
    response: Response,
    users: UserRepositoryDep,
    restaurants: RestaurantDirectoryDep,
    platform: PlatformSignInDep,
    tokens: TokenServiceDep,
    platform_tokens: PlatformTokenServiceDep,
) -> StaffAccessTokenResponse | PlatformAdminAccessTokenResponse:
    """Un token nuevo para una sesión que sigue válida.

    El celular del mesero lo pide antes de que venza el actual, así el turno
    no se corta cada hora. Una cuenta desactivada no llega acá: el principal
    ya se validó contra la base. Sirve a las dos clases de cuenta y responde la
    variante que corresponda al token que llegó.

    Una vista previa no se renueva: vence a los 30 minutos y se vuelve a abrir
    desde la plataforma, que la deja otra vez en su bitácora.
    """
    if isinstance(subject, PlatformClaims):
        account = await _read_platform_account(platform, subject)
        token = platform_tokens.issue_platform(account.id)
        return with_session_cookie(
            response,
            PlatformAdminAccessTokenResponse.issued(account, token.value, token.expires_in_seconds),
        )
    principal = subject
    if principal.preview:
        message = "La vista previa no se renueva: ábrela otra vez desde la plataforma."
        raise unauthenticated(message)
    try:
        session = await ReadCurrentSession(users, restaurants)(principal.user_id)
    except UserNotFoundError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, "La cuenta ya no existe.", error.code) from error
    token = tokens.issue(principal.user_id, principal.restaurant_id)
    return with_session_cookie(
        response, StaffAccessTokenResponse.issued(session, token.value, token.expires_in_seconds)
    )


async def _read_platform_account(
    platform: PlatformSignInDep, claims: PlatformClaims
) -> PlatformAccount:
    """La cuenta de plataforma releída de la base; sin ella, la sesión no sigue."""
    account = await platform.read(claims.admin_id, claims.issued_at)
    if account is None:
        message = "La cuenta ya no está disponible."
        raise unauthenticated(message)
    # Para la telemetría del panel de observabilidad: quién hizo la petición.
    annotate_account(PLATFORM, account_id=account.id)
    return account


@router.get("/me", response_model=SessionResponse, summary="Sesión de la cuenta que pregunta")
async def read_current_session(
    subject: SessionSubjectDep,
    request: Request,
    users: UserRepositoryDep,
    restaurants: RestaurantDirectoryDep,
    platform: PlatformSignInDep,
) -> StaffSessionResponse | PlatformAdminSessionResponse:
    """La sesión, con el valor anti-CSRF de la cookie si la hay.

    Así una pestaña que perdió el `csrf_token` (o una recarga que no lo guardó)
    lo recupera sin volver a entrar: este GET no lo pide. Solo lo leen los
    orígenes que CORS permite.
    """
    if isinstance(subject, PlatformClaims):
        platform_session = PlatformAdminSessionResponse.from_account(
            await _read_platform_account(platform, subject)
        )
        platform_session.csrf_token = request.cookies.get(CSRF_COOKIE)
        return platform_session
    principal = subject
    # El principal solo trae identificadores, restaurante y permisos. El
    # perfil lo posee este módulo, así que acá sí se lee la entidad entera.
    try:
        session = await ReadCurrentSession(users, restaurants)(principal.user_id)
    except UserNotFoundError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, "La cuenta ya no existe.", error.code) from error
    staff_session = StaffSessionResponse.from_session(session, preview=principal.preview)
    if not principal.preview:
        staff_session.csrf_token = request.cookies.get(CSRF_COOKIE)
    return staff_session


@router.post(
    "/me/terms",
    response_model=StaffSessionResponse,
    summary="Aceptar por separado los términos y el tratamiento de datos personales",
)
async def accept_terms(
    payload: AcceptTermsRequest,
    principal: PrincipalDep,
    users: UserRepositoryDep,
    restaurants: RestaurantDirectoryDep,
    activity: ActivityRecorderDep,
    clock: ClockDep,
) -> StaffSessionResponse:
    """Ley N.º 29733: dos consentimientos separados antes de trabajar.

    La cuenta acepta los términos y condiciones y, aparte, el tratamiento de sus
    datos personales. Responde la sesión ya al día. Si alguna versión leída no es
    la vigente, 409: la interfaz vuelve a mostrar el texto nuevo.
    """
    try:
        await AcceptTerms(users, activity, clock)(
            AcceptTermsCommand(
                user_id=principal.user_id,
                terms_version=payload.terms_version,
                privacy_version=payload.privacy_version,
                preview=principal.preview,
            )
        )
        session = await ReadCurrentSession(users, restaurants)(principal.user_id)
    except PreviewSessionRestrictedError as error:
        raise api_error(status.HTTP_403_FORBIDDEN, error) from error
    except UserNotFoundError as error:
        raise api_error(status.HTTP_404_NOT_FOUND, error) from error
    except OutdatedTermsError as error:
        raise api_error(status.HTTP_409_CONFLICT, error) from error
    logger.info(
        "auth.terms_accepted",
        user_id=principal.user_id,
        terms_version=payload.terms_version,
        privacy_version=payload.privacy_version,
    )
    return StaffSessionResponse.from_session(session, preview=principal.preview)


@router.post(
    "/me/password",
    response_model=StaffAccessTokenResponse,
    summary="Cambiar la contraseña propia",
)
async def change_own_password(
    payload: ChangeOwnPasswordRequest,
    principal: TemporaryPasswordPrincipalDep,
    response: Response,
    users: UserRepositoryDep,
    restaurants: RestaurantDirectoryDep,
    hasher: PasswordHasherDep,
    activity: ActivityRecorderDep,
    tokens: TokenServiceDep,
    clock: ClockDep,
) -> StaffAccessTokenResponse:
    """Cambia la contraseña, cierra las demás sesiones y devuelve una nueva.

    El cambio corta todos los tokens emitidos antes, el de esta petición
    incluido. La respuesta trae el token nuevo y la sesión, con la forma del
    login, para que quien cambió la contraseña siga dentro sin volver a entrar.
    """
    try:
        await ChangeOwnPassword(users, hasher, activity, clock)(
            ChangeOwnPasswordCommand(
                user_id=principal.user_id,
                current_password=payload.current_password,
                new_password=payload.new_password,
                preview=principal.preview,
            )
        )
    except PreviewSessionRestrictedError as error:
        # 403 y no 401: la credencial es válida, lo que no cabe es la acción.
        raise api_error(status.HTTP_403_FORBIDDEN, error) from error
    except UserNotFoundError as error:
        raise api_error(status.HTTP_404_NOT_FOUND, error) from error
    except WrongCurrentPasswordError as error:
        logger.warning("auth.password_change_rejected", user_id=principal.user_id)
        raise api_error(status.HTTP_400_BAD_REQUEST, error) from error
    except WeakPasswordError as error:
        raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, error) from error
    logger.info("auth.password_changed", user_id=principal.user_id)
    # Se emite después de sellar el corte; `iat` lleva fracción de segundo, así
    # que pasa la comparación aunque sea del mismo segundo del cambio.
    session = await ReadCurrentSession(users, restaurants)(principal.user_id)
    token = tokens.issue(principal.user_id, principal.restaurant_id)
    return with_session_cookie(
        response, StaffAccessTokenResponse.issued(session, token.value, token.expires_in_seconds)
    )


@router.get(
    "/me/access-history",
    response_model=AccessHistoryPageResponse,
    summary="Los últimos inicios de sesión de la cuenta propia",
)
async def read_own_access_history(
    subject: SessionSubjectDep,
    history: AccessHistoryDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> AccessHistoryPageResponse:
    """RFS-15: cada intento con fecha, resultado, dispositivo, red y ubicación.

    Sirve a las dos clases de cuenta. Se guarda 90 días.
    """
    if isinstance(subject, PlatformClaims):
        kind, account_id = PLATFORM_ACCOUNT, subject.admin_id
    else:
        kind, account_id = STAFF_ACCOUNT, subject.user_id
    page = await history.list_for_account(kind, account_id, PageRequest(limit=limit, offset=offset))
    return AccessHistoryPageResponse.from_page(page)
