"""Companion configuration: one small TOML file plus ``CREMIND_TAG_*`` overrides.

The file lives at ``<user config dir>/config.toml`` (``platformdirs``, app name
``cremind-tag``) unless ``CREMIND_TAG_CONFIG`` names another path. It is read
with ``tomllib`` and written with the tiny writer below, so it stays a plain
file a person can edit::

    [hardware]
    gateway_url = "COM7"                       # or /dev/ttyACM0, socket://127.0.0.1:7777
    bridge_url = "COM9"                        # a bridge's maintenance port
    fontpack = "C:/packs/noto-v1.ctfp"
    jlink_tool = "auto"                        # auto | nrfutil | nrfjprog | jlink

    [paths]
    data_dir = "C:/Users/me/AppData/Local/cremind-tag"

    [secrets]
    backend = "auto"                           # auto | keyring | file

Every setting is a field of a *section* dataclass. A section declares its TOML
table name (``SECTION``) and the prefix of its environment overrides
(``ENV_PREFIX``); an environment variable ``<ENV_PREFIX><FIELD>`` (upper case)
wins over the file. Later components add their own sections with
:func:`register_section` (for example a ``[cremind]`` section with the server
URL); tables nobody registered are preserved verbatim when the file is saved.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import tomllib
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, ClassVar

import platformdirs

log = logging.getLogger(__name__)

APP_NAME = "cremind-tag"
ENV_PREFIX = "CREMIND_TAG_"
CONFIG_ENV = ENV_PREFIX + "CONFIG"
JLINK_TOOLS = ("auto", "nrfutil", "nrfjprog", "jlink")
SECRETS_BACKENDS = ("auto", "keyring", "file")


class ConfigError(ValueError):
    """The configuration file or an override is invalid."""


def default_config_path() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=True)) / "config.toml"


def default_data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


@dataclass
class HardwareSettings:
    """Where the companion finds its hardware and tools."""

    SECTION: ClassVar[str] = "hardware"
    ENV_PREFIX: ClassVar[str] = ENV_PREFIX

    gateway_url: str | None = None
    """Gateway serial port: ``COM7``, ``/dev/ttyACM0`` or any pyserial URL (``socket://host:port``)."""
    bridge_url: str | None = None
    """A bridge's maintenance port (font installation, flash test)."""
    fontpack: Path | None = None
    """The font pack (``.ctfp``) bridges should have active."""
    jlink_tool: str = "auto"
    """Preferred SWD tool for enrollment: auto | nrfutil | nrfjprog | jlink."""
    jlink_serial: str | None = None
    """Serial number of the J-Link probe to use when several are attached."""

    def validate(self) -> None:
        if self.jlink_tool not in JLINK_TOOLS:
            raise ConfigError(f"hardware.jlink_tool must be one of {', '.join(JLINK_TOOLS)}")


@dataclass
class PathsSettings:
    """Local state locations."""

    SECTION: ClassVar[str] = "paths"
    ENV_PREFIX: ClassVar[str] = ENV_PREFIX

    data_dir: Path = field(default_factory=default_data_dir)
    """SQLite database, secrets fallback file, enrollment images, simulator state."""

    def validate(self) -> None:
        pass


@dataclass
class SecretsSettings:
    """Where tag secrets and connector credentials are kept (see ``cremind_tag.secrets``)."""

    SECTION: ClassVar[str] = "secrets"
    ENV_PREFIX: ClassVar[str] = ENV_PREFIX + "SECRETS_"

    backend: str = "auto"

    def validate(self) -> None:
        if self.backend not in SECRETS_BACKENDS:
            raise ConfigError(f"secrets.backend must be one of {', '.join(SECRETS_BACKENDS)}")


_SECTIONS: dict[str, type] = {}


def register_section(cls: type) -> type:
    """Register a section dataclass (usable as a class decorator).

    The class needs ``SECTION`` (TOML table name) and may set ``ENV_PREFIX``
    (default ``CREMIND_TAG_<SECTION>_``) and a ``validate()`` method. Field
    types may be ``str``, ``int``, ``float``, ``bool``, ``Path``, optional
    forms of those, or ``list[str]``.
    """
    if not dataclasses.is_dataclass(cls):
        raise TypeError(f"{cls!r} is not a dataclass")
    name = getattr(cls, "SECTION", None)
    if not isinstance(name, str) or not name:
        raise TypeError(f"{cls.__name__} needs a SECTION name")
    if not hasattr(cls, "ENV_PREFIX"):
        cls.ENV_PREFIX = f"{ENV_PREFIX}{name.upper()}_"  # type: ignore[attr-defined]
    _SECTIONS[name] = cls
    return cls


for _cls in (HardwareSettings, PathsSettings, SecretsSettings):
    register_section(_cls)


# ---------------------------------------------------------------------------
# Value conversion
# ---------------------------------------------------------------------------


def _field_types(cls: type) -> dict[str, Any]:
    hints = typing.get_type_hints(cls)
    return {f.name: hints[f.name] for f in fields(cls)}


def _base_type(annotation: Any) -> tuple[Any, bool]:
    """(type without None, optional?) for ``X | None`` / ``Optional[X]``."""
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _convert(value: Any, annotation: Any, where: str, *, from_env: bool = False) -> Any:
    base, optional = _base_type(annotation)
    if value is None or (from_env and optional and value == ""):
        if optional:
            return None
        raise ConfigError(f"{where}: a value is required")
    try:
        if base is Path:
            return Path(os.path.expandvars(os.path.expanduser(str(value))))
        if base is bool:
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in ("1", "true", "yes", "on"):
                return True
            if text in ("0", "false", "no", "off"):
                return False
            raise ValueError(value)
        if base is int:
            if isinstance(value, bool):
                raise ValueError(value)
            return int(value, 0) if isinstance(value, str) else int(value)
        if base is float:
            return float(value)
        if base is str:
            if not isinstance(value, str) and not from_env:
                raise ValueError(value)
            return str(value)
        if typing.get_origin(base) is list:
            if from_env:
                return [part.strip() for part in str(value).split(",") if part.strip()]
            if not isinstance(value, list):
                raise ValueError(value)
            return [str(v) for v in value]
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: invalid value {value!r}") from None
    raise ConfigError(f"{where}: unsupported field type {annotation!r}")


def _to_toml_value(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    return value


# ---------------------------------------------------------------------------
# Tiny TOML writer (tables of scalars and flat lists; enough for this file)
# ---------------------------------------------------------------------------


def _toml_string(text: str) -> str:
    out = ['"']
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_key(key: str) -> str:
    if key and all(c.isalnum() or c in "-_" for c in key) and key.isascii():
        return key
    return _toml_string(key)


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, Path):
        return _toml_string(value.as_posix())
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml_scalar(v) for v in value) + "]"
    raise ConfigError(f"cannot write {type(value).__name__} to TOML")


