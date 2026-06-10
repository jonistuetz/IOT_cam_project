"""Schlichtes, einheitliches Logging für den Doorbell-Dienst."""


def debug(message: str) -> None:
  print(f"[face-verifier] {message}", flush=True)
