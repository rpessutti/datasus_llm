"""
Ponto de entrada da aplicação DATASUS LLM.

A aplicação foi modularizada; este arquivo apenas importa o FastAPI
criado em api/routes.py e o executa com uvicorn.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

from config.settings import PROJECT_DIR
from api.routes import app

load_dotenv(PROJECT_DIR / ".env")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("DATASUS_HOST", "0.0.0.0"), port=int(os.getenv("DATASUS_PORT", "8001")))