def dumps_toml(data: Mapping[str, Any]) -> str:
    """Serialise top-level scalars and one level of tables (``None`` values are omitted)."""
    lines: list[str] = []
    for key, value in data.items():
        if value is not None and not isinstance(value, Mapping):
            lines.append(f"{_toml_key(key)} = {_toml_scalar(value)}")
    for key, value in data.items():
        if isinstance(value, Mapping):
            if lines:
                lines.append("")
            lines.append(f"[{_toml_key(key)}]")
            for sub_key, sub_value in value.items():
                if sub_value is None:
                    continue
                if isinstance(sub_value, Mapping):
                    raise ConfigError(f"nested table {key}.{sub_key} is not supported by the writer")
                lines.append(f"{_toml_key(sub_key)} = {_toml_scalar(sub_value)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class Config:
    """Loaded configuration: typed sections plus the raw tables of the file."""

    def __init__(self, path: Path, raw: dict[str, Any], env: Mapping[str, str]) -> None:
        self.path = path
        self._raw = raw  # file contents (what save() writes back)
        self._env = env
        self._cache: dict[str, Any] = {}

    # -- typed access -------------------------------------------------------

    def section[T](self, cls: type[T]) -> T:
        """The typed section ``cls`` (file values, then environment overrides)."""
        name: str = cls.SECTION  # type: ignore[attr-defined]
        if name not in self._cache:
            self._cache[name] = self._build(cls)
        return self._cache[name]  # type: ignore[no-any-return,unused-ignore]

    def _build(self, cls: type) -> Any:
        name: str = cls.SECTION  # type: ignore[attr-defined]
        prefix: str = cls.ENV_PREFIX  # type: ignore[attr-defined]
        table = self._raw.get(name, {})
        if not isinstance(table, dict):
            raise ConfigError(f"[{name}] must be a table")
        kwargs: dict[str, Any] = {}
        for fname, annotation in _field_types(cls).items():
            env_name = prefix + fname.upper()
            if env_name in self._env:
                kwargs[fname] = _convert(self._env[env_name], annotation, env_name, from_env=True)
            elif fname in table:
                kwargs[fname] = _convert(table[fname], annotation, f"{self.path}: {name}.{fname}")
        instance = cls(**kwargs)
        validate = getattr(instance, "validate", None)
        if callable(validate):
            validate()
        return instance

    @property
    def hardware(self) -> HardwareSettings:
        return self.section(HardwareSettings)

    @property
    def paths(self) -> PathsSettings:
        return self.section(PathsSettings)

    @property
    def secrets(self) -> SecretsSettings:
        return self.section(SecretsSettings)

    @property
    def data_dir(self) -> Path:
        return self.paths.data_dir

    def ensure_data_dir(self) -> Path:
        path = self.data_dir
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "companion.sqlite3"

    # -- editing ------------------------------------------------------------

    def raw_table(self, name: str) -> dict[str, Any]:
        """A copy of a file table (registered or not)."""
        return dict(self._raw.get(name, {}))

    def set(self, section: str, key: str, value: Any) -> None:
        """Set a file value (validated when the section is registered); ``None`` removes it."""
        cls = _SECTIONS.get(section)
        if cls is not None:
            types_ = _field_types(cls)
            if key not in types_:
                raise ConfigError(f"unknown setting {section}.{key}")
            if value is not None:
                converted = _convert(value, types_[key], f"{section}.{key}", from_env=isinstance(value, str))
                value = _to_toml_value(converted)
        table = self._raw.setdefault(section, {})
        if value is None:
            table.pop(key, None)
        else:
            table[key] = _to_toml_value(value)
        self._cache.pop(section, None)
        if cls is not None:
            self.section(cls)  # re-validate now rather than on next use

    def save(self) -> Path:
        """Write the file values (never environment overrides) atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(dumps_toml(self._raw), encoding="utf-8")
        os.replace(tmp, self.path)
        return self.path

    def as_dict(self) -> dict[str, dict[str, Any]]:
        """Effective values of every registered section (for display)."""
        out: dict[str, dict[str, Any]] = {}
        for name, cls in _SECTIONS.items():
            instance: Any = self.section(cls)
            out[name] = {f.name: _to_toml_value(getattr(instance, f.name)) for f in fields(cls)}
        return out


def load_config(path: Path | str | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Read the configuration file (missing file = defaults) and apply overrides."""
    env = os.environ if env is None else env
    if path is None:
        path = Path(env[CONFIG_ENV]) if env.get(CONFIG_ENV) else default_config_path()
    path = Path(path)
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from None
    config = Config(path, raw, env)
    for cls in _SECTIONS.values():
        config.section(cls)  # validate eagerly so errors surface at load time
    return config
