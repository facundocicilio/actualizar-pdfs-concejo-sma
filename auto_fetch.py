import os
import re
import io
import json
import time
import hashlib
import argparse
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

import requests
import fitz  # PyMuPDF
from bs4 import BeautifulSoup
from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.utils import HfHubHTTPError

HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
HF_REPO_ID = os.getenv("HF_REPO_ID", "").strip()
SOURCE_URL = os.getenv("SOURCE_URL", "").strip()

INDEX_PATH = "index/index.json"
DEBUG = os.getenv("DEBUG", "1").strip().lower() in {"1", "true", "yes", "y"}

UA = os.getenv(
    "HTTP_USER_AGENT",
    "Mozilla/5.0 (compatible; ObservatorioActasBot/2.0; +https://github.com/FacundoCicilio/Actualizar-PDFs-Concejo-SMA)"
)

PROCESSOR_VERSION = 26

session = requests.Session()
session.headers.update({"User-Agent": UA})

def log(msg): print(msg, flush=True)
def dbg(msg):
    if DEBUG: print(msg, flush=True)

def must_env(name):
    val = os.getenv(name, "").strip()
    if not val:
        raise SystemExit(f"Falta variable de entorno {name}.")
    return val

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

def normalize_space(s):
    s = (s or "").replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")

def normalize_text(s):
    if not s: return ""
    s = str(s).lower()
    s = _strip_accents(s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def safe_int(v):
    try:
        if v is None: return None
        return int(str(v).strip())
    except: return None

def canonicalize_url(url):
    parsed = urlparse((url or "").strip())
    parsed = parsed._replace(fragment="")
    return urlunparse(parsed)

def sha256_bytes(data):
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()

def make_doc_id(pdf_bytes, filename):
    h = hashlib.sha256()
    h.update(pdf_bytes)
    h.update((filename or "").encode("utf-8", errors="ignore"))
    return h.hexdigest()[:16]

def safe_filename_from_url(url, content_disposition=None):
    if content_disposition:
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition, re.I)
        if m:
            name = m.group(1).strip().strip('"').strip("'")
            name = os.path.basename(name)
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
            if not name.lower().endswith(".pdf"): name += ".pdf"
            return name
    path = urlparse(url).path
    name = os.path.basename(path) or "archivo.pdf"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    if not name.lower().endswith(".pdf"): name += ".pdf"
    return name

BOILERPLATE_PATTERNS = [
    r"Donar\s+Órganos\s+y\s+Sangre\s+es\s+Donar\s+Vida",
    r"Donar\s+Organos\s+y\s+Sangre\s+es\s+Donar\s+Vida",
    r"Inf[oó]rmese,?\s*sea\s+donante",
    r"\bsea\s+donante\b",
    r"(?:https?:\/\/)?(?:www\.)?incucai\.gov\.ar",
    r"CONCEJO\s+DELIBERANTE\s+SAN\s+MARTIN\s+DE\s+LOS\s+ANDES",
    r"CONCEJO\s+DELIBERANTE",
    r"P[áa]gina\s+\d+\s+de\s+\d+",
    r"\bP[áa]g\.\s*\d+\b",
]

def remove_boilerplate_lines(text):
    lines = (text or "").splitlines()
    cleaned = []
    for ln in lines:
        ln_strip = ln.strip()
        if not ln_strip:
            cleaned.append("")
            continue
        if any(re.search(p, ln_strip, re.IGNORECASE) for p in BOILERPLATE_PATTERNS):
            continue
        ln_strip = re.sub(r"^[\-\•\●\·\*]+\s*", "", ln_strip).strip()
        cleaned.append(ln_strip)
    return "\n".join(cleaned)

def remove_boilerplate_global(text):
    t = text or ""
    for p in BOILERPLATE_PATTERNS:
        t = re.sub(p, " ", t, flags=re.IGNORECASE)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n\s+\n", "\n\n", t)
    return t.strip()

