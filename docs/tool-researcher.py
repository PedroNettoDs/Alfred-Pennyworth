"""
Alfred Researcher Tool — Open WebUI

Importe este arquivo em Open WebUI:
  Workspace → Tools → "+" → cole o conteúdo → salve como "alfred_researcher"

Depois adicione o tool ao modelo Alfred Pennyworth:
  Workspace → Models → Alfred Pennyworth → Tools → habilite "alfred_researcher"
"""

import json
import os

import requests


class Tools:
    def __init__(self):
        self._base = "http://172.17.0.1:7071"

    def research_topic(self, topic: str) -> str:
        """
        Pesquisa um tema na web, sintetiza os resultados com IA e salva
        notas estruturadas no vault do Obsidian (pasta research/{slug}/).
        Use sempre que Pedro pedir para pesquisar, estudar ou explorar um assunto.

        :param topic: O tema a ser pesquisado (ex: "Kubernetes operators", "RAG com LangChain")
        :return: Resultado da pesquisa com caminho das notas salvas no vault
        """
        token = os.getenv("SHELL_EXECUTOR_TOKEN", "")
        try:
            resp = requests.post(
                f"{self._base}/research",
                json={"topic": topic, "num_queries": 4, "results_per_query": 8},
                headers={"Authorization": f"Bearer {token}"},
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
                    f"Use list_vault('research/{data[\"slug\"]}') para ver o conteúdo salvo."
                )
            return f"Erro {resp.status_code}: {resp.text[:300]}"
        except requests.exceptions.Timeout:
            return "Timeout: a pesquisa demorou mais de 5 minutos. Tente um tema mais específico."
        except Exception as e:
            return f"Erro ao chamar o research service: {e}"
