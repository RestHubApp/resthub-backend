"""Adaptador de entrada HTTP para el acceso y la sesión propia.

Traduce credenciales a un token y errores de dominio a códigos de estado. No
decide nada: la regla de quién puede entrar vive en el caso de uso.

También deja el rastro de seguridad del acceso en los logs: ingresos, ingresos
fallidos y cambios de contraseña. Vive acá y no en los casos de uso porque
escribir un log es tecnología, y el dominio no la conoce.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, NoReturn

from fastapi import APIRouter, Query, Request, Response, status

from resthub.core.access_history import (
    PLATFORM_ACCOUNT,
    STAFF_ACCOUNT,
    AccessEntry,
    AccessHistoryDep,
    AccessOutcome,
    LoginAttempt,
)
from resthub.core.account_sessions import (
    PREVIEW_SESSION,
    SessionOrigin,
    SessionRegistryDep,
    SqlSessionRegistry,
)
from resthub.core.activity import ActivityKind
from resthub.core.activity_log import ActivityRecorderDep
from resthub.core.auth import (
    UNAUTHENTICATED_HEADERS,
    PlatformTokenServiceDep,
    PrincipalDep,
    SessionSubjectDep,
    TemporaryPasswordLifetimeDep,
    TokenServiceDep,
    client_address,
    ensure_unlocked,
    unauthenticated,
)
from resthub.core.clock_dependency import ClockDep
from resthub.core.database import SessionDep
from resthub.core.error_codes import ErrorCode
from resthub.core.errors import ApiError, api_error
from resthub.core.identity import (
    AccessToken,
    InvalidTokenError,
    PlatformClaims,
    PlatformTokenService,
    TokenService,
)
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
from resthub.core.two_factor import (
    PlatformTwoFactorPolicyDep,
    SecondFactor,
    TwoFactorStoreDep,
    check_second_factor,
)
from resthub.modules.accounts.adapters.api.dependencies import (
    PasswordHasherDep,
    PlatformAdminDirectoryDep,
    PlatformSignInDep,
    PreviewCodesDep,
    RestaurantDirectoryDep,
    UserRepositoryDep,
)
from resthub.modules.accounts.adapters.api.idle_lock_router import router as idle_lock_router
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
from resthub.modules.accounts.adapters.api.security_router import router as security_router
from resthub.modules.accounts.adapters.api.two_factor_router import router as two_factor_router
from resthub.modules.accounts.domain.exceptions import (
    InactiveRestaurantError,
    InvalidCredentialsError,
    InvalidPreviewCodeError,
    OutdatedTermsError,
    PreviewSessionRestrictedError,
    TemporaryPasswordExpiredError,
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
# Sesiones propias, reautenticación, PIN y verificación en dos pasos.
router.include_router(security_router)
router.include_router(idle_lock_router)
router.include_router(two_factor_router)
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
    platform_tokens: PlatformTokenServiceDep,
    history: AccessHistoryDep,
    events: EventPublisherDep,
    sessions: SessionRegistryDep,
    two_factor: TwoFactorStoreDep,
    clock: ClockDep,
    lifetime: TemporaryPasswordLifetimeDep,
    session: SessionDep,
) -> StaffAccessTokenResponse | PlatformAdminAccessTokenResponse:
    """Un solo acceso para las dos clases de cuenta.

    Prueba primero al personal de los restaurantes; si no hay una credencial
    válida del personal, a la administración del sistema. La respuesta trae
    `kind` (`staff` o `platform`) y la sesión que corresponde. Una credencial
    que no sirve en ninguna responde el mismo 401 y gasta el mismo contador de
    intentos, así que no revela de qué clase es el correo.

    Con la verificación en dos pasos (RFS-22), la contraseña correcta sin
    `otp_code` responde 401 `two_factor_required`, y un código equivocado, 401
    `invalid_two_factor_code`, que cuenta como intento fallido. Una contraseña
    temporal que pasó `TEMPORARY_PASSWORD_HOURS` responde 401
    `temporary_password_expired` (ASVS 6.4.1), también contado como fallido.

    Un acceso correcto cierra la sesión que ese navegador traía en su cookie
    (ASVS V7.2.4): entrar otra vez, con la misma cuenta o con otra, no deja
    abierta la anterior.
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
    use_case = AuthenticateUser(
        users,
        restaurants,
        hasher,
        tokens,
        activity,
        clock=clock,
        temporary_password_lifetime=lifetime,
    )
    try:
        result = await use_case(
            AuthenticateUserCommand(email=str(payload.email), password=payload.password)
        )
    except InvalidCredentialsError as error:
        # No es del personal: queda probar con la administración del sistema.
        try:
            signed_in = await platform.authenticate(email, payload.password)
        except TemporaryPasswordExpiredError as expired:
            await _reject_expired(remember, email)
            raise api_error(
                status.HTTP_401_UNAUTHORIZED, expired, headers=UNAUTHENTICATED_HEADERS
            ) from expired
        if signed_in is not None:
            await check_second_factor(
                two_factor,
                session,
                (PLATFORM_ACCOUNT, signed_in.account.id),
                payload.otp_code,
                on_failure=lambda: remember(AccessOutcome.FAILED),
            )
            await throttle.succeeded(email, address, session)
            await remember(AccessOutcome.SUCCEEDED)
            await close_browser_session(request, sessions, session, tokens, platform_tokens)
            await open_device_session(
                sessions,
                session,
                request,
                signed_in.token,
                account=(PLATFORM_ACCOUNT, signed_in.account.id, None),
            )
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
    except TemporaryPasswordExpiredError as error:
        await _reject_expired(remember, email)
        raise api_error(
            status.HTTP_401_UNAUTHORIZED, error, headers=UNAUTHENTICATED_HEADERS
        ) from error
    except InactiveRestaurantError as error:
        await remember(AccessOutcome.BLOCKED)
        logger.warning(
            "auth.login_failed", reason=type(error).__name__, email=mask_email(str(payload.email))
        )
        raise api_error(status.HTTP_403_FORBIDDEN, error) from error

    user = result.session.user
    factor = await check_second_factor(
        two_factor,
        session,
        (STAFF_ACCOUNT, user.id or 0),
        payload.otp_code,
        on_failure=lambda: remember(AccessOutcome.FAILED),
    )
    if factor is SecondFactor.RECOVERY:
        await activity.record(user.restaurant_id, user.id or 0, ActivityKind.RECOVERY_CODE_USED)
    await throttle.succeeded(email, address, session)
    _alert_if_unfamiliar(events, await remember(AccessOutcome.SUCCEEDED))
    await close_browser_session(request, sessions, session, tokens, platform_tokens)
    await open_device_session(
        sessions,
        session,
        request,
        result.token,
        account=(STAFF_ACCOUNT, user.id or 0, user.restaurant_id),
    )
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


def _cookie_session_ids(
    request: Request, tokens: TokenService, platform_tokens: PlatformTokenService
) -> list[str]:
    """Los `sid` de las cookies de sesión que trae el navegador, si la firma es buena."""
    found: list[str | None] = []
    staff_token = request.cookies.get(STAFF_COOKIE)
    platform_token = request.cookies.get(PLATFORM_COOKIE)
    try:
        if staff_token:
            found.append(tokens.decode(staff_token).session_id)
    except InvalidTokenError:
        pass  # Vencida o ajena: no hay sesión que cerrar.
    try:
        if platform_token:
            found.append(platform_tokens.decode_platform(platform_token).session_id)
    except InvalidTokenError:
        pass  # Igual que arriba.
    return [session_id for session_id in found if session_id]


async def close_browser_session(
    request: Request,
    registry: SqlSessionRegistry,
    session: SessionDep,
    tokens: TokenService,
    platform_tokens: PlatformTokenService,
) -> None:
    """Cierra la sesión que el navegador traía en su cookie antes de abrir otra (ASVS V7.2.4).

    La cookie se reemplaza con la respuesta, pero la sesión anterior seguiría
    abierta en el servidor: quien se hubiera llevado su token podría seguir
    usándola. Solo se mira la cookie firmada por este servidor; no exige
    `X-CSRF-Token` porque el formulario de acceso no tiene sesión abierta.
    """
    for session_id in _cookie_session_ids(request, tokens, platform_tokens):
        state = await registry.find(session, session_id)
        if state is not None and registry.is_alive(state):
            await registry.revoke(session, session_id)
            logger.info("auth.previous_session_closed", account_kind=state.account_kind)


async def _reject_expired(
    remember: Callable[[AccessOutcome], Awaitable[AccessEntry]], email: str
) -> None:
    """La contraseña era la temporal y venció: cuenta como intento fallido."""
    await remember(AccessOutcome.FAILED)
    logger.warning(
        "auth.login_failed", reason="temporary_password_expired", email=mask_email(email)
    )


async def open_device_session(
    registry: SqlSessionRegistry,
    session: SessionDep,
    request: Request,
    token: AccessToken,
    *,
    account: tuple[str, int, int | None],
) -> None:
    """Anota la sesión del dispositivo que el token lleva firmada (RFS-17)."""
    kind, account_id, restaurant_id = account
    await registry.open(
        session,
        session_id=token.session_id,
        account_kind=kind,
        account_id=account_id,
        restaurant_id=restaurant_id,
        origin=SessionOrigin(client_address(request), request.headers.get("user-agent", "")),
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
    subject: SessionSubjectDep,
    activity: ActivityRecorderDep,
    response: Response,
    sessions: SessionRegistryDep,
    session: SessionDep,
) -> None:
    """Cierra la sesión de este dispositivo y la deja en la bitácora; responde 204.

    Solo esta: las de los demás dispositivos siguen abiertas. Sirve a las dos
    clases de cuenta, también con la contraseña temporal o la sesión bloqueada.
    Una vista previa cierra su sesión (su token deja de valer) y no deja asiento:
    sus entradas y salidas son de la plataforma.
    """
    if isinstance(subject, PlatformClaims):
        if subject.session_id is not None:
            await sessions.revoke(session, subject.session_id)
        clear_session(response)
        logger.info("platform.logout", admin_id=subject.admin_id)
        return
    principal = subject
    if principal.preview:
        if principal.session_id is not None:
            await sessions.revoke(session, principal.session_id)
        logger.info("auth.preview_ended", user_id=principal.user_id)
        return
    if principal.session_id is not None:
        await sessions.revoke(session, principal.session_id)
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
    request: Request,
    codes: PreviewCodesDep,
    users: UserRepositoryDep,
    restaurants: RestaurantDirectoryDep,
    admins: PlatformAdminDirectoryDep,
    tokens: TokenServiceDep,
    activity: ActivityRecorderDep,
    clock: ClockDep,
    sessions: SessionRegistryDep,
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

    user = result.session.user
    # Su propia sesión, para que salir de la vista previa corte el token.
    await open_device_session(
        sessions,
        session,
        request,
        result.token,
        account=(PREVIEW_SESSION, user.id or 0, user.restaurant_id),
    )
    # Confirmado antes de responder, no después: la sesión de la petición
    # confirma cuando la respuesta ya salió, y un token entregado con el código
    # todavía sin marcar usado dejaría una ventana para canjearlo otra vez.
    await session.commit()
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
        token = platform_tokens.issue_platform(account.id, session_id=subject.session_id)
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
    token = tokens.issue(
        principal.user_id, principal.restaurant_id, session_id=principal.session_id
    )
    return with_session_cookie(
        response,
        StaffAccessTokenResponse.issued(
            session, token.value, token.expires_in_seconds, locked=principal.session_locked
        ),
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
    two_factor: TwoFactorStoreDep,
    platform_required: PlatformTwoFactorPolicyDep,
    db: SessionDep,
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
        platform_session.two_factor_setup_required = platform_required and not (
            await two_factor.is_enabled(db, PLATFORM_ACCOUNT, subject.admin_id)
        )
        return platform_session
    principal = subject
    # El principal solo trae identificadores, restaurante y permisos. El
    # perfil lo posee este módulo, así que acá sí se lee la entidad entera.
    try:
        session = await ReadCurrentSession(users, restaurants)(principal.user_id)
    except UserNotFoundError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, "La cuenta ya no existe.", error.code) from error
    staff_session = StaffSessionResponse.from_session(
        session, preview=principal.preview, locked=principal.session_locked
    )
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
    response_model=AccessTokenResponse,
    summary="Cambiar la contraseña propia (personal o administración del sistema)",
)
async def change_own_password(
    payload: ChangeOwnPasswordRequest,
    subject: SessionSubjectDep,
    response: Response,
    users: UserRepositoryDep,
    restaurants: RestaurantDirectoryDep,
    hasher: PasswordHasherDep,
    activity: ActivityRecorderDep,
    tokens: TokenServiceDep,
    platform: PlatformSignInDep,
    platform_tokens: PlatformTokenServiceDep,
    clock: ClockDep,
    lifetime: TemporaryPasswordLifetimeDep,
    sessions: SessionRegistryDep,
    db: SessionDep,
) -> StaffAccessTokenResponse | PlatformAdminAccessTokenResponse:
    """Cambia la contraseña, cierra las demás sesiones y devuelve una nueva.

    Sirve a las dos clases de cuenta y responde la variante que corresponda
    (`kind`), con la forma del login. Pasa también con la contraseña temporal,
    que es para lo que está. No desde una sesión bloqueada por inactividad:
    primero se desbloquea.

    El cambio corta todos los tokens emitidos antes, el de esta petición
    incluido. La respuesta trae el token nuevo y la sesión, para que quien
    cambió la contraseña siga dentro sin volver a entrar. 400
    `wrong_current_password`, 422 `weak_password`, 401 `temporary_password_expired`.
    """
    if isinstance(subject, PlatformClaims):
        return await _change_platform_password(
            payload, subject, response, platform, platform_tokens, sessions, db
        )
    principal = ensure_unlocked(subject)
    try:
        await ChangeOwnPassword(
            users,
            hasher,
            activity,
            clock,
            restaurants=restaurants,
            temporary_password_lifetime=lifetime,
        )(
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
    except (UserNotFoundError, WrongCurrentPasswordError, WeakPasswordError) as error:
        _raise_password_change_error(error, account=principal.user_id)
    except TemporaryPasswordExpiredError as error:
        raise api_error(
            status.HTTP_401_UNAUTHORIZED, error, headers=UNAUTHENTICATED_HEADERS
        ) from error
    logger.info("auth.password_changed", user_id=principal.user_id)
    # Se emite después de sellar el corte; `iat` lleva fracción de segundo, así
    # que pasa la comparación aunque sea del mismo segundo del cambio.
    session = await ReadCurrentSession(users, restaurants)(principal.user_id)
    # Las demás sesiones ya no sirven (el corte las alcanzó): se marcan cerradas
    # para que no figuren como abiertas. Esta sigue, con un token nuevo.
    await sessions.revoke_all(db, STAFF_ACCOUNT, principal.user_id, keep=principal.session_id)
    token = tokens.issue(
        principal.user_id, principal.restaurant_id, session_id=principal.session_id
    )
    return with_session_cookie(
        response, StaffAccessTokenResponse.issued(session, token.value, token.expires_in_seconds)
    )


def _raise_password_change_error(
    error: UserNotFoundError | WrongCurrentPasswordError | WeakPasswordError, *, account: int
) -> NoReturn:
    if isinstance(error, UserNotFoundError):
        raise api_error(status.HTTP_404_NOT_FOUND, error) from error
    if isinstance(error, WrongCurrentPasswordError):
        logger.warning("auth.password_change_rejected", account_id=account)
        raise api_error(status.HTTP_400_BAD_REQUEST, error) from error
    raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, error) from error