def extract_text_from_pdf_bytes(pdf_bytes, max_pages=80):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = []
    n_pages = min(len(doc), max_pages)
    for i in range(n_pages):
        page = doc.load_page(i)
        words = page.get_text("words") or []
        if not words: continue
        words.sort(key=lambda w: (round(w[1], 1), round(w[0], 1)))
        line_bins = {}
        for w in words:
            bin_y = int(round(w[1] / 3.0))
            line_bins.setdefault(bin_y, []).append(w)
        for bin_y in sorted(line_bins.keys()):
            line_words = sorted(line_bins[bin_y], key=lambda w: w[0])
            txt = " ".join([w[4] for w in line_words]).strip()
            if txt: out.append(txt)
        out.append("")
    doc.close()
    return "\n".join(out)

def extract_cover_lines(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if len(doc) == 0: return []
    page = doc.load_page(0)
    words = page.get_text("words") or []
    doc.close()
    if not words: return []
    words.sort(key=lambda w: (round(w[1], 1), round(w[0], 1)))
    line_bins = {}
    for w in words:
        bin_y = int(round(w[1] / 3.0))
        line_bins.setdefault(bin_y, []).append(w)
    lines = []
    for bin_y in sorted(line_bins.keys()):
        line_words = sorted(line_bins[bin_y], key=lambda w: w[0])
        txt = " ".join([w[4] for w in line_words]).strip()
        if txt: lines.append(txt)
    return lines

MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

def parse_spanish_date_any(s):
    s = (s or "").strip()
    m = re.search(r"\b(\d{1,2})\s+de\s+([A-Za-zÁÉÍÓÚáéíóúñÑ]+)\s+de\s+(20\d{2})\b", s, re.IGNORECASE)
    if not m: return None
    d, mn, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    mo = MONTHS.get(mn)
    if not mo: return None
    try: return datetime(y, mo, d)
    except: return None

def parse_date_any(s):
    if not s: return datetime.min
    s = str(s).strip()
    m = re.match(r"(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100: y += 2000
        try: return datetime(y, mo, d)
        except: return datetime.min
    dt = parse_spanish_date_any(s)
    if dt: return dt
    return datetime.min

def infer_num_year_from_filename(filename):
    if not filename: return (None, None)
    f = filename.lower()
    m = re.search(r"\bno[-_\s]*0*(\d{1,3})[-_](\d{2})\b", f)
    if m: return (int(m.group(1)), 2000 + int(m.group(2)))
    m = re.search(r"\bno[-_\s]*0*(\d{1,3})[-_](20\d{2})\b", f)
    if m: return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"\b[-_](\d{1,3})[-_](20\d{2})\b", f)
    if m: return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"\b(\d{1,3})[-_](\d{2})\b", f)
    if m: return (int(m.group(1)), 2000 + int(m.group(2)))
    return (None, None)

def normalize_session_number(s):
    if not s: return None
    m = re.search(r"\b(\d{1,3})\b", str(s))
    return int(m.group(1)) if m else None

def extract_box_header_info(pdf_bytes):
    lines = extract_cover_lines(pdf_bytes)
    if not lines: return {}
    page_text = "\n".join(lines)
    stype = None
    if re.search(r"\bORDEN\s+DEL\s+D[IÍ]A\b", page_text, re.IGNORECASE): stype = "ORDEN DEL DIA"
    elif re.search(r"\bACTA\b", page_text, re.IGNORECASE): stype = "ACTA"
    mnum = re.search(r"\bSESIO[NÓO]\s+(?:ORDINARIA|EXTRAORDINARIA|ESPECIAL)\s+N(?:ro|°|º|o)?[:\.]?\s*0*(\d{1,3})\b", page_text, re.IGNORECASE)
    sn = str(int(mnum.group(1))) if mnum else None
    my = re.search(r"\bA[ñn]o\s+(20\d{2})\b", page_text, re.IGNORECASE)
    sy = int(my.group(1)) if my else None
    dt = parse_spanish_date_any(page_text)
    date_str = None
    if dt:
        date_str = dt.strftime("%d/%m/%Y")
        if sy is None: sy = dt.year
    out = {}
    if stype: out["session_type"] = stype
    if sn: out["session_number"] = sn
    if sy: out["session_year"] = sy
    if date_str: out["date"] = date_str
    return out

