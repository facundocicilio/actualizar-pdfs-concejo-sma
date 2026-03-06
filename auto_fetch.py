import os
import re
import json
import time
import hashlib
import argparse
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from huggingface_hub import HfApi, hf_hub_download

HF_TOKEN = os.getenv("HF_TOKEN")
HF_REPO_ID = os.getenv("HF_REPO_ID")

# Permite override manual si querés
SOURCE_URL = os.getenv("SOURCE_URL", "").strip()

INDEX_PATH = "index/index.json"

UA = os.getenv(
    "HTTP_USER_AGENT",
    "Mozilla/5.0 (compatible; ObservatorioActasBot/1.1; +https://github.com/FacundoCicilio/Actualizar-PDFs-Concejo-SMA)"
)

DEBUG = os.getenv("DEBUG", "1").strip().lower() in {"1", "true", "yes", "y"}

session = requests.Session()
session.headers.update({"User-Agent": UA})


def log(msg: str) -> None:
    print(msg, flush=True)


def dbg(msg: str) -> None:
    if DEBUG:
        print(msg, flush=True)


def must_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise SystemExit(f"Falta variable de entorno {name}. Revisá Secrets y workflow.")
    return val


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def canonicalize_url(url: str) -> str:
    """
    Normaliza la URL para comparar mejor:
    - saca fragment (#...)
    - preserva query
    - mantiene esquema/host/path
    """
    parsed = urlparse(url.strip())
    parsed = parsed._replace(fragment="")
    return urlunparse(parsed)


def safe_filename_from_url(url: str, content_disposition: str | None = None) -> str:
    """
    Intenta sacar nombre desde Content-Disposition; si no, desde URL.
    """
    if content_disposition:
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition, re.I)
        if m:
            name = m.group(1).strip().strip('"').strip("'")
            name = os.path.basename(name)
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
            if not name.lower().endswith(".pdf"):
                name += ".pdf"
            return name

    path = urlparse(url).path
    name = os.path.basename(path) or "archivo.pdf"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def load_index() -> dict:
    try:
        file_path = hf_hub_download(
            repo_id=HF_REPO_ID,
            filename=INDEX_PATH,
            repo_type="dataset",
            token=HF_TOKEN,
        )
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("index.json no es un dict")
            data.setdefault("source", {})
            data.setdefault("files", {})
            data.setdefault("last_run", None)
            return data
    except Exception as e:
        dbg(f"⚠️ No pude cargar index previo: {e}")
        return {"source": {}, "files": {}, "last_run": None}


def save_index(index: dict) -> None:
    tmp_file = "tmp_index.json"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

    log(f"📝 Guardando index con {len(index.get('files', {}))} archivos.")
    api = HfApi()
    api.upload_file(
        path_or_fileobj=tmp_file,
        path_in_repo=INDEX_PATH,
        repo_id=HF_REPO_ID,
        repo_type="dataset",
        token=HF_TOKEN,
    )
    log("✅ Index subido correctamente.")


def is_probable_document_link(text: str, href: str) -> bool:
    """
    Detecta links probables a documentos.
    No depende solo del texto visible, porque puede venir mal escrito.
    """
    href_l = (href or "").lower()
    text_l = (text or "").lower()

    # Caso ideal: el href ya apunta a pdf
    if ".pdf" in href_l:
        return True

    # Pistas comunes de documentos/descargas
    href_keywords = [
        "upload",
        "uploads",
        "download",
        "descarga",
        "archivo",
        "file",
        "media",
        "wp-content",
        "document",
        "docs",
    ]
    text_keywords = [
        "orden",
        "día",
        "dia",
        "sesión",
        "sesion",
        "acta",
        "extraordinaria",
        "ordinaria",
        "documento",
        "descargar",
        "pdf",
    ]

    if any(k in href_l for k in href_keywords) and any(k in text_l for k in text_keywords):
        return True

    return False


def fetch_pdf_links(page_url: str) -> list[str]:
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

        dbg("----")
        dbg(f"[{i}] Texto: {text}")
        dbg(f"[{i}] Href raw: {raw_href}")
        dbg(f"[{i}] Href absoluto: {full}")

        if not raw_href:
            continue

        if raw_href.lower().startswith(("javascript:", "mailto:", "tel:")):
            continue

        if is_probable_document_link(text, raw_href):
            dbg(f"[{i}] ✅ Candidato a documento")
            candidates.append(full)

    # dedupe manteniendo orden
    seen = set()
    out = []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            out.append(u)

    log(f"📄 Documentos candidatos únicos encontrados: {len(out)}")
    for u in out:
        log(f"   - {u}")

    return out


def download_pdf(url: str) -> tuple[bytes, str, str | None]:
    """
    Descarga el archivo. Devuelve:
    - bytes
    - final_url (tras redirecciones)
    - content-disposition
    """
    r = session.get(url, timeout=60, allow_redirects=True)
    r.raise_for_status()

    final_url = canonicalize_url(r.url)
    content_type = (r.headers.get("Content-Type") or "").lower()
    content_disposition = r.headers.get("Content-Disposition")

    dbg(f"📨 Final URL: {final_url}")
    dbg(f"📨 Content-Type: {content_type}")
    dbg(f"📨 Content-Disposition: {content_disposition}")

    # Chequeo flexible
    looks_like_pdf = (
        ".pdf" in final_url.lower()
        or "application/pdf" in content_type
        or b"%PDF" in r.content[:1024]
    )

    if not looks_like_pdf:
        raise ValueError(
            f"El contenido descargado no parece PDF. URL final: {final_url} | Content-Type: {content_type}"
        )

    return r.content, final_url, content_disposition


def upload_pdf_to_hf(pdf_bytes: bytes, path_in_repo: str) -> None:
    api = HfApi()
    api.upload_file(
        path_or_fileobj=pdf_bytes,
        path_in_repo=path_in_repo,
        repo_id=HF_REPO_ID,
        repo_type="dataset",
        token=HF_TOKEN,
    )


def ensure_files_dict(index: dict) -> dict:
    files = index.get("files")
    if isinstance(files, dict):
        return files
    return {}


def normalize_known_urls(known: dict) -> dict:
    """
    Crea un mapa por URL canonizada.
    """
    out = {}
    for key, meta in known.items():
        ck = canonicalize_url(key)
        out[ck] = meta
    return out


def main():
    must_env("HF_TOKEN")
    must_env("HF_REPO_ID")

    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=None, help="Año a revisar (ej: 2026)")
    parser.add_argument("--source-url", type=str, default=None, help="URL manual para revisar")
    parser.add_argument("--sleep", type=float, default=0.0, help="Pausa entre descargas (segundos)")
    args = parser.parse_args()

    year = args.year or datetime.now().year
    page_url = (args.source_url or SOURCE_URL or f"https://prensacd.cdsma.gob.ar/ordenes-del-dia-{year}/").strip()

    log("🚀 Auto fetch iniciado")
    log(f"📦 Repo: {HF_REPO_ID}")
    log(f"📰 Fuente: {page_url}")
    log(f"🗓️ Año: {year}")
    log(f"🐞 DEBUG: {DEBUG}")

    index = load_index()
    index["last_run"] = utc_now_iso()
    index["source"] = {"page_url": page_url, "year": year}

    known_raw = ensure_files_dict(index)
    known = normalize_known_urls(known_raw)

    log("📚 URLs ya conocidas en index:")
    if known:
        for k in known.keys():
            log(f"   - {k}")
    else:
        log("   (ninguna)")

    pdf_links = fetch_pdf_links(page_url)

    # Comparación por URL canonizada
    new_links = [u for u in pdf_links if canonicalize_url(u) not in known]

    log(f"🆕 Nuevos detectados: {len(new_links)}")
    for u in new_links:
        log(f"   - {u}")

    if not new_links:
        save_index(index)
        log("✅ No hay PDFs nuevos. Index actualizado.")
        return

    uploaded_count = 0
    errors = []

    for url in new_links:
        try:
            log(f"⬇️ Descargando: {url}")
            data, final_url, content_disposition = download_pdf(url)
            digest = sha256_bytes(data)
            fname = safe_filename_from_url(final_url, content_disposition)
            path_in_repo = f"pdfs/ordenes_del_dia/{year}/{fname}"

            log(f"✅ Descargado OK. Bytes: {len(data)}")
            log(f"🔐 SHA256: {digest}")
            log(f"📁 Nombre inferido: {fname}")
            log(f"📦 Path en repo: {path_in_repo}")

            # Si la final_url ya existe en index, evita duplicado accidental por redirect distinto
            if final_url in known:
                log(f"⚠️ Saltado: la URL final ya existe en index: {final_url}")
                continue

            upload_pdf_to_hf(data, path_in_repo)
            log("✅ PDF subido a Hugging Face.")

            meta = {
                "url": final_url,
                "discovered_url": canonicalize_url(url),
                "sha256": digest,
                "path_in_repo": path_in_repo,
                "uploaded_at": utc_now_iso(),
                "filename": fname,
                "year": year,
            }

            # guardamos con URL final, no con URL cruda
            known[final_url] = meta
            uploaded_count += 1

            if args.sleep > 0:
                time.sleep(args.sleep)

        except Exception as e:
            err = f"❌ Error con {url}: {e}"
            log(err)
            errors.append(err)

    index["files"] = known
    if errors:
        index["last_errors"] = errors[-20:]

    save_index(index)
    log(f"🏁 Listo. Subidos {uploaded_count} PDFs nuevos. Index actualizado.")

    if errors:
        log("⚠️ Hubo errores en algunos archivos:")
        for e in errors:
            log(f"   - {e}")


if __name__ == "__main__":
    main()
