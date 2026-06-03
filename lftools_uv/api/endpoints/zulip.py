# SPDX-License-Identifier: EPL-1.0
##############################################################################
# Copyright (c) 2026 The Linux Foundation and others.
#
# All rights reserved. This program and the accompanying materials
# are made available under the terms of the Eclipse Public License v1.0
# which accompanies this distribution, and is available at
# http://www.eclipse.org/legal/epl-v10.html
##############################################################################
"""Zulip REST API helpers for lftools-uv.

This module is the API/business-logic layer for the ``lftools-uv zulip``
command group. It is intentionally import-safe even when the optional
``zulip`` extra is not installed: the import of the upstream ``zulip``
Python package is wrapped so that callers can detect availability via
:func:`zulip_available` and surface a friendly install hint from the CLI
layer (FR-022).

Public surface:

* Configuration resolution — :class:`ZulipConfig`,
  :func:`resolve_config` (precedence per FR-011/FR-012).
* Client factory — :func:`get_client`.
* Feature-level detection — :func:`get_server_feature_level`,
  :func:`check_feature_level` (FR-019 canonical error format).
* Resolution helpers — :func:`resolve_channel`, :func:`resolve_users`,
  :func:`resolve_groups`.
* Domain exceptions — :class:`ZulipConfigError`, :class:`ZulipAPIError`,
  :class:`ZulipFeatureLevelError`, :class:`ZulipAmbiguityError`,
  :class:`ZulipLockoutError`, :class:`ZulipNotFoundError`,
  :class:`ZulipValidationError`.

See ``specs/001-zulip-channel-mgmt/`` for the full feature design.
"""

from __future__ import annotations

import configparser
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from lftools_uv import config as lf_config

try:  # pragma: no cover - import guard exercised by integration tests
    import zulip as _zulip_module
except ImportError:  # pragma: no cover - exercised when extra not installed
    _zulip_module = None

if TYPE_CHECKING:  # pragma: no cover
    import zulip as _zulip_module_type  # noqa: F401

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain exceptions
# ---------------------------------------------------------------------------


class ZulipError(Exception):
    """Base class for all Zulip-related errors raised by this module."""


class ZulipConfigError(ZulipError):
    """Raised when Zulip configuration cannot be located or parsed."""


class ZulipAPIError(ZulipError):
    """Raised when the Zulip server returns an error response."""


class ZulipFeatureLevelError(ZulipError):
    """Raised when the server lacks the required Zulip feature level.

    The string form follows the FR-019 canonical format
    ``This operation requires Zulip feature level X (server has Y)``.
    """

    def __init__(self, required: int, actual: int, feature_name: str = "") -> None:
        self.required = required
        self.actual = actual
        self.feature_name = feature_name
        message = f"This operation requires Zulip feature level {required} (server has {actual})"
        super().__init__(message)


