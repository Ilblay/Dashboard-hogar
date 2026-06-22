"""
Dashboard Hogar - Plan B (muestreo via /status)
================================================
Como los endpoints /statistics/* requieren APIs de pago, usamos el endpoint
/v1.0/devices/{id}/status (que SI funciona con IoT Core) y construimos
nuestro propio historico tomando muestras periodicas.

En cada ejecucion:
  - Llamamos /status de cada enchufe
  - Extraemos 'add_ele' (kWh acumulado historico del dispositivo)
  - Calculamos delta vs la ultima muestra guardada
  - Atribuimos el delta a la hora actual y tramo correspondiente
  - Acumulamos en historico.json

Para que sea util:
  - Hay que ejecutarlo periodicamente (cada 15-30 min idealmente)
  - Usar Programador de Tareas de Windows (ver README)
"""

from __future__ import annotations
import hmac, hashlib, time, json, sys
import datetime as dt
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qsl

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data"
HISTORICO_PATH = DATA_DIR / "historico.json"
DASHBOARD_PATH = BASE_DIR / "dashboard.html"
TEMPLATE_PATH = BASE_DIR / "dashboard_template.html"
LOG_PATH = DATA_DIR / "ultimo_update.json"
DATA_DIR.mkdir(exist_ok=True)

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


# ------------------ TUYA API CLIENT ------------------

def _normalize_path(path: str) -> str:
    sp = urlsplit(path)
    if not sp.query:
        return path
    params = parse_qsl(sp.query, keep_blank_values=True)
    params.sort(key=lambda kv: kv[0])
    return f"{sp.path}?{urlencode(params)}"


class TuyaClient:
    def __init__(self, access_id, access_secret, base_url):
        self.access_id = access_id
        self.access_secret = access_secret
        self.base_url = base_url.rstrip("/")
        self._token = None
        self._token_expiry = 0

    def _sign(self, method, path, access_token="", body=""):
        t = str(int(time.time() * 1000))
        nonce = ""
        content_sha = hashlib.sha256(body.encode("utf-8")).hexdigest() if body else EMPTY_SHA256
        path_norm = _normalize_path(path)
        string_to_sign = f"{method}\n{content_sha}\n\n{path_norm}"
        if access_token:
            str_to_hash = self.access_id + access_token + t + nonce + string_to_sign
        else:
            str_to_hash = self.access_id + t + nonce + string_to_sign
        sign = hmac.new(self.access_secret.encode("utf-8"),
                        str_to_hash.encode("utf-8"),
                        hashlib.sha256).hexdigest().upper()
        return sign, t, path_norm

    def _ensure_token(self):
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        sign, t, path_norm = self._sign("GET", "/v1.0/token?grant_type=1")
        r = requests.get(self.base_url + path_norm,
                         headers={"client_id": self.access_id, "sign": sign, "t": t,
                                  "sign_method": "HMAC-SHA256"},
                         timeout=20)
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"Token: {data}")
        self._token = data["result"]["access_token"]
        self._token_expiry = time.time() + int(data["result"].get("expire_time", 7200))
        return self._token

    def get(self, path):
        token = self._ensure_token()
        sign, t, path_norm = self._sign("GET", path, access_token=token)
        r = requests.get(self.base_url + path_norm,
                         headers={"client_id": self.access_id, "access_token": token,
                                  "sign": sign, "t": t, "sign_method": "HMAC-SHA256"},
                         timeout=30)
        return r.json()

    def get_status(self, device_id):
        return self.get(f"/v1.0/devices/{device_id}/status")

    def get_device_info(self, device_id):
        return self.get(f"/v1.0/devices/{device_id}")

    def get_device_logs(self, device_id, start_time_ms, end_time_ms, codes=None, size=100, last_row_key=""):
        """
        Obtiene los logs/eventos de un dispositivo entre dos timestamps (ms).
        Endpoint v2.0 (nuevo): /v2.0/cloud/thing/{device_id}/report-logs
        Devuelve hasta 100 eventos por pagina; usar last_row_key para paginar.
        """
        path = f"/v2.0/cloud/thing/{device_id}/report-logs?start_time={start_time_ms}&end_time={end_time_ms}&size={size}"
        if codes:
            path += f"&codes={codes}"
        if last_row_key:
            path += f"&last_row_key={last_row_key}"
        return self.get(path)

    def fetch_all_logs(self, device_id, start_time_ms, end_time_ms, codes="cur_power,switch_1,add_ele,switch_led,bright_value,bright_value_v2", verbose=False):
        """Pagina todos los logs del rango y devuelve lista completa de eventos."""
        all_events = []
        last_row_key = ""
        max_pages = 50  # safety guard
        for _ in range(max_pages):
            r = self.get_device_logs(device_id, start_time_ms, end_time_ms, codes=codes, last_row_key=last_row_key)
            if not r.get("success"):
                if verbose:
                    print(f"     [warn] logs: {r.get('msg', r)}")
                break
            result = r.get("result", {})
            logs = result.get("logs", [])
            all_events.extend(logs)
            if not result.get("has_next"):
                break
            last_row_key = result.get("last_row_key", "")
            if not last_row_key:
                break
        return all_events


# ------------------ BILLING PERIOD ------------------

