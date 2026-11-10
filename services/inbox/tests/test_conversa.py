"""
Testes de conversa.py (motor da Conversa com o Alfred): contexto montado, laço com ferramenta segura, aprovação
aprovada / negada / expirada, limite de voltas, parar, resumo do histórico, persistência e a fila do WhatsApp
segurando o próximo item durante um turno.

    cd services/inbox
    venv/bin/python -m unittest tests.test_conversa -v

Offline: vault temporário (agenda.yml, tasks.yml, pendencias.yml e conversas.db de verdade); o Ollama é um
roteiro (conversa._stream_ollama trocado por um gerador que grava cada payload); memória, RAG e resumo (LLM)
mockados; ferramentas temporárias no registro do lib_ferramentas.
"""

import copy
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import yaml

AQUI = Path(__file__).parent
INBOX = AQUI.parent
sys.path.insert(0, str(INBOX))
sys.path.insert(0, str(INBOX.parent))

import core        # noqa: E402
import whatsapp    # noqa: E402
import conversa    # noqa: E402
from _shared import lib_ferramentas as lf  # noqa: E402

BRT = timezone(timedelta(hours=-3))


# ══════════════════════════════════════════════════════════════
#  Ollama de mentira
# ══════════════════════════════════════════════════════════════

def texto(*partes, entrada=100, saida=10) -> list[dict]:
    """Resposta em texto, em pedaços, como o stream do /api/chat."""
    return ([{"message": {"role": "assistant", "content": p}, "done": False} for p in partes]
            + [{"message": {"role": "assistant", "content": ""}, "done": True, "prompt_eval_count": entrada,
                "eval_count": saida}])


def chamada(nome: str, args=None, entrada=100) -> list[dict]:
    """Uma tool_call no formato do Ollama 0.34 (id + function.index)."""
    return [{"message": {"role": "assistant", "content": "",
                         "tool_calls": [{"id": "call_1", "function": {"index": 0, "name": nome,
                                                                      "arguments": args or {}}}]},
             "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "prompt_eval_count": entrada,
             "eval_count": 5}]


class OllamaFalso:
    """Substitui conversa._stream_ollama. Cada chamada consome o próximo item do roteiro: lista de pedaços,
    função payload → lista, ou exceção. Roteiro vazio → `padrao(payload)`."""

    def __init__(self):
        self.roteiro: list = []
        self.payloads: list[dict] = []
        self.padrao = lambda payload: texto("(fim do roteiro)")

    def __call__(self, payload: dict):
        self.payloads.append(copy.deepcopy(payload))   # conversa continua mexendo na lista de mensagens
        passo = self.roteiro.pop(0) if self.roteiro else self.padrao
        if isinstance(passo, Exception):
            raise passo
        pedacos = passo(payload) if callable(passo) else passo
        yield from pedacos


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="alfred-conversa-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "alfred"
        self.vault.mkdir()
        env = {
            "VAULT_ALFRED": str(self.vault), "VAULT_PEDRO": "", "VAULT_NOTICIAS": "",
            "ALFRED_DB": str(self.tmp / "alfred.db"), "MEMORIA_DB": str(self.tmp / "memoria.db"),
            "OLLAMA_BASE_URL": "http://127.0.0.1:9",   # nada deve chegar ao Ollama
            "MODEL_CHAT": "modelo-teste", "EVOLUTION_URL": "", "EVOLUTION_API_KEY": "",
        }
        self.ollama = OllamaFalso()
        self.cfg = {"modelo": "modelo-teste", "modelo_ferramentas": "", "num_ctx": 16384}
        self.memoria = mock.Mock(return_value="")
        self.rag = mock.Mock(return_value="")
        self.resumo = mock.Mock(return_value="")
        for p in (mock.patch.dict(os.environ, env),
                  mock.patch.object(conversa, "_stream_ollama", self.ollama),
                  mock.patch.object(conversa, "_cfg", lambda: dict(self.cfg)),
                  mock.patch.object(conversa.lib_memoria, "contexto", self.memoria),
                  mock.patch.object(conversa.lib_rag, "contexto", self.rag),
                  mock.patch.object(conversa, "ollama_generate", self.resumo),
                  mock.patch.object(core, "_notify", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)
        conversa._WA_CACHE.clear()
        self.addCleanup(self.esperar_livre)

    def esperar_livre(self, timeout: float = 10):
        """O worker é um só para todos os testes: nenhum turno pode sobrar para o próximo."""
        fim = time.monotonic() + timeout
        while conversa.ocupado():
            if time.monotonic() > fim:
                for ap in list(conversa._APROVACOES):
                    conversa.negar(ap)
                self.fail("turno da conversa não terminou")
            time.sleep(0.01)

    def registrar(self, nome: str, funcao, perigosa: bool = False):
        lf.ferramenta(nome, "ferramenta de teste", perigosa=perigosa)(funcao)
        self.addCleanup(lf._REGISTRO.pop, nome, None)

    def eventos(self, turno, ao_aprovar=None, timeout: float = 10) -> list[dict]:
        """Lê a fila do turno até "fim" ou "erro"; `ao_aprovar(evento)` responde aos cartões de aprovação."""
        lista = []
        while True:
            ev = turno.fila.get(timeout=timeout)
            lista.append(ev)
            if ev["tipo"] == "aprovar" and ao_aprovar:
                ao_aprovar(ev)
            if ev["tipo"] in ("fim", "erro"):
                return lista

    def turno(self, pergunta: str = "quais pendências tenho?", cid=None, **kw):
        cid = cid or conversa.nova_conversa()["id"]
        t = conversa.enviar(cid, pergunta)
        return t, self.eventos(t, **kw)

    def sql(self, consulta: str, *params) -> list[dict]:
        with closing(sqlite3.connect(self.vault / "conversas.db")) as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(consulta, params)]


# ══════════════════════════════════════════════════════════════
#  Contexto
# ══════════════════════════════════════════════════════════════

