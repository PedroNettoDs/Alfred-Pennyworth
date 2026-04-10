"""
title: Alfred Memory
description: Memória persistente entre sessões. Salva e recupera contexto chave-valor com expiração opcional. Armazena em VAULT_ALFRED/memory.yml.
author: Alfred Pennyworth
version: 1.0.0
"""

from pydantic import BaseModel, Field
from datetime import datetime
from pathlib import Path
from typing import Optional
import yaml


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
        f = self._mem_file()
        if not f.exists():
            return {"memories": []}
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and "memories" in data else {"memories": []}
        except Exception:
            return {"memories": []}

    def _save(self, data: dict):
        f = self._mem_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def _is_active(self, mem: dict) -> bool:
        exp = mem.get("expires_at")
        if not exp:
            return True
        try:
            return datetime.fromisoformat(exp) > datetime.now()
        except Exception:
            return True

    def remember(self, key: str, value: str, expires_at: Optional[str] = None) -> str:
        """
        Salva uma informação na memória persistente.

        Use quando Pedro compartilhar contexto pessoal relevante que Alfred precisará nas próximas sessões:
        projetos em andamento, preferências, prazos importantes, configurações, decisões tomadas.

        Diferença do vault: memória é para contexto operacional rápido (o que Pedro está fazendo),
        vault é para documentos completos (pesquisas, sínteses).

        :param key: Chave identificadora, ex: "projeto_atual", "preferencia_editor", "prova_bd"
        :param value: Valor a memorizar — seja descritivo, ex: "Estudando RAG com ChromaDB para TCC"
        :param expires_at: Data/hora de expiração ISO 8601, ex: "2026-05-16" (opcional)
        :return: Confirmação
        """
        data = self._load()
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
        data = self._load()
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
        data = self._load()
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
        data = self._load()
        before = len(data["memories"])
        data["memories"] = [m for m in data["memories"] if m.get("key") != key]
        if len(data["memories"]) < before:
            self._save(data)
            return f"Memória '{key}' removida."
        return f"Nenhuma memória encontrada para '{key}'."