def current_billing_period(today, start_day):
    if today.day >= start_day:
        start = today.replace(day=start_day)
    else:
        prev_month = today.replace(day=1) - dt.timedelta(days=1)
        start = prev_month.replace(day=start_day)
    next_month = (start.replace(day=28) + dt.timedelta(days=8)).replace(day=1)
    end = next_month.replace(day=start_day) - dt.timedelta(days=1)
    return start, end


def previous_billing_period(today, start_day):
    cur_start, _ = current_billing_period(today, start_day)
    prev_end = cur_start - dt.timedelta(days=1)
    prev_start = (prev_end.replace(day=1) - dt.timedelta(days=1)).replace(day=start_day)
    if prev_start > prev_end:
        prev_start = prev_end.replace(day=start_day) - dt.timedelta(days=30)
    return prev_start, prev_end


def tramo_of_hour(hour, tramos_cfg):
    for nombre, info in tramos_cfg.items():
        if hour in info["horas"]:
            return nombre
    return "dia"


# ------------------ SAMPLE & RECORD ------------------

def parse_status_dict(status_list):
    """Tuya status devuelve [{code: 'X', value: ...}, ...]. Convertir a dict."""
    if not isinstance(status_list, list):
        return {}
    return {it.get("code"): it.get("value") for it in status_list if it.get("code")}


def sample_and_record(client, dev_id, dev_name, historico, verbose=True, dev_type="SMART_PLUG", wattage_nominal=10):
    """
    Pide /status, calcula consumo desde la muestra anterior y lo suma al hourly.
    Maneja DOS tipos de dispositivos:
      - SMART_PLUG: usa cur_power (W reales) * tiempo
      - LAMP: usa wattage_nominal * (bright/1000) * tiempo cuando switch_led=True
    """
    historico.setdefault("hourly", {}).setdefault(dev_id, {})
    historico.setdefault("samples", {}).setdefault(dev_id, [])

    r = client.get_status(dev_id)
    if not r.get("success"):
        if verbose:
            print(f"     [warn] status: {r.get('msg', r)}")
        return None

    status = parse_status_dict(r.get("result", []))
    if verbose:
        print(f"     status completo:")
        for k, v in status.items():
            print(f"       {k} = {v}")

    add_ele = status.get("add_ele")
    cur_power = status.get("cur_power")
    cur_voltage = status.get("cur_voltage")
    cur_current = status.get("cur_current")
    switch = status.get("switch_1") or status.get("switch")
    if cur_power is None:
        cur_power = status.get("power") or status.get("cur_power_a")
    if add_ele is None:
        add_ele = status.get("energy_total") or status.get("total_energy") or status.get("ele")

    # Datos especificos de lampara
    switch_led = status.get("switch_led")
    if isinstance(switch_led, str):
        switch_led = switch_led.lower() == "true"
    bright_value = status.get("bright_value") or status.get("bright_value_v2")

    # Para lamparas: calcular "potencia efectiva" en W
    pot_efectiva_w = None
    if dev_type == "LAMP":
        if switch_led and bright_value:
            try:
                pot_efectiva_w = wattage_nominal * (int(bright_value) / 1000)
            except (TypeError, ValueError):
                pot_efectiva_w = 0
        else:
            pot_efectiva_w = 0
        # Usar pot_efectiva como cur_power para guardar historicamente (ya en W reales)
        # Multiplicar por 10 para mantener convencion "raw cur_power = W * 10"
        cur_power = pot_efectiva_w * 10

    now = dt.datetime.now()
    now_iso = now.isoformat(timespec="seconds")

    sample = {
        "t": now_iso,
        "add_ele": add_ele,
        "cur_power": cur_power,
        "cur_voltage": cur_voltage,
        "cur_current": cur_current,
        "switch": switch if dev_type != "LAMP" else switch_led,
        "bright_value": bright_value,
        "pot_efectiva_w": pot_efectiva_w,
        "dev_type": dev_type,
    }
    samples_list = historico["samples"][dev_id]
    last_sample = samples_list[-1] if samples_list else None
    samples_list.append(sample)
    # Solo se usa la ultima muestra para calcular el delta de consumo, por lo que
    # mantener un buffer pequeno (50) evita que el archivo se infle a varios MB
    # y genere diffs gigantes que cuelgan el editor / asistente.
    if len(samples_list) > 50:
        del samples_list[0: len(samples_list) - 50]

    if verbose:
        if add_ele is not None:
            print(f"     add_ele: {add_ele} | cur_power: {cur_power} W")
        else:
            print(f"     [info] sin add_ele en status; codes={list(status.keys())}")

    # --- ESTIMACION DE CONSUMO VIA CUR_POWER * TIEMPO ---
    # Mas confiable que add_ele (que reporta en bursts).
    # cur_power viene en 0.1W (factor 0.1), asumimos que fue el consumo promedio
    # entre la muestra anterior y esta.
    if last_sample and cur_power is not None and last_sample.get("cur_power") is not None:
        try:
            t_now = dt.datetime.fromisoformat(now_iso)
            t_prev = dt.datetime.fromisoformat(last_sample["t"])
            delta_seconds = (t_now - t_prev).total_seconds()
            # Cap a 6h para evitar sobreestimar si el dispositivo ciclo durante un gap muy largo
            if 0 < delta_seconds <= 21600:
                power_actual_w = float(cur_power) * 0.1
                power_prev_w = float(last_sample["cur_power"]) * 0.1
                power_promedio_w = (power_actual_w + power_prev_w) / 2
                # Distribuir el consumo prorrateado entre las horas que cubre el intervalo
                # (en vez de meterlo todo en la hora actual, que sesgaba para gaps largos)
                t = t_prev
                total_kwh = 0.0
                while t < t_now:
                    hour_start = t.replace(minute=0, second=0, microsecond=0)
                    segment_end = min(hour_start + dt.timedelta(hours=1), t_now)
                    segment_secs = (segment_end - t).total_seconds()
                    if segment_secs <= 0:
                        break
                    segment_kwh = power_promedio_w * (segment_secs / 3600) / 1000
                    if segment_kwh > 0.0001:
                        # scale_factor = 0.01 -> raw * 0.01 = kWh -> raw = kwh * 100
                        raw_value = segment_kwh * 100
                        hour_key = t.strftime("%Y%m%d%H")
                        historico["hourly"][dev_id][hour_key] = historico["hourly"][dev_id].get(hour_key, 0.0) + raw_value
                        total_kwh += segment_kwh
                    t = segment_end
                if verbose and total_kwh > 0.0001:
                    horas_str = f"{delta_seconds/3600:.2f}h" if delta_seconds >= 3600 else f"{delta_seconds:.0f}s"
                    print(f"     [consumo] {total_kwh:.4f} kWh en {horas_str} (prom {power_promedio_w:.0f}W)")
            elif verbose and delta_seconds > 21600:
                print(f"     [warn] gap de {delta_seconds/3600:.1f}h descartado (>6h)")
        except Exception as e:
            if verbose:
                print(f"     [warn] calculo consumo: {e}")

    # Nota: NO usamos add_ele para sumar al historico (lo hace cur_power*tiempo arriba)
    # add_ele queda solo como dato informativo en samples para verificar consistencia
    return sample