class TestContexto(Base):
    def gravar_agenda(self, itens):
        (self.vault / "agenda").mkdir(exist_ok=True)
        (self.vault / "agenda" / "agenda.yml").write_text(yaml.dump({"itens": itens}, allow_unicode=True),
                                                          encoding="utf-8")

    def gravar_pendencias(self, pend):
        (self.vault / "pendencias").mkdir(exist_ok=True)
        (self.vault / "pendencias" / "pendencias.yml").write_text(
            yaml.dump({"pendencias": pend}, allow_unicode=True), encoding="utf-8")

    def test_personalidade_sem_no_think(self):
        msgs = conversa.montar_contexto(conversa.nova_conversa()["id"], "oi")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("Alfred Pennyworth", msgs[0]["content"])
        self.assertNotIn("/no_think", msgs[0]["content"])
        self.assertEqual(msgs[-1], {"role": "user", "content": "oi"})

    def test_bloco_agora(self):
        agora = datetime(2026, 10, 30, 9, 0, tzinfo=BRT)   # sexta-feira
        self.gravar_agenda([
            {"id": "a", "titulo": "Dentista", "inicio": "2026-10-30T12:00-03:00", "local": "centro",
             "status": "no_calendario"},
            {"id": "b", "titulo": "Prova de redes", "inicio": "2026-10-31", "dia_inteiro": True, "status": "pendente"},
            {"id": "c", "titulo": "Daqui a 3 dias", "inicio": "2026-11-02T10:00-03:00", "status": "pendente"},
            {"id": "d", "titulo": "Desfeito", "inicio": "2026-10-30T15:00-03:00", "status": "desfeito"},
            {"id": "e", "titulo": "Já passou", "inicio": "2026-10-30T08:00-03:00", "status": "no_calendario"},
        ])
        (self.vault / "tasks.yml").write_text(yaml.dump({"tasks": [
            {"id": 1, "title": "Entregar relatório", "status": "open", "due_date": "2026-10-28"},
            {"id": 2, "title": "Pagar boleto", "status": "open", "due_date": "30/10/2026"},
            {"id": 3, "title": "Amanhã", "status": "open", "due_date": "2026-10-31"},
            {"id": 4, "title": "Concluída", "status": "done", "due_date": "2026-10-01"},
        ]}, allow_unicode=True), encoding="utf-8")
        wa = lambda contato, urg, quando, txt: {   # noqa: E731
            "id": f"wa-{contato}", "tipo": "whatsapp", "status": "aguardando", "titulo": contato,
            "criado_em": quando, "whatsapp": {"contato": contato, "urgencia": urg, "texto": txt, "recebido_em": quando}}
        self.gravar_pendencias([
            wa("Ana", "baixa", "2026-10-30T08:50:00-03:00", "bom dia"),
            wa("Caio", "alta", "2026-10-29T08:00:00-03:00", "preciso do orçamento\nhoje"),
            {"id": "ev-1", "tipo": "evento", "status": "aguardando", "titulo": "Meetup de IA",
             "criado_em": "2026-10-30T07:00:00-03:00", "evento": {"data_inicio": "2026-11-05 19:00"}},
            wa("Bia", "media", "2026-10-28T08:00:00-03:00", "e aí"),
            {**wa("Zé", "alta", "2026-10-30T08:00:00-03:00", "x"), "status": "respondida"},
        ])
        bloco = conversa.bloco_agora(agora)
        self.assertTrue(bloco.startswith("## Agora\nsexta-feira, 30 de outubro de 2026, 09:00."))
        self.assertIn("- sex 30/10 12:00 — Dentista (centro)", bloco)
        self.assertIn("- sáb 31/10, dia inteiro — Prova de redes", bloco)
        for fora in ("Daqui a 3 dias", "Desfeito", "Já passou", "Amanhã", "Concluída", "Zé"):
            self.assertNotIn(fora, bloco)
        self.assertLess(bloco.index("Dentista"), bloco.index("Prova de redes"))
        self.assertIn("- #1 Entregar relatório — venceu em 28/10 (2 dia(s) atrás)", bloco)
        self.assertIn("- #2 Pagar boleto — hoje", bloco)
        self.assertIn("Pendências aguardando Pedro: 4. As mais urgentes:", bloco)
        linhas = bloco.splitlines()
        i = linhas.index("Pendências aguardando Pedro: 4. As mais urgentes:")
        self.assertEqual(linhas[i + 1], "- [wa-Caio] WhatsApp · Caio (urgente): preciso do orçamento hoje")
        self.assertEqual(linhas[i + 2], "- [wa-Ana] WhatsApp · Ana: bom dia")
        self.assertEqual(linhas[i + 3], "- [ev-1] evento · Meetup de IA — 2026-11-05 19:00")
        self.assertNotIn("Bia", bloco)   # só as 3 mais urgentes
        self.assertIn("WhatsApp: modo simulado", bloco)

    def test_bloco_agora_vazio_e_parte_quebrada(self):
        (self.vault / "tasks.yml").write_text("tasks: [isto não fecha", encoding="utf-8")
        bloco = conversa.bloco_agora(datetime(2026, 10, 30, 9, 0, tzinfo=BRT))
        self.assertIn("Compromissos nas próximas 48 h: nenhum.", bloco)
        self.assertIn("Pendências aguardando Pedro: nenhuma.", bloco)
        self.assertNotIn("Tarefas", bloco)   # tasks.yml ilegível: a parte some, o resto fica

    def test_whatsapp_com_cache_e_sem_resposta(self):
        from _shared import lib_whatsapp
        with mock.patch.object(lib_whatsapp, "configurado", return_value=True), \
                mock.patch.object(lib_whatsapp, "estado_instancia", return_value={"ok": True, "estado": "open"}) as est:
            self.assertEqual(conversa._whatsapp(datetime.now()), "WhatsApp: conectado.")
            self.assertEqual(conversa._whatsapp(datetime.now()), "WhatsApp: conectado.")
        self.assertEqual(est.call_count, 1)
        self.assertEqual(est.call_args.kwargs["timeout"], conversa.WA_TIMEOUT_S)
        conversa._WA_CACHE.clear()
        with mock.patch.object(lib_whatsapp, "configurado", return_value=True), \
                mock.patch.object(lib_whatsapp, "estado_instancia", return_value={"ok": False, "erro": "timeout"}):
            self.assertIn("não respondeu", conversa._whatsapp(datetime.now()))

    def test_memoria_e_rag_so_pela_heuristica(self):
        cid = conversa.nova_conversa()["id"]
        self.memoria.return_value = "Fatos que Alfred já sabe:\n- [Pedro] estuda na FATEC"
        self.rag.return_value = "Trechos das notas dos cofres:\n[1] docker.md (pedro)\nvolumes"
        msgs = conversa.montar_contexto(cid, "quais pendências tenho?")
        self.memoria.assert_called_once_with("quais pendências tenho?")
        self.rag.assert_not_called()
        self.assertIn("estuda na FATEC", msgs[0]["content"])
        msgs = conversa.montar_contexto(cid, "o que eu anotei sobre docker nas minhas notas?")
        self.rag.assert_called_once_with("o que eu anotei sobre docker nas minhas notas?", 1500)
        self.assertIn("[1] docker.md (pedro)", msgs[0]["content"])

    def test_quer_rag(self):
        sim = ["o que eu anotei sobre RAG?", "resume minhas notas de redes", "o que estudei na aula de física?",
               "o que tem no meu cofre sobre Kubernetes?", "você já pesquisou sobre eBPF?",
               "como estão minhas anotações da certificação AWS?"]
        nao = ["quais pendências tenho?", "como está a GPU?", "marca dentista sexta 15h", "oi Alfred"]
        for p in sim:
            self.assertTrue(conversa._quer_rag(p), p)
        for p in nao:
            self.assertFalse(conversa._quer_rag(p), p)

    def test_falha_na_memoria_nao_derruba(self):
        self.memoria.side_effect = RuntimeError("Ollama caiu")
        msgs = conversa.montar_contexto(conversa.nova_conversa()["id"], "oi")
        self.assertIn("## Agora", msgs[0]["content"])

    def test_historico_no_contexto(self):
        self.ollama.roteiro = [texto("Bom dia, senhor.")]
        t, _ = self.turno("bom dia")
        msgs = conversa.montar_contexto(t.conversa_id, "e agora?")
        self.assertEqual([(m["role"], m["content"]) for m in msgs[1:]],
                         [("user", "bom dia"), ("assistant", "Bom dia, senhor."), ("user", "e agora?")])

    def test_cfg_le_perfis(self):
        from _shared import perfis
        with mock.patch.object(perfis, "load", return_value={"conversa": {"num_ctx": 8192,
                                                                         "modelo_ferramentas": "gemma4:e4b"}}):
            cfg = _CFG_ORIGINAL()   # o _cfg de verdade (o setUp troca o do módulo)
        self.assertEqual(cfg, {"modelo": "modelo-teste", "modelo_ferramentas": "gemma4:e4b", "num_ctx": 8192})
        with mock.patch.object(perfis, "load", return_value={}):
            self.assertEqual(_CFG_ORIGINAL()["num_ctx"], 16384)


_CFG_ORIGINAL = conversa._cfg


# ══════════════════════════════════════════════════════════════
#  Laço do turno
# ══════════════════════════════════════════════════════════════

class TestLaco(Base):
    def setUp(self):
        super().setUp()
        def eco(texto: str) -> str:
            return f"eco: {texto}"
        self.registrar("_t_eco", eco)

    def test_ferramenta_segura(self):
        self.ollama.roteiro = [chamada("_t_eco", {"texto": "oi"}, entrada=3300),
                               texto("Feito, ", "senhor.", entrada=3400, saida=7)]
        t, evs = self.turno()
        tipos = [(e["tipo"], e.get("estado")) for e in evs]
        self.assertEqual(tipos, [("ferramenta", "rodando"), ("ferramenta", "ok"), ("texto", None), ("texto", None),
                                 ("fim", None)])
        self.assertEqual(evs[1]["resultado"], "eco: oi")
        self.assertEqual(evs[1]["argumentos"], {"texto": "oi"})
        self.assertFalse(evs[1]["perigosa"])
        self.assertEqual([e["n"] for e in evs], [1, 2, 3, 4, 5])
        fim = evs[-1]
        self.assertEqual((fim["texto"], fim["parado"], fim["voltas"], fim["tokens_entrada"], fim["tokens_saida"]),
                         ("Feito, senhor.", False, 1, 3400, 12))

        p1, p2 = self.ollama.payloads
        self.assertEqual((p1["model"], p1["stream"], p1["think"], p1["options"]), ("modelo-teste", True, False,
                                                                                   {"num_ctx": 16384}))
        nomes = {f["function"]["name"] for f in p1["tools"]}
        self.assertIn("_t_eco", nomes)
        self.assertIn("listar_pendencias", nomes)
        self.assertEqual(p2["messages"][-2]["tool_calls"][0]["function"]["name"], "_t_eco")
        self.assertEqual(p2["messages"][-1], {"role": "tool", "content": "eco: oi", "tool_name": "_t_eco"})

        msgs = conversa.mensagens(t.conversa_id)
        self.assertEqual([m["papel"] for m in msgs], ["user", "tool", "assistant"])
        self.assertEqual((msgs[1]["ferramenta"], json.loads(msgs[1]["argumentos"]), msgs[1]["resultado"]),
                         ("_t_eco", {"texto": "oi"}, "eco: oi"))
        self.assertEqual((msgs[2]["texto"], msgs[2]["modelo"], msgs[2]["tokens_entrada"], msgs[2]["tokens_saida"]),
                         ("Feito, senhor.", "modelo-teste", 3400, 12))
        self.assertIsNotNone(msgs[2]["tempo_ms"])
        self.assertEqual({m["turno_id"] for m in msgs}, {t.id})
        self.assertEqual(t.estado, "fim")

    def test_resultado_com_erro(self):
        def quebra():
            raise ValueError("deu ruim")
        self.registrar("_t_quebra", quebra)
        self.ollama.roteiro = [chamada("_t_quebra"), texto("A ferramenta falhou, senhor.")]
        _, evs = self.turno()
        self.assertEqual(evs[1]["estado"], "erro")
        self.assertTrue(evs[1]["resultado"].startswith("[erro] _t_quebra: deu ruim"))

    def test_ollama_fora_do_ar(self):
        self.ollama.roteiro = [RuntimeError("Ollama fora do ar (http://127.0.0.1:9)")]
        t, evs = self.turno()
        self.assertEqual([e["tipo"] for e in evs], ["erro"])
        self.assertIn("Ollama fora do ar", evs[0]["erro"])
        self.assertEqual(t.estado, "erro")
        self.esperar_livre()
        self.assertFalse(conversa.ocupado())
        self.assertEqual([m["papel"] for m in conversa.mensagens(t.conversa_id)], ["user"])

    def test_resposta_vazia_vira_erro(self):
        self.ollama.roteiro = [texto("")]
        _, evs = self.turno()
        self.assertEqual(evs[-1]["tipo"], "erro")
        self.assertIn("resposta vazia", evs[-1]["erro"])

    def test_limite_de_voltas(self):
        execucoes = []
        def conta() -> str:
            execucoes.append(1)
            return f"volta {len(execucoes)}"
        self.registrar("_t_conta", conta)
        self.ollama.padrao = lambda payload: chamada("_t_conta") if payload.get("tools") else texto("Chega, senhor.")
        _, evs = self.turno()
        self.assertEqual(len(execucoes), conversa.MAX_VOLTAS)
        self.assertEqual(len(self.ollama.payloads), conversa.MAX_VOLTAS + 1)
        self.assertTrue(all("tools" in p for p in self.ollama.payloads[:-1]))
        self.assertNotIn("tools", self.ollama.payloads[-1])
        self.assertEqual((evs[-1]["tipo"], evs[-1]["texto"], evs[-1]["voltas"]), ("fim", "Chega, senhor.", 6))

    def test_modelo_ferramentas_separado(self):
        self.cfg["modelo_ferramentas"] = "gemma4:e4b"
        self.ollama.roteiro = [chamada("_t_eco", {"texto": "x"}), texto("rascunho do e4b"),
                               texto("Resposta final.")]
        t, evs = self.turno()
        self.assertEqual([p["model"] for p in self.ollama.payloads], ["gemma4:e4b", "gemma4:e4b", "modelo-teste"])
        self.assertIn("tools", self.ollama.payloads[1])
        self.assertNotIn("tools", self.ollama.payloads[2])
        self.assertEqual([e["texto"] for e in evs if e["tipo"] == "texto"], ["Resposta final."])
        self.assertEqual(evs[-1]["texto"], "Resposta final.")
        msgs = conversa.mensagens(t.conversa_id)
        self.assertEqual((msgs[1]["papel"], msgs[1]["modelo"]), ("tool", "gemma4:e4b"))
        self.assertEqual((msgs[2]["texto"], msgs[2]["modelo"]), ("Resposta final.", "modelo-teste"))


# ══════════════════════════════════════════════════════════════
#  Aprovação de ferramenta perigosa
# ══════════════════════════════════════════════════════════════

class TestAprovacao(Base):
    def setUp(self):
        super().setUp()
        self.feitos = []
        def perigo(alvo: str) -> str:
            self.feitos.append(alvo)
            return f"apagado: {alvo}"
        self.registrar("_t_perigo", perigo, perigosa=True)
        self.ollama.roteiro = [chamada("_t_perigo", {"alvo": "logs"}), texto("Como quiser, senhor.")]

    def aprovacoes(self) -> list[dict]:
        return self.sql("SELECT * FROM aprovacoes")

    def test_aprovada(self):
        respostas = []
        _, evs = self.turno(ao_aprovar=lambda ev: respostas.append(conversa.aprovar(ev["id"])))
        self.assertEqual(respostas, [True])
        self.assertEqual(self.feitos, ["logs"])
        cartao = next(e for e in evs if e["tipo"] == "aprovar")
        self.assertEqual((cartao["nome"], cartao["argumentos"]), ("_t_perigo", {"alvo": "logs"}))
        self.assertTrue(cartao["expira_em"])
        self.assertEqual([(e["tipo"], e.get("estado")) for e in evs][:3],
                         [("aprovar", None), ("ferramenta", "rodando"), ("ferramenta", "ok")])
        self.assertEqual(self.ollama.payloads[1]["messages"][-1]["content"], "apagado: logs")
        ap, = self.aprovacoes()
        self.assertEqual((ap["status"], ap["ferramenta"], json.loads(ap["argumentos"]), ap["resultado"]),
                         ("aprovada", "_t_perigo", {"alvo": "logs"}, "apagado: logs"))
        self.assertTrue(ap["decidida_em"])
        self.assertFalse(conversa.aprovar(cartao["id"]))   # já decidida

    def test_negada(self):
        _, evs = self.turno(ao_aprovar=lambda ev: conversa.negar(ev["id"]))
        self.assertEqual(self.feitos, [])
        ferr = next(e for e in evs if e["tipo"] == "ferramenta")
        self.assertEqual(ferr["estado"], "negada")
        self.assertTrue(ferr["resultado"].startswith("[não executada] Pedro negou"))
        self.assertTrue(self.ollama.payloads[1]["messages"][-1]["content"].startswith("[não executada]"))
        self.assertEqual(evs[-1]["texto"], "Como quiser, senhor.")
        self.assertEqual(self.aprovacoes()[0]["status"], "negada")

    def test_expirada(self):
        cartoes = []
        with mock.patch.object(conversa, "APROVACAO_S", 0.2):
            _, evs = self.turno(ao_aprovar=cartoes.append)
        self.assertEqual(self.feitos, [])
        self.assertEqual(next(e for e in evs if e["tipo"] == "ferramenta")["estado"], "expirada")
        self.assertIn("5 minutos", self.ollama.payloads[1]["messages"][-1]["content"])
        self.assertEqual(self.aprovacoes()[0]["status"], "expirada")
        self.assertFalse(conversa.aprovar(cartoes[0]["id"]))   # tarde demais

    def test_aprovacao_desconhecida(self):
        self.assertFalse(conversa.aprovar("nao-existe"))
        self.assertFalse(conversa.negar("nao-existe"))
        self.ollama.roteiro = []


# ══════════════════════════════════════════════════════════════
#  Parar e a fila de um worker só
# ══════════════════════════════════════════════════════════════

