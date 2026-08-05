"""Global, non-persona Discord branding assets."""

from __future__ import annotations

from importlib.resources import files

GLOBAL_PROFILE_BANNER_PATH = ("config", "assets", "lssd-profile-banner.png")
MAX_BRANDING_ASSET_BYTES = 10_485_760


class BrandingConfigurationError(RuntimeError):
    """Raised when a packaged global branding asset is unavailable or invalid."""


def load_global_profile_banner() -> bytes:
    """Load the single LSSD banner shared by every bot persona."""

    resource = files("arrest_report_agent").joinpath(*GLOBAL_PROFILE_BANNER_PATH)
    try:
        payload = resource.read_bytes()
    except (FileNotFoundError, OSError) as error:
        raise BrandingConfigurationError("global LSSD profile banner is unavailable") from error
    if not payload:
        raise BrandingConfigurationError("global LSSD profile banner cannot be empty")
    if len(payload) > MAX_BRANDING_ASSET_BYTES:
        raise BrandingConfigurationError(
            f"global LSSD profile banner exceeds {MAX_BRANDING_ASSET_BYTES} bytes"
        )
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise BrandingConfigurationError("global LSSD profile banner must be a PNG image")
    return payload
