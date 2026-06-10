"""Zeit-Helfer: UTC-Zeitstempel und lokale, menschenlesbare Labels."""

from datetime import datetime, timezone


def utc_now() -> str:
  return datetime.now(timezone.utc).isoformat()


def to_local_time_label(utc_iso: str) -> str:
  return datetime.fromisoformat(utc_iso).astimezone().strftime("%d.%m.%Y %H:%M:%S")


def local_time_label() -> str:
  return datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S")