class ZulipAmbiguityError(ZulipError):
    """Raised when a name lookup matches more than one entity."""

    def __init__(self, message: str, matches: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.matches = matches or []


class ZulipNotFoundError(ZulipError):
    """Raised when a channel/user/group cannot be located by name or id."""


class ZulipLockoutError(ZulipError):
    """Raised when an operation would lock all users out of a channel."""


class ZulipValidationError(ZulipError):
    """Raised for client-side validation failures (e.g. mutex flags)."""


# ---------------------------------------------------------------------------
# Optional-dependency detection
# ---------------------------------------------------------------------------


def zulip_available() -> bool:
    """Return ``True`` when the optional ``zulip`` package is importable."""
    return _zulip_module is not None


def _require_zulip() -> Any:
    """Return the imported ``zulip`` module or raise :class:`ZulipConfigError`.

    The CLI layer normally short-circuits before this is reached (it
    presents the canonical FR-022 install hint when the extra is
    missing); ``_require_zulip`` exists so that the API layer can be
    consumed programmatically with a clear error when the extra is not
    installed.
    """
    if _zulip_module is None:  # pragma: no cover - defensive
        raise ZulipConfigError(
            "The 'zulip' Python package is not installed. Install with: pip install \"lftools-uv[zulip]\""
        )
    return _zulip_module


# ---------------------------------------------------------------------------
# Configuration resolution (FR-011 / FR-012)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ZulipConfig:
    """Resolved Zulip API credentials.

    Either ``config_path`` (a path to a zuliprc-format file) OR the
    three credential fields will be populated, depending on which
    source produced the configuration. The :func:`get_client` factory
    handles both cases transparently.
    """

    email: str | None = None
    api_key: str | None = None
    site: str | None = None
    config_path: Path | None = None
    source: str = "unknown"


_ZULIPRC_API_SECTION = "api"
_LFTOOLS_ZULIP_SECTION = "zulip"


def _load_zuliprc(path: Path) -> ZulipConfig:
    """Validate a zuliprc-format file and return a :class:`ZulipConfig`.

    The file is not parsed here for credential extraction — the
    ``zulip.Client`` consumes the file directly. Parsing only validates
    that the file is readable and contains the expected ``[api]``
    section, producing a clear error otherwise.
    """
    parser = configparser.ConfigParser()
    try:
        with path.open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except OSError as exc:
        raise ZulipConfigError(f"Cannot read Zulip config file {path}: {exc}") from exc
    except configparser.Error as exc:
        raise ZulipConfigError(f"Malformed Zulip config file {path}: {exc}") from exc

    if not parser.has_section(_ZULIPRC_API_SECTION):
        raise ZulipConfigError(f"Zulip config file {path} is missing required [api] section")
    return ZulipConfig(config_path=path, source=str(path))


def _load_lftools_ini() -> ZulipConfig | None:
    """Return a :class:`ZulipConfig` synthesized from ``lftools.ini``.

    Returns ``None`` when the ``[zulip]`` section is absent.
    """
    if not lf_config.has_section(_LFTOOLS_ZULIP_SECTION):
        return None
    try:
        email = lf_config.get_setting(_LFTOOLS_ZULIP_SECTION, "email")
        api_key = lf_config.get_setting(_LFTOOLS_ZULIP_SECTION, "key")
        site = lf_config.get_setting(_LFTOOLS_ZULIP_SECTION, "site")
    except (configparser.NoOptionError, configparser.NoSectionError) as exc:
        raise ZulipConfigError(f"lftools.ini [zulip] section is incomplete: {exc}") from exc
    if not (isinstance(email, str) and isinstance(api_key, str) and isinstance(site, str)):
        raise ZulipConfigError("lftools.ini [zulip] section must define email, key, site")
    return ZulipConfig(
        email=email,
        api_key=api_key,
        site=site,
        source="lftools.ini[zulip]",
    )


def resolve_config(
    zuliprc: Path | None = None,
    *,
    cwd: Path | None = None,
    home: Path | None = None,
) -> ZulipConfig:
    """Resolve Zulip configuration using the FR-011/FR-012 precedence chain.

    Precedence (first match wins):

    1. ``zuliprc`` argument (from ``--zuliprc`` CLI flag).
    2. ``./zuliprc`` in the current working directory.
    3. ``[zulip]`` section in ``lftools.ini``.
    4. ``~/.zuliprc``.

    Parameters ``cwd`` and ``home`` allow tests to inject filesystem
    locations; defaults are ``Path.cwd()`` and ``Path.home()``.

    Raises :class:`ZulipConfigError` when no source resolves.
    """
    cwd = cwd or Path.cwd()
    home = home or Path.home()

    if zuliprc is not None:
        path = Path(zuliprc)
        if not path.exists():
            raise ZulipConfigError(f"--zuliprc path does not exist: {path}")
        return _load_zuliprc(path)

    cwd_candidate = cwd / "zuliprc"
    if cwd_candidate.exists():
        return _load_zuliprc(cwd_candidate)

    ini_config = _load_lftools_ini()
    if ini_config is not None:
        return ini_config

    home_candidate = home / ".zuliprc"
    if home_candidate.exists():
        return _load_zuliprc(home_candidate)

    raise ZulipConfigError(
        "No Zulip configuration found. Searched: --zuliprc flag, ./zuliprc, lftools.ini [zulip] section, ~/.zuliprc"
    )


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def get_client(zuliprc: Path | None = None, *, config: ZulipConfig | None = None) -> Any:
    """Instantiate a ``zulip.Client`` from the resolved configuration.

    ``zuliprc`` and ``config`` are mutually exclusive; supply at most one.
    When neither is given, configuration is resolved via
    :func:`resolve_config`.
    """
    if zuliprc is not None and config is not None:
        raise ZulipValidationError("Pass either 'zuliprc' or 'config', not both")
    resolved = config or resolve_config(zuliprc)
    if resolved.config_path is not None:
        zulip_module = _require_zulip()
        return zulip_module.Client(config_file=str(resolved.config_path))
    # No zuliprc file — all three credential fields must be populated.
    missing: list[str] = []
    if not (isinstance(resolved.email, str) and resolved.email.strip()):
        missing.append("email")
    if not (isinstance(resolved.api_key, str) and resolved.api_key.strip()):
        missing.append("api_key")
    if not (isinstance(resolved.site, str) and resolved.site.strip()):
        missing.append("site")
    if missing:
        raise ZulipConfigError(f"Incomplete Zulip credentials from {resolved.source}: missing {', '.join(missing)}")
    zulip_module = _require_zulip()
    return zulip_module.Client(
        email=resolved.email,
        api_key=resolved.api_key,
        site=resolved.site,
    )


# ---------------------------------------------------------------------------
# Feature-level detection (FR-019)
# ---------------------------------------------------------------------------


#: Hardcoded feature-level thresholds determined by consulting the Zulip
#: changelog. Each name maps to the minimum ``zulip_feature_level`` that
#: a server must report before the corresponding capability is exposed.
#:
#: References (Zulip changelog,
#: https://zulip.com/api/changelog):
#:
#: * Feature level 1 — initial introduction of the
#:   ``zulip_feature_level`` field; all servers we target report >= 1.
#: * Feature level 12 — web-public streams and spectator access.
#: * Feature level 197 — group-based access control via
#:   ``can_access_group``.
#: * Feature level 161 — ``can_remove_subscribers_group`` permission.
#: * Feature level 334 — ``topic_policy`` per-channel field.
#: * Feature level 59 — stream reactivation via stream update API.
FEATURE_LEVELS: dict[str, int] = {
    "web-public": 12,
    "can-access-group": 197,
    "can-remove-subscribers-group": 161,
    "topic-policy": 334,
    "unarchive": 59,
}


def get_server_feature_level(client: Any) -> int:
    """Return the server's reported ``zulip_feature_level``.

    The result is cached on the client instance as ``_lftools_feature_level``
    to avoid repeated HTTP calls within a single CLI invocation.
    """
    cached = getattr(client, "_lftools_feature_level", None)
    if isinstance(cached, int):
        return cached
    try:
        response = client.get_server_settings()
    except Exception as exc:  # pragma: no cover - network errors
        raise ZulipAPIError(f"Failed to query server settings: {exc}") from exc
    if not isinstance(response, dict) or response.get("result") != "success":
        raise ZulipAPIError(f"Unexpected server_settings response: {response!r}")
    level = response.get("zulip_feature_level")
    if not isinstance(level, int):
        # Some very old servers omit the field; treat as level 0.
        level = 0
    try:
        client._lftools_feature_level = level
    except AttributeError:  # pragma: no cover - defensive
        pass
    return level


def check_feature_level(
    client: Any,
    required_level: int,
    feature_name: str = "",
) -> None:
    """Raise :class:`ZulipFeatureLevelError` when server feature level is too low."""
    actual = get_server_feature_level(client)
    if actual < required_level:
        raise ZulipFeatureLevelError(
            required=required_level,
            actual=actual,
            feature_name=feature_name,
        )


# ---------------------------------------------------------------------------
# Channel resolution
# ---------------------------------------------------------------------------


def _fetch_streams(client: Any, include_archived: bool) -> list[dict[str, Any]]:
    """Return the raw stream listing from the Zulip server.

    Includes archived streams when ``include_archived`` is ``True``.
    """
    request: dict[str, Any] = {
        "include_public": True,
        "include_subscribed": True,
        "include_all_active": True,
    }
    if include_archived:
        request["include_archived"] = True
    try:
        response = client.call_endpoint(url="streams", method="GET", request=request)
    except Exception as exc:  # pragma: no cover - network errors
        raise ZulipAPIError(f"Failed to list channels: {exc}") from exc
    if not isinstance(response, dict) or response.get("result") != "success":
        raise ZulipAPIError(f"Unexpected streams response: {response!r}")
    streams = response.get("streams", [])
    if not isinstance(streams, list):
        raise ZulipAPIError(f"Malformed streams payload: {response!r}")
    return streams


def resolve_channel(
    client: Any,
    *,
    name: str | None = None,
    channel_id: int | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Resolve a channel by name (case-insensitive) or numeric ID.

    Returns the raw stream dict from the Zulip API. Raises
    :class:`ZulipNotFoundError` when no match exists. When the channel
    exists only in the archived set and ``include_archived`` is
    ``False``, the error message advises adding ``--include-archived``
    per FR-018.
    """
    if (name is None) == (channel_id is None):
        raise ZulipValidationError("resolve_channel requires exactly one of 'name' or 'channel_id'")
    active_streams = _fetch_streams(client, include_archived=include_archived)

    if channel_id is not None:
        for stream in active_streams:
            if stream.get("stream_id") == channel_id:
                return stream
        if not include_archived:
            archived_streams = _fetch_streams(client, include_archived=True)
            for stream in archived_streams:
                if stream.get("stream_id") == channel_id:
                    raise ZulipNotFoundError(
                        f"Channel id {channel_id} is archived. Use --include-archived to operate on archived channels."
                    )
        raise ZulipNotFoundError(f"No channel with id {channel_id}")

    assert name is not None  # for type narrowing
    target = name.casefold()
    for stream in active_streams:
        if str(stream.get("name", "")).casefold() == target:
            return stream
    if not include_archived:
        archived_streams = _fetch_streams(client, include_archived=True)
        for stream in archived_streams:
            if str(stream.get("name", "")).casefold() == target:
                raise ZulipNotFoundError(
                    f"Channel '{name}' is archived. Use --include-archived to operate on archived channels."
                )
    raise ZulipNotFoundError(f"Channel '{name}' not found")


# ---------------------------------------------------------------------------
# User resolution
# ---------------------------------------------------------------------------


IdMode = Literal["email", "id", "name"]


def _fetch_users(client: Any) -> list[dict[str, Any]]:
    """Return the raw user listing from the Zulip server."""
    try:
        response = client.get_members({"include_custom_profile_fields": False})
    except Exception as exc:  # pragma: no cover - network errors
        raise ZulipAPIError(f"Failed to list users: {exc}") from exc
    if not isinstance(response, dict) or response.get("result") != "success":
        raise ZulipAPIError(f"Unexpected users response: {response!r}")
    members = response.get("members", [])
    if not isinstance(members, list):
        raise ZulipAPIError(f"Malformed users payload: {response!r}")
    return members


def _resolve_single_user(
    ident: str,
    members: list[dict[str, Any]],
    *,
    mode: IdMode,
) -> dict[str, Any]:
    """Resolve a single user identifier against a pre-fetched member list.

    Factored out of :func:`resolve_users` so that callers needing
    per-identifier error handling (e.g. bulk mutations that report
    partial failures) can drive the loop themselves and capture
    failures one-by-one instead of aborting on the first bad entry.

    Raises :class:`ZulipValidationError` for malformed input,
    :class:`ZulipNotFoundError` when the identifier matches nothing,
    and :class:`ZulipAmbiguityError` when a lookup matches more than one
    user. Ambiguity is normally expected only for full-name lookups, but
    malformed member payloads can also duplicate email or ID matches.
    """
    ident = ident.strip()
    if not ident:
        raise ZulipValidationError("User identifier must not be empty")
    if mode == "email":
        matches = [u for u in members if u.get("delivery_email") == ident or u.get("email") == ident]
    elif mode == "id":
        try:
            wanted = int(ident)
        except ValueError as exc:
            raise ZulipValidationError(f"--by-id requires a numeric identifier, got {ident!r}") from exc
        matches = [u for u in members if u.get("user_id") == wanted]
    elif mode == "name":
        matches = [u for u in members if u.get("full_name") == ident]
    else:  # pragma: no cover - guarded by Literal
        raise ZulipValidationError(f"Unknown user id mode: {mode!r}")

    if not matches:
        raise ZulipNotFoundError(f"No user found matching {ident!r} (--by-{mode})")
    if len(matches) > 1:
        raise ZulipAmbiguityError(
            f"User name {ident!r} matched {len(matches)} users; use --by-email or --by-id to disambiguate",
            matches=[
                {
                    "user_id": m.get("user_id"),
                    "full_name": m.get("full_name"),
                    "email": m.get("delivery_email") or m.get("email"),
                }
                for m in matches
            ],
        )
    return matches[0]


def resolve_users(
    client: Any,
    identifiers: Iterable[str],
    *,
    mode: IdMode,
) -> list[dict[str, Any]]:
    """Resolve a list of user identifiers per the chosen ``mode``.

    Returns one user dict per identifier in input order. Raises
    :class:`ZulipNotFoundError` when an identifier resolves to nothing,
    or :class:`ZulipAmbiguityError` (mode ``name`` only) when a
    full-name lookup matches more than one user. Email and ID lookups
    are unique by construction.
    """
    members = _fetch_users(client)
    return [_resolve_single_user(ident, members, mode=mode) for ident in identifiers]


# ---------------------------------------------------------------------------
# Group resolution
# ---------------------------------------------------------------------------


#: Display-name → API name mapping for built-in system role groups.
SYSTEM_ROLE_GROUPS: dict[str, str] = {
    "owners": "role:owners",
    "administrators": "role:administrators",
    "moderators": "role:moderators",
    "full members": "role:fullmembers",
    "members": "role:members",
    "everyone": "role:everyone",
    "nobody": "role:nobody",
}


#: Reverse of :data:`SYSTEM_ROLE_GROUPS`: maps the Zulip ``role:`` API name
#: to the human-facing display name presented in ``group list`` output and
#: accepted by ``--allow-group``/``--can-remove-subscribers-group``.
#:
#: Derived from :data:`SYSTEM_ROLE_GROUPS` to avoid drift — display names
#: are Title Case versions of the lowercase keys, with the historical
#: ``Full Members`` two-word form preserved.
def _build_system_role_display_names() -> dict[str, str]:
    overrides = {"full members": "Full Members"}
    return {
        api_name: overrides.get(display.lower(), display.title()) for display, api_name in SYSTEM_ROLE_GROUPS.items()
    }


SYSTEM_ROLE_DISPLAY_NAMES: dict[str, str] = _build_system_role_display_names()


def _fetch_groups(client: Any) -> list[dict[str, Any]]:
    """Return the raw user_groups listing from the Zulip server."""
    try:
        response = client.call_endpoint(url="user_groups", method="GET")
    except Exception as exc:  # pragma: no cover - network errors
        raise ZulipAPIError(f"Failed to list user groups: {exc}") from exc
    if not isinstance(response, dict) or response.get("result") != "success":
        raise ZulipAPIError(f"Unexpected user_groups response: {response!r}")
    groups = response.get("user_groups", [])
    if not isinstance(groups, list):
        raise ZulipAPIError(f"Malformed user_groups payload: {response!r}")
    return groups


def _resolve_single_group_token(token: str, groups: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve a single comma-item token to a group dict.

    Supports the ``id:NUM`` prefix and the system-role display-name
    mapping. The caller is expected to have stripped and filtered the
    input so ``token`` is always non-empty.

    Raises :class:`ZulipAmbiguityError`, :class:`ZulipNotFoundError`,
    or :class:`ZulipValidationError` as appropriate.
    """
    # ``resolve_groups`` filters empty/whitespace tokens before calling
    # this helper, so by construction ``token`` is non-empty here.
    if token.lower().startswith("id:"):
        suffix = token[3:].strip()
        try:
            wanted = int(suffix)
        except ValueError as exc:
            raise ZulipValidationError(f"id: prefix requires a numeric identifier, got {suffix!r}") from exc
        for grp in groups:
            if grp.get("id") == wanted:
                return grp
        raise ZulipNotFoundError(f"No user group with id {wanted}")

    api_name = SYSTEM_ROLE_GROUPS.get(token.casefold())
    if api_name is not None:
        for grp in groups:
            if grp.get("name") == api_name:
                return grp
        raise ZulipNotFoundError(f"System role group '{token}' not found on server")

    target = token.casefold()
    matches = [
        grp for grp in groups if str(grp.get("name", "")).casefold() == target and not grp.get("is_system_group", False)
    ]
    if not matches:
        raise ZulipNotFoundError(f"No user group named {token!r}")
    if len(matches) > 1:
        raise ZulipAmbiguityError(
            f"Group name {token!r} matched {len(matches)} groups; use the id:NUM prefix to disambiguate",
            matches=[{"id": g.get("id"), "name": g.get("name")} for g in matches],
        )
    return matches[0]


GroupSettingValue = int | dict[str, list[int]]


def _build_group_setting_value(group_ids: list[int]) -> GroupSettingValue:
    """Translate resolved group IDs into a Zulip group-setting value.

    Single group → simple int. Multiple groups → complex object with
    ``direct_members`` empty and ``direct_subgroups`` populated.
    """
    if len(group_ids) == 1:
        return group_ids[0]
    return {"direct_members": [], "direct_subgroups": group_ids}


def resolve_groups(
    client: Any,
    spec: str,
    *,
    allow_nobody: bool = True,
) -> tuple[list[dict[str, Any]], GroupSettingValue]:
    """Resolve a comma-separated ``--allow-group``-style argument.

    Returns a tuple ``(group_dicts, group_setting_value)`` suitable for
    sending to either the POST (raw value) or PATCH (wrapped under
    ``{"new": ...}``) endpoints. The caller is responsible for applying
    the PATCH wrapper.

    When ``allow_nobody`` is ``False`` (e.g. for lockout-prevention
    checks on private channel create/update), the helper raises
    :class:`ZulipLockoutError` if the resolved set is exactly the single
    ``Nobody`` system group.

    Empty / whitespace-only segments inside the comma-separated value
    are tolerated and stripped (so ``"design, , foo"`` is equivalent
    to ``"design, foo"``). A spec containing only empty segments is
    still rejected with :class:`ZulipValidationError`.
    """
    tokens = [t for t in (part.strip() for part in spec.split(",")) if t]
    if not tokens:
        raise ZulipValidationError("Group specification must not be empty")
    groups = _fetch_groups(client)
    resolved = [_resolve_single_group_token(tok, groups) for tok in tokens]
    if not allow_nobody and len(resolved) == 1 and resolved[0].get("name") == "role:nobody":
        raise ZulipLockoutError(
            "'Nobody' does not satisfy lockout prevention — it disables "
            "the permission entirely. Specify --subscribe users or a "
            "non-Nobody --allow-group."
        )
    group_ids: list[int] = []
    for grp in resolved:
        gid = grp.get("id")
        if not isinstance(gid, int):
            raise ZulipAPIError(f"Group object missing numeric id: {grp!r}")
        group_ids.append(gid)
    return resolved, _build_group_setting_value(group_ids)


# ---------------------------------------------------------------------------
# US1 — List Channels (T022)
# ---------------------------------------------------------------------------


def _normalize_channel(stream: dict[str, Any]) -> dict[str, Any]:
    """Project a raw Zulip stream dict into the documented shape.

    Returns a stable subset of fields per ``data-model.md`` with a
    derived ``type`` of ``public``, ``private``, or ``web-public``.
    ``stream_id`` is required and validated as an ``int``;
    ``subscriber_count`` defaults to ``0`` when missing or not numeric
    so that downstream consumers can rely on it being an integer.
    """
    stream_id = stream.get("stream_id")
    if not isinstance(stream_id, int):
        raise ZulipAPIError(f"Stream object missing numeric stream_id: {stream!r}")
    if stream.get("is_web_public"):
        channel_type = "web-public"
    elif stream.get("invite_only"):
        channel_type = "private"
    else:
        channel_type = "public"
    raw_count = stream.get("subscriber_count")
    subscriber_count = raw_count if isinstance(raw_count, int) else 0
    raw_name = stream.get("name")
    raw_desc = stream.get("description")
    return {
        "stream_id": stream_id,
        "name": str(raw_name) if isinstance(raw_name, str) else "",
        "description": str(raw_desc) if isinstance(raw_desc, str) else "",
        "type": channel_type,
        "subscriber_count": subscriber_count,
        "is_archived": bool(stream.get("is_archived", False)),
    }


def list_channels(client: Any, *, include_archived: bool = False) -> list[dict[str, Any]]:
    """Return a normalized list of channels visible to the authenticated user.

    When ``include_archived`` is ``False`` (the default), only active
    streams are returned. When ``True``, the server's streams endpoint
    is queried with ``include_archived=True`` so that the response
    already contains both active and archived streams in a single call.

    Each entry is the dict produced by :func:`_normalize_channel`.
    """
    streams = _fetch_streams(client, include_archived=include_archived)
    return [_normalize_channel(s) for s in streams]


# ---------------------------------------------------------------------------
# Channel subscribers (US7)
# ---------------------------------------------------------------------------


def list_subscribers(
    client: Any,
    *,
    name: str | None = None,
    channel_id: int | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """List subscribers of a channel, enriched with name/email metadata.

    Resolves the target channel via :func:`resolve_channel` (so the
    same name/id targeting rules apply, including the friendly
    ``--include-archived`` hint when the channel exists only in the
    archived set). Then calls the Zulip ``GET /streams/{id}/members``
    endpoint and cross-references each subscriber ``user_id`` against
    the users listing to populate ``full_name`` and ``email``.

    Returns a list of ``{"user_id", "full_name", "email"}`` dicts in
    the order returned by the server. When a subscriber's metadata is
    not present in the users listing (e.g. deactivated accounts), the
    enrichment fields are populated with ``None`` rather than raising.

    Raises :class:`ZulipValidationError` if neither or both of
    ``name``/``channel_id`` are supplied, :class:`ZulipNotFoundError`
    when the channel cannot be located, or :class:`ZulipAPIError` for
    server-side failures.
    """
    if (name is None) == (channel_id is None):
        raise ZulipValidationError("list_subscribers requires exactly one of 'name' or 'channel_id'")
    stream = resolve_channel(
        client,
        name=name,
        channel_id=channel_id,
        include_archived=include_archived,
    )
    stream_id = stream.get("stream_id")
    if not isinstance(stream_id, int) or isinstance(stream_id, bool):
        raise ZulipAPIError(f"Resolved channel missing numeric stream_id: {stream!r}")

    try:
        response = client.call_endpoint(
            url=f"streams/{stream_id}/members",
            method="GET",
        )
    except Exception as exc:  # pragma: no cover - network errors
        raise ZulipAPIError(f"Failed to list subscribers: {exc}") from exc
    if not isinstance(response, dict) or response.get("result") != "success":
        raise ZulipAPIError(f"Unexpected subscribers response: {response!r}")
    subscriber_ids = response.get("subscribers", [])
    if not isinstance(subscriber_ids, list):
        raise ZulipAPIError(f"Malformed subscribers payload: {response!r}")
    if not subscriber_ids:
        return []

    normalized_ids: list[int] = []
    for raw_id in subscriber_ids:
        if not isinstance(raw_id, int) or isinstance(raw_id, bool):
            raise ZulipAPIError(f"Malformed subscriber id in payload: {raw_id!r}")
        normalized_ids.append(raw_id)

    # Build a user_id → member dict lookup so enrichment is O(N+M).
    members = _fetch_users(client)
    by_id: dict[int, dict[str, Any]] = {}
    for member in members:
        uid = member.get("user_id")
        if isinstance(uid, int) and not isinstance(uid, bool):
            by_id[uid] = member

    enriched: list[dict[str, Any]] = []
    for uid in normalized_ids:
        member_record = by_id.get(uid)
        if member_record is None:
            enriched.append({"user_id": uid, "full_name": None, "email": None})
            continue
        full_name = member_record.get("full_name")
        email = member_record.get("delivery_email") or member_record.get("email")
        enriched.append(
            {
                "user_id": uid,
                "full_name": "" if full_name is None else str(full_name),
                "email": "" if email is None else str(email),
            }
        )
    return enriched


# ---------------------------------------------------------------------------
# User listing (US2)
# ---------------------------------------------------------------------------


def _normalize_user(member: dict[str, Any]) -> dict[str, Any]:
    """Project a raw Zulip ``members`` entry to the CLI/JSON contract shape.

    Matches the schema documented in ``contracts/cli-commands.md`` for
    ``zulip user list``: ``user_id``, ``full_name``, ``email``,
    ``is_bot``, ``is_active``.

    Behavioural notes:

    * ``full_name`` / ``email`` are coerced via ``str(...)``; only an
      explicit ``None`` (or missing key) collapses to ``""`` so that
      legitimate falsy-but-stringifiable values are preserved.
    * ``user_id`` is required and validated to be an ``int``; the
      Zulip API guarantees this, so a missing or non-numeric value
      indicates a malformed payload and raises
      :class:`ZulipAPIError`.
    """
    user_id = member.get("user_id")
    if not isinstance(user_id, int):
        raise ZulipAPIError(f"Malformed user payload: missing/invalid user_id in {member!r}")
    full_name = member.get("full_name")
    email = member.get("email")
    return {
        "user_id": user_id,
        "full_name": "" if full_name is None else str(full_name),
        "email": "" if email is None else str(email),
        "is_bot": bool(member.get("is_bot", False)),
        "is_active": bool(member.get("is_active", True)),
    }


def list_users(
    client: Any,
    *,
    include_bots: bool = False,
    include_deactivated: bool = False,
) -> list[dict[str, Any]]:
    """List users on the Zulip server (US2).

    Defaults exclude bot accounts and deactivated users, matching the
    CLI's default behavior. Pass ``include_bots=True`` /
    ``include_deactivated=True`` to relax those filters independently.

    Returns a list of normalized user dicts in the order the server
    returned them. Raises :class:`ZulipAPIError` on transport / server
    errors.
    """
    members = _fetch_users(client)
    result: list[dict[str, Any]] = []
    for member in members:
        if not include_bots and member.get("is_bot", False):
            continue
        if not include_deactivated and not member.get("is_active", True):
            continue
        result.append(_normalize_user(member))
    return result


# ---------------------------------------------------------------------------
# Group listing (US3)
# ---------------------------------------------------------------------------


def _normalize_group(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw ``user_groups`` API entry to the lftools schema.

    Maps system role groups to their display names per
    :data:`SYSTEM_ROLE_DISPLAY_NAMES`; custom group names pass through
    unchanged. ``member_count`` is derived from the ``members`` array
    length, or ``0`` when the server omits ``members``.

    Raises :class:`ZulipAPIError` when required fields are missing or
    have unexpected types — ``id`` must be an int, ``name`` must be a
    non-empty string, ``members`` must be a list when present, and
    ``is_system_group`` (when present) must be a ``bool``. The
    ``description`` field is coerced to a string (Zulip historically
    returns an empty string when not set, but ``None`` is also
    tolerated).
    """
    raw_id = raw.get("id")
    if not isinstance(raw_id, int):
        raise ZulipAPIError(f"Group object missing numeric 'id': {raw!r}")
    api_name = raw.get("name")
    if not isinstance(api_name, str) or not api_name:
        raise ZulipAPIError(f"Group object missing string 'name': {raw!r}")
    members = raw.get("members", [])
    if not isinstance(members, list):
        raise ZulipAPIError(f"Group object has non-list 'members': {raw!r}")
    is_system_raw = raw.get("is_system_group", False)
    if not isinstance(is_system_raw, bool):
        raise ZulipAPIError(f"Group object has non-boolean 'is_system_group': {raw!r}")
    if is_system_raw:
        display = SYSTEM_ROLE_DISPLAY_NAMES.get(api_name, api_name)
    else:
        display = api_name
    description = raw.get("description")
    return {
        "group_id": raw_id,
        "name": display,
        "description": "" if description is None else str(description),
        "member_count": len(members),
        "type": "system" if is_system_raw else "custom",
    }


def list_groups(
    client: Any,
    *,
    group_name: str | None = None,
    group_id: int | None = None,
) -> list[dict[str, Any]]:
    """List Zulip user groups (custom and system role groups).

    Returns a list of normalized group dicts with keys ``group_id``,
    ``name``, ``description``, ``member_count``, and ``type`` (either
    ``"custom"`` or ``"system"``). System role groups are returned with
    their human display name (e.g. ``"Administrators"``) rather than
    the internal ``role:administrators`` API name.

    ``group_name`` and ``group_id`` are mutually exclusive filters.
    ``group_name`` matching is case-insensitive against the display
    name and applies after the system-role name mapping; a collision
    that resolves to more than one group raises
    :class:`ZulipAmbiguityError` with the matches listed.
    """
    if group_name is not None and group_id is not None:
        raise ZulipValidationError("Specify only one of --group-name or --group-id, not both")
    raw_groups = _fetch_groups(client)
    normalized: list[dict[str, Any]] = []
    for raw in raw_groups:
        if not isinstance(raw, dict):
            raise ZulipAPIError(f"Malformed user_groups entry (not a dict): {raw!r}")
        normalized.append(_normalize_group(raw))

    if group_id is not None:
        return [g for g in normalized if g["group_id"] == group_id]

    if group_name is not None:
        target = group_name.casefold()
        matches = [g for g in normalized if str(g["name"]).casefold() == target]
        if len(matches) > 1:
            raise ZulipAmbiguityError(
                f"Group name {group_name!r} matched {len(matches)} groups; use --group-id to disambiguate",
                matches=[{"group_id": m["group_id"], "name": m["name"]} for m in matches],
            )
        return matches

    return normalized


# ---------------------------------------------------------------------------
# US4 — Create Channel (T034)
# ---------------------------------------------------------------------------


#: Valid topic-policy values per spec.
VALID_TOPIC_POLICIES = frozenset({"allow", "deny", "follow-default"})

#: Zulip API mapping from topic_policy string to integer.
TOPIC_POLICY_MAP: dict[str, int] = {
    "allow": 1,
    "deny": 2,
    "follow-default": 0,
}
TOPIC_POLICY_REVERSE_MAP: dict[int, str] = {value: key for key, value in TOPIC_POLICY_MAP.items()}


def create_channel(
    client: Any,
    *,
    name: str,
    description: str = "",
    channel_type: Literal["public", "private", "web-public"] = "public",
    subscribe_user_ids: list[int] | None = None,
    allow_group_value: GroupSettingValue | None = None,
    can_remove_subscribers_group_value: GroupSettingValue | None = None,
    announce: bool | None = None,
    topic_policy: str | None = None,
) -> dict[str, Any]:
    """Create a new Zulip channel (stream).

    Parameters
    ----------
    client
        Authenticated Zulip client instance.
    name
        The channel name (required).
    description
        Optional channel description.
    channel_type
        One of ``public``, ``private``, or ``web-public``.
    subscribe_user_ids
        List of user IDs to subscribe on creation.
    allow_group_value
        Resolved group-setting value for ``can_access_group`` field.
        For private channels, callers should validate this is not the
        ``Nobody`` group before calling (to prevent lockout).
    can_remove_subscribers_group_value
        Resolved group-setting value for ``can_remove_subscribers_group``.
    announce
        ``True`` to announce, ``False`` to suppress, ``None`` for API default.
    topic_policy
        One of ``allow``, ``deny``, ``follow-default``, or ``None``.

    Returns
    -------
    dict
        A ``MutationResult``-style dict with keys ``status``, ``channel_id``,
        ``channel_name``, ``operation``, and ``type``.

    Raises
    ------
    ZulipValidationError
        For client-side validation failures (e.g. invalid topic-policy value).
    ZulipLockoutError
        When creating a private channel without subscribers and allow-group
        is either missing or only contains ``Nobody``.
    ZulipFeatureLevelError
        When the server lacks the required feature level for web-public,
        topic-policy, or can-remove-subscribers-group features.
    ZulipAPIError
        For transport or server errors.
    """
    # Validate topic_policy if provided
    if topic_policy is not None and topic_policy not in VALID_TOPIC_POLICIES:
        raise ZulipValidationError(
            f"Invalid topic-policy value: {topic_policy!r}. Valid values are: {', '.join(sorted(VALID_TOPIC_POLICIES))}"
        )

    # Feature-level checks
    if channel_type == "web-public":
        check_feature_level(client, FEATURE_LEVELS["web-public"], "web-public channels")

    if topic_policy is not None:
        check_feature_level(client, FEATURE_LEVELS["topic-policy"], "topic-policy")

    if allow_group_value is not None:
        check_feature_level(client, FEATURE_LEVELS["can-access-group"], "group-based access control")

    if can_remove_subscribers_group_value is not None:
        check_feature_level(client, FEATURE_LEVELS["can-remove-subscribers-group"], "can-remove-subscribers-group")

    # Lockout prevention for private channels:
    # Require at least one subscriber OR a non-None allow_group_value.
    # Callers must validate that allow_group_value is not the Nobody group
    # before calling (the CLI does this via resolve_groups with allow_nobody=False).
    has_subscribers = bool(subscribe_user_ids)
    has_allow_group = allow_group_value is not None

    if channel_type == "private" and not has_subscribers and not has_allow_group:
        raise ZulipLockoutError(
            "Private channels require at least one --subscribe user or a non-Nobody --allow-group to prevent lockout."
        )

    # Build subscription payload
    subscription: dict[str, Any] = {"name": name}
    if description:
        subscription["description"] = description

    principals: list[int] = list(subscribe_user_ids) if subscribe_user_ids else []

    request: dict[str, Any] = {
        "subscriptions": [subscription],
        "principals": principals,
        "invite_only": channel_type == "private",
        "is_web_public": channel_type == "web-public",
    }

    if announce is True:
        request["announce"] = True
    elif announce is False:
        request["announce"] = False
    # None = API default (no key)

    if allow_group_value is not None:
        request["can_access_group"] = allow_group_value

    if can_remove_subscribers_group_value is not None:
        request["can_remove_subscribers_group"] = can_remove_subscribers_group_value

    # Make the API call
    try:
        response = client.call_endpoint(
            url="users/me/subscriptions",
            method="POST",
            request=request,
        )
    except Exception as exc:  # pragma: no cover - network errors
        raise ZulipAPIError(f"Failed to create channel: {exc}") from exc

    if not isinstance(response, dict) or response.get("result") != "success":
        msg = response.get("msg", str(response)) if isinstance(response, dict) else str(response)
        raise ZulipAPIError(f"Failed to create channel: {msg}")

    # Extract the stream_id from the response
    # The subscriptions endpoint returns subscriptions in the response as a dict
    # mapping email -> list of stream names. We need to fetch the stream to get its ID.
    warnings: list[str] = []
    try:
        stream = resolve_channel(client, name=name)
        stream_id = stream["stream_id"]
    except ZulipNotFoundError:
        # Channel was created but we can't find it - unusual edge case
        stream_id = None
        if topic_policy is not None:
            warnings.append(f"Channel created but could not locate to apply topic-policy '{topic_policy}'")

    # If topic_policy was requested, apply it via PATCH using the topics_policy field
    # (introduced in Zulip feature level 334)
    topic_policy_applied = False
    if topic_policy is not None and stream_id is not None:
        topic_policy_value = TOPIC_POLICY_MAP[topic_policy]
        try:
            patch_response = client.call_endpoint(
                url=f"streams/{stream_id}",
                method="PATCH",
                request={"topics_policy": topic_policy_value},
            )
            if isinstance(patch_response, dict) and patch_response.get("result") == "success":
                topic_policy_applied = True
            else:
                patch_msg = patch_response.get("msg") if isinstance(patch_response, dict) else str(patch_response)
                warnings.append(f"Failed to apply topic-policy '{topic_policy}': {patch_msg}")
                log.warning("Failed to set topic_policy on channel %s: %s", name, patch_msg)
        except Exception as exc:  # pragma: no cover
            warnings.append(f"Failed to apply topic-policy '{topic_policy}': {exc}")
            log.warning("Failed to set topic_policy on channel %s: %s", name, exc)

    # Determine overall status
    status = "success"
    if warnings:
        status = "partial"

    result: dict[str, Any] = {
        "status": status,
        "channel_id": stream_id,
        "channel_name": name,
        "operation": "create",
        "type": channel_type,
    }
    if topic_policy is not None:
        result["topic_policy_applied"] = topic_policy_applied
    if warnings:
        result["warnings"] = warnings

    return result


# ---------------------------------------------------------------------------
# Subscription management (US5)
# ---------------------------------------------------------------------------


#: Spec-defined maximum number of users that can be subscribed in a single
#: invocation. See data-model.md / contracts/cli-commands.md.
MAX_SUBSCRIBE_USERS = 50


def subscribe_users(
    client: Any,
    channel: str | int,
    users: Iterable[str],
    *,
    id_mode: IdMode,
    include_archived: bool = False,
    _resolved_stream: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Subscribe one or more users to a channel.

    ``channel`` may be a string (channel name) or an int (numeric
    ``stream_id``). Numeric channel names are explicitly preserved by
    the CLI layer — callers that want id-based resolution must pass an
    actual ``int``.

    ``users`` is the iterable of identifiers (emails, ids, or full
    names depending on ``id_mode``). Up to :data:`MAX_SUBSCRIBE_USERS`
    identifiers per invocation are permitted (FR / spec cap of 50).

    ``_resolved_stream`` is an internal optimisation: when the caller
    has *already* resolved the channel (e.g. the CLI layer pre-resolves
    so it can thread channel context into ``--json`` error payloads),
    it may pass the resulting stream dict here to skip a redundant
    ``GET /streams`` round-trip. Callers outside this package should
    leave it as ``None``.

    Returns the standard bulk-mutation payload with ``status``,
    ``channel_id``, ``channel_name``, ``operation``, ``results``, and
    ``errors`` fields per ``contracts/cli-commands.md``. Per-user
    outcomes are derived from the Zulip server's ``subscribed`` and
    ``already_subscribed`` maps. The ``unauthorized`` list, if any, is
    surfaced under ``errors``.

    Raises:
        ZulipValidationError: empty user list, more than 50 users, or
            other client-side validation failures.
        ZulipNotFoundError / ZulipAmbiguityError: from resolve_users
            (e.g. unknown identifier, ambiguous full-name match) or
            from resolve_channel (e.g. unknown channel).
        ZulipAPIError: when the Zulip subscribe endpoint returns an
            error response.
    """
    user_list = list(users)
    if not user_list:
        raise ZulipValidationError("subscribe_users requires at least one user identifier")
    if len(user_list) > MAX_SUBSCRIBE_USERS:
        raise ZulipValidationError(
            f"subscribe_users accepts at most {MAX_SUBSCRIBE_USERS} users per invocation (got {len(user_list)})"
        )

    if isinstance(channel, bool):  # bool is an int subclass — reject explicitly
        raise ZulipValidationError(f"Invalid channel argument: {channel!r}")
    if _resolved_stream is not None:
        # Caller (e.g. the CLI) has already resolved the channel. Trust
        # the supplied dict and skip the redundant API round-trip.
        stream = _resolved_stream
    elif isinstance(channel, int):
        stream = resolve_channel(client, channel_id=channel, include_archived=include_archived)
    else:
        stream = resolve_channel(client, name=str(channel), include_archived=include_archived)

    channel_id = stream.get("stream_id")
    channel_name = stream.get("name")
    if not isinstance(channel_id, int) or not isinstance(channel_name, str):
        raise ZulipAPIError(f"Malformed stream object: {stream!r}")

    resolved_users = resolve_users(client, user_list, mode=id_mode)

    # Build a stable per-user identity used for matching the server
    # response and for the ``user`` field in the result payload. Prefer
    # delivery_email (the Zulip "real" address) and fall back to email.
    user_emails: list[str] = []
    for u in resolved_users:
        email = u.get("delivery_email") or u.get("email")
        if not isinstance(email, str) or not email:
            raise ZulipAPIError(f"Resolved user missing email: {u!r}")
        user_emails.append(email)

    import json as _json

    request = {
        "subscriptions": _json.dumps([{"name": channel_name}]),
        "principals": _json.dumps(user_emails),
    }

    try:
        response = client.call_endpoint(url="users/me/subscriptions", method="POST", request=request)
    except Exception as exc:  # pragma: no cover - network errors
        raise ZulipAPIError(f"Failed to subscribe users: {exc}") from exc
    if not isinstance(response, dict) or response.get("result") != "success":
        msg = response.get("msg") if isinstance(response, dict) else None
        raise ZulipAPIError(f"Subscribe request failed: {msg or response!r}")

    # Defensive: the Zulip API contract documents these as a dict / dict
    # / list, but real-world responses can drift (or be replayed via a
    # fake client in tests). Validate the shapes up front so a server-
    # side regression surfaces as a clear ZulipAPIError instead of a
    # misleading "no response from server" per-user error.
    subscribed_raw = response.get("subscribed", {})
    already_raw = response.get("already_subscribed", {})
    unauthorized_raw = response.get("unauthorized", [])
    if not isinstance(subscribed_raw, dict):
        raise ZulipAPIError(
            f"Malformed subscribe response: 'subscribed' must be a dict, got {type(subscribed_raw).__name__}"
        )
    if not isinstance(already_raw, dict):
        raise ZulipAPIError(
            f"Malformed subscribe response: 'already_subscribed' must be a dict, got {type(already_raw).__name__}"
        )
    if not isinstance(unauthorized_raw, list):
        raise ZulipAPIError(
            f"Malformed subscribe response: 'unauthorized' must be a list, got {type(unauthorized_raw).__name__}"
        )

    def _channel_users(field_name: str, mapping: dict[str, Any]) -> set[str]:
        users = mapping.get(channel_name, [])
        if not isinstance(users, list):
            raise ZulipAPIError(
                f"Malformed subscribe response: '{field_name}[{channel_name}]' must be a list, "
                f"got {type(users).__name__}"
            )
        return {str(user) for user in users}

    subscribed = _channel_users("subscribed", subscribed_raw)
    already = _channel_users("already_subscribed", already_raw)
    unauthorized: list[Any] = unauthorized_raw

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    accounted: set[str] = set()
    for email in user_emails:
        if email in subscribed:
            results.append({"user": email, "status": "subscribed"})
            accounted.add(email)
        elif email in already:
            results.append({"user": email, "status": "already_subscribed"})
            accounted.add(email)
        elif email in unauthorized:
            errors.append({"user": email, "error": "unauthorized"})
            accounted.add(email)
    # Any user not mentioned in either map is treated as an error so
    # callers can detect silent partial failures.
    for email in user_emails:
        if email not in accounted:
            errors.append({"user": email, "error": "no response from server"})

    if errors and not results:
        status = "error"
    elif errors:
        status = "partial"
    else:
        status = "success"

    return {
        "status": status,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "operation": "subscribe",
        "results": results,
        "errors": errors,
    }


# Subscription mutations (US6 — unsubscribe)
# ---------------------------------------------------------------------------


def unsubscribe_users(
    client: Any,
    users: Iterable[str],
    *,
    channel: str | None = None,
    channel_id: int | None = None,
    id_mode: IdMode,
    include_archived: bool = False,
    resolved_channel: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Unsubscribe one or more users from a channel.

    Resolves the channel target (by name or numeric id) and the user
    identifiers (per ``id_mode``), then calls the Zulip
    ``DELETE /users/me/subscriptions`` endpoint. The return value is a
    bulk :class:`MutationResult`-shaped dict:

    .. code-block:: python

       {
           "status": "success" | "partial" | "error",
           "channel_id": int,
           "channel_name": str,
           "operation": "unsubscribe",
           "results": [
               {"user": "<identifier>", "status": "unsubscribed"},
               {"user": "<identifier>", "status": "not_subscribed"},
           ],
           "errors": [],
       }

    The Zulip server returns ``removed`` for users who were unsubscribed
    and ``not_removed`` for users who were not subscribed in the first
    place — the latter is reported as a ``not_subscribed`` no-op,
    consistent with the CLI contract "exit 0 = all succeeded (including
    no-ops)".

    Pass ``resolved_channel`` (a stream dict as returned by
    :func:`resolve_channel`) to skip the internal channel resolution
    step. This lets callers that have already resolved the channel
    (for example the CLI, which needs the resolved id available for
    the ``--json`` error payload before invoking this function) avoid
    a redundant ``GET /streams`` round-trip. When ``resolved_channel``
    is supplied, ``channel`` and ``channel_id`` are ignored.
    """
    if resolved_channel is None:
        if (channel is None) == (channel_id is None):
            raise ZulipValidationError("unsubscribe_users requires exactly one of 'channel' or 'channel_id'")
    user_list = list(users)
    if not user_list:
        raise ZulipValidationError("unsubscribe_users requires at least one user")
    if len(user_list) > MAX_SUBSCRIBE_USERS:
        raise ZulipValidationError(
            f"unsubscribe_users accepts at most {MAX_SUBSCRIBE_USERS} users per invocation (got {len(user_list)})"
        )

    if resolved_channel is not None:
        target = resolved_channel
    else:
        target = resolve_channel(
            client,
            name=channel,
            channel_id=channel_id,
            include_archived=include_archived,
        )
    resolved_target_id = target.get("stream_id")
    resolved_target_name = target.get("name")
    if not isinstance(resolved_target_id, int) or not isinstance(resolved_target_name, str):
        raise ZulipAPIError(f"Malformed stream object: {target!r}")

    # Resolve identifiers one-by-one so a single bad entry does not
    # abort the whole bulk operation. Per-user resolution failures are
    # captured into ``errors`` while successfully-resolved users still
    # get sent to the Zulip API. This matches the bulk-operation
    # behavior described in the data-model spec (status: partial).
    members = _fetch_users(client)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    principals: list[Any] = []
    resolved_pairs: list[tuple[str, Any]] = []  # (original, principal)

    for original in user_list:
        try:
            user = _resolve_single_user(original, members, mode=id_mode)
        except ZulipAmbiguityError as exc:
            match_parts = [f"{m.get('full_name')} <{m.get('email')}> (id: {m.get('user_id')})" for m in exc.matches]
            detail = f"{exc}; matches: {', '.join(match_parts)}"
            errors.append({"user": original, "error": detail, "matches": exc.matches})
            continue
        except (ZulipNotFoundError, ZulipValidationError) as exc:
            errors.append({"user": original, "error": str(exc)})
            continue
        if id_mode == "id":
            principal: Any = int(user["user_id"])
        else:
            # For both name- and email-mode lookups we send delivery_email
            # (falling back to ``email``) as principals so that the server
            # can match against the channel's subscriber list. Zulip
            # principals accept emails or user IDs interchangeably.
            principal = user.get("delivery_email") or user.get("email")
            if not isinstance(principal, str) or not principal:
                errors.append({"user": original, "error": f"Resolved user missing email: {user!r}"})
                continue
        principals.append(principal)
        resolved_pairs.append((original, principal))

    if not principals:
        # Every identifier failed to resolve — skip the API call entirely
        # and return an all-errors payload so the caller can surface the
        # per-user failures without a spurious server round-trip.
        return {
            "status": "error",
            "channel_id": resolved_target_id,
            "channel_name": resolved_target_name,
            "operation": "unsubscribe",
            "results": results,
            "errors": errors,
        }

    # The DELETE response reports removed/not_removed by stream, not by
    # principal, so request one principal at a time to preserve per-user
    # results and partial-failure reporting.
    for original, principal in resolved_pairs:
        try:
            response = client.call_endpoint(
                url="users/me/subscriptions",
                method="DELETE",
                request={
                    "subscriptions": json.dumps([resolved_target_name]),
                    "principals": json.dumps([principal]),
                },
            )
        except Exception as exc:  # pragma: no cover - network errors
            raise ZulipAPIError(f"Failed to unsubscribe users: {exc}") from exc

        if not isinstance(response, dict) or response.get("result") != "success":
            msg = (response or {}).get("msg") if isinstance(response, dict) else None
            raise ZulipAPIError(f"Unexpected unsubscribe response: {msg or response!r}")

        removed_set = {str(item) for item in response.get("removed", []) or []}
        not_removed_set = {str(item) for item in response.get("not_removed", []) or []}

        if resolved_target_name in removed_set:
            results.append({"user": original, "status": "unsubscribed"})
        elif resolved_target_name in not_removed_set:
            results.append({"user": original, "status": "not_subscribed"})
        else:
            errors.append(
                {
                    "user": original,
                    "error": "Server did not report an outcome for this user",
                }
            )

    if errors and not results:
        status = "error"
    elif errors:
        status = "partial"
    else:
        status = "success"

    return {
        "status": status,
        "channel_id": resolved_target_id,
        "channel_name": resolved_target_name,
        "operation": "unsubscribe",
        "results": results,
        "errors": errors,
    }


# Channel update (US8 — FR-004)
# ---------------------------------------------------------------------------


#: Allowed values for the ``--type`` flag on ``channel update``.
ChannelType = Literal["public", "private", "web-public"]

#: Allowed values for the ``--topic-policy`` flag.
TopicPolicy = Literal["allow", "deny", "follow-default"]


def _subscriber_count(
    client: Any,
    stream_id: int,
    *,
    channel: dict[str, Any] | None = None,
) -> int:
    """Return the number of subscribers to ``stream_id``.

    Fast path: when the already-resolved ``channel`` dict carries a
    ``subscriber_count`` integer (exposed by recent Zulip servers), use
    it directly to avoid an extra round-trip.

    Slow path: fall back to ``GET /api/v1/streams/{stream_id}/members``
    and count the returned ``subscribers`` list. This is materially
    more expensive on large channels because it fetches the full
    subscriber-ID list just to answer a yes/no question.

    Used by :func:`update_channel` to decide whether the lockout-
    prevention rule applies when converting a channel to private
    (FR-004 / spec scenario 13/14).
    """
    if channel is not None:
        hint = channel.get("subscriber_count")
        if isinstance(hint, int) and hint >= 0:
            return hint
    try:
        response = client.call_endpoint(
            url=f"streams/{stream_id}/members",
            method="GET",
        )
    except Exception as exc:  # pragma: no cover - network errors
        raise ZulipAPIError(f"Failed to query channel members: {exc}") from exc
    if not isinstance(response, dict) or response.get("result") != "success":
        raise ZulipAPIError(f"Unexpected members response: {response!r}")
    subscribers = response.get("subscribers", [])
    if not isinstance(subscribers, list):
        raise ZulipAPIError(f"Malformed members payload: {response!r}")
    return len(subscribers)


def update_channel(
    client: Any,
    *,
    name: str | None = None,
    channel_id: int | None = None,
    new_name: str | None = None,
    description: str | None = None,
    channel_type: ChannelType | None = None,
    topic_policy: TopicPolicy | None = None,
    subscribe_user_specs: Iterable[str] | None = None,
    user_id_mode: IdMode | None = None,
    allow_group: str | None = None,
    can_remove_subscribers_group: str | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Update channel settings via ``PATCH /api/v1/streams/{stream_id}``.

    Implements FR-004 (channel update) end-to-end:

    * Validates that at least one setting flag is supplied (rename,
      description, type, topic-policy, allow-group, or
      can-remove-subscribers-group).
    * Applies the FR-019 feature-level checks for web-public,
      topic-policy, can-access-group (``--allow-group``) and
      can-remove-subscribers-group.
    * Enforces lockout prevention when converting to ``private``: if
      the channel currently has 0 subscribers, the caller must supply
      either ``subscribe_user_specs`` (a non-empty list) or a non-Nobody
      ``allow_group`` value. ``Nobody`` does NOT satisfy this rule.
      When ``subscribe_user_specs`` is supplied, the users are
      resolved AND actually subscribed via
      ``POST /api/v1/users/me/subscriptions`` before the PATCH so that
      access is genuinely retained (the API call would otherwise lock
      the channel out, despite passing client-side validation).
    * Resolves group specs and wraps them using the group-setting-update
      ``{"new": value}`` envelope required by the Zulip PATCH endpoints.
      Note that this wrapping differs from the POST endpoints
      (``streams`` create), which take the raw value.
    * Returns the standard ``MutationResult`` dict
      (``status``/``channel_id``/``channel_name``/``operation``).
    """
    # ------------------------------------------------------------------
    # Argument validation
    # ------------------------------------------------------------------
    subscribe_list: list[str] = list(subscribe_user_specs or [])
    settings_specified = any(
        v is not None
        for v in (
            new_name,
            description,
            channel_type,
            topic_policy,
            allow_group,
            can_remove_subscribers_group,
        )
    ) or bool(subscribe_list)
    if not settings_specified:
        raise ZulipValidationError(
            "channel update requires at least one setting to change "
            "(--name, --description, --type, --topic-policy, --allow-group, "
            "--subscribe, or --can-remove-subscribers-group)"
        )

    valid_channel_types = {"public", "private", "web-public"}
    if channel_type is not None and channel_type not in valid_channel_types:
        raise ZulipValidationError(
            f"Invalid channel_type {channel_type!r}; expected one of {', '.join(sorted(valid_channel_types))}"
        )
    valid_topic_policies = {"allow", "deny", "follow-default"}
    if topic_policy is not None and topic_policy not in valid_topic_policies:
        raise ZulipValidationError(
            f"Invalid topic_policy {topic_policy!r}; expected one of {', '.join(sorted(valid_topic_policies))}"
        )

    # ------------------------------------------------------------------
    # Feature-level gating (FR-019)
    # ------------------------------------------------------------------
    if channel_type == "web-public":
        # Fetch server settings ONCE and reuse for both the feature-
        # level check (by priming the cached level) and the spectator-
        # access validation (spec scenario 8). Avoids two HTTP calls
        # when the cache is cold.
        try:
            settings_response = client.get_server_settings()
        except Exception as exc:  # pragma: no cover - network errors
            raise ZulipAPIError(f"Failed to query server settings: {exc}") from exc
        if not isinstance(settings_response, dict) or settings_response.get("result") != "success":
            raise ZulipAPIError(f"Unexpected server-settings response: {settings_response!r}")
        # Prime the feature-level cache so the following check_feature_level
        # call does not issue a second HTTP request.
        level_value = settings_response.get("zulip_feature_level")
        if isinstance(level_value, int):
            try:
                client._lftools_feature_level = level_value
            except AttributeError:  # pragma: no cover - defensive
                pass
        check_feature_level(client, FEATURE_LEVELS["web-public"], feature_name="web-public")
        # ``realm_enable_spectator_access`` is present on recent Zulip
        # servers; defensively allow the transition when the field is
        # absent (older servers leave enforcement to the API itself).
        spectator = settings_response.get("realm_enable_spectator_access")
        if spectator is False:
            # Use ZulipValidationError (not ZulipFeatureLevelError) so
            # the user sees the actual cause — feature-level error
            # messages are formatted as version mismatches and would
            # be misleading when the realm has explicitly disabled
            # spectator access.
            raise ZulipValidationError(
                "Cannot convert channel to web-public: spectator access "
                "is disabled on this Zulip realm (realm_enable_spectator_access=false). "
                "Enable spectator access in the realm settings first."
            )
    if topic_policy is not None:
        check_feature_level(client, FEATURE_LEVELS["topic-policy"], feature_name="topic-policy")
    if allow_group is not None:
        check_feature_level(client, FEATURE_LEVELS["can-access-group"], feature_name="can-access-group")
    if can_remove_subscribers_group is not None:
        check_feature_level(
            client,
            FEATURE_LEVELS["can-remove-subscribers-group"],
            feature_name="can-remove-subscribers-group",
        )

    # ------------------------------------------------------------------
    # Resolve target channel
    # ------------------------------------------------------------------
    channel = resolve_channel(
        client,
        name=name,
        channel_id=channel_id,
        include_archived=include_archived,
    )
    stream_id_raw = channel.get("stream_id")
    if not isinstance(stream_id_raw, int):
        raise ZulipAPIError(f"Resolved channel missing stream_id: {channel!r}")
    stream_id = stream_id_raw
    name_raw = channel.get("name")
    if not isinstance(name_raw, str):
        raise ZulipAPIError(f"Resolved channel missing name: {channel!r}")
    resolved_name = name_raw

    # ------------------------------------------------------------------
    # Resolve --allow-group (with lockout-aware allow_nobody)
    # ------------------------------------------------------------------
    # ``allow_group`` is always resolved with allow_nobody=True; the
    # lockout-prevention block below decides whether a Nobody-only
    # value is acceptable in the current context. (Per spec, Nobody is
    # only forbidden when converting to private with 0 existing
    # subscribers and no --subscribe targets; on a channel that
    # already has subscribers, Nobody is allowed and simply disables
    # future joins.)
    allow_group_value: GroupSettingValue | None = None
    allow_group_resolved: list[dict[str, Any]] | None = None
    if allow_group is not None:
        allow_group_resolved, allow_group_value = resolve_groups(
            client,
            allow_group,
            allow_nobody=True,
        )

    can_remove_value: GroupSettingValue | None = None
    if can_remove_subscribers_group is not None:
        _, can_remove_value = resolve_groups(client, can_remove_subscribers_group)

    # ------------------------------------------------------------------
    # Lockout prevention on type→private (spec scenarios 13/14, FR-004)
    # ------------------------------------------------------------------
    is_type_to_private = channel_type == "private" and not bool(channel.get("invite_only"))
    if is_type_to_private:
        has_subs_to_add = bool(subscribe_list)
        # An allow-group satisfies lockout prevention only if it
        # resolves to something other than just the Nobody system role
        # (which would disable the permission entirely).
        allow_group_is_only_nobody = (
            allow_group_resolved is not None
            and len(allow_group_resolved) == 1
            and allow_group_resolved[0].get("name") == "role:nobody"
        )
        allow_group_satisfies = allow_group_value is not None and not allow_group_is_only_nobody

        if not has_subs_to_add and not allow_group_satisfies:
            # Inspect current subscriber count; if zero, refuse.
            current = _subscriber_count(client, stream_id, channel=channel)
            if current == 0:
                raise ZulipLockoutError(
                    "Converting channel to private with no existing subscribers "
                    "would lock everyone out. Specify --subscribe users or a "
                    "non-Nobody --allow-group to retain access."
                )

    # ------------------------------------------------------------------
    # Resolve --subscribe identifiers. When supplied we actually
    # subscribe the users BEFORE issuing the PATCH so that
    # type-to-private conversions truly retain access — relying on the
    # lockout-prevention bypass without actually subscribing would
    # still lock the channel out.
    # ------------------------------------------------------------------
    if subscribe_list and channel_type != "private":
        raise ZulipValidationError("--subscribe is only valid when using --type private")
    if subscribe_list and user_id_mode is None:
        raise ZulipValidationError("--subscribe requires one of --by-email/--by-id/--by-name")
    if subscribe_list:
        assert user_id_mode is not None  # for type narrowing (validated above)
        resolved_users = resolve_users(client, subscribe_list, mode=user_id_mode)
        principals: list[Any] = []
        for user in resolved_users:
            user_id_value = user.get("user_id")
            if isinstance(user_id_value, int):
                principals.append(user_id_value)
                continue
            email = user.get("delivery_email") or user.get("email")
            if isinstance(email, str) and email:
                principals.append(email)
                continue
            raise ZulipAPIError(f"Resolved user missing usable principal: {user!r}")
        try:
            sub_response = client.call_endpoint(
                url="users/me/subscriptions",
                method="POST",
                request={
                    "subscriptions": json.dumps([{"name": resolved_name}]),
                    "principals": json.dumps(principals),
                },
            )
        except Exception as exc:  # pragma: no cover - network errors
            raise ZulipAPIError(f"Failed to subscribe users during update: {exc}") from exc
        if not isinstance(sub_response, dict) or sub_response.get("result") != "success":
            msg = ""
            if isinstance(sub_response, dict):
                msg = str(sub_response.get("msg") or sub_response)
            raise ZulipAPIError(f"Subscribe-during-update failed: {msg or sub_response!r}")

    # ------------------------------------------------------------------
    # Build PATCH request
    # ------------------------------------------------------------------
    request: dict[str, Any] = {}
    if new_name is not None:
        request["new_name"] = new_name
    if description is not None:
        request["description"] = description
    if channel_type is not None:
        request["is_private"] = channel_type == "private"
        request["is_web_public"] = channel_type == "web-public"
    if topic_policy is not None:
        request["topics_policy"] = TOPIC_POLICY_MAP[topic_policy]
    if allow_group_value is not None:
        request["can_access_group"] = {"new": allow_group_value}
    if can_remove_value is not None:
        request["can_remove_subscribers_group"] = {"new": can_remove_value}

    # ------------------------------------------------------------------
    # PATCH /api/v1/streams/{stream_id}
    # ------------------------------------------------------------------
    try:
        response = client.call_endpoint(
            url=f"streams/{stream_id}",
            method="PATCH",
            request=request,
        )
    except Exception as exc:  # pragma: no cover - network errors
        raise ZulipAPIError(f"Failed to update channel: {exc}") from exc
    if not isinstance(response, dict) or response.get("result") != "success":
        msg = ""
        if isinstance(response, dict):
            msg = str(response.get("msg") or response)
        raise ZulipAPIError(f"Update failed: {msg or response!r}")

    # Reflect rename in the returned channel_name.
    effective_name = new_name if new_name is not None else resolved_name
    return {
        "status": "success",
        "channel_id": stream_id,
        "channel_name": effective_name,
        "operation": "update",
    }


# Topic policy convenience helpers (FR-021)
# ---------------------------------------------------------------------------


def _normalize_topic_policy(raw_policy: Any) -> TopicPolicy:
    """Translate Zulip topic-policy API values to CLI policy strings."""
    if isinstance(raw_policy, str) and raw_policy in VALID_TOPIC_POLICIES:
        return raw_policy  # type: ignore[return-value]
    if not isinstance(raw_policy, bool) and isinstance(raw_policy, int) and raw_policy in TOPIC_POLICY_REVERSE_MAP:
        return TOPIC_POLICY_REVERSE_MAP[raw_policy]  # type: ignore[return-value]
    raise ZulipAPIError(f"Malformed topic-policy value from server: {raw_policy!r}")


def _resolve_topic_policy_channel(
    client: Any,
    channel: str | int | dict[str, Any],
    *,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Resolve a topic-policy target passed as name, ID, or stream dict."""
    if isinstance(channel, bool) or channel is None:
        raise ZulipValidationError("topic-policy requires a channel name or id")
    if isinstance(channel, dict):
        return channel
    if isinstance(channel, int):
        if channel <= 0:
            raise ZulipValidationError(f"topic-policy requires a positive channel id (got {channel})")
        return resolve_channel(client, channel_id=channel, include_archived=include_archived)
    if isinstance(channel, str):
        channel_name = channel.strip()
        if not channel_name:
            raise ZulipValidationError("topic-policy requires a non-empty channel name")
        return resolve_channel(client, name=channel_name, include_archived=include_archived)
    raise ZulipValidationError(f"Unsupported channel target type: {type(channel).__name__}")


def get_topic_policy(
    client: Any,
    channel: str | int | dict[str, Any],
    *,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Return the current topic-editing policy for a channel."""
    check_feature_level(client, FEATURE_LEVELS["topic-policy"], feature_name="topic-policy")
    target = _resolve_topic_policy_channel(client, channel, include_archived=include_archived)
    stream_id = target.get("stream_id")
    channel_name = target.get("name")
    if not isinstance(stream_id, int) or not isinstance(channel_name, str):
        raise ZulipAPIError(f"Resolved channel missing stream_id/name: {target!r}")

    try:
        response = client.call_endpoint(url=f"streams/{stream_id}", method="GET")
    except Exception as exc:  # pragma: no cover - network errors
        raise ZulipAPIError(f"Failed to read topic policy for {channel_name!r}: {exc}") from exc
    if not isinstance(response, dict) or response.get("result") != "success":
        msg = response.get("msg") if isinstance(response, dict) else None
        raise ZulipAPIError(f"Failed to read topic policy for {channel_name!r}: {msg or response!r}")
    stream_info = response.get("stream")
    if not isinstance(stream_info, dict):
        raise ZulipAPIError(f"Malformed stream-info response for {channel_name!r}: {response!r}")

    raw_policy = stream_info.get("topics_policy", stream_info.get("topic_policy"))
    topic_policy = _normalize_topic_policy(raw_policy)
    return {
        "channel_id": stream_id,
        "channel_name": channel_name,
        "topic_policy": topic_policy,
    }


def set_topic_policy(
    client: Any,
    channel: str | int | dict[str, Any],
    policy: TopicPolicy,
    *,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Set a channel topic-editing policy via the Zulip PATCH endpoint."""
    if policy not in VALID_TOPIC_POLICIES:
        raise ZulipValidationError(
            f"Invalid topic-policy value: {policy!r}. Valid values are: {', '.join(sorted(VALID_TOPIC_POLICIES))}"
        )
    check_feature_level(client, FEATURE_LEVELS["topic-policy"], feature_name="topic-policy")
    target = _resolve_topic_policy_channel(client, channel, include_archived=include_archived)
    stream_id = target.get("stream_id")
    channel_name = target.get("name")
    if not isinstance(stream_id, int) or not isinstance(channel_name, str):
        raise ZulipAPIError(f"Resolved channel missing stream_id/name: {target!r}")

    try:
        response = client.call_endpoint(
            url=f"streams/{stream_id}",
            method="PATCH",
            request={"topics_policy": TOPIC_POLICY_MAP[policy]},
        )
    except Exception as exc:  # pragma: no cover - network errors
        raise ZulipAPIError(f"Failed to set topic policy for {channel_name!r}: {exc}") from exc
    if not isinstance(response, dict) or response.get("result") != "success":
        msg = response.get("msg") if isinstance(response, dict) else None
        raise ZulipAPIError(f"Failed to set topic policy for {channel_name!r}: {msg or response!r}")

    return {
        "status": "success",
        "channel_id": stream_id,
        "channel_name": channel_name,
        "operation": "topic-policy",
        "topic_policy": policy,
    }


# US9 — Archive a channel (T054)
# ---------------------------------------------------------------------------


def archive_channel(
    client: Any,
    channel: str | int,
    *,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Archive (deactivate) a Zulip channel.

    Resolves ``channel`` via :func:`resolve_channel` (by name when a
    string is supplied, by id when an int is supplied) and then calls
    the Zulip ``DELETE /streams/{stream_id}`` endpoint to deactivate
    the stream. The operation is idempotent: if the resolved channel
    is already archived, no DELETE call is issued and a success
    ``MutationResult`` is returned anyway. This matches the FR-018
    expectations for ``--include-archived``.

    Returns the standard ``MutationResult`` payload:
    ``{"status": "success", "channel_id": <int>, "channel_name": <str>,
    "operation": "archive"}``.
    """
    if isinstance(channel, bool) or channel is None:
        raise ZulipValidationError("archive_channel requires a channel name or id")
    if isinstance(channel, int):
        if channel <= 0:
            raise ZulipValidationError(f"archive_channel requires a positive channel id (got {channel})")
        target = resolve_channel(client, channel_id=channel, include_archived=include_archived)
    elif isinstance(channel, str):
        channel_name = channel.strip()
        if not channel_name:
            raise ZulipValidationError("archive_channel requires a non-empty channel name")
        target = resolve_channel(client, name=channel_name, include_archived=include_archived)
    else:  # pragma: no cover - defensive
        raise ZulipValidationError(f"Unsupported channel target type: {type(channel).__name__}")

    stream_id = target.get("stream_id")
    name = target.get("name")
    if not isinstance(stream_id, int):
        raise ZulipAPIError(f"Resolved channel missing numeric stream_id: {target!r}")
    if not isinstance(name, str) or not name:
        raise ZulipAPIError(f"Resolved channel missing string name: {target!r}")

    if target.get("is_archived"):
        # Already-archived no-op: return success without calling DELETE.
        log.debug("Channel %r (id=%s) already archived; skipping DELETE", name, stream_id)
        return {
            "status": "success",
            "channel_id": stream_id,
            "channel_name": name,
            "operation": "archive",
        }

    try:
        response = client.call_endpoint(
            url=f"streams/{stream_id}",
            method="DELETE",
        )
    except Exception as exc:  # pragma: no cover - network errors
        raise ZulipAPIError(f"Failed to archive channel {name!r}: {exc}") from exc

    if not isinstance(response, dict):
        raise ZulipAPIError(f"Malformed archive response for {name!r}: {response!r}")
    result_field = response.get("result")
    if result_field != "success":
        # The Zulip server reports an already-deactivated stream via
        # ``code == "STREAM_DEACTIVATED"``. Treat only that documented
        # code as idempotent success; any other non-success response is a
        # genuine API error.
        code = str(response.get("code", ""))
        msg = str(response.get("msg", ""))
        if code == "STREAM_DEACTIVATED":
            log.debug(
                "Server reports channel %r already deactivated; treating as success",
                name,
            )
        else:
            detail = msg or repr(response)
            raise ZulipAPIError(f"Failed to archive channel {name!r}: {detail}")

    return {
        "status": "success",
        "channel_id": stream_id,
        "channel_name": name,
        "operation": "archive",
    }


# ---------------------------------------------------------------------------
# Channel mutations — unarchive (US10)
# ---------------------------------------------------------------------------


def unarchive_channel(
    client: Any,
    channel: str | None = None,
    *,
    channel_id: int | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Reactivate (unarchive) an archived channel.

    Resolves the target channel by name (case-insensitive) or numeric
    ``channel_id``. When ``include_archived`` is ``True`` the listing
    request includes archived streams alongside the active set (so the
    resolver can match either); when ``False`` and the channel exists
    only in the archived set, :class:`ZulipNotFoundError` is raised
    with the FR-018 advisory message suggesting ``--include-archived``.

    Already-active channels are handled idempotently: the function
    returns a success ``MutationResult`` without contacting the stream
    update API, so retries are safe (FR-013 idempotency). Archived
    channels are reactivated with ``PATCH streams/{stream_id}`` and
    ``{"is_archived": False}``.

    Returns the canonical ``MutationResult`` dict:
    ``{"status": "success", "channel_id": int, "channel_name": str,
    "operation": "unarchive"}``.

    Raises:
        ZulipValidationError: if neither/both of ``channel``/``channel_id``
            are supplied.
        ZulipFeatureLevelError: if the server's reported feature level
            is below :data:`FEATURE_LEVELS`[``"unarchive"``].
        ZulipNotFoundError: if the target channel cannot be located
            (FR-018 message includes ``--include-archived`` hint when
            the channel exists only in the archived set).
        ZulipAPIError: if the Zulip server returns a non-success
            stream update response.
    """
    if (channel is None) == (channel_id is None):
        raise ZulipValidationError("unarchive_channel requires exactly one of 'channel' or 'channel_id'")
    if channel_id is not None:
        if isinstance(channel_id, bool) or channel_id <= 0:
            raise ZulipValidationError(f"unarchive_channel requires a positive channel id (got {channel_id})")
    elif channel is not None:
        channel = channel.strip()
        if not channel:
            raise ZulipValidationError("unarchive_channel requires a non-empty channel name")

    check_feature_level(
        client,
        required_level=FEATURE_LEVELS["unarchive"],
        feature_name="unarchive",
    )

    stream = resolve_channel(
        client,
        name=channel,
        channel_id=channel_id,
        include_archived=include_archived,
    )

    stream_id = stream.get("stream_id")
    if not isinstance(stream_id, int):
        raise ZulipAPIError(f"Resolved channel missing numeric stream_id: {stream!r}")
    stream_name = stream.get("name")
    if not isinstance(stream_name, str) or not stream_name:
        raise ZulipAPIError(f"Resolved channel missing string name: {stream!r}")

    # Idempotent no-op: already-active channels skip the PATCH entirely so
    # retries after a partial failure are safe.
    if not stream.get("is_archived", False):
        return {
            "status": "success",
            "channel_id": stream_id,
            "channel_name": stream_name,
            "operation": "unarchive",
        }

    try:
        response = client.call_endpoint(
            url=f"streams/{stream_id}",
            method="PATCH",
            request={"is_archived": False},
        )
    except Exception as exc:  # pragma: no cover - network errors
        raise ZulipAPIError(f"Failed to unarchive channel {stream_name!r}: {exc}") from exc

    if not isinstance(response, dict) or response.get("result") != "success":
        msg = (response or {}).get("msg") if isinstance(response, dict) else None
        raise ZulipAPIError(f"Failed to unarchive channel {stream_name!r}: {msg or response!r}")

    return {
        "status": "success",
        "channel_id": stream_id,
        "channel_name": stream_name,
        "operation": "unarchive",
    }
