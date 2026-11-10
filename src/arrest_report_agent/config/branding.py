"""Global, non-persona Discord branding assets."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

DEFAULT_PROFILE_BANNER_PATH = "package:config/assets/default-profile-banner.png"
MAX_BRANDING_ASSET_BYTES = 10_485_760


class BrandingConfigurationError(RuntimeError):
    """Raised when a packaged global branding asset is unavailable or invalid."""


def load_global_profile_banner(path: str = DEFAULT_PROFILE_BANNER_PATH) -> bytes | None:
    """Load a configured local or packaged PNG; blank/none explicitly clears the banner."""
    if not path.strip() or path.strip() == "none":
        return None
    resource = (
        files("arrest_report_agent").joinpath(path.removeprefix("package:"))
        if path.startswith("package:") else Path(path)
    )
    try:
        payload = resource.read_bytes()
    except (FileNotFoundError, OSError) as error:
        raise BrandingConfigurationError("global profile banner is unavailable") from error
    if not payload:
        raise BrandingConfigurationError("global profile banner cannot be empty")
    if len(payload) > MAX_BRANDING_ASSET_BYTES:
        raise BrandingConfigurationError(
            f"global profile banner exceeds {MAX_BRANDING_ASSET_BYTES} bytes"
        )
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise BrandingConfigurationError("global profile banner must be a PNG image")
    return payload
