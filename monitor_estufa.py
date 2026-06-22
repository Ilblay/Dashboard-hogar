"""
Monitor en vivo para diagnostico de Estufa 2.
Hace 12 lecturas con 15 segundos entre cada una (3 minutos total)
y muestra como cambia add_ele y cur_power.

Uso:
    python monitor_estufa.py
"""
from __future__ import annotations
import time
import datetime as dt
from pathlib import Path
import importlib.util

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fd", BASE / "fetch_data.py")
fd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fd)

cfg = fd.load_config()
client = fd.TuyaClient(cfg["tuya"]["access_id"], cfg["tuya"]["access_secret"], cfg["tuya"]["base_url"])

# Device IDs
DEVS = [
    ("Estufa 2", "ebaf20a100bb6117a6jeqy"),
    ("Estufa",   "eb790c104bcea8bc0asowa"),
    ("Fuente",   "eb52127ae37539c74aog5h"),
    ("Monitor",  "eb61b2b660407ab168f0p7"),
]

N_LECTURAS = 12
INTERVALO = 15  # segundos

print("=" * 70)
print(" Monitor en vivo - 3 minutos de lecturas cada 15 segundos")
print("=" * 70)
print(" Tip: prende la estufa al maximo ANTES de empezar")
print(" Si add_ele sube entre lecturas -> el plug si mide consumo")
print(" Si cur_power cambia -> el dato en vivo funciona")
print("=" * 70)
print()

ultimos = {}  # {dev_id: {"add_ele": X, "cur_power": Y}}

for i in range(N_LECTURAS):
    ahora = dt.datetime.now().strftime("%H:%M:%S")
    print(f"--- Lectura {i+1}/{N_LECTURAS} a las {ahora} ---")
    for dev_name, dev_id in DEVS:
        try:
            r = client.get_status(dev_id)
            if not r.get("success"):
                print(f"  [warn] {dev_name}: {r.get('msg', r)}")
                continue
            status = {it["code"]: it.get("value") for it in r.get("result", []) if it.get("code")}
            add_ele = status.get("add_ele")
            cur_power = status.get("cur_power")
            cur_current = status.get("cur_current")
            cur_voltage = status.get("cur_voltage")
            switch = status.get("switch_1") or status.get("switch")

            prev = ultimos.get(dev_id, {})
            d_ele = (add_ele - prev["add_ele"]) if prev.get("add_ele") is not None and add_ele is not None else None
            marker = ""
            if d_ele is not None and d_ele > 0:
                marker = f"  [+{d_ele}]"

            voltaje_v = (cur_voltage or 0) / 10
            corriente_a = (cur_current or 0) / 1000
            watts = (cur_power or 0) / 10  # asumiendo factor 0.1

            print(f"  {dev_name:10}  switch={switch}  add_ele={add_ele}{marker}  cur_power={cur_power} (~{watts:.0f}W)  V={voltaje_v:.1f}  I={corriente_a:.2f}A")
            ultimos[dev_id] = {"add_ele": add_ele, "cur_power": cur_power}
        except Exception as e:
            print(f"  [error] {dev_name}: {e}")
    print()
    if i < N_LECTURAS - 1:
        time.sleep(INTERVALO)

print("=" * 70)
print(" Fin del monitor. Analiza la salida:")
print("=" * 70)
print(" - Si add_ele NO cambio nunca:    el plug no esta midiendo (sensor mal)")
print(" - Si add_ele subio 0 -> 1 -> 2:  esta midiendo, pero lento")
print(" - Si cur_power cambio:           el dato en vivo funciona, solo delay")
print(" - Si cur_power siempre 0:        Tuya cachea, no reporta vivo")
print("=" * 70)
input("Presiona Enter para cerrar...")
