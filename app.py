import streamlit as st
from pathlib import Path
import fitz
import re
import requests
from bs4 import BeautifulSoup

st.set_page_config(page_title="ACTAS SMA", layout="wide")
st.title("📄 ACTAS SMA — Resumen claro")

RAW = Path("data/raw_pdfs")
RAW.mkdir(parents=True, exist_ok=True)

pdfs = sorted(RAW.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
if not pdfs:
    st.info("Copiá PDFs en data/raw_pdfs y refrescá.")
    st.stop()

selected = st.sidebar.selectbox("Elegí un PDF", pdfs, format_func=lambda p: p.name)

# -----------------------------
# Utilidades base
# -----------------------------
def clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def find_date_es(text: str):
    m = re.search(r"\d{1,2}\s+de\s+\w+\s+de\s+20\d{2}", text, flags=re.IGNORECASE)
    return m.group(0) if m else None

def extract_text_local(pdf_path: Path) -> str:
    doc = fitz.open(pdf_path)
    parts = []
    for page in doc:
        parts.append(page.get_text("text") or "")
    return "\n".join(parts)

def session_title(text: str) -> str:
    clean = clean_spaces(text)
    fecha = find_date_es(clean) or "Fecha no detectada"
    tipo = "Sesión Ordinaria" if "ORDINARIA" in clean.upper() else "Sesión"
    nro = re.search(r"N[°º]\s*\d+", clean)
    nro = nro.group(0) if nro else ""
    return f"{tipo} {nro} — {fecha}".strip()

def high_level(text: str) -> list[str]:
    u = text.upper()
    bullets = []
    if "PERÍODO ORDINARIO" in u or "PERIODO ORDINARIO" in u:
        bullets.append("Inicio del período ordinario.")
    if "BOLETÍN" in u or "BOLETIN" in u:
        bullets.append("Ingreso/tratamiento de Boletines de Asuntos Entrados.")
    if "DICTAMEN" in u:
        bullets.append("Ingreso de dictámenes vinculados a expedientes y decretos.")
    if "AD REFER" in u:
        bullets.append("Varios decretos tratados “ad referéndum”.")
    if "TARIF" in u:
        bullets.append("Cambios/actualizaciones tarifarias o valores asociados.")
    if "AGUA" in u:
        bullets.append("Puntos vinculados a abastecimiento de agua.")
    if "LICENCIA" in u or "HABILIT" in u:
        bullets.append("Puntos vinculados a licencias/habilitaciones comerciales.")
    if not bullets:
        bullets = ["Puntos administrativos del orden del día."]
    return bullets[:6]

def categorize(tema: str) -> str:
    u = tema.upper()
    if "AGUA" in u:
        return "Agua"
    if "TARIF" in u or "TARIFARIA" in u or "ORDENANZA" in u:
        return "Tarifaria / normativa"
    if "LICENCIA" in u or "HABILIT" in u or "COMERCIAL" in u or "CONFITER" in u:
        return "Comercio"
    if "DONACIÓN" in u or "DONACION" in u:
        return "Donación"
    if "JUNTA VECINAL" in u or "BANCA" in u:
        return "Participación vecinal"
    if "DESIGNACIÓN" in u or "DESIGNACION" in u or "COMISION" in u:
        return "Institucional"
    if "CONTRAT" in u or "LICIT" in u:
        return "Contratación"
    if "DECRETO" in u:
        return "Decretos"
    return "General"

def normalize_topic_human(tema: str, max_len: int = 170) -> str:
    t = clean_spaces(tema)
    t = re.sub(r"^Dictamen\s+s/\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+(sobre|para la|de la|de uso de la)\s*$", "", t, flags=re.IGNORECASE).strip()
    if len(t) > max_len:
        t = t[:max_len].rstrip() + "…"
    return t

# -----------------------------
# Parser de Tema: (merge)
# -----------------------------
STOP_PATTERNS = [
    r"^\d+\.\s",
    r"^EE-\d",
    r"^Miembro Informante",
    r"^\d{1,2}/\d{1,2}/20\d{2}",
]

def is_stop_line(ln: str) -> bool:
    if not ln:
        return True
    for pat in STOP_PATTERNS:
        if re.search(pat, ln, flags=re.IGNORECASE):
            return True
    return False

def extract_topics_merged(text: str) -> list[dict]:
    raw = [clean_spaces(ln) for ln in text.splitlines()]
    raw = [ln for ln in raw if ln]

    topics = []
    i = 0
    while i < len(raw):
        ln = raw[i]

        if "TEMA:" in ln.upper():
            # capturar referencia tipo 95/2026 si viene en la misma línea
            ref = None
            mref = re.search(r"\b(\d{1,4}\/20\d{2}|\d{1,4}\/\d{2})\b", ln)
            if mref:
                ref = mref.group(1)

            base = ln.split("Tema:", 1)[-1].strip() if "Tema:" in ln else ln
            parts = [base]

            j = i + 1
            added = 0
            while j < len(raw) and added < 6:
                nxt = raw[j]
                if "TEMA:" in nxt.upper():
                    break
                if is_stop_line(nxt):
                    break
                if len(nxt) <= 2:
                    break
                parts.append(nxt)
                added += 1
                j += 1

            tema_full = clean_spaces(" ".join(parts))

            dec = re.search(r"DECRETO\s*N[°º]?\s*([0-9]{1,4}\/\d{2})", tema_full, flags=re.IGNORECASE)
            ordz = re.search(r"ORDENANZA\s*N[°º]?\s*([0-9]{3,6}\/\d{2})", tema_full, flags=re.IGNORECASE)

            topics.append({
                "ref": ref,
                "tema": tema_full,
                "tema_corto": normalize_topic_human(tema_full),
                "categoria": categorize(tema_full),
                "decreto": dec.group(1) if dec else None,
                "ordenanza": ordz.group(1) if ordz else None,
                "contexto": [],
                "boletin_url": None,
                "que_cambia": "",
            })

            i = j
            continue

        i += 1

    seen = set()
    uniq = []
    for t in topics:
        key = t["tema"].lower()
        if key not in seen:
            seen.add(key)
            uniq.append(t)
    return uniq

def add_context(text: str, topics: list[dict], window: int = 2):
    lines = [clean_spaces(ln) for ln in text.splitlines() if clean_spaces(ln)]
    for t in topics:
        target = t.get("decreto") or t.get("ref") or t["tema_corto"][:35]
        hit = None

        for idx, ln in enumerate(lines):
            low = ln.lower()
            if "tema:" in low and target and str(target).lower() in low:
                hit = idx
                break
        if hit is None and target:
            for idx, ln in enumerate(lines):
                if str(target).lower() in ln.lower():
                    hit = idx
                    break

        if hit is None:
            t["contexto"] = []
            continue

        start = max(0, hit - window)
        end = min(len(lines), hit + window + 1)
        block = lines[start:end]

        noise = ("Miembro Informante:", "Página", "Page")
        filtered = []
        for ln in block:
            if any(ln.startswith(p) for p in noise):
                continue
            filtered.append(ln)

        t["contexto"] = filtered[:5]

# -----------------------------
# Respaldo SMA boletín: probar meses (silencioso)
# -----------------------------
@st.cache_data(show_spinner=False)
def try_fetch_boletin_text_for_decreto(decreto: str):
    m = re.match(r"0*([0-9]{1,4})\/(\d{2})", decreto)
    if not m:
        return None, None
    num = str(int(m.group(1)))
    yy = m.group(2)
    yyyy = "20" + yy

    for month in range(1, 13):
        mm = f"{month:02d}"
        url = f"https://boletinoficial.sma.gob.ar/wp-content/uploads/{yyyy}/{mm}/{num}-{yy}.pdf"
        try:
            r = requests.get(url, timeout=15)
            if r.status_code != 200 or not r.content:
                continue
            doc = fitz.open(stream=r.content, filetype="pdf")
            parts = []
            for page in doc:
                t = page.get_text("text") or ""
                t = re.sub(r"[ \t]+", " ", t)
                t = re.sub(r"\n{3,}", "\n\n", t).strip()
                if t:
                    parts.append(t)
            bt = "\n\n".join(parts).strip()
            if bt:
                return url, bt
        except Exception:
            continue
    return None, None

def parse_quecambia_1line(text: str) -> str:
    clean = clean_spaces(text)
    vp = re.search(r"VALOR\s+DEL\s+PUNTO.*?\$\s*([0-9\.\,]+)", clean, flags=re.IGNORECASE)
    if vp:
        return f"Actualiza el valor del punto a ${vp.group(1)} (menciona IPC)."
    if "TARIF" in clean.upper():
        return "Incluye cambios tarifarios/normativos (ver respaldo)."
    if "AGUA" in clean.upper():
        return "Incluye medidas/contrataciones vinculadas a agua (ver respaldo)."
    return ""

# -----------------------------
# Resumir URL cualquiera (PDF o página) + detección tipo
# -----------------------------
@st.cache_data(show_spinner=False)
def fetch_url(url: str) -> tuple[str, bytes, str]:
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=60, allow_redirects=True)
    r.raise_for_status()
    ctype = (r.headers.get("Content-Type") or "").lower()
    return ctype, r.content, r.url

