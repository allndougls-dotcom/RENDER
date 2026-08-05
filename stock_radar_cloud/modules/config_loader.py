"""Carga la configuracion desde .env o variables de entorno."""

import os
from pathlib import Path


def cargar_config() -> dict:
    project_root = Path(__file__).parent.parent

    env_paths = [
        project_root / "config" / ".env",
        project_root / ".env",
    ]

    for env_path in env_paths:
        if env_path.exists():
            _load_dotenv(env_path)
            print(f"  Config cargada desde: {env_path}")
            break
    else:
        print("  No se encontro .env — usando valores por defecto")
        print("  Copia config/.env.example a config/.env\n")

    fmp_key = os.getenv("FMP_API_KEY", "").strip()
    use_fmp = bool(fmp_key and fmp_key not in ("TU_API_KEY_AQUI", ""))

    return {
        "FMP_API_KEY":   fmp_key,
        "USE_FMP":       use_fmp,
        "MAX_EMPRESAS":  int(os.getenv("MAX_EMPRESAS", "503")),
        "YEARS_HISTORY": int(os.getenv("YEARS_HISTORY", "2")),
    }


def _load_dotenv(path: Path):
    """Parser robusto de .env — soporta UTF-8, UTF-8 BOM y latin-1."""
    for encoding in ('utf-8-sig', 'utf-8', 'latin-1'):
        try:
            with open(path, encoding=encoding) as f:
                lines = f.readlines()
            break
        except UnicodeDecodeError:
            continue
    else:
        return

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
