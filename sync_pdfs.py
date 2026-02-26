from pathlib import Path
import requests
from bs4 import BeautifulSoup
import re
from urllib.parse import urljoin

RAW = Path("data/raw_pdfs")
RAW.mkdir(parents=True, exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0"}

# ✅ Poné acá las páginas que suelen listar PDFs
SOURCES = [
    "https://prensacd.cdsma.gob.ar/",  # ejemplo (ajustar a la página real donde estén listados)
]

def find_pdf_links(page_url: str) -> list[str]:
    r = requests.get(page_url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if ".pdf" in href.lower():
            links.append(urljoin(page_url, href))
    # dedupe
    out, seen = [], set()
    for u in links:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def safe_name(url: str) -> str:
    name = url.split("/")[-1].split("?")[0]
    name = re.sub(r"[^a-zA-Z0-9\.\-\_\(\)\s]", "", name).strip()
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name

def download_pdf(url: str) -> Path | None:
    name = safe_name(url)
    out = RAW / name
    if out.exists():
        return None
    r = requests.get(url, headers=HEADERS, timeout=60)
    if r.status_code != 200 or not r.content:
        return None
    if b"%PDF" not in r.content[:1024]:
        return None
    out.write_bytes(r.content)
    return out

def main():
    new_files = []
    for src in SOURCES:
        try:
            pdfs = find_pdf_links(src)
            for u in pdfs:
                p = download_pdf(u)
                if p:
                    new_files.append(p.name)
        except Exception as e:
            print(f"[WARN] {src}: {e}")

    print(f"✅ Nuevos: {len(new_files)}")
    for f in new_files[:50]:
        print(" -", f)

if __name__ == "__main__":
    main()