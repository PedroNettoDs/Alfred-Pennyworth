"""
Testes do Poder 3 — WhatsApp (Evolution API → pendência "o que você quer responder?").

    cd services/inbox
    venv/bin/python -m unittest tests.test_whatsapp -v          # LLM/TTS/transcrição mockados (rápido, offline)
    ALFRED_TESTE_LIVE=1 venv/bin/python -m unittest tests.test_whatsapp -v   # usa o Ollama + edge-tts de verdade

Nunca chama a Evolution API: EVOLUTION_URL fica vazio → envios simulados em logs/whatsapp_enviados.jsonl
(aqui redirecionado para um diretório temporário). O vault também é temporário.
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).parent
INBOX = AQUI.parent
sys.path.insert(0, str(INBOX))
sys.path.insert(0, str(INBOX.parent))

LIVE = os.getenv("ALFRED_TESTE_LIVE", "").lower() in ("1", "true", "yes")
TMP = Path(tempfile.mkdtemp(prefix="alfred-wa-test-"))

# Ambiente ANTES de importar os módulos (main.py lê SHELL_EXECUTOR_TOKEN na importação)
os.environ.update({
    "VAULT_ALFRED": str(TMP / "vault"),
    "EVOLUTION_URL": "",                       # → simulado
    "EVOLUTION_API_KEY": "CHAVE-DE-TESTE",
    "EVOLUTION_INSTANCE": "alfred",
    "SHELL_EXECUTOR_TOKEN": "token-de-teste",
    "WHATSAPP_NUMERO_PEDRO": "5514999990000",
    "WHATSAPP_DRY_RUN": "true",
    "WHATSAPP_DEFAULT": "pedro_whatsapp",
    "INBOX_AUTOPLAY": "false",
})
if not LIVE:
    os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:9")   # nada deve chegar ao Ollama

import core        # noqa: E402
import whatsapp    # noqa: E402
import main        # noqa: E402
from _shared import lib_db, lib_whatsapp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PAYLOADS = AQUI / "payloads"
_CARREGAR_CFG = whatsapp.carregar_cfg
AGUARDADORES: list = []   # processos de notificação que teriam sido abertos (core._iniciar_aguardador)
TEXTO_AUDIO = "Oi Pedro, aqui é o Carlos da Fatec. Você consegue me mandar o orçamento do servidor até sexta-feira?"


def payload(nome: str, ref: str = "") -> dict:
    raw = (PAYLOADS / nome).read_text(encoding="utf-8")
    if ref:
        raw = raw.replace("__REF__", ref)
    return json.loads(raw)


# ── mocks do LLM / TTS / transcrição ──────────────────────────

def _fake_classificar(prompt, **kw):
    if "RASCUNHO da resposta" in prompt:
        return {"rascunho": "Recebi, Carlos! Vou ver o orçamento e te retorno.",
                "compromisso": {"titulo": "Entregar orçamento — Fatec", "data": "2026-10-23", "hora": ""}}
    texto = prompt.lower()
    if "sorteado" in texto or "bit.ly" in texto:
        return {"categoria": "spam", "confianca": 0.95, "requer_resposta": False, "urgencia": "baixa",
                "resumo": "número desconhecido com link de prêmio", "motivo": "golpe"}
    if "obrigada pela carona" in texto:
        return {"categoria": "pessoal", "confianca": 0.9, "requer_resposta": False, "urgencia": "baixa",
                "resumo": "Juliana agradece a carona", "motivo": "agradecimento"}
    return {"categoria": "profissional", "confianca": 0.9, "requer_resposta": True, "urgencia": "alta",
            "resumo": "Carlos da Fatec pede o orçamento do servidor até sexta", "motivo": "pedido com prazo"}


def _fake_generate(prompt, **kw):
    if "recado de VOZ" in prompt:
        return "Pedro, o Carlos da Fatec pediu o orçamento do servidor até sexta-feira. O que você quer responder?"
    return ("## Quem mandou e o que quer\nCarlos, da Fatec, pede o orçamento do servidor até sexta.\n\n"
            "## Minha leitura\nParece legítimo e tem prazo.\n\n"
            "## O que você quer responder?\nResponda com o texto que devo enviar, ou **ignorar**.")


def _fake_tts(texto, voice, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(b"ID3fake-mp3")
    return True


def _fake_transcrever(fonte, mimetype="", **kw):
    assert isinstance(fonte, bytes) and len(fonte) > 1000, "transcrever_audio deve receber os bytes do OGG"
    assert "ogg" in mimetype
    return {"texto": TEXTO_AUDIO, "metodo": "mock", "duracao_s": 7.7, "segmentos": 1, "cortado": False, "erro": None}


class Base(unittest.TestCase):
    ATRASO = 0   # atraso_minutos usado nos testes (perfis.yml tem 20): 0 = pendência na hora, como antes da fila

    @classmethod
    def setUpClass(cls):
        (TMP / "vault").mkdir(parents=True, exist_ok=True)

        def cfg_de_teste(nome=None):
            n, cfg = _CARREGAR_CFG(nome)
            return n, {**cfg, "atraso_minutos": cls.ATRASO}

        cls.patches = [
            mock.patch.object(whatsapp, "carregar_cfg", cfg_de_teste),
            mock.patch.object(whatsapp, "FILA_FILE", TMP / "whatsapp_fila.json"),
            mock.patch.object(whatsapp, "SEEN_FILE", TMP / "whatsapp_seen.json"),
            mock.patch.object(lib_whatsapp, "ENVIADOS_FILE", TMP / "whatsapp_enviados.jsonl"),
            # ids enviados pelo Alfred (eco): nunca o logs/ do repositório
            mock.patch.object(lib_whatsapp, "IDS_ENVIADOS_FILE", TMP / "whatsapp_ids_enviados.json"),
            mock.patch.object(core, "_notify", lambda *a, **k: None),
            mock.patch.object(core, "_iniciar_aguardador", lambda comando, pid: AGUARDADORES.append((comando, pid))),
        ]
        if not LIVE:
            cls.patches += [
                mock.patch.object(whatsapp, "ollama_generate_json", _fake_classificar),
                mock.patch.object(whatsapp, "ollama_generate", _fake_generate),
                mock.patch.object(whatsapp, "tts_save", _fake_tts),
                mock.patch.object(whatsapp, "transcrever_audio", _fake_transcrever),
            ]
        for p in cls.patches:
            p.start()
        cls.client = TestClient(main.app)
        cls.auth = {"Authorization": "Bearer token-de-teste"}

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        # vault e registros limpos a cada teste
        for f in (TMP / "vault" / "pendencias").glob("*") if (TMP / "vault" / "pendencias").is_dir() else []:
            f.unlink()
        for f in (TMP / "whatsapp_seen.json", TMP / "whatsapp_enviados.jsonl", TMP / "whatsapp_fila.json",
                  TMP / "whatsapp_ids_enviados.json"):
            if f.exists():
                f.unlink()

    # helpers
    def post_webhook(self, nome, ref="", headers=None, **query):
        h = headers if headers is not None else {"apikey": "CHAVE-DE-TESTE"}
        return self.client.post("/whatsapp/webhook", params=query, json=payload(nome, ref), headers=h)

    def pendencias(self, status="aguardando"):
        return [p for p in core.listar_pendencias(status) if p.get("tipo") == "whatsapp"]

    def enviados(self):
        return lib_whatsapp.ultimos_envios(50)


class TestPayloads(Base):
    def test_normalizar_texto(self):
        m = whatsapp.normalizar_payload(payload("evolution_texto.json"))
        self.assertEqual(m["tipo_msg"], "texto")
        self.assertEqual(m["numero"], "5514988887777")
        self.assertEqual(m["nome"], "Carlos Fatec")
        self.assertFalse(m["from_me"]); self.assertFalse(m["grupo"])
        self.assertIn("orçamento", m["texto"])
        self.assertEqual(m["dono"], "5514900000000")

    def test_normalizar_audio(self):
        m = whatsapp.normalizar_payload(payload("evolution_audio.json"))
        self.assertEqual(m["tipo_msg"], "audio")
        self.assertTrue(m["audio"]["base64"])
        self.assertEqual(m["audio"]["segundos"], 8)
        self.assertTrue(m["audio"]["ptt"])
        ogg = base64.b64decode(m["audio"]["base64"])
        self.assertEqual(ogg[:4], b"OggS")

    def test_normalizar_citacao(self):
        m = whatsapp.normalizar_payload(payload("evolution_resposta_pedro.json", ref="20261019-x"))
        self.assertIsNotNone(m["citacao"])
        self.assertIn("ref: 20261019-x", m["citacao"]["texto"])
        self.assertEqual(m["citacao"]["stanza_id"], "SIMULADO-PERGUNTA")

    def test_evento_desconhecido_ignorado(self):
        p = payload("evolution_texto.json"); p["event"] = "connection.update"
        r = whatsapp.processar_mensagem_whatsapp(p, enviar=False)
        self.assertEqual(r["acao"], "ignorada"); self.assertIn("connection.update", r["motivo"])


class TestWebhook(Base):
    def test_auth_obrigatoria(self):
        sem_chave = payload("evolution_texto.json"); sem_chave["apikey"] = "outra-instancia"
        self.assertEqual(self.client.post("/whatsapp/webhook", json=sem_chave).status_code, 401)
        self.assertEqual(self.client.post("/whatsapp/webhook", json=sem_chave, headers={"apikey": "errada"}).status_code, 401)
        self.assertEqual(self.client.post("/whatsapp/webhook", json=sem_chave, headers={"Authorization": "Bearer x"}).status_code, 401)
        # campo apikey do payload (token da instância = EVOLUTION_API_KEY) vale sozinho
        p = payload("evolution_texto.json"); self.assertEqual(p["apikey"], "CHAVE-DE-TESTE")
        r = self.client.post("/whatsapp/webhook", params={"enviar": "false"}, json=p)
        self.assertEqual(r.status_code, 200, r.text)
        # Bearer do SHELL_EXECUTOR_TOKEN (N8N no meio) também
        self.setUp()
        r = self.client.post("/whatsapp/webhook", params={"enviar": "false"}, json=payload("evolution_texto.json"), headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)

    def test_texto_cria_pendencia(self):
        r = self.post_webhook("evolution_texto.json", enviar="false")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["acao"], "pendencia_criada", d)
        self.assertEqual(d["classificacao"]["categoria"], "profissional")
        p = d["pendencia"]
        self.assertEqual(p["tipo"], "whatsapp"); self.assertEqual(p["status"], "aguardando")
        self.assertIsNone(p["resposta_texto"])
        self.assertEqual(p["whatsapp"]["numero"], "5514988887777")
        self.assertEqual(p["whatsapp"]["message_id"], "3EB0A1B2C3D4E5F60001")
        # arquivos no vault
        md = core.vault() / p["arquivos"]["md"]
        self.assertTrue(md.is_file())
        txt = md.read_text(encoding="utf-8")
        self.assertIn("tipo: whatsapp", txt); self.assertIn("O que você quer responder?", txt)
        self.assertIn("## Mensagem original", txt); self.assertIn(f"responder-texto {p['id']}", txt)
        self.assertTrue((core.vault() / p["arquivos"]["mp3"]).is_file())
        # pendencias.yml com o mesmo esquema
        y = core.obter_pendencia(p["id"])
        self.assertEqual(y["status"], "aguardando")
        for k in ("id", "tipo", "titulo", "criado_em", "evento", "arquivos", "resposta_links", "lembrado_em",
                  "respondido_em", "resposta", "resposta_texto", "nota", "whatsapp"):
            self.assertIn(k, y)
        # notificação para o N8N mandar a Pedro
        n = d["notificacao"]
        self.assertEqual(n["numero"], "5514999990000")
        self.assertTrue(n["texto"].rstrip().endswith(f"ref: {p['id']}"))
        self.assertIn("O que você quer responder?", n["texto"])
        self.assertEqual(n["audio_url"], f"/pendencias/{p['id']}/audio")
        # GET /pendencias/{id}/audio serve o mp3 da pergunta
        a = self.client.get(n["audio_url"], headers=self.auth)
        self.assertEqual(a.status_code, 200); self.assertTrue(len(a.content) > 5)
        # nada foi enviado (enviar=false)
        self.assertEqual(self.enviados(), [])

    def test_audio_transcreve_e_cria_pendencia(self):
        r = self.post_webhook("evolution_audio.json", enviar="false")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["acao"], "pendencia_criada", d)
        self.assertTrue(d["transcricao"]["texto"], d["transcricao"])
        if not LIVE:
            self.assertEqual(d["transcricao"]["metodo"], "mock")
        else:
            self.assertEqual(d["transcricao"]["metodo"], "ollama")
            self.assertIn("orçamento", d["transcricao"]["texto"].lower())
        p = d["pendencia"]
        self.assertEqual(p["whatsapp"]["tipo_msg"], "audio")
        self.assertIn("orçamento", p["whatsapp"]["texto"].lower())
        self.assertEqual(p["whatsapp"]["transcricao"]["metodo"], d["transcricao"]["metodo"])
        self.assertIn("áudio de 8s", (core.vault() / p["arquivos"]["md"]).read_text(encoding="utf-8"))

    def test_duplicada(self):
        self.assertEqual(self.post_webhook("evolution_texto.json", enviar="false").json()["acao"], "pendencia_criada")
        self.assertEqual(self.post_webhook("evolution_texto.json", enviar="false").json()["acao"], "duplicada")
        self.assertEqual(len(self.pendencias()), 1)

    def test_grupo_spam_e_sem_resposta_nao_viram_pendencia(self):
        for nome, trecho in (("evolution_grupo.json", "grupo"), ("evolution_spam.json", "spam"),
                             ("evolution_pessoal_sem_resposta.json", "não pede resposta")):
            d = self.post_webhook(nome, enviar="false").json()
            self.assertEqual(d["acao"], "ignorada", (nome, d))
            self.assertIn(trecho, d["motivo"], (nome, d))
        self.assertEqual(self.pendencias(), [])

    def cfg_com(self, **extra):
        """carregar_cfg do teste com chaves do perfis.yml sobrepostas."""
        base = whatsapp.carregar_cfg

        def cfg(nome=None):
            n, c = base(nome)
            return n, {**c, **extra}
        return mock.patch.object(whatsapp, "carregar_cfg", cfg)

    def audio_de_grupo(self, mid, mencoes=()):
        p = payload("evolution_audio.json")
        p["data"]["key"].update(id=mid, remoteJid="120363012345678901@g.us",
                                participant="5514988887777@s.whatsapp.net")
        if mencoes:
            p["data"]["message"]["audioMessage"]["contextInfo"] = {"mentionedJid": list(mencoes)}
        return p

    def test_audio_descartado_sem_texto_nao_e_transcrito(self):
        # grupo do condomínio mandando áudio: a heurística descarta sem texto → nada de gemma4
        transcrever = mock.Mock(side_effect=_fake_transcrever)
        casos = (("grupos: ignorar", self.audio_de_grupo("AUDIO-GRUPO-1"), {"grupos": "ignorar"}, "grupo"),
                 ("grupos: mencao sem Pedro", self.audio_de_grupo("AUDIO-GRUPO-2"), {"grupos": "mencao"}, "grupo"),
                 ("contatos_ignorar", payload("evolution_audio.json"), {"contatos_ignorar": ["Carlos"]}, "ignorado"))
        with mock.patch.object(whatsapp, "transcrever_audio", transcrever):
            for nome, p, extra, categoria in casos:
                with self.subTest(nome), self.cfg_com(**extra):
                    post = lambda: self.client.post("/whatsapp/webhook", params={"enviar": "false"}, json=p,
                                                    headers={"apikey": "CHAVE-DE-TESTE"}).json()
                    d = post()
                    self.assertEqual((d["acao"], d["classificacao"]["categoria"]), ("ignorada", categoria), d)
                    self.assertIsNone(d["transcricao"])
                    self.assertEqual(post()["acao"], "duplicada")   # continua marcada em whatsapp_seen.json
            transcrever.assert_not_called()

            # Pedro mencionado no grupo (grupos: mencao) pode virar pendência → transcreve
            with self.cfg_com(grupos="mencao"):
                d = self.client.post("/whatsapp/webhook", params={"enviar": "false"},
                                     json=self.audio_de_grupo("AUDIO-GRUPO-3", ["5514999990000@s.whatsapp.net"]),
                                     headers={"apikey": "CHAVE-DE-TESTE"}).json()
        self.assertEqual(transcrever.call_count, 1)
        self.assertEqual(d["acao"], "pendencia_criada", d)
        self.assertIn("mencionou Pedro", d["classificacao"]["resumo"])
        self.assertEqual(len(self.pendencias()), 1)

    def test_agrupa_segunda_mensagem_do_contato(self):
        d1 = self.post_webhook("evolution_texto.json", enviar="false").json()
        d2 = self.post_webhook("evolution_audio.json", enviar="false").json()
        self.assertEqual(d2["acao"], "agrupada", d2)
        self.assertEqual(d2["pendencia"]["id"], d1["pendencia"]["id"])
        self.assertEqual(len(d2["pendencia"]["whatsapp"]["mensagens"]), 2)
        self.assertIn("mais uma mensagem", d2["notificacao"]["texto"])
        self.assertIsNone(d2["notificacao"]["audio_url"])
        self.assertEqual(len(self.pendencias()), 1)

    def test_nao_agrupa_em_pendencia_resolvida_por_outro_processo_no_meio(self):
        """O webhook decide agrupar com uma leitura feita antes da classificação; se no meio dela outro caminho
        (notificação no PC, CLI) responde a pendência, a mensagem nova vira pendência nova — não some."""
        pid = self.post_webhook("evolution_texto.json", enviar="false").json()["pendencia"]["id"]
        original = whatsapp.classificar_whatsapp

        def classificar_e_responder_no_meio(*a, **kw):
            r = original(*a, **kw)
            whatsapp.responder_texto(pid, "Ok, combinado.", enviar=False, origem="via notificação no PC")
            return r

        with mock.patch.object(whatsapp, "classificar_whatsapp", classificar_e_responder_no_meio):
            d2 = self.post_webhook("evolution_audio.json", enviar="false").json()
        self.assertEqual(d2["acao"], "pendencia_criada", d2)
        self.assertNotEqual(d2["pendencia"]["id"], pid)
        p = core.obter_pendencia(pid)
        self.assertEqual(p["status"], "respondida")
        self.assertEqual([m["message_id"] for m in p["whatsapp"]["mensagens"]], ["3EB0A1B2C3D4E5F60001"])
        self.assertEqual([x["id"] for x in self.pendencias()], [d2["pendencia"]["id"]])
        # agrupar_mensagem direto numa pendência já resolvida: None e nada gravado
        msg = whatsapp.normalizar_payload(payload("evolution_texto.json"))
        self.assertIsNone(whatsapp.agrupar_mensagem(pid, msg, "mais uma"))
        self.assertEqual(len(core.obter_pendencia(pid)["whatsapp"]["mensagens"]), 1)

    def test_resposta_de_pedro_citando_ref(self):
        pid = self.post_webhook("evolution_texto.json", enviar="false").json()["pendencia"]["id"]
        d = self.post_webhook("evolution_resposta_pedro.json", ref=pid)
        self.assertEqual(d.status_code, 200, d.text)
        d = d.json()
        self.assertEqual(d["acao"], "resposta_registrada", d)
        p = core.obter_pendencia(pid)
        self.assertEqual(p["status"], "respondida")
        self.assertIn("Mando o orçamento até quinta", p["resposta_texto"])
        self.assertEqual(p["resposta"], "texto")
        self.assertTrue(p["whatsapp"]["envio"]["simulado"])
        self.assertEqual(p["whatsapp"]["envio"]["destino"], "5514988887777")
        env = self.enviados()
        self.assertEqual(len(env), 1)
        self.assertEqual(env[0]["numero"], "5514988887777")
        self.assertIn("Mando o orçamento", env[0]["conteudo"]); self.assertTrue(env[0]["quoted"])
        self.assertIn("Enviado a", d["notificacao"]["texto"]); self.assertEqual(d["notificacao"]["numero"], "5514999990000")
        self.assertIn("## Resposta", (core.vault() / p["arquivos"]["md"]).read_text(encoding="utf-8"))
        # responder de novo → já respondida
        d2 = self.post_webhook("evolution_resposta_pedro.json", ref=pid).json()
        self.assertEqual(d2["acao"], "ignorada"); self.assertIn("respondida", d2["motivo"])

    def test_resposta_ignorar(self):
        pid = self.post_webhook("evolution_texto.json", enviar="false").json()["pendencia"]["id"]
        d = self.post_webhook("evolution_resposta_ignorar.json", ref=pid).json()
        self.assertEqual(d["acao"], "pendencia_ignorada", d)
        self.assertEqual(core.obter_pendencia(pid)["status"], "ignorada")
        self.assertEqual(self.enviados(), [])

    def test_pedro_responde_direto_do_celular(self):
        pid = self.post_webhook("evolution_texto.json", enviar="false").json()["pendencia"]["id"]
        d = self.post_webhook("evolution_fromme_direto.json").json()
        self.assertEqual(d["acao"], "respondida_manual", d)
        p = core.obter_pendencia(pid)
        self.assertEqual(p["status"], "respondida_manual")
        self.assertIn("já te mando", p["resposta_texto"])
        self.assertEqual(self.enviados(), [])

    def test_eco_do_proprio_envio_ignorado(self):
        pid = self.post_webhook("evolution_texto.json", enviar="false").json()["pendencia"]["id"]
        self.post_webhook("evolution_resposta_pedro.json", ref=pid)
        mid = core.obter_pendencia(pid)["whatsapp"]["envio"]["message_id"]
        eco = payload("evolution_fromme_direto.json"); eco["data"]["key"]["id"] = mid
        d = self.post_webhook("evolution_fromme_direto.json")  # outra mensagem fromMe sem pendência aberta
        self.assertEqual(d.json()["acao"], "ignorada")
        d = self.client.post("/whatsapp/webhook", json=eco, headers={"apikey": "CHAVE-DE-TESTE"}).json()
        self.assertEqual(d["acao"], "ignorada"); self.assertIn("eco", d["motivo"])

    def test_notificar_pelo_python(self):
        d = self.post_webhook("evolution_texto.json", notificar="true").json()
        self.assertEqual(d["acao"], "pendencia_criada")
        env = self.enviados()
        self.assertEqual([e["tipo"] for e in env], ["texto", "audio"])
        self.assertEqual(env[0]["numero"], "5514999990000")
        self.assertIn(f"ref: {d['pendencia']['id']}", env[0]["conteudo"])
        self.assertTrue(core.obter_pendencia(d["pendencia"]["id"])["whatsapp"]["pergunta_msg_id"])

    def test_webhook_async(self):
        r = self.client.post("/whatsapp/webhook-async", params={"enviar": "false"},
                             json=payload("evolution_texto.json"), headers={"apikey": "CHAVE-DE-TESTE"})
        self.assertEqual(r.status_code, 202, r.text)
        # TestClient executa as background tasks antes de devolver
        self.assertEqual(len(self.pendencias()), 1)


class TestEvolutionV237(Base):
    """Formato real da Evolution v2.3.7 (validação com WhatsApp real, 2026-10-19) — instância no número de Pedro."""

    def test_normalizar_contextinfo_fora_de_message(self):
        m = whatsapp.normalizar_payload(payload("evolution_v237_texto.json"))
        self.assertEqual((m["numero"], m["tipo_msg"], m["dono"]), ("5514988887777", "texto", "5514999990000"))
        self.assertIsNone(m["citacao"])
        m = whatsapp.normalizar_payload(payload("evolution_v237_resposta_pedro.json", ref="20261019-x"))
        self.assertTrue(m["from_me"])
        self.assertEqual(m["citacao"]["stanza_id"], "3EB05F4E8EF5B8E031816C")
        self.assertIn("ref: 20261019-x", m["citacao"]["texto"])

    def test_grupo_participante_lid(self):
        m = whatsapp.normalizar_payload(payload("evolution_v237_grupo.json"))
        self.assertTrue(m["grupo"])
        self.assertEqual(m["participante"], "5514977776666")
        self.assertEqual(self.post_webhook("evolution_v237_grupo.json", enviar="false").json()["acao"], "ignorada")

    def test_fluxo_texto_resposta_citada_no_proprio_numero(self):
        pid = self.post_webhook("evolution_v237_texto.json", enviar="false").json()["pendencia"]["id"]
        d = self.post_webhook("evolution_v237_resposta_pedro.json", ref=pid).json()
        self.assertEqual(d["acao"], "resposta_registrada", d)
        p = core.obter_pendencia(pid)
        self.assertEqual((p["status"], p["resposta_texto"]), ("respondida", "Claro, estou na execução dele"))
        env = self.enviados()
        self.assertEqual((env[0]["numero"], env[0]["quoted"]), ("5514988887777", True))

    def test_ignorar_e_resposta_direta_citando_o_contato(self):
        pid = self.post_webhook("evolution_v237_texto.json", enviar="false").json()["pendencia"]["id"]
        self.assertEqual(self.post_webhook("evolution_v237_resposta_ignorar.json", ref=pid).json()["acao"], "pendencia_ignorada")
        self.assertEqual(core.obter_pendencia(pid)["status"], "ignorada")
        # nova pendência; Pedro responde o Carlos citando a mensagem dele (sem ref) → respondida_manual
        p2 = payload("evolution_v237_texto.json"); p2["data"]["key"]["id"] = "OUTRA-MENSAGEM-DO-CARLOS"
        pid2 = self.client.post("/whatsapp/webhook", params={"enviar": "false"}, json=p2,
                                headers={"apikey": "CHAVE-DE-TESTE"}).json()["pendencia"]["id"]
        d = self.post_webhook("evolution_v237_fromme_direto.json").json()
        self.assertEqual(d["acao"], "respondida_manual", d)
        self.assertEqual(core.obter_pendencia(pid2)["resposta_texto"], "Tenho horário para as 16:00, pode ser?")
        self.assertEqual(self.enviados(), [])

    def test_audio_sem_base64_baixa_com_mensagem_original(self):
        ogg_b64 = base64.b64encode((AQUI / "fixtures" / "audio_carlos_fatec.ogg").read_bytes()).decode()
        chamadas = []

        def fake_baixar(key, message=None):
            chamadas.append((key, message))
            return {"ok": True, "base64": ogg_b64, "mimetype": "audio/ogg; codecs=opus"}

        with mock.patch.object(whatsapp, "baixar_midia", fake_baixar):
            d = self.post_webhook("evolution_v237_audio_sem_base64.json", enviar="false").json()
        self.assertEqual(d["acao"], "pendencia_criada", d)
        self.assertTrue(d["transcricao"]["texto"])
        key, message = chamadas[0]
        self.assertEqual(key["id"], "A56164FA6A523054ADF7EEDAF6881119")
        self.assertEqual(len(message["audioMessage"]["mediaKey"]), 32)   # bloco original, não só url/mimetype

    def test_baixar_midia_sem_message_vazio(self):
        corpos = []
        with mock.patch.object(lib_whatsapp, "_post", lambda path, body, timeout=60: corpos.append(body) or {"ok": False}), \
             mock.patch.object(lib_whatsapp, "configurado", lambda: True):
            lib_whatsapp.baixar_midia({"id": "X1", "remoteJid": "1@s.whatsapp.net"})
            lib_whatsapp.baixar_midia({"id": "X2"}, {"audioMessage": {"mediaKey": {"0": 1}}})
        self.assertNotIn("message", corpos[0]["message"])   # {} faria a Evolution falhar ("empty media key")
        self.assertIn("audioMessage", corpos[1]["message"]["message"])

    def test_mensagens_simultaneas_nao_perdem_pendencia(self):
        import threading
        import time

        def lento(prompt, **kw):
            time.sleep(0.3)
            return _fake_generate(prompt, **kw)

        p2 = payload("evolution_v237_texto.json")
        p2["data"]["key"].update(id="MSG-DE-OUTRO", remoteJid="5511911112222@s.whatsapp.net",
                                 remoteJidAlt="5511911112222@s.whatsapp.net")
        p2["data"]["pushName"] = "Outro Contato"
        ctx = mock.patch.object(whatsapp, "ollama_generate", lento) if not LIVE else mock.MagicMock()
        with ctx:
            ts_ = [threading.Thread(target=whatsapp.processar_mensagem_whatsapp, args=(p,), kwargs={"enviar": False})
                   for p in (payload("evolution_v237_texto.json"), p2)]
            [t.start() for t in ts_]
            [t.join() for t in ts_]
        self.assertEqual(len(self.pendencias()), 2)


class TestNotificacaoPC(Base):
    """Notificação no PC: Enviar (rascunho editável) / Não enviar / Agenda (tasks.yml + lembrete + Google Agenda)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import importlib.util
        spec = importlib.util.spec_from_file_location("inbox_assistant", INBOX / "inbox-assistant.py")
        cls.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cli)

    def setUp(self):
        super().setUp()
        AGUARDADORES.clear()
        (TMP / "vault" / "tasks.yml").unlink(missing_ok=True)

    def _cria(self):
        return self.post_webhook("evolution_texto.json", enviar="false").json()["pendencia"]

    def _clica(self, *escolhas, caixas=()):
        """Roda o aguardador com a GUI simulada: `escolhas` = botões clicados em ordem, `caixas` = textos digitados."""
        esc, cx = list(escolhas), list(caixas)
        with mock.patch.object(self.cli, "_menu_whatsapp_gui", lambda *a, **k: esc.pop(0) if esc else ""), \
             mock.patch.object(self.cli, "_caixa_texto_gui", lambda *a, **k: cx.pop(0) if cx else None), \
             mock.patch.object(self.cli, "_dormir_ate", lambda *a: None), \
             mock.patch.object(self.cli.shutil, "which", lambda nome: None):
            self.cli.cmd_aguardar_whatsapp(mock.Mock(id=self.pid))

    def test_pendencia_tem_rascunho_e_abre_notificacao_com_botoes(self):
        p = self._cria()
        self.assertIn("Vou ver o orçamento", p["whatsapp"]["rascunho"])
        self.assertEqual(p["whatsapp"]["compromisso"]["data"], "2026-10-23")
        self.assertIn("## Sugestão de resposta", (core.vault() / p["arquivos"]["md"]).read_text(encoding="utf-8"))
        self.assertEqual(AGUARDADORES, [("_aguardar-whatsapp", p["id"])])

    def test_enviar_rascunho_editado(self):
        self.pid = self._cria()["id"]
        self._clica("ouvir", "enviar", caixas=["Recebi, Carlos! Mando até quinta."])
        p = core.obter_pendencia(self.pid)
        self.assertEqual((p["status"], p["resposta_texto"]), ("respondida", "Recebi, Carlos! Mando até quinta."))
        self.assertEqual(self.enviados()[-1]["numero"], "5514988887777")

    def test_enviar_cancelado_volta_e_nao_enviar_ignora(self):
        self.pid = self._cria()["id"]
        self._clica("enviar", "nao", caixas=[None])
        self.assertEqual(core.obter_pendencia(self.pid)["status"], "ignorada")
        self.assertEqual(self.enviados(), [])

    def test_agenda_cria_tarefa_e_lembrete(self):
        self.pid = self._cria()["id"]
        self._clica("agenda", caixas=["xyz", "23/10 14:00"])
        p = core.obter_pendencia(self.pid)
        self.assertEqual(p["status"], "aguardando")
        self.assertTrue(p["whatsapp"]["lembrar_em"].startswith("2026-10-23T14:00") or p["whatsapp"]["lembrar_em"].startswith("2027-10-23T14:00"))
        import yaml
        tasks = yaml.safe_load((core.vault() / "tasks.yml").read_text(encoding="utf-8"))["tasks"]
        self.assertEqual(tasks[-1]["title"], "Entregar orçamento — Fatec — 14:00")
        self.assertIn("whatsapp", tasks[-1]["tags"])
        self.assertIn("## Agenda", (core.vault() / p["arquivos"]["md"]).read_text(encoding="utf-8"))

    def test_respondida_por_outro_caminho_fecha_a_notificacao(self):
        self.pid = self._cria()["id"]
        whatsapp.responder_texto(self.pid, "ignorar")
        self._clica("enviar", caixas=["não deveria sair"])
        self.assertEqual(self.enviados(), [])

    def test_interpretar_quando_e_google_url(self):
        from datetime import datetime
        agora = datetime(2026, 10, 19, 16, 40).astimezone()
        casos = {"20/10 14:00": "20/10/2026 14:00", "amanhã 10h": "20/10/2026 10:00", "terça 15h": "20/10/2026 15:00",
                 "2026-10-21 09:30": "21/10/2026 09:30", "01/01": "01/01/2027 09:00", "16:00": "20/10/2026 16:00",   # só hora, já passou → amanhã
                 "17:00": "19/10/2026 17:00"}
        for txt, esperado in casos.items():
            self.assertEqual(whatsapp.interpretar_quando(txt, agora)[0].strftime("%d/%m/%Y %H:%M"), esperado, txt)
        self.assertIsNone(whatsapp.interpretar_quando("sei lá", agora))
        url = whatsapp.google_agenda_url("Reunião", datetime(2026, 10, 21, 18, 0), True)
        self.assertIn("dates=20261021T180000%2F20261021T190000", url)
        self.assertIn("dates=20261021%2F20261022", whatsapp.google_agenda_url("x", datetime(2026, 10, 21), False))


class TestResponderTexto(Base):
    def _cria(self):
        return self.post_webhook("evolution_texto.json", enviar="false").json()["pendencia"]["id"]

    def test_endpoint_registra_e_envia(self):
        pid = self._cria()
        r = self.client.post(f"/pendencias/{pid}/responder-texto", json={"texto": "Carlos, mando amanhã cedo."}, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        p = r.json()
        self.assertEqual(p["status"], "respondida"); self.assertEqual(p["resposta_texto"], "Carlos, mando amanhã cedo.")
        self.assertTrue(p["whatsapp"]["envio"]["simulado"])
        self.assertEqual(self.enviados()[0]["numero"], "5514988887777")
        # sem auth
        self.assertEqual(self.client.post(f"/pendencias/{pid}/responder-texto", json={"texto": "x"}).status_code, 401)

    def test_endpoint_erros(self):
        self.assertEqual(self.client.post("/pendencias/nao-existe/responder-texto", json={"texto": "x"}, headers=self.auth).status_code, 404)
        pid = self._cria()
        self.assertEqual(self.client.post(f"/pendencias/{pid}/responder-texto", json={"texto": "   "}, headers=self.auth).status_code, 400)
        # pendência de e-mail não aceita texto livre / whatsapp não aceita "vou"
        self.assertEqual(self.client.post(f"/pendencias/{pid}/responder", json={"resposta": "vou"}, headers=self.auth).status_code, 400)

    def test_funcao_sem_enviar_e_ignorar(self):
        pid = self._cria()
        p = whatsapp.responder_texto(pid, "Ok, combinado.", enviar=False)
        self.assertEqual(p["status"], "respondida"); self.assertEqual(self.enviados(), [])
        pid2 = self.post_webhook("evolution_audio.json", enviar="false").json()["pendencia"]["id"] if False else None
        self.setUp()
        pid3 = self._cria()
        p = whatsapp.responder_texto(pid3, "ignorar")
        self.assertEqual(p["status"], "ignorada"); self.assertIsNone(p["resposta_texto"])

    def test_envio_falho_mantem_aguardando(self):
        pid = self._cria()
        with mock.patch.object(whatsapp, "enviar_texto", lambda *a, **k: {"ok": False, "simulado": False, "erro": "HTTP 500"}):
            r = self.client.post(f"/pendencias/{pid}/responder-texto", json={"texto": "tenta"}, headers=self.auth)
        self.assertEqual(r.status_code, 502, r.text)
        p = core.obter_pendencia(pid)
        self.assertEqual(p["status"], "aguardando"); self.assertEqual(p["resposta_texto"], "tenta")
        self.assertEqual(p["whatsapp"]["envio"]["erro"], "HTTP 500")
        self.assertIsNone(p["respondido_via"])           # voltou a aguardar: não foi resolvida por ninguém
        # repetir funciona
        r = self.client.post(f"/pendencias/{pid}/responder-texto", json={"texto": "tenta"}, headers=self.auth)
        self.assertEqual(r.status_code, 200)

    def test_respondido_via_e_ja_resolvida(self):
        pid = self._cria()
        p = whatsapp.responder_texto(pid, "Ok, combinado.", enviar=True, origem="via painel")
        self.assertEqual((p["status"], p["respondido_via"]), ("respondida", "painel"))
        self.assertEqual(core.obter_pendencia(pid)["respondido_via"], "painel")
        with self.assertRaises(core.PendenciaJaResolvida) as cm:
            whatsapp.responder_texto(pid, "de novo", origem="via terminal")
        e = cm.exception
        self.assertIsInstance(e, ValueError)                     # quem já tratava ValueError continua igual
        self.assertEqual(str(e), f"Pendência {pid} já está com status 'respondida'")
        self.assertEqual((e.pid, e.status, e.caminho), (pid, "respondida", "Painel do Alfred"))
        with self.assertRaises(core.PendenciaJaResolvida):
            whatsapp.agendar_pendencia(pid, core._now() + timedelta(days=1))
        self.assertEqual(len(self.enviados()), 1)
        # API devolve 400 com a mesma mensagem de antes
        r = self.client.post(f"/pendencias/{pid}/responder-texto", json={"texto": "x"}, headers=self.auth)
        self.assertEqual(r.status_code, 400); self.assertIn("já está com status", r.json()["detail"])

    def test_responder_email_pela_api_grava_respondido_via(self):
        with core.trava_pendencias():
            data = core._load_pend()
            data["pendencias"].append({"id": "20261020-meetup-api", "tipo": "evento", "status": "aguardando",
                                       "titulo": "Meetup", "evento": {"data_inicio": None}, "arquivos": {}})
            core._save_pend(data)
        r = self.client.post("/pendencias/20261020-meetup-api/responder", json={"resposta": "nao_vou"}, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        p = core.obter_pendencia("20261020-meetup-api")
        self.assertEqual((p["status"], p["respondido_via"]), ("nao_vou", "API"))
        self.assertEqual(core.caminho_resolucao(p), "API (N8N, chat ou extensão)")

    def test_status_repassa_timeout(self):
        chamadas = []
        with mock.patch.object(lib_whatsapp, "configurado", lambda: True), \
             mock.patch.object(lib_whatsapp, "_get", lambda path, timeout=30: chamadas.append(timeout) or {"ok": False, "erro": "fora"}):
            self.assertEqual(whatsapp.status(timeout=2)["estado_instancia"], "fora")
            whatsapp.status()
        self.assertEqual(chamadas, [2, 30])

    def test_listagem_e_status(self):
        pid = self._cria()
        r = self.client.get("/pendencias", headers=self.auth).json()["pendencias"]
        self.assertTrue(any(p["id"] == pid and p["tipo"] == "whatsapp" for p in r))
        s = self.client.get("/whatsapp/status", headers=self.auth).json()
        self.assertTrue(s["simulado"]); self.assertEqual(s["pendencias_whatsapp_aguardando"], 1)
        h = self.client.get("/health").json()
        self.assertIn("whatsapp", h)


class TestFilaAtraso(Base):
    """atraso_minutos: resumo, áudio e avisos só depois de N min sem resposta de Pedro na conversa."""
    ATRASO = 20

    def setUp(self):
        super().setUp()
        AGUARDADORES.clear()
        self.agora = datetime(2026, 10, 19, 17, 0).astimezone()
        p = mock.patch.object(core, "_now", lambda: self.agora)
        p.start()
        self.addCleanup(p.stop)

    def avancar(self, minutos):
        self.agora += timedelta(minutes=minutos)

    def test_mensagem_relevante_vai_para_a_fila(self):
        r = self.post_webhook("evolution_texto.json", notificar="true")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["acao"], "agendada", d)
        self.assertEqual(d["prazo"], (self.agora + timedelta(minutes=20)).isoformat(timespec="seconds"))
        self.assertEqual(d["fila"]["mensagens"], 1)
        self.assertEqual(d["classificacao"]["categoria"], "profissional")   # classificar continua na hora
        self.assertIsNone(d["notificacao"]); self.assertIsNone(d["pendencia"])
        # nada gerado nem avisado ainda
        self.assertEqual(self.pendencias(), []); self.assertEqual(self.enviados(), []); self.assertEqual(AGUARDADORES, [])
        self.assertFalse((core.vault() / "pendencias").is_dir() and list((core.vault() / "pendencias").glob("*.mp3")))
        fila = whatsapp.listar_fila()
        self.assertEqual((len(fila), fila[0]["contato"], fila[0]["faltam_min"]), (1, "Carlos Fatec", 20.0))
        # irrelevante não entra na fila; repetida é duplicada
        self.assertEqual(self.post_webhook("evolution_spam.json").json()["acao"], "ignorada")
        self.assertEqual(self.post_webhook("evolution_texto.json").json()["acao"], "duplicada")
        self.assertEqual(len(whatsapp.listar_fila()), 1)
        s = self.client.get("/whatsapp/status", headers=self.auth).json()
        self.assertEqual(len(s["fila_espera"]), 1)

    def test_cria_pendencia_e_avisa_ao_vencer_o_prazo(self):
        self.post_webhook("evolution_texto.json", notificar="true")
        self.avancar(19)
        self.assertEqual(whatsapp.processar_fila(), [])
        self.assertEqual(self.pendencias(), [])
        self.avancar(1)
        rs = whatsapp.processar_fila()
        self.assertEqual([r["acao"] for r in rs], ["pendencia_criada"], rs)
        p = rs[0]["pendencia"]
        self.assertEqual((p["status"], p["whatsapp"]["message_id"]), ("aguardando", "3EB0A1B2C3D4E5F60001"))
        self.assertEqual(p["criado_em"], self.agora.isoformat(timespec="minutes"))
        self.assertTrue((core.vault() / p["arquivos"]["mp3"]).is_file())
        self.assertEqual(AGUARDADORES, [("_aguardar-whatsapp", p["id"])])            # notificação no PC
        self.assertEqual([e["tipo"] for e in self.enviados()], ["texto", "audio"])   # aviso pelo WhatsApp
        self.assertIn(f"ref: {p['id']}", self.enviados()[0]["conteudo"])
        self.assertEqual(whatsapp.listar_fila(), [])
        self.assertEqual(whatsapp.processar_fila(), [])
        # webhook repetido depois de virar pendência → duplicada
        self.assertEqual(self.post_webhook("evolution_texto.json").json()["acao"], "duplicada")

    def test_enviar_false_do_webhook_vale_ao_vencer(self):
        self.post_webhook("evolution_texto.json", enviar="false", notificar="true")
        self.avancar(21)
        self.assertEqual(whatsapp.processar_fila()[0]["acao"], "pendencia_criada")
        self.assertEqual(self.enviados(), [])

    def cfg_com(self, **extra):
        """carregar_cfg do teste (com atraso_minutos=20) e chaves do perfis.yml sobrepostas."""
        base = whatsapp.carregar_cfg

        def cfg(nome=None):
            n, c = base(nome)
            return n, {**c, **extra}
        return mock.patch.object(whatsapp, "carregar_cfg", cfg)

    def arquivos_no_vault(self):
        pd = core.vault() / "pendencias"
        return sorted(f.name for f in pd.glob("*") if f.suffix in (".md", ".mp3")) if pd.is_dir() else []

    def test_pedro_responde_antes_do_prazo_cancela(self):
        """fromMe na espera: nada de pendência aguardando, aviso ou envio — só o registro respondida_manual
        com a mensagem do contato, o rascunho que o Alfred proporia e a resposta de Pedro."""
        self.assertEqual(self.post_webhook("evolution_texto.json", notificar="true").json()["acao"], "agendada")
        self.avancar(5)
        d = self.post_webhook("evolution_fromme_direto.json").json()   # Pedro responde Carlos pelo celular
        self.assertEqual(d["acao"], "fila_cancelada", d)
        self.assertEqual(d["fila_cancelada"], ["3EB0A1B2C3D4E5F60001"])
        self.assertEqual(whatsapp.listar_fila(), [])
        self.avancar(30)
        self.assertEqual(whatsapp.processar_fila(), [])
        self.assertEqual(self.pendencias(), []); self.assertEqual(self.enviados(), [])
        self.assertEqual(AGUARDADORES, [])
        self.assertEqual(self.arquivos_no_vault(), [])        # sem .md nem .mp3
        # o registro: o que o Alfred proporia × o que Pedro respondeu
        todas = self.pendencias("todas")
        self.assertEqual(len(todas), 1, todas)
        r = todas[0]
        self.assertEqual(d["respondidas_na_espera"], [r["id"]])
        self.assertEqual(d["pendencia"]["id"], r["id"])
        self.assertEqual((r["status"], r["resposta"], r["respondido_via"]), ("respondida_manual", "texto", "celular"))
        wa = r["whatsapp"]
        self.assertTrue(wa["respondida_na_espera"])
        self.assertEqual([m["message_id"] for m in wa["mensagens"]], ["3EB0A1B2C3D4E5F60001"])
        self.assertIn("orçamento do servidor", wa["texto"])
        if not LIVE:
            self.assertEqual(wa["rascunho"], "Recebi, Carlos! Vou ver o orçamento e te retorno.")
        self.assertIn("já te mando", r["resposta_texto"])
        self.assertEqual(wa["envio"], {"manual": True, "message_id": "3EB0A1B2C3D4E5F60006"})
        self.assertEqual(r["arquivos"], {"md": None, "mp3": None})
        self.assertEqual(r["criado_em"], (self.agora - timedelta(minutes=35)).isoformat(timespec="minutes"))
        self.assertEqual(core.caminho_resolucao(r), "resposta direta pelo celular")
        # CLI `ver` não quebra com md = None
        import importlib.util
        import io
        from contextlib import redirect_stdout
        spec = importlib.util.spec_from_file_location("inbox_assistant", INBOX / "inbox-assistant.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.cmd_ver(mock.Mock(id=r["id"])), 0)
        self.assertIn("respondida_manual", out.getvalue())
        # nova mensagem do contato depois disso → fila de novo, prazo recomeça
        p2 = payload("evolution_texto.json"); p2["data"]["key"]["id"] = "NOVA-DO-CARLOS"
        d = self.client.post("/whatsapp/webhook", json=p2, headers={"apikey": "CHAVE-DE-TESTE"}).json()
        self.assertEqual((d["acao"], d["prazo"]), ("agendada", (self.agora + timedelta(minutes=20)).isoformat(timespec="seconds")))

    def test_fromme_de_outra_conversa_nao_cancela(self):
        self.post_webhook("evolution_texto.json")
        outro = payload("evolution_fromme_direto.json")
        outro["data"]["key"]["remoteJid"] = "5511911112222@s.whatsapp.net"
        d = self.client.post("/whatsapp/webhook", json=outro, headers={"apikey": "CHAVE-DE-TESTE"}).json()
        self.assertEqual(d["acao"], "ignorada", d)
        self.assertEqual(len(whatsapp.listar_fila()), 1)

    def test_eco_de_resposta_do_alfred_nao_cancela(self):
        """Pedro responde a pendência antiga pelo Alfred; a mensagem nova (ainda não avisada) continua na fila."""
        self.post_webhook("evolution_texto.json", enviar="false")
        self.avancar(20)
        pid = whatsapp.processar_fila()[0]["pendencia"]["id"]
        self.avancar(60)
        p2 = payload("evolution_texto.json"); p2["data"]["key"]["id"] = "NOVA-DO-CARLOS"
        self.client.post("/whatsapp/webhook", params={"enviar": "false"}, json=p2, headers={"apikey": "CHAVE-DE-TESTE"})
        envio = whatsapp.responder_texto(pid, "Mando até quinta.")["whatsapp"]["envio"]
        eco = payload("evolution_fromme_direto.json"); eco["data"]["key"]["id"] = envio["message_id"]
        d = self.client.post("/whatsapp/webhook", json=eco, headers={"apikey": "CHAVE-DE-TESTE"}).json()
        self.assertIn("eco", d["motivo"]); self.assertNotIn("fila_cancelada", d)
        self.assertEqual(len(whatsapp.listar_fila()), 1)

    def _eco_durante_envio_de_outro_processo(self, marcar_id: bool, texto_eco: str):
        """Pendência antiga respondida pela notificação no PC (outro processo) enquanto a mensagem nova do
        contato está na fila; o eco fromMe do envio chega ANTES de o message_id ir para pendencias.yml."""
        self.post_webhook("evolution_texto.json", enviar="false")
        self.avancar(20)
        pid = whatsapp.processar_fila()[0]["pendencia"]["id"]
        self.avancar(60)
        p2 = payload("evolution_texto.json"); p2["data"]["key"]["id"] = "NOVA-DO-CARLOS"
        self.client.post("/whatsapp/webhook", params={"enviar": "false"}, json=p2, headers={"apikey": "CHAVE-DE-TESTE"})
        self.assertEqual(len(whatsapp.listar_fila()), 1)
        ecos = []

        def enviar_com_eco(numero, texto, quoted=None, delay=0):
            if marcar_id:
                lib_whatsapp._marcar_envio("ECO-ALFRED")
            eco = payload("evolution_fromme_direto.json")
            eco["data"]["key"]["id"] = "ECO-ALFRED"
            eco["data"]["message"]["conversation"] = texto_eco
            # a API processa o eco no meio do envio (a trava de thread não vale entre processos)
            ecos.append(whatsapp.processar_mensagem_whatsapp.__wrapped__(eco, enviar=False))
            return {"ok": True, "simulado": False, "message_id": "ECO-ALFRED"}

        with mock.patch.object(whatsapp, "enviar_texto", enviar_com_eco):
            whatsapp.responder_texto(pid, "Ok, combinado.", origem="via notificação no PC")
        d = ecos[0]
        self.assertEqual(d["acao"], "ignorada", d); self.assertIn("eco", d["motivo"])
        self.assertNotIn("fila_cancelada", d); self.assertNotIn("respondidas_na_espera", d)
        self.assertEqual(len(whatsapp.listar_fila()), 1)                 # a mensagem nova continua esperando
        self.assertEqual([p["id"] for p in self.pendencias("todas")], [pid])   # nenhum respondida_manual falso
        self.assertEqual(core.obter_pendencia(pid)["whatsapp"]["envio"]["message_id"], "ECO-ALFRED")

    def test_eco_de_outro_processo_com_id_registrado_nao_cancela(self):
        self._eco_durante_envio_de_outro_processo(marcar_id=True, texto_eco="(texto como a Evolution devolveu)")

    def test_eco_de_outro_processo_antes_do_id_nao_cancela(self):
        self._eco_durante_envio_de_outro_processo(marcar_id=False, texto_eco="Ok,  combinado.")

    def test_envio_interrompido_antigo_nao_vira_eco(self):
        """Processo morreu entre a T1 e a T2 (envio.ok=None para sempre): passado ENVIO_EM_CURSO_MAX, uma resposta
        manual de Pedro com o mesmo texto volta a ser resposta direta, não eco."""
        class Morreu(BaseException):
            pass

        self.post_webhook("evolution_texto.json", enviar="false")
        self.avancar(20)
        pid = whatsapp.processar_fila()[0]["pendencia"]["id"]
        with mock.patch.object(whatsapp, "enviar_texto", side_effect=Morreu()):
            with self.assertRaises(Morreu):
                whatsapp.responder_texto(pid, "Ok, combinado.", origem="via notificação no PC")
        envio = core.obter_pendencia(pid)["whatsapp"]["envio"]
        self.assertEqual((core.obter_pendencia(pid)["status"], envio["ok"]), ("respondida", None))
        self.avancar(60)
        p2 = payload("evolution_texto.json"); p2["data"]["key"]["id"] = "NOVA-DO-CARLOS"
        self.client.post("/whatsapp/webhook", params={"enviar": "false"}, json=p2, headers={"apikey": "CHAVE-DE-TESTE"})
        self.assertEqual(len(whatsapp.listar_fila()), 1)
        manual = payload("evolution_fromme_direto.json")
        manual["data"]["key"]["id"] = "MANUAL-DO-PEDRO"
        manual["data"]["message"]["conversation"] = "Ok, combinado."
        d = self.client.post("/whatsapp/webhook", json=manual, headers={"apikey": "CHAVE-DE-TESTE"}).json()
        self.assertEqual(d["acao"], "fila_cancelada", d)
        self.assertEqual(d["fila_cancelada"], ["NOVA-DO-CARLOS"])
        self.assertEqual(whatsapp.listar_fila(), [])

    def test_mensagens_seguidas_juntam_no_mesmo_item(self):
        d1 = self.post_webhook("evolution_texto.json", enviar="false").json()
        self.avancar(8)
        d2 = self.post_webhook("evolution_audio.json", enviar="false").json()   # mesmo contato, áudio
        self.assertEqual(d2["acao"], "agendada", d2)
        self.assertEqual(d2["fila"]["mensagens"], 2)
        self.assertEqual(d2["prazo"], d1["prazo"])          # prazo contado da primeira
        self.assertEqual(len(whatsapp.listar_fila()), 1)
        self.avancar(12)
        rs = whatsapp.processar_fila()
        self.assertEqual(len(rs), 1, rs)
        p = rs[0]["pendencia"]
        wa = p["whatsapp"]
        self.assertEqual([m["message_id"] for m in wa["mensagens"]], ["3EB0A1B2C3D4E5F60001", "3EB0A1B2C3D4E5F60002"])
        self.assertIn("orçamento", wa["texto"]); self.assertIn("Carlos da Fatec", wa["texto"])   # texto + transcrição
        self.assertEqual(wa["key"]["id"], "3EB0A1B2C3D4E5F60002")   # a resposta cita a última mensagem
        self.assertEqual(wa["transcricao"]["metodo"], "mock" if not LIVE else "ollama")
        md = (core.vault() / p["arquivos"]["md"]).read_text(encoding="utf-8")
        self.assertIn("2 mensagens seguidas (texto, áudio)", md); self.assertIn("## Mensagens originais", md)
        self.assertIn("2 mensagens seguidas", rs[0]["notificacao"]["texto"])
        self.assertEqual(len(self.pendencias()), 1)
        # depois de criada, nova mensagem do contato agrupa na pendência aberta (Pedro ainda não respondeu)
        p3 = payload("evolution_texto.json"); p3["data"]["key"]["id"] = "TERCEIRA-DO-CARLOS"
        d3 = self.client.post("/whatsapp/webhook", params={"enviar": "false"}, json=p3,
                              headers={"apikey": "CHAVE-DE-TESTE"}).json()
        self.assertEqual((d3["acao"], d3["pendencia"]["id"]), ("agrupada", p["id"]))

    def test_resposta_direta_fecha_pendencia_e_cancela_fila(self):
        """Pendência antiga ainda aguardando (fora da janela de agrupar) + nova mensagem na fila: um fromMe resolve as duas."""
        self.assertEqual(self.post_webhook("evolution_texto.json", enviar="false").json()["acao"], "agendada")
        self.avancar(20)
        pid = whatsapp.processar_fila()[0]["pendencia"]["id"]
        self.avancar(60)                             # passou agrupar_minutos (30)
        p2 = payload("evolution_texto.json"); p2["data"]["key"]["id"] = "NOVA-DO-CARLOS"
        self.assertEqual(self.client.post("/whatsapp/webhook", params={"enviar": "false"}, json=p2,
                                          headers={"apikey": "CHAVE-DE-TESTE"}).json()["acao"], "agendada")
        avisos = list(AGUARDADORES)
        d = self.post_webhook("evolution_fromme_direto.json").json()
        self.assertEqual(d["acao"], "respondida_manual", d)
        self.assertEqual(d["fila_cancelada"], ["NOVA-DO-CARLOS"])
        p = core.obter_pendencia(pid)
        self.assertEqual((p["status"], p["respondido_via"]), ("respondida_manual", "celular"))
        self.assertIn("já te mando", p["resposta_texto"])
        # a mensagem que ainda esperava na fila vira o registro respondida_manual (sem pendência aguardando)
        self.assertEqual(len(d["respondidas_na_espera"]), 1, d)
        r = core.obter_pendencia(d["respondidas_na_espera"][0])
        self.assertEqual((r["status"], r["respondido_via"]), ("respondida_manual", "celular"))
        self.assertTrue(r["whatsapp"]["respondida_na_espera"])
        self.assertEqual([m["message_id"] for m in r["whatsapp"]["mensagens"]], ["NOVA-DO-CARLOS"])
        if not LIVE:
            self.assertEqual(r["whatsapp"]["rascunho"], "Recebi, Carlos! Vou ver o orçamento e te retorno.")
        self.assertIn("já te mando", r["resposta_texto"])
        self.assertEqual(self.pendencias(), [])
        self.assertEqual(AGUARDADORES, avisos)       # nenhuma notificação nova
        self.assertEqual(self.enviados(), [])
        self.avancar(30)
        self.assertEqual(whatsapp.processar_fila(), [])

    def test_rascunho_indisponivel_registra_sem_rascunho(self):
        """LLM fora do ar (ou erro no rascunho) → o registro sai assim mesmo, com rascunho None."""
        def sem_rascunho(prompt, **kw):
            return None if "RASCUNHO da resposta" in prompt else _fake_classificar(prompt, **kw)

        casos = (("LLM mudo", mock.patch.object(whatsapp, "ollama_generate_json", sem_rascunho)),
                 ("exceção", mock.patch.object(whatsapp, "gerar_rascunho", side_effect=RuntimeError("Ollama caiu"))))
        for nome, ctx in casos:
            with self.subTest(nome):
                self.setUp()
                self.assertEqual(self.post_webhook("evolution_texto.json", enviar="false").json()["acao"], "agendada")
                with ctx:
                    d = self.post_webhook("evolution_fromme_direto.json").json()
                self.assertEqual(d["acao"], "fila_cancelada", d)
                r = self.pendencias("todas")
                self.assertEqual([x["id"] for x in r], d["respondidas_na_espera"])
                self.assertEqual(r[0]["status"], "respondida_manual")
                self.assertIsNone(r[0]["whatsapp"]["rascunho"]); self.assertIsNone(r[0]["whatsapp"]["compromisso"])
                self.assertIn("já te mando", r[0]["resposta_texto"])

    def test_falha_ao_registrar_nao_derruba_o_webhook(self):
        self.post_webhook("evolution_texto.json", enviar="false")
        with mock.patch.object(whatsapp, "registrar_resposta_direta", side_effect=OSError("disco cheio")):
            r = self.post_webhook("evolution_fromme_direto.json")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual((d["acao"], d["fila_cancelada"], d["respondidas_na_espera"]),
                         ("fila_cancelada", ["3EB0A1B2C3D4E5F60001"], []))
        self.assertEqual(whatsapp.listar_fila(), []); self.assertEqual(self.pendencias("todas"), [])

    def test_ignorados_nao_geram_registro(self):
        """Grupos / contatos_ignorar / confiança-relevância continuam valendo: só item que estava na fila vira registro."""
        grupo = payload("evolution_fromme_direto.json")
        grupo["data"]["key"]["remoteJid"] = "120363012345678901@g.us"
        spam_jid = payload("evolution_spam.json")["data"]["key"]["remoteJid"]
        spam = payload("evolution_fromme_direto.json")
        spam["data"]["key"]["remoteJid"] = spam_jid
        casos = (("contatos_ignorar", "evolution_texto.json", payload("evolution_fromme_direto.json"),
                  {"contatos_ignorar": ["Carlos"]}),
                 ("grupo ignorado", "evolution_grupo.json", grupo, {"grupos": "ignorar"}),
                 ("spam / sem relevância", "evolution_spam.json", spam, {}))
        for nome, recebida, resposta, extra in casos:
            with self.subTest(nome), self.cfg_com(**extra):
                self.setUp()
                d = self.post_webhook(recebida, enviar="false").json()
                self.assertEqual(d["acao"], "ignorada", d)
                self.assertEqual(whatsapp.listar_fila(), [])
                d = self.client.post("/whatsapp/webhook", json=resposta, headers={"apikey": "CHAVE-DE-TESTE"}).json()
                self.assertNotIn("fila_cancelada", d); self.assertNotIn("respondidas_na_espera", d)
                self.assertEqual(self.pendencias("todas"), []); self.assertEqual(self.enviados(), [])

    def test_webhook_repetido_depois_do_registro_e_duplicada(self):
        self.post_webhook("evolution_texto.json", enviar="false")
        d = self.post_webhook("evolution_fromme_direto.json").json()
        self.assertEqual(len(d["respondidas_na_espera"]), 1, d)
        self.assertEqual(self.post_webhook("evolution_texto.json").json()["acao"], "duplicada")
        (TMP / "whatsapp_seen.json").unlink()        # sem o seen, o próprio pendencias.yml acusa a duplicata
        self.assertEqual(self.post_webhook("evolution_texto.json").json()["acao"], "duplicada")
        self.assertEqual(whatsapp.listar_fila(), []); self.assertEqual(len(self.pendencias("todas")), 1)

    def test_pendencia_ja_criada_respondida_manual_guarda_rascunho_e_resposta(self):
        self.post_webhook("evolution_texto.json", enviar="false")
        self.avancar(20)
        pid = whatsapp.processar_fila()[0]["pendencia"]["id"]
        rascunho = core.obter_pendencia(pid)["whatsapp"]["rascunho"]
        self.assertTrue(rascunho)
        self.avancar(3)                              # dentro da janela de agrupar: mesmo assim é resposta de Pedro
        d = self.post_webhook("evolution_fromme_direto.json").json()
        self.assertEqual((d["acao"], d["pendencia"]["id"]), ("respondida_manual", pid), d)
        self.assertNotIn("respondidas_na_espera", d)
        p = core.obter_pendencia(pid)
        self.assertEqual((p["status"], p["respondido_via"]), ("respondida_manual", "celular"))
        self.assertEqual(p["whatsapp"]["rascunho"], rascunho)          # o que o Alfred proporia continua lá
        self.assertIn("já te mando", p["resposta_texto"])              # e o que Pedro respondeu
        self.assertEqual(p["whatsapp"]["envio"]["manual"], True)
        self.assertIn("RESPONDIDA DIRETO NO CELULAR", (core.vault() / p["arquivos"]["md"]).read_text(encoding="utf-8"))
        self.assertEqual(core.caminho_resolucao(p), "resposta direta pelo celular")
        self.assertEqual(self.enviados(), [])

    def test_atraso_zero_mantem_fluxo_imediato(self):
        with mock.patch.object(TestFilaAtraso, "ATRASO", 0):
            d = self.post_webhook("evolution_texto.json", enviar="false").json()
        self.assertEqual(d["acao"], "pendencia_criada", d)
        self.assertEqual(whatsapp.listar_fila(), [])
        # o parâmetro da função sobrepõe o perfis.yml (CLI: whatsapp simular --imediato)
        p2 = payload("evolution_texto.json"); p2["data"]["key"].update(id="OUTRO", remoteJid="5511911112222@s.whatsapp.net")
        self.assertEqual(whatsapp.processar_mensagem_whatsapp(p2, enviar=False, atraso_minutos=0)["acao"], "pendencia_criada")

    def test_fila_sobrevive_a_reinicio_e_erro_tenta_de_novo(self):
        self.post_webhook("evolution_texto.json", enviar="false")
        fila = json.loads((TMP / "whatsapp_fila.json").read_text(encoding="utf-8"))["itens"]
        self.assertEqual(fila[0]["mensagens"][0]["msg"]["message_id"], "3EB0A1B2C3D4E5F60001")
        self.avancar(25)
        with mock.patch.object(whatsapp, "criar_pendencia_whatsapp", side_effect=OSError("disco cheio")):
            self.assertEqual(whatsapp.processar_fila(), [])
        self.assertEqual(json.loads((TMP / "whatsapp_fila.json").read_text(encoding="utf-8"))["itens"][0]["tentativas"], 1)
        self.assertEqual(whatsapp.processar_fila()[0]["acao"], "pendencia_criada")

    def test_audio_na_fila_nao_guarda_base64(self):
        self.post_webhook("evolution_audio.json", enviar="false")
        bruto = (TMP / "whatsapp_fila.json").read_text(encoding="utf-8")
        self.assertNotIn("T2dnUw", bruto)            # "OggS" em base64
        self.assertIn("Carlos da Fatec", bruto)      # a transcrição fica

    def test_thread_da_api_processa_vencidos(self):
        self.post_webhook("evolution_texto.json", enviar="false")
        self.avancar(21)
        import time
        with TestClient(main.app):                   # lifespan → thread whatsapp-fila (1ª volta na hora)
            for _ in range(50):
                if self.pendencias():
                    break
                time.sleep(0.05)
        self.assertEqual(len(self.pendencias()), 1)
        self.assertEqual(whatsapp.listar_fila(), [])

    def test_cli_fila_e_processar_fila(self):
        import importlib.util
        import io
        from contextlib import redirect_stdout
        spec = importlib.util.spec_from_file_location("inbox_assistant", INBOX / "inbox-assistant.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        self.post_webhook("evolution_texto.json", notificar="true")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.cmd_whatsapp(cli.build_parser().parse_args(["whatsapp", "fila"])), 0)
            args = cli.build_parser().parse_args(["whatsapp", "processar-fila", "--todos", "--sem-enviar"])
            self.assertEqual(cli.cmd_whatsapp(args), 0)
        self.assertIn("Carlos Fatec", out.getvalue()); self.assertIn("pendencia_criada", out.getvalue())
        self.assertEqual(len(self.pendencias()), 1)
        self.assertEqual(self.enviados(), [])         # --sem-enviar sobrepõe o notificar=true do webhook


class TestHistoricoECache(Base):
    """SQLite/FTS5 (_shared/lib_db.py): histórico das conversas e cache de classificação."""
    JID_CARLOS = "5514988887777@s.whatsapp.net"
    JID_SPAM = "5511900001111@s.whatsapp.net"

    def setUp(self):
        super().setUp()
        for f in (TMP / "vault" / ".alfred").glob("alfred.db*") if (TMP / "vault" / ".alfred").is_dir() else []:
            f.unlink()

    def test_mensagem_vai_para_o_historico(self):
        self.post_webhook("evolution_texto.json", enviar="false")
        h = lib_db.historico_contato(self.JID_CARLOS)
        self.assertIn("Carlos Fatec: Pedro, bom dia! Conseguiu ver o orçamento", h)
        self.assertEqual(len(lib_db.buscar_wa("orcamento")), 1)       # sem acento também acha
        self.post_webhook("evolution_texto.json", enviar="false")      # duplicada: não grava de novo
        self.assertEqual(lib_db.estatisticas()["wa_msgs"], 1)

    def test_grupo_ignorado_nao_grava(self):
        self.post_webhook("evolution_grupo.json", enviar="false")
        self.assertEqual(lib_db.estatisticas().get("wa_msgs", 0), 0)

    def test_historico_desligado_no_perfil(self):
        with mock.patch.object(whatsapp, "carregar_cfg", lambda n=None: ("t", {**_CARREGAR_CFG(n)[1], "atraso_minutos": 0, "historico": False})):
            self.post_webhook("evolution_texto.json", enviar="false")
        self.assertEqual(lib_db.estatisticas().get("wa_msgs", 0), 0)

    def test_historico_entra_no_prompt_sem_repetir_a_mensagem_atual(self):
        lib_db.registrar_wa("ANTIGA-1", self.JID_CARLOS, "Carlos Fatec", "Semana passada falamos do rack de 42U")
        prompts = []

        def captura(prompt, **kw):
            prompts.append(prompt)
            return _fake_generate(prompt, **kw)

        with mock.patch.object(whatsapp, "ollama_generate", captura):
            self.post_webhook("evolution_texto.json", enviar="false")
        pergunta = next(p for p in prompts if "CONVERSA RECENTE" in p)
        trecho = pergunta.split("CONVERSA RECENTE", 1)[1]
        self.assertIn("rack de 42U", trecho)
        self.assertNotIn("Conseguiu ver o orçamento", trecho)   # a mensagem atual já vai em MENSAGEM RECEBIDA

    def test_sem_historico_prompt_diz_nenhuma(self):
        prompts = []
        with mock.patch.object(whatsapp, "ollama_generate", lambda p, **kw: prompts.append(p) or _fake_generate(p)):
            self.post_webhook("evolution_texto.json", enviar="false")
        self.assertTrue(next(p for p in prompts if "CONVERSA RECENTE" in p).rstrip().endswith("nenhuma"))

    def _semear_cache_spam(self):
        res = {"categoria": "spam", "confianca": 0.95, "requer_resposta": False, "urgencia": "baixa",
               "resumo": "x", "motivo": "golpe", "origem": "llm"}
        for _ in range(lib_db.MINIMO_CONFIRMACOES):
            lib_db.cache_put("whatsapp", f"{self.JID_SPAM}|texto", res)

    def test_cache_pula_o_llm_para_contato_spam(self):
        self._semear_cache_spam()

        def nao_deveria(prompt, **kw):
            raise AssertionError("o LLM não devia ser chamado — o contato está no cache")

        with mock.patch.object(whatsapp, "ollama_generate_json", nao_deveria):
            r = self.post_webhook("evolution_spam.json", enviar="false")
        c = r.json()["classificacao"]
        self.assertEqual((c["categoria"], c["origem"]), ("spam", "cache"))
        self.assertEqual(r.json()["acao"], "ignorada")

    def test_cache_aprende_com_o_llm_e_so_ativa_na_terceira(self):
        for i in range(3):
            p = payload("evolution_spam.json")
            p["data"]["key"]["id"] = f"SPAM-{i}"
            origem = self.client.post("/whatsapp/webhook", params={"enviar": "false"}, json=p,
                                      headers={"apikey": "CHAVE-DE-TESTE"}).json()["classificacao"]["origem"]
            self.assertEqual(origem, "llm", f"mensagem {i} já veio do cache")
        p = payload("evolution_spam.json")
        p["data"]["key"]["id"] = "SPAM-3"
        r = self.client.post("/whatsapp/webhook", params={"enviar": "false"}, json=p, headers={"apikey": "CHAVE-DE-TESTE"})
        self.assertEqual(r.json()["classificacao"]["origem"], "cache")

    def test_cache_nunca_vale_para_categoria_de_gente(self):
        # profissional/pessoal não entram no cache, mesmo repetidos
        for i in range(4):
            p = payload("evolution_texto.json")
            p["data"]["key"]["id"] = f"CARLOS-{i}"
            r = self.client.post("/whatsapp/webhook", params={"enviar": "false"}, json=p, headers={"apikey": "CHAVE-DE-TESTE"})
            self.assertNotEqual(r.json()["classificacao"]["origem"], "cache")
        self.assertEqual(lib_db.estatisticas()["cache_entradas"], 0)

    def test_endpoints_de_busca_e_indexacao(self):
        self.post_webhook("evolution_texto.json", enviar="false")
        (TMP / "vault" / "notas").mkdir(parents=True, exist_ok=True)
        (TMP / "vault" / "notas" / "servidor.md").write_text("# Servidor\nOrçamento do servidor novo", encoding="utf-8")
        self.assertEqual(self.client.get("/busca", params={"q": "servidor"}).status_code, 401)   # exige Bearer
        r = self.client.post("/indexar", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreaterEqual(r.json()["indice"]["alfred"]["total"], 1)
        r = self.client.get("/busca", params={"q": "orcamento servidor", "cofre": "alfred"}, headers=self.auth)
        self.assertIn("notas/servidor.md", [x["caminho"] for x in r.json()["resultados"]])
        r = self.client.get("/whatsapp/buscar", params={"q": "orcamento", "jid": self.JID_CARLOS}, headers=self.auth)
        self.assertEqual(len(r.json()["resultados"]), 1)
        self.assertEqual(self.client.get("/health").json()["db"]["wa_msgs"], 1)

    def test_cache_desligado_no_perfil(self):
        self._semear_cache_spam()
        with mock.patch.object(whatsapp, "carregar_cfg", lambda n=None: ("t", {**_CARREGAR_CFG(n)[1], "atraso_minutos": 0, "cache_classificacao": False})):
            r = self.post_webhook("evolution_spam.json", enviar="false")
        self.assertEqual(r.json()["classificacao"]["origem"], "llm")


class TestCacheEmail(unittest.TestCase):
    """core.classificar + lib_db: remetente de envio em massa que cai sempre numa categoria não acionável."""

    def setUp(self):
        for f in (TMP / "vault" / ".alfred").glob("alfred.db*") if (TMP / "vault" / ".alfred").is_dir() else []:
            f.unlink()
        self.chamadas = []

    def _llm(self, categoria, confianca=0.95):
        def f(prompt, **kw):
            self.chamadas.append(prompt)
            return {"categoria": categoria, "confianca": confianca, "resumo": "r", "motivo": "m"}
        return mock.patch.object(core, "ollama_generate_json", f)

    @staticmethod
    def _email(addr="ofertas@loja.com", assunto="Oferta", bulk=True):
        return {"from_addr": addr, "from_domain": addr.split("@")[1], "from_name": "Loja", "subject": assunto,
                "date_iso": "2026-10-23T08:00", "is_bulk": bulk, "attachments": [], "read": False, "ics": [],
                "text": "corpo", "message_id": "<x@y>"}

    def test_promocao_recorrente_pula_o_llm_na_quarta(self):
        with self._llm("promocao"):
            origens = [core.classificar(self._email(assunto=f"Oferta {i}"), {})["origem"] for i in range(4)]
        self.assertEqual(origens, ["llm", "llm", "llm", "cache"])
        self.assertEqual(len(self.chamadas), 3)

    def test_resumo_do_cache_e_o_assunto_do_email_atual(self):
        with self._llm("promocao"):
            for i in range(3):
                core.classificar(self._email(assunto=f"Oferta {i}"), {})
            c = core.classificar(self._email(assunto="Black Friday"), {})
        self.assertEqual((c["origem"], c["resumo"]), ("cache", "Black Friday"))

    def test_so_envio_em_massa_e_so_categoria_nao_acionavel(self):
        with self._llm("promocao"):
            for _ in range(4):
                core.classificar(self._email(bulk=False), {})          # e-mail individual: nunca cacheia
        self.assertEqual(len(self.chamadas), 4)
        self.chamadas.clear()
        with self._llm("evento"):
            for _ in range(4):
                core.classificar(self._email(addr="eventos@x.com"), {})   # acionável: nunca cacheia
        self.assertEqual(len(self.chamadas), 4)
        self.assertEqual(lib_db.estatisticas()["cache_entradas"], 0)

    def test_desligado_no_perfil(self):
        with self._llm("promocao"):
            for _ in range(5):
                core.classificar(self._email(), {"cache_classificacao": False})
        self.assertEqual(len(self.chamadas), 5)


class TestArtefatos(unittest.TestCase):
    def test_workflow_n8n_valido(self):
        d = json.loads((INBOX / "n8n" / "alfred-whatsapp.json").read_text(encoding="utf-8"))
        nomes = {n["name"] for n in d["nodes"]}
        self.assertIn("Webhook Evolution", nomes)
        for src, c in d["connections"].items():
            self.assertIn(src, nomes)
            for saida in c["main"]:
                for dst in saida:
                    self.assertIn(dst["node"], nomes)
        self.assertTrue(any("/whatsapp/webhook" in json.dumps(n) for n in d["nodes"]))
        self.assertTrue(any("sendWhatsAppAudio" in json.dumps(n) for n in d["nodes"]))

    def test_fixture_ogg(self):
        ogg = AQUI / "fixtures" / "audio_carlos_fatec.ogg"
        self.assertTrue(ogg.is_file()); self.assertEqual(ogg.read_bytes()[:4], b"OggS")

    def test_templates_existem(self):
        from _shared.lib_templates import load_template
        for t in ("whatsapp_classificar", "pergunta_whatsapp", "audio_pergunta_whatsapp"):
            load_template(t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
