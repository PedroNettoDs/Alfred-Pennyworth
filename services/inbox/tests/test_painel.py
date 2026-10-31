"""
Testes do Painel do Alfred (painel.py + painel_web/): autenticação, restrição a 127.0.0.1, CSRF, ações
(responder/ignorar/agendar WhatsApp, vou/não vou, desfazer agenda, aprovar/recusar limpeza), polling,
pendência já resolvida, envio duplicado, Evolution fora do ar, áudio e cabeçalhos de segurança.

    cd services/inbox
    venv/bin/python -m unittest tests.test_painel -v

Offline: vault temporário, LLM/TTS/transcrição mockados, EVOLUTION_URL vazio + WHATSAPP_DRY_RUN=true
(envios simulados em logs/whatsapp_enviados.jsonl, aqui redirecionado para o diretório temporário).
O TestClient se apresenta como 127.0.0.1 (client=) — os testes de rede trocam o IP do cliente.
"""

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).parent
INBOX = AQUI.parent
sys.path.insert(0, str(INBOX))
sys.path.insert(0, str(INBOX.parent))

TMP = Path(tempfile.mkdtemp(prefix="alfred-painel-test-"))
TOKEN = "token-de-teste"          # o mesmo dos outros módulos (main.TOKEN é lido na 1ª importação)
BASE = "http://127.0.0.1:7072"

# Ambiente ANTES de importar os módulos; reaplicado em cada teste (outros módulos de teste mexem nele)
ENV = {
    "VAULT_ALFRED": str(TMP / "vault"),
    "EVOLUTION_URL": "",                       # → simulado
    "EVOLUTION_API_KEY": "CHAVE-DE-TESTE",
    "EVOLUTION_INSTANCE": "alfred",
    "SHELL_EXECUTOR_TOKEN": TOKEN,
    "WHATSAPP_NUMERO_PEDRO": "5514999990000",
    "WHATSAPP_DRY_RUN": "true",
    "WHATSAPP_DEFAULT": "pedro_whatsapp",
    "INBOX_DEFAULT": "pedro_hotmail",
    "INBOX_AUTOPLAY": "false",
    "OLLAMA_BASE_URL": "http://127.0.0.1:9",   # nada deve chegar ao Ollama
    "PORT_SHELL_EXECUTOR": "9",                # tela Sistema: nada de bater em serviços reais desta máquina
    "PORT_RESEARCHER": "9",
}
os.environ.update(ENV)

import core        # noqa: E402
import whatsapp    # noqa: E402
import agenda      # noqa: E402
import limpeza     # noqa: E402
import main        # noqa: E402
import painel      # noqa: E402
from _shared import lib_whatsapp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PAYLOADS = AQUI / "payloads"
_CARREGAR_CFG = whatsapp.carregar_cfg
ESTADO = {"atraso": 0}            # atraso_minutos usado pelo carregar_cfg de teste
RASCUNHO = "Recebi, Carlos! Vou ver o orçamento e te retorno."


def payload(nome: str) -> dict:
    return json.loads((PAYLOADS / nome).read_text(encoding="utf-8"))


# ── mocks do LLM / TTS (mesmo comportamento dos de test_whatsapp) ──

def _fake_json(prompt, **kw):
    if "RASCUNHO da resposta" in prompt:
        return {"rascunho": RASCUNHO,
                "compromisso": {"titulo": "Entregar orçamento — Fatec", "data": "2026-10-23", "hora": ""}}
    return {"categoria": "profissional", "confianca": 0.9, "requer_resposta": True, "urgencia": "alta",
            "resumo": "Carlos da Fatec pede o orçamento do servidor até sexta", "motivo": "pedido com prazo"}


def _fake_generate(prompt, **kw):
    return ("## Quem mandou e o que quer\nCarlos, da Fatec, pede o **orçamento** do servidor.\n\n"
            "<script>alert(1)</script>\n\n## O que você quer responder?\nResponda com o texto, ou **ignorar**.")


