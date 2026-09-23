"""Testa o lifespan da aplicação para verificar se ela inicializa corretamente."""
import asyncio
import sys
import os
from pathlib import Path

# Adiciona o diretório raiz ao path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.chdir(ROOT)

from api.routes import app, lifespan

async def test():
    print("=== TESTANDO LIFESPAN DA APLICAÇÃO ===")
    try:
        # Entra no lifespan
        async with lifespan(app):
            print("✓ Lifespan iniciado com sucesso!")
            print("✓ A aplicação está no ar!")
            # Testa o status
            from api.routes import cache
            status = cache.status()
            print(f"✓ Status do cache: {status}")
    except Exception as e:
        print(f"✗ ERRO no lifespan: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    print("✓ Lifespan finalizado corretamente")
    return 0

if __name__ == "__main__":
    exit_code = asyncio.run(test())
    sys.exit(exit_code)