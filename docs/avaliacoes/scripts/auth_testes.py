#!/usr/bin/env python3
"""
Testes de contrato da autenticação do Alfred — TestClient em processo, token FICTÍCIO, vault temporário.

Nada aqui abre socket nem fala com os serviços em produção (7071–7074): o app FastAPI é chamado dentro do
próprio processo, com o mesmo harness de services/inbox/tests/test_painel.py (Ollama → 127.0.0.1:9, WhatsApp
simulado, token "token-de-teste"). As linhas "MEDIDA:" são o resultado a copiar para o relatório.

    cd services/inbox
    venv/bin/python ../../docs/avaliacoes/scripts/auth_testes.py -v

(o venv do inbox fica no checkout principal; num worktree use o caminho dele)
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[3]
INBOX = RAIZ / "services" / "inbox"
sys.path.insert(0, str(INBOX))
sys.path.insert(0, str(INBOX.parent))

from tests.test_painel import Base, TOKEN, BASE, main, payload  # noqa: E402  (define o ambiente de teste)
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CONTAINER = "172.17.0.2"      # IP de um container na bridge: o cliente "de fora" mais provável
LIVRES = {"/vivo", "/health"}  # liveness/health: sem credencial por desenho


def medida(chave, valor):
    print(f"MEDIDA: {chave} = {valor}")


class TestContrato(Base):
    def _rotas_inbox(self):
        return [r for r in main.app.routes if isinstance(r, APIRoute) and not r.path.startswith("/painel")]

    def test_toda_rota_exige_credencial_menos_as_livres(self):
        """Sem Authorization, a partir de IP de container: o que ainda responde 2xx?"""
        cli = self.cliente(CONTAINER)
        contagem, serve = {}, []
        total = 0
        for rota in self._rotas_inbox():
            caminho = rota.path.replace("{evento}", "x")
            for p in ("pid", "lid", "aid", "evento"):
                caminho = caminho.replace("{" + p + "}", "x")
            for metodo in sorted(rota.methods - {"HEAD", "OPTIONS"}):
                total += 1
                r = cli.request(metodo, caminho, json={} if metodo == "POST" else None)
                contagem[r.status_code] = contagem.get(r.status_code, 0) + 1
                if 200 <= r.status_code < 300:
                    serve.append(f"{metodo} {rota.path}")
        medida("rotas_inbox_testadas", total)
        medida("status_sem_credencial", json.dumps(contagem, sort_keys=True))
        medida("rotas_que_respondem_2xx_sem_credencial", serve)
        self.assertEqual({s.split(" ", 1)[1] for s in serve}, LIVRES)

    def test_health_sem_credencial_e_o_que_ele_mostra(self):
        with mock.patch.object(main, "find_thunderbird_profile", lambda: None):
            r = self.cliente(CONTAINER).get("/health")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        medida("health_campos_topo", sorted(d))
        medida("health_whatsapp_campos", sorted(d.get("whatsapp") or {}))
        medida("health_db_campos", sorted(d.get("db") or {}))
        medida("health_tem_numero_do_dono", bool((d.get("whatsapp") or {}).get("numero_pedro")))

    def test_ics_so_olha_o_ip_do_cliente(self):
        loop = self.cliente("127.0.0.1")
        medida("ics_loopback_host_local", loop.get("/agenda/alfred.ics").status_code)
        medida("ics_loopback_host_estranho", loop.get("/agenda/alfred.ics", headers={"Host": "evil.example"}).status_code)
        medida("ics_loopback_com_x_forwarded_for",
               loop.get("/agenda/alfred.ics", headers={"X-Forwarded-For": "203.0.113.9"}).status_code)
        medida("ics_container", self.cliente(CONTAINER).get("/agenda/alfred.ics").status_code)
        # o Painel, no mesmo cenário, recusa Host estranho (TestRede.test_host_estranho_recusado)
        medida("painel_loopback_host_estranho",
               loop.get("/painel/login", headers={"Host": "evil.example"}).status_code)

    def test_token_vazio_falha_fechado(self):
        cli = self.cliente(CONTAINER)
        with mock.patch.object(main, "TOKEN", ""):
            for h in ("Bearer ", "Bearer", ""):
                r = cli.get("/pendencias", headers={"Authorization": h})
                self.assertEqual(r.status_code, 401, h)
        medida("token_vazio_no_servidor_aceita_bearer_vazio", False)

    def test_chave_da_evolution_so_vale_no_webhook(self):
        cli = self.cliente(CONTAINER)
        evo = {"Authorization": "Bearer CHAVE-DE-TESTE"}
        no_webhook = cli.post("/whatsapp/webhook", params={"enviar": "false"}, json=payload("evolution_texto.json"),
                              headers={"apikey": "CHAVE-DE-TESTE"})
        fora = [cli.get(u, headers=evo).status_code for u in ("/pendencias", "/whatsapp/status", "/agenda", "/busca?q=x")]
        medida("evolution_key_no_webhook", no_webhook.status_code)
        medida("evolution_key_nas_outras_rotas", fora)
        self.assertEqual(no_webhook.status_code, 200)
        self.assertEqual(set(fora), {401})

    def test_token_mestre_tambem_vale_no_webhook(self):
        r = self.cliente(CONTAINER).post("/whatsapp/webhook", params={"enviar": "false"},
                                         json=payload("evolution_texto.json"),
                                         headers={"Authorization": f"Bearer {TOKEN}"})
        medida("token_mestre_no_webhook", r.status_code)
        self.assertEqual(r.status_code, 200)


class TestExecutor(unittest.TestCase):
    def test_execute_sem_token_e_com_token_vazio(self):
        spec = importlib.util.spec_from_file_location("executor_main", RAIZ / "services" / "shell-executor" / "main.py")
        mod = importlib.util.module_from_spec(spec)
        with mock.patch.dict("os.environ", {"SHELL_EXECUTOR_TOKEN": TOKEN}):
            spec.loader.exec_module(mod)
        cli = TestClient(mod.app, client=(CONTAINER, 50000))
        sem = cli.post("/execute", json={"command": "date"})
        errado = cli.post("/execute", json={"command": "date"}, headers={"Authorization": "Bearer errado"})
        with mock.patch.object(mod, "TOKEN", ""):
            vazio = cli.post("/execute", json={"command": "date"}, headers={"Authorization": "Bearer "})
        medida("executor_sem_token / token_errado / servidor_sem_token", [sem.status_code, errado.status_code, vazio.status_code])
        self.assertEqual([sem.status_code, errado.status_code, vazio.status_code], [401, 401, 401])


if __name__ == "__main__":
    unittest.main()
