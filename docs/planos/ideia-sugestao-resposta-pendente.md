# Ideia (futuro) — Sugestão proativa de resposta a mensagens não respondidas

Status: ideia, não planejada ainda.

## Problema

Hoje o Alfred já guarda fila de pendências "o que você quer responder?" (WhatsApp, `alfred_pendencias.py`),
mas é reativa: só aparece quando alguém manda mensagem pra Pedro. Não existe análise do lado
contrário — mensagens que **Pedro mandou** e não teve resposta, ou conversas que travaram dos dois lados.

Exemplo: Pedro manda "vamos marcar X, precisamos fazer Y" ou uma pergunta direta e amigável, e ninguém
respondeu. Hoje isso se perde — Alfred não acompanha.

## Ideia

Alfred passa a avaliar o próprio histórico de mensagens (enviadas e recebidas, `wa_msgs` / `lib_db.py`)
para achar:
- mensagens de Pedro sem resposta do contato;
- mensagens do contato sem resposta de Pedro (já cobre via pendências, mas sem peso de urgência/amizade).

Pra cada caso, cruzar dois critérios:
1. **Amizade/proximidade** com a pessoa (sinal: frequência de troca, histórico, talvez categoria manual de contato).
2. **Urgência/natureza da mensagem** (ex.: convite com data, pedido direto, pergunta aberta e sincera
   pesa mais que post genérico ou mensagem de grupo).

E a partir disso, gerar uma **sugestão de envio de mensagem** priorizada — tipo um "Pedro, você perguntou
X pro [contato] há 3 dias e não respondeu / não te responderam, quer que eu prepare um follow-up?".

## Onde provavelmente entra

- Classificação: estender `services/inbox/whatsapp.py` (já classifica/enfileira) ou nova rotina periódica
  (estilo `vigia.py`, roda a cada N min/h) que varre `wa_msgs` procurando threads sem resposta há X tempo.
- Peso amizade/urgência: precisa de um classificador (pode ser o próprio LLM local, tipo o que já
  classifica convites/pendências) — não regra fixa, texto livre varia demais.
- Saída: nova categoria de pendência em `alfred_pendencias.py` (ex. "sugestão de follow-up") ou aba
  própria no Painel, com opção "Enviar", "Ignorar", "Lembrar depois".

## Em aberto

- Definir o que conta como "amigo próximo" (manual? aprendido pela frequência?).
- Decidir threshold de tempo sem resposta por tipo de contato.
- Evitar alarme falso: mensagem tipo "ok", "👍" não deve contar como pendência.
