"""
title: Alfred Researcher
description: Pesquisa um tema na web, sintetiza com IA e salva notas no vault. Use quando Pedro pedir para pesquisar ou estudar algo.
author: Alfred Pennyworth
version: 2.0.0
"""

import os
import requests
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        researcher_url: str = Field(
            default="http://172.17.0.1:7071",
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
                return (
                    f"Pesquisa concluída.\n"
                    f"- Tema: {data['topic']}\n"
                    f"- Fontes encontradas: {data['sources_found']}\n"
                    f"- Vault: {data['vault_path']}\n"
                    f"- Arquivos: {', '.join(data['files'])}\n"
                    f"- Consultas usadas: {'; '.join(data['queries_used'])}\n\n"
                    f"Use list_vault('research/{data['slug']}') para ver o conteúdo salvo."
                )
            return f"Erro {resp.status_code}: {resp.text[:300]}"
        except requests.exceptions.Timeout:
            return "Timeout: a pesquisa demorou mais de 5 minutos. Tente um tema mais específico."
        except Exception as e:
            return f"Erro ao chamar o research service: {e}"