def _fake_tts(texto, voice, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(b"ID3fake-mp3")
    return True


def _cfg_de_teste(nome=None):
    n, cfg = _CARREGAR_CFG(nome)
    return n, {**cfg, "atraso_minutos": ESTADO["atraso"]}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patches = [
            mock.patch.dict(os.environ, ENV),
            mock.patch.object(whatsapp, "carregar_cfg", _cfg_de_teste),
            mock.patch.object(whatsapp, "FILA_FILE", TMP / "whatsapp_fila.json"),
            mock.patch.object(whatsapp, "SEEN_FILE", TMP / "whatsapp_seen.json"),
            mock.patch.object(lib_whatsapp, "ENVIADOS_FILE", TMP / "whatsapp_enviados.jsonl"),
            mock.patch.object(lib_whatsapp, "IDS_ENVIADOS_FILE", TMP / "whatsapp_ids_enviados.json"),
            mock.patch.object(limpeza, "REGISTRO_FILE", TMP / "inbox_categorias.json"),
            mock.patch.object(core, "_notify", lambda *a, **k: None),
            mock.patch.object(core, "_iniciar_aguardador", lambda comando, pid: None),
            mock.patch.object(whatsapp, "ollama_generate_json", _fake_json),
            mock.patch.object(whatsapp, "ollama_generate", _fake_generate),
            mock.patch.object(whatsapp, "tts_save", _fake_tts),
            mock.patch.object(core, "ollama_generate", _fake_generate),
            mock.patch.object(core, "tts_save", _fake_tts),
            mock.patch.object(painel, "_arquivo_desligado", lambda: TMP / "alfred_desligado"),
            mock.patch.object(painel, "ATRASO_LOGIN_FALHO", 0.01),
        ]
        for p in cls.patches:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in reversed(cls.patches):
            p.stop()

    def setUp(self):
        os.environ.update(ENV)
        os.environ.pop("PAINEL_SESSAO_DIAS", None)
        ESTADO["atraso"] = 0
        shutil.rmtree(TMP / "vault", ignore_errors=True)
        (TMP / "vault").mkdir(parents=True)
        for f in ("whatsapp_seen.json", "whatsapp_enviados.jsonl", "whatsapp_fila.json",
                  "whatsapp_ids_enviados.json", "alfred_desligado"):
            (TMP / f).unlink(missing_ok=True)
        painel._CACHE_WA.clear()
        painel._REVOGADOS.clear()
        self.respostas = []   # tudo que o painel devolveu (o token não pode aparecer em nenhuma)

    # ── helpers ──
    def cliente(self, ip="127.0.0.1", **kw) -> TestClient:
        return TestClient(main.app, base_url=BASE, client=(ip, 50000), **kw)

    def guardar(self, r):
        self.respostas.append(r)
        return r

    def entrar(self, cli=None, token=TOKEN, **form) -> TestClient:
        cli = cli or self.cliente()
        r = self.guardar(cli.post("/painel/login", data={"token": token, **form}, headers={"Origin": BASE},
                                  follow_redirects=False))
        self.assertEqual(r.status_code, 303, r.text)
        return cli

    def csrf(self, cli, url="/painel") -> str:
        html = self.guardar(cli.get(url)).text
        m = re.search(r'<meta name="csrf-token" content="([0-9a-f]+)">', html)
        self.assertTrue(m, "página sem csrf-token")
        return m.group(1)

    def acao(self, cli, url, corpo=None, csrf=None, origem=BASE, **headers):
        h = dict(headers)
        if csrf is not False:
            h["X-CSRF-Token"] = csrf if csrf is not None else self.csrf(cli)
        if origem:
            h["Origin"] = origem
        return self.guardar(cli.post(url, json=corpo or {}, headers=h))

    def criar_wa(self, nome="evolution_texto.json") -> dict:
        cli = TestClient(main.app)
        r = cli.post("/whatsapp/webhook", params={"enviar": "false"}, json=payload(nome),
                     headers={"apikey": "CHAVE-DE-TESTE"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["acao"], "pendencia_criada", d)
        return d["pendencia"]

    def fromme(self) -> dict:
        r = TestClient(main.app).post("/whatsapp/webhook", json=payload("evolution_fromme_direto.json"),
                                      headers={"apikey": "CHAVE-DE-TESTE"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def criar_email(self, sufixo="") -> dict:
        m = {"text": "Convite para o meetup de Python em Bauru.", "from_addr": "eventos@meetup.org",
             "from_name": "Meetup Bauru", "subject": f"Convite: Python Bauru{sufixo}",
             "date_iso": "2026-10-24T10:00:00-03:00", "message_id": f"<convite{sufixo}@meetup.org>"}
        ev = {"tipo": "evento", "titulo": f"Python Bauru{sufixo}", "organizador": "Meetup Bauru",
              "quem": "Meetup Bauru", "data_inicio": "2026-11-05T19:00", "data_fim": None, "local": "Bauru",
              "online": False, "link": "https://meetup.org/python-bauru", "custo": "gratuito",
              "o_que_pedem": "confirmar presença", "prazo_resposta": None, "resumo": "Meetup de Python"}
        return core.criar_pendencia(m, ev, {}, "pedro_hotmail")

    def enviados(self):
        return lib_whatsapp.ultimos_envios(50)

    def tearDown(self):
        for r in self.respostas:
            self.assertNotIn(TOKEN, r.text, f"token apareceu na resposta de {r.request.url}")
            for k, v in r.headers.items():
                self.assertNotIn(TOKEN, v, f"token apareceu no cabeçalho {k}")


# ══════════════════════════════════════════════════════════════
#  1. Autenticação e sessão
# ══════════════════════════════════════════════════════════════

class TestAutenticacao(Base):
    def test_sem_sessao_vai_para_o_login(self):
        cli = self.cliente()
        r = self.guardar(cli.get("/painel", follow_redirects=False))
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/painel/login"))
        r = self.guardar(cli.get("/painel/pendencias?aba=resolvidas", follow_redirects=False))
        self.assertEqual(r.headers["location"], "/painel/login?proximo=%2Fpainel%2Fpendencias%3Faba%3Dresolvidas")
        login = self.guardar(cli.get("/painel"))   # seguindo o redirecionamento
        self.assertEqual(login.status_code, 200)
        self.assertIn('name="token"', login.text)
        self.assertNotIn("csrf-token", login.text)
        # API e parciais: 401 JSON (o JS leva ao login)
        for url in ("/painel/api/contadores", "/painel/parcial/pendencias", "/painel/api/pendencias/x/estado"):
            r = self.guardar(cli.get(url))
            self.assertEqual(r.status_code, 401, url)
            self.assertEqual(r.json()["erro"], "Sessão expirada. Entre de novo.")

    def test_token_errado(self):
        cli = self.cliente()
        r = self.guardar(cli.post("/painel/login", data={"token": "palpite-123"}, headers={"Origin": BASE},
                                  follow_redirects=False))
        self.assertEqual(r.status_code, 401)
        self.assertIn("Token inválido", r.text)
        self.assertNotIn("palpite-123", r.text)            # nunca ecoa o que foi digitado
        self.assertNotIn("set-cookie", r.headers)
        self.assertEqual(self.guardar(cli.get("/painel", follow_redirects=False)).status_code, 303)

    def test_token_certo_cookie_e_inicio(self):
        cli = self.cliente()
        r = self.guardar(cli.post("/painel/login", data={"token": TOKEN}, headers={"Origin": BASE},
                                  follow_redirects=False))
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/painel"))
        ck = r.headers["set-cookie"]
        self.assertTrue(ck.startswith("alfred_painel=v1."), ck)
        for parte in ("HttpOnly", "SameSite=Strict", "Max-Age=604800", "Path=/"):
            self.assertIn(parte, ck)
        self.assertNotIn("Secure", ck)    # http em 127.0.0.1
        inicio = self.guardar(cli.get("/painel"))
        self.assertEqual(inicio.status_code, 200)
        self.assertIn("<h1 class=\"tela-titulo\">Início</h1>", inicio.text)
        self.assertIn('aria-current="page"', inicio.text)
        self.assertIn("Nenhuma pendência aguardando, senhor.", inicio.text)
        # já logado, o login leva ao Início
        r = self.guardar(cli.get("/painel/login", follow_redirects=False))
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/painel"))

    def test_proximo_so_dentro_do_painel(self):
        r = self.guardar(self.cliente().post("/painel/login", data={"token": TOKEN, "proximo": "/painel/agenda"},
                                             headers={"Origin": BASE}, follow_redirects=False))
        self.assertEqual(r.headers["location"], "/painel/agenda")
        for ruim in ("//evil.example/painel", "https://evil.example/", "/outra"):
            r = self.guardar(self.cliente().post("/painel/login", data={"token": TOKEN, "proximo": ruim},
                                                 headers={"Origin": BASE}, follow_redirects=False))
            self.assertEqual(r.headers["location"], "/painel", ruim)

    def test_dias_de_sessao_configuravel(self):
        os.environ["PAINEL_SESSAO_DIAS"] = "2"
        r = self.guardar(self.cliente().post("/painel/login", data={"token": TOKEN}, headers={"Origin": BASE},
                                             follow_redirects=False))
        self.assertIn("Max-Age=172800", r.headers["set-cookie"])
        expira = int(r.headers["set-cookie"].split("=", 1)[1].split(".")[1])
        self.assertAlmostEqual(expira - time.time(), 2 * 86400, delta=60)

    def test_dias_de_sessao_invalido_ou_fora_da_faixa_volta_a_7(self):
        """inf/1e400 davam 500 no login (int(inf)); valores minúsculos davam Max-Age=0 (login em laço)."""
        for valor in ("inf", "1e400", "-inf", "nan", "0.000001", "0.01", "0", "-3", "abc", "3651", "1e9"):
            os.environ["PAINEL_SESSAO_DIAS"] = valor
            r = self.guardar(self.cliente().post("/painel/login", data={"token": TOKEN}, headers={"Origin": BASE},
                                                 follow_redirects=False))
            self.assertEqual(r.status_code, 303, valor)
            self.assertIn("Max-Age=604800", r.headers["set-cookie"], valor)
        for valor, max_age in (("0.5", 43200), ("3650", 3650 * 86400), (str(1 / 24), 3600)):
            os.environ["PAINEL_SESSAO_DIAS"] = valor
            r = self.guardar(self.cliente().post("/painel/login", data={"token": TOKEN}, headers={"Origin": BASE},
                                                 follow_redirects=False))
            self.assertIn(f"Max-Age={max_age}", r.headers["set-cookie"], valor)

    def test_cookie_e_csrf_nao_ascii_nao_derrubam(self):
        """compare_digest(str, str) levantava TypeError com caractere não ASCII: 500 no painel, no login e
        até no GET de áudio com Bearer válido (cookie gravado por outra página de 127.0.0.1)."""
        p = self.criar_wa()
        url_audio = f"/pendencias/{p['id']}/audio"
        for bruto in ("alfred_painel=v1.1.é.é".encode("latin-1"), "alfred_painel=v1.1.é.é".encode("utf-8"),
                      "alfred_painel=v1.²³.n.é".encode("utf-8")):
            h = {"cookie": bruto}
            cli = self.cliente()
            r = self.guardar(cli.get("/painel", headers=h, follow_redirects=False))
            self.assertEqual(r.status_code, 303, bruto)
            self.assertEqual(self.guardar(cli.get("/painel/login", headers=h)).status_code, 200, bruto)
            self.assertEqual(self.guardar(cli.get("/painel/api/contadores", headers=h)).status_code, 401, bruto)
            r = cli.get(url_audio, headers={**h, "Authorization": f"Bearer {TOKEN}"})
            self.assertEqual((r.status_code, r.content), (200, b"ID3fake-mp3"), bruto)
            self.assertEqual(cli.get("/pendencias/nao-existe/audio",
                                     headers={**h, "Authorization": f"Bearer {TOKEN}"}).status_code, 404, bruto)
            self.assertEqual(self.guardar(cli.get(url_audio, headers=h)).status_code, 401, bruto)
        # X-CSRF-Token não ASCII com sessão válida e Origin certo → 403 (não 500), nada muda
        cli = self.entrar()
        self.csrf(cli)
        url = f"/painel/api/pendencias/{p['id']}/responder-texto"
        for token_ruim in ("é".encode("latin-1"), "é".encode("utf-8")):
            r = self.guardar(cli.post(url, json={"texto": "oi"}, headers={"X-CSRF-Token": token_ruim, "Origin": BASE}))
            self.assertEqual(r.status_code, 403, token_ruim)
            self.assertIn("recusou a ação por segurança", r.json()["erro"])
        self.assertEqual(core.obter_pendencia(p["id"])["status"], "aguardando")

    def test_contadores_renovam_o_csrf_depois_de_entrar_de_novo(self):
        """Sessão expirada no meio de uma edição: Pedro entra de novo em outra aba e a página aberta pega o
        X-CSRF-Token novo pelo polling de contadores ("Tentar de novo" funciona sem recarregar)."""
        cli = self.entrar()
        antigo = self.csrf(cli)
        self.assertEqual(self.guardar(cli.get("/painel/api/contadores")).headers["x-csrf-token"], antigo)
        self.acao(cli, "/painel/sair", csrf=antigo)               # sessão revogada (ex.: Sair em outra aba)
        r = self.guardar(cli.get("/painel/api/contadores"))
        self.assertEqual(r.status_code, 401); self.assertNotIn("x-csrf-token", r.headers)
        self.entrar(cli)                                           # entrou de novo
        novo = self.guardar(cli.get("/painel/api/contadores")).headers["x-csrf-token"]
        self.assertNotEqual(novo, antigo)
        p = self.criar_wa()
        url = f"/painel/api/pendencias/{p['id']}/responder-texto"
        self.assertEqual(self.acao(cli, url, {"texto": "Oi"}, csrf=antigo).status_code, 403)
        self.assertEqual(self.acao(cli, url, {"texto": "Oi"}, csrf=novo).status_code, 200)

    def test_cookie_adulterado_expirado_ou_revogado(self):
        cli = self.entrar()
        valor = cli.cookies.get("alfred_painel")
        _v, expira, nonce, assin = valor.split(".")

        def status_com(cookie):
            c = self.cliente(cookies={"alfred_painel": cookie})
            return self.guardar(c.get("/painel", follow_redirects=False)).status_code

        self.assertEqual(status_com(valor), 200)
        adulterado = f"v1.{expira}.{nonce}.{assin[:-1]}{'0' if assin[-1] != '0' else '1'}"
        self.assertEqual(status_com(adulterado), 303)
        mais_tempo = f"v1.{int(expira) + 999999}.{nonce}.{assin}"      # esticar a validade quebra a assinatura
        self.assertEqual(status_com(mais_tempo), 303)
        # expirado, mesmo com assinatura correta
        venceu = int(time.time()) - 10
        sig = hmac.new(TOKEN.encode(), f"painel-sessao|{venceu}|{nonce}".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(status_com(f"v1.{venceu}.{nonce}.{sig}"), 303)
        # assinado com outro token (token trocado no .env invalida as sessões)
        futuro = int(time.time()) + 3600
        sig = hmac.new(b"outro-token", f"painel-sessao|{futuro}|{nonce}".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(status_com(f"v1.{futuro}.{nonce}.{sig}"), 303)
        self.assertEqual(status_com("lixo"), 303)
        # Sair revoga a sessão (mesmo que alguém tenha guardado o cookie)
        r = self.acao(cli, "/painel/sair")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("alfred_painel=", r.headers["set-cookie"])
        self.assertEqual(status_com(valor), 303)

    def test_login_exige_origem_e_token_no_env(self):
        r = self.guardar(self.cliente().post("/painel/login", data={"token": TOKEN}, follow_redirects=False))
        self.assertEqual(r.status_code, 403)
        r = self.guardar(self.cliente().post("/painel/login", data={"token": TOKEN},
                                             headers={"Origin": "http://evil.example"}, follow_redirects=False))
        self.assertEqual(r.status_code, 403)
        os.environ["SHELL_EXECUTOR_TOKEN"] = ""
        r = self.guardar(self.cliente().post("/painel/login", data={"token": ""}, headers={"Origin": BASE},
                                             follow_redirects=False))
        self.assertEqual(r.status_code, 401)
        self.assertIn("SHELL_EXECUTOR_TOKEN não definido no .env", r.text)
        self.assertIn("SHELL_EXECUTOR_TOKEN não definido no .env", self.guardar(self.cliente().get("/painel/login")).text)


# ══════════════════════════════════════════════════════════════
#  2. Só 127.0.0.1 + Host local   ·   3. CSRF / origem
# ══════════════════════════════════════════════════════════════

class TestRede(Base):
    def test_outro_ip_e_container_recusados(self):
        cookie = self.entrar().cookies.get("alfred_painel")
        for ip in ("10.0.0.5", "172.17.0.2", "192.168.0.10"):
            cli = self.cliente(ip, cookies={"alfred_painel": cookie})
            for metodo, url in (("get", "/painel"), ("get", "/painel/login"), ("get", "/painel/pendencias"),
                                ("get", "/painel/api/contadores"), ("get", "/painel/parcial/inicio"),
                                ("get", "/painel/static/painel.js"), ("post", "/painel/login"),
                                ("post", "/painel/api/pendencias/x/responder-texto")):
                r = self.guardar(getattr(cli, metodo)(url, follow_redirects=False))
                self.assertEqual(r.status_code, 403, (ip, url))
                self.assertEqual(r.json()["erro"], "Painel disponível apenas em 127.0.0.1.")
                self.assertIn("default-src 'self'", r.headers["content-security-policy"])
            # o áudio com o cookie do painel também não vale de fora (sem Bearer → 401)
            self.assertEqual(self.guardar(cli.get("/pendencias/x/audio")).status_code, 401)

    def test_host_estranho_recusado(self):
        cli = self.entrar()
        for host in ("evil.example", "evil.example:7072", "127.0.0.1.evil.example", "192.168.0.10:7072"):
            r = self.guardar(cli.get("/painel", headers={"Host": host}, follow_redirects=False))
            self.assertEqual(r.status_code, 403, host)
        for host in ("localhost:7072", "127.0.0.1"):
            r = self.guardar(cli.get("/painel/login", headers={"Host": host}, follow_redirects=False))
            self.assertIn(r.status_code, (200, 303), host)

    def test_ipv6_loopback_aceito(self):
        r = self.guardar(self.cliente("::1").get("/painel/login", headers={"Host": "[::1]:7072"}))
        self.assertEqual(r.status_code, 200)

    def test_endpoints_bearer_continuam_iguais(self):
        cli = self.cliente("172.17.0.2")   # N8N / tools no Docker
        r = cli.get("/pendencias", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(cli.get("/pendencias").status_code, 401)


class TestCSRF(Base):
    def test_post_sem_csrf_ou_com_origem_estranha_nao_muda_nada(self):
        p = self.criar_wa()
        cli = self.entrar()
        token = self.csrf(cli)
        url = f"/painel/api/pendencias/{p['id']}/responder-texto"
        casos = [
            dict(csrf=False),                                     # sem X-CSRF-Token
            dict(csrf="0" * 64),                                  # token errado
            dict(origem="http://evil.example"),                   # Origin de outro site
            dict(origem="http://localhost:7072"),                 # outra origem local (≠ Host)
            dict(origem=None),                                    # sem Origin nem Referer
        ]
        for c in casos:
            r = self.acao(cli, url, {"texto": "oi"}, **({"csrf": token} | c))
            self.assertEqual(r.status_code, 403, c)
            self.assertIn("recusou a ação por segurança", r.json()["erro"])
        # outro usuário/sessão: o CSRF é amarrado ao nonce da sessão
        outro = self.csrf(self.entrar())
        self.assertEqual(self.acao(cli, url, {"texto": "oi"}, csrf=outro).status_code, 403)
        # sem sessão: 401
        self.assertEqual(self.acao(self.cliente(), url, {"texto": "oi"}, csrf=token).status_code, 401)
        # nada mudou
        self.assertEqual(core.obter_pendencia(p["id"])["status"], "aguardando")
        self.assertEqual(self.enviados(), [])
        # Referer da própria origem (sem Origin) é aceito
        r = self.acao(cli, url, {"texto": "Ok!"}, csrf=token, origem=None, Referer=f"{BASE}/painel/pendencias/{p['id']}")
        self.assertEqual(r.status_code, 200, r.text)


# ══════════════════════════════════════════════════════════════
#  4–7. WhatsApp: rascunho, envio, já resolvida, duplo clique, falha
# ══════════════════════════════════════════════════════════════

class TestWhatsApp(Base):
    def test_detalhe_mostra_rascunho_e_dados(self):
        p = self.criar_wa()
        cli = self.entrar()
        html = self.guardar(cli.get(f"/painel/pendencias/{p['id']}")).text
        # aguardando: o banner dinâmico (âmbar, role=alert) fica escondido até o polling/409 revelar
        self.assertIn('<div class="aviso aviso--aguardando" role="alert" data-banner-resolvida hidden>', html)
        m = re.search(r'<textarea[^>]*name="texto"[^>]*>([^<]*)</textarea>', html)
        self.assertTrue(m, "sem textarea da resposta")
        self.assertEqual(m.group(1), RASCUNHO)
        for trecho in ("Carlos Fatec", "+55 14 98888-7777", "Resumo do Alfred", "Carlos da Fatec pede o orçamento",
                       "profissional", "90%", "Urgência alta", "Autorizar envio", "Não enviar", "Agendar",
                       f'data-estado-url="/painel/api/pendencias/{p["id"]}/estado"', 'id="dialogo-envio"',
                       "Modo simulado: nada sairá da máquina", f'src="/pendencias/{p["id"]}/audio"',
                       "Histórico", "Pendência criada", "23/10/2026"):
            self.assertIn(trecho, html)
        self.assertNotIn("Esta pendência já foi resolvida por", html.split("data-banner-texto>")[1][:80])

    def test_autorizar_envio_com_texto_editado(self):
        p = self.criar_wa()
        cli = self.entrar()
        editado = "Carlos, mando o orçamento até quinta. Abraço!"
        r = self.acao(cli, f"/painel/api/pendencias/{p['id']}/responder-texto", {"texto": editado})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mensagem"], "Simulado: nada saiu da máquina.")
        self.assertTrue(r.json()["ok"])
        y = core.obter_pendencia(p["id"])
        self.assertEqual((y["status"], y["resposta_texto"], y["respondido_via"]), ("respondida", editado, "painel"))
        self.assertTrue(y["whatsapp"]["envio"]["ok"]) ; self.assertTrue(y["whatsapp"]["envio"]["simulado"])
        env = self.enviados()
        self.assertEqual(len(env), 1)
        self.assertEqual((env[0]["numero"], env[0]["conteudo"]), ("5514988887777", editado))
        # saiu de aguardando; detalhe mostra o resultado, sem ações
        self.assertEqual(self.guardar(cli.get("/painel/api/contadores")).json()["aguardando"], 0)
        html = self.guardar(cli.get(f"/painel/pendencias/{p['id']}")).text
        self.assertIn("Esta pendência já foi resolvida por Painel do Alfred.", html)
        self.assertNotIn("data-acao=", html.replace('data-acao="/painel/sair"', ""))
        self.assertIn("Simulado: nada saiu da máquina", html)

    def test_envio_real_mostra_enviado_para_contato(self):
        p = self.criar_wa()
        cli = self.entrar()
        with mock.patch.object(whatsapp, "enviar_texto",
                               return_value={"ok": True, "simulado": False, "message_id": "REAL-1"}) as env:
            r = self.acao(cli, f"/painel/api/pendencias/{p['id']}/responder-texto", {"texto": "Oi Carlos"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mensagem"], "Enviado para Carlos Fatec.")
        env.assert_called_once()

    def test_resolvida_pelo_celular_enquanto_aberta(self):
        p = self.criar_wa()
        cli = self.entrar()
        token = self.csrf(cli, f"/painel/pendencias/{p['id']}")      # detalhe aberto
        d = self.fromme()                                               # Pedro responde direto do celular
        self.assertEqual(d["acao"], "respondida_manual", d)
        estado = self.guardar(cli.get(f"/painel/api/pendencias/{p['id']}/estado")).json()
        self.assertEqual(estado, {"status": "respondida_manual", "resolvida": True,
                                  "caminho": "resposta direta pelo celular",
                                  "mensagem": "Esta pendência já foi resolvida por resposta direta pelo celular."})
        for url, corpo in ((f"/painel/api/pendencias/{p['id']}/responder-texto", {"texto": "Oi"}),
                           (f"/painel/api/pendencias/{p['id']}/ignorar", {}),
                           (f"/painel/api/pendencias/{p['id']}/agendar", {"quando": "amanhã 10h"})):
            r = self.acao(cli, url, corpo, csrf=token)
            self.assertEqual(r.status_code, 409, url)
            self.assertEqual(r.json()["erro"], "Esta pendência já foi resolvida por resposta direta pelo celular.")
        self.assertEqual(self.enviados(), [])
        y = core.obter_pendencia(p["id"])
        self.assertEqual((y["status"], y["whatsapp"]["rascunho"]), ("respondida_manual", RASCUNHO))
        # a página reaberta mostra o lado a lado e nenhuma ação
        html = self.guardar(cli.get(f"/painel/pendencias/{p['id']}")).text
        self.assertIn("Esta pendência já foi resolvida por resposta direta pelo celular.", html)
        # já abre resolvida: aviso neutro com role=status (o âmbar + role=alert é só do caso dinâmico)
        aviso = re.search(r'<div class="([^"]*)" role="([^"]*)"[^>]*>\s*<svg[^>]*>.*?</svg>\s*'
                          r'<p class="aviso__texto">Esta pendência já foi resolvida', html, re.S)
        self.assertTrue(aviso, "aviso de já resolvida não encontrado")
        self.assertEqual((aviso.group(1), aviso.group(2)), ("aviso", "status"))
        self.assertNotIn("data-banner-resolvida", html)
        self.assertNotIn("aviso--aguardando", html.split('<div class="detalhe">')[1])   # (fora o <noscript>)
        self.assertIn("O Alfred proporia", html); self.assertIn("já te mando", html)
        self.assertNotIn('id="form-resposta"', html)

    def test_corrida_dentro_do_responder_texto_vira_409(self):
        """Checagem prévia passou, mas outro caminho resolveu antes da trava: PendenciaJaResolvida → 409."""
        p = self.criar_wa()
        cli = self.entrar()
        token = self.csrf(cli)
        original = whatsapp.responder_texto

        def antes_resolve(pid, *a, **k):
            original(pid, "Respondi pelo terminal", origem="via terminal")   # outro processo chegou antes
            return original(pid, *a, **k)

        with mock.patch.object(whatsapp, "responder_texto", antes_resolve):
            r = self.acao(cli, f"/painel/api/pendencias/{p['id']}/responder-texto", {"texto": "Oi"}, csrf=token)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["erro"], "Esta pendência já foi resolvida por terminal.")
        self.assertEqual(len(self.enviados()), 1)

    def test_duplo_clique_um_envio_so(self):
        p = self.criar_wa()
        cli = self.entrar()
        token = self.csrf(cli)
        cookie = cli.cookies.get("alfred_painel")
        url = f"/painel/api/pendencias/{p['id']}/responder-texto"
        original = lib_whatsapp.enviar_texto

        def envio_lento(*a, **k):   # alarga a janela: os dois cliques chegam com o primeiro ainda enviando
            time.sleep(0.3)
            return original(*a, **k)

        barreira = threading.Barrier(2)
        resultados = []

        def clicar():
            c = self.cliente(cookies={"alfred_painel": cookie})
            barreira.wait()
            resultados.append(c.post(url, json={"texto": "Fechado, Carlos!"},
                                     headers={"X-CSRF-Token": token, "Origin": BASE}))

        with mock.patch.object(whatsapp, "enviar_texto", envio_lento):
            ts = [threading.Thread(target=clicar) for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(20)
        self.respostas += resultados
        self.assertEqual(sorted(r.status_code for r in resultados), [200, 409], [r.text for r in resultados])
        conflito = next(r for r in resultados if r.status_code == 409)
        self.assertEqual(conflito.json()["erro"], "Esta pendência já foi resolvida por Painel do Alfred.")
        self.assertEqual(len(self.enviados()), 1)
        # e duas seguidas (sem corrida): a segunda também é recusada, sem novo envio
        r = self.acao(cli, url, {"texto": "Fechado, Carlos!"}, csrf=token)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(self.enviados()), 1)

    def test_falha_de_envio_mantem_texto_e_permite_tentar_de_novo(self):
        p = self.criar_wa()
        cli = self.entrar()
        token = self.csrf(cli)
        url = f"/painel/api/pendencias/{p['id']}/responder-texto"
        texto = "Carlos, segue amanhã cedo."
        with mock.patch.object(whatsapp, "enviar_texto",
                               return_value={"ok": False, "simulado": False, "erro": "Evolution fora do ar"}):
            r = self.acao(cli, url, {"texto": texto}, csrf=token)
        self.assertEqual(r.status_code, 502, r.text)
        self.assertTrue(r.json()["pode_tentar_de_novo"])
        self.assertIn("Evolution fora do ar", r.json()["erro"])
        y = core.obter_pendencia(p["id"])
        self.assertEqual((y["status"], y["resposta_texto"], y.get("respondido_via")), ("aguardando", texto, None))
        self.assertEqual(self.enviados(), [])
        # o detalhe reaberto traz o texto guardado (não o rascunho) e o aviso da falha
        html = self.guardar(cli.get(f"/painel/pendencias/{p['id']}")).text
        self.assertRegex(html, r'<textarea[^>]*name="texto"[^>]*>' + re.escape(texto) + "</textarea>")
        self.assertIn("O último envio falhou", html)
        # nova tentativa (Evolution de volta / simulado) → 200
        r = self.acao(cli, url, {"texto": texto}, csrf=token)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mensagem"], "Simulado: nada saiu da máquina.")
        self.assertEqual(core.obter_pendencia(p["id"])["status"], "respondida")
        self.assertEqual(len(self.enviados()), 1)

    def test_nao_enviar_e_texto_vazio(self):
        p = self.criar_wa()
        cli = self.entrar()
        token = self.csrf(cli)
        r = self.acao(cli, f"/painel/api/pendencias/{p['id']}/responder-texto", {"texto": "   "}, csrf=token)
        self.assertEqual((r.status_code, r.json()["campo"]), (400, "texto"))
        r = self.acao(cli, f"/painel/api/pendencias/{p['id']}/ignorar", {}, csrf=token)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Nada foi enviado", r.json()["mensagem"])
        y = core.obter_pendencia(p["id"])
        self.assertEqual((y["status"], y["respondido_via"]), ("ignorada", "painel"))
        self.assertEqual(self.enviados(), [])
        self.assertEqual(self.acao(cli, "/painel/api/pendencias/nao-existe/ignorar", {}, csrf=token).status_code, 404)

    def test_agendar(self):
        p = self.criar_wa()
        cli = self.entrar()
        token = self.csrf(cli)
        html = self.guardar(cli.get(f"/painel/pendencias/{p['id']}")).text
        self.assertIn('value="23/10/2026"', html)     # sugestao_quando (compromisso do rascunho)
        url = f"/painel/api/pendencias/{p['id']}/agendar"
        r = self.acao(cli, url, {"quando": "não sei quando"}, csrf=token)
        self.assertEqual((r.status_code, r.json()["campo"]), (400, "quando"))
        r = self.acao(cli, url, {"quando": "amanhã 10h"}, csrf=token)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(d["google_url"].startswith("https://calendar.google.com/"))
        self.assertIn("continua aguardando", d["mensagem"])
        y = core.obter_pendencia(p["id"])
        self.assertEqual(y["status"], "aguardando")
        self.assertEqual(y["whatsapp"]["agenda"]["task_id"], d["task_id"])
        tasks = (core.vault() / "tasks.yml").read_text(encoding="utf-8")
        self.assertIn("10:00", tasks)
        self.assertEqual(self.enviados(), [])


# ══════════════════════════════════════════════════════════════
#  8. E-mail: Vou / Não vou
# ══════════════════════════════════════════════════════════════

class TestEmail(Base):
    def test_vou_igual_ao_cli(self):
        import yaml
        pelo_cli = self.criar_email(" A")
        pelo_painel = self.criar_email(" B")
        cli = self.entrar()
        html = self.guardar(cli.get(f"/painel/pendencias/{pelo_painel['id']}")).text
        for trecho in ("Explicação do Alfred", "<strong>orçamento</strong>", "https://meetup.org/python-bauru",
                       'value="vou"', 'value="nao_vou"', "Meetup Bauru", "05/11/2026 19:00"):
            self.assertIn(trecho, html)
        self.assertNotIn("<script>alert", html)                  # markdown seguro: HTML do LLM escapado
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("Como responder", html)
        self.assertNotIn("Dados extraídos", html)              # o cartão "Dados do e-mail" já mostra
        self.assertNotIn("aguardando resposta", html)          # linha de status do .md (fica velha)

        ref = core.responder(pelo_cli["id"], "vou", "", origem="via terminal")   # ./run.sh responder <id> vou
        r = self.acao(cli, f"/painel/api/pendencias/{pelo_painel['id']}/responder", {"resposta": "vou", "nota": ""})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        y = core.obter_pendencia(pelo_painel["id"])
        self.assertEqual((y["status"], y["resposta"], y["respondido_via"]), ("vou", "vou", "painel"))
        self.assertEqual((ref["status"], ref["resposta"]), ("vou", "vou"))
        self.assertEqual(d["task_id"], y["task_id"])
        self.assertIn(f"Tarefa #{y['task_id']}", d["mensagem"])
        tasks = yaml.safe_load((core.vault() / "tasks.yml").read_text(encoding="utf-8"))["tasks"]
        titulos = {t["id"]: t["title"] for t in tasks}
        self.assertEqual(titulos[ref["task_id"]], "Ir: Python Bauru A")
        self.assertEqual(titulos[y["task_id"]], "Ir: Python Bauru B")
        self.assertEqual([t["due_date"] for t in tasks], ["2026-11-05", "2026-11-05"])
        md = (core.vault() / y["arquivos"]["md"]).read_text(encoding="utf-8")
        self.assertIn("status: vou", md); self.assertIn("**VOU**", md)
        # agenda do evento (mesmo efeito do CLI: _agendar_evento_email)
        self.assertEqual(len([i for i in agenda.listar() if i["origem"].get("canal") == "email"]), 2)
        # de novo → já resolvida
        r = self.acao(cli, f"/painel/api/pendencias/{pelo_painel['id']}/responder", {"resposta": "nao_vou"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["erro"], "Esta pendência já foi resolvida por Painel do Alfred.")

    def test_nao_vou_com_nota_e_validacoes(self):
        p = self.criar_email()
        wa = self.criar_wa()
        cli = self.entrar()
        token = self.csrf(cli)
        url = f"/painel/api/pendencias/{p['id']}/responder"
        self.assertEqual(self.acao(cli, url, {"resposta": "talvez"}, csrf=token).status_code, 400)
        self.assertEqual(self.acao(cli, f"/painel/api/pendencias/{wa['id']}/responder", {"resposta": "vou"},
                                   csrf=token).status_code, 400)
        r = self.acao(cli, url, {"resposta": "nao_vou", "nota": "Conflita com a aula"}, csrf=token)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mensagem"], "Registrado: não vou.")
        y = core.obter_pendencia(p["id"])
        self.assertEqual((y["status"], y["nota"], y.get("task_id")), ("nao_vou", "Conflita com a aula", None))
        self.assertFalse((core.vault() / "tasks.yml").exists())
        # resolvida pelo terminal enquanto o detalhe estava aberto → 409 com o caminho
        p2 = self.criar_email(" 2")
        core.responder(p2["id"], "vou", origem="via terminal")
        r = self.acao(cli, f"/painel/api/pendencias/{p2['id']}/responder", {"resposta": "nao_vou"}, csrf=token)
        self.assertEqual((r.status_code, r.json()["erro"]), (409, "Esta pendência já foi resolvida por terminal."))
        self.assertEqual(core.obter_pendencia(p2["id"])["status"], "vou")


# ══════════════════════════════════════════════════════════════
#  9. Limpeza   ·   10. Agenda
# ══════════════════════════════════════════════════════════════

def _candidatos(n=2):
    itens = [{"message_id": f"promo-{i}@loja.com", "from": "ofertas@loja.com", "from_name": "Loja",
              "subject": f"Oferta {i}", "date": "2026-08-01T10:00:00-03:00", "pasta": "INBOX",
              "conta": "pedro@hotmail.com", "motivo": "promocao há 85 dias"} for i in range(n)]
    return {"inbox": "pedro_hotmail", "candidatos": itens, "total": n}


class TestLimpeza(Base):
    def propor(self, n=2):
        with mock.patch.object(limpeza, "candidatos", lambda *a, **k: _candidatos(n)):
            return limpeza.propor(notificar=False)

    def test_aprovar_e_recusar(self):
        lote = self.propor(2)
        cli = self.entrar()
        html = self.guardar(cli.get("/painel/limpeza")).text
        for trecho in ("2 e-mails", "Oferta 0", "ofertas@loja.com", "01/08/2026 10:00", "Motivo: 2 promocao",
                       'data-dialogo-mensagem="Os 2 e-mails irão para a Lixeira da conta. Nada é apagado de forma permanente."',
                       'id="dialogo-limpeza"', f'id="lote-{lote["id"]}-emails"'):
            self.assertIn(trecho, html)
        self.assertIn('"limpeza_aguardando": 1', json.dumps(self.guardar(cli.get("/painel/api/contadores")).json()))
        token = self.csrf(cli)
        with mock.patch.object(limpeza, "avaliar", lambda m, *a, **k: ("promocao há 85 dias", "")):
            r = self.acao(cli, f"/painel/api/limpeza/{lote['id']}/aprovar", {"nota": "pode"}, csrf=token)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("Lixeira", r.json()["mensagem"])
            l = limpeza.obter_lote(lote["id"])
            self.assertEqual((l["status"], l["nota"], l["total"]), ("aprovado", "pode", 2))
            r = self.acao(cli, f"/painel/api/limpeza/{lote['id']}/aprovar", {}, csrf=token)
        self.assertEqual((r.status_code, r.json()["erro"]), (409, "Este lote já foi resolvido (aprovado)."))
        self.assertEqual(self.acao(cli, f"/painel/api/limpeza/{lote['id']}/recusar", {}, csrf=token).status_code, 409)

        outro = self.propor(3)
        r = self.acao(cli, f"/painel/api/limpeza/{outro['id']}/recusar", {"nota": "ainda não"}, csrf=token)
        self.assertEqual(r.status_code, 200, r.text)
        l = limpeza.obter_lote(outro["id"])
        self.assertEqual((l["status"], l["nota"]), ("recusado", "ainda não"))
        self.assertEqual(self.acao(cli, "/painel/api/limpeza/nao-existe/aprovar", {}, csrf=token).status_code, 404)
        html = self.guardar(cli.get("/painel/parcial/limpeza")).text
        self.assertNotIn('class="lote__form"', html)    # nenhum lote aguardando → sem botões

    def test_vazia(self):
        html = self.guardar(self.entrar().get("/painel/limpeza")).text
        self.assertIn("Nenhum lote de limpeza, senhor.", html)


class TestAgenda(Base):
    def test_desfazer_como_o_cli(self):
        amanha = core._now().replace(hour=15, minute=0, second=0, microsecond=0) + timedelta(days=1)
        a = agenda.adicionar("Reunião com a Attano", amanha, notificar=False)
        b = agenda.adicionar("Dentista", amanha + timedelta(days=1), notificar=False)
        agenda.registrar_resultado(b["id"], "criar", True, "Agenda")      # extensão já pôs no calendário
        cli = self.entrar()
        html = self.guardar(cli.get("/painel/agenda")).text
        for trecho in ("Reunião com a Attano", "Dentista", "Pendente", "No calendário", "Desfazer", "Manual",
                       f"{amanha:%d/%m/%Y} 15:00"):
            self.assertIn(trecho, html)
        token = self.csrf(cli)
        r = self.acao(cli, f"/painel/api/agenda/{a['id']}/desfazer", csrf=token)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(agenda.obter(a["id"])["status"], "cancelado")          # pendente → cancelado
        r = self.acao(cli, f"/painel/api/agenda/{b['id']}/desfazer", csrf=token)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(agenda.obter(b["id"])["status"], "desfazer")           # no_calendario → desfazer
        self.assertIn("Thunderbird", r.json()["mensagem"])
        for aid in (a["id"], b["id"]):
            r = self.acao(cli, f"/painel/api/agenda/{aid}/desfazer", csrf=token)
            self.assertEqual(r.status_code, 409, aid)
        self.assertEqual(agenda.obter(b["id"])["status"], "desfazer")           # não virou "cancelado"
        self.assertEqual(self.acao(cli, "/painel/api/agenda/x/desfazer", csrf=token).status_code, 404)
        html = self.guardar(cli.get("/painel/parcial/agenda")).text
        self.assertIn("Desfeitos e cancelados", html)

    def test_vazia(self):
        self.assertIn("A agenda está vazia.", self.guardar(self.entrar().get("/painel/agenda")).text)


# ══════════════════════════════════════════════════════════════
#  11. Polling   ·   12. Evolution fora do ar   ·   13. Resolvidas   ·   14. Áudio
# ══════════════════════════════════════════════════════════════

class TestPolling(Base):
    def test_parcial_mostra_pendencia_criada_depois(self):
        cli = self.entrar()
        pagina = self.guardar(cli.get("/painel/pendencias?tipo=whatsapp&q=carlos")).text
        self.assertIn('data-poll-url="/painel/parcial/pendencias?aba=aguardando&amp;tipo=whatsapp&amp;q=carlos"', pagina)
        url = "/painel/parcial/pendencias?aba=aguardando&tipo=whatsapp&q=carlos"
        antes = self.guardar(cli.get(url))
        self.assertEqual(antes.status_code, 200)
        self.assertIn("Nada encontrado para essa busca.", antes.text)
        self.assertNotIn("<html", antes.text)          # fragmento, sem página inteira
        p = self.criar_wa()                             # chega uma mensagem com o painel aberto
        depois = self.guardar(cli.get(url)).text
        self.assertIn(f'href="/painel/pendencias/{p["id"]}"', depois)
        self.assertIn("Carlos Fatec", depois); self.assertIn("Urgência alta", depois)
        self.assertRegex(depois, r">(agora|há \d+ (min|h|dias?))</time>")
        self.assertEqual(self.guardar(cli.get("/painel/api/contadores")).json(),
                         {"aguardando": 1, "whatsapp": 1, "email": 0, "limpeza_aguardando": 0})
        self.assertIn("Carlos Fatec", self.guardar(cli.get("/painel/parcial/inicio")).text)
        self.assertIn("Nada encontrado", self.guardar(cli.get("/painel/parcial/pendencias?tipo=email")).text)
        self.assertIn("Carlos Fatec", self.guardar(cli.get("/painel/parcial/pendencias?q=ORCAMENTO")).text)
        self.assertEqual(self.guardar(cli.get("/painel/parcial/nada")).status_code, 404)

    def test_subtitulo_aguardando_acompanha_o_polling(self):
        """O "N aguardando" do cabeçalho de Pendências é um [data-contador] (o JS o atualiza a cada rodada)."""
        cli = self.entrar()
        sub = re.compile(r'<p class="tela-subtitulo"><span data-contador="aguardando">(\d+)</span> aguardando</p>')
        m = sub.search(self.guardar(cli.get("/painel/pendencias")).text)
        self.assertTrue(m, "subtítulo sem data-contador")
        self.assertEqual(m.group(1), "0")
        self.criar_wa()
        # mesma chave que o polling lê em /painel/api/contadores
        self.assertEqual(self.guardar(cli.get("/painel/api/contadores")).json()["aguardando"], 1)
        self.assertEqual(sub.search(self.guardar(cli.get("/painel/pendencias?tipo=email")).text).group(1), "1")

    def test_ordem_urgencia_alta_primeiro(self):
        self.criar_email()
        p = self.criar_wa()
        html = self.guardar(self.entrar().get("/painel/parcial/pendencias")).text
        self.assertLess(html.index(p["id"]), html.index("Python Bauru"))


class TestEvolutionForaDoAr(Base):
    def test_sistema_mostra_indisponivel_e_resto_funciona(self):
        p = self.criar_wa()
        os.environ["EVOLUTION_URL"] = "http://127.0.0.1:9"   # configurada, mas ninguém escuta
        cli = self.entrar()
        r = self.guardar(cli.get("/painel/sistema"))
        self.assertEqual(r.status_code, 200)
        wa = r.text.split('id="s-whatsapp"')[1].split("</section>")[0]
        self.assertIn("Indisponível", wa)
        self.assertIn("Modo simulado", wa)                    # WHATSAPP_DRY_RUN=true continua valendo
        for url in ("/painel", "/painel/pendencias", "/painel/pendencias?aba=resolvidas", f"/painel/pendencias/{p['id']}",
                    "/painel/agenda", "/painel/limpeza", "/painel/parcial/sistema", "/painel/parcial/inicio"):
            self.assertEqual(self.guardar(cli.get(url)).status_code, 200, url)
        self.assertIn("Indisponível", self.guardar(cli.get("/painel/parcial/inicio")).text)
        r = self.acao(cli, f"/painel/api/pendencias/{p['id']}/responder-texto", {"texto": "Oi"})
        self.assertEqual((r.status_code, r.json()["mensagem"]), (200, "Simulado: nada saiu da máquina."))

    def test_alfred_desligado(self):
        (TMP / "alfred_desligado").write_text("25/10 22:00\nmodo jogo\n", encoding="utf-8")
        cli = self.entrar()
        self.assertIn("Desligado desde 25/10 22:00", self.guardar(cli.get("/painel")).text)
        self.assertIn("Modo jogo desde 25/10 22:00", self.guardar(cli.get("/painel/sistema")).text)


class TestTelaQR(Base):
    def qr(self, r):
        cli = self.entrar()
        with mock.patch.object(painel.whatsapp, "obter_qrcode", lambda: r):
            return self.guardar(cli.get("/painel/whatsapp"))

    def test_mostra_o_qr_e_recarrega(self):
        r = self.qr({"ok": True, "base64": "data:image/png;base64,iVBORw0KGgo=", "pairingCode": "ABCD-1234"})
        self.assertEqual(r.status_code, 200)
        self.assertIn('src="data:image/png;base64,iVBORw0KGgo="', r.text)
        self.assertIn("ABCD-1234", r.text)
        self.assertIn('http-equiv="refresh"', r.text)

    def test_mostra_o_erro_da_evolution(self):
        r = self.qr({"ok": False, "erro": "Evolution API inacessível em http://x: recusada"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Evolution API inacessível", r.text)

    def test_conectado_nao_recarrega(self):
        r = self.qr({"ok": True, "conectado": True})
        self.assertIn("Nada a parear", r.text)
        self.assertNotIn('http-equiv="refresh"', r.text)

    def test_sem_sessao_vai_para_o_login(self):
        r = self.guardar(self.cliente().get("/painel/whatsapp", follow_redirects=False))
        self.assertEqual(r.status_code, 303)


class TestResolvidas(Base):
    def test_respondida_na_espera_lado_a_lado(self):
        ESTADO["atraso"] = 20
        r = TestClient(main.app).post("/whatsapp/webhook", json=payload("evolution_texto.json"),
                                      headers={"apikey": "CHAVE-DE-TESTE"})
        self.assertEqual(r.json()["acao"], "agendada", r.text)
        d = self.fromme()                                    # Pedro responde antes do prazo
        self.assertEqual(d["acao"], "fila_cancelada", d)
        pid = d["respondidas_na_espera"][0]
        cli = self.entrar()
        aguard = self.guardar(cli.get("/painel/pendencias")).text
        self.assertIn("Nenhuma pendência aguardando, senhor.", aguard)
        html = self.guardar(cli.get("/painel/pendencias?aba=resolvidas")).text
        self.assertIn(f'href="/painel/pendencias/{pid}"', html)
        self.assertIn("O Alfred proporia", html); self.assertIn("Você respondeu", html)
        self.assertIn(RASCUNHO, html)
        self.assertIn("Carlos, já te mando, só fechando os números.", html)
        self.assertIn("Respondida pelo celular", html)
        self.assertRegex(html, r'class="linha__tempo"[^>]*>\d{2}/\d{2}/\d{4} \d{2}:\d{2}</time>')
        det = self.guardar(cli.get(f"/painel/pendencias/{pid}")).text
        self.assertIn("Esta pendência já foi resolvida por resposta direta pelo celular.", det)
        self.assertIn("antes do aviso do Alfred", det)
        self.assertIn("Sem áudio", det)
        self.assertIn("Entrou na espera", det)
        self.assertNotIn('id="form-resposta"', det)


class TestAudio(Base):
    def test_audio_com_sessao_sem_sessao_e_expirado(self):
        p = self.criar_wa()
        url = f"/pendencias/{p['id']}/audio"
        cli = self.entrar()
        r = self.guardar(cli.get(url))
        self.assertEqual((r.status_code, r.content), (200, b"ID3fake-mp3"))
        self.assertEqual(self.guardar(self.cliente().get(url)).status_code, 401)            # sem nada
        self.assertEqual(self.cliente().get(url, headers={"Authorization": f"Bearer {TOKEN}"}).status_code, 200)
        det = self.guardar(cli.get(f"/painel/pendencias/{p['id']}")).text
        self.assertIn("<audio controls", det)
        (core.vault() / p["arquivos"]["mp3"]).unlink()      # regra dos 2 dias apagou o arquivo
        det = self.guardar(cli.get(f"/painel/pendencias/{p['id']}")).text
        self.assertIn("Áudio expirado", det)
        self.assertNotIn("<audio controls", det)
        self.assertEqual(self.guardar(cli.get(url)).status_code, 404)
        # o vigia (limpar_audios) apaga e grava arquivos.mp3=None + audio_removido_em → continua "expirado"
        (core.vault() / p["arquivos"]["mp3"]).write_bytes(b"ID3fake-mp3")
        self.assertEqual(whatsapp.limpar_audios(dias=0)["removidos"], [p["id"]])
        y = core.obter_pendencia(p["id"])
        self.assertIsNone(y["arquivos"]["mp3"]); self.assertTrue(y["audio_removido_em"])
        det = self.guardar(cli.get(f"/painel/pendencias/{p['id']}")).text
        self.assertIn("Áudio expirado", det)
        self.assertIn("Áudio de resumo apagado", det)


# ══════════════════════════════════════════════════════════════
#  15–16. CSP, nada inline, token nunca exposto
# ══════════════════════════════════════════════════════════════

class TestSemJinja2(unittest.TestCase):
    def test_api_sobe_sem_o_painel_quando_falta_o_jinja2(self):
        """Venv antigo (o run.sh só instala o requirements ao criar o venv): sem jinja2, a API sobe sem
        /painel — webhook, N8N, extensões e tools continuam; o áudio continua com Bearer."""
        tmp = Path(tempfile.mkdtemp(prefix="alfred-sem-jinja-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        script = (
            "import sys\n"
            "class Bloqueia:\n"
            "    def find_spec(self, nome, caminho=None, alvo=None):\n"
            "        if nome == 'jinja2' or nome.startswith('jinja2.'):\n"
            "            raise ModuleNotFoundError(\"No module named 'jinja2'\", name='jinja2')\n"
            "        return None\n"
            "sys.meta_path.insert(0, Bloqueia())\n"
            "import main\n"
            "from fastapi.testclient import TestClient\n"
            "assert main.painel is None, 'painel deveria estar desativado'\n"
            "c = TestClient(main.app, client=('127.0.0.1', 50000))\n"
            "print('health', c.get('/health').status_code)\n"
            "print('webhook', c.post('/whatsapp/webhook', json={}).status_code)\n"
            "print('painel', c.get('/painel').status_code)\n"
            "print('audio_sem', c.get('/pendencias/x/audio').status_code)\n"
            "print('audio_bearer', c.get('/pendencias/x/audio', headers={'Authorization': 'Bearer t-sem-jinja'}).status_code)\n"
        )
        env = {**os.environ, "VAULT_ALFRED": str(tmp / "vault"), "EVOLUTION_URL": "", "WHATSAPP_DRY_RUN": "true",
               "SHELL_EXECUTOR_TOKEN": "t-sem-jinja", "EVOLUTION_API_KEY": "CHAVE-DE-TESTE",
               "OLLAMA_BASE_URL": "http://127.0.0.1:9"}
        r = subprocess.run([sys.executable, "-c", script], cwd=str(INBOX), env=env, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Painel desativado", r.stdout)
        self.assertIn("venv/bin/pip install -r requirements.txt", r.stdout)
        saida = dict(ln.split() for ln in r.stdout.splitlines() if ln.split() and ln.split()[0] in
                     ("health", "webhook", "painel", "audio_sem", "audio_bearer"))
        self.assertEqual(saida, {"health": "200", "webhook": "401", "painel": "404", "audio_sem": "401",
                                 "audio_bearer": "404"})


class TestCabecalhos(Base):
    def test_csp_sem_inline_e_sem_token(self):
        p = self.criar_wa()
        e = self.criar_email()
        cli = self.cliente()
        paginas = [self.guardar(cli.get("/painel/login"))]
        self.entrar(cli)
        for url in ("/painel", "/painel/pendencias", "/painel/pendencias?aba=resolvidas", f"/painel/pendencias/{p['id']}",
                    f"/painel/pendencias/{e['id']}", "/painel/agenda", "/painel/limpeza", "/painel/sistema",
                    "/painel/pendencias/nao-existe"):
            paginas.append(self.guardar(cli.get(url)))
        for r in paginas:
            u = str(r.request.url)
            self.assertEqual(r.headers["content-security-policy"], painel.CSP, u)
            self.assertEqual(r.headers["x-frame-options"], "DENY", u)
            self.assertEqual(r.headers["x-content-type-options"], "nosniff", u)
            self.assertEqual(r.headers["referrer-policy"], "same-origin", u)
            self.assertEqual(r.headers["cache-control"], "no-store", u)
            for tag in re.findall(r"<script\b[^>]*>", r.text):
                self.assertIn('src="/painel/static/painel.js"', tag, u)
            self.assertEqual(r.text.count("<script"), 1, u)
            self.assertNotRegex(r.text, r"\son[a-z]+\s*=", u)
            self.assertNotRegex(r.text, r"\sstyle\s*=|<style", u)
            self.assertNotIn("javascript:", r.text, u)
        for nome, tipo in (("painel.css", "text/css"), ("painel.js", "text/javascript")):
            r = self.guardar(cli.get(f"/painel/static/{nome}"))
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.headers["content-type"].startswith(tipo))
            self.assertEqual(r.headers["cache-control"], "no-cache")
        self.assertEqual(self.guardar(cli.get("/painel/static/../main.py")).status_code, 404)
        self.assertEqual(self.guardar(cli.get("/painel/static/outro.js")).status_code, 404)
        # o token não aparece em nenhuma resposta (checado também no tearDown de todos os testes)
        self.assertNotIn(TOKEN, cli.cookies.get("alfred_painel"))


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
