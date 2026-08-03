"""Error classification and safe roleplay-facing messages."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import discord
import openai
from agents.exceptions import AgentsException, MaxTurnsExceeded, ModelBehaviorError

from arrest_report_agent.domain import ErrorKind
from arrest_report_agent.domain.errors import ContextUnavailableError, PublicationError


@dataclass(frozen=True)
class ClassifiedError:
    kind: ErrorKind
    retryable: bool


def classify_error(error: BaseException) -> ClassifiedError:
    """Map provider, runner and Discord failures to stable operational classes."""

    if isinstance(error, (asyncio.TimeoutError, TimeoutError, openai.APITimeoutError)):
        return ClassifiedError(ErrorKind.TIMEOUT, True)
    if isinstance(error, openai.RateLimitError):
        detail = f"{getattr(error, 'code', '')} {getattr(error, 'body', '')}".casefold()
        if "quota" in detail or "insufficient_quota" in detail:
            return ClassifiedError(ErrorKind.QUOTA_EXCEEDED, False)
        return ClassifiedError(ErrorKind.RATE_LIMITED, True)
    if isinstance(error, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return ClassifiedError(ErrorKind.AUTH_CONFIGURATION, False)
    if isinstance(error, (openai.InternalServerError, openai.APIConnectionError)):
        return ClassifiedError(ErrorKind.PROVIDER_INTERNAL, True)
    if isinstance(error, openai.APIStatusError) and error.status_code >= 500:
        return ClassifiedError(ErrorKind.PROVIDER_INTERNAL, True)
    if isinstance(error, (MaxTurnsExceeded, ModelBehaviorError, AgentsException)):
        return ClassifiedError(ErrorKind.AGENT_EXECUTION, False)
    if isinstance(error, (discord.Forbidden, discord.HTTPException, PublicationError)):
        return ClassifiedError(ErrorKind.DISCORD_FAILURE, True)
    if isinstance(error, ContextUnavailableError):
        return ClassifiedError(ErrorKind.CONTEXT_UNAVAILABLE, True)
    return ClassifiedError(ErrorKind.UNKNOWN, False)


def public_error_message(kind: ErrorKind) -> str:
    """Return a contextual message without leaking provider or report data."""

    messages = {
        ErrorKind.QUOTA_EXCEEDED: (
            "La mesa de revisión alcanzó su capacidad operativa para este turno. Para no emitir "
            "un criterio apresurado, el expediente queda derivado a revisión manual."
        ),
        ErrorKind.RATE_LIMITED: (
            "La central está recibiendo demasiadas solicitudes. Intenta mencionarme nuevamente "
            "en unos minutos o solicita revisión manual."
        ),
        ErrorKind.TIMEOUT: (
            "La revisión excedió el tiempo operativo permitido. No emitiré un veredicto incompleto; "
            "queda pendiente de un supervisor o de un nuevo intento."
        ),
        ErrorKind.PROVIDER_INTERNAL: (
            "La central de análisis presentó una falla temporal. No pude completar una revisión "
            "confiable y el caso queda derivado."
        ),
        ErrorKind.AUTH_CONFIGURATION: (
            "La conexión con la central no está correctamente acreditada. Un administrador debe "
            "revisar la configuración del servicio."
        ),
        ErrorKind.AGENT_EXECUTION: (
            "No pude cerrar la revisión con garantías suficientes. El reporte queda marcado para "
            "revisión manual."
        ),
        ErrorKind.DISCORD_FAILURE: (
            "Discord impidió completar una acción necesaria. Revisa los permisos del bot en este "
            "canal y su hilo."
        ),
        ErrorKind.CONTEXT_UNAVAILABLE: (
            "No pude reconstruir el reporte raíz o el historial necesario. Comprueba que el mensaje "
            "siga disponible y que tenga acceso al historial."
        ),
        ErrorKind.UNKNOWN: (
            "Se produjo una incidencia no identificada y detuve la revisión para no entregar un "
            "veredicto inseguro."
        ),
    }
    return messages[kind]
