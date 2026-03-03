import os
import re
import json
import hashlib
import argparse
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from huggingface_hub import HfApi, hf_hub_download

HF_TOKEN = os.getenv("HF_TOKEN")
HF_REPO_ID = os.getenv("HF_REPO_ID")

# Permite override manual si querés (por ej. otra página)
SOURCE_URL = os.getenv("SOURCE_URL", "").strip()

INDEX_PATH = "index/index.json"

UA = os.getenv(
    "HTTP_USER_AGENT",
    "Mozilla/5.0 (compatible; ObservatorioActasBot/1.0; +https://github.com/FacundoCicilio/Actualizar-PDFs-Concejo-SMA)"
)

session = requests.Session()
session.headers.update({"User-Agent": UA})


def must_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise SystemExit(f"Falta variable de entorno {name}. Revisá Secrets y workflow.")
    return val


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_index() -> dict:
    try:
        file_path = hf_hub_download(
            repo_id=HF_REPO_ID,
            filename=INDEX_PATH,
            repo_type="dataset",
            token=HF_TOKEN,
        )
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"source": {}, "files": {}, "last_run": None}


def save_index(index: dict) -> None:
    tmp_file = "tmp_index.json"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

    api = HfApi()
    api.upload_file(
        path_or_fileobj=tmp_file,
        path_in_repo=INDEX_PATH,
        repo_id=HF_REPO_ID,
        repo_type="dataset",
        token=HF_TOKEN,
    )


def sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def safe_filename_from_url(url: str) -> str:
    path = urlparse(url).path
    name = path.split("/")[-1] or "archivo.pdf"
    # limpieza mínima
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def fetch_pdf_links(page_url: str) -> list[str]:
    r = session.get(page_url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if ".pdf" in href.lower():
            full = urljoin(page_url, href)
            links.append(full)

    # dedupe manteniendo orden
    seen = set()
    out = []
    for u in links:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def download_pdf(url: str) -> bytes:
    r = session.get(url, timeout=60)
    r.raise_for_status()
    # chequeo liviano
    if b"%PDF" not in r.content[:1024]:
        # a veces vienen con headers raros igual, pero si no parece PDF avisamos
        print(f"⚠️ Warning: el contenido no parece PDF en {url} (igual lo intento subir).")
    return r.content


def upload_pdf_to_hf(pdf_bytes: bytes, path_in_repo: str) -> None:
    api = HfApi()
    api.upload_file(
        path_or_fileobj=pdf_bytes,
        path_in_repo=path_in_repo,
        repo_id=HF_REPO_ID,
        repo_type="dataset",
        token=HF_TOKEN,
    )


def main():
    must_env("HF_TOKEN")
    must_env("HF_REPO_ID")

    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=None, help="Año a revisar (ej: 2026)")
    args = parser.parse_args()

    year = args.year or datetime.now().year

    # Si no te pasan SOURCE_URL, armamos URL estándar como la que nos diste
    page_url = SOURCE_URL or f"https://prensacd.cdsma.gob.ar/ordenes-del-dia-{year}/"

    print("Auto fetch iniciado")
    print("Repo:", HF_REPO_ID)
    print("Fuente:", page_url)

    index = load_index()
    index["last_run"] = utc_now_iso()
    index["source"] = {"page_url": page_url, "year": year}

    known = index.get("files", {})  # key: url, value: metadata

    pdf_links = fetch_pdf_links(page_url)
    print(f"Encontrados {len(pdf_links)} links PDF en la página.")

    new_links = [u for u in pdf_links if u not in known]
    print(f"Nuevos detectados: {len(new_links)}")

    if not new_links:
        save_index(index)
        print("No hay PDFs nuevos. Index actualizado.")
        return

    uploaded_count = 0

    for url in new_links:
        try:
            print(f"Descargando: {url}")
            data = download_pdf(url)
            digest = sha256_bytes(data)
            fname = safe_filename_from_url(url)

            # Guardamos PDFs en una carpeta ordenada
            path_in_repo = f"pdfs/ordenes_del_dia/{year}/{fname}"

            print(f"Subiendo a HF: {path_in_repo}")
            upload_pdf_to_hf(data, path_in_repo)

            known[url] = {
                "url": url,
                "sha256": digest,
                "path_in_repo": path_in_repo,
                "uploaded_at": utc_now_iso(),
                "filename": fname,
                "year": year,
            }
            uploaded_count += 1
        except Exception as e:
            print(f"❌ Error con {url}: {e}")

    index["files"] = known
    save_index(index)

    print(f"Listo. Subidos {uploaded_count} PDFs nuevos. Index actualizado.")


if __name__ == "__main__":
    main()