class TestParar(Base):
    def test_parar_no_meio_do_texto(self):
        liberar = threading.Event()

        def lento(payload):
            yield from texto("Primeira parte")[:1]
            liberar.wait(5)
            yield from texto(" e segunda parte")

        self.ollama.roteiro = [lento]
        t = conversa.enviar(conversa.nova_conversa()["id"], "conta uma história")
        primeiro = t.fila.get(timeout=5)
        self.assertEqual(primeiro["texto"], "Primeira parte")
        self.assertTrue(conversa.parar(t.id))
        liberar.set()
        evs = self.eventos(t)
        self.assertEqual(evs[-1]["tipo"], "fim")
        self.assertTrue(evs[-1]["parado"])
        self.assertEqual(evs[-1]["texto"], "Primeira parte")
        self.assertEqual(t.estado, "parado")
        self.assertEqual(conversa.mensagens(t.conversa_id)[-1]["texto"], "Primeira parte")
        self.assertFalse(conversa.parar(t.id))   # já acabou
        self.assertFalse(conversa.parar("nao-existe"))

    def test_parar_durante_aprovacao_e_na_fila(self):
        self.registrar("_t_perigo", lambda: "não devia rodar", perigosa=True)
        self.ollama.roteiro = [chamada("_t_perigo")]
        a = conversa.enviar(conversa.nova_conversa()["id"], "apaga tudo")
        cartao = a.fila.get(timeout=5)
        self.assertEqual(cartao["tipo"], "aprovar")
        self.assertEqual(a.estado, "aguardando_aprovacao")
        # o worker é um só: o turno de outra conversa espera na fila
        b = conversa.enviar(conversa.nova_conversa()["id"], "e agora?")
        self.assertEqual(b.estado, "na_fila")
        self.assertTrue(conversa.ocupado())
        self.assertTrue(conversa.parar(b.id))
        self.assertTrue(conversa.parar(a.id))
        evs_a = self.eventos(a)
        self.assertEqual(next(e for e in evs_a if e["tipo"] == "ferramenta")["estado"], "cancelada")
        self.assertTrue(evs_a[-1]["parado"])
        self.assertEqual(self.sql("SELECT status FROM aprovacoes")[0]["status"], "cancelada")
        evs_b = self.eventos(b)
        self.assertEqual([e["tipo"] for e in evs_b], ["fim"])
        self.assertTrue(evs_b[0]["parado"])
        self.assertEqual(len(self.ollama.payloads), 1)   # o turno parado na fila nem chamou o Ollama
        self.esperar_livre()
        self.assertFalse(conversa.ocupado())


# ══════════════════════════════════════════════════════════════
#  Resumo do histórico
# ══════════════════════════════════════════════════════════════

class TestResumo(Base):
    def popular(self, cid: str, n: int, inicio: int = 1) -> list[int]:
        ids = []
        with closing(conversa._conectar()) as con, con:
            for i in range(inicio, inicio + n):
                ids.append(conversa._gravar_mensagem(con, cid, "user" if i % 2 else "assistant", f"mensagem {i}"))
        return ids

    def test_resume_tudo_menos_as_12_ultimas(self):
        cid = conversa.nova_conversa()["id"]
        ids = self.popular(cid, 32)
        self.assertFalse(conversa._atualizar_resumo(cid, "modelo-teste", 16384))   # 32: ainda cabe
        self.resumo.assert_not_called()
        ids += self.popular(cid, 1, inicio=33)
        self.resumo.return_value = "RESUMO 1"
        self.assertTrue(conversa._atualizar_resumo(cid, "modelo-teste", 16384))
        prompt = self.resumo.call_args.args[0]
        self.assertIn("RESUMO ANTERIOR:\n(nenhum)", prompt)
        self.assertIn("Pedro: mensagem 1\n", prompt)
        self.assertIn("Alfred: mensagem 2\n", prompt)
        self.assertIn("mensagem 21", prompt)
        self.assertNotIn("mensagem 22", prompt)
        kw = self.resumo.call_args.kwargs
        self.assertEqual((kw["model"], kw["think"], kw["options"]["num_ctx"]), ("modelo-teste", False, 16384))
        conv = conversa.obter_conversa(cid)
        self.assertEqual((conv["resumo"], conv["resumo_ate"]), ("RESUMO 1", ids[20]))

        msgs = conversa.montar_contexto(cid, "e então?")
        self.assertIn("## Resumo da conversa até aqui\nRESUMO 1", msgs[0]["content"])
        self.assertEqual([m["content"] for m in msgs[1:-1]], [f"mensagem {i}" for i in range(22, 34)])

        # incremental: o resumo anterior entra no prompt do próximo
        self.popular(cid, 21, inicio=34)
        self.resumo.return_value = "RESUMO 2"
        self.assertTrue(conversa._atualizar_resumo(cid, "modelo-teste", 16384))
        self.assertIn("RESUMO ANTERIOR:\nRESUMO 1", self.resumo.call_args.args[0])
        self.assertNotIn("mensagem 21\n", self.resumo.call_args.args[0])

    def test_resumo_que_falha_nao_grava(self):
        cid = conversa.nova_conversa()["id"]
        self.popular(cid, 40)
        self.resumo.return_value = ""
        self.assertFalse(conversa._atualizar_resumo(cid, "modelo-teste", 16384))
        self.assertEqual(conversa.obter_conversa(cid)["resumo_ate"], 0)
        msgs = conversa.montar_contexto(cid, "oi")
        self.assertEqual(len(msgs) - 2, conversa.HISTORICO + conversa.RESUMO_A_CADA)   # teto mesmo sem resumo

    def test_turno_refaz_o_resumo(self):
        cid = conversa.nova_conversa()["id"]
        self.popular(cid, 32)
        self.resumo.return_value = "RESUMO DO TURNO"
        self.ollama.roteiro = [texto("Pois não.")]
        self.turno("mais uma", cid=cid)
        self.esperar_livre()   # o resumo roda depois do "fim"
        self.assertEqual(conversa.obter_conversa(cid)["resumo"], "RESUMO DO TURNO")


# ══════════════════════════════════════════════════════════════
#  Persistência, validação, fila do WhatsApp, CLI
# ══════════════════════════════════════════════════════════════

