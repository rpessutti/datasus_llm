"""Mostra a proxima etapa do refactor e atualiza o progresso.json.

Uso:
    python -u dev_tools/refactor_status.py
    python -u dev_tools/refactor_status.py --init _backups/simplificacao_cache_<ts>/progresso.json
    python -u dev_tools/refactor_status.py --done F0.1
    python -u dev_tools/refactor_status.py --doing F1.1
    python -u dev_tools/refactor_status.py --bloqueada F1.1 "mensagem curta"
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

PADRAO = "_backups/simplificacao_cache_*/progresso.json"
ORDEM = [
    "F0.1", "F0.2", "F0.3", "F0.4",
    "F1.1", "F1.2", "F1.3", "F1.4",
    "F2.1", "F2.2", "F2.3", "F2.4", "F2.5",
    "F3.1", "F3.2", "F3.3", "F3.4", "F3.5", "F3.6",
    "F4.1", "F4.2", "F4.3", "F4.4", "F4.5", "F4.6",
    "F5.1", "F5.2", "F5.3", "F5.4", "F5.5", "F5.6",
    "F6.1", "F7.1", "F7.2", "F7.3", "F7.4",
]


def achar() -> Path | None:
    """Retorna o progresso.json mais recente (ou None)."""
    candidatos = sorted(Path(".").glob(PADRAO))
    return candidatos[-1] if candidatos else None


def iniciar(path: Path) -> int:
    """Cria o progresso.json com todas as etapas pendentes (idempotente)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        print(f"progresso ja existe: {path}")
        return 0
    dados = {
        "versao": 1,
        "iniciado_em": datetime.now().isoformat(timespec="seconds"),
        "ts_backup": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "etapas": {etapa: "pending" for etapa in ORDEM},
        "wip_atual": None,
        "ultimo_erro": None,
    }
    path.write_text(json.dumps(dados, indent=2), encoding="utf-8")
    print(f"criado: {path}")
    return 0


def marcar(etapa: str, estado: str, erro: str | None = None) -> int:
    """Atualiza o estado de uma etapa no progresso.json mais recente."""
    if etapa not in ORDEM:
        print(f"etapa invalida: {etapa}")
        return 2
    path = achar()
    if path is None:
        print("progresso.json: AUSENTE -> rodar --init primeiro")
        return 1
    dados = json.loads(path.read_text(encoding="utf-8"))
    dados.setdefault("etapas", {})[etapa] = estado
    if erro is not None:
        dados["ultimo_erro"] = erro
    elif estado != "bloqueada":
        dados["ultimo_erro"] = None
    path.write_text(json.dumps(dados, indent=2), encoding="utf-8")
    print(f"{etapa}: {estado} ({path})")
    return 0


def resumo_new() -> None:
    """Imprime linhas e hash dos *_new.py (reusa check_new_state)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from check_new_state import estado  # type: ignore
    except Exception as exc:  # pragma: no cover - fallback defensivo
        print(f"_new: indisponivel ({exc})")
        return
    arquivos = sorted(
        p for p in Path(".").rglob("*_new.py")
        if not {"_backups", ".venv", "__pycache__"}.intersection(p.parts)
    )
    if not arquivos:
        print("_new: nenhum (F0.4 ainda nao executada)")
        return
    for path in arquivos:
        try:
            info = estado(path)
        except (SyntaxError, UnicodeDecodeError) as exc:
            print(f"  {path}: FALHA ({type(exc).__name__})")
            continue
        print(
            f"  {path} linhas={info['linhas']} metodos={info['metodos']} "
            f"sha={info['sha256']}"
        )


def main() -> int:
    """Imprime o estado, executa --init/--done/--doing/--bloqueada."""
    if sys.argv[1:2] == ["--init"]:
        destino = Path(sys.argv[2]) if len(sys.argv) > 2 \
            else Path("_backups/progresso.json")
        return iniciar(destino)
    for flag, estado in (("--done", "done"), ("--doing", "doing"),
                         ("--bloqueada", "bloqueada")):
        if sys.argv[1:2] == [flag]:
            if len(sys.argv) < 3:
                print(f"uso: {flag} <etapa> [mensagem]")
                return 2
            erro = sys.argv[3] if len(sys.argv) > 3 else None
            return marcar(sys.argv[2], estado, erro)

    path = achar()
    if path is None or not path.is_file():
        print("progresso.json: AUSENTE -> comecar pela F0.1 (backup)")
        print("criar com: python -u dev_tools/refactor_status.py --init <...>/progresso.json")
        return 0
    dados = json.loads(path.read_text(encoding="utf-8"))
    etapas = dados.get("etapas", {})
    print(f"progresso: {path}")
    print(f"iniciado_em: {dados.get('iniciado_em')} | wip: {dados.get('wip_atual')}")
    if dados.get("ultimo_erro"):
        print(f"ultimo_erro: {dados['ultimo_erro']}")
    for etapa in ORDEM:
        if etapas.get(etapa) != "done":
            print(f"  {etapa}: {etapas.get(etapa, 'pending')}")
    resumo_new()
    pendentes = [e for e in ORDEM if etapas.get(e) != "done"]
    if pendentes:
        print(f"\nproxima etapa: {pendentes[0]}")
        print("passo: PLANO_SIMPLIFICACAO_CACHE.md §7.1 + checkpoint §7.0.2")
    else:
        print("\ntodas as etapas concluidas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