def sync_device_logs(client, dev_id, dev_name, historico, tramos_cfg, verbose=True, lookback_days=7, dev_type="SMART_PLUG"):
    """
    Pull incremental de logs de Tuya (eventos al segundo).
    Calcula consumo desde los eventos y lo suma al historico.
    No tiene problema con saltos del cron - los eventos siempre existen en Tuya.

    historico["events"][dev_id] = lista cronologica de eventos {t, code, value}
    historico["last_event_ts"][dev_id] = ultimo timestamp procesado
    """
    import time as _time
    historico.setdefault("events", {}).setdefault(dev_id, [])
    historico.setdefault("last_event_ts", {})

    now_ms = int(_time.time() * 1000)
    last_ts = historico["last_event_ts"].get(dev_id, 0)
    # Si nunca procesamos antes, empezar lookback_days atras
    start_ms = last_ts + 1 if last_ts > 0 else (now_ms - lookback_days * 24 * 3600 * 1000)

    if verbose:
        delta_h = (now_ms - start_ms) / 3600000
        print(f"     pulling logs desde hace {delta_h:.1f}h...")

    # Codes a pedir segun tipo de dispositivo
    if dev_type == "LAMP":
        codes = "switch_led,bright_value,bright_value_v2"
    else:
        codes = "cur_power,switch_1"
    new_events = client.fetch_all_logs(dev_id, start_ms, now_ms, codes=codes, verbose=verbose)
    if verbose:
        print(f"     {len(new_events)} eventos nuevos recibidos")

    # Tuya devuelve eventos en orden descendente (mas reciente primero). Invertir.
    new_events.sort(key=lambda e: int(e.get("event_time", 0)))

    # Agregar al historico
    for ev in new_events:
        historico["events"][dev_id].append({
            "t": int(ev.get("event_time", 0)),
            "code": ev.get("code"),
            "value": ev.get("value"),
            "from": ev.get("event_from"),
        })

    if new_events:
        historico["last_event_ts"][dev_id] = max(int(e.get("event_time", 0)) for e in new_events)

    # Limitar historial a ultimos 90 dias para no inflar el archivo
    cutoff = now_ms - 90 * 24 * 3600 * 1000
    historico["events"][dev_id] = [e for e in historico["events"][dev_id] if e["t"] >= cutoff]

    return new_events