def parse_metadata(text, filename, box):
    t = text or ""
    head = "\n".join(t.splitlines()[:200])
    session_type = box.get("session_type")
    session_number = box.get("session_number")
    session_year = box.get("session_year")
    date_str = box.get("date")
    f_num, f_year = infer_num_year_from_filename(filename or "")
    if session_number is None and f_num is not None: session_number = str(f_num)
    if session_year is None and f_year is not None: session_year = f_year
    if session_type is None:
        if re.search(r"orden\s+del\s+d[ií]a", head, re.IGNORECASE): session_type = "ORDEN DEL DIA"
        elif re.search(r"\bacta\b", head, re.IGNORECASE): session_type = "ACTA"
    if date_str is None:
        m = re.search(r"San\s+Mart[ií]n\s+de\s+los\s+Andes\s*,?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})", head, re.IGNORECASE)
        if m: date_str = m.group(1)
    if date_str is None:
        m = re.search(r"(\d{1,2}\s+de\s+[A-Za-zÁÉÍÓÚáéíóúñÑ]+\s+de\s+20\d{2})", head, re.IGNORECASE)
        if m:
            dt = parse_date_any(m.group(1))
            if dt != datetime.min: date_str = dt.strftime("%d/%m/%Y")
    if session_year is None:
        dt = parse_date_any(date_str)
        if dt != datetime.min: session_year = dt.year
    sn_int = normalize_session_number(session_number)
    session_number = str(sn_int) if sn_int is not None else None
    return {"session_type": session_type, "session_number": session_number, "session_year": session_year, "date": date_str}

TOPIC_RULES = [
    ("Agua/Servicios", ["agua", "cloaca", "gas", "energía", "energia", "luz", "residuos", "basura", "saneamiento"]),
    ("Tarifas", ["tarifa", "tasas", "impuesto", "tribut", "arancel", "reajuste", "actualización", "actualizacion"]),
    ("Obra pública", ["obra", "pavimento", "asfalto", "vereda", "plaza", "puente", "licitación", "licitacion", "contratación", "contratacion"]),
    ("Seguridad", ["seguridad", "tránsito", "transito", "control", "inspección", "inspeccion", "alcoholemia", "emergencia"]),
    ("Comercio", ["comercio", "habilitación", "habilitacion", "local", "feria", "gastronomía", "gastronomia", "turismo"]),
    ("Ambiente", ["ambiente", "bosque", "incendio", "impacto", "contaminación", "contaminacion", "reserva"]),
    ("Vivienda/Urbanismo", ["vivienda", "urban", "lote", "zonificación", "zonificacion", "catastro", "edificación", "edificacion", "loteo"]),
    ("Educación/Cultura", ["educación", "educacion", "escuela", "cultura", "biblioteca", "museo", "deporte"]),
]

def classify_topics(text):
    t = (text or "").lower()
    return [label for label, keys in TOPIC_RULES if any(k in t for k in keys)]

IMPACT_RULES = [
    ("💰 Bolsillo", ["tarifa", "tasa", "impuesto", "tribut", "arancel", "reajuste", "actualización", "actualizacion", "módulo", "modulo", "valor"]),
    ("🏗 Infraestructura", ["obra", "licitación", "licitacion", "contratación", "contratacion", "pavimento", "asfalto", "vereda", "plaza", "puente"]),
    ("🚦 Tránsito/Seguridad", ["tránsito", "transito", "circulación", "circulacion", "corte", "estacionamiento", "control", "inspección", "inspeccion", "alcoholemia", "seguridad"]),
    ("🚰 Servicios", ["agua", "cloaca", "gas", "energía", "energia", "luz", "residuos", "basura", "saneamiento"]),
    ("🏪 Comercio", ["habilitación", "habilitacion", "comercio", "local", "feria", "gastronomía", "gastronomia", "turismo"]),
    ("🏘 Urbanismo", ["lote", "loteo", "urban", "zonificación", "zonificacion", "catastro", "edificación", "edificacion", "vivienda"]),
    ("🌿 Ambiente", ["ambiente", "bosque", "incendio", "impacto", "contaminación", "contaminacion", "reserva"]),
]

