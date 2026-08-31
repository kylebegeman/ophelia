"""Host-local resolution for opaque manifest v2 secret references.

The resolver deliberately reads only root-owned or daemon-owned dotenv files
beneath the selected runtime root.  It never interpolates shell expressions and
never includes a secret value in an exception or report.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import stat
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Tuple, TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from .manifest_v2 import ManifestV2


_REFERENCE_COMPONENT = re.compile(r"^[a-z][a-z0-9-]*$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_VALUE_BYTES = 2 * 1024 * 1024


class RuntimeSecretError(ValueError):
    """A secret reference is invalid, unavailable, or backed by unsafe state."""


class RuntimeSecretResolver:
    """Resolve ``secret://app/environment/key`` from private runtime env files."""

    def __init__(self, runtime_root: Path) -> None:
        requested = Path(runtime_root).expanduser()
        if requested.is_symlink() or (requested.exists() and not requested.is_dir()):
            raise RuntimeSecretError("Runtime secret root must be a real directory.")
        self.runtime_root = requested.resolve(strict=False)
        self.trusted_owner_uids = {0, os.geteuid()}
        if self.runtime_root.exists():
            metadata = self.runtime_root.stat()
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise RuntimeSecretError("Runtime secret root must use mode 0700 or stricter.")
            self.trusted_owner_uids.add(metadata.st_uid)

    def __call__(self, reference: str) -> str:
        app, environment, key = parse_runtime_secret_reference(reference)
        for candidate in self._candidates(app, environment):
            if not candidate.exists() and not candidate.is_symlink():
                continue
            values = _read_private_env(candidate, self.trusted_owner_uids)
            if key in values:
                return values[key]
        raise RuntimeSecretError("Secret reference is not available from the host runtime.")

    def available(self, references: Iterable[str]) -> bool:
        try:
            for reference in sorted(set(references)):
                self(reference)
        except (OSError, RuntimeSecretError, UnicodeError, ValueError):
            return False
        return True

    def _candidates(self, app: str, environment: str) -> Tuple[Path, ...]:
        return (
            self.runtime_root / "apps" / app / "environments" / environment / "env",
            self.runtime_root / "apps" / app / "env",
            self.runtime_root / "secrets" / (app + "." + environment + ".env"),
        )


def manifest_secret_bindings_available(
    manifest: "ManifestV2",
    resolver: Callable[[str], str],
) -> bool:
    """Validate binding presence and representation without retaining values."""

    try:
        for secret in manifest.secrets:
            value = resolver(secret.ref)
            if not isinstance(value, str) or not value or "\x00" in value:
                return False
            if secret.mode == "env" and any(character in value for character in "\r\n"):
                return False
            if secret.mode == "file" and not _valid_file_value(value, secret.encoding):
                return False
        for route in manifest.routes:
            if route.client_auth is None:
                continue
            trust = resolver(route.client_auth.trust_pool_ref)
            decoded = _decoded_file_value(trust, route.client_auth.trust_pool_encoding)
            if decoded is None or b"-----BEGIN CERTIFICATE-----" not in decoded:
                return False
            if route.client_auth.forward is not None:
                authorization = resolver(route.client_auth.forward.authorization_ref)
                if (
                    not isinstance(authorization, str)
                    or len(authorization) < 32
                    or len(authorization) > 4096
                    or any(character in authorization for character in "\x00\r\n")
                ):
                    return False
    except (OSError, RuntimeError, ValueError, UnicodeError):
        return False
    return True


def parse_runtime_secret_reference(reference: str) -> Tuple[str, str, str]:
    if not isinstance(reference, str) or len(reference) > 4096:
        raise RuntimeSecretError("Secret reference must be a bounded string.")
    parsed = urlparse(reference)
    components = [item for item in parsed.path.split("/") if item]
    if (
        parsed.scheme != "secret"
        or not parsed.netloc
        or parsed.params
        or parsed.query
        or parsed.fragment
        or len(components) < 2
        or _REFERENCE_COMPONENT.fullmatch(parsed.netloc) is None
        or any(_REFERENCE_COMPONENT.fullmatch(item) is None for item in components)
    ):
        raise RuntimeSecretError(
            "Secret reference must use secret://app/environment/key syntax."
        )
    environment = components[0]
    key = "_".join(components[1:]).replace("-", "_").upper()
    if _ENV_NAME.fullmatch(key) is None:
        raise RuntimeSecretError("Secret reference key cannot map to an environment name.")
    return parsed.netloc, environment, key


def _read_private_env(path: Path, trusted_owner_uids: Iterable[int]) -> Dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeSecretError("Secret provider path must be a regular file.")
    metadata = path.stat()
    if metadata.st_uid not in set(trusted_owner_uids):
        raise RuntimeSecretError("Secret provider path has an untrusted owner.")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeSecretError("Secret provider path must use mode 0600 or stricter.")
    if metadata.st_size > _MAX_FILE_BYTES:
        raise RuntimeSecretError("Secret provider file exceeds its size limit.")
    document = path.read_text(encoding="utf-8")
    values: Dict[str, str] = {}
    for line_number, raw_line in enumerate(document.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise RuntimeSecretError(
                "Secret provider contains an invalid assignment on line %d." % line_number
            )
        raw_name, raw_value = line.split("=", 1)
        name = raw_name.strip()
        if _ENV_NAME.fullmatch(name) is None or name in values:
            raise RuntimeSecretError(
                "Secret provider contains an invalid or duplicate name on line %d."
                % line_number
            )
        value = _dotenv_value(raw_value.strip(), line_number)
        if not value or len(value.encode("utf-8")) > _MAX_VALUE_BYTES or "\x00" in value:
            raise RuntimeSecretError(
                "Secret provider contains an invalid value on line %d." % line_number
            )
        values[name] = value
    return values


def _dotenv_value(raw: str, line_number: int) -> str:
    if raw.startswith("'"):
        if len(raw) < 2 or not raw.endswith("'"):
            raise RuntimeSecretError(
                "Secret provider contains an unterminated value on line %d." % line_number
            )
        return raw[1:-1]
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeSecretError(
                "Secret provider contains an invalid quoted value on line %d." % line_number
            ) from exc
        if not isinstance(value, str):
            raise RuntimeSecretError(
                "Secret provider contains a non-string value on line %d." % line_number
            )
        return value
    if "\r" in raw or "\n" in raw:
        raise RuntimeSecretError(
            "Secret provider contains an invalid scalar on line %d." % line_number
        )
    return raw


def _valid_file_value(value: str, encoding: str) -> bool:
    return _decoded_file_value(value, encoding) is not None


def _decoded_file_value(value: object, encoding: str) -> Optional[bytes]:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    try:
        if encoding == "plain":
            decoded = value.encode("utf-8")
        elif encoding == "base64":
            decoded = base64.b64decode(value.encode("ascii"), validate=True)
        else:
            return None
    except (UnicodeError, binascii.Error, ValueError):
        return None
    if not decoded or len(decoded) > _MAX_VALUE_BYTES or b"\x00" in decoded:
        return None
    return decoded
