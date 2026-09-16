"""Host and boot identity used to prove safe post-reboot task recovery."""

from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import uuid
from typing import Any


RECOVERY_CONTEXT_SCHEMA = "v10-recovery-context-1"
_IDENTITY_SCHEMES = {
    "windows-machineguid-sha256+boot-environment-guid",
    "linux-machine-id-sha256+boot-id",
}


class RecoveryIdentityError(RuntimeError):
    """The local host/boot identity cannot be established safely."""


def capture_recovery_context() -> dict[str, str]:
    """Return a non-secret machine digest and a kernel-issued boot identifier."""
    if os.name == "nt":
        return _capture_windows_context()
    if os.name == "posix" and Path("/proc/sys/kernel/random/boot_id").is_file():
        return _capture_linux_context()
    raise RecoveryIdentityError("reboot identity is unsupported on this operating system")


def _capture_windows_context() -> dict[str, str]:
    try:
        import winreg

        access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            access,
        ) as key:
            raw_machine, _ = winreg.QueryValueEx(key, "MachineGuid")
        if not isinstance(raw_machine, str):
            raise ValueError("MachineGuid is not text")
        machine = str(uuid.UUID(raw_machine.strip())).lower()
    except (OSError, TypeError, ValueError) as exc:
        raise RecoveryIdentityError(
            f"Windows MachineGuid cannot be read ({type(exc).__name__})"
        ) from None

    # SYSTEM_BOOT_ENVIRONMENT_INFORMATION begins with BootIdentifier (GUID).
    # A larger zeroed buffer avoids depending on architecture-specific tail padding.
    try:
        ntdll = ctypes.WinDLL("ntdll")
        query = ntdll.NtQuerySystemInformation
        query.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
        query.restype = ctypes.c_long
        buffer = ctypes.create_string_buffer(64)
        returned = ctypes.c_ulong(0)
        status = int(query(90, ctypes.byref(buffer), ctypes.sizeof(buffer), ctypes.byref(returned)))
        if status < 0 or returned.value < 16:
            raise OSError(f"NtQuerySystemInformation failed with NTSTATUS {status & 0xffffffff:#010x}")
        boot = str(uuid.UUID(bytes_le=bytes(buffer.raw[:16]))).lower()
        if boot == "00000000-0000-0000-0000-000000000000":
            raise ValueError("empty boot identifier")
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise RecoveryIdentityError(
            f"Windows boot GUID cannot be read ({type(exc).__name__}: {exc})"
        ) from exc

    return {
        "schema_version": RECOVERY_CONTEXT_SCHEMA,
        "identity_scheme": "windows-machineguid-sha256+boot-environment-guid",
        "machine_digest": hashlib.sha256(machine.encode("utf-8")).hexdigest(),
        "boot_identifier": boot,
    }


def _capture_linux_context() -> dict[str, str]:
    try:
        machine_raw = Path("/etc/machine-id").read_text(encoding="ascii").strip().lower()
        boot_raw = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip().lower()
        if not machine_raw or not boot_raw:
            raise ValueError("empty host or boot identifier")
        boot = str(uuid.UUID(boot_raw)).lower()
    except (OSError, UnicodeError, ValueError) as exc:
        raise RecoveryIdentityError("Linux machine or boot identity is unavailable") from exc
    return {
        "schema_version": RECOVERY_CONTEXT_SCHEMA,
        "identity_scheme": "linux-machine-id-sha256+boot-id",
        "machine_digest": hashlib.sha256(machine_raw.encode("ascii")).hexdigest(),
        "boot_identifier": boot,
    }


def validate_recovery_context(value: Any) -> dict[str, str]:
    """Validate a captured context without accepting caller-supplied identity."""
    if not isinstance(value, dict):
        raise RecoveryIdentityError("recovery identity provider returned invalid data")
    expected = {"schema_version", "identity_scheme", "machine_digest", "boot_identifier"}
    if set(value) != expected:
        raise RecoveryIdentityError("recovery identity provider returned invalid fields")
    if value["schema_version"] != RECOVERY_CONTEXT_SCHEMA:
        raise RecoveryIdentityError("recovery identity schema is unsupported")
    scheme = value["identity_scheme"]
    digest = value["machine_digest"]
    boot = value["boot_identifier"]
    if not isinstance(scheme, str) or scheme not in _IDENTITY_SCHEMES or not isinstance(digest, str) or len(digest) != 64:
        raise RecoveryIdentityError("recovery machine identity is invalid")
    if not isinstance(boot, str):
        raise RecoveryIdentityError("recovery boot identity is invalid")
    try:
        int(digest, 16)
        normalized_boot = str(uuid.UUID(boot)).lower()
    except (TypeError, ValueError) as exc:
        raise RecoveryIdentityError("recovery boot identity is invalid") from exc
    if normalized_boot == "00000000-0000-0000-0000-000000000000":
        raise RecoveryIdentityError("recovery boot identity is invalid")
    return {
        "schema_version": RECOVERY_CONTEXT_SCHEMA,
        "identity_scheme": scheme,
        "machine_digest": digest.lower(),
        "boot_identifier": normalized_boot,
    }


__all__ = [
    "RECOVERY_CONTEXT_SCHEMA",
    "RecoveryIdentityError",
    "capture_recovery_context",
    "validate_recovery_context",
]