def events_to_hourly_kwh_lamp(events_list, wattage_nominal=10):
    """
    Para lamparas: reconstruye el estado on/off y brillo a lo largo del tiempo,
    y calcula consumo asumiendo potencia = wattage * (bright / 1000) durante encendida.
    Devuelve {YYYYMMDDHH: valor_raw_para_aggregate} con scale_factor 0.01.
    """
    import datetime as _dt
    # Solo eventos relevantes, ordenados
    evs = [e for e in events_list if e.get("code") in ("switch_led", "bright_value", "bright_value_v2")]
    evs.sort(key=lambda e: e["t"])
    if not evs:
        return {}

    hourly = {}
    # Estado inicial: asumimos OFF, brillo maximo (los eventos van actualizando)
    is_on = False
    bright = 1000
    prev_t = None

    def emit_consumption(t_start_ms, t_end_ms, power_w):
        """Agregar consumo a hourly buckets, repartido si cruza horas."""
        if t_end_ms <= t_start_ms or power_w <= 0:
            return
        # Ir hora por hora desde start hasta end
        cur = t_start_ms
        while cur < t_end_ms:
            dt_cur = _dt.datetime.fromtimestamp(cur / 1000)
            next_hour_dt = (dt_cur.replace(minute=0, second=0, microsecond=0) + _dt.timedelta(hours=1))
            next_hour_ms = int(next_hour_dt.timestamp() * 1000)
            slice_end = min(next_hour_ms, t_end_ms)
            delta_s = (slice_end - cur) / 1000
            wh = power_w * (delta_s / 3600)
            kwh = wh / 1000
            if kwh > 0:
                hour_key = dt_cur.strftime("%Y%m%d%H")
                raw_value = kwh * 100  # para scale_factor 0.01 del config
                hourly[hour_key] = hourly.get(hour_key, 0) + raw_value
            cur = slice_end

    for ev in evs:
        t_ms = ev["t"]
        # Si estaba prendida desde el evento anterior, contabilizar consumo hasta ahora
        if prev_t is not None and is_on:
            power_w = wattage_nominal * (bright / 1000)
            emit_consumption(prev_t, t_ms, power_w)

        # Actualizar estado segun el evento
        code = ev.get("code")
        val = ev.get("value")
        if code == "switch_led":
            try:
                # value puede venir como "true"/"false" o bool
                if isinstance(val, str):
                    is_on = val.lower() == "true"
                else:
                    is_on = bool(val)
            except Exception:
                pass
        elif code in ("bright_value", "bright_value_v2"):
            try:
                b = int(val)
                if 0 < b <= 1000:
                    bright = b
            except (TypeError, ValueError):
                pass

        prev_t = t_ms

    return hourly


def events_to_hourly_kwh(events_list, scale=0.0001):
    """
    Convierte eventos de cur_power (raw) en consumo por hora (raw para aggregate).
    Logica: cada par consecutivo de eventos cur_power define un intervalo a potencia promedio.
    Energia (Wh) = (P1+P2)/2 * 0.1 (factor cur_power) * duracion_segundos / 3600
    Devuelve {YYYYMMDDHH: valor_raw_para_aggregate}

    scale: cur_power viene en *0.1 W, asi que dividimos por 10. Para que el aggregate del config
    (con scale_factor=0.01 = "raw * 0.01 = kWh") de el kWh correcto:
      kwh = (potencia_w * tiempo_h) / 1000
      raw_para_aggregate = kwh / scale_factor_config(0.01) = kwh * 100
    """
    import datetime as _dt
    # Filtrar solo eventos de cur_power y ordenar por tiempo
    power_events = [e for e in events_list if e.get("code") == "cur_power"]
    power_events.sort(key=lambda e: e["t"])

    hourly = {}
    for i in range(1, len(power_events)):
        prev = power_events[i-1]
        curr = power_events[i]
        try:
            p1 = float(prev["value"]) * 0.1  # cur_power raw -> watts
            p2 = float(curr["value"]) * 0.1
        except (TypeError, ValueError):
            continue
        t1_ms = prev["t"]
        t2_ms = curr["t"]
        delta_s = (t2_ms - t1_ms) / 1000
        if delta_s <= 0 or delta_s > 6 * 3600:  # skip gaps > 6h
            continue
        prom_w = (p1 + p2) / 2
        wh = prom_w * (delta_s / 3600)
        kwh = wh / 1000
        if kwh <= 0:
            continue
        # Atribuir al tramo de la hora media
        mid_ms = (t1_ms + t2_ms) / 2
        dt_mid = _dt.datetime.fromtimestamp(mid_ms / 1000)
        hour_key = dt_mid.strftime("%Y%m%d%H")
        # Convertir a valor "raw para aggregate" segun scale_factor 0.01 del config
        raw_value = kwh * 100
        hourly[hour_key] = hourly.get(hour_key, 0) + raw_value

    return hourly


# ------------------ AGGREGATION ------------------

