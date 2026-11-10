"""Os testes publicam a atividade (lib_atividade) numa pasta temporária, nunca em logs/atividade: senão o que
os testes simulam (ciclos, pendências) apareceria na fila de verdade e na tela, pelo Pestinha."""
import os
import tempfile

os.environ.setdefault("ALFRED_ATIVIDADE_DIR", tempfile.mkdtemp(prefix="alfred-atividade-testes-"))

# A porta da GPU (lib_porta) também: marcas numa pasta temporária. E nenhum teste fala com o laya-serve de
# verdade: com o checkpoint "porta" no ar, um teste que simula Pedro falando faria os seguintes esperarem.
os.environ.setdefault("ALFRED_PORTA_DIR", tempfile.mkdtemp(prefix="alfred-porta-testes-"))
os.environ.setdefault("LAYA_URL", "http://127.0.0.1:9")