class TestPersistencia(Base):
    def test_conversa_titulo_busca(self):
        self.ollama.roteiro = [texto("Há 3 pendências esperando o senhor.")]
        t, _ = self.turno("Quais pendências tenho para hoje?")
        self.assertTrue((self.vault / "conversas.db").exists())
        conv = conversa.obter_conversa(t.conversa_id)
        self.assertEqual(conv["titulo"], "Quais pendências tenho para hoje?")
        self.assertLessEqual(conv["criada_em"], conv["atualizada_em"])
        outra = conversa.nova_conversa()
        self.assertEqual(conversa.listar_conversas()[0]["id"], outra["id"])
        achados = conversa.buscar("pendencias")   # sem acento acha com acento
        self.assertEqual({a["conversa_id"] for a in achados}, {t.conversa_id})
        self.assertEqual({a["papel"] for a in achados}, {"user", "assistant"})
        self.assertIn("[pendências]", achados[0]["trecho"])
        self.assertEqual(conversa.buscar("   "), [])

    def test_titulo_curto_e_reaberto(self):
        self.ollama.roteiro = [texto("Olá."), texto("De nada.")]
        t, _ = self.turno("oi")
        self.assertEqual(conversa.obter_conversa(t.conversa_id)["titulo"], "oi")
        self.turno("valeu", cid=t.conversa_id)
        self.assertEqual(conversa.obter_conversa(t.conversa_id)["titulo"], "oi")   # o título não muda
        self.assertEqual(len(conversa.mensagens(t.conversa_id)), 4)

    def test_validacao(self):
        with self.assertRaises(KeyError):
            conversa.enviar("nao-existe", "oi")
        cid = conversa.nova_conversa()["id"]
        with self.assertRaises(ValueError):
            conversa.enviar(cid, "   ")
        self.registrar("_t_perigo", lambda: "ok", perigosa=True)
        self.ollama.roteiro = [chamada("_t_perigo"), texto("Feito.")]
        t = conversa.enviar(cid, "faz")
        self.assertEqual(t.fila.get(timeout=5)["tipo"], "aprovar")
        with self.assertRaises(RuntimeError):
            conversa.enviar(cid, "e mais isso")   # uma pergunta por vez na mesma conversa
        conversa.negar(next(iter(conversa._APROVACOES)))
        self.eventos(t)
        self.assertEqual([m["papel"] for m in conversa.mensagens(cid)], ["user", "tool", "assistant"])

    def test_gravacao_e_transacao(self):
        cid = conversa.nova_conversa()["id"]
        with self.assertRaises(sqlite3.IntegrityError):
            with closing(conversa._conectar()) as con, con:
                conversa._gravar_mensagem(con, cid, "user", "vai")
                conversa._gravar_mensagem(con, cid, "papel-invalido", "não vai")
        self.assertEqual(conversa.mensagens(cid), [])   # a transação inteira voltou