def aggregate(devices_raw, devices_cfg, tramos_cfg, scale, start, end, manual_daily=None, distribucion=None, registros=None):
    """
    devices_raw: {device_id: {YYYYMMDDHH: valor_raw}}  - viene del muestreo /status
    manual_daily: {fecha_iso: {nombre_dispositivo: kwh}} - viene del config (app Smart Life)
    distribucion: {noche: 0.4, dia: 0.4, punta: 0.2} - proporcion para distribuir kWh diario por tramo
    registros: lista de {dispositivo, fecha, tramo, kwh} (registros_manuales del form).
               Se suman SIEMPRE, incluso si la fecha esta en manual_daily.
    Si un dia tiene data manual, se usa esa. Si no, se usa la del muestreo.
    """
    device_map = {d["id"]: d for d in devices_cfg}
    name_to_id = {d["name"]: d["id"] for d in devices_cfg}
    daily = {}
    tramos_totales = {nombre: {} for nombre in tramos_cfg}
    per_device = {}

    manual_daily = manual_daily or {}
    distribucion = distribucion or {"noche": 0.416, "dia": 0.363, "punta": 0.221}

    # Set de (dev_id, fecha_iso) que ya tienen data manual -> ignorar muestras para esos
    manual_keys = set()
    for fecha, dev_kwh in manual_daily.items():
        for dev_name, kwh in dev_kwh.items():
            dev_id = name_to_id.get(dev_name)
            if dev_id:
                manual_keys.add((dev_id, fecha))

    # 1) Procesar muestras horarias del /status
    for dev_id, hourly in devices_raw.items():
        dev_name = device_map.get(dev_id, {}).get("name", dev_id)
        per_device.setdefault(dev_name, {"noche": 0.0, "dia": 0.0, "punta": 0.0, "total": 0.0})
        for hkey, val in hourly.items():
            if len(hkey) < 10: continue
            try:
                d = dt.date(int(hkey[:4]), int(hkey[4:6]), int(hkey[6:8]))
                h = int(hkey[8:10])
            except ValueError: continue
            if not (start <= d <= end): continue
            if (dev_id, d.isoformat()) in manual_keys:
                continue  # ignorar muestras de dias con data manual
            kwh = float(val) * scale
            tramo = tramo_of_hour(h, tramos_cfg)
            day_str = d.isoformat()
            daily.setdefault(day_str, {})
            daily[day_str][dev_name] = daily[day_str].get(dev_name, 0.0) + kwh
            tramos_totales[tramo][dev_name] = tramos_totales[tramo].get(dev_name, 0.0) + kwh
            per_device[dev_name][tramo] += kwh
            per_device[dev_name]["total"] += kwh

    # 2) Procesar datos manuales (kWh diario, distribuido por tramo segun distribucion)
    for fecha_iso, dev_kwh in manual_daily.items():
        try:
            d = dt.date.fromisoformat(fecha_iso)
        except ValueError:
            continue
        if not (start <= d <= end):
            continue
        day_str = d.isoformat()
        for dev_name, kwh_total in dev_kwh.items():
            kwh_total = float(kwh_total)
            per_device.setdefault(dev_name, {"noche": 0.0, "dia": 0.0, "punta": 0.0, "total": 0.0})
            daily.setdefault(day_str, {})
            daily[day_str][dev_name] = daily[day_str].get(dev_name, 0.0) + kwh_total
            for tramo, prop in distribucion.items():
                kwh_t = kwh_total * prop
                tramos_totales[tramo][dev_name] = tramos_totales[tramo].get(dev_name, 0.0) + kwh_t
                per_device[dev_name][tramo] += kwh_t
            per_device[dev_name]["total"] += kwh_total

    # 3) Procesar registros_manuales (form del dashboard).
    # Se suman SIEMPRE, sin filtrar por manual_keys (a diferencia de las muestras).
    name_to_cfg = {d["name"]: d for d in devices_cfg}
    for reg in (registros or []):
        try:
            dev_name = reg.get("dispositivo")
            if dev_name not in name_to_cfg:
                continue
            d = dt.date.fromisoformat(reg.get("fecha", ""))
            if not (start <= d <= end):
                continue
            tramo = reg.get("tramo", "dia")
            if tramo not in tramos_totales:
                continue
            kwh = float(reg.get("kwh", 0))
            if kwh <= 0:
                continue
            day_str = d.isoformat()
            per_device.setdefault(dev_name, {"noche": 0.0, "dia": 0.0, "punta": 0.0, "total": 0.0})
            daily.setdefault(day_str, {})
            daily[day_str][dev_name] = daily[day_str].get(dev_name, 0.0) + kwh
            tramos_totales[tramo][dev_name] = tramos_totales[tramo].get(dev_name, 0.0) + kwh
            per_device[dev_name][tramo] += kwh
            per_device[dev_name]["total"] += kwh
        except Exception as e:
            print(f"     [warn] registro manual ignorado: {e}")

    totales_tramo = {nombre: sum(devs.values()) for nombre, devs in tramos_totales.items()}
    return {"daily": daily, "tramos_totales": tramos_totales,
            "per_device": per_device, "totales_tramo": totales_tramo,
            "kwh_total": sum(d["total"] for d in per_device.values())}


def calcular_costo(totales_tramo, tarifa, dias_periodo):
    costo_energia = 0.0
    desglose = {}
    for nombre, kwh in totales_tramo.items():
        precio = tarifa["tramos"][nombre]["precio_kwh"]
        costo = kwh * precio
        desglose[nombre] = {"kwh": kwh, "precio_kwh": precio, "costo": costo}
        costo_energia += costo
    cargo_fijo = tarifa["cargo_fijo_mensual"] * (dias_periodo / 30.0)
    return {"energia": round(costo_energia), "cargo_fijo": round(cargo_fijo),
            "total": round(costo_energia + cargo_fijo), "desglose_tramo": desglose}


# ------------------ I/O ------------------

def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    # Si hay env vars (caso GitHub Actions), sobrescribir credenciales
    import os as _os
    env_id = _os.environ.get("TUYA_ACCESS_ID")
    env_secret = _os.environ.get("TUYA_ACCESS_SECRET")
    if env_id and env_secret:
        cfg["tuya"]["access_id"] = env_id
        cfg["tuya"]["access_secret"] = env_secret
        print("  [env] usando credenciales de variables de entorno (GitHub Actions)")
    return cfg
def load_historico():
    if HISTORICO_PATH.exists():
        try:
            with open(HISTORICO_PATH, encoding="utf-8") as f: return json.load(f)
        except Exception: return {}
    return {}
