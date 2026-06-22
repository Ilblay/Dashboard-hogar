"""
Historial detallado de la Estufa - Ultimos 7 dias
==================================================
Consulta los logs de Tuya (eventos al milisegundo) de la Estufa
y genera reporte timeline con encendidos, apagados, duracion y consumo calculado.
Sirve para comparar contra la app Smart Life y validar el calculo.

Uso: python historial_estufa.py
     o doble-click en historial_estufa.bat
"""

from __future__ import annotations
import sys, json, datetime as dt
from pathlib import Path
import importlib.util

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fd", BASE / "fetch_data.py")
fd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fd)

# Configuracion: dispositivo a analizar
DEVICE_NAME = "Estufa"
DAYS = 7

# Cargar config
cfg = fd.load_config()
client = fd.TuyaClient(cfg["tuya"]["access_id"], cfg["tuya"]["access_secret"], cfg["tuya"]["base_url"])
tarifa = cfg["tariff"]
tramos_cfg = tarifa["tramos"]

# Encontrar device_id
dev = next((d for d in cfg["devices"] if d["name"] == DEVICE_NAME), None)
if dev is None:
    print(f"ERROR: no se encontro dispositivo '{DEVICE_NAME}' en config.json")
    sys.exit(1)
DEVICE_ID = dev["id"]

# Rango de fechas
now_ms = int(dt.datetime.now().timestamp() * 1000)
start_ms = now_ms - DAYS * 24 * 3600 * 1000
start_dt = dt.datetime.fromtimestamp(start_ms / 1000)
end_dt = dt.datetime.fromtimestamp(now_ms / 1000)

print("=" * 70)
print(f"  HISTORIAL DETALLADO - {DEVICE_NAME}")
print(f"  Desde: {start_dt.strftime('%Y-%m-%d %H:%M')}")
print(f"  Hasta: {end_dt.strftime('%Y-%m-%d %H:%M')}")
print("=" * 70)
print()

print("Consultando logs en Tuya...")
events = client.fetch_all_logs(DEVICE_ID, start_ms, now_ms, codes="cur_power,switch_1", verbose=True)
print(f"  {len(events)} eventos recibidos\n")

if not events:
    print("No hay eventos en el rango. Posibles causas:")
    print("  - El dispositivo no estuvo activo")
    print("  - Tuya ya borro los logs (>7 dias)")
    print("  - Permisos de API insuficientes")
    sys.exit(0)

# Ordenar cronologicamente
events.sort(key=lambda e: int(e.get("event_time", 0)))

# Funcion helper: tramo y precio
def precio_kwh_hora(hour):
    for nombre, info in tramos_cfg.items():
        if hour in info["horas"]:
            return info["precio_kwh"], nombre
    return tramos_cfg["dia"]["precio_kwh"], "dia"


# Reconstruir timeline: pares consecutivos de cur_power = intervalo a potencia promedio
# y eventos switch_1 marcan encendidos/apagados
def fmt_dur(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{int(seconds/60)}min {int(seconds%60)}s"
    h = int(seconds / 3600)
    m = int((seconds % 3600) / 60)
    return f"{h}h {m}min"


# Agrupar eventos por dia
events_by_day = {}
for ev in events:
    t = int(ev["event_time"])
    d = dt.datetime.fromtimestamp(t / 1000).strftime("%Y-%m-%d")
    events_by_day.setdefault(d, []).append(ev)

dias_orden = sorted(events_by_day.keys())

total_kwh_periodo = 0
total_costo_periodo = 0

for dia in dias_orden:
    evs = events_by_day[dia]
    print(f"📅 {dia}  ({len(evs)} eventos)")
    print("─" * 60)

    # Reconstruir intervalos
    power_evs = [e for e in evs if e.get("code") == "cur_power"]
    switch_evs = [e for e in evs if e.get("code") == "switch_1"]

    # Encendidos / apagados
    if switch_evs:
        for sw in switch_evs:
            t_str = dt.datetime.fromtimestamp(int(sw["event_time"]) / 1000).strftime("%H:%M:%S")
            val = sw.get("value")
            is_on = str(val).lower() == "true" if isinstance(val, str) else bool(val)
            arrow = "✓ ON " if is_on else "✗ OFF"
            origen = sw.get("event_from", "?")
            print(f"  {t_str}  {arrow}  (origen: {origen})")

    # Consumo desde cur_power
    kwh_dia = 0
    costo_dia = 0
    minutos_en = 0
    for i in range(1, len(power_evs)):
        prev = power_evs[i-1]
        curr = power_evs[i]
        try:
            p1 = float(prev["value"]) * 0.1
            p2 = float(curr["value"]) * 0.1
        except (TypeError, ValueError):
            continue
        t1 = int(prev["event_time"])
        t2 = int(curr["event_time"])
        delta_s = (t2 - t1) / 1000
        if delta_s <= 0 or delta_s > 6 * 3600:
            continue
        prom_w = (p1 + p2) / 2
        wh = prom_w * (delta_s / 3600)
        kwh = wh / 1000
        if prom_w > 50:
            minutos_en += delta_s / 60
        # tramo de la hora media
        mid = (t1 + t2) / 2
        dt_mid = dt.datetime.fromtimestamp(mid / 1000)
        precio, tramo = precio_kwh_hora(dt_mid.hour)
        costo = kwh * precio
        kwh_dia += kwh
        costo_dia += costo

    fmt_clp = lambda n: f"${n:,.0f}".replace(",", ".")
    print(f"  ── Consumo del dia: {kwh_dia:.3f} kWh ({fmt_clp(costo_dia)})")
    print(f"  ── Tiempo estimado encendida: {fmt_dur(minutos_en * 60)}")
    print()
    total_kwh_periodo += kwh_dia
    total_costo_periodo += costo_dia

print("=" * 70)
print(f"  RESUMEN {DAYS} DIAS:")
print(f"  Total consumo:  {total_kwh_periodo:.3f} kWh")
print(f"  Total costo:    {fmt_clp(total_costo_periodo)} CLP")
print(f"  Promedio dia:   {total_kwh_periodo/max(1,len(dias_orden)):.3f} kWh / {fmt_clp(total_costo_periodo/max(1,len(dias_orden)))}")
print("=" * 70)
print()
print("Compara estos numeros con la app Smart Life para validar.")
print()
input("Presiona Enter para salir...")
