"""
Testes de integracoes.py (instalar, atualizar, abrir e fechar o Pestinha) e da tela Integrações do Painel.

    cd services/inbox
    venv/bin/python -m unittest tests.test_integracoes -v

Offline: pasta do pet, units e atividade em diretórios temporários; git, pip e systemctl são simulados
(nenhum download, nenhuma unit de verdade). Só o teste do argv usa processos reais (dois `sleep` em Python).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).parent
sys.path.insert(0, str(AQUI.parent))
sys.path.insert(0, str(AQUI.parent.parent))

from tests.test_painel import Base as BasePainel  # noqa: E402  (ambiente de teste do Painel + helpers)
import integracoes  # noqa: E402
from _shared import lib_atividade  # noqa: E402


class Maquina:
    """git, pip e systemctl de mentira: registra os comandos e cria na pasta o que os de verdade criariam."""

    def __init__(self, pet: Path):
        self.pet = pet
        self.cmds: list[list[str]] = []
        self.ativa = False                 # unit rodando
        self.habilitada = False
        self.sessao = True                 # graphical-session.target
        self.sujo = ""                     # saída de `git status --porcelain`
        self.commit = "aaa1111"
        self.falhar: tuple = ()            # trecho de comando que deve dar erro
        self.segurar: threading.Event | None = None   # o pip espera por este evento

    def __call__(self, cmd, timeout=15):
        cmd = [str(c) for c in cmd]
        self.cmds.append(cmd)
        if self.falhar and all(p in cmd for p in self.falhar):
            return 1, "fatal: deu ruim\nerro: sem rede"
        if cmd[:2] == ["git", "clone"]:
            self.pet.mkdir(parents=True, exist_ok=True)
            (self.pet / "main.py").write_text("")
            (self.pet / "requirements.txt").write_text("")
        elif cmd[1:3] == ["-m", "venv"]:
            (self.pet / "venv" / "bin").mkdir(parents=True)
            (self.pet / "venv" / "bin" / "python").write_text("")
        elif cmd[0].endswith("pip") and self.segurar:
            self.segurar.wait(5)
        elif cmd[0] == "git" and "pull" in cmd:
            self.commit = "bbb2222"
        elif cmd[0] == "git" and "status" in cmd:
            return 0, self.sujo
        elif cmd[0] == "git" and "log" in cmd:
            return 0, self.commit
        elif cmd[0] == "git" and "branch" in cmd:
            return 0, "alfred"
        elif cmd[0] == "pgrep":
            return 1, ""
        elif cmd[:2] == ["systemctl", "--user"]:
            verbo, alvo = cmd[2], cmd[-1]
            if verbo == "is-active":
                ok = self.sessao if alvo.endswith(".target") else self.ativa
                return (0, "active") if ok else (3, "inactive")
            if verbo == "is-enabled":
                return (0, "enabled") if self.habilitada else (1, "disabled")
            self.ativa = {"start": True, "stop": False}.get(verbo, self.ativa)
            self.habilitada = {"enable": True, "disable": False}.get(verbo, self.habilitada)
        return 0, ""

    def rodou(self, *trecho: str) -> int:
        return sum(1 for c in self.cmds if all(p in c for p in trecho))


class ComMaquina(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="alfred-integracoes-test-"))
        self.pet = self.tmp / "pet"
        self.maq = Maquina(self.pet)
        for p in (mock.patch.dict(os.environ, {"PESTINHA_DIR": str(self.pet), "XDG_CONFIG_HOME": str(self.tmp / "config"),
                                               "ALFRED_ATIVIDADE_DIR": str(self.tmp / "atividade")}),
                  mock.patch.object(integracoes, "_rodar", self.maq)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        integracoes._marcar(acao="", andando=False, passo="", mensagem="", erro="")
        self.unit = self.tmp / "config" / "systemd" / "user" / integracoes.UNIT


class TestInstalar(ComMaquina):
    def test_estado_sem_nada(self):
        e = integracoes.estado()
        self.assertEqual((e["codigo"], e["instalado"], e["ligado"], e["rodando"], e["no_login"], e["unit"]),
                         (False, False, False, False, False, False))
        self.assertEqual((e["pasta"], e["repo"], e["commit"]), (str(self.pet), integracoes.REPO, ""))

    def test_do_zero_baixa_so_do_endereco_fixo_liga_e_grava_a_unit(self):
        passos = []
        self.assertIn(str(self.pet), integracoes.instalar(passos.append))
        clone = next(c for c in self.maq.cmds if c[:2] == ["git", "clone"])
        self.assertEqual(clone[-2:], [integracoes.REPO, str(self.pet)])
        self.assertEqual(clone[clone.index("--branch") + 1], integracoes.RAMO)
        self.assertEqual(self.maq.rodou("sparse-checkout", "set", *integracoes.PASTAS), 1)
        self.assertEqual(self.maq.rodou("install", "-r", str(self.pet / "requirements.txt")), 1)
        self.assertEqual(passos, ["Baixando o código", "Criando o ambiente Python",
                                  "Instalando as dependências (PySide6, numpy)", "Ligando ao Alfred"])
        self.assertEqual(json.loads((self.pet / "memory" / "alfred.json").read_text()),
                         {"pasta": str(lib_atividade.pasta())})
        unit = self.unit.read_text(encoding="utf-8")
        self.assertNotIn("@ALFRED_DIR@", unit)
        self.assertIn(f'ExecStart="{integracoes.PROJECT_ROOT}/services/scripts/alfred-pestinha.sh"', unit)
        self.assertTrue((integracoes.PROJECT_ROOT / "services/scripts/alfred-pestinha.sh").is_file())
        self.assertEqual(self.maq.rodou("enable"), 0)                    # instalar não liga a abertura no login
        e = integracoes.estado()
        self.assertEqual((e["instalado"], e["ligado"], e["unit"], e["ramo"], e["commit"]),
                         (True, True, True, "alfred", "aaa1111"))

    def test_de_novo_nao_baixa_nem_regrava(self):
        integracoes.instalar()
        integracoes.instalar()
        self.assertEqual(self.maq.rodou("git", "clone"), 1)
        self.assertEqual(self.maq.rodou("-m", "venv"), 1)
        self.assertEqual(self.maq.rodou("daemon-reload"), 1)

    def test_pasta_com_outra_coisa_nao_e_tocada(self):
        self.pet.mkdir()
        (self.pet / "notas.txt").write_text("minhas")
        with self.assertRaisesRegex(integracoes.Falha, "não é o Pestinha"):
            integracoes.instalar()
        self.assertEqual(self.maq.rodou("git", "clone"), 0)
        self.assertEqual([f.name for f in self.pet.iterdir()], ["notas.txt"])

    def test_falha_do_git_vira_texto_legivel(self):
        self.maq.falhar = ("git", "clone")
        with self.assertRaisesRegex(integracoes.Falha, "Não deu para baixar o Pestinha: erro: sem rede"):
            integracoes.instalar()
        self.assertFalse(self.unit.exists())

    def test_atualizar_recusa_com_alteracoes_locais(self):
        with self.assertRaisesRegex(integracoes.Falha, "ainda não está instalado"):
            integracoes.atualizar()
        integracoes.instalar()
        self.maq.sujo = " M pestinha/app.py"
        with self.assertRaisesRegex(integracoes.Falha, "alterações locais"):
            integracoes.atualizar()
        self.assertEqual(self.maq.rodou("pull"), 0)

    def test_atualizar_so_avanca(self):
        integracoes.instalar()
        self.assertIn("aaa1111 → bbb2222", integracoes.atualizar())
        self.assertEqual(self.maq.rodou("pull", "--ff-only"), 1)
        self.assertIn("já estava na última versão", integracoes.atualizar())


class TestAbrirFechar(ComMaquina):
    def test_abrir_pela_unit_fechar_e_login(self):
        with self.assertRaisesRegex(integracoes.Falha, "Instale o Pestinha primeiro"):
            integracoes.abrir()
        integracoes.instalar()
        self.assertEqual(integracoes.abrir(), "Pestinha aberto.")
        self.assertEqual(self.maq.rodou("start", integracoes.UNIT), 1)
        self.assertTrue(integracoes.estado()["rodando"])
        self.assertEqual(integracoes.abrir(), "O Pestinha já está aberto.")
        self.assertEqual(self.maq.rodou("start", integracoes.UNIT), 1)   # não abre duas vezes
        self.assertEqual(integracoes.fechar(), "Pestinha fechado.")
        self.assertFalse(integracoes.estado()["rodando"])
        self.assertIn("vai abrir no login", integracoes.executar("login-ligar"))
        self.assertTrue(integracoes.estado()["no_login"])
        self.assertIn("não abre mais", integracoes.executar("login-desligar"))
        with self.assertRaisesRegex(integracoes.Falha, "Ação desconhecida"):
            integracoes.executar("apagar-tudo")

    def test_sem_sessao_grafica_nao_abre(self):
        integracoes.instalar()
        self.maq.sessao = False
        with self.assertRaisesRegex(integracoes.Falha, "Sem sessão gráfica"):
            integracoes.abrir()
        self.assertEqual(self.maq.rodou("start", integracoes.UNIT), 0)

    def test_aberto_a_mao_conta_como_aberto(self):
        integracoes.instalar()
        with mock.patch.object(integracoes, "_pids_do_pet", lambda p: [4242]):
            e = integracoes.estado()
            self.assertEqual((e["rodando"], e["a_mao"]), (True, True))
            self.assertIn("aberto à mão", integracoes.abrir())
        self.assertEqual(self.maq.rodou("start", integracoes.UNIT), 0)


class TestArgv(unittest.TestCase):
    def test_so_o_processo_do_pet_conta(self):
        """Fechar manda SIGTERM a estes pids: um processo que só tem o caminho na linha de comando não pode entrar."""
        tmp = Path(tempfile.mkdtemp(prefix="alfred-integracoes-argv-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "main.py").write_text("import time\ntime.sleep(30)\n")
        pet = subprocess.Popen([sys.executable, str(tmp / "main.py")])
        outro = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", str(tmp / "main.py")])
        self.addCleanup(lambda: [p.kill() or p.wait() for p in (pet, outro)])
        fim = time.time() + 5
        while time.time() < fim and pet.pid not in integracoes._pids_do_pet(tmp):
            time.sleep(0.05)
        self.assertEqual(integracoes._pids_do_pet(tmp), [pet.pid])
        self.assertEqual(integracoes._pids_do_pet(tmp / "outra"), [])


class TestTarefa(ComMaquina):
    def test_uma_por_vez_e_o_passo_aparece(self):
        self.maq.segurar = threading.Event()
        t = integracoes.iniciar("instalar")
        self.assertIsNone(integracoes.iniciar("atualizar"))              # já há uma em andamento
        fim = time.time() + 5
        while time.time() < fim and not integracoes.tarefa()["passo"].startswith("Instalando"):
            time.sleep(0.01)
        self.assertEqual({k: integracoes.tarefa()[k] for k in ("acao", "andando")}, {"acao": "instalar", "andando": True})
        self.maq.segurar.set()
        t.join(5)
        fim_t = integracoes.tarefa()
        self.assertEqual((fim_t["andando"], fim_t["erro"]), (False, ""))
        self.assertIn("instalado", fim_t["mensagem"])

    def test_erro_fica_na_tarefa(self):
        self.maq.falhar = ("git", "clone")
        integracoes.iniciar("instalar").join(5)
        t = integracoes.tarefa()
        self.assertEqual((t["andando"], t["mensagem"]), (False, ""))
        self.assertIn("Não deu para baixar", t["erro"])


# ── tela do Painel ────────────────────────────────────────────

def _estado(**mudancas) -> dict:
    e = {"pasta": "/home/x/dev/pestinha", "repo": integracoes.REPO, "codigo": True, "venv": True, "instalado": True,
         "ramo": "alfred", "commit": "aaa1111", "ligado": True, "unit": True, "rodando": False, "a_mao": False,
         "no_login": False, "xprintidle": True, "sessao_grafica": True, "publicando": True,
         "tarefa": {"acao": "", "andando": False, "passo": "", "mensagem": "", "erro": ""}}
    e.update(mudancas)
    return e


API = "/painel/api/integracoes/pestinha/"


class TestTela(BasePainel):
    def ver(self, cli, url="/painel/integracoes", **estado):
        with mock.patch.object(integracoes, "estado", lambda: _estado(**estado)):
            return self.guardar(cli.get(url, follow_redirects=False))

    def test_exige_sessao(self):
        r = self.ver(self.cliente())
        self.assertEqual(r.status_code, 303)
        self.assertIn("/painel/login", r.headers["location"])
        self.assertEqual(self.ver(self.cliente(), "/painel/parcial/integracoes").status_code, 401)

    def test_botoes_seguem_o_estado(self):
        cli = self.entrar()
        r = self.ver(cli)
        self.assertEqual(r.status_code, 200)
        self.assertIn('href="/painel/integracoes" aria-current="page"', r.text)
        for texto in ("alfred · aaa1111", API + "abrir", API + "login-ligar", API + "atualizar"):
            self.assertIn(texto, r.text)
        for texto in (API + "instalar", API + "fechar"):
            self.assertNotIn(texto, r.text)

        aberto = self.ver(cli, "/painel/parcial/integracoes", rodando=True, a_mao=True, no_login=True).text
        self.assertIn(API + "fechar", aberto); self.assertIn("Aberto à mão", aberto)
        self.assertIn(API + "login-desligar", aberto); self.assertNotIn(API + "abrir", aberto)

        novo = self.ver(cli, "/painel/parcial/integracoes", codigo=False, venv=False, instalado=False, ligado=False,
                        commit="", xprintidle=False, publicando=False).text
        self.assertIn(API + "instalar", novo); self.assertIn(integracoes.REPO, novo)
        self.assertIn("sudo apt install xprintidle", novo); self.assertIn("fora do ar", novo)
        for texto in (API + "abrir", API + "atualizar", "Abrir no login"):
            self.assertNotIn(texto, novo)

    def test_tarefa_em_andamento_esconde_os_botoes(self):
        cli = self.entrar()
        tarefa = {"acao": "instalar", "andando": True, "passo": "Baixando o código", "mensagem": "", "erro": ""}
        html = self.ver(cli, "/painel/parcial/integracoes", tarefa=tarefa).text
        self.assertIn("Instalando: Baixando o código", html)
        self.assertNotIn("data-acao", html)
        erro = self.ver(cli, "/painel/parcial/integracoes", tarefa=dict(tarefa, andando=False, erro="<b>sem rede</b>")).text
        self.assertIn("&lt;b&gt;sem rede&lt;/b&gt;", erro)                # texto do erro escapado

    def test_acoes(self):
        cli = self.entrar()
        token = self.csrf(cli)
        with mock.patch.object(integracoes, "executar", return_value="Pestinha aberto.") as executar, \
                mock.patch.object(integracoes, "iniciar", return_value=object()) as iniciar:
            self.assertEqual(self.acao(cli, API + "abrir", csrf=False).status_code, 403)
            self.assertEqual(self.acao(cli, API + "abrir", csrf=token, origem="http://evil.example").status_code, 403)
            self.assertEqual(self.acao(self.cliente(), API + "abrir", csrf=token).status_code, 401)
            self.assertEqual(self.acao(cli, API + "apagar-tudo", csrf=token).status_code, 404)
            executar.assert_not_called(); iniciar.assert_not_called()

            r = self.acao(cli, API + "abrir", csrf=token)
            self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "mensagem": "Pestinha aberto."}))
            executar.assert_called_once_with("abrir")

            r = self.acao(cli, API + "instalar", csrf=token)                # longa: segue em segundo plano
            self.assertEqual(r.status_code, 200); iniciar.assert_called_once_with("instalar")
            iniciar.return_value = None
            self.assertEqual(self.acao(cli, API + "atualizar", csrf=token).status_code, 409)

            executar.side_effect = integracoes.Falha("Sem sessão gráfica ativa.")
            r = self.acao(cli, API + "abrir", csrf=token)
            self.assertEqual((r.status_code, r.json()["erro"]), (409, "Sem sessão gráfica ativa."))


if __name__ == "__main__":
    unittest.main()