def infer_impact(text):
    t = (text or "").lower()
    hits = [label for label, keys in IMPACT_RULES if any(k in t for k in keys)]
    if not hits:
        return {"impact_tags": [], "impact_text": "🧩 Informativo: no se ve un impacto directo claro (puede ser interno/administrativo)."}
    msg_map = {
        "💰 Bolsillo": "Puede impactar en montos a pagar (tasas/tarifas/condiciones).",
        "🏗 Infraestructura": "Puede implicar obras, mejoras o cortes asociados.",
        "🚦 Tránsito/Seguridad": "Puede cambiar circulación, controles o normas.",
        "🚰 Servicios": "Puede afectar servicios básicos (agua/cloaca/energía/residuos).",
        "🏪 Comercio": "Puede afectar habilitaciones, comercios o turismo.",
        "🏘 Urbanismo": "Puede afectar loteos, zonificación o desarrollo urbano.",
        "🌿 Ambiente": "Puede afectar regulaciones ambientales o áreas naturales.",
    }
    top = hits[:2]
    text_out = " ".join([f"{h}: {msg_map.get(h, '')}".strip() for h in top]).strip()
    return {"impact_tags": hits[:4], "impact_text": text_out}

BASURA_PATTERNS = [
    re.compile(r"^\d{5,6}[-/]\d{1,6}/\d{4}\s+\d{2}/\d{2}/\d{4}\s+Miembro\s+Informante:", re.IGNORECASE),
    re.compile(r"^EE-\d{4}-\d+\S*\s+\d{2}/\d{2}/\d{4}\s+Miembro\s+Informante:", re.IGNORECASE),
    re.compile(r"^Miembro\s+Informante:", re.IGNORECASE),
]

def is_basura(s):
    s = (s or "").strip()
    for p in BASURA_PATTERNS:
        if p.search(s):
            return True
    return False

def clean_que_dice(s):
    """Limpia redundancias del texto antes de mostrarlo."""
    s = normalize_space(s)
    # Quitar "Dictamen s/" redundante
    s = re.sub(r"^Dictamen\s+s/\s*", "", s, flags=re.IGNORECASE).strip()
    # Quitar "Tema: Dictamen s/" 
    s = re.sub(r"^Tema:\s*Dictamen\s+s/\s*", "", s, flags=re.IGNORECASE).strip()
    # Quitar "Tema:" al inicio
    s = re.sub(r"^Tema:\s*", "", s, flags=re.IGNORECASE).strip()
    # Quitar número de decreto/expediente al inicio si es lo primero
    s = re.sub(r"^\d+/\d{4}\s*[-—–]\s*", "", s).strip()
    # Quitar ".- De Hacienda", ".- De Gobierno" al final
    s = re.sub(r"\s*\.-\s*De\s+\w+\s*$", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"\s*\.-\s*$", "", s).strip()
    return s

def build_que_dice(raw):
    s = normalize_space(raw)
    if not s: return ""
    if is_basura(s): return ""

    # Limpiar texto
    s = clean_que_dice(s)
    if not s: return ""

    action = ""
    t = s.lower()
    if "declaración de interés" in t or "declaracion de interes" in t:
        action = "Declaración de Interés"
    elif "dictamen" in t:
        action = "Dictamen"
        s = re.sub(r"^Dictamen\s+s/\s*", "", s, flags=re.IGNORECASE).strip()
    elif "decreto" in t:
        action = "Decreto"
    elif "nota" in t:
        action = "Nota"
    elif "habilitación" in t or "habilitacion" in t or "licencia comercial" in t:
        action = "Habilitación"
    elif "convenio" in t:
        action = "Convenio"
    elif "obra" in t or "licitación" in t or "licitacion" in t:
        action = "Obra pública"
    elif "regularización" in t or "regularizacion" in t:
        action = "Regularización"
    elif "autorización" in t or "autorizacion" in t:
        action = "Autorización"

    main = s if len(s) <= 260 else s[:260].rsplit(" ", 1)[0] + "…"
    if action:
        return f"{action}: {main}".strip()
    return main.strip()

# Patrón SMA: "1. 05001-82/2024   02/03/2026   Miembro Informante: Concejal Vita"
# siguiente línea: "181/2026 --- Tema: Dictamen s/ ..."
MI_PAT = re.compile(
    r"^(\d{1,2})\.\s+((?:\d{5,6}[-/]\d{1,6}/\d{4}|EE-\d{4}-\d+\S*))\s+\d{2}/\d{2}/\d{4}\s+Miembro\s+Informante:",
    re.IGNORECASE
)
TEMA_LINE_PAT = re.compile(r"^\d+/\d{4}\s*---\s*Tema:\s*(.+)$", re.IGNORECASE)

