"""
Testes da memória automática (_shared/lib_memoria.py): portão, extração, deduplicação, auditoria com
impressão digital, contexto para prompts, espelho no cofre, Ollama fora do ar e CLI.

    cd services/inbox
    venv/bin/python -m unittest tests.test_memoria -v

Offline: vault temporário, LLM e embeddings mockados (o LLM falso responde pelo JSON Schema pedido; o
embedding falso põe cada "assunto" num eixo). Os testes de Ollama fora batem em 127.0.0.1:9 (recusado).
A seção memoria: do perfis.yml é trocada pelos defaults — mudar o perfis.yml não quebra os testes.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).parent
sys.path.insert(0, str(AQUI.parent))
sys.path.insert(0, str(AQUI.parent.parent))

from _shared import lib_memoria  # noqa: E402

ASSUNTOS = ["café", "bauru", "aniversário", "attanotech", "camarão", "homelab", "chá", "academia"]


def embed_falso(texto, model=None):
    """Cada assunto citado acende um eixo; texto sem assunto ganha um eixo próprio (crc32)."""
    t = texto.lower()
    v = [0.0] * 64
    for i, assunto in enumerate(ASSUNTOS):
        if assunto in t:
            v[i] = 1.0
    if not any(v):
        v[len(ASSUNTOS) + zlib.crc32(t.encode()) % (64 - len(ASSUNTOS))] = 1.0
    return v


class LLMFalso:
    """Substitui ollama_generate_json: uma fila de respostas por tipo de chamada, escolhido pelo schema."""

    def __init__(self):
        self.filas = {"extrair": [], "deduplicar": [], "auditar": []}
        self.chamadas = []

    def __call__(self, prompt, model=None, timeout=120, schema=None, options=None):
        tipo = {id(lib_memoria._SCHEMA_EXTRAIR): "extrair", id(lib_memoria._SCHEMA_DECIDIR): "deduplicar",
                id(lib_memoria._SCHEMA_AUDITAR): "auditar"}[id(schema)]
        self.chamadas.append((tipo, prompt))
        fila = self.filas[tipo]
        r = fila.pop(0) if fila else None
        return r(prompt) if callable(r) else r

    def quantas(self, tipo):
        return sum(1 for t, _ in self.chamadas if t == tipo)


def fato(texto, sujeito="Pedro", tipo="preferencia", validade="permanente"):
    return {"fato": texto, "sujeito": sujeito, "tipo": tipo, "validade": validade}


def auditoria_sem_mudanca(prompt):
    """Resposta da auditoria que devolve todos os fatos do prompt como estão."""
    entrada = json.loads(prompt.split("FATOS:\n", 1)[1])
    return {"fatos": [{k: f[k] for k in ("id", "fato", "tipo", "validade")} for f in entrada]}


class Base(unittest.TestCase):
    mockar_ollama = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="alfred-memoria-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pasta = self.tmp / "vault" / "memoria"
        # Ambiente só durante o teste: outros módulos de teste rodam no mesmo processo
        for p in (mock.patch.dict(os.environ, {"VAULT_ALFRED": str(self.tmp / "vault"), "MEMORIA_DB": "",
                                               "MODEL_CLASSIFY": "", "OLLAMA_BASE_URL": "http://127.0.0.1:9"}),
                  mock.patch("_shared.perfis.load", return_value={})):
            p.start()
            self.addCleanup(p.stop)
        if self.mockar_ollama:
            self.llm = LLMFalso()
            self.embed = mock.Mock(side_effect=embed_falso)
            for p in (mock.patch.object(lib_memoria, "ollama_generate_json", self.llm),
                      mock.patch.object(lib_memoria, "ollama_embed", self.embed)):
                p.start()
                self.addCleanup(p.stop)

    def cfg(self, **memoria):
        """Troca a seção memoria: do perfis.yml até o fim do teste."""
        p = mock.patch("_shared.perfis.load", return_value={"memoria": memoria})
        p.start()
        self.addCleanup(p.stop)

    def espelho(self, chave="pedro") -> str:
        return (self.pasta / f"{chave}.md").read_text(encoding="utf-8")

    def textos(self, sujeito=None):
        return sorted(f["fato"] for f in lib_memoria.listar(sujeito))


# ══════════════════════════════════════════════════════════════

class TestPortao(Base):
    def test_rejeita_saudacao_emoji_e_risada(self):
        for t in ("", "oi", "Bom dia! Tudo bem? 😀", "ok, valeu!", "😂😂😂", "kkkkkkkk",
                  "Boa noite, obrigado, abraços", "hahahaha rsrsrs kkkkkk", None):
            self.assertFalse(lib_memoria._vale_extrair(t), t)

    def test_aceita_fato_curto(self):
        for t in ("Sou alérgico a camarão, anota aí", "Meu aniversário é dia 3 de maio"):
            self.assertTrue(lib_memoria._vale_extrair(t), t)


class TestExtrair(Base):
    def test_extrai_guarda_e_espelha(self):
        self.llm.filas["extrair"].append({"fatos": [
            fato("Prefere café sem açúcar.", sujeito="eu"),
            fato("é cliente da AttanoTech", sujeito="Carlos Silva", tipo="relacao"),
        ]})
        out = lib_memoria.extrair("Eu tomo café sem açúcar. O Carlos Silva é cliente da AttanoTech.",
                                  "whatsapp", sujeito="Pedro")
        self.assertEqual([(f["fato"], f["sujeito"], f["acao"]) for f in out],
                         [("Prefere café sem açúcar", "Pedro", "novo"),
                          ("é cliente da AttanoTech", "Carlos Silva", "novo")])
        self.assertEqual(len(lib_memoria.listar()), 2)
        self.assertTrue((self.pasta / "memoria.db").exists())
        pedro = self.espelho("pedro")
        self.assertIn('sujeito: "Pedro"', pedro)
        self.assertIn("- Prefere café sem açúcar", pedro)
        self.assertIn("é cliente da AttanoTech", self.espelho("carlos-silva"))
        self.assertEqual(list(self.pasta.glob(".*.tmp")), [])

    def test_prompt_leva_texto_fonte_sujeito_e_data(self):
        self.llm.filas["extrair"].append({"fatos": []})
        lib_memoria.extrair("Estou montando um homelab com Proxmox", "email", sujeito="Ana")
        prompt = self.llm.chamadas[0][1]
        self.assertIn("Estou montando um homelab com Proxmox", prompt)
        self.assertIn("FONTE: email", prompt)
        self.assertIn('("eu", "meu") é: Ana', prompt)
        self.assertIn(f"Hoje é {lib_memoria.date.today().isoformat()}", prompt)

    def test_normaliza_saida_do_llm(self):
        self.llm.filas["extrair"].append({"fatos": [
            fato("gosta de chá verde", tipo="comida", validade="amanhã"),
            fato("treina na academia", sujeito="", validade="até 2099-11-15"),
            fato("mora em Bauru", sujeito="Pedro Netto", validade="2026-13-40"),
            fato("", sujeito="Ana"), "lixo", {"sem": "fato"},
        ]})
        out = lib_memoria.extrair("Texto qualquer com fatos sobre a Ana e o Pedro", "teste", sujeito="Ana")
        self.assertEqual([(f["fato"], f["sujeito"], f["tipo"], f["validade"]) for f in out], [
            ("gosta de chá verde", "Pedro", "outro", "permanente"),
            ("treina na academia", "Ana", "preferencia", "2099-11-15"),
            ("mora em Bauru", "Pedro", "preferencia", "permanente"),
        ])

    def test_parte_do_nome_de_quem_escreveu_vira_o_nome_completo(self):
        # gemma4 devolveu "Carlos" para um texto de "Carlos Silva" — não pode virar outro sujeito
        self.llm.filas["extrair"].append({"fatos": [fato("é vegetariano", sujeito="carlos"),
                                                    fato("mora em Bauru", sujeito="Pedro")]})
        out = lib_memoria.extrair("Sou vegetariano, e você continua em Bauru?", "whatsapp", sujeito="Carlos Silva")
        self.assertEqual([f["sujeito"] for f in out], ["Carlos Silva", "Pedro"])

    def test_sem_sujeito_o_fato_fica_com_pedro(self):
        self.llm.filas["extrair"].append({"fatos": [fato("tem um homelab", sujeito="")]})
        out = lib_memoria.extrair("Montei um homelab com três máquinas", "cli")
        self.assertEqual(out[0]["sujeito"], "Pedro")
        self.assertIn("não informado", self.llm.chamadas[0][1])

    def test_portao_negado_nao_chama_llm(self):
        self.assertEqual(lib_memoria.extrair("ok, valeu!", "whatsapp"), [])
        self.assertEqual(self.llm.chamadas, [])
        self.embed.assert_not_called()

    def test_resposta_invalida_devolve_vazio(self):
        for resp in (None, {"fatos": "nenhum"}, [], {"outra": 1}, {"fatos": [{"fato": ""}]}):
            self.llm.filas["extrair"].append(resp)
            self.assertEqual(lib_memoria.extrair("Sou alérgico a camarão, anota aí", "teste"), [], resp)
        self.assertEqual(lib_memoria.listar(), [])

    def test_memoria_desligada_nao_chama_llm(self):
        self.cfg(ativo=False)
        self.assertEqual(lib_memoria.extrair("Sou alérgico a camarão, anota aí", "teste"), [])
        self.assertEqual(self.llm.chamadas, [])

    def test_sem_vault_nem_db_tudo_vazio(self):
        with mock.patch.dict(os.environ, {"VAULT_ALFRED": "", "MEMORIA_DB": ""}):
            self.assertIsNone(lib_memoria.db_path())
            self.assertEqual(lib_memoria.extrair("Sou alérgico a camarão, anota aí", "teste"), [])
            self.assertEqual(lib_memoria.guardar([fato("prefere café")]), [])
            self.assertEqual(lib_memoria.listar(), [])
            self.assertEqual(lib_memoria.auditar(), {})
            self.assertEqual(lib_memoria.contexto("café"), "")
        self.assertEqual(self.llm.chamadas, [])

    def test_memoria_db_troca_o_caminho(self):
        db = self.tmp / "outro" / "m.db"
        with mock.patch.dict(os.environ, {"MEMORIA_DB": str(db)}):
            lib_memoria.guardar([fato("prefere café")])
        self.assertTrue(db.exists())
        self.assertTrue((db.parent / "pedro.md").exists())


class TestDedup(Base):
    def setUp(self):
        super().setUp()
        self.antigo = lib_memoria.guardar([fato("prefere café sem açúcar")], "whatsapp")[0]

    def test_texto_igual_descarta_sem_llm(self):
        out = lib_memoria.guardar([fato("Prefere café, sem açúcar.")])
        self.assertEqual((out[0]["acao"], out[0]["id"]), ("descartado", self.antigo["id"]))
        self.assertEqual(self.llm.chamadas, [])
        self.assertEqual(self.textos(), ["prefere café sem açúcar"])

    def test_fundir(self):
        self.llm.filas["deduplicar"].append({"decisao": "fundir", "fato": "prefere café coado, sem açúcar"})
        out = lib_memoria.guardar([fato("prefere café coado")])
        self.assertEqual((out[0]["acao"], out[0]["id"]), ("fundido", self.antigo["id"]))
        self.assertEqual(self.textos(), ["prefere café coado, sem açúcar"])
        prompt = self.llm.chamadas[0][1]
        self.assertIn("ANTIGO: prefere café sem açúcar", prompt)
        self.assertIn("NOVO: prefere café coado", prompt)
        self.assertIn("prefere café coado, sem açúcar", self.espelho())

    def test_manter_antigo(self):
        self.llm.filas["deduplicar"].append({"decisao": "manter_antigo", "fato": ""})
        out = lib_memoria.guardar([fato("toma café sem açúcar")])
        self.assertEqual(out[0]["acao"], "descartado")
        self.assertEqual(self.textos(), ["prefere café sem açúcar"])

    def test_manter_novo(self):
        self.llm.filas["deduplicar"].append({"decisao": "manter_novo", "fato": ""})
        out = lib_memoria.guardar([fato("parou de tomar café")])
        self.assertEqual(out[0]["acao"], "substituiu")
        self.assertNotEqual(out[0]["id"], self.antigo["id"])
        self.assertEqual(self.textos(), ["parou de tomar café"])
        self.assertNotIn("sem açúcar", self.espelho())

    def test_sujeitos_diferentes_nao_deduplicam(self):
        lib_memoria.guardar([fato("prefere café sem açúcar", sujeito="Carlos")])
        self.assertEqual(self.llm.chamadas, [])
        self.assertEqual(len(lib_memoria.listar()), 2)

    def test_abaixo_do_limiar_nao_chama_llm(self):
        lib_memoria.guardar([fato("mora em Bauru"), fato("prefere café e chá")])   # café+chá: cosseno 0,71
        self.assertEqual(self.llm.chamadas, [])
        self.assertEqual(len(lib_memoria.listar()), 3)

    def test_decisao_falha_guarda_os_dois(self):
        for resp in (None, {"decisao": "talvez", "fato": ""}, {"decisao": "fundir", "fato": ""}):
            self.llm.filas["deduplicar"].append(resp)
        textos = ["prefere café coado", "toma café toda manhã", "café só depois do almoço"]
        out = lib_memoria.guardar([fato(t) for t in textos])
        self.assertEqual([f["acao"] for f in out], ["novo"] * 3)
        self.assertEqual(len(lib_memoria.listar()), 4)


class TestAuditoria(Base):
    def guardar(self, *textos, **kw):
        return [f["id"] for f in lib_memoria.guardar([fato(t, **kw) for t in textos], "teste")]

    def test_junta_reescreve_e_remove(self):
        ids = self.guardar("prefere café sem açúcar", "toma café sem açúcar", "mora em bauru sp ali", "bom dia")
        self.llm.filas["auditar"].append({"fatos": [
            {"id": ids[0], "fato": "prefere café sem açúcar", "tipo": "preferencia", "validade": "permanente"},
            {"id": ids[2], "fato": "mora em Bauru (SP)", "tipo": "identidade", "validade": "permanente"},
        ]})
        res = lib_memoria.auditar()
        self.assertEqual((res["antes"], res["depois"], res["reescritos"], res["removidos"]), (4, 2, 1, 2))
        self.assertEqual(res["auditados"], ["pedro"])
        self.assertEqual(self.textos(), ["mora em Bauru (SP)", "prefere café sem açúcar"])
        espelho = self.espelho()
        self.assertIn("mora em Bauru (SP)", espelho)
        self.assertNotIn("bom dia", espelho)
        self.assertIn('"id": %d' % ids[3], self.llm.chamadas[-1][1])

    def test_segunda_auditoria_sem_mudanca_pula_llm(self):
        self.guardar("prefere café sem açúcar", "mora em Bauru")
        self.llm.filas["auditar"] += [auditoria_sem_mudanca, auditoria_sem_mudanca]
        self.assertEqual(lib_memoria.auditar()["auditados"], ["pedro"])
        res = lib_memoria.auditar()
        self.assertEqual((res["auditados"], res["pulados"]), ([], ["pedro"]))
        self.assertEqual(self.llm.quantas("auditar"), 1)

    def test_auditoria_que_reescreve_tambem_fica_pulada_depois(self):
        ids = self.guardar("mora em bauru sp ali")
        self.llm.filas["auditar"].append({"fatos": [
            {"id": ids[0], "fato": "mora em Bauru (SP)", "tipo": "identidade", "validade": "permanente"}]})
        lib_memoria.auditar()
        self.assertEqual(lib_memoria.auditar()["pulados"], ["pedro"])
        self.assertEqual(self.llm.quantas("auditar"), 1)

    def test_mudanca_depois_da_auditoria_reaudita_so_aquele_sujeito(self):
        self.guardar("prefere café sem açúcar")
        self.guardar("é cliente da AttanoTech", sujeito="Carlos")
        self.llm.filas["auditar"] += [auditoria_sem_mudanca] * 3
        lib_memoria.auditar()
        self.guardar("mora em Bauru")
        res = lib_memoria.auditar()
        self.assertEqual((res["auditados"], res["pulados"]), (["pedro"], ["carlos"]))
        self.assertEqual(self.llm.quantas("auditar"), 3)

    def test_rede_de_seguranca_recusa_corte_grande(self):
        ids = self.guardar(*[f"fato número {n} sobre a rotina" for n in range(10)])
        self.llm.filas["auditar"].append({"fatos": [
            {"id": i, "fato": "fato", "tipo": "outro", "validade": "permanente"} for i in ids[:2]]})
        res = lib_memoria.auditar()
        self.assertEqual((res["depois"], res["auditados"]), (10, []))
        lib_memoria.auditar()                       # sem impressão salva: tenta de novo
        self.assertEqual(self.llm.quantas("auditar"), 2)

    def test_id_inventado_e_repetido_sao_ignorados(self):
        ids = self.guardar("prefere café sem açúcar")
        self.llm.filas["auditar"].append({"fatos": [
            {"id": ids[0], "fato": "prefere café sem açúcar", "tipo": "preferencia", "validade": "permanente"},
            {"id": ids[0], "fato": "outro texto", "tipo": "outro", "validade": "permanente"},
            {"id": 999, "fato": "fato inventado", "tipo": "outro", "validade": "permanente"},
            {"id": "x", "fato": "id torto"}, "lixo"]})
        lib_memoria.auditar()
        self.assertEqual(self.textos(), ["prefere café sem açúcar"])

    def test_vencido_sai_sem_llm_e_apaga_espelho(self):
        self.guardar("está de férias", validade="2000-01-01")
        self.assertTrue((self.pasta / "pedro.md").exists())
        res = lib_memoria.auditar()
        self.assertEqual((res["expirados"], res["depois"]), (1, 0))
        self.assertEqual(self.llm.quantas("auditar"), 0)
        self.assertFalse((self.pasta / "pedro.md").exists())

    def test_llm_fora_nao_muda_nada_e_tenta_de_novo(self):
        self.guardar("prefere café sem açúcar", "mora em Bauru")
        res = lib_memoria.auditar()
        self.assertEqual((res["depois"], res["auditados"]), (2, []))
        lib_memoria.auditar()
        self.assertEqual(self.llm.quantas("auditar"), 2)

    def test_lotes(self):
        self.cfg(auditar_lote=2)
        self.guardar("prefere café sem açúcar", "mora em Bauru", "treina na academia")
        self.llm.filas["auditar"] += [auditoria_sem_mudanca] * 2
        self.assertEqual(lib_memoria.auditar()["auditados"], ["pedro"])
        self.assertEqual(self.llm.quantas("auditar"), 2)

    def test_completa_vetor_que_faltou(self):
        self.embed.side_effect = lambda *a, **k: []
        self.guardar("prefere café sem açúcar")
        self.embed.side_effect = embed_falso
        self.llm.filas["auditar"].append(auditoria_sem_mudanca)
        lib_memoria.auditar()
        self.llm.filas["deduplicar"].append({"decisao": "manter_antigo", "fato": ""})
        self.assertEqual(lib_memoria.guardar([fato("toma café sem açúcar")])[0]["acao"], "descartado")


class TestContexto(Base):
    def setUp(self):
        super().setUp()
        lib_memoria.guardar([fato("prefere café sem açúcar"), fato("mora em Bauru", tipo="identidade"),
                             fato("é cliente da AttanoTech", sujeito="Carlos Silva", tipo="relacao"),
                             fato("faz aniversário em 3 de maio", tipo="identidade")], "teste")

    def linhas(self, texto):
        return texto.splitlines()[1:]

    def test_traz_o_mais_relevante_primeiro(self):
        txt = lib_memoria.contexto("vou passar um café para o Pedro")
        self.assertTrue(txt.startswith("Fatos que Alfred já sabe"))
        self.assertEqual(self.linhas(txt)[0], "- [Pedro] prefere café sem açúcar")

    def test_respeita_k(self):
        self.assertEqual(len(self.linhas(lib_memoria.contexto("café do Pedro em Bauru", k=1))), 1)
        self.assertEqual(lib_memoria.contexto("café", k=0), "")

    def test_acha_pelo_nome_do_sujeito(self):
        self.assertIn("- [Carlos Silva] é cliente da AttanoTech", lib_memoria.contexto("rascunho para o Carlos"))

    def test_nada_relevante_devolve_vazio(self):
        self.assertEqual(lib_memoria.contexto("xyzw qwerty"), "")
        self.assertEqual(lib_memoria.contexto("   "), "")

    def test_banco_vazio_nao_chama_embedding(self):
        with mock.patch.dict(os.environ, {"VAULT_ALFRED": str(self.tmp / "vazio")}):
            self.embed.reset_mock()
            self.assertEqual(lib_memoria.contexto("café"), "")
            self.embed.assert_not_called()

    def test_vencido_fica_de_fora(self):
        lib_memoria.guardar([fato("está no homelab até domingo", validade="2000-01-01")])
        self.assertEqual(lib_memoria.contexto("homelab"), "")

    def test_sem_embedding_usa_so_fts(self):
        self.embed.side_effect = lambda *a, **k: []
        self.assertIn("AttanoTech", lib_memoria.contexto("quem é da AttanoTech?"))


class TestOllamaFora(Base):
    """Sem mock: ollama_generate_json/ollama_embed de verdade contra 127.0.0.1:9 (conexão recusada)."""
    mockar_ollama = False

    def test_extrair_devolve_vazio_sem_excecao(self):
        self.assertEqual(lib_memoria.extrair("Sou alérgico a camarão e moro em Bauru", "teste"), [])
        self.assertEqual(lib_memoria.listar(), [])

    def test_guardar_contexto_e_auditar_seguem_sem_ollama(self):
        out = lib_memoria.guardar([fato("é alérgico a camarão", tipo="saude")])
        self.assertEqual(out[0]["acao"], "novo")
        self.assertIn("camarão", lib_memoria.contexto("pode servir camarão?"))
        res = lib_memoria.auditar()
        self.assertEqual((res["depois"], res["auditados"]), (1, []))


class TestEspelho(Base):
    def test_falha_na_gravacao_preserva_o_arquivo_antigo(self):
        lib_memoria.guardar([fato("prefere café sem açúcar")])
        antes = self.espelho()
        with mock.patch.object(lib_memoria.os, "replace", side_effect=OSError("disco cheio")):
            lib_memoria.guardar([fato("mora em Bauru")])
        self.assertEqual(self.espelho(), antes)
        self.assertEqual(list(self.pasta.glob(".*.tmp")), [])


class TestCLI(Base):
    def rodar(self, *argv) -> str:
        buf = io.StringIO()
        # log() também escreve no stdout; aqui só interessa a saída da CLI
        with mock.patch.object(lib_memoria, "load_env"), mock.patch.object(lib_memoria, "log"), \
                redirect_stdout(buf):
            self.assertEqual(lib_memoria.main(list(argv)), 0)
        return buf.getvalue()

    def test_extrair_listar_buscar_auditar(self):
        self.llm.filas["extrair"].append({"fatos": [fato("é alérgica a camarão", sujeito="", tipo="saude")]})
        out = json.loads(self.rodar("extrair", "Sou alérgica a camarão, viu?", "--sujeito", "Ana"))
        self.assertEqual((out[0]["sujeito"], out[0]["acao"]), ("Ana", "novo"))
        self.assertEqual(len(json.loads(self.rodar("listar", "--sujeito", "ana"))), 1)
        self.assertIn("- [Ana] é alérgica a camarão", self.rodar("buscar", "camarão"))
        self.assertIn("nada na memória", self.rodar("buscar", "xyzw"))
        self.assertEqual(json.loads(self.rodar("auditar"))["antes"], 1)


if __name__ == "__main__":
    unittest.main()
