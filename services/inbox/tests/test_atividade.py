"""
Testes de _shared/lib_atividade.py: atividade em andamento, passos, avisos, pedidos e o Despachante.

    cd services/inbox
    venv/bin/python -m unittest tests.test_atividade -v

Offline: pasta temporária (ALFRED_ATIVIDADE_DIR) e um systemd falso — nenhuma unit é iniciada.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

AQUI =Path(__file__).parent
sys.path.insert(0, str(AQUI.parent.parent))

from _shared import lib_atividade as la  # noqa: E402

INBOX, RAG = "alfred-inbox.service", "alfred-rag.service"


class SistemaFalso:
    def __init__(self):
        self.units = {INBOX, RAG}
        self.ativas = set()
        self.proximas = {INBOX: 2000.0, RAG: 1500.0, "alfred-outra.service": 1.0}
        self.iniciadas = []
        self.soltar = threading.Event()     # a rotina "roda" até o teste soltar

    def instaladas(self):
        return set(self.units)

    def rodando(self):
        return set(self.ativas)

    def timers(self):
        return dict(self.proximas)

    def iniciar(self, unit):
        self.iniciadas.append(unit)
        self.soltar.wait(5)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="alfred-atividade-test-"))
        amb = mock.patch.dict(os.environ, {"ALFRED_ATIVIDADE_DIR": str(self.tmp)})   # ao sair, volta a pasta dos testes
        amb.start()
        self.sis = SistemaFalso()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(amb.stop)
        self.addCleanup(self.sis.soltar.set)

    def estado(self):
        return json.loads((self.tmp / "estado.json").read_text(encoding="utf-8"))


class TestPublicar(Base):
    def test_atividade_com_passo_aparece_e_some(self):
        d = la.Despachante(self.sis)
        with la.atividade("email", "Ler e classificar os e-mails"):
            la.passo("Classificando e-mails", 3, 12)
            agora = d.tick(100.0)["agora"]
            self.assertEqual([(a["tipo"], a["passo"], a["feito"], a["total"]) for a in agora],
                             [("email", "Classificando e-mails", 3, 12)])
        self.assertEqual(d.tick(101.0)["agora"], [])
        self.assertEqual(list((self.tmp / "agora").glob("*.json")), [])

    def test_decorador_e_passo_fora_de_atividade(self):
        la.passo("sem atividade")            # não faz nada, não levanta

        @la.atividade("rag", "Indexar os cofres")
        def trabalho():
            return [a["tipo"] for a in la.Despachante(self.sis).tick(1.0)["agora"]]

        self.assertEqual(trabalho(), ["rag"])

    def test_processo_morto_sai_do_agora(self):
        (self.tmp / "agora").mkdir(parents=True)
        orfao = self.tmp / "agora" / "999999-abc.json"
        orfao.write_text(json.dumps({"id": "abc", "tipo": "email", "rotulo": "x", "pid": 2 ** 22 + 12345}))
        self.assertEqual(la.Despachante(self.sis).tick(1.0)["agora"], [])
        self.assertFalse(orfao.exists())

    def test_unit_rodando_sem_instrumentacao_aparece_pelo_nome(self):
        self.sis.ativas = {RAG, "alfred-outra.service"}
        agora = la.Despachante(self.sis).tick(1.0)["agora"]
        self.assertEqual([(a["tipo"], a["rotulo"]) for a in agora], [("rag", "Indexar os cofres")])

    def test_unit_instrumentada_nao_duplica(self):
        self.sis.ativas = {INBOX}
        with la.atividade("email", "Ler e classificar os e-mails"):
            self.assertEqual(len(la.Despachante(self.sis).tick(1.0)["agora"]), 1)

    def test_evento_vale_por_um_tempo(self):
        d = la.Despachante(self.sis)
        la.evento("pendencia", "Convite da Fatec")
        agora = json.loads(next((self.tmp / "eventos").glob("*.json")).read_text())["quando"]
        self.assertEqual([(e["tipo"], e["texto"]) for e in d.tick(agora + 1)["eventos"]],
                         [("pendencia", "Convite da Fatec")])
        self.assertEqual(d.tick(agora + la.EVENTO_VALE_S + 1)["eventos"], [])
        self.assertEqual(list((self.tmp / "eventos").glob("*.json")), [])

    def test_publicar_nunca_derruba_o_trabalho(self):
        os.environ["ALFRED_ATIVIDADE_DIR"] = "/proc/nao-da-para-gravar-aqui"
        with la.atividade("email", "x"):
            la.passo("y")
        la.evento("pendencia", "z")


class TestNegligencia(Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, la, "FONTE_NEGLIGENCIA", la.FONTE_NEGLIGENCIA)

    def test_sem_fonte_publica_none(self):
        la.Despachante(self.sis).tick(1.0)
        self.assertIsNone(self.estado()["negligencia"])

    def test_publica_limpo_e_rele_so_de_tempos_em_tempos(self):
        lidas = []

        def fonte():
            lidas.append(1)
            return {"nivel": 7, "motivo": "promessa", "texto": "x" * 500, "link": "http://127.0.0.1:7073/painel/agenda",
                    "quantos": 2}

        la.FONTE_NEGLIGENCIA = fonte
        d = la.Despachante(self.sis)
        n = d.tick(100.0)["negligencia"]
        self.assertEqual((n["nivel"], n["motivo"], len(n["texto"]), n["link"]),
                         (1.0, "promessa", 200, "http://127.0.0.1:7073/painel/agenda"))
        d.tick(100.0 + la.NEGLIGENCIA_A_CADA_S - 1)
        self.assertEqual(len(lidas), 1)
        la.FONTE_NEGLIGENCIA = lambda: None                       # Pedro resolveu tudo
        self.assertIsNone(d.tick(100.0 + la.NEGLIGENCIA_A_CADA_S)["negligencia"])

    def test_fonte_com_erro_mantem_a_ultima(self):
        la.FONTE_NEGLIGENCIA = lambda: {"nivel": 0.5, "motivo": "tarefa", "texto": "Pagar boleto"}
        d = la.Despachante(self.sis)
        d.tick(100.0)
        la.FONTE_NEGLIGENCIA = lambda: 1 / 0
        self.assertEqual(d.tick(200.0)["negligencia"]["nivel"], 0.5)


class TestFila(Base):
    def test_catalogo_e_agendadas_so_das_units_instaladas(self):
        est = la.Despachante(self.sis).tick(1.0)
        self.assertEqual({c["tipo"] for c in est["catalogo"]}, {"email", "rag"})
        self.assertEqual([(a["tipo"], a["quando"]) for a in est["agendadas"]], [("rag", 1500.0), ("email", 2000.0)])

    def test_na_frente_roda_primeiro_e_uma_por_vez(self):
        d = la.Despachante(self.sis)
        self.sis.ativas = {"alfred-vigia.service"}       # fora do catálogo instalado: não segura a fila
        la.pedir("enfileirar", tipo="rag")
        la.pedir("enfileirar", tipo="email", frente=True)
        la.pedir("enfileirar", tipo="nao_existe")
        est = d.tick(1.0)
        self.assertEqual(self.sis.iniciadas, [INBOX])                       # o da frente saiu da fila e roda
        self.assertEqual([i["tipo"] for i in est["fila"]], ["rag"])
        self.assertEqual([a["tipo"] for a in est["agora"]], ["email"])
        d.tick(2.0)
        self.assertEqual(self.sis.iniciadas, [INBOX])                       # a anterior ainda não terminou
        self.sis.soltar.set()
        d._em_curso[1].join(5)
        d.tick(3.0)
        self.assertEqual(self.sis.iniciadas, [INBOX, RAG])
        self.assertEqual(self.estado()["fila"], [])

    def test_fila_espera_rotina_do_timer(self):
        d = la.Despachante(self.sis)
        self.sis.ativas = {RAG}
        la.pedir("enfileirar", tipo="email")
        self.assertEqual([i["tipo"] for i in d.tick(1.0)["fila"]], ["email"])
        self.assertEqual(self.sis.iniciadas, [])
        self.sis.ativas = set()
        d.tick(2.0)
        self.assertEqual(self.sis.iniciadas, [INBOX])

    def test_recado_feito_so_encerra_recado_publicado(self):
        recados, encerrados = [{"id": "p1", "texto": "Marina"}], []
        def encerrar(pid):
            encerrados.append(pid)
            recados.clear()
        with mock.patch.object(la, "FONTE_RECADOS", lambda: recados), mock.patch.object(la, "AO_RECADO_FEITO", encerrar):
            d = la.Despachante(self.sis)
            self.assertEqual([r["id"] for r in d.tick(1.0)["recados"]], ["p1"])
            la.pedir("recado_feito", id="outra-pendencia")
            la.pedir("recado_feito", id="p1")
            self.assertEqual(d.tick(2.0)["recados"], [])          # relido na mesma volta, sem esperar os 5 s
        self.assertEqual(encerrados, ["p1"])

    def test_aviso_leva_a_pendencia_e_o_feito_do_pet_encerra_a_que_esta_aberta(self):
        abertas, encerradas = [{"id": "p1", "texto": "Convite", "link": "http://127.0.0.1:7073/painel/pendencias/p1"}], []
        def encerrar(pid):
            encerradas.append(pid)
            abertas.clear()
        with mock.patch.object(la, "FONTE_PENDENTES", lambda: abertas), mock.patch.object(la, "AO_RECADO_FEITO", encerrar):
            d = la.Despachante(self.sis)
            la.evento("pendencia", "Convite", ref="p1", link=abertas[0]["link"])
            est = d.tick()
            self.assertEqual([(e["ref"], e["link"]) for e in est["eventos"]], [("p1", abertas[0]["link"])])
            self.assertEqual(est["pendentes"], abertas)
            la.pedir("recado_feito", id="ja-resolvida")
            la.pedir("recado_feito", id="p1")
            self.assertEqual(d.tick()["pendentes"], [])           # resolvida: sai da lista na mesma volta
        self.assertEqual(encerradas, ["p1"])

    def test_recado_leva_o_tipo_e_o_horario_em_que_nasceu(self):
        recados = [{"id": "remedio:x:1", "tipo": "remedio", "texto": "Tomar Amoxicilina", "quando": 1762870000.0},
                   {"id": "p1", "texto": "Marina"}]
        with mock.patch.object(la, "FONTE_RECADOS", lambda: recados):
            publicados = la.Despachante(self.sis).tick(1.0)["recados"]
        self.assertEqual([(r["tipo"], r["quando"]) for r in publicados], [("remedio", 1762870000.0), ("recado", None)])

    def test_frente_cancelar_e_fila_sobrevive_ao_reinicio(self):
        d = la.Despachante(self.sis)
        self.sis.ativas = {RAG}                           # segura a fila para dar tempo de mexer nela
        la.pedir("enfileirar", tipo="email")
        la.pedir("enfileirar", tipo="rag")
        la.pedir("enfileirar", tipo="email")
        a, b, c = [i["id"] for i in d.tick(1.0)["fila"]]
        la.pedir("frente", id=c)
        la.pedir("cancelar", id=a)
        la.pedir("cancelar", id="nao-existe")
        self.assertEqual([i["id"] for i in d.tick(2.0)["fila"]], [c, b])
        self.assertEqual([i["id"] for i in la.Despachante(self.sis).fila], [c, b])

    def test_batimento_regrava_o_estado_parado(self):
        d = la.Despachante(self.sis)
        d.tick(10.0)
        d.tick(11.0)
        self.assertEqual(self.estado()["atualizado"], 10.0)
        d.tick(10.0 + la.BATIMENTO_S)
        self.assertEqual(self.estado()["atualizado"], 10.0 + la.BATIMENTO_S)
        self.assertEqual(la.estado()["versao"], la.VERSAO)

    def test_links_do_painel_seguem_a_porta(self):
        with mock.patch.dict(os.environ, {"PORT_INBOX": "7999"}):
            la.Despachante(self.sis).tick(1.0)
        self.assertEqual(self.estado()["links"], {"painel": "http://127.0.0.1:7999/painel",
                                                  "conversa": "http://127.0.0.1:7999/painel/conversa",
                                                  "escrita": "http://127.0.0.1:7999/painel/escrita"})


    def test_notado_vai_para_o_estado_e_usos_seguidos_contam_como_um(self):
        d = la.Despachante(self.sis)
        self.assertIsNone(d.tick(1.0)["notado"])
        with mock.patch.object(la, "_notado_em", 0.0), mock.patch.object(la.time, "time", return_value=500.0):
            la.notado("painel")
            la.notado("painel")                     # dentro de NOTADO_A_CADA_S: não regrava
        self.assertEqual(d.tick(2.0)["notado"], 500.0)
        with mock.patch.object(la, "_notado_em", 0.0), mock.patch.object(la.time, "time", return_value=900.0):
            la.pedir("enfileirar", tipo="rag")      # mexer na fila também é uso
            self.assertEqual(d.tick(3.0)["notado"], 900.0)

if __name__ == "__main__":
    unittest.main()
