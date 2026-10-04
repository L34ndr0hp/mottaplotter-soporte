"""Calendario semanal para configuraciones GGv2 (json_version v4.x).

Modelo de datos GGv2 en GG_relay_control:
  channel_schedules[tipo]["N"]            → horario normal {name, config:[{days, status:{on|off}}]}
  channel_schedules[tipo]["special"]["N"] → horario especial (se usa en días especiales "automatico")
  devices[x].config_x_relay[relay]        → {schedule_type, schedule_number, config, registers}
  devices[x].special_days                 → lista (o dict) de {day_groups, config_x_relay}
  special_days[grupo]                     → ["DD-MM", ...]
  extensions                              → [{schedule_type, schedule_numbers, time_range, desired_status}]

Prioridad al calcular un día: extensión > día especial > horario regular.
"""
import json
from datetime import date, datetime, timedelta

DIA_SEG = 86400
PALETA = ["#F0BD00", "#3DBE6E", "#378ADD", "#E67E22", "#8E44AD",
          "#E74C3C", "#16A085", "#D35400", "#2980B9", "#C0392B"]


# ─── Utilidades ────────────────────────────────────────────────────────────────
def desenvolver_config(config_data):
    """Acepta la respuesta de la API ({identifier, config}), una lista o el config directo."""
    if isinstance(config_data, list):
        config_data = config_data[0]
    if "lambda_functions" not in config_data and isinstance(config_data.get("config"), dict):
        config_data = config_data["config"]
    return config_data


def es_config_ggv2(config_data):
    """True si la configuración usa el formato GGv2 (horarios numerados)."""
    cfg = desenvolver_config(config_data)
    if str(cfg.get("json_version", "")).startswith("v4"):
        return True
    rc = cfg.get("lambda_functions", {}).get("GG_relay_control", {})
    for body in (rc.get("channel_schedules") or {}).values():
        if isinstance(body, dict) and any(str(k).isdigit() for k in body):
            return True
    return False


def _seg(t):
    h, m, s = (int(x) for x in t.split(":"))
    return h * 3600 + m * 60 + s


def _seg_fin(t):
    """Los fines se guardan como HH:MM:59 → se muestran como el minuto siguiente."""
    s = _seg(t)
    return s + 1 if s % 60 == 59 else s


def _rangos(pares):
    out = []
    for p in pares or []:
        if isinstance(p, (list, tuple)) and len(p) == 2:
            s, e = _seg(p[0]), _seg_fin(p[1])
            if e > s:
                out.append((s, e))
    return out


def _complemento(rangos):
    out, cur = [], 0
    for s, e in sorted(rangos):
        if s > cur:
            out.append((cur, s))
        cur = max(cur, e)
    if cur < DIA_SEG:
        out.append((cur, DIA_SEG))
    return out


def _encendidos_status(status):
    if not isinstance(status, dict):
        return []
    if "on" in status:
        return _rangos(status["on"])
    if "off" in status:
        # Supuesto: fuera de los tramos "off" el canal queda encendido
        return _complemento(_rangos(status["off"]))
    return []


def _encendidos_entrada(entrada, weekday):
    res = []
    for item in (entrada or {}).get("config") or []:
        if isinstance(item, dict) and weekday in item.get("days", range(1, 8)):
            res.extend(_encendidos_status(item.get("status")))
    return res


def _norm_dia(d):
    partes = str(d).replace("/", "-").split("-")
    return f"{int(partes[0]):02d}-{int(partes[1]):02d}"


def _lista(valor):
    if isinstance(valor, dict):
        return [v for v in valor.values() if isinstance(v, dict)]
    if isinstance(valor, list):
        return [v for v in valor if isinstance(v, dict)]
    return []


def _parse_ext(s):
    dt = datetime.strptime(s.strip(), "%d-%m-%YT%H:%M:%S")
    return dt


def _pintar(segs, s, e, tipo, extra=""):
    """Sobrescribe [s,e) con un nuevo tramo (mayor prioridad)."""
    out = []
    for a, b, k, x in segs:
        if b <= s or a >= e:
            out.append((a, b, k, x))
            continue
        if a < s:
            out.append((a, s, k, x))
        if b > e:
            out.append((e, b, k, x))
    out.append((s, e, tipo, extra))
    return sorted(out)