class TestConversasAnteriores(Base):
    FALA = "Estou estudando BitTorrent e DHT para o meu TG na Fatec. Como o Kademlia acha os pares?"
    RESPOSTA = ("O Kademlia organiza os nós por distância XOR e consulta os mais próximos a cada passo, senhor. "
                "Cada consulta reduz pela metade a distância até o alvo.")

    def setUp(self):
        super().setUp()
        self.extrair = mock.Mock(return_value=[{"fato": "estuda DHT"}])
        self.resumo.return_value = "Pedro estuda DHT para o TG. Kademlia explicado."
        self.vetores = mock.Mock(return_value=[])
        for p in (mock.patch.object(conversa.lib_memoria, "extrair", self.extrair),
                  mock.patch.object(conversa.lib_rag, "_buscar_vetores", self.vetores)):
            p.start()
            self.addCleanup(p.stop)

    def envelhecer(self, cid: str, horas: float = 48):
        quando = (datetime.now().astimezone() - timedelta(hours=horas)).isoformat(timespec="seconds")
        with closing(sqlite3.connect(self.vault / "conversas.db")) as con, con:
            con.execute("UPDATE conversas SET atualizada_em = ? WHERE id = ?", (quando, cid))

    def falar(self, cid: str, pergunta: str, resposta: str):
        self.ollama.roteiro = [texto(resposta)]
        self.eventos(conversa.enviar(cid, pergunta))

    def parada(self, pergunta: str = FALA, resposta: str = RESPOSTA, horas: float = 48) -> str:
        cid = conversa.nova_conversa()["id"]
        self.falar(cid, pergunta, resposta)
        self.envelhecer(cid, horas)
        return cid

    def nota(self, cid: str) -> Path:
        criada = conversa.obter_conversa(cid)["criada_em"]
        return self.vault / "conversas" / criada[:7] / f"{cid}.md"

    def linha(self, cid: str) -> dict:
        return self.sql("SELECT * FROM conversas_notas WHERE conversa_id = ?", cid)[0]

    def test_parada_vira_nota_e_memoria(self):
        cid = self.parada()
        self.assertEqual(conversa.comprimir(), {"notas": 1, "curtas": 0, "fatos": 1, "pendentes": 0})
        corpo = self.nota(cid).read_text(encoding="utf-8")
        meta = yaml.safe_load(corpo.split("---\n")[1])
        conv = conversa.obter_conversa(cid)
        self.assertEqual((meta["conversa_id"], meta["tipo"], meta["title"]), (cid, "conversa", conv["titulo"]))
        self.assertIn(f"# {conv['titulo']}\n", corpo)
        self.assertIn(f"127.0.0.1:7073/painel/conversa/{cid})", corpo)
        self.assertIn("2 mensagens", corpo)
        self.assertTrue(corpo.endswith("## Resumo\n\nPedro estuda DHT para o TG. Kademlia explicado.\n"))
        prompt = self.resumo.call_args.args[0]
        self.assertIn("Kademlia acha os pares", prompt)
        self.assertIn("distância XOR", prompt)
        self.assertEqual(self.resumo.call_args.kwargs["model"], "modelo-teste")
        # memória: só as falas de Pedro, com ele como sujeito
        self.extrair.assert_called_once_with(self.FALA, "conversa", "Pedro")
        self.assertEqual(self.linha(cid)["caminho"], f"conversas/{conv['criada_em'][:7]}/{cid}.md")

        self.resumo.reset_mock()
        self.extrair.reset_mock()
        self.assertEqual(conversa.comprimir(), {"notas": 0, "curtas": 0, "fatos": 0, "pendentes": 0})
        self.resumo.assert_not_called()
        self.extrair.assert_not_called()

    def test_recente_fica_de_fora(self):
        cid = self.parada(horas=23)
        self.assertEqual(conversa.comprimir()["notas"], 0)
        self.resumo.assert_not_called()
        self.assertFalse(self.nota(cid).exists())
        self.assertEqual(conversa.comprimir(horas=22)["notas"], 1)

    def test_retomada_refaz_a_nota_e_so_o_novo_vai_para_a_memoria(self):
        cid = self.parada()
        conversa.comprimir()
        self.extrair.reset_mock()
        self.falar(cid, "E a tabela de roteamento, como fica?", "Em k-buckets, senhor.")
        self.envelhecer(cid)
        self.resumo.return_value = "Resumo novo com k-buckets."
        self.assertEqual(conversa.comprimir()["notas"], 1)
        self.extrair.assert_called_once_with("E a tabela de roteamento, como fica?", "conversa", "Pedro")
        corpo = self.nota(cid).read_text(encoding="utf-8")
        self.assertIn("Resumo novo com k-buckets.", corpo)
        self.assertNotIn("Kademlia explicado", corpo)
        self.assertIn("4 mensagens", corpo)

    def test_resumo_de_conversa_longa_e_incremental(self):
        cid = self.parada()
        ultima = conversa.mensagens(cid)[-1]["id"]
        with closing(sqlite3.connect(self.vault / "conversas.db")) as con, con:
            con.execute("UPDATE conversas SET resumo = 'RESUMO ANTIGO', resumo_ate = ? WHERE id = ?", (ultima - 1, cid))
        conversa.comprimir()
        prompt = self.resumo.call_args.args[0]
        self.assertIn("RESUMO ANTERIOR:\nRESUMO ANTIGO", prompt)
        self.assertIn("distância XOR", prompt)
        self.assertNotIn("Kademlia acha os pares", prompt)

    def test_curta_nao_gera_nota_mas_passa_pela_memoria(self):
        cid = self.parada("oi", "Boa noite, senhor.")
        self.assertEqual(conversa.comprimir(), {"notas": 0, "curtas": 1, "fatos": 1, "pendentes": 0})
        self.resumo.assert_not_called()
        self.extrair.assert_called_once_with("oi", "conversa", "Pedro")
        self.assertFalse(self.nota(cid).exists())
        self.assertEqual(self.linha(cid)["caminho"], "")
        self.extrair.reset_mock()
        self.assertEqual(conversa.comprimir()["curtas"], 0)
        self.extrair.assert_not_called()

    def test_sem_mensagem_nao_entra(self):
        cid = conversa.nova_conversa()["id"]
        self.envelhecer(cid)
        self.assertEqual(conversa.comprimir(), {"notas": 0, "curtas": 0, "fatos": 0, "pendentes": 0})

    def test_llm_fora_para_e_tenta_na_proxima(self):
        a, b = self.parada(), self.parada()
        self.resumo.return_value = ""
        self.assertEqual(conversa.comprimir(), {"notas": 0, "curtas": 0, "fatos": 0, "pendentes": 2})
        self.assertEqual(self.resumo.call_count, 1)
        self.extrair.assert_not_called()
        self.assertEqual(self.sql("SELECT * FROM conversas_notas"), [])
        self.resumo.return_value = "Agora foi."
        self.assertEqual(conversa.comprimir()["notas"], 2)
        self.assertTrue(self.nota(a).exists() and self.nota(b).exists())

    def test_erro_ao_gravar_nao_derruba(self):
        self.parada()
        with mock.patch.object(conversa, "gravar_atomico", side_effect=OSError("disco cheio")):
            self.assertEqual(conversa.comprimir()["pendentes"], 1)
        self.assertEqual(self.sql("SELECT * FROM conversas_notas"), [])

    def test_apagar_leva_a_nota(self):
        cid = self.parada()
        conversa.comprimir()
        self.assertTrue(self.nota(cid).exists())
        arquivo = self.nota(cid)
        self.assertTrue(conversa.apagar_conversa(cid))
        self.assertFalse(arquivo.exists())
        self.assertEqual(self.sql("SELECT * FROM conversas_notas"), [])

    def test_quer_conversas(self):
        sim = ["o que falamos sobre BitTorrent?", "lembra da conversa passada sobre o TG?", "aquela conversa do orçamento",
               "você me recomendou qual livro?", "como te falei ontem, vou viajar", "na última vez o que ficou combinado?",
               "já conversamos sobre isso", "a gente falou de Docker", "lembra que eu ia trocar de GPU?"]
        nao = ["quais pendências tenho?", "como está a GPU?", "oi Alfred", "marca dentista sexta 15h",
               "o que eu anotei sobre RAG?"]
        for p in sim:
            self.assertTrue(conversa._quer_conversas(p), p)
        for p in nao:
            self.assertFalse(conversa._quer_conversas(p), p)

    def resumo_vetorial(self, *cids) -> list[dict]:
        return [{"cofre": "alfred", "caminho": f"conversas/2026-11/{c}.md", "titulo": c, "trecho": f"resumo  de\n{c}"}
                for c in cids]

    def test_citar_conversa_passada_poe_o_resumo_no_contexto(self):
        antiga, atual = self.parada(), conversa.nova_conversa()["id"]
        self.vetores.return_value = self.resumo_vetorial(atual, antiga)
        msgs = conversa.montar_contexto(atual, "o que falamos sobre DHT?")
        self.vetores.assert_called_once_with("o que falamos sobre DHT?", "alfred", "conversas", 6)
        sistema = msgs[0]["content"]
        self.assertIn("## Conversas anteriores com Pedro", sistema)
        self.assertIn(f"[{antiga}] ", sistema)
        self.assertIn(f"resumo de {antiga}", sistema)
        self.assertNotIn(f"[{atual}]", sistema)              # a conversa aberta não é "anterior"
        self.vetores.reset_mock()
        msgs = conversa.montar_contexto(atual, "quais pendências tenho?")
        self.vetores.assert_not_called()
        self.assertNotIn("Conversas anteriores", msgs[0]["content"])

    def test_sem_resumo_proximo_nao_abre_bloco(self):
        msgs = conversa.montar_contexto(conversa.nova_conversa()["id"], "o que falamos sobre DHT?")
        self.assertNotIn("Conversas anteriores", msgs[0]["content"])

    def test_bloco_respeita_o_teto(self):
        grandes = [self.parada() for _ in range(3)]
        self.vetores.return_value = [{"caminho": f"conversas/2026-11/{c}.md", "trecho": "x" * 650} for c in grandes]
        bloco = conversa._bloco_anteriores("falamos", "outra")
        self.assertEqual(bloco.count("x" * 650), 2)

    def test_chromadb_com_problema_nao_derruba(self):
        self.vetores.side_effect = RuntimeError("resposta estranha")
        cid = self.parada()
        msgs = conversa.montar_contexto(conversa.nova_conversa()["id"], "o que falamos sobre DHT?")
        self.assertNotIn("Conversas anteriores", msgs[0]["content"])
        self.assertEqual([a["conversa_id"] for a in conversa.buscar_anteriores("Kademlia")], [cid])

    def test_buscar_anteriores_ordem_de_confianca(self):
        literal = self.parada()
        so_no_resumo = self.parada("Quanto custa um servidor?", "Depende, senhor, do que for rodar nele todos os dias.")
        self.vetores.return_value = self.resumo_vetorial(so_no_resumo, literal)
        achados = conversa.buscar_anteriores("Kademlia pares")
        self.assertEqual([a["conversa_id"] for a in achados], [literal, so_no_resumo])
        self.assertIn("[Kademlia]", achados[0]["trecho"])
        self.assertEqual(achados[0]["titulo"], conversa.obter_conversa(literal)["titulo"])
        self.assertRegex(achados[0]["quando"], r"^\d{2}/\d{2}/\d{4}$")
        self.assertEqual(achados[1]["trecho"], f"resumo de {so_no_resumo}")
        self.assertEqual(len(conversa.buscar_anteriores("Kademlia pares", limite=1)), 1)

    def test_qualquer_palavra_so_quando_nada_mais_achou(self):
        cid = self.parada()
        self.assertEqual(conversa.buscar_anteriores("Kademlia inexistente")[0]["conversa_id"], cid)   # alguma palavra
        self.assertEqual(conversa.buscar_anteriores("Kademlia inexistente", so_resumos=True), [])
        self.vetores.return_value = self.resumo_vetorial(cid)
        self.assertEqual(conversa.buscar_anteriores("inexistente demais")[0]["trecho"], f"resumo de {cid}")
        self.vetores.return_value = []
        self.assertEqual(conversa.buscar_anteriores("zzzz yyyy"), [])

    def test_conversa_em_andamento_e_apagada_ficam_de_fora(self):
        cid = self.parada()
        t = conversa.Turno(cid, "x", "m")
        conversa._TURNOS[t.id] = t
        self.addCleanup(conversa._TURNOS.pop, t.id, None)
        self.assertEqual(conversa.buscar_anteriores("Kademlia"), [])
        del conversa._TURNOS[t.id]
        self.assertEqual(len(conversa.buscar_anteriores("Kademlia")), 1)
        self.vetores.return_value = self.resumo_vetorial("nao-existe-mais")
        self.assertEqual([a["conversa_id"] for a in conversa.buscar_anteriores("Kademlia")], [cid])

    def test_cli(self):
        self.parada()
        saida = io.StringIO()
        with redirect_stdout(saida):
            self.assertEqual(conversa.cmd_comprimir(Namespace(horas=24)), 0)
        self.assertEqual(saida.getvalue().strip().splitlines()[-1], "1 nota(s), 0 curta(s) sem nota, 1 fato(s) na memória")
        self.parada()
        self.resumo.return_value = ""
        saida = io.StringIO()
        with redirect_stdout(saida):
            self.assertEqual(conversa.cmd_comprimir(Namespace(horas=24)), 1)
        self.assertIn("1 para a próxima passada", saida.getvalue())


