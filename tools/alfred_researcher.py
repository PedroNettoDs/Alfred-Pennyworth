"""
title: Alfred Researcher
description: Pesquisa um tema na web, sintetiza com IA e salva notas no vault. Use quando Pedro pedir para pesquisar ou estudar algo.
author: Alfred Pennyworth
version: 2.1.0
"""

import os
import requests
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        researcher_url: str = Field(
            default="http://172.17.0.1:7072",
            description="URL do Research Service no host",
        )
        token: str = Field(
            default="",
            description="Token Bearer — configure nas Valves após importar",
        )

    def __init__(self):
        self.valves = self.Valves()

    def research_topic(self, topic: str) -> str:
        """
        Pesquisa um tema na web, sintetiza os resultados com IA e salva
        notas estruturadas no vault do Obsidian (pasta research/{slug}/).
        Use sempre que Pedro pedir para pesquisar, estudar ou explorar um assunto.

        :param topic: O tema a ser pesquisado (ex: "Kubernetes operators", "RAG com LangChain")
        :return: Resultado da pesquisa com caminho das notas salvas no vault
        """
        try:
            resp = requests.post(
                f"{self.valves.researcher_url}/research",
                json={"topic": topic, "num_queries": 4, "results_per_query": 8},
                headers={"Authorization": f"Bearer {self.valves.token}"},
                timeout=300,
            )
            if resp.status_code == 200:
                data = resp.json()
                slug = data.get("matched_slug") or data.get("slug", "")
                if data.get("cached"):
                    # acerto de cache (exato ou semântico): a resposta não traz sources_found nem queries_used
                    if data.get("cache_type") == "semantic":
                        origem = (f"do cache (semântico: tema parecido \"{data.get('matched_topic', '?')}\", "
                                  f"similaridade {data.get('similarity', '?')})")
                    else:
                        origem = "do cache (exato, pesquisa dos últimos 7 dias)"
                    return (
                        f"Pesquisa {origem}.\n"
                        f"- Tema: {data.get('topic', topic)}\n"
                        f"- Vault: {data.get('vault_path', '?')}\n"
                        f"- Arquivos: {', '.join(data.get('files') or [])}\n\n"
                        f"{data.get('synthesis', '')}\n\n"
                        f"Use list_vault('research/{slug}') para ver o conteúdo salvo."
                    )
                return (
                    f"Pesquisa concluída.\n"
                    f"- Tema: {data.get('topic', topic)}\n"
                    f"- Fontes encontradas: {data.get('sources_found', '?')}\n"
                    f"- Vault: {data.get('vault_path', '?')}\n"
                    f"- Arquivos: {', '.join(data.get('files') or [])}\n"
                    f"- Consultas usadas: {'; '.join(data.get('queries_used') or [])}\n\n"
                    f"Use list_vault('research/{slug}') para ver o conteúdo salvo."
                )
            return f"Erro {resp.status_code}: {resp.text[:300]}"
        except requests.exceptions.Timeout:
            return "Timeout: a pesquisa demorou mais de 5 minutos. Tente um tema mais específico."
        except Exception as e:
            return f"Erro ao chamar o research service: {e}"
