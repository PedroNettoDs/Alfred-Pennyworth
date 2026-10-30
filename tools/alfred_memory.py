"""
title: Alfred Memory
description: Memória persistente entre sessões. Salva e recupera contexto chave-valor com expiração opcional. Armazena em VAULT_ALFRED/memory.yml.
author: Alfred Pennyworth
version: 1.1.0
"""

from pydantic import BaseModel, Field
from datetime import date, datetime
from pathlib import Path
from typing import Optional
import re
import yaml


class MemoriaIlegivel(Exception):
    """memory.yml existe mas não dá para ler: nada é gravado por cima."""


def _expiracao(valor) -> Optional[datetime]:
    """expires_at → datetime local sem fuso. Aceita ISO 8601 (com ou sem hora/fuso), DD/MM/AAAA e date/datetime
    do YAML. None/"" → None. Inválido → ValueError. Antes, com fuso a comparação aware × naive dava TypeError e
    a memória nunca expirava; DD/MM/AAAA idem."""
    if valor in (None, ""):
        return None
    if isinstance(valor, datetime):
        dt = valor
    elif isinstance(valor, date):
        dt = datetime(valor.year, valor.month, valor.day)
    else:
        txt = str(valor).strip()
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", txt)
        if m:
            dt = datetime(int(m[3]), int(m[2]), int(m[1]), int(m[4] or 0), int(m[5] or 0))
        else:
            dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


class Tools:
    class Valves(BaseModel):
        vault_path: str = Field(
            default="/vaults/alfred",
            description="Caminho do vault do Alfred",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _mem_file(self) -> Path:
        return Path(self.valves.vault_path) / "memory.yml"

    def _load(self) -> dict:
        """Lê memory.yml. Arquivo ilegível levanta MemoriaIlegivel — antes virava "sem memórias" e a próxima
        gravação apagava todas."""
        f = self._mem_file()
        if not f.exists():
            return {"memories": []}
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            linha = getattr(getattr(e, "problem_mark", None), "line", None)
            onde = f" (linha {linha + 1})" if linha is not None else ""
            raise MemoriaIlegivel(f"{f.name} está com erro de YAML{onde} — corrija o arquivo antes; nada foi alterado.")
        except OSError as e:
            raise MemoriaIlegivel(f"não consegui ler {f.name}: {e}")
        if data is None:
            return {"memories": []}
        mems = data.get("memories") if isinstance(data, dict) else None
        if not isinstance(data, dict) or not isinstance(mems or [], list) \
                or not all(isinstance(m, dict) for m in mems or []):
            raise MemoriaIlegivel(f"{f.name} não tem o formato esperado (memories: lista) — corrija o arquivo antes; "
                                  "nada foi alterado.")
        data["memories"] = mems or []
        return data

    def _save(self, data: dict):
        f = self._mem_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def _is_active(self, mem: dict) -> bool:
        try:
            exp = _expiracao(mem.get("expires_at"))
        except ValueError:
            return True   # data ilegível gravada à mão: não some sem aviso
        return exp is None or exp > datetime.now()

    def remember(self, key: str, value: str, expires_at: Optional[str] = None) -> str:
        """
        Salva uma informação na memória persistente.

        Use quando Pedro compartilhar contexto pessoal relevante que Alfred precisará nas próximas sessões:
        projetos em andamento, preferências, prazos importantes, configurações, decisões tomadas.

        Diferença do vault: memória é para contexto operacional rápido (o que Pedro está fazendo),
        vault é para documentos completos (pesquisas, sínteses).

        :param key: Chave identificadora, ex: "projeto_atual", "preferencia_editor", "prova_bd"
        :param value: Valor a memorizar — seja descritivo, ex: "Estudando RAG com ChromaDB para TCC"
        :param expires_at: Data/hora de expiração ISO 8601, ex: "2026-05-16" ou "2026-05-16T18:00" (aceita
            DD/MM/AAAA; opcional)
        :return: Confirmação
        """
        if expires_at:
            try:
                _expiracao(expires_at)
            except ValueError:
                return (f"Data de expiração inválida: {expires_at!r}. Use AAAA-MM-DD, AAAA-MM-DDTHH:MM ou DD/MM/AAAA. "
                        "Nada memorizado.")
            m = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*", str(expires_at))
            if m:   # grava em ISO (o daily_digest compara os 10 primeiros caracteres)
                expires_at = date(int(m[3]), int(m[2]), int(m[1])).isoformat()
        try:
            data = self._load()
        except MemoriaIlegivel as e:
            return f"Erro: {e} Nada memorizado."
        memories = data["memories"]

        # Atualiza se a chave já existe
        for mem in memories:
            if mem.get("key") == key:
                mem["value"] = value
                mem["expires_at"] = expires_at or None
                mem["updated_at"] = datetime.now().isoformat(timespec="minutes")
                self._save(data)
                return f"Memória '{key}' atualizada."

        # Nova entrada
        memories.append({
            "key": key,
            "value": value,
            "expires_at": expires_at or None,
            "created_at": datetime.now().isoformat(timespec="minutes"),
        })
        self._save(data)
        exp_str = f" (expira em {expires_at})" if expires_at else ""
        return f"Memorizado: '{key}' → \"{value}\"{exp_str}"

    def recall(self, key: str) -> str:
        """
        Recupera uma memória específica pelo nome da chave.

        :param key: Chave a recuperar
        :return: Valor memorizado ou aviso de que não existe
        """
        try:
            data = self._load()
        except MemoriaIlegivel as e:
            return f"Erro: {e}"
        for mem in data["memories"]:
            if mem.get("key") == key:
                if not self._is_active(mem):
                    return f"Memória '{key}' expirou em {mem.get('expires_at')}."
                return f"{key}: {mem['value']}"
        return f"Nenhuma memória encontrada para '{key}'."

    def recall_all(self) -> str:
        """
        Lista todas as memórias ativas (não expiradas).

        Use antes de responder perguntas sobre projetos em andamento, preferências
        ou contexto pessoal do Pedro. Fornece contexto rápido da sessão.

        :return: Lista de todas as memórias ativas ou aviso de que não há nenhuma
        """
        try:
            data = self._load()
        except MemoriaIlegivel as e:
            return f"Erro: {e}"
        active = [m for m in data["memories"] if self._is_active(m)]

        if not active:
            return "Nenhuma memória ativa registrada."

        lines = []
        for mem in active:
            exp = mem.get("expires_at")
            exp_str = f" (expira {exp})" if exp else ""
            lines.append(f"• **{mem['key']}**: {mem['value']}{exp_str}")

        return f"**Memórias ativas ({len(active)}):**\n" + "\n".join(lines)

    def forget(self, key: str) -> str:
        """
        Remove uma memória pelo nome da chave.

        Use quando Pedro pedir para esquecer algo ou quando a informação se tornar irrelevante.

        :param key: Chave a remover
        :return: Confirmação
        """
        try:
            data = self._load()
        except MemoriaIlegivel as e:
            return f"Erro: {e} Nada removido."
        before = len(data["memories"])
        data["memories"] = [m for m in data["memories"] if m.get("key") != key]
        if len(data["memories"]) < before:
            self._save(data)
            return f"Memória '{key}' removida."
        return f"Nenhuma memória encontrada para '{key}'."
