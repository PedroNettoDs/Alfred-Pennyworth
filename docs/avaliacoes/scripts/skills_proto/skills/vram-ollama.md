---
nome: vram-ollama
descricao: Ver o que ocupa a GPU e por que o Ollama está lento ou trocando de modelo.
gatilhos: ["a gpu está cheia", "o modelo está lento", "quanto de vram sobrou", "qual modelo está carregado"]
ferramentas: [status_gpu, executar_comando]
comandos:
  - nvidia-smi
  - ollama ps
  - ollama list
perigosa: false
---
## Quando usar
Pedro reclama de lentidão do chat, de VRAM cheia ou quer saber que modelo está na GPU.

## Passos
1. `status_gpu` (nvidia-smi): VRAM usada/total, uso, processos.
2. `ollama ps`: modelos carregados, tamanho, CPU/GPU, tempo até descarregar.
3. Se houver dois modelos grandes ou um modelo parcialmente na CPU, diga qual e quanto ocupa.
4. Lembre: gemma4:12b ocupa ~8,4 GB; bge-m3 junto pode tirar o 12b da GPU.

## Cuidados
- Não descarregue modelo nem rode `ollama run`/`pull`: só leitura.

## Verificação
- Os números citados vêm do nvidia-smi e do ollama ps desta chamada.
