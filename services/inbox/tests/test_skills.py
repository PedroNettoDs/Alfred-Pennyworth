"""
Testes de lib_skills.py (catálogo de skills: procedimentos em texto) e das ferramentas buscar_skill /
listar_skills / ver_skill.

    cd services/inbox
    venv/bin/python -m unittest tests.test_skills -v

Offline: SKILLS_DIR aponta para uma loja temporária com alguns SKILL.md de mentira; nada toca a pasta skills/
do projeto nem a rede.
"""

import sys
import tempfile
import unittest
from pathlib import Path

AQUI = Path(__file__).parent
INBOX = AQUI.parent
sys.path.insert(0, str(INBOX))
sys.path.insert(0, str(INBOX.parent))

from _shared import lib_skills as S          # noqa: E402
from _shared import lib_ferramentas as lf    # noqa: E402


def _skill(raiz: Path, categoria: str, nome: str, descricao: str, corpo: str) -> None:
    d = raiz / categoria / nome
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f'---\nname: {nome}\ndescription: "{descricao}"\n---\n\n{corpo}\n',
        encoding="utf-8",
    )


class TestSkills(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.tmp.name)
        _skill(self.raiz, "web", "offensive-sqli",
               "SQL injection testing. Covers error-based, blind, and UNION SQL injection.",
               "## Procedimento\n1. Mapeie entradas.\n2. Injete payloads SQL.")
        _skill(self.raiz, "privesc", "offensive-linux-privesc",
               "Linux privilege escalation via SUID, sudo, and kernel exploits.",
               "## Procedimento\nRode linpeas. Procure SUID.")
        _skill(self.raiz, "wireless", "offensive-wifi",
               "Wi-Fi attacks against WPA2 networks and handshakes.",
               "## Procedimento\nCapture o handshake. Quebre com hashcat.")
        self._env = S.os.environ.get("SKILLS_DIR")
        S.os.environ["SKILLS_DIR"] = str(self.raiz)
        S._cache["assinatura"] = None   # força recarga nesta loja

    def tearDown(self):
        if self._env is None:
            S.os.environ.pop("SKILLS_DIR", None)
        else:
            S.os.environ["SKILLS_DIR"] = self._env
        S._cache["assinatura"] = None
        self.tmp.cleanup()

    def test_carrega_e_lista(self):
        self.assertEqual(len(S.carregar()), 3)
        self.assertEqual(S.categorias(), ["privesc", "web", "wireless"])
        self.assertEqual([s["nome"] for s in S.listar()],
                         ["offensive-linux-privesc", "offensive-sqli", "offensive-wifi"])
        self.assertEqual([s["nome"] for s in S.listar("wireless")], ["offensive-wifi"])

    def test_busca_rankeia_pelo_assunto(self):
        self.assertEqual(S.buscar("sql injection", 3)[0]["nome"], "offensive-sqli")
        self.assertEqual(S.buscar("escalar privilegio linux", 3)[0]["nome"], "offensive-linux-privesc")
        self.assertEqual(S.buscar("wpa2 wifi handshake", 3)[0]["nome"], "offensive-wifi")

    def test_busca_vazia_e_sem_match(self):
        self.assertEqual(S.buscar(""), [])
        self.assertEqual(S.buscar("zzzzxxxx nada disso"), [])

    def test_ver_corpo_e_inexistente(self):
        corpo = S.ver("offensive-sqli")
        self.assertIn("Injete payloads SQL", corpo)
        self.assertNotIn("name:", corpo)          # frontmatter removido
        self.assertIsNone(S.ver("nao-existe"))

    def test_cache_recarrega_ao_mudar(self):
        self.assertEqual(len(S.carregar()), 3)
        _skill(self.raiz, "cloud", "offensive-cloud", "AWS and Azure attacks.", "## Procedimento\nPacu.")
        self.assertEqual(len(S.carregar()), 4)

    def test_ferramentas_registradas_e_sem_risco(self):
        nomes = [f["function"]["name"] for f in lf.esquemas()]
        for t in ("buscar_skill", "listar_skills", "ver_skill"):
            self.assertIn(t, nomes)
            self.assertEqual(lf.motivo_risco(t), "")   # read-only: roda sem aprovação

    def test_ferramenta_ver_skill_executa(self):
        out = lf.executar("ver_skill", {"nome": "offensive-wifi"})
        self.assertIn("hashcat", out)
        erro = lf.executar("ver_skill", {"nome": "fantasma"})
        self.assertIn("não encontrada", erro)


if __name__ == "__main__":
    unittest.main()