def pdf_text_from_bytes(pdf_bytes: bytes) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    parts = []
    for page in doc:
        t = page.get_text("text") or ""
        t = re.sub(r"[ \t]+", " ", t)
        t = re.sub(r"\n{3,}", "\n\n", t).strip()
        if t:
            parts.append(t)
    return "\n\n".join(parts).strip()

def extract_visible_text_from_html(html_bytes: bytes) -> str:
    soup = BeautifulSoup(html_bytes, "lxml")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [clean_spaces(x) for x in text.splitlines()]
    lines = [x for x in lines if x and len(x) > 2]
    joined = "\n".join(lines)
    return joined[:200000]

def find_pdf_links_in_html(html_bytes: bytes, base_url: str) -> list[str]:
    soup = BeautifulSoup(html_bytes, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if ".pdf" in href.lower():
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                m = re.match(r"^(https?://[^/]+)", base_url)
                if m:
                    href = m.group(1) + href
            elif not href.startswith("http"):
                href = base_url.rstrip("/") + "/" + href.lstrip("/")
            links.append(href)
    out, seen = [], set()
    for u in links:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def summarize_normative(text: str) -> dict:
    clean = clean_spaces(text)
    upper = clean.upper()

    tipo = None
    numero = None
    for label in ["ORDENANZA", "LEY", "DECRETO", "RESOLUCIÓN", "RESOLUCION"]:
        if label in upper:
            tipo = "RESOLUCIÓN" if label in ["RESOLUCIÓN", "RESOLUCION"] else label
            break

    if tipo:
        m = re.search(rf"{tipo}\s*N[°º]?\s*([0-9]{{1,6}}\/\d{{2}}|[0-9]{{1,6}})", clean, flags=re.IGNORECASE)
        if m:
            numero = m.group(1)

    fecha = find_date_es(clean)

    articles = []
    art_iter = re.finditer(r"(ART[ÍI]CULO\s+\d+\s*[°º]?\s*[-–:]?)", text, flags=re.IGNORECASE)
    art_positions = [m.start() for m in art_iter][:8]
    if art_positions:
        for idx, start in enumerate(art_positions):
            end = art_positions[idx + 1] if idx + 1 < len(art_positions) else min(len(text), start + 1800)
            chunk = clean_spaces(text[start:end])
            first_sentence = re.split(r"(?<=[\.\;])\s+", chunk)[0]
            first_sentence = clean_spaces(first_sentence)
            if len(first_sentence) > 220:
                first_sentence = first_sentence[:220].rstrip() + "…"
            articles.append(first_sentence)

    amounts = re.findall(r"\$\s*[0-9\.\,]+", clean)
    amounts = list(dict.fromkeys(amounts))[:6]

    titulo = "Documento"
    if tipo and numero:
        titulo = f"{tipo} {numero}"
    elif tipo:
        titulo = tipo

    return {"titulo": titulo, "fecha": fecha, "articulos": articles, "montos": amounts}

def detect_doc_type(text: str) -> str:
    u = clean_spaces(text).upper()
    if ("ORDEN DEL DÍA" in u) or ("ORDEN DEL DIA" in u) or ("ASUNTOS ENTRADOS" in u and "TEMA:" in u):
        return "orden_dia"
    if ("ACTA" in u and "SESI" in u) or ("SESIÓN" in u) or ("SESION" in u):
        if "TEMA:" in u:
            return "orden_dia"
        return "acta"
    if "LEY" in u and "ART" in u:
        return "normativo"
    if "ORDENANZA" in u or "DECRETO" in u or "RESOLUCIÓN" in u or "RESOLUCION" in u:
        return "normativo"
    return "desconocido"

def summarize_orden_dia_text(text: str) -> dict:
    titulo = session_title(text)
    bullets = high_level(text)
    topics = extract_topics_merged(text)
    temas = [t["tema_corto"] for t in topics][:12]
    return {"titulo": titulo, "bullets": bullets, "temas": temas, "n_temas": len(topics)}

# -----------------------------
# RUN
# -----------------------------
text = extract_text_local(selected)
if not text.strip():
    st.warning("Este PDF parece escaneado (imagen). Para eso necesitamos OCR.")
    st.stop()

st.subheader(session_title(text))

# Resumir por link (PDF o página) con detección de tipo
with st.expander("➕ Resumir por link (PDF o página web)", expanded=False):
    st.markdown(
        "**Pegá cualquier link**: PDF directo o página web (Boletín / municipal / etc). "
        "Detecto si es **Orden del Día/Acta** o **Normativa (ordenanza/ley/decreto)** y lo resumo acorde."
    )
    url = st.text_input(
        "URL",
        placeholder="Ej: https://www.boletinoficial.gob.ar/detalleAviso/primera/324095/20250416"
    )

    if st.button("Resumir link"):
        if not url or not url.strip():
            st.error("Pegá una URL.")
        else:
            try:
                with st.spinner("Abriendo link..."):
                    ctype, content, final_url = fetch_url(url.strip())

                used_pdf = False
                extracted_text = ""

                if ("application/pdf" in ctype) or final_url.lower().endswith(".pdf") or ".pdf" in final_url.lower():
                    extracted_text = pdf_text_from_bytes(content)
                    used_pdf = True
                else:
                    pdf_links = find_pdf_links_in_html(content, final_url)
                    if pdf_links:
                        with st.spinner("Encontré PDF dentro de la página, lo estoy leyendo..."):
                            ctype2, content2, final_pdf_url = fetch_url(pdf_links[0])
                            if ("pdf" in ctype2) or final_pdf_url.lower().endswith(".pdf"):
                                extracted_text = pdf_text_from_bytes(content2)
                                used_pdf = True
                            else:
                                extracted_text = extract_visible_text_from_html(content)
                    else:
                        extracted_text = extract_visible_text_from_html(content)

                if not extracted_text.strip():
                    st.warning("No pude extraer texto (puede estar escaneado o ser una página muy dinámica).")
                else:
                    doc_type = detect_doc_type(extracted_text)

                    if doc_type in ["orden_dia", "acta"]:
                        st.success("Tipo detectado: Orden del Día / Acta")
                        s = summarize_orden_dia_text(extracted_text)

                        st.subheader(s["titulo"])
                        st.caption(f"Temas detectados: {s['n_temas']}")

                        st.markdown("### ✅ Qué pasó")
                        for b in s["bullets"]:
                            st.write("• " + b)

                        st.markdown("### 📌 Temas")
                        for tt in s["temas"]:
                            st.write("• " + tt)

                    elif doc_type == "normativo":
                        st.success("Tipo detectado: Normativa (ordenanza/ley/decreto)")
                        s = summarize_normative(extracted_text)

                        st.subheader(s["titulo"])
                        if s["fecha"]:
                            st.caption(f"Fecha detectada: {s['fecha']}")

                        st.markdown("### ✅ En 1 frase")
                        line = []
                        if s["montos"]:
                            line.append(f"Montos: {', '.join(s['montos'][:2])}")
                        st.write("• " + (" — ".join(line) if line else "Documento normativo (ver puntos clave abajo)."))

                        st.markdown("### 📌 Puntos clave")
                        if s["articulos"]:
                            for a in s["articulos"][:6]:
                                st.write("• " + a)
                        else:
                            st.write("• No detecté artículos en formato típico.")

                        if s["montos"]:
                            st.markdown("**Montos:** " + ", ".join(s["montos"]))

                    else:
                        st.info("Tipo detectado: no claro (resumen básico)")
                        lines = [clean_spaces(x) for x in extracted_text.splitlines()]
                        lines = [x for x in lines if x and len(x) > 10][:12]
                        for ln in lines:
                            st.write("• " + ln)

                    st.caption("Fuente usada: " + ("PDF" if used_pdf else "Texto visible de la página"))

                    with st.expander("Ver texto extraído (para verificar)", expanded=False):
                        st.text(extracted_text[:120000])

            except Exception as e:
                st.error(f"No pude procesar esa URL: {e}")

# Actas / Orden del día (PDF local seleccionado)
st.markdown("### ✅ Qué pasó")
for b in high_level(text):
    st.write("• " + b)

topics = extract_topics_merged(text)
add_context(text, topics, window=2)

# respaldo SMA SOLO si realmente hay decreto detectado
for t in topics:
    if t.get("decreto"):
        url_b, bt = try_fetch_boletin_text_for_decreto(t["decreto"])
        if url_b and bt:
            t["boletin_url"] = url_b
            t["que_cambia"] = parse_quecambia_1line(bt)

st.markdown("### 📌 Temas")

header = st.columns([0.6, 1.3, 5.2, 1.5, 1.1, 2.6])
header[0].markdown("**#**")
header[1].markdown("**Categoría**")
header[2].markdown("**Tema**")
header[3].markdown("**N° (Expte/Decreto)**")
header[4].markdown("**Respaldo**")
header[5].markdown("**Qué cambia**")

for i, t in enumerate(topics, start=1):
    cols = st.columns([0.6, 1.3, 5.2, 1.5, 1.1, 2.6])
    cols[0].write(str(i))
    cols[1].write(t["categoria"])
    cols[2].write(t["tema_corto"])

    # si hay decreto, mostrar decreto; si no, mostrar referencia tipo 95/2026
    if t.get("decreto"):
        cols[3].write(t["decreto"])
    elif t.get("ref"):
        cols[3].write(t["ref"])

    if t.get("boletin_url"):
        cols[4].write("✅")

    if t.get("que_cambia"):
        cols[5].write(t["que_cambia"])

st.markdown("---")
st.markdown("### 🔎 Detalles (abrí solo lo que te interese)")

for i, t in enumerate(topics, start=1):
    with st.expander(f"{i}) {t['tema_corto']}", expanded=False):
        st.markdown("**Tema completo:**")
        st.write(t["tema"])

        if t.get("ref") and not t.get("decreto"):
            st.caption(f"Referencia detectada: {t['ref']}")

        if t["contexto"]:
            st.markdown("**Extracto del Orden del Día:**")
            for ln in t["contexto"]:
                st.write("• " + ln)

        if t.get("boletin_url"):
            st.markdown("**Respaldo (Boletín Oficial SMA):**")
            st.write(t["boletin_url"])
            if t.get("que_cambia"):
                st.markdown("**Qué cambia (1 línea):**")
                st.write("• " + t["que_cambia"])

with st.expander("📚 Ver texto completo del PDF seleccionado", expanded=False):
    st.text(text)