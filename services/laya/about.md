# laya — decisões tipadas locais (System 1)

Serve o modelo [Laya](https://huggingface.co/convaiinnovations/laya) (Apache-2.0, encoder ModernBERT
com cabeça de decisão) em `127.0.0.1:PORT_LAYA` (padrão 7074) via `laya-serve`. Não gera texto:
responde perguntas tipadas `choice` / `score` / `noul` sobre um estado (texto, e-mail, dict) numa
só passada. Regra do projeto: **o Laya decide, o LLM escreve**
(plano em [docs/planos/odysseus-port.md](../../docs/planos/odysseus-port.md)).

## Como rodar

```bash
./run.sh                                   # cria o venv na 1ª vez e baixa os pesos (~650 MB)
systemctl --user status alfred-laya        # unit instalada pelo services/scripts/instalar.sh
curl -s localhost:7074/health
```

Só escuta em 127.0.0.1. Variáveis (todas opcionais, no `.env` da raiz): `PORT_LAYA`, `LAYA_MODELS`
(padrão `multilingual`, 322M, ~1,3 GB de VRAM), `LAYA_DEVICE` (auto: CUDA se houver),
`LAYA_CONFIANCA_MINIMA` (padrão 0.7), `LAYA_DESLIGADO=true` (força o caminho LLM).

## Uso

```python
from _shared.lib_laya import escolher, pontuar, sim_nao, decidir
escolher(texto, "Which category fits the message?", {"work": "job, meetings", "finance": "bills"})
```

Toda função devolve `None` se o serviço estiver fora do ar, der erro ou a confiança
(`answer_confidence`, a probabilidade da resposta escolhida) ficar abaixo do limiar; o chamador
segue pelo LLM. Testes: `cd services/inbox && venv/bin/python -m unittest tests.test_laya -v`.

## Validação (31/10/2026, RTX 3060)

- Latência: ~30 ms por pergunta na GPU (a 1ª chamada leva ~450 ms).
- `choice` acertou 3/3 mensagens em PT-BR (convite de reunião → personal/work, promoção → marketing,
  boleto → finance), com instruções e rótulos em inglês.
- `noul` **não é confiável** em PT-BR: "promoção 50% off" deu 0.92 para "convida para evento", e
  "me lembra de pagar o boleto" deu 0.006 para "pede lembrete". Prefira `choice` com uma opção
  "nenhuma/outro". A Fase 1 deve medir cada pergunta contra um lote fixo antes de trocar o LLM.

## Benchmark de e-mail (31/10/2026) — Laya zero-shot reprovado

54 e-mails reais (Thunderbird, 7 dias), referência = `classificar()` atual com `gemma4:12b`:

| Formulação | Concordância com o LLM |
|---|---|
| `choice` 7 categorias, critérios em inglês | 15/54 (28%); 52% com `answer_confidence` ≥ 0.9 (23 e-mails) |
| `choice` 7 categorias, critérios em PT | 14/54 (26%) |
| `choice` binário acionável × descartável | 28/54 (52%); 7/14 com confiança ≥ 0.9 |

Erro mais comum: promoção → "contato" (10 de 54), com confiança alta. O critério da Fase 1 (≥ 85%)
não foi atingido, então `core.classificar()` continua só no LLM. Próximo passo possível: treinar
o Laya (`laya-train`) com rótulos do próprio LLM.

Latência medida com o gemma4:12b dividindo a GPU: ~7 s por e-mail no LLM contra ~0,05 s no Laya
(primeiras chamadas até 1,7 s).

## Treino 1 — e-mail (31/10/2026)

`treino/rotular_emails.py` rotulou 615 e-mails com `classificar()` (`gemma4:12b`); 533 com confiança
≥ 0.8 viraram 476 de treino e 57 de teste (divisão fixa por hash). `laya-train` sobre
`laya-multilingual`, 4 épocas, `--shuffle-options`, RTX 3060: ~20 min. Checkpoint em
`treino/dados/modelo-email` (fora do git). Avaliação: `venv/bin/python treino/avaliar.py treino/dados/modelo-email`.

| | Antes | Depois |
|---|---|---|
| Acerto no teste (57) | 26% | 81% |
| Teste sem itens repetidos no treino (53), limiar 0.6 | — | decide 85%, acerto 87% |

Passa no critério de 85% no modo cascata, **mas todos os erros vão na direção cara**: notícia →
promoção/transacional (5) e evento → transacional (1). Com o Laya pulando o LLM, 5–6 de ~29 e-mails
importantes sairiam do jornal. Contato nunca foi perdido. Ainda não ligado ao `classificar()`.
Próximos passos: mais exemplos de notícia (só há 615 e-mails na caixa) e peso maior para as classes
acionáveis, ou usar o Laya só para pular o LLM quando ele disser `promocao`/`transacional` e o
remetente for envio em massa já visto.