def _fusionar(segs):
    out = []
    for a, b, k, x in sorted(segs):
        if out and out[-1][2] == k and out[-1][3] == x and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b), k, x)
        else:
            out.append((a, b, k, x))
    return out


def _lunes(d):
    return d - timedelta(days=d.weekday())


# ─── Modelo ────────────────────────────────────────────────────────────────────
def _catalogo_series(rc):
    series = []
    for tipo, body in (rc.get("channel_schedules") or {}).items():
        if not isinstance(body, dict):
            continue
        for num, entrada in body.items():
            if num == "special" and isinstance(entrada, dict):
                for snum, sent in entrada.items():
                    sent = sent if isinstance(sent, dict) else {}
                    series.append({
                        "id": f"{tipo}/special/{snum}", "tipo": tipo, "numero": str(snum),
                        "especial": True, "nombre": sent.get("name") or f"special {snum}",
                        "entrada": sent,
                    })
            elif isinstance(entrada, dict):
                series.append({
                    "id": f"{tipo}/{num}", "tipo": tipo, "numero": str(num),
                    "especial": False, "nombre": entrada.get("name") or str(num),
                    "entrada": entrada,
                })
    return series


def _referencias(rc):
    """(tipo, numero) → lista de (dispositivo, relay, config) del horario normal."""
    refs = {}
    for dev_name, dev in (rc.get("devices") or {}).items():
        if not isinstance(dev, dict):
            continue
        for relay, item in (dev.get("config_x_relay") or {}).items():
            if not isinstance(item, dict) or "schedule_type" not in item:
                continue
            clave = (item["schedule_type"], str(item.get("schedule_number", 1)))
            refs.setdefault(clave, []).append((dev_name, relay, item.get("config", "automatico")))
    return refs


def _config_dia_especial(rc, tipo, numero, grupos):
    """Config del día especial que aplica a (tipo, numero), o None."""
    for dev in (rc.get("devices") or {}).values():
        if not isinstance(dev, dict):
            continue
        for sd in _lista(dev.get("special_days")):
            if not set(sd.get("day_groups") or []) & set(grupos):
                continue
            for item in (sd.get("config_x_relay") or {}).values():
                if (isinstance(item, dict) and item.get("schedule_type") == tipo
                        and str(item.get("schedule_number", 1)) == numero):
                    return item
    return None


def _series_relevantes(series, servicio):
    """Series que se evalúan contra la solicitud según el servicio pedido."""
    servicio = (servicio or "").lower()
    claves = []
    if "iluminac" in servicio:
        claves += ["light", "ilum", "lumin"]
    if "clima" in servicio:
        claves += ["clima", "hvac", "termo", "aire"]
    if not claves:
        return [s["id"] for s in series]
    elegidas = [s["id"] for s in series if any(c in s["tipo"].lower() for c in claves)]
    return elegidas or [s["id"] for s in series]


def calcular_eventos(config_data, rango_ini, rango_fin):
    """Devuelve (series, eventos, dias_especiales) para los días [rango_ini, rango_fin]."""
    cfg = desenvolver_config(config_data)
    rc = cfg.get("lambda_functions", {}).get("GG_relay_control")
    if not rc:
        raise ValueError("La configuración no tiene 'GG_relay_control' (sin relés para graficar).")

    series = _catalogo_series(rc)
    refs = _referencias(rc)
    schedules = rc.get("channel_schedules") or {}
    global_sd = {g: {_norm_dia(d) for d in dias}
                 for g, dias in (rc.get("special_days") or {}).items() if isinstance(dias, list)}

    extensiones = []
    for ext in rc.get("extensions") or []:
        try:
            ini = _parse_ext(ext["time_range"]["start"])
            fin = _parse_ext(ext["time_range"]["end"])
        except (KeyError, ValueError, TypeError):
            continue
        if fin.second == 59:
            fin += timedelta(seconds=1)
        extensiones.append({
            "tipo": ext.get("schedule_type"),
            "numeros": {str(n) for n in ext.get("schedule_numbers") or []},
            "ini": ini, "fin": fin,
            "estado": "extension" if str(ext.get("desired_status", "on")).lower() == "on" else "ext_off",
            "nombre": str(ext.get("name", "")),
        })

    eventos, dias_especiales = [], {}
    d = rango_ini
    while d <= rango_fin:
        clave_dia = f"{d.day:02d}-{d.month:02d}"
        grupos = [g for g, dias in global_sd.items() if clave_dia in dias]
        if grupos:
            dias_especiales[d.isoformat()] = grupos
        weekday = d.isoweekday()
        dia_ini = datetime.combine(d, datetime.min.time())
        dia_fin = dia_ini + timedelta(days=1)
        por_serie = {}

        for s in series:
            if s["especial"]:
                continue
            segs = []
            cfg_esp = _config_dia_especial(rc, s["tipo"], s["numero"], grupos) if grupos else None
            if cfg_esp:
                modo = cfg_esp.get("config", "automatico")
                if modo == "manual_encendido":
                    segs = [(0, DIA_SEG, "special", "")]
                elif modo == "automatico":
                    ent_esp = (schedules.get(s["tipo"], {}).get("special") or {}).get(s["numero"])
                    sid_esp = f"{s['tipo']}/special/{s['numero']}"
                    por_serie.setdefault(sid_esp, []).extend(
                        (a, b, "special", "") for a, b in _encendidos_entrada(ent_esp, weekday))
                # manual_apagado: el día queda apagado
            else:
                modos = {m for _, _, m in refs.get((s["tipo"], s["numero"]), [])}
                if modos == {"manual_encendido"}:
                    segs = [(0, DIA_SEG, "regular", "")]
                elif modos != {"manual_apagado"}:
                    segs = [(a, b, "regular", "") for a, b in _encendidos_entrada(s["entrada"], weekday)]

            for ext in extensiones:
                if ext["tipo"] != s["tipo"] or s["numero"] not in ext["numeros"]:
                    continue
                a, b = max(ext["ini"], dia_ini), min(ext["fin"], dia_fin)
                if a >= b:
                    continue
                segs = _pintar(segs, int((a - dia_ini).total_seconds()),
                               int((b - dia_ini).total_seconds()), ext["estado"], ext["nombre"])
            if segs:
                por_serie.setdefault(s["id"], []).extend(segs)

        for sid, segs in por_serie.items():
            for a, b, k, x in _fusionar(segs):
                if b > a:
                    eventos.append({"sid": sid, "fecha": d.isoformat(), "s": a, "e": b,
                                    "tipo": k, "ext": x})
        d += timedelta(days=1)

    return series, refs, eventos, dias_especiales


# ─── HTML ──────────────────────────────────────────────────────────────────────
def build_calendar_html(config_data, sucursal_name, start_date, end_date, solicitud_data=None):
    hoy = date.today()
    rango_ini = _lunes(start_date) - timedelta(days=7)
    rango_fin = _lunes(end_date) + timedelta(days=13)
    # Incluir la semana actual si está razonablemente cerca del rango pedido
    if abs((hoy - start_date).days) <= 120:
        rango_ini = min(rango_ini, _lunes(hoy))
        rango_fin = max(rango_fin, _lunes(hoy) + timedelta(days=6))

    series, refs, eventos, dias_especiales = calcular_eventos(config_data, rango_ini, rango_fin)

    series_out = []
    for i, s in enumerate(series):
        dispositivos = [] if s["especial"] else [
            f"{dev} · {relay} ({modo})" for dev, relay, modo in refs.get((s["tipo"], s["numero"]), [])]
        series_out.append({
            "id": s["id"],
            "label": f"{s['tipo'].capitalize()} - {s['nombre']}" + (" · Special schedule" if s["especial"] else ""),
            "nombre": s["nombre"],
            "color": PALETA[i % len(PALETA)],
            "sin_comportamiento": not (s["entrada"].get("config") or []),
            "dispositivos": dispositivos,
        })

    solicitud = None
    if solicitud_data:
        def _dt(v):
            try:
                return datetime.strptime(v.strip(), "%d/%m/%Y %H:%M").strftime("%Y-%m-%dT%H:%M")
            except Exception:
                return None
        ini, fin = _dt(solicitud_data.get("fecha_inicio") or ""), _dt(solicitud_data.get("fecha_fin") or "")
        if ini and fin:
            solicitud = {"ini": ini, "fin": fin,
                         "series": _series_relevantes(series, solicitud_data.get("servicio"))}

    datos = {
        "titulo": f"Programación configurada — {sucursal_name}",
        "series": series_out,
        "eventos": eventos,
        "dias_especiales": dias_especiales,
        "rango_ini": rango_ini.isoformat(),
        "rango_fin": rango_fin.isoformat(),
        "semana_inicial": _lunes(start_date).isoformat(),
        "solicitud": solicitud,
    }
    html = _PLANTILLA.replace("__DATOS__", json.dumps(datos, ensure_ascii=False).replace("</", "<\\/"))
    return html, 1240, 1040


