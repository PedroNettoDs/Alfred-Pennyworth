"""Os testes publicam a atividade (lib_atividade) numa pasta temporária, nunca em logs/atividade: senão o que
os testes simulam (ciclos, pendências) apareceria na fila de verdade e na tela, pelo Pestinha."""
import os
import tempfile

os.environ.setdefault("ALFRED_ATIVIDADE_DIR", tempfile.mkdtemp(prefix="alfred-atividade-testes-"))
