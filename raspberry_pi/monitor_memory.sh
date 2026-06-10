#!/usr/bin/env bash
# Speicher-/Swap-Beobachtung fuer den HS-IoT-Doorbell-Dienst.
#
# Zweck: Pruefen, ob sich Performance-Probleme ueber die Laufzeit aufbauen
# (Speicherverbrauch steigt, Swap fuellt sich, Dienst startet neu) und nach
# einem Reboot wieder verschwinden.
#
# Aufruf auf dem Raspberry Pi:
#   bash monitor_memory.sh            # alle 30 s, Log nach ~/hs-iot-mem.log
#   bash monitor_memory.sh 10         # alle 10 s
#   bash monitor_memory.sh 30 /tmp/m.log
#
# Beenden mit Strg+C.

SERVICE="hs-iot-doorbell"
MATCH="face_verifier.py"   # Prozess-Kommandozeile, falls nicht via systemd gefunden
INTERVAL="${1:-30}"
LOGFILE="${2:-$HOME/hs-iot-mem.log}"

echo "Beobachte Dienst '$SERVICE' alle ${INTERVAL}s. Log: $LOGFILE"
echo "Beenden mit Strg+C."
header="zeit                 rss_mb  mem_frei_mb  swap_used_mb  restarts  pid"
echo "$header" | tee -a "$LOGFILE"

while true; do
  pid=$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null)
  if [ -z "$pid" ] || [ "$pid" = "0" ]; then
    # Fallback: Prozess ueber die Kommandozeile suchen (z. B. manuell gestartet).
    pid=$(pgrep -f "$MATCH" | head -n1)
  fi
  restarts=$(systemctl show -p NRestarts --value "$SERVICE" 2>/dev/null)
  [ -z "$restarts" ] && restarts="-"

  if [ -n "$pid" ] && [ "$pid" != "0" ] && [ -r "/proc/$pid/status" ]; then
    rss_kb=$(awk '/VmRSS/{print $2}' "/proc/$pid/status")
    rss_mb=$(( rss_kb / 1024 ))
  else
    rss_mb="-"
    pid="-"
  fi

  mem_free_mb=$(free -m | awk '/Mem:/{print $7}')
  swap_used_mb=$(free -m | awk '/Swap:/{print $3}')
  ts=$(date '+%Y-%m-%d %H:%M:%S')

  printf '%s  %6s  %11s  %12s  %8s  %s\n' \
    "$ts" "$rss_mb" "$mem_free_mb" "$swap_used_mb" "$restarts" "$pid" \
    | tee -a "$LOGFILE"

  sleep "$INTERVAL"
done
