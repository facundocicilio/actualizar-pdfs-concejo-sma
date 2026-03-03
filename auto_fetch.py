import os
import json
from datetime import datetime
from huggingface_hub import HfApi, hf_hub_download

HF_TOKEN = os.getenv("HF_TOKEN")
HF_REPO_ID = os.getenv("HF_REPO_ID")

INDEX_PATH = "index/index.json"

def load_index():
    try:
        file_path = hf_hub_download(
            repo_id=HF_REPO_ID,
            filename=INDEX_PATH,
            repo_type="dataset",
            token=HF_TOKEN,
        )
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_index(index):
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

def main():
    print("Auto fetch iniciado")
    print("Repo:", HF_REPO_ID)

    index = load_index()

    # Solo prueba: agregamos timestamp
    index["last_run"] = datetime.utcnow().isoformat()

    save_index(index)

    print("Index actualizado correctamente")

if __name__ == "__main__":
    main()
