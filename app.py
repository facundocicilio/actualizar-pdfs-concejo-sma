# app.py — Observatorio de Actas (SMA)
# Incluye:
# - Histórico + detalle de ítems
# - Buscador global con botón (mejor para celular) + búsqueda más precisa (sin “contaminar” por people)
# - Suscripción email + métricas simples
# - Upload admin de actas al dataset
# - Ordenanzas/Expedientes (manual): subir PDF o pegar link a PDF, extraer texto si se puede y generar resumen; si es escaneado, permite resumen manual
# - Enriquecimiento: si un ítem trae solo un número (ej 05000-590/2012), se puede mostrar/usar un “glosario” manual guardado por la comunidad

import os
import re
import io
import json
import time
import hashlib
import unicodedata
import urllib.request
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple

import streamlit as st
import pandas as pd
import fitz  # PyMuPDF
from huggingface_hub import hf_hub_download, HfApi

from hf_dataset_store import (
    DATASET_REPO_ID,
    make_doc_id,
    load_processed_json,
    upload_pdf_and_json,
)

try:
    from hf_dataset_store import load_index  # type: ignore
except Exception:
    load_index = None  # type: ignore


# ============================================================
# VERSIONADO
# ============================================================
PROCESSOR_VERSION = 24


# ============================================================
# APP CONFIG / BRANDING
# ============================================================
APP_TITLE = "Observatorio de Actas"
APP_SUBTITLE = "Concejo Deliberante de San Martín de los Andes"
APP_TAGLINE = "Resúmenes claros y buscables de la actividad legislativa"

st.set_page_config(page_title=f"{APP_TITLE} — SMA", layout="wide")
st.title(f"🏛️ {APP_TITLE}")
st.caption(f"**{APP_SUBTITLE}** · {APP_TAGLINE}")


# ============================================================
# HF Helpers (para métricas / suscriptores / glosario manual)
# ============================================================
HF_TOKEN = (st.secrets.get("HF_TOKEN", None) or os.getenv("HF_TOKEN", "")).strip()
hf_api = HfApi(token=HF_TOKEN) if HF_TOKEN else HfApi()

SUBSCRIBERS_PATH = "subscribers/subscribers.jsonl"
VISITS_PATH = "analytics/visits.jsonl"
MANUAL_REFS_PATH = "refs/manual_refs.jsonl"


def _hf_download_text(repo_id: str, path_in_repo: str) -> Optional[str]:
    try:
        local_path = hf_hub_download(repo_id=repo_id, repo_type="dataset", filename=path_in_repo)
        with open(local_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def _hf_upload_text(repo_id: str, path_in_repo: str, text: str, commit_message: str) -> None:
    bio = io.BytesIO(text.encode("utf-8"))
    hf_api.upload_file(
        repo_id=repo_id,
        repo_type="dataset",
        path_in_repo=path_in_repo,
        path_or_fileobj=bio,
        commit_message=commit_message,
    )


def _read_jsonl(repo_id: str, path_in_repo: str) -> List[Dict[str, Any]]:
    txt = _hf_download_text(repo_id, path_in_repo)
    if not txt:
        return []
    out: List[Dict[str, Any]] = []
    for ln in txt.splitlines():
        ln = (ln or "").strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def _append_jsonl(repo_id: str, path_in_repo: str, row: Dict[str, Any], commit_message: str) -> None:
    rows = _read_jsonl(repo_id, path_in_repo)
    rows.append(row)
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    _hf_upload_text(repo_id, path_in_repo, text, commit_message=commit_message)


# ============================================================
# Admin gate
# ============================================================
def admin_gate() -> bool:
    admin_pass = st.secrets.get("ADMIN_PASS", "")
    if not admin_pass:
        return True
    with st.sidebar:
        st.markdown("### 🔒 Admin")
        entered = st.text_input("Admin password", type="password")
    return entered == admin_pass


# ============================================================
# Helpers
# ============================================================
def normalize_space(s: str) -> str:
    s = (s or "").replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def find_first(patterns: List[str], text: str, flags=re.IGNORECASE) -> Optional[str]:
    for p in patterns:
        m = re.search(p, text, flags)
        if m:
            return m.group(1).strip()
    return None


def safe_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(str(v).strip())
    except Exception:
        return None


def sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = str(s).lower()
    s = _strip_accents(s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def normalize_name(s: str) -> str:
    s = normalize_text(s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ============================================================
# Boilerplate
# ============================================================
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


def remove_boilerplate_lines(text: str) -> str:
    lines = (text or "").splitlines()
    cleaned: List[str] = []
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


def remove_boilerplate_global(text: str) -> str:
    t = text or ""
    for p in BOILERPLATE_PATTERNS:
        t = re.sub(p, " ", t, flags=re.IGNORECASE)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n\s+\n", "\n\n", t)
    return t.strip()


def is_boilerplate_text(s: str) -> bool:
    return any(re.search(p, s or "", re.IGNORECASE) for p in BOILERPLATE_PATTERNS)


# ============================================================
# PDF extraction (WORDS)
# ============================================================
def extract_text_from_pdf_bytes(pdf_bytes: bytes, max_pages: int = 80) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out: List[str] = []
    n_pages = min(len(doc), max_pages)

    for i in range(n_pages):
        page = doc.load_page(i)
        words = page.get_text("words") or []
        if not words:
            continue

        words.sort(key=lambda w: (round(w[1], 1), round(w[0], 1)))

        line_bins: Dict[int, List[tuple]] = {}
        for w in words:
            y = w[1]
            bin_y = int(round(y / 3.0))
            line_bins.setdefault(bin_y, []).append(w)

        for bin_y in sorted(line_bins.keys()):
            line_words = sorted(line_bins[bin_y], key=lambda w: w[0])
            txt = " ".join([w[4] for w in line_words]).strip()
            if txt:
                out.append(txt)

        out.append("")

    doc.close()
    return "\n".join(out)


# ============================================================
# Golden source: portada
# ============================================================
MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def parse_spanish_date_any(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    m = re.search(r"\b(\d{1,2})\s+de\s+([A-Za-zÁÉÍÓÚáéíóúñÑ]+)\s+de\s+(20\d{2})\b", s, re.IGNORECASE)
    if not m:
        return None
    d = int(m.group(1))
    mn = m.group(2).lower()
    y = int(m.group(3))
    mo = MONTHS.get(mn)
    if not mo:
        return None
    try:
        return datetime(y, mo, d)
    except Exception:
        return None


def extract_cover_lines(pdf_bytes: bytes) -> List[str]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if len(doc) == 0:
        return []
    page = doc.load_page(0)
    words = page.get_text("words") or []
    doc.close()

    if not words:
        return []

    words.sort(key=lambda w: (round(w[1], 1), round(w[0], 1)))

    line_bins: Dict[int, List[tuple]] = {}
    for w in words:
        y = w[1]
        bin_y = int(round(y / 3.0))
        line_bins.setdefault(bin_y, []).append(w)

    lines: List[str] = []
    for bin_y in sorted(line_bins.keys()):
        line_words = sorted(line_bins[bin_y], key=lambda w: w[0])
        txt = " ".join([w[4] for w in line_words]).strip()
        if txt:
            lines.append(txt)

    return lines


def extract_box_header_info(pdf_bytes: bytes) -> Dict[str, Any]:
    lines = extract_cover_lines(pdf_bytes)
    if not lines:
        return {}
    page_text = "\n".join(lines)

    stype = None
    if re.search(r"\bORDEN\s+DEL\s+D[IÍ]A\b", page_text, re.IGNORECASE):
        stype = "ORDEN DEL DIA"
    elif re.search(r"\bACTA\b", page_text, re.IGNORECASE):
        stype = "ACTA"

    mnum = re.search(
        r"\bSESIO[NÓO]\s+(?:ORDINARIA|EXTRAORDINARIA|ESPECIAL)\s+N(?:ro|°|º|o)?[:\.]?\s*0*(\d{1,3})\b",
        page_text,
        re.IGNORECASE,
    )
    sn = str(int(mnum.group(1))) if mnum else None

    my = re.search(r"\bA[ñn]o\s+(20\d{2})\b", page_text, re.IGNORECASE)
    sy = int(my.group(1)) if my else None

    dt = parse_spanish_date_any(page_text)
    date_str = None
    if dt:
        date_str = dt.strftime("%d/%m/%Y")
        if sy is None:
            sy = dt.year

    out: Dict[str, Any] = {}
    if stype:
        out["session_type"] = stype
    if sn:
        out["session_number"] = sn
    if sy:
        out["session_year"] = sy
    if date_str:
        out["date"] = date_str
    return out


# ============================================================
# Filename fallback
# ============================================================
def infer_num_year_from_filename(filename: str) -> Tuple[Optional[int], Optional[int]]:
    if not filename:
        return (None, None)
    f = filename.lower()

    m = re.search(r"\bno[-_\s]*0*(\d{1,3})[-_](\d{2})\b", f)
    if m:
        return (int(m.group(1)), 2000 + int(m.group(2)))

    m = re.search(r"\bno[-_\s]*0*(\d{1,3})[-_](20\d{2})\b", f)
    if m:
        return (int(m.group(1)), int(m.group(2)))

    m = re.search(r"\b[-_](\d{1,3})[-_](20\d{2})\b", f)
    if m:
        return (int(m.group(1)), int(m.group(2)))

    m = re.search(r"\b(\d{1,3})[-_](\d{2})\b", f)
    if m:
        return (int(m.group(1)), 2000 + int(m.group(2)))

    return (None, None)


def normalize_session_number(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    m = re.search(r"\b(\d{1,3})\b", str(s))
    return int(m.group(1)) if m else None


def parse_date_any(s: Optional[str]) -> datetime:
    if not s:
        return datetime.min
    s = s.strip()

    m = re.match(r"(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return datetime(y, mo, d)
        except Exception:
            return datetime.min

    dt = parse_spanish_date_any(s)
    if dt:
        return dt

    return datetime.min


def fmt_date_short(dt: datetime, fallback: str = "s/f") -> str:
    if dt == datetime.min:
        return fallback
    return dt.strftime("%d/%m/%Y")


# ============================================================
# Topics + Impacto
# ============================================================
TOPIC_RULES = [
    ("Agua/Servicios", ["agua", "cloaca", "gas", "energía", "luz", "residuos", "basura", "saneamiento"]),
    ("Tarifas", ["tarifa", "tasas", "impuesto", "tribut", "arancel", "reajuste", "actualización"]),
    ("Obra pública", ["obra", "pavimento", "asfalto", "vereda", "plaza", "puente", "licitación", "contratación"]),
    ("Seguridad", ["seguridad", "tránsito", "control", "inspección", "alcoholemia", "emergencia"]),
    ("Comercio", ["comercio", "habilitación", "local", "feria", "gastronomía", "turismo"]),
    ("Ambiente", ["ambiente", "bosque", "incendio", "impacto", "contaminación", "reserva"]),
    ("Vivienda/Urbanismo", ["vivienda", "urban", "lote", "zonificación", "catastro", "edificación", "loteo"]),
    ("Educación/Cultura", ["educación", "escuela", "cultura", "biblioteca", "museo", "deporte"]),
]


def classify_topics(text: str) -> List[str]:
    t = (text or "").lower()
    topics: List[str] = []
    for label, keys in TOPIC_RULES:
        if any(k in t for k in keys):
            topics.append(label)
    return topics


IMPACT_RULES = [
    ("💰 Bolsillo", ["tarifa", "tasa", "impuesto", "tribut", "arancel", "reajuste", "actualización", "módulo", "valor"]),
    ("🏗 Infraestructura", ["obra", "licitación", "contratación", "pavimento", "asfalto", "vereda", "plaza", "puente"]),
    ("🚦 Tránsito/Seguridad", ["tránsito", "circulación", "corte", "estacionamiento", "control", "inspección", "alcoholemia", "seguridad"]),
    ("🚰 Servicios", ["agua", "cloaca", "gas", "energía", "luz", "residuos", "basura", "saneamiento"]),
    ("🏪 Comercio", ["habilitación", "comercio", "local", "feria", "gastronomía", "turismo"]),
    ("🏘 Urbanismo", ["lote", "loteo", "urban", "zonificación", "catastro", "edificación", "vivienda"]),
    ("🌿 Ambiente", ["ambiente", "bosque", "incendio", "impacto", "contaminación", "reserva"]),
]


def infer_impact(text: str) -> Dict[str, Any]:
    t = (text or "").lower()
    hits: List[str] = []
    for label, keys in IMPACT_RULES:
        if any(k in t for k in keys):
            hits.append(label)

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


# ============================================================
# Entidades
# ============================================================
ORG_HINTS = [
    " S.A.", " SA", " S.R.L.", " SRL", " SAS", " S.A.S.", " COOP", " COOPERATIVA", " ASOCIACIÓN", " FUNDACIÓN",
    " CLUB", " CÁMARA", " CAMARA", " MUNICIPALIDAD", " PROVINCIA", " MINISTERIO", " ENTE", " DIRECCIÓN",
    " DIRECCION", " HOSPITAL", " ESCUELA", " UNIVERSIDAD",
]

TITLE_PREFIXES = r"(?:Sr\.|Sra\.|Señor|Señora|Dr\.|Dra\.|Ing\.|Arq\.|Agrim\.|Lic\.|Prof\.|Abog\.|Cdor\.|Téc\.|Tec\.)"

PERSON_STOP_PHRASES = {
    "Acuerdo Parlamentario",
    "Ad Referéndum",
    "Ad Referendum",
    "Asuntos Entrados",
    "Asunto Entrado",
    "Miembro Informante",
    "Miembros Informantes",
    "Periodo Ordinario",
    "Período Ordinario",
    "Sala Investigadora",
    "Solicitud Banca",
    "Solicitud de Banca",
    "Banca Automática",
    "Banca Automática Móvil",
    "Comisiones",
    "Comisión",
    "Comisiones de",
    "Comisiones de Decretos",
    "Comisiones de Gobierno",
    "Convenio Marco",
    "Interés Municipal",
    "Orden del Día",
    "ORDEN DEL DÍA",
}

NON_PERSON_CONTAINS = {
    "comision", "comisiones", "asuntos", "entrados", "referendum", "referéndum",
    "acuerdo", "parlamentario", "orden del dia", "orden del día", "decretos",
    "tema", "proyecto", "expediente", "dictamen", "homenajes", "de hacienda",
    "de gobierno", "de turismo", "de transito", "de tránsito", "de obras", "de planeamiento",
    "banca", "periodo", "período",
}

NON_PERSON_LEADING_WORDS = {
    "Lago", "Laguna", "Cerro", "Barrio", "Ruta", "Avenida", "Avda", "Av.", "Plaza",
    "Parque", "Costanera", "Centro", "Municipalidad", "Concejo", "Provincia",
    "Ministerio", "Dirección", "Direccion", "Secretaría", "Secretaria",
    "Junta", "Juzgado", "Hospital", "Escuela", "Universidad",
    "Departamento", "Comision", "Comisión", "Comisiones", "Sala",
    "Concejal", "Concejala",
}

TRAILING_NOISE = {"Dictamen", "Tema", "Proyecto", "Expediente", "Decreto", "Ordenanza", "Nota", "Solicitud", "Banca", "Informe", "Tratamiento"}


def _clean_name_candidate(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^[\-\•\●\·\*]+\s*", "", s).strip()
    s = re.sub(r"\s{2,}", " ", s)
    s = s.strip(" ,;:.")
    return s


def _strip_trailing_noise(name: str) -> str:
    toks = (name or "").split()
    while toks and toks[-1] in TRAILING_NOISE:
        toks = toks[:-1]
    return " ".join(toks)


def _looks_like_person(name: str) -> bool:
    name = _clean_name_candidate(name)
    name = _strip_trailing_noise(name)
    if not name:
        return False
    if is_boilerplate_text(name):
        return False
    if name in PERSON_STOP_PHRASES:
        return False

    low = normalize_text(name)
    if any(tok in low for tok in NON_PERSON_CONTAINS):
        return False

    first = name.split(" ", 1)[0]
    if first in NON_PERSON_LEADING_WORDS:
        return False

    toks = name.split()
    if len(toks) < 2:
        return False

    if len(name) < 7 or len(name) > 80:
        return False

    if re.search(r"\bDE\s+(HACIENDA|GOBIERNO|TURISMO|TRANSITO|TRÁNSITO|OBRAS|PLANEAMIENTO)\b", name.upper()):
        return False

    if name.isupper() and len(name) > 18:
        return False

    return True


def extract_entities(text: str) -> Dict[str, List[str]]:
    t = text or ""
    persons = set()

    for m in re.finditer(
        rf"\bIntendente\s+Municipal(?:\s+{TITLE_PREFIXES})?\s+([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+(?:\s+[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+){{1,4}})\b",
        t
    ):
        cand = _strip_trailing_noise(_clean_name_candidate(m.group(1)))
        if _looks_like_person(cand):
            persons.add(cand)

    for m in re.finditer(
        r"\bvecin[oa]\s+([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+(?:\s+[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+){1,4})\b",
        t
    ):
        cand = _strip_trailing_noise(_clean_name_candidate(m.group(1)))
        if _looks_like_person(cand):
            persons.add(cand)

    for m in re.finditer(
        rf"\b{TITLE_PREFIXES}\s+([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+(?:\s+[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+){{1,5}})\b",
        t
    ):
        cand = _strip_trailing_noise(_clean_name_candidate(m.group(1)))
        if _looks_like_person(cand):
            persons.add(cand)

    for m in re.finditer(
        r"\b([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+)\s*,\s*([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+(?:\s+[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+){0,2})\b",
        t
    ):
        cand = _strip_trailing_noise(_clean_name_candidate(f"{m.group(2)} {m.group(1)}"))
        if _looks_like_person(cand):
            persons.add(cand)

    orgs = set()
    for ln in (t.splitlines() or []):
        ln_strip = (ln or "").strip()
        if not ln_strip:
            continue
        if is_boilerplate_text(ln_strip):
            continue
        up = ln_strip.upper()
        if any(h.strip().upper() in up for h in ORG_HINTS):
            frag = normalize_space(ln_strip)
            if 5 < len(frag) < 140:
                orgs.add(frag)

    return {"persons": sorted(persons), "orgs": sorted(orgs)}


# ============================================================
# "Qué dice"
# ============================================================
STOP_PHRASES = [
    "Comisiones", "Comisión", "Decretos Ad Referéndum", "Ad Referéndum",
    "Orden del Día", "ORDEN DEL DÍA",
    "C) Comisiones", "C) COMISIONES",
]

PLACE_HINTS = [
    "Costanera", "Lago", "Lácar", "Lacar", "San Martín de los Andes", "San Martin de los Andes",
    "Barrio", "Ruta", "Avenida", "Plaza", "Centro", "Municipalidad", "Concejo Deliberante",
]


def clean_snippet(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    for p in STOP_PHRASES:
        s = s.replace(p, "")
    s = re.sub(r"\s+", " ", s).strip(" -–—,;:. ")
    return s


def extract_person_in_line(s: str) -> Optional[str]:
    m = re.search(
        r"\bvecin[oa]\s+([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+(?:\s+[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+){0,4})\b",
        s
    )
    if m:
        cand = _clean_name_candidate(m.group(1))
        return cand if _looks_like_person(cand) else None

    m = re.search(
        rf"\b{TITLE_PREFIXES}\s+([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+(?:\s+[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+){{1,5}})\b",
        s
    )
    if m:
        cand = _clean_name_candidate(m.group(1))
        return cand if _looks_like_person(cand) else None

    m = re.search(
        r"\bdictad[oa]\s+del\s+([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+(?:\s+[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+){1,4})\b",
        s,
        re.IGNORECASE
    )
    if m:
        cand = _clean_name_candidate(m.group(1))
        return cand if _looks_like_person(cand) else None

    return None


def extract_place(s: str) -> Optional[str]:
    low = s.lower()
    for h in PLACE_HINTS:
        idx = low.find(h.lower())
        if idx >= 0:
            start = max(0, idx - 45)
            end = min(len(s), idx + 80)
            return s[start:end].strip(" ,;:.")
    return None


def infer_action(s: str) -> str:
    t = s.lower()
    if "dictamen" in t:
        return "Dictamen"
    if "decreto" in t:
        return "Decreto"
    if "nota" in t:
        return "Nota"
    if "pedido" in t or "solicita" in t:
        return "Pedido"
    if "habilitación" in t or "habilitacion" in t:
        return "Habilitación"
    if "obra" in t or "licitación" in t or "licitacion" in t or "pavimento" in t:
        return "Obra pública"
    return "Tema"


def build_que_dice(raw: str) -> str:
    s = clean_snippet(raw)
    if not s:
        return ""
    if is_boilerplate_text(s):
        return ""

    action = infer_action(s)
    person = extract_person_in_line(s)
    place = extract_place(s)

    main = s
    if len(main) > 260:
        main = main[:260].rsplit(" ", 1)[0] + "…"

    parts = [f"{action}:"]
    if person:
        parts.append(person + " —")
    parts.append(main)
    if place and place not in main:
        parts.append(f"📍 {place}")

    out = " ".join(parts)
    out = re.sub(r"\s+", " ", out).strip()
    return out


# ============================================================
# Metadata
# ============================================================
def parse_metadata(text: str, filename: str, box: Dict[str, Any]) -> Dict[str, Any]:
    t = text or ""
    head = "\n".join(t.splitlines()[:200])

    session_type = box.get("session_type")
    session_number = box.get("session_number")
    session_year = box.get("session_year")
    date_str = box.get("date")

    f_num, f_year = infer_num_year_from_filename(filename or "")
    if session_number is None and f_num is not None:
        session_number = str(f_num)
    if session_year is None and f_year is not None:
        session_year = f_year

    if session_type is None:
        session_type = find_first(
            [r"(Orden\s+del\s+D[ií]a)", r"(Acta)", r"(Sesión\s+Ordinaria)", r"(Sesión\s+Extraordinaria)", r"(Sesión\s+Especial)"],
            head,
        )

    if date_str is None:
        date_str = find_first(
            [
                r"San\s+Mart[ií]n\s+de\s+los\s+Andes\s*,?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
                r"San\s+Mart[ií]n\s+de\s+los\s+Andes\s*,?\s*(\d{1,2}\s+de\s+[A-Za-zÁÉÍÓÚáéíóúñÑ]+\s+de\s+20\d{2})",
                r"(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
                r"(\d{1,2}\s+de\s+[A-Za-zÁÉÍÓÚáéíóúñÑ]+\s+de\s+20\d{2})",
            ],
            head,
            flags=re.IGNORECASE | re.DOTALL,
        )
        dt = parse_date_any(date_str)
        if dt != datetime.min:
            date_str = dt.strftime("%d/%m/%Y")

    if session_year is None:
        dt = parse_date_any(date_str)
        if dt != datetime.min:
            session_year = dt.year

    sn_int = normalize_session_number(session_number)
    session_number = str(sn_int) if sn_int is not None else None

    time_str = find_first(
        [
            r"(?:Hora|HORA)\s*(?:de\s*inicio\s*)?:?\s*(\d{1,2}:\d{2})",
            r"(?:Siendo|SIENDO)\s+las\s+(\d{1,2}:\d{2})",
        ],
        head,
        flags=re.IGNORECASE,
    )

    decrees = sorted(set(re.findall(r"(?:Decreto|DECRETO)\s*(?:N[°º]\s*)?([A-Z0-9\-\./]+)", t, re.IGNORECASE)))
    exped = sorted(set(re.findall(r"(?:Expediente|EXPEDIENTE)\s*(?:N[°º]\s*)?([A-Z0-9\-\./]+)", t, re.IGNORECASE)))

    council = sorted(
        set(
            re.findall(
                r"(?:Concejal(?:a)?|CONCEJAL(?:A)?)(?:\s+informante)?\s*[:\-]?\s+([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ\.\- ]{2,60})",
                t,
                re.IGNORECASE,
            )
        )
    )

    return {
        "session_type": session_type,
        "session_number": session_number,
        "session_year": session_year,
        "date": date_str,
        "time": time_str,
        "decretos": decrees[:300],
        "expedientes": exped[:300],
        "council_members": council[:300],
        "filename_inferred_number": f_num,
        "filename_inferred_year": f_year,
    }


# ============================================================
# Items
# ============================================================
def extract_items(text: str) -> List[Dict[str, Any]]:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    items: List[Dict[str, Any]] = []

    exp_tema_pat = re.compile(r"^(\d{1,6}(?:[-]\d{1,6})?/\d{4})\s*[—–-]\s*Tema:\s*(.*)$", re.IGNORECASE)
    exp_pat = re.compile(r"^(\d{1,6}(?:[-]\d{1,6})?/\d{4})\s*[—–-]\s*(.*)$", re.IGNORECASE)
    num_pat = re.compile(r"^(\d{1,2})[\.\)]\s+(.*)$")

    def is_new_item(line: str) -> Optional[Tuple[str, re.Match]]:
        for typ, p in [("exp_tema", exp_tema_pat), ("exp", exp_pat), ("num", num_pat)]:
            m = p.match(line)
            if m:
                return (typ, m)
        return None

    i = 0
    while i < len(lines):
        hit = is_new_item(lines[i])
        if not hit:
            i += 1
            continue

        typ, m = hit
        key = (m.group(1) or "").strip()
        first = (m.group(2) or "").strip()

        parts = [first] if first else []
        j = i + 1
        while j < len(lines):
            if is_new_item(lines[j]):
                break
            parts.append(lines[j])
            j += 1

        full = normalize_space(" ".join(parts))
        full = remove_boilerplate_global(full)

        if is_boilerplate_text(full):
            i = j
            continue

        dec_inside = find_first([r"(?:Decreto)\s*(?:N[°º]\s*)?([A-Z0-9\-\./]+)"], full)
        exp_inside = find_first([r"(?:Expediente)\s*(?:N[°º]\s*)?([A-Z0-9\-\./]+)"], full)

        qd = build_que_dice(full)
        if not qd:
            i = j
            continue

        impact = infer_impact(full)

        items.append(
            {
                "key": key if key else None,
                "title": full[:2000],
                "que_dice": qd,
                "impact_text": impact["impact_text"],
                "impact_tags": impact["impact_tags"],
                "decreto": dec_inside,
                "expediente": exp_inside or (key if typ in ("exp", "exp_tema") else None),
                "topics": classify_topics(full),
            }
        )

        i = j

    for idx, it in enumerate(items, start=1):
        if not it.get("key"):
            it["key"] = str(idx)

    return items[:800]


# ============================================================
# Retrocompat
# ============================================================
def ensure_items_schema(items: Any) -> List[Dict[str, Any]]:
    if not items:
        return []
    if not isinstance(items, list):
        return []

    if items and isinstance(items[0], str):
        fixed: List[Dict[str, Any]] = []
        for idx, s in enumerate(items, start=1):
            qd = build_que_dice(s)
            if not qd:
                continue
            imp = infer_impact(s)
            fixed.append(
                {
                    "key": str(idx),
                    "title": s,
                    "que_dice": qd,
                    "impact_text": imp["impact_text"],
                    "impact_tags": imp["impact_tags"],
                    "decreto": None,
                    "expediente": None,
                    "topics": classify_topics(s),
                }
            )
        return fixed

    fixed_items: List[Dict[str, Any]] = []
    for idx, it in enumerate(items, start=1):
        if not isinstance(it, dict):
            continue
        key = it.get("key") or it.get("id") or it.get("nro") or str(idx)
        title = it.get("title") or it.get("texto") or it.get("descripcion") or ""
        qd = it.get("que_dice") or it.get("qué_dice") or build_que_dice(title)
        if not qd:
            continue
        base = {**it, "key": str(key), "title": title, "que_dice": qd}
        if "impact_text" not in base:
            imp = infer_impact(title)
            base["impact_text"] = imp["impact_text"]
            base["impact_tags"] = imp["impact_tags"]
        fixed_items.append(base)

    return fixed_items


def build_summary_citizen(meta: Dict[str, Any], topics: List[str], items: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    head = []
    dt = parse_date_any(meta.get("date"))
    date_disp = fmt_date_short(dt, fallback=(meta.get("date") or "s/f"))

    head.append(f"**Fecha:** {date_disp}")
    if meta.get("time"):
        head.append(f"**Hora:** {meta['time']}")

    stype = meta.get("session_type") or "ORDEN DEL DIA"
    sn = meta.get("session_number") or "s/n"
    sy = meta.get("session_year") or "s/año"
    head.append(f"**Documento:** {stype} N° {sn} (Año {sy})")

    parts.append(" · ".join(head))

    if topics:
        parts.append(f"**Temas:** {', '.join(topics)}")

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
                if imp:
                    parts.append(f"  - _Impacto_: {imp}")
    else:
        parts.append("No se detectaron ítems claros (PDF escaneado o texto muy roto).")

    return "\n".join(parts)


def process_pdf(pdf_bytes: bytes, filename: str) -> Dict[str, Any]:
    box = extract_box_header_info(pdf_bytes)

    raw_text = extract_text_from_pdf_bytes(pdf_bytes)
    raw_text = remove_boilerplate_lines(raw_text)
    raw_text = remove_boilerplate_global(raw_text)
    text = normalize_space(raw_text)

    meta = parse_metadata(text, filename=filename, box=box)
    topics = classify_topics(text)
    items = extract_items(text)
    entities = extract_entities(text)

    return {
        "processor_version": PROCESSOR_VERSION,
        "source": {"filename": filename, "processed_at": datetime.utcnow().isoformat() + "Z"},
        "box_meta": box,
        **meta,
        "topics": topics,
        "items": items,
        "entities": entities,
        "summary_citizen": build_summary_citizen(meta, topics, items),
        "text_preview": text[:12000],
    }


# ============================================================
# Dataset utils
# ============================================================
def download_raw_pdf(repo_id: str, doc_id: str) -> bytes:
    filename = f"raw/{doc_id}.pdf"
    local_path = hf_hub_download(repo_id=repo_id, repo_type="dataset", filename=filename)
    with open(local_path, "rb") as f:
        return f.read()


def is_stale_cached(cached: Optional[Dict[str, Any]]) -> bool:
    if not cached:
        return True
    return cached.get("processor_version") != PROCESSOR_VERSION


def normalize_cached_result(cached: Dict[str, Any]) -> Dict[str, Any]:
    cached = dict(cached)
    cached["items"] = ensure_items_schema(cached.get("items"))

    sn_norm = normalize_session_number(cached.get("session_number"))
    if sn_norm is not None:
        cached["session_number"] = str(sn_norm)

    if not cached.get("session_year"):
        dt = parse_date_any(cached.get("date"))
        if dt != datetime.min:
            cached["session_year"] = dt.year

    if not cached.get("entities"):
        cached["entities"] = extract_entities(cached.get("text_preview", ""))

    if not cached.get("summary_citizen"):
        meta = {
            "session_type": cached.get("session_type"),
            "session_number": cached.get("session_number"),
            "session_year": cached.get("session_year"),
            "date": cached.get("date"),
            "time": cached.get("time"),
        }
        cached["summary_citizen"] = build_summary_citizen(meta, cached.get("topics") or [], cached.get("items") or [])

    return cached


# ============================================================
# UI helpers: orden + label
# ============================================================
def doc_sort_key(r: Dict[str, Any]) -> Tuple[int, int, int]:
    year = safe_int(r.get("session_year"))
    sn = normalize_session_number(r.get("session_number"))
    dt = parse_date_any(r.get("date"))
    ts = int(dt.timestamp()) if dt != datetime.min else 0
    return (year or 0, sn or 0, ts)


def doc_label(r: Dict[str, Any]) -> str:
    stype = (r.get("session_type") or "ORDEN DEL DIA").strip()
    year = safe_int(r.get("session_year"))
    sn = normalize_session_number(r.get("session_number"))
    dt = parse_date_any(r.get("date"))
    date_disp = fmt_date_short(dt, fallback=(r.get("date") or "s/f"))

    num_txt = f"{sn:02d}" if isinstance(sn, int) else "s/n"
    year_txt = str(year) if year else "s/año"

    base = f"{stype} {num_txt} — Año {year_txt} · {date_disp}"

    # fallback útil si sigue flojo el metadata
    if num_txt == "s/n" and year_txt == "s/año":
        fname = (r.get("original_filename") or r.get("source_filename") or "").strip()
        if fname:
            base = f"{stype} · {fname}"

    return base


def enrich_index_record(r: Dict[str, Any]) -> Dict[str, Any]:
    rr = dict(r or {})
    doc_id = rr.get("doc_id")
    cached: Dict[str, Any] = {}
    if doc_id:
        try:
            cached = load_processed_json(DATASET_REPO_ID, doc_id) or {}
        except Exception:
            cached = {}

    # completar desde processed JSON si falta
    for k in ["session_type", "session_number", "session_year", "date", "time"]:
        if not rr.get(k) and cached.get(k):
            rr[k] = cached.get(k)

    # fallback desde source.filename si no hay original_filename
    if not rr.get("original_filename"):
        source = cached.get("source") or {}
        if source.get("filename"):
            rr["original_filename"] = source.get("filename")

    # fallback desde filename
    if not rr.get("session_number") or not rr.get("session_year"):
        fnum, fyr = infer_num_year_from_filename(rr.get("original_filename") or "")
        if not rr.get("session_number") and fnum is not None:
            rr["session_number"] = str(fnum)
        if not rr.get("session_year") and fyr is not None:
            rr["session_year"] = fyr

    if not rr.get("session_year"):
        dt = parse_date_any(rr.get("date"))
        if dt != datetime.min:
            rr["session_year"] = dt.year

    if not rr.get("session_type"):
        rr["session_type"] = "ORDEN DEL DIA"

    return rr


def build_history_options(idx: List[Dict[str, Any]]) -> List[Tuple[str, str, str]]:
    enriched = [enrich_index_record(r) for r in idx]
    enriched_sorted = sorted(enriched, key=doc_sort_key, reverse=True)

    raw_options: List[Tuple[str, str, str]] = []
    for r in enriched_sorted:
        label = doc_label(r)
        doc_id = r.get("doc_id") or ""
        original_filename = r.get("original_filename") or ""
        raw_options.append((label, doc_id, original_filename))

    # hacer labels únicos para que no colisionen en selectbox
    counts: Dict[str, int] = {}
    unique_options: List[Tuple[str, str, str]] = []
    for label, doc_id, original_filename in raw_options:
        counts[label] = counts.get(label, 0) + 1
        unique_label = label
        if counts[label] > 1:
            short_id = doc_id[:8] if doc_id else "sinid"
            unique_label = f"{label} · {short_id}"
        unique_options.append((unique_label, doc_id, original_filename))

    return unique_options


# ============================================================
# Glosario manual
# ============================================================
REF_PAT = re.compile(
    r"\b(\d{4,6}[-]?\d{1,4}\/\d{4}|\d{1,4}\/\d{4}|EE-\d{4}-\d{3,}|[A-Z]{1,4}-\d{4}-\d{3,})\b"
)


@st.cache_data(show_spinner=False)
def load_manual_refs() -> List[Dict[str, Any]]:
    try:
        return _read_jsonl(DATASET_REPO_ID, MANUAL_REFS_PATH)
    except Exception:
        return []


@st.cache_data(show_spinner=False)
def manual_refs_map() -> Dict[str, Dict[str, Any]]:
    rows = load_manual_refs()
    m: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        key = normalize_text(r.get("ref_number") or "")
        if not key:
            continue
        if key not in m or (r.get("created_at") or "") > (m[key].get("created_at") or ""):
            m[key] = r
    return m


def find_refs_in_text(s: str) -> List[str]:
    if not s:
        return []
    hits = []
    for m in REF_PAT.finditer(s):
        val = (m.group(1) or "").strip()
        if val and val not in hits:
            hits.append(val)
    return hits[:6]


def render_refs_enrichment(text: str) -> None:
    refs = find_refs_in_text(text)
    if not refs:
        return
    m = manual_refs_map()
    shown = 0
    for ref in refs:
        k = normalize_text(ref)
        if k in m:
            r = m[k]
            title = (r.get("title") or "").strip()
            summary = (r.get("summary") or "").strip()
            label = f"📌 Referencia {ref}"
            if title:
                label += f" — {title}"
            st.info(label)
            if summary:
                st.caption(summary[:450] + ("…" if len(summary) > 450 else ""))
            shown += 1
    if shown == 0:
        return


# ============================================================
# UI: detalle ítem
# ============================================================
def render_item_detail(df_all: pd.DataFrame, selected_key: str, file_tag: str) -> None:
    if df_all.empty:
        st.info("No hay ítems para mostrar.")
        return

    df_all = df_all.copy()
    df_all["key"] = df_all["key"].astype(str)
    row_df = df_all[df_all["key"] == str(selected_key)]
    if row_df.empty:
        st.warning("No encontré el ítem seleccionado.")
        return

    row = row_df.iloc[0].to_dict()

    st.markdown(f"#### Ítem {row.get('key')}")
    st.write(f"**Qué dice:** {row.get('que_dice','')}")
    if row.get("impact_text"):
        st.write(f"**¿En qué te afecta?:** {row.get('impact_text')}")

    meta_line = []
    if row.get("expediente"):
        meta_line.append(f"**Expediente:** {row.get('expediente')}")
    if row.get("decreto"):
        meta_line.append(f"**Decreto:** {row.get('decreto')}")
    if row.get("topics"):
        meta_line.append(f"**Temas:** {', '.join(row.get('topics') or [])}")
    if meta_line:
        st.caption(" · ".join(meta_line))

    render_refs_enrichment(str(row.get("title", "")))

    st.caption("Texto original (extraído del PDF)")
    st.text_area(
        f"Original ({file_tag})",
        value=str(row.get("title", ""))[:6000],
        height=220,
        key=f"orig_{file_tag}",
    )

    c1, c2 = st.columns(2)
    with c1:
        st.caption("Copiable (resumen)")
        st.code(row.get("que_dice", ""), language="markdown")
    with c2:
        st.caption("Para WhatsApp (sin markdown)")
        wa = f"Ítem {row.get('key')}\n{row.get('que_dice','')}\n\nImpacto: {row.get('impact_text','')}".strip()
        st.text_area(f"WhatsApp ({file_tag})", value=wa, height=140, key=f"wa_{file_tag}")

    st.download_button(
        "📄 Descargar este ítem (TXT)",
        data=wa.encode("utf-8"),
        file_name=f"item_{file_tag}_{row.get('key')}.txt",
        mime="text/plain",
        key=f"dl_{file_tag}",
    )


# ============================================================
# Buscador Global
# ============================================================
def make_snippet(text: str, query_norm: str, width: int = 220) -> str:
    if not text:
        return ""
    t_norm = normalize_text(text)
    i = t_norm.find(query_norm)
    if i == -1:
        return (text[:width] + "…") if len(text) > width else text
    start = max(0, i - width // 2)
    end = min(len(text), start + width)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def build_search_records_from_acta(acta: Dict[str, Any], doc_id: str) -> List[Dict[str, Any]]:
    year = acta.get("session_year")
    acta_num = acta.get("session_number")
    fecha = acta.get("date")

    summary = acta.get("summary_citizen") or ""
    text_preview = acta.get("text_preview") or ""

    ents = acta.get("entities") or {}
    people = ents.get("persons") or []
    orgs = ents.get("orgs") or []

    acta_blob = normalize_text(" ".join([
        summary,
        text_preview,
        " ".join((acta.get("decretos") or [])[:50]),
        " ".join((acta.get("expedientes") or [])[:50]),
    ]))

    records: List[Dict[str, Any]] = [{
        "level": "acta",
        "year": year,
        "doc_id": doc_id,
        "acta_num": acta_num,
        "fecha": fecha,
        "item_key": None,
        "item_title": None,
        "search_blob": acta_blob,
        "people": people,
        "orgs": orgs,
        "raw_for_snippet": {"summary_citizen": summary, "text_preview": text_preview},
    }]

    items = ensure_items_schema(acta.get("items") or [])
    for it in items:
        title = it.get("title") or ""
        que_dice = it.get("que_dice") or ""
        impacto = it.get("impact_text") or ""
        decreto = it.get("decreto") or ""
        expediente = it.get("expediente") or ""

        blob = normalize_text(" ".join([title, que_dice, impacto, decreto, expediente]))

        records.append({
            "level": "item",
            "year": year,
            "doc_id": doc_id,
            "acta_num": acta_num,
            "fecha": fecha,
            "item_key": str(it.get("key") or ""),
            "item_title": (it.get("que_dice") or it.get("title") or "")[:220],
            "search_blob": blob,
            "people": people,
            "orgs": orgs,
            "raw_for_snippet": {
                "title": title,
                "que_dice": que_dice,
                "impact_text": impacto,
                "decreto": decreto,
                "expediente": expediente,
            }
        })

    return records


def score_record(rec: Dict[str, Any], qn: str) -> int:
    score = 0

    people_norm = [normalize_name(p) for p in (rec.get("people") or [])]
    q_words = (qn or "").split()
    strict_person = (len(q_words) == 1)

    if strict_person:
        if qn in people_norm:
            score += 220
    else:
        if qn in people_norm:
            score += 220
        elif any(qn and qn in pn for pn in people_norm):
            score += 140

    raw = rec.get("raw_for_snippet") or {}
    title = (raw.get("que_dice") or raw.get("title") or rec.get("item_title") or "")
    if title and qn in normalize_text(title):
        score += 90

    blob = rec.get("search_blob") or ""
    if qn and qn in blob:
        score += 30

    return score


def search_records(records: List[Dict[str, Any]], query: str, year_filter: Optional[int] = None) -> List[Dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []
    qn = normalize_name(q)

    hits: List[Dict[str, Any]] = []
    for rec in records:
        if year_filter and rec.get("year"):
            try:
                if int(rec["year"]) != int(year_filter):
                    continue
            except Exception:
                continue

        blob = rec.get("search_blob") or ""
        people_norm = [normalize_name(p) for p in (rec.get("people") or [])]

        q_words = (qn or "").split()
        strict_person = (len(q_words) == 1)

        text_match = (qn in blob)
        if strict_person:
            person_match = any(qn == pn for pn in people_norm)
        else:
            person_match = any(qn and (qn == pn or qn in pn) for pn in people_norm)

        if not (text_match or person_match):
            continue

        out = dict(rec)
        out["_person_match"] = person_match
        out["_text_match"] = text_match
        out["_score"] = score_record(rec, qn)

        raw = rec.get("raw_for_snippet") or {}
        base_text = raw.get("que_dice") or raw.get("summary_citizen") or raw.get("text_preview") or raw.get("title") or ""
        out["_snippet"] = make_snippet(base_text, normalize_text(qn))
        hits.append(out)

    def _date_key(x: Dict[str, Any]) -> datetime:
        f = x.get("fecha") or ""
        try:
            return datetime.fromisoformat(str(f).replace("Z", "").strip())
        except Exception:
            return datetime.min

    hits.sort(key=lambda r: (r.get("_score", 0), _date_key(r)), reverse=True)
    return hits


@st.cache_data(show_spinner=False)
def load_index_safe() -> List[Dict[str, Any]]:
    if load_index is None:
        return []
    try:
        idx = load_index(DATASET_REPO_ID) or []
        return [dict(x or {}) for x in idx]
    except Exception:
        return []


@st.cache_data(show_spinner=False)
def build_global_search_records() -> List[Dict[str, Any]]:
    idx = load_index_safe()
    records: List[Dict[str, Any]] = []
    for r in idx:
        doc_id = r.get("doc_id")
        if not doc_id:
            continue
        cached = load_processed_json(DATASET_REPO_ID, doc_id) or {}
        if not cached:
            continue
        cached = normalize_cached_result(cached)
        records.extend(build_search_records_from_acta(cached, doc_id=doc_id))
    return records


def render_global_search_tab() -> None:
    st.subheader("🔎 Buscar en todo")

    if load_index is None:
        st.info("Buscador global no disponible: tu hf_dataset_store.py no tiene load_index().")
        return

    records = build_global_search_records()
    years = sorted({r.get("year") for r in records if r.get("year")}, reverse=True)
    year_opt = st.selectbox("Año", ["Todos"] + [str(y) for y in years], index=0)

    with st.form("global_search_form", clear_on_submit=False):
        query = st.text_input(
            "Buscar (texto o nombre de persona)",
            placeholder="Ej: Pablo / Gomez / expediente 123 / tarifa / parque…",
            key="global_search_q",
        )
        submitted = st.form_submit_button("🔎 Buscar")

    if not submitted:
        st.caption("Tip: en celular, usá el botón **Buscar** (no hace falta tocar Enter).")
        return

    if not query.strip():
        st.warning("Escribí algo para buscar.")
        return

    year_filter = None if year_opt == "Todos" else int(year_opt)
    hits = search_records(records, query=query, year_filter=year_filter)

    st.caption(f"Resultados: {len(hits)} (mostrando hasta 80)")

    for i, h in enumerate(hits[:80], start=1):
        doc_id = h.get("doc_id")
        acta_num = h.get("acta_num") or "s/n"
        year = h.get("year") or "s/año"
        fecha = h.get("fecha") or "s/f"
        level = h.get("level")
        item_key = h.get("item_key")

        title = f"Acta {acta_num} ({year}) — {fecha}"
        if level == "item" and item_key:
            title += f" — Ítem {item_key}"

        with st.expander(title, expanded=False):
            if h.get("_person_match"):
                st.caption("✅ Coincidencia por **persona** detectada")
            elif h.get("_text_match"):
                st.caption("✅ Coincidencia por **texto**")

            snip = h.get("_snippet") or ""
            if snip:
                st.write(snip)

            render_refs_enrichment(snip)

            c1, c2 = st.columns([1, 2])
            with c1:
                if st.button("📄 Abrir acta", key=f"open_{doc_id}_{item_key}_{level}_{i}"):
                    st.session_state["open_doc_id"] = doc_id
                    st.session_state["open_item_key"] = item_key
                    st.success("Listo: abrí la acta desde la pestaña Histórico (se preselecciona).")
            with c2:
                st.caption(f"doc_id: {doc_id}")


# ============================================================
# Suscripción
# ============================================================
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def render_subscribe_tab(admin_ok: bool) -> None:
    st.subheader("📩 Suscripción por Email")
    st.write("Dejá tu email para recibir aviso cuando aparezca una nueva acta (cuando esté activo el auto-fetch).")

    consent = st.checkbox("Acepto recibir emails del Observatorio (máximo 1 por acta nueva).", value=True)
    email = st.text_input("Tu email", placeholder="tuemail@dominio.com", key="sub_email").strip()

    if st.button("✅ Suscribirme"):
        if not consent:
            st.warning("Necesitás aceptar el consentimiento para suscribirte.")
        elif not email or not EMAIL_RE.match(email):
            st.warning("Email inválido.")
        elif not HF_TOKEN:
            st.error("Falta HF_TOKEN para guardar suscripciones en el dataset.")
        else:
            email_norm = email.lower().strip()
            email_hash = hashlib.sha256(email_norm.encode("utf-8")).hexdigest()[:16]
            existing = _read_jsonl(DATASET_REPO_ID, SUBSCRIBERS_PATH)
            if any((r.get("email_hash") == email_hash) for r in existing):
                st.info("Ese email ya está suscripto ✅")
            else:
                row = {
                    "email": email_norm,
                    "email_hash": email_hash,
                    "created_at": datetime.utcnow().isoformat() + "Z",
                    "source": "streamlit",
                }
                _append_jsonl(DATASET_REPO_ID, SUBSCRIBERS_PATH, row, commit_message="Add subscriber")
                st.success("Suscripción guardada ✅")

    if admin_ok:
        with st.expander("🔒 Admin — Ver suscriptores"):
            rows = _read_jsonl(DATASET_REPO_ID, SUBSCRIBERS_PATH) if HF_TOKEN else []
            st.caption(f"Total: {len(rows)}")
            if rows:
                df = pd.DataFrame(rows).sort_values("created_at", ascending=False)
                st.dataframe(df, width="stretch", hide_index=True)
            else:
                st.info("No hay suscriptores (o no hay HF_TOKEN).")


# ============================================================
# Métricas
# ============================================================
def _get_or_make_session_id() -> str:
    if "anon_session_id" not in st.session_state:
        raw = f"{time.time()}-{os.urandom(16).hex()}"
        st.session_state["anon_session_id"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return str(st.session_state["anon_session_id"])


def log_visit_once_per_session() -> None:
    if st.session_state.get("visit_logged"):
        return
    st.session_state["visit_logged"] = True

    if not HF_TOKEN:
        return

    row = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "session": _get_or_make_session_id(),
    }
    try:
        _append_jsonl(DATASET_REPO_ID, VISITS_PATH, row, commit_message="Log visit")
    except Exception:
        pass


def render_admin_metrics(admin_ok: bool) -> None:
    if not admin_ok:
        return
    with st.sidebar:
        with st.expander("📈 Métricas (admin)", expanded=False):
            if not HF_TOKEN:
                st.info("Sin HF_TOKEN: no se guardan/leen métricas.")
                return
            rows = _read_jsonl(DATASET_REPO_ID, VISITS_PATH)
            if not rows:
                st.info("Sin visitas registradas todavía.")
                return
            today = datetime.utcnow().strftime("%Y-%m-%d")

            last_7 = set()
            last_30 = set()
            dt_today = datetime.utcnow()
            for i in range(7):
                last_7.add((dt_today - pd.Timedelta(days=i)).strftime("%Y-%m-%d"))
            for i in range(30):
                last_30.add((dt_today - pd.Timedelta(days=i)).strftime("%Y-%m-%d"))

            visits_today = sum(1 for r in rows if r.get("date") == today)
            visits_7 = sum(1 for r in rows if r.get("date") in last_7)
            visits_30 = sum(1 for r in rows if r.get("date") in last_30)

            st.metric("Hoy", visits_today)
            st.metric("Últimos 7 días", visits_7)
            st.metric("Últimos 30 días", visits_30)
            st.metric("Total", len(rows))


# ============================================================
# Ordenanzas / Expedientes (manual)
# ============================================================
def _download_pdf_from_url(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _summarize_pdf_for_manual_ref(pdf_bytes: bytes, filename: str) -> Dict[str, Any]:
    try:
        r = process_pdf(pdf_bytes, filename=filename)
    except Exception:
        r = {
            "summary_citizen": "",
            "text_preview": "",
            "topics": [],
            "items": [],
        }

    text_preview = (r.get("text_preview") or "").strip()
    items = ensure_items_schema(r.get("items") or [])

    if len(text_preview) < 200 and not items:
        return {"auto_summary": "", "auto_topics": [], "auto_items": [], "has_text": False}

    parts: List[str] = []
    if r.get("topics"):
        parts.append("Temas: " + ", ".join((r.get("topics") or [])[:6]))
    if items:
        parts.append("Puntos:")
        for it in items[:6]:
            qd = (it.get("que_dice") or "").strip()
            if qd:
                parts.append(f"- {qd}")
    else:
        lines = [ln.strip() for ln in text_preview.splitlines() if ln.strip()]
        if lines:
            parts.append("Extracto:")
            for ln in lines[:8]:
                parts.append(f"- {ln[:180]}")

    auto_summary = "\n".join(parts).strip()
    return {
        "auto_summary": auto_summary,
        "auto_topics": r.get("topics") or [],
        "auto_items": items[:50],
        "has_text": True,
    }


def render_manual_refs_tab(admin_ok: bool) -> None:
    st.subheader("📜 Ordenanzas / Expedientes (manual)")
    st.write(
        "Cargá una referencia para que cuando en un acta aparezca solo un número (ej: **05000-590/2012**) se entienda de qué trata."
    )
    st.markdown("👉 **Podés subir un PDF o pegar un link a PDF.** Si el PDF está escaneado (foto), tal vez no se extrae texto: en ese caso, guardás un **resumen manual corto**.")

    with st.form("manual_ref_form", clear_on_submit=False):
        ref_number = st.text_input(
            "Número (ordenanza / expediente / referencia)",
            placeholder="Ej: 05000-590/2012",
            key="ref_number",
        ).strip()

        title = st.text_input(
            "Título corto (opcional)",
            placeholder="Ej: Tarifas / Concesión / Habilitación…",
            key="ref_title",
        ).strip()

        fuente = st.radio("Fuente", ["Subir PDF", "Link a PDF"], horizontal=True, key="ref_source")

        pdf_bytes: Optional[bytes] = None
        source_url = ""

        if fuente == "Subir PDF":
            up = st.file_uploader("Subí el PDF", type=["pdf"], key="ref_pdf")
            if up is not None:
                pdf_bytes = up.read()
        else:
            source_url = st.text_input(
                "Pegá el link directo al PDF",
                placeholder="https://.../archivo.pdf",
                key="ref_url",
            ).strip()

        manual_summary = st.text_area(
            "Resumen manual (si el PDF es escaneado) — opcional",
            placeholder="- De qué trata\n- Qué autoriza/prohíbe\n- A quién afecta\n- Monto/plazo si aplica",
            height=150,
            key="ref_manual_summary",
        ).strip()

        consent = st.checkbox("Tengo permiso para compartir esto (o es de fuente pública).", value=True, key="ref_consent")
        submitted = st.form_submit_button("✅ Guardar")

    if submitted:
        if not consent:
            st.warning("Necesitás confirmar permiso / fuente pública.")
            return
        if not ref_number:
            st.warning("Falta el número de referencia.")
            return

        ref_key = normalize_text(ref_number)

        if fuente == "Link a PDF":
            if not source_url:
                st.warning("Pegá un link a PDF.")
                return
            try:
                with st.spinner("Bajando PDF desde el link…"):
                    pdf_bytes = _download_pdf_from_url(source_url)
            except Exception as e:
                st.error(f"No pude bajar el PDF: {e}")
                return

        auto_summary = ""
        has_text = False
        doc_id = None

        if pdf_bytes:
            doc_id = make_doc_id(pdf_bytes, filename=f"{ref_number}.pdf")
            try:
                with st.spinner("Intentando extraer texto y generar resumen…"):
                    out = _summarize_pdf_for_manual_ref(pdf_bytes, filename=f"{ref_number}.pdf")
                auto_summary = (out.get("auto_summary") or "").strip()
                has_text = bool(out.get("has_text"))
            except Exception:
                auto_summary = ""
                has_text = False

        final_summary = ""
        if auto_summary and len(auto_summary) > 80:
            final_summary = auto_summary
        elif manual_summary:
            final_summary = manual_summary

        if not final_summary:
            st.warning("No se pudo generar resumen automático y no cargaste resumen manual. Poné al menos 3 bullets.")
            return

        row = {
            "ref_number": ref_number,
            "ref_key": ref_key,
            "title": title,
            "summary": final_summary,
            "source_url": source_url,
            "doc_id": doc_id,
            "has_text_extracted": has_text,
            "created_at": datetime.utcnow().isoformat() + "Z",
        }

        if not HF_TOKEN:
            st.error("Falta HF_TOKEN para guardar esto en el dataset.")
            return

        try:
            _append_jsonl(DATASET_REPO_ID, MANUAL_REFS_PATH, row, commit_message=f"Add manual ref {ref_number}")
            st.success("Guardado ✅ (ya se puede usar para enriquecer ítems con ese número).")
            load_manual_refs.clear()
            manual_refs_map.clear()
        except Exception as e:
            st.error(f"No pude guardar en el dataset: {e}")

    with st.expander("📚 Ver registros cargados (últimos 50)"):
        rows = load_manual_refs()
        rows = sorted(rows, key=lambda r: (r.get("created_at") or ""), reverse=True)[:50]
        if not rows:
            st.info("Todavía no hay registros.")
        else:
            df = pd.DataFrame(rows)
            cols = [c for c in ["created_at", "ref_number", "title", "doc_id", "source_url", "has_text_extracted", "summary"] if c in df.columns]
            st.dataframe(df[cols], width="stretch", hide_index=True)


# ============================================================
# UI principal
# ============================================================
admin_ok = admin_gate()
log_visit_once_per_session()
render_admin_metrics(admin_ok)

tab_hist, tab_search, tab_refs, tab_sub, tab_upload = st.tabs(
    ["🗂️ Histórico", "🔎 Buscar en todo", "📜 Ordenanzas/Expedientes", "📩 Suscripción", "📄 Subir (admin)"]
)

# ------------------------------------------------------------
# HISTÓRICO
# ------------------------------------------------------------
with tab_hist:
    st.subheader("🗂️ Histórico guardado")

    preferred_doc_id = st.session_state.get("open_doc_id")

    if load_index is None:
        st.info("Histórico no disponible: tu hf_dataset_store.py no tiene load_index().")
    else:
        try:
            idx = load_index(DATASET_REPO_ID) or []
        except Exception as e:
            st.error(f"No pude leer el índice del dataset: {e}")
            idx = []

        if not idx:
            st.info("Todavía no hay documentos guardados.")
        else:
            options = build_history_options(idx)
            label_list = [lab for lab, _, _ in options if lab]

            chosen_index = 0
            if preferred_doc_id:
                for i, (_, did, _) in enumerate(options):
                    if did == preferred_doc_id:
                        chosen_index = i
                        break

            choice = st.selectbox("Elegí un documento", label_list, index=chosen_index, key="hist_doc_choice")
            chosen = next((x for x in options if x[0] == choice), None)

            if chosen:
                _, chosen_id, chosen_name = chosen

                cached = load_processed_json(DATASET_REPO_ID, chosen_id) or {}
                cached = normalize_cached_result(cached)

                stale = is_stale_cached(cached) or not (cached.get("items") or [])

                do_reprocess = False
                if admin_ok:
                    if stale:
                        st.warning("Documento en versión vieja o sin ítems.")
                    do_reprocess = st.button("♻️ Reprocesar y actualizar histórico")

                if do_reprocess and admin_ok:
                    with st.spinner("Bajando PDF raw del dataset…"):
                        pdf_bytes = download_raw_pdf(DATASET_REPO_ID, chosen_id)
                    with st.spinner("Reprocesando…"):
                        new_result = process_pdf(pdf_bytes, chosen_name or "documento.pdf")
                    with st.spinner("Guardando update…"):
                        upload_pdf_and_json(
                            repo_id=DATASET_REPO_ID,
                            doc_id=chosen_id,
                            pdf_bytes=pdf_bytes,
                            processed_json=new_result,
                            original_filename=chosen_name or "documento.pdf",
                            source_url=(cached.get("source_url") or ""),
                            sha256_digest=sha256_bytes(pdf_bytes),
                        )
                    st.success("Histórico actualizado ✅")
                    cached = normalize_cached_result(new_result)

                st.subheader("🧍 Resumen")
                st.write(cached.get("summary_citizen", ""))

                st.subheader("📊 Ítems")
                items = cached.get("items") or []
                if items:
                    df = pd.DataFrame(items)
                    df["key"] = df["key"].astype(str)
                    df["qué_dice"] = df["que_dice"]

                    if "impact_text" not in df.columns:
                        df["impact_text"] = df.get("title", "").apply(lambda x: infer_impact(str(x))["impact_text"])

                    df_show = df.copy()
                    cols = [c for c in ["key", "qué_dice", "impact_text", "decreto", "expediente", "topics"] if c in df_show.columns]

                    st.markdown("#### 🔎 Buscar en ítems")

                    with st.form("hist_items_search_form", clear_on_submit=False):
                        q = st.text_input("Buscar por palabra, nombre, expediente, decreto…", "", key="hist_search")
                        topic_values: List[str] = []
                        if "topics" in df_show.columns:
                            for xs in df_show["topics"].tolist():
                                if isinstance(xs, list):
                                    topic_values.extend(xs)
                        topic_values = sorted(set(topic_values))
                        topic_filter = st.multiselect("Filtrar por temas", topic_values, key="hist_topics")
                        run_filter = st.form_submit_button("Filtrar")

                    if run_filter:
                        if q:
                            qlow = q.lower()
                            mask = df_show[cols].astype(str).apply(lambda col: col.str.lower().str.contains(qlow, na=False)).any(axis=1)
                            df_show = df_show[mask]
                        if topic_filter and "topics" in df_show.columns:
                            df_show = df_show[df_show["topics"].apply(lambda xs: any(t in (xs or []) for t in topic_filter))]

                    st.dataframe(df_show[cols], width="stretch", hide_index=True)

                    st.download_button(
                        "⬇️ Descargar ítems (CSV)",
                        data=df_show[cols].to_csv(index=False).encode("utf-8"),
                        file_name=f"actas_sma_items_{chosen_id}.csv",
                        mime="text/csv",
                    )

                    st.markdown("### 🔍 Ver detalle de un ítem")
                    keys = [str(k) for k in df_show["key"].tolist()]
                    preferred_item_key = st.session_state.get("open_item_key")
                    default_idx = 0
                    if preferred_item_key and preferred_item_key in keys:
                        default_idx = keys.index(preferred_item_key)

                    if keys:
                        selected_key = st.selectbox("Elegí un ítem por número", keys, index=default_idx, key="detail_key_hist")
                        render_item_detail(df, selected_key, file_tag=f"{chosen_id}_hist")
                    else:
                        st.info("No hay ítems en el filtro actual.")
                else:
                    st.info("Este documento no tiene ítems detectados.")


# ------------------------------------------------------------
# BUSCADOR GLOBAL
# ------------------------------------------------------------
with tab_search:
    render_global_search_tab()

# ------------------------------------------------------------
# ORDENANZAS/EXPEDIENTES (manual)
# ------------------------------------------------------------
with tab_refs:
    render_manual_refs_tab(admin_ok)

# ------------------------------------------------------------
# SUSCRIPCIÓN
# ------------------------------------------------------------
with tab_sub:
    render_subscribe_tab(admin_ok)

# ------------------------------------------------------------
# UPLOAD (ADMIN)
# ------------------------------------------------------------
with tab_upload:
    if not admin_ok:
        st.info("Modo público: la carga de PDFs está deshabilitada. (Necesitas admin)")
    else:
        colA, colB = st.columns([2, 1])
        with colB:
            st.markdown("### Persistencia")
            persist = st.toggle("Guardar en Dataset", value=True)
            force_reprocess = st.toggle("Forzar reprocesado (ignorar cache)", value=False)
            st.caption(f"Dataset: {DATASET_REPO_ID}")

        with colA:
            uploaded = st.file_uploader("Subí un PDF (acta/orden del día)", type=["pdf"], key="upload_admin")
            if uploaded is None:
                st.info("Subí un PDF para procesar y guardar.")
            else:
                pdf_bytes = uploaded.read()
                doc_id = make_doc_id(pdf_bytes, uploaded.name)

                cached = load_processed_json(DATASET_REPO_ID, doc_id)
                stale = is_stale_cached(cached)

                if cached and (not force_reprocess) and (not stale):
                    result = normalize_cached_result(cached)
                    st.success("Cargando desde Dataset (cache OK). ✅")
                else:
                    with st.spinner("Procesando PDF…"):
                        result = process_pdf(pdf_bytes, uploaded.name)

                    if persist:
                        with st.spinner("Guardando en Dataset…"):
                            upload_pdf_and_json(
                                repo_id=DATASET_REPO_ID,
                                doc_id=doc_id,
                                pdf_bytes=pdf_bytes,
                                processed_json=result,
                                original_filename=uploaded.name,
                                source_url="",
                                sha256_digest=sha256_bytes(pdf_bytes),
                            )
                        st.success("Dataset actualizado ✅ (reemplaza versión vieja)")

                result = normalize_cached_result(result)

                st.subheader("🧍 Resumen")
                st.write(result.get("summary_citizen", ""))

                st.subheader("📊 Proyectos / Ítems")
                items = result.get("items") or []
                if items:
                    df = pd.DataFrame(items)
                    df["key"] = df["key"].astype(str)
                    df["qué_dice"] = df["que_dice"]
                    show_cols = [c for c in ["key", "qué_dice", "impact_text", "decreto", "expediente", "topics"] if c in df.columns]
                    st.dataframe(df[show_cols], width="stretch", hide_index=True)

                with st.expander("🧩 Texto extraído (preview)"):
                    st.text(result.get("text_preview", ""))