def save_historico(data):
    with open(HISTORICO_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
def render_dashboard(payload):
    with open(TEMPLATE_PATH, encoding="utf-8") as f: tpl = f.read()
    return tpl.replace("/*__DATA__*/null", json.dumps(payload, indent=2, default=str, ensure_ascii=False))


# ------------------ MAIN ------------------

def main():
    print("Dashboard Hogar - Plan B (muestreo via /status)")
    cfg = load_config()
    client = TuyaClient(cfg["tuya"]["access_id"], cfg["tuya"]["access_secret"], cfg["tuya"]["base_url"])

    today = dt.date.today()
    start_day = cfg["billing_period"]["dia_inicio"]
    cur_start, cur_end = current_billing_period(today, start_day)
    prev_start, prev_end = previous_billing_period(today, start_day)

    devices_track = [d for d in cfg["devices"] if d.get("track_energy")]
    print(f"  Trackeando {len(devices_track)} dispositivos.")
    print(f"  Periodo actual: {cur_start} -> {cur_end}")

    historico = load_historico()
    tramos_cfg = cfg["tariff"]["tramos"]
    scale = float(cfg["fetch_options"]["scale_factor"])

    # device_status_live: estado actual de TODOS los dispositivos (incluyendo lamparas)
    device_status_live = {}

    # Muestreo en RAFAGA: varios ciclos que leen todos los enchufes,
    # para capturar mejor el ciclo on/off de la estufa con termostato.
    N_CICLOS = 4
    INTERVALO_CICLOS = 15  # segundos entre ciclos
    print(f"  Muestreo: {N_CICLOS} ciclos separados por {INTERVALO_CICLOS}s (~{N_CICLOS*INTERVALO_CICLOS}s total)")

    # Inicializar mapa para guardar la ultima sample por dispositivo (para device_status_live)
    last_samples_map = {}

    # Muestreo en RAFAGA via /status para TODOS los dispositivos (enchufes y lamparas)
    # Lamparas usan wattage_nominal * (bright/1000) cuando switch_led = true
    # (NO usamos /logs porque requiere API service no disponible en free trial)
    for ciclo in range(N_CICLOS):
        if ciclo > 0:
            print(f"  ... ciclo {ciclo+1}/{N_CICLOS} (esperando {INTERVALO_CICLOS}s)")
            time.sleep(INTERVALO_CICLOS)
        for dev in devices_track:
            dev_id = dev["id"]; dev_name = dev["name"]
            dev_type = dev.get("type", "SMART_PLUG")
            wattage = dev.get("wattage_nominal", 10)
            if ciclo == 0:
                print(f"  -> {dev_name} (muestra, type={dev_type})")
            try:
                sample = sample_and_record(client, dev_id, dev_name, historico,
                                           verbose=(ciclo == 0), dev_type=dev_type, wattage_nominal=wattage)
                if sample is not None:
                    last_samples_map[dev_name] = (dev, sample)
            except Exception as e:
                print(f"     [error] {e}")

    # Despues de los ciclos, construir device_status_live a partir de la ultima sample
    for dev_name, (dev, sample) in last_samples_map.items():
        device_status_live[dev_name] = {
            "id": dev["id"],
            "type": dev.get("type", "SMART_PLUG"),
            "track_energy": True,
            "switch": sample.get("switch"),
            "cur_power": sample.get("cur_power"),
            "cur_voltage": sample.get("cur_voltage"),
            "cur_current": sample.get("cur_current"),
            "add_ele_total": sample.get("add_ele"),
            "last_seen": sample.get("t"),
        }

    # Muestreo de estado de TODAS las lamparas (siempre, sin importar track_energy)
    lamparas = [d for d in cfg["devices"] if d.get("type") == "LAMP"]
    for dev in lamparas:
        dev_id = dev["id"]; dev_name = dev["name"]
        print(f"  -> {dev_name} (estado lampara)")
        try:
            r = client.get_status(dev_id)
            if r.get("success"):
                status = parse_status_dict(r.get("result", []))
                # value puede ser bool real o string
                sw = status.get("switch_led")
                if sw is None:
                    sw = status.get("switch_1") or status.get("switch")
                if isinstance(sw, str):
                    sw = sw.lower() == "true"
                bright = status.get("bright_value") or status.get("bright_value_v2")
                # Calcular potencia efectiva si está prendida
                wattage = dev.get("wattage_nominal", 10)
                pot_efectiva = (wattage * (int(bright) / 1000)) if (sw and bright) else 0
                device_status_live[dev_name] = {
                    "id": dev_id,
                    "type": dev.get("type", "LAMP"),
                    "track_energy": dev.get("track_energy", False),
                    "switch": sw,
                    "bright": bright,
                    "work_mode": status.get("work_mode"),
                    "cur_power": pot_efectiva * 10 if pot_efectiva else 0,  # *10 para que el frontend que divide por 10 muestre los W reales
                    "wattage_nominal": wattage,
                    "last_seen": dt.datetime.now().isoformat(timespec="seconds"),
                }
                if device_status_live[dev_name]["switch"] is not None:
                    print(f"     switch: {sw}, bright: {bright}, potencia efectiva: {pot_efectiva:.1f}W")
            else:
                print(f"     [warn] {r.get('msg', r)}")
        except Exception as e:
            print(f"     [error] {e}")

    # (Desactivado el sync de /logs porque ese endpoint no esta autorizado en plan free.
    # El consumo viene 100% del polling /status hecho arriba en el muestreo en rafaga.)

    # Los REGISTROS MANUALES se procesan al construir el payload (no al historico)
    # asi se aplican UNA SOLA VEZ por ejecucion y no se duplican.

    save_historico(historico)

    # Agregar y construir payload
    devices_raw_current = {
        dev_id: {k: v for k, v in (historico.get("hourly", {}).get(dev_id, {})).items()
                 if cur_start.strftime("%Y%m%d") <= k[:8] <= cur_end.strftime("%Y%m%d")}
        for dev in devices_track for dev_id in [dev["id"]]
    }
    devices_raw_previous = {
        dev_id: {k: v for k, v in (historico.get("hourly", {}).get(dev_id, {})).items()
                 if prev_start.strftime("%Y%m%d") <= k[:8] <= prev_end.strftime("%Y%m%d")}
        for dev in devices_track for dev_id in [dev["id"]]
    }

    # Datos manuales del config (de la app Smart Life)
    manual_cfg = cfg.get("consumo_diario_manual", {})
    manual_daily = manual_cfg.get("datos", {})
    distribucion = manual_cfg.get("distribucion_tramos")

    # REGISTROS MANUALES (form del dashboard) se pasan a aggregate() para que se
    # sumen SIEMPRE, sin que el filtro manual_keys los descarte cuando la fecha
    # tambien esta en consumo_diario_manual.datos.
    registros = cfg.get("registros_manuales", {}).get("entradas", [])

    agg_cur = aggregate(devices_raw_current, cfg["devices"], tramos_cfg, scale, cur_start, cur_end,
                        manual_daily=manual_daily, distribucion=distribucion, registros=registros)
    agg_prev = aggregate(devices_raw_previous, cfg["devices"], tramos_cfg, scale, prev_start, prev_end,
                         manual_daily=manual_daily, distribucion=distribucion, registros=registros)

    dias_cur = (cur_end - cur_start).days + 1
    dias_prev = (prev_end - prev_start).days + 1
    dias_transcurridos = (today - cur_start).days + 1

    costo_cur = calcular_costo(agg_cur["totales_tramo"], cfg["tariff"], dias_transcurridos)
    costo_prev_tuya = calcular_costo(agg_prev["totales_tramo"], cfg["tariff"], dias_prev)

    kwh_por_dia = agg_cur["kwh_total"] / max(dias_transcurridos, 1)
    kwh_proyectado = kwh_por_dia * dias_cur
    if agg_cur["kwh_total"] > 0:
        costo_energia_proy = costo_cur["energia"] * (kwh_proyectado / agg_cur["kwh_total"])
    else:
        costo_energia_proy = 0
    costo_proyectado = round(costo_energia_proy + cfg["tariff"]["cargo_fijo_mensual"])

    boletas = cfg.get("boletas_historicas", {})
    ultima_boleta = boletas.get("ultima_boleta")
    if ultima_boleta and agg_prev["kwh_total"] < 1.0:
        kwh_n = ultima_boleta["kwh_noche"]; kwh_d = ultima_boleta["kwh_dia"]; kwh_p = ultima_boleta["kwh_punta"]
        periodo_anterior = {
            "inicio": ultima_boleta["periodo_inicio"], "fin": ultima_boleta["periodo_fin"],
            "dias_total": dias_prev, "kwh_total": ultima_boleta["kwh_total"],
            "costo": {"energia": ultima_boleta["costo_energia_noche"]+ultima_boleta["costo_energia_dia"]+ultima_boleta["costo_energia_punta"]+ultima_boleta["costo_transporte"],
                      "cargo_fijo": ultima_boleta["costo_administracion"], "total": ultima_boleta["costo_total"],
                      "desglose_tramo": {
                          "noche": {"kwh": kwh_n, "precio_kwh": ultima_boleta["costo_energia_noche"]/max(kwh_n,1), "costo": ultima_boleta["costo_energia_noche"]},
                          "dia":   {"kwh": kwh_d, "precio_kwh": ultima_boleta["costo_energia_dia"]/max(kwh_d,1),   "costo": ultima_boleta["costo_energia_dia"]},
                          "punta": {"kwh": kwh_p, "precio_kwh": ultima_boleta["costo_energia_punta"]/max(kwh_p,1), "costo": ultima_boleta["costo_energia_punta"]}}},
            "per_device": {}, "totales_tramo": {"noche": kwh_n, "dia": kwh_d, "punta": kwh_p}, "fuente": "boleta",
        }
    else:
        periodo_anterior = {"inicio": prev_start.isoformat(), "fin": prev_end.isoformat(),
                            "dias_total": dias_prev, "kwh_total": round(agg_prev["kwh_total"], 3),
                            "costo": costo_prev_tuya, "per_device": agg_prev["per_device"],
                            "totales_tramo": agg_prev["totales_tramo"], "fuente": "muestreo"}

    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "tarifa": cfg["tariff"],
        "periodo_actual": {
            "inicio": cur_start.isoformat(), "fin": cur_end.isoformat(),
            "dias_total": dias_cur, "dias_transcurridos": dias_transcurridos,
            "kwh_total": round(agg_cur["kwh_total"], 3),
            "kwh_proyectado": round(kwh_proyectado, 1),
            "costo": costo_cur, "costo_proyectado": costo_proyectado,
            "per_device": agg_cur["per_device"], "daily": agg_cur["daily"],
            "totales_tramo": agg_cur["totales_tramo"],
        },
        "periodo_anterior": periodo_anterior,
        "historico_anual": boletas.get("consumo_mensual_13m", []),
        "ultima_boleta": ultima_boleta,
        "dispositivos_tracked": [d["name"] for d in devices_track],
        "dispositivos_no_tracked": [d["name"] for d in cfg["devices"] if not d.get("track_energy")],
        "dispositivos_meta": {d["name"]: {"type": d.get("type", "SMART_PLUG"),
                                          "wattage_nominal": d.get("wattage_nominal")}
                              for d in cfg["devices"]},
        "device_status_live": device_status_live,
        "modo": "muestreo_periodico+manual",
        "distribucion_tramos": cfg.get("consumo_diario_manual", {}).get("distribucion_tramos", {"noche": 0.416, "dia": 0.363, "punta": 0.221}),
    }

    # --- ALERTAS ---
    presupuesto = cfg.get("presupuesto")
    if presupuesto:
        objetivo = presupuesto["objetivo_mensual_clp"]
        umbral_proy_pct = presupuesto.get("umbral_proyeccion_pct", 90)
        umbral_critico_pct = presupuesto.get("umbral_proyeccion_critico_pct", 100)
        kwh_dia_max = presupuesto.get("kwh_dia_max_total", 0)
        por_dev = presupuesto.get("por_dispositivo", {})

        alertas = []
        # 1) Proyeccion vs objetivo
        pct = (costo_proyectado / objetivo) * 100 if objetivo > 0 else 0
        if pct >= umbral_critico_pct:
            sobre = costo_proyectado - objetivo
            alertas.append({
                "nivel": "critico",
                "tipo": "proyeccion",
                "titulo": f"Proyeccion supera el objetivo (${costo_proyectado:,} CLP)".replace(",", "."),
                "detalle": f"A este ritmo terminaras pagando {pct:.0f}% del objetivo de ${objetivo:,} CLP. Sobre el limite: ${sobre:,} CLP.".replace(",", "."),
            })
        elif pct >= umbral_proy_pct:
            alertas.append({
                "nivel": "advertencia",
                "tipo": "proyeccion",
                "titulo": f"Te acercas al limite ({pct:.0f}%)",
                "detalle": f"Proyectado ${costo_proyectado:,} CLP de un objetivo de ${objetivo:,} CLP.".replace(",", "."),
            })

        # 2) Consumo del dia mas reciente vs umbral total
        if agg_cur.get("daily") and kwh_dia_max > 0:
            ultimo_dia = sorted(agg_cur["daily"].keys())[-1]
            kwh_ultimo = sum(agg_cur["daily"][ultimo_dia].values())
            if kwh_ultimo > kwh_dia_max:
                alertas.append({
                    "nivel": "advertencia",
                    "tipo": "dia_total",
                    "titulo": f"Consumo del {ultimo_dia} supera el limite diario",
                    "detalle": f"{kwh_ultimo:.2f} kWh > {kwh_dia_max:.2f} kWh/dia (limite del presupuesto).",
                })

        # 3. Por dispositivo: revisar promedio diario
        if agg_cur.get("daily"):
            dias_orden = sorted(agg_cur["daily"].keys())
            for nombre, lim in por_dev.items():
                kwh_max_dev = lim.get("kwh_dia_max")
                if kwh_max_dev is None:
                    continue
                kwh_lista = [agg_cur["daily"][d].get(nombre, 0) for d in dias_orden]
                if not kwh_lista:
                    continue
                promedio = sum(kwh_lista) / len(kwh_lista)
                if promedio > kwh_max_dev:
                    alertas.append({
                        "nivel": "advertencia",
                        "tipo": "dispositivo",
                        "dispositivo": nombre,
                        "titulo": f"{nombre}: promedio diario sobre limite",
                        "detalle": f"Promedio {promedio:.2f} kWh/dia (limite: {kwh_max_dev} kWh/dia).",
                    })

        payload["presupuesto"] = presupuesto
        payload["alertas"] = alertas

    html = render_dashboard(payload)
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "ultimo_update": dt.datetime.now().isoformat(timespec="seconds"),
            "kwh_total_periodo": payload["periodo_actual"]["kwh_total"],
            "costo_proyectado": costo_proyectado,
        }, f, indent=2)

    fmt = lambda n: f"${n:,}".replace(",", ".")
    print(f"\n  Acumulado del periodo: {payload['periodo_actual']['kwh_total']} kWh ({fmt(costo_cur['total'])})")
    print(f"  Proyectado fin de periodo: {fmt(costo_proyectado)} CLP")
    if payload.get("alertas"):
        print(f"  Alertas activas: {len(payload['alertas'])}")
        for a in payload["alertas"]:
            print(f"    [{a['nivel']}] {a['titulo']}")
    print(f"  Dashboard: {DASHBOARD_PATH}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