_PLANTILLA = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#fff;font-family:Inter,Arial,sans-serif;color:#1f2937;padding:14px 18px}
#captureRoot{background:#fff;padding-bottom:6px}
.titulo{font-size:13px;font-weight:600;color:#374151;margin-bottom:10px}
.series{display:flex;flex-wrap:wrap;gap:8px 20px;margin-bottom:10px}
.serie{display:flex;align-items:center;gap:8px;cursor:pointer;user-select:none;font-size:14px;color:#6b7280}
.chk{width:22px;height:22px;border-radius:6px;display:flex;align-items:center;justify-content:center;
     color:#fff;font-size:14px;font-weight:700;border:2px solid transparent}
.serie.off .chk{background:#fff !important}
.serie.off span.lbl{opacity:.55}
.badge{font-size:11px;background:#FEF3C7;color:#B45309;padding:3px 8px;border-radius:6px;font-weight:600}
.estilos{display:flex;flex-wrap:wrap;gap:6px 22px;font-size:12px;color:#6b7280;margin-bottom:14px}
.estilos span{display:flex;align-items:center;gap:6px}
.mu{width:22px;height:12px;border-radius:3px;background:#6b7280;display:inline-block}
.barra{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.nav{display:flex;background:#f3f4f6;border-radius:8px}
.nav button,.hoy{border:none;background:#f3f4f6;color:#6b7280;font-size:16px;padding:6px 14px;cursor:pointer;border-radius:8px}
.nav button:disabled,.hoy:disabled{opacity:.35;cursor:default}
.hoy{font-size:13px}
.rango{flex:1;text-align:center;font-size:22px;font-weight:700;color:#1f2937;margin-right:120px}
.resumen{font-size:12px;margin:-4px 0 10px 0;padding:6px 10px;border-radius:6px;display:none}
.grid{display:grid;grid-template-columns:52px repeat(7,1fr);border:1px solid #eef0f3;border-radius:6px}
.cab{height:40px;display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:15px;
     font-weight:500;border-left:1px solid #eef0f3;border-bottom:1px solid #eef0f3}
.cab small{font-size:9px;color:#B45309;font-weight:600}
.horas div{font-size:12px;color:#374151;text-align:right;padding-right:6px;transform:translateY(-1px)}
.col{position:relative;border-left:1px solid #eef0f3}
.col.hoyc{background:#ECFDF3}
.ev{position:absolute;border-radius:3px;overflow:hidden;color:#fff;font-size:11px;line-height:15px;
    padding:3px 5px;text-shadow:0 0 2px rgba(0,0,0,.25);border:1px solid rgba(0,0,0,.06)}
.ev.extension{background-image:repeating-linear-gradient(-45deg,rgba(255,255,255,.38) 0 6px,transparent 6px 14px)}
.ev.special{background-image:repeating-linear-gradient(45deg,rgba(0,0,0,.20) 0 3px,transparent 3px 8px)}
.ev.ext_off{background:rgba(255,255,255,.7) !important;border:2px dashed;color:#374151;text-shadow:none}
.ahora{position:absolute;left:0;right:0;height:0;border-top:1.5px solid #ef4444;z-index:5}
.req{position:absolute;right:0;width:6px;z-index:4}
.req.ok{background:#2E7D32}.req.bad{background:#C62828}
.esp{position:absolute;inset:0;background:repeating-linear-gradient(45deg,rgba(107,114,128,.06) 0 4px,transparent 4px 10px);pointer-events:none}
#btnCopy{position:fixed;top:10px;right:14px;z-index:20;background:#378ADD;color:#fff;border:none;border-radius:6px;
         padding:7px 12px;font-size:12px;cursor:pointer;box-shadow:0 2px 6px rgba(0,0,0,.2)}
#copyMsg{position:fixed;top:14px;right:130px;z-index:20;font-size:11px;color:#2E7D32;display:none;background:#fff;
         padding:3px 8px;border-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,.15)}
</style></head>
<body>
<div id="captureRoot">
  <div class="titulo" id="titulo"></div>
  <div class="series" id="series"></div>
  <div class="estilos">
    <span><i class="mu"></i>horario regular</span>
    <span><i class="mu" style="background-image:repeating-linear-gradient(45deg,rgba(0,0,0,.3) 0 2px,transparent 2px 5px)"></i>día especial</span>
    <span><i class="mu" style="background-image:repeating-linear-gradient(-45deg,rgba(255,255,255,.5) 0 3px,transparent 3px 7px)"></i>extensión</span>
    <span><i class="mu" style="background:#fff;border:2px dashed #6b7280"></i>extensión apagando</span>
    <span id="lgReq" style="display:none"><i class="mu" style="background:#2E7D32;width:6px"></i>cumple solicitud
      <i class="mu" style="background:#C62828;width:6px;margin-left:8px"></i>no cumple</span>
  </div>
  <div class="barra">
    <div class="nav"><button id="prev">&#8249;</button><button id="next">&#8250;</button></div>
    <button class="hoy" id="hoy">hoy</button>
    <div class="rango" id="rango"></div>
  </div>
  <div class="resumen" id="resumen"></div>
  <div class="grid" id="grid"></div>
</div>
<span id="copyMsg">✅ Copiado — pégalo con Ctrl+V</span>
<button id="btnCopy" onclick="copiar()">📋 Copiar</button>

<script>
const D = __DATOS__;
const HH = 32;
const DIAS = ['Lu','Ma','Mi','Ju','Vi','Sá','Do'];
const MESES = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];
const visibles = new Set(D.series.map(s => s.id));
const serie = Object.fromEntries(D.series.map(s => [s.id, s]));
const orden = Object.fromEntries(D.series.map((s, i) => [s.id, i]));
const porFecha = {};
D.eventos.forEach(e => (porFecha[e.fecha] = porFecha[e.fecha] || []).push(e));

const pISO = s => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
const iso = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
const sumar = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
const lunes = d => sumar(d, -((d.getDay() + 6) % 7));
const hhmm = s => s >= 86400 ? '00:00' : String(Math.floor(s / 3600)).padStart(2, '0') + ':' + String(Math.floor(s % 3600 / 60)).padStart(2, '0');
const esc = t => String(t).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

const R0 = pISO(D.rango_ini), R1 = lunes(pISO(D.rango_fin));
const HOY = new Date(); HOY.setHours(0, 0, 0, 0);
let semana = pISO(D.semana_inicial);

const req = D.solicitud ? {
  ini: new Date(D.solicitud.ini), fin: new Date(D.solicitud.fin), series: new Set(D.solicitud.series)
} : null;

document.getElementById('titulo').textContent = D.titulo;

function renderSeries() {
  const cont = document.getElementById('series');
  cont.innerHTML = '';
  D.series.forEach(s => {
    const on = visibles.has(s.id);
    const el = document.createElement('div');
    el.className = 'serie' + (on ? '' : ' off');
    el.title = s.dispositivos.length ? 'Usado por:\n' + s.dispositivos.join('\n') : '';
    el.innerHTML = `<span class="chk" style="background:${s.color};border-color:${s.color}">${on ? '✓' : ''}</span>`
      + `<span class="lbl">${esc(s.label)}</span>`
      + (s.sin_comportamiento ? '<span class="badge">sin comportamiento</span>' : '');
    el.onclick = () => { on ? visibles.delete(s.id) : visibles.add(s.id); render(); };
    cont.appendChild(el);
  });
}

// Tramos encendidos (union) de las series relevantes y visibles para un día
function encendidos(fecha) {
  return (porFecha[fecha] || [])
    .filter(e => e.tipo !== 'ext_off' && visibles.has(e.sid) && (!req || req.series.has(e.sid)))
    .map(e => [e.s, e.e]).sort((a, b) => a[0] - b[0]);
}

function cumplimientoDia(dia) {
  if (!req) return [];
  const d0 = new Date(dia), d1 = sumar(dia, 1);
  const a = Math.max(req.ini, d0), b = Math.min(req.fin, d1);
  if (a >= b) return [];
  const s0 = Math.round((a - d0) / 1000), s1 = Math.round((b - d0) / 1000);
  const out = []; let cur = s0;
  encendidos(iso(dia)).forEach(([x, y]) => {
    x = Math.max(x, s0); y = Math.min(y, s1);
    if (y <= cur) return;
    if (x > cur) out.push([cur, x, false]);
    out.push([Math.max(x, cur), y, true]); cur = y;
  });
  if (cur < s1) out.push([cur, s1, false]);
  return out;
}

function renderResumen() {
  const el = document.getElementById('resumen');
  if (!req) return;
  document.getElementById('lgReq').style.display = 'flex';
  let falta = 0; const dias = [];
  for (let d = new Date(req.ini.getFullYear(), req.ini.getMonth(), req.ini.getDate()); d < req.fin; d = sumar(d, 1)) {
    const f = cumplimientoDia(d).filter(t => !t[2]).reduce((acc, t) => acc + t[1] - t[0], 0);
    if (f > 0) { falta += f; dias.push(String(d.getDate()).padStart(2, '0') + '/' + String(d.getMonth() + 1).padStart(2, '0')); }
  }
  el.style.display = 'block';
  if (falta === 0) {
    el.style.background = '#E8F5E9'; el.style.color = '#1B5E20';
    el.textContent = '✅ La programación cubre todo el tramo solicitado.';
  } else {
    el.style.background = '#FDECEC'; el.style.color = '#B71C1C';
    el.textContent = `❌ Faltan ${(falta / 3600).toFixed(1).replace('.0', '')} h sin cubrir de la solicitud (días: ${dias.join(', ')}).`;
  }
}

function render() {
  renderSeries();
  const d6 = sumar(semana, 6);
  document.getElementById('rango').textContent = semana.getFullYear() === d6.getFullYear()
    ? (semana.getMonth() === d6.getMonth()
        ? `${semana.getDate()} – ${d6.getDate()} ${MESES[d6.getMonth()]} ${d6.getFullYear()}`
        : `${semana.getDate()} ${MESES[semana.getMonth()]} – ${d6.getDate()} ${MESES[d6.getMonth()]} ${d6.getFullYear()}`)
    : `${semana.getDate()} ${MESES[semana.getMonth()]} ${semana.getFullYear()} – ${d6.getDate()} ${MESES[d6.getMonth()]} ${d6.getFullYear()}`;
  document.getElementById('prev').disabled = semana <= R0;
  document.getElementById('next').disabled = semana >= R1;
  const semHoy = lunes(HOY);
  document.getElementById('hoy').disabled = semHoy < R0 || semHoy > R1 || +semHoy === +semana;

  const g = document.getElementById('grid');
  let h = '<div class="cab" style="border-left:none"></div>';
  for (let i = 0; i < 7; i++) {
    const d = sumar(semana, i), esp = D.dias_especiales[iso(d)];
    h += `<div class="cab">${DIAS[i]} ${d.getDate()}${esp ? `<small>★ ${esc(esp.join(', '))}</small>` : ''}</div>`;
  }
  h += '<div class="horas" style="position:relative;height:' + 24 * HH + 'px">';
  for (let k = 0; k < 24; k++) h += `<div style="position:absolute;top:${k * HH}px;right:0;line-height:16px">${String(k).padStart(2, '0')}:00</div>`;
  h += '</div>';

  for (let i = 0; i < 7; i++) {
    const d = sumar(semana, i), f = iso(d);
    const lineas = `background-image:repeating-linear-gradient(to bottom,transparent 0 ${HH - 1}px,#eef0f3 ${HH - 1}px ${HH}px)`;
    h += `<div class="col${+d === +HOY ? ' hoyc' : ''}" style="height:${24 * HH}px;${lineas}">`;
    if (D.dias_especiales[f]) h += '<div class="esp"></div>';
    const evs = (porFecha[f] || []).filter(e => visibles.has(e.sid));
    const carriles = [...new Set(evs.map(e => e.sid))].sort((a, b) => orden[a] - orden[b]);
    const ancho = 100 / Math.max(carriles.length, 1);
    evs.forEach(e => {
      const s = serie[e.sid], c = carriles.indexOf(e.sid);
      const top = e.s / 3600 * HH, alto = Math.max((e.e - e.s) / 3600 * HH - 3, 6);
      const horas = (e.s === 0 && e.e === 86400) ? '00:00' : `${hhmm(e.s)} - ${hhmm(e.e)}`;
      const estado = e.tipo === 'ext_off' ? 'Off' : 'On';
      const tip = `${s.label}\n${horas} · ${estado}` + (e.tipo === 'extension' || e.tipo === 'ext_off' ? `\nExtensión ${e.ext}` : '')
        + (e.tipo === 'special' ? '\nDía especial' : '');
      const color = e.tipo === 'ext_off' ? `border-color:${s.color}` : `background-color:${s.color}`;
      h += `<div class="ev ${e.tipo}" title="${esc(tip)}" style="top:${top + 1}px;height:${alto}px;`
        + `left:calc(${c * ancho}% + 3px);width:calc(${ancho}% - 6px);${color}">`
        + `<div>${horas}</div>` + (alto > 26 ? `<div>${esc(s.nombre)} · ${estado}</div>` : '') + '</div>';
    });
    cumplimientoDia(d).forEach(([a, b, ok]) => {
      h += `<div class="req ${ok ? 'ok' : 'bad'}" title="${ok ? 'Cumple' : 'No cumple'} solicitud ${hhmm(a)} - ${hhmm(b)}"`
        + ` style="top:${a / 3600 * HH}px;height:${(b - a) / 3600 * HH}px"></div>`;
    });
    if (+d === +HOY) {
      const n = new Date(), s = n.getHours() * 3600 + n.getMinutes() * 60;
      h += `<div class="ahora" style="top:${s / 3600 * HH}px"></div>`;
    }
    h += '</div>';
  }
  g.innerHTML = h;
}

document.getElementById('prev').onclick = () => { semana = sumar(semana, -7); render(); };
document.getElementById('next').onclick = () => { semana = sumar(semana, 7); render(); };
document.getElementById('hoy').onclick = () => { semana = lunes(HOY); render(); };
render();
renderResumen();
</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<script>
async function copiar() {
  const btn = document.getElementById('btnCopy'), msg = document.getElementById('copyMsg');
  btn.textContent = '⏳ Capturando...'; btn.disabled = true;
  try {
    const t = document.getElementById('captureRoot');
    const canvas = await html2canvas(t, {backgroundColor: '#ffffff', scale: 2, logging: false});
    canvas.toBlob(async blob => {
      try {
        await navigator.clipboard.write([new ClipboardItem({'image/png': blob})]);
        msg.textContent = '✅ Copiado — pégalo con Ctrl+V';
      } catch (e) {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob); a.download = 'programacion.png'; a.click();
        msg.textContent = '💾 Clipboard bloqueado — descargado como PNG';
      }
      msg.style.display = 'inline'; setTimeout(() => (msg.style.display = 'none'), 4000);
      btn.textContent = '📋 Copiar'; btn.disabled = false;
    }, 'image/png');
  } catch (e) {
    btn.textContent = '📋 Copiar'; btn.disabled = false;
    alert('Error al capturar: ' + e.message);
  }
}
</script>
</body></html>"""