class TestFilaWhatsapp(Base):
    def setUp(self):
        super().setUp()
        self.fila = self.tmp / "whatsapp_fila.json"
        vencido = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(timespec="seconds")
        self.fila.write_text(json.dumps({"itens": [{"id": "m1", "remote_jid": "5511@s.whatsapp.net",
                                                    "contato": "Caio", "prazo": vencido, "mensagens": []}]}),
                             encoding="utf-8")
        self.criar = mock.Mock(return_value={"acao": "pendencia"})
        for p in (mock.patch.object(whatsapp, "FILA_FILE", self.fila),
                  mock.patch.object(whatsapp, "_criar_do_item", self.criar),
                  mock.patch.object(whatsapp, "disparar_lembretes", lambda *a, **k: [])):
            p.start()
            self.addCleanup(p.stop)

    def test_turno_ativo_segura_o_proximo_item(self):
        with mock.patch.object(conversa, "ocupado", return_value=True):
            self.assertEqual(whatsapp.processar_fila(), [])
        self.criar.assert_not_called()
        self.assertEqual(len(json.loads(self.fila.read_text())["itens"]), 1)   # esperando, não descartado
        self.assertEqual(whatsapp.processar_fila(), [{"acao": "pendencia"}])
        self.assertEqual(json.loads(self.fila.read_text())["itens"], [])

    def test_ocupado_durante_o_turno(self):
        self.registrar("_t_perigo", lambda: "ok", perigosa=True)
        self.ollama.roteiro = [chamada("_t_perigo"), texto("Feito.")]
        self.assertFalse(conversa.ocupado())
        t = conversa.enviar(conversa.nova_conversa()["id"], "faz")
        cartao = t.fila.get(timeout=5)
        self.assertTrue(conversa.ocupado())
        self.assertEqual(whatsapp.processar_fila(), [])
        conversa.aprovar(cartao["id"])
        self.eventos(t)
        self.esperar_livre()
        self.assertEqual(len(whatsapp.processar_fila()), 1)


class TestCli(Base):
    def test_conversa_no_terminal(self):
        self.registrar("_t_perigo", lambda: "executado", perigosa=True)
        self.ollama.roteiro = [chamada("_t_perigo"), texto("Pronto, ", "senhor.")]
        saida = io.StringIO()
        with mock.patch("builtins.input", return_value="s"), redirect_stdout(saida):
            rc = conversa.cmd_conversa(Namespace(texto=["faz", "isso"], conversa=None, modelo=None))
        self.assertEqual(rc, 0)
        out = saida.getvalue()
        self.assertIn("_t_perigo {} precisa da sua aprovação", out)
        self.assertIn("↳ ok: executado", out)
        self.assertIn("Pronto, senhor.", out)
        self.assertIn("1 volta(s) de ferramenta", out)

    def test_conversa_inexistente(self):
        saida = io.StringIO()
        with redirect_stdout(saida):
            rc = conversa.cmd_conversa(Namespace(texto=["oi"], conversa="nao-existe", modelo=None))
        self.assertEqual(rc, 1)
        self.assertIn("conversa não encontrada", saida.getvalue())


if __name__ == "__main__":
    unittest.main()