def extract_items(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    items = []

    exp_tema_pat = re.compile(r"^(\d{1,6}(?:[-]\d{1,6})?/\d{4})\s*[—–-]\s*Tema:\s*(.*)$", re.IGNORECASE)
    exp_pat = re.compile(r"^(\d{1,6}(?:[-]\d{1,6})?/\d{4})\s*[—–-]\s*(.*)$", re.IGNORECASE)
    num_pat = re.compile(r"^(\d{1,2})[\.\)]\s+(.*)$")

    def is_new_item(line):
        if MI_PAT.match(line): return None
        for typ, p in [("exp_tema", exp_tema_pat), ("exp", exp_pat), ("num", num_pat)]:
            m = p.match(line)
            if m: return (typ, m)
        return None

    i = 0
    while i < len(lines):
        mi_match = MI_PAT.match(lines[i])
        if mi_match:
            exp_ref = mi_match.group(2).strip()
            tema_text = ""
            j = i + 1
            while j < len(lines) and j < i + 5:
                tm = TEMA_LINE_PAT.match(lines[j])
                if tm:
                    tema_text = tm.group(1).strip()
                    j += 1
                    while j < len(lines):
                        if MI_PAT.match(lines[j]): break
                        if TEMA_LINE_PAT.match(lines[j]): break
                        if is_new_item(lines[j]): break
                        tema_text += " " + lines[j]
                        j += 1
                    break
                j += 1
            if tema_text:
                full = normalize_space(tema_text)
                full = remove_boilerplate_global(full)
                if full:
                    qd = build_que_dice(full)
                    impact = infer_impact(full)
                    items.append({"key": exp_ref, "title": full[:2000], "que_dice": qd,
                                  "impact_text": impact["impact_text"], "impact_tags": impact["impact_tags"],
                                  "decreto": None, "expediente": exp_ref, "topics": classify_topics(full)})
            i = j
            continue

        hit = is_new_item(lines[i])
        if not hit:
            i += 1
            continue

        typ, m = hit
        key = (m.group(1) or "").strip()
        first = (m.group(2) or "").strip()

        # Si el primer texto es basura (encabezado con Miembro Informante), saltear
        if is_basura(first):
            i += 1
            continue

        parts = [first] if first else []
        j = i + 1
        while j < len(lines):
            if MI_PAT.match(lines[j]): break
            if is_new_item(lines[j]): break
            parts.append(lines[j])
            j += 1

        full = normalize_space(" ".join(parts))
        full = remove_boilerplate_global(full)
        if not full:
            i = j
            continue

        # Filtrar si el texto completo es basura
        if is_basura(full):
            i = j
            continue

        dec_inside = None
        mdec = re.search(r"(?:Decreto)\s*(?:N[°º]\s*)?([A-Z0-9\-\./]+)", full, re.IGNORECASE)
        if mdec: dec_inside = mdec.group(1)
        exp_inside = None
        mexp = re.search(r"(?:Expediente)\s*(?:N[°º]\s*)?([A-Z0-9\-\./]+)", full, re.IGNORECASE)
        if mexp: exp_inside = mexp.group(1)
        qd = build_que_dice(full)
        impact = infer_impact(full)
        items.append({"key": key if key else None, "title": full[:2000], "que_dice": qd,
                       "impact_text": impact["impact_text"], "impact_tags": impact["impact_tags"],
                       "decreto": dec_inside, "expediente": exp_inside or (key if typ in ("exp", "exp_tema") else None),
                       "topics": classify_topics(full)})
        i = j

    for idx, it in enumerate(items, start=1):
        if not it.get("key"): it["key"] = str(idx)
    return items[:800]

def build_summary_citizen(meta, topics, items):
    parts = []
    head = []
    if meta.get("date"): head.append(f"**Fecha:** {meta['date']}")
    if meta.get("session_type") or meta.get("session_number") or meta.get("session_year"):
        head.append(f"**Documento:** {(meta.get('session_type') or 'ORDEN DEL DIA')} N° {(meta.get('session_number') or 's/n')} (Año {(meta.get('session_year') or 's/año')})")
    if head: parts.append(" · ".join(head))
    if topics: parts.append(f"**Temas:** {', '.join(topics)}")
    parts.append("")
    if items:
        parts.append(f"**Ítems detectados:** {len(items)}")
        parts.append("")
        parts.append("**Puntos (claro y directo):**")
        for it in items[:10]:
            qd = (it.get("que_dice") or "").strip()
            imp = (it.get("impact_text") or "").strip()
            if qd:
                parts.append(f"- **{it.get('key')}**: {qd}")
                if imp: parts.append(f"  - _Impacto_: {imp}")
    else:
        parts.append("No se detectaron ítems claros (PDF escaneado o texto muy roto).")
    return "\n".join(parts)

def process_pdf(pdf_bytes, filename):
    box = extract_box_header_info(pdf_bytes)
    raw_text = extract_text_from_pdf_bytes(pdf_bytes)
    raw_text = remove_boilerplate_lines(raw_text)
    raw_text = remove_boilerplate_global(raw_text)
    text = normalize_space(raw_text)
    meta = parse_metadata(text, filename=filename, box=box)
    topics = classify_topics(text)
    items = extract_items(text)
    return {
        "processor_version": PROCESSOR_VERSION,
        "source": {"filename": filename, "processed_at": datetime.utcnow().isoformat() + "Z"},
        "box_meta": box, **meta, "topics": topics, "items": items,
        "entities": {"persons": [], "orgs": []},
        "summary_citizen": build_summary_citizen(meta, topics, items),
        "text_preview": text[:12000],
    }

def api():
    if not HF_TOKEN: raise RuntimeError("Falta HF_TOKEN.")
    return HfApi(token=HF_TOKEN)

def download_text_file(repo_id, path):
    try:
        local = hf_hub_download(repo_id=repo_id, repo_type="dataset", filename=path, token=HF_TOKEN)
        with open(local, "r", encoding="utf-8") as f: return f.read()
    except HfHubHTTPError: return None
    except: return None

def load_processed_json(repo_id, doc_id):
    raw = download_text_file(repo_id, f"processed/{doc_id}.json")
    if not raw: return None
    try: return json.loads(raw)
    except: return None

def rebuild_index_from_processed(repo_id):
    log("🛠 Reconstruyendo index desde processed/*.json ...")
    out = []
    files = api().list_repo_files(repo_id=repo_id, repo_type="dataset")
    processed_files = [p for p in files if p.startswith("processed/") and p.endswith(".json")]
    dbg(f"Processed encontrados: {len(processed_files)}")
    for p in processed_files:
        m = re.match(r"processed/([a-f0-9]{16})\.json$", p)
        if not m: continue
        doc_id = m.group(1)
        payload = load_processed_json(repo_id, doc_id) or {}
        original_filename = payload.get("source", {}).get("filename") or f"{doc_id}.pdf"
        record = {
            "doc_id": doc_id, "original_filename": original_filename,
            "pdf_path": f"raw/{doc_id}.pdf", "json_path": p,
            "created_at": payload.get("source", {}).get("processed_at") or utc_now_iso(),
            "session_type": payload.get("session_type"), "session_number": payload.get("session_number"),
            "session_year": payload.get("session_year"), "date": payload.get("date"),
            "topics": payload.get("topics", []), "source_url": payload.get("source_url"),
            "sha256": payload.get("sha256"),
        }
        out.append(record)
    out.sort(key=lambda r: (safe_int(r.get("session_year")) or 0, normalize_session_number(r.get("session_number")) or 0, r.get("created_at") or ""), reverse=True)
    log(f"✅ Index reconstruido con {len(out)} documentos.")
    return out

def load_index(repo_id):
    raw = download_text_file(repo_id, INDEX_PATH)
    if not raw: return []
    try: data = json.loads(raw)
    except: return rebuild_index_from_processed(repo_id)
    if isinstance(data, list): return data
    if isinstance(data, dict): return rebuild_index_from_processed(repo_id)
    return []

def save_index(repo_id, index, message="Update index"):
    content = json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8")
    api().upload_file(path_or_fileobj=io.BytesIO(content), path_in_repo=INDEX_PATH,
                      repo_id=repo_id, repo_type="dataset", commit_message=message)

def upload_pdf_and_json(repo_id, doc_id, pdf_bytes, processed_json, original_filename, source_url, sha256_digest):
    hf = api()
    pdf_path = f"raw/{doc_id}.pdf"
    json_path = f"processed/{doc_id}.json"
    hf.upload_file(path_or_fileobj=io.BytesIO(pdf_bytes), path_in_repo=pdf_path,
                   repo_id=repo_id, repo_type="dataset", commit_message=f"Add PDF {doc_id}")
    processed_json = dict(processed_json or {})
    processed_json["source_url"] = source_url
    processed_json["sha256"] = sha256_digest
    json_bytes = json.dumps(processed_json, ensure_ascii=False, indent=2).encode("utf-8")
    hf.upload_file(path_or_fileobj=io.BytesIO(json_bytes), path_in_repo=json_path,
                   repo_id=repo_id, repo_type="dataset", commit_message=f"Add processed {doc_id}")
    index = load_index(repo_id)
    record = {
        "doc_id": doc_id, "original_filename": original_filename,
        "pdf_path": pdf_path, "json_path": json_path,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "session_type": processed_json.get("session_type"), "session_number": processed_json.get("session_number"),
        "session_year": processed_json.get("session_year"), "date": processed_json.get("date"),
        "topics": processed_json.get("topics", []), "source_url": source_url, "sha256": sha256_digest,
    }
    index = [r for r in index if r.get("doc_id") != doc_id]
    index.insert(0, record)
    save_index(repo_id, index, message=f"Index {doc_id}")
    return {"pdf_path": pdf_path, "json_path": json_path}

def is_probable_document_link(text, href):
    href_l = (href or "").lower()
    text_l = (text or "").lower()
    if ".pdf" in href_l: return True
    href_keywords = ["upload", "uploads", "download", "descarga", "archivo", "file", "media", "wp-content", "document", "docs"]
    text_keywords = ["orden", "día", "dia", "sesión", "sesion", "acta", "extraordinaria", "ordinaria", "documento", "descargar", "descarga", "pdf"]
    if any(k in href_l for k in href_keywords) and any(k in text_l for k in text_keywords): return True
    return False

def fetch_pdf_links(page_url):
    log(f"🌐 Leyendo página: {page_url}")
    r = session.get(page_url, timeout=30)
    log(f"📡 Status code página: {r.status_code}")
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    all_links = soup.find_all("a", href=True)
    log(f"🔎 Cantidad total de enlaces <a>: {len(all_links)}")
    candidates = []
    for i, a in enumerate(all_links, start=1):
        raw_href = (a.get("href") or "").strip()
        text = a.get_text(" ", strip=True)
        full = canonicalize_url(urljoin(page_url, raw_href))
        dbg(f"[{i}] Texto: {text} | Href: {raw_href}")
        if not raw_href: continue
        if raw_href.lower().startswith(("javascript:", "mailto:", "tel:")): continue
        if is_probable_document_link(text, raw_href):
            dbg(f"[{i}] ✅ Candidato a documento")
            candidates.append(full)
    seen = set()
    out = []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            out.append(u)
    log(f"📄 Documentos candidatos únicos encontrados: {len(out)}")
    for u in out: log(f"   - {u}")
    return out

def download_pdf(url):
    r = session.get(url, timeout=60, allow_redirects=True)
    r.raise_for_status()
    final_url = canonicalize_url(r.url)
    content_type = (r.headers.get("Content-Type") or "").lower()
    content_disposition = r.headers.get("Content-Disposition")
    dbg(f"📨 Final URL: {final_url} | Content-Type: {content_type}")
    looks_like_pdf = ".pdf" in final_url.lower() or "application/pdf" in content_type or b"%PDF" in r.content[:1024]
    if not looks_like_pdf:
        raise ValueError(f"No parece PDF. URL final: {final_url} | Content-Type: {content_type}")
    return r.content, final_url, content_disposition

def main():
    must_env("HF_TOKEN")
    must_env("HF_REPO_ID")

    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--source-url", type=str, default=None)
    parser.add_argument("--sleep", type=float, default=0.0)
    args = parser.parse_args()

    year = args.year or datetime.now().year
    page_url = (args.source_url or SOURCE_URL or f"https://prensacd.cdsma.gob.ar/ordenes-del-dia-{year}/").strip()

    log("🚀 Auto fetch iniciado")
    log(f"📦 Repo: {HF_REPO_ID}")
    log(f"📰 Fuente: {page_url}")
    log(f"🗓️ Año: {year}")
    log(f"🐞 DEBUG: {DEBUG}")

    index = load_index(HF_REPO_ID)
    known_source_urls = {canonicalize_url((r.get("source_url") or "").strip()) for r in index if (r.get("source_url") or "").strip()}
    known_doc_ids = {r.get("doc_id") for r in index if r.get("doc_id")}

    log(f"📚 Documentos ya indexados: {len(index)}")
    for r in index[:20]:
        dbg(f"   - {r.get('doc_id')} | {r.get('original_filename')} | {r.get('source_url')}")

    pdf_links = fetch_pdf_links(page_url)
    new_links = [u for u in pdf_links if canonicalize_url(u) not in known_source_urls]
    log(f"🆕 Nuevos detectados por URL: {len(new_links)}")
    for u in new_links: log(f"   - {u}")

    uploaded_count = 0
    reprocessed_count = 0
    skipped_count = 0
    errors = []

    # Procesar todos los links: nuevos se suben, existentes se reprocesан si versión vieja
    for url in pdf_links:
        try:
            canon_url = canonicalize_url(url)
            is_new = canon_url not in known_source_urls

            log(f"⬇️ Descargando: {url}")
            data, final_url, content_disposition = download_pdf(url)
            digest = sha256_bytes(data)
            fname = safe_filename_from_url(final_url, content_disposition)
            doc_id = make_doc_id(data, fname)

            log(f"✅ Descargado OK. Bytes: {len(data)} | doc_id: {doc_id}")

            # Si ya existe, verificar si necesita reprocesado
            if not is_new and doc_id in known_doc_ids:
                existing_json = load_processed_json(HF_REPO_ID, doc_id)
                if existing_json and existing_json.get("processor_version", 0) < PROCESSOR_VERSION:
                    log(f"♻️ Reprocesando versión vieja ({existing_json.get('processor_version')}) → {PROCESSOR_VERSION}")
                    processed = process_pdf(data, fname)
                    upload_pdf_and_json(repo_id=HF_REPO_ID, doc_id=doc_id, pdf_bytes=data,
                                        processed_json=processed, original_filename=fname,
                                        source_url=final_url, sha256_digest=digest)
                    reprocessed_count += 1
                    log("✅ Reprocesado y actualizado.")
                else:
                    log(f"⚠️ Saltado: versión actual, doc_id {doc_id}")
                    skipped_count += 1
                continue

            if not is_new:
                log(f"⚠️ Saltado: source_url ya existente {final_url}")
                skipped_count += 1
                continue

            with_index_same_url = any(canonicalize_url((r.get("source_url") or "").strip()) == final_url for r in index)
            if with_index_same_url:
                log(f"⚠️ Saltado: source_url ya existente {final_url}")
                skipped_count += 1
                continue

            log("🧠 Procesando PDF...")
            processed = process_pdf(data, fname)

            log("📦 Subiendo raw + processed + index...")
            upload_pdf_and_json(repo_id=HF_REPO_ID, doc_id=doc_id, pdf_bytes=data,
                                processed_json=processed, original_filename=fname,
                                source_url=final_url, sha256_digest=digest)

            index.insert(0, {
                "doc_id": doc_id, "original_filename": fname,
                "pdf_path": f"raw/{doc_id}.pdf", "json_path": f"processed/{doc_id}.json",
                "created_at": datetime.utcnow().isoformat() + "Z",
                "session_type": processed.get("session_type"), "session_number": processed.get("session_number"),
                "session_year": processed.get("session_year"), "date": processed.get("date"),
                "topics": processed.get("topics", []), "source_url": final_url, "sha256": digest,
            })
            known_doc_ids.add(doc_id)
            known_source_urls.add(final_url)
            uploaded_count += 1
            log("✅ Documento subido y indexado correctamente.")

            if args.sleep > 0: time.sleep(args.sleep)

        except Exception as e:
            err = f"❌ Error con {url}: {e}"
            log(err)
            errors.append(err)

    log(f"🏁 Fin. Nuevos: {uploaded_count} | Reprocesados: {reprocessed_count} | Saltados: {skipped_count} | Errores: {len(errors)}")
    if errors:
        log("⚠️ Errores:")
        for e in errors: log(f"   - {e}")

if __name__ == "__main__":
    main()