async def _change_platform_password(
    payload: ChangeOwnPasswordRequest,
    claims: PlatformClaims,
    response: Response,
    platform: PlatformSignInDep,
    platform_tokens: PlatformTokenServiceDep,
    sessions: SqlSessionRegistry,
    db: SessionDep,
) -> PlatformAdminAccessTokenResponse:
    """La administración del sistema cambia su contraseña (ASVS 6.2.2)."""
    await _read_platform_account(platform, claims)
    try:
        account = await platform.change_password(
            claims.admin_id, payload.current_password, payload.new_password
        )
    except (UserNotFoundError, WrongCurrentPasswordError, WeakPasswordError) as error:
        _raise_password_change_error(error, account=claims.admin_id)
    except TemporaryPasswordExpiredError as error:
        raise api_error(
            status.HTTP_401_UNAUTHORIZED, error, headers=UNAUTHENTICATED_HEADERS
        ) from error
    logger.info("platform.password_changed", admin_id=claims.admin_id)
    await sessions.revoke_all(db, PLATFORM_ACCOUNT, claims.admin_id, keep=claims.session_id)
    token = platform_tokens.issue_platform(claims.admin_id, session_id=claims.session_id)
    return with_session_cookie(
        response,
        PlatformAdminAccessTokenResponse.issued(account, token.value, token.expires_in_seconds),
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
