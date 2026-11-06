### Por execução

| modo | pergunta | status | tempo (s) | buscas | URLs buscadas | páginas lidas | fontes no relatório | tokens entrada | tokens saída | modelos (chamadas) | carga de modelo (s) | espera lock (s) | palavras |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|
| rapido | tecnico | ok | 27.4 | 4 | 18 | 0 | 12 | 1008 | 976 | gemma4:12b ×2 | 0.0 | 0.0 | 554 |
| rapido-general | tecnico | ok | 53.5 | 4 | 25 | 0 | 12 | 1446 | 1006 | gemma4:12b ×2 | 26.4 | 177.5 | 552 |
| profundo | tecnico | ok | 291.8 | 5 | 25 | 3 | 1 | 2240 | 1213 | gemma4:e4b ×4, qwen3.6:35b-a3b ×1 | 91.0 | 0.0 | 602 |
| profundo-general | tecnico | ok | 361.5 | 6 | 14 | 6 | 6 | 14384 | 2220 | gemma4:e4b ×7, qwen3.6:35b-a3b ×1 | 90.7 | 380.2 | 596 |
| iterativo | tecnico | ok | 341.2 | 6 | 47 | 14 | 11 | 37418 | 9534 | gemma4:12b ×21 | 74.0 | 127.1 | 1394 |
| rapido | noticia | ok | 21.8 | 4 | 32 | 0 | 12 | 1880 | 811 | gemma4:12b ×2 | 0.0 | 435.0 | 430 |
| rapido-general | noticia | ok | 52.8 | 4 | 16 | 0 | 12 | 1547 | 874 | gemma4:12b ×2 | 25.3 | 134.1 | 492 |
| profundo | noticia | ok | 241.2 | 5 | 28 | 4 | 4 | 8748 | 2582 | gemma4:e4b ×5, qwen3.6:35b-a3b ×1 | 84.4 | 122.0 | 988 |
| profundo-general | noticia | ok | 259.4 | 5 | 40 | 15 | 14 | 33457 | 5187 | gemma4:e4b ×16, qwen3.6:35b-a3b ×1 | 71.7 | 0.0 | 1322 |
| iterativo | noticia | ok | 243.8 | 9 | 28 | 7 | 7 | 17838 | 5604 | gemma4:12b ×13 | 99.4 |  | 1382 |
| rapido | produtos | ok | 53.6 | 4 | 25 | 0 | 12 | 1543 | 981 | gemma4:12b ×2 | 26.1 | 0.0 | 599 |
| rapido-general | produtos | ok | 44.6 | 4 | 27 | 0 | 12 | 1597 | 1051 | gemma4:12b ×2 | 19.4 | 0.0 | 584 |
| profundo | produtos | ok | 213.0 | 5 | 19 | 1 | 1 | 2249 | 2012 | gemma4:e4b ×2, qwen3.6:35b-a3b ×1 | 79.7 | 0.0 | 1168 |
| profundo-general | produtos | ok | 303.8 | 5 | 40 | 14 | 12 | 31955 | 4477 | gemma4:e4b ×15, qwen3.6:35b-a3b ×1 | 72.2 | 0.0 | 1046 |
| iterativo | produtos | ok | 415.6 | 6 | 36 | 13 | 13 | 60339 | 11784 | gemma4:12b ×20 | 96.1 | 0.0 | 1402 |
| rapido | academico | ok | 47.2 | 4 | 25 | 0 | 12 | 1012 | 943 | gemma4:12b ×2 | 22.4 | 0.0 | 555 |
| rapido-general | academico | ok | 48.5 | 4 | 27 | 0 | 12 | 1560 | 1036 | gemma4:12b ×2 | 21.7 | 0.0 | 604 |
| profundo | academico | erro | 32.8 | 5 | 24 | 0 | 0 | 144 | 94 | gemma4:e4b ×1 | 22.2 | 0.0 | 0 |
| profundo-general | academico | ok | 306.2 | 5 | 35 | 10 | 8 | 16330 | 3913 | gemma4:e4b ×11, qwen3.6:35b-a3b ×1 | 77.1 | 0.0 | 1170 |
| iterativo | academico | ok | 279.7 | 6 | 50 | 7 | 7 | 27768 | 7507 | gemma4:12b ×14 | 88.2 | 0.0 | 1431 |
| rapido | brasil | ok | 51.7 | 4 | 28 | 0 | 12 | 1847 | 785 | gemma4:12b ×2 | 25.8 | 0.0 | 443 |
| rapido-general | brasil | ok | 33.5 | 4 | 25 | 0 | 12 | 1331 | 833 | gemma4:12b ×2 | 11.1 | 0.0 | 447 |
| profundo | brasil | ok | 226.3 | 5 | 34 | 14 | 14 | 26091 | 3703 | gemma4:e4b ×15, qwen3.6:35b-a3b ×1 | 75.0 | 0.0 | 589 |
| profundo-general | brasil | ok | 270.9 | 5 | 38 | 15 | 15 | 30770 | 5355 | gemma4:e4b ×16, qwen3.6:35b-a3b ×1 | 76.8 | 0.0 | 1617 |
| iterativo | brasil | ok | 411.6 | 6 | 50 | 16 | 16 | 53064 | 12519 | gemma4:12b ×23 | 82.1 | 0.0 | 1404 |

### Médias por modo

Custo (tempo, URLs, páginas, tokens): média das execuções ok. Qualidade: todas as execuções julgadas — uma falha conta cobertura 0 e não entra nas citações.

| modo | ok / total | tempo (s) | URLs buscadas | páginas lidas | fontes | tokens entrada | tokens saída | citações sustentadas | cobertura | alucinações por relatório |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| rapido | 5 / 5 | 40.3 | 25.6 | 0.0 | 12.0 | 1458.0 | 899.2 | 47% (50) | 48% (5) | 0.2 |
| rapido-general | 5 / 5 | 46.6 | 24.0 | 0.0 | 12.0 | 1496.2 | 960.0 | 90% (50) | 78% (5) | 0.0 |
| profundo | 4 / 5 | 243.1 | 26.5 | 5.5 | 5.0 | 9832.0 | 2377.5 | 94% (35) | 45% (5) | 0.0 |
| profundo-general | 5 / 5 | 300.4 | 33.4 | 12.0 | 11.0 | 25379.2 | 4230.4 | 92% (50) | 85% (5) | 0.2 |
| iterativo | 5 / 5 | 338.4 | 42.2 | 11.4 | 10.8 | 39285.4 | 9389.6 | 72% (48) | 88% (5) | 0.6 |

### Qualidade por execução

| execução | citações (S/P/N/F) | cobertura (4 aspectos) | alucinações | observação |
|---|---|---|---:|---|
| rapido-tecnico | 0/0/10/0 | 0.5 1 1 0 | 1 | os 12 trechos são MDN/Hackage (news,it); texto inteiro vem da memória do modelo; exemplo RestartSec=5s + StartLimitBurst=5 'tenta 5 vezes' é falso com o StartLimitIntervalSec padrão de 10 s |
| rapido-general-tecnico | 9/1/0/0 | 0.5 1 1 1 | 0 | trechos reais (Red Hat, ServerFault, SUSE); o modelo numerou citações sem o prompt pedir; exemplo de unit [Unit]/[Service] tirado do trecho 10 |
| profundo-tecnico | 6/0/0/0 | 0 0 0 0 | 0 | só 6 frases citam; 1 nota (MDN onRestartRequired) passou no filtro de cosseno; o relatório diz com honestidade que as fontes não cobrem systemd — citação certa, conteúdo inútil |
| profundo-general-tecnico | 8/1/1/0 | 1 1 1 1 | 1 | relevância 14/14 com general; 'padrão é on-failure' atribuído a [1][3] mas vem da nota do Plesk (padrão do Plesk, não do systemd) e [1] diz o contrário; resto fiel às notas e às páginas |
| iterativo-tecnico | 4/1/4/1 | 1 1 1 0 | 2 | cita '[Descoberta n]' (número da janela de 10, sem URL nem lista no relatório); falsos: padrão 5 e 30 s (o próprio selivan diz 10 s) e 'RestartSec < 100 ms é ignorado para proteger o kernel'; sem exemplo de unit apesar de 3 fontes trazerem |
| rapido-noticia | 8/2/0/0 | 1 0 0 1 | 0 | news,it funciona para notícia: 12 trechos relevantes; sem percentuais nacionais nem datas (só votos de 2 municípios); fiel aos trechos |
| rapido-general-noticia | 8/1/1/0 | 1 1 1 1 | 0 | general também acha notícia (Agência Brasil, TSE, Reuters); traz total nacional de votos e a data do 2º turno, que o news,it não trouxe |
| profundo-noticia | 8/2/0/0 | 1 1 1 1 | 0 | números conferidos nas páginas (51,79/48,21 e 47,03/45,16); plano sem data de hoje gerou subperguntas no futuro (tendências, fatores que 'influenciarão'), então metade do relatório é pré-eleição; 'coalizão Nordeste' vem de manchete de link relacionado de 05/10 |
| profundo-general-noticia | 9/1/0/0 | 0 0 1 1 | 0 | refeito em 05/11 (em 03/11 falhou com SearXNG suspenso); o plano sem a data de hoje pediu só pré-eleição (candidatos, pesquisas de 2024, regras), nenhuma das 14 páginas lidas diz o resultado e o relatório não responde quem venceu — fala em 'empate técnico' das pesquisas; citações corretas |
| iterativo-noticia | 7/1/0/0 | 1 1 1 1 | 0 | só 8 frases citam; números conferidos nas páginas (apuração 1º e 2º turno, diferença calculada certa); de novo '[Descoberta n]' sem URL no texto; rodadas 2 e 3 vazias por SearXNG suspenso |
| rapido-produtos | 6/1/3/0 | 0.5 0 0 0.5 | 0 | news,it: 3 trechos de notícia sobre homelab com Pi 5, o resto MDN (Math.PI, Firefox 1.5) e Hackage; nenhum número de desempenho, consumo ou preço |
| rapido-general-produtos | 9/1/0/0 | 1 1 0 0.5 | 0 | 12 trechos de comparação de verdade (picockpit, makeuseof, xda); consumo 8–10 W e pico 12 W com fonte; sem preço; temperatura do Orange Pi 5 Max atribuída ao Orange Pi 5 |
| profundo-produtos | 8/2/0/0 | 0.5 0 0.5 0 | 0 | news,it: 2 de 40 resultados passaram no filtro, 1 página lida (notícia de jan/2025 do Pi 5 de 16 GB); nada sobre Orange Pi 5 nem consumo; preços conferidos na página mas antigos; relatório honesto sobre as lacunas |
| profundo-general-produtos | 8/2/0/0 | 1 0.5 0.5 1 | 0 | general: 37 de 40 resultados relevantes, 12 páginas lidas; números conferidos nas páginas; erro herdado da fonte [2] (diz que o Orange Pi 5 usa RK3566 e saiu em 2021 — é RK3588S) e o relatório aponta a divergência entre fontes; consumo só de Pi 4 e Orange Pi Zero 3 |
| iterativo-produtos | 4/5/1/0 | 1 0.5 1 1 | 0 | 13 fontes boas de comparação; números conferidos nas páginas (8 núcleos, 30–50%, 2.300 MB/s, 2–6 W), mas '[Descoberta n]' só bate com a fonte certa em parte dos casos (numeração da janela muda entre rodadas); fora da amostra: 'empate' no consumo em repouso contradiz a fonte (2,7–4 W × 5–8 W) e NVMe do Pi 5 'via USB ou GPIO' está errado |
| rapido-academico | 0/0/10/0 | 1 1 0 0 | 0 | news,it: os 12 trechos são MDN (Math.E, <em>, do...while) e Hackage; texto vem da memória do modelo, correto no geral, sem números de evidência nem críticas |
| rapido-general-academico | 7/3/0/0 | 1 1 0.5 0.5 | 0 | general: trechos do arXiv 1803.03635, PMLR (Malach 2020), OpenReview e textos em português; 2 trechos fora do tema (USP psicologia, crítica de voto); '>90%' do resumo do artigo vale para poda em geral, não para a LTH |
| profundo-academico | 0/0/0/0 | 0 0 0 0 | 0 | falhou: news,it devolveu 24 URLs de MDN/Hackage, o filtro de relevância barrou todas e o job terminou em ErroPesquisa — Pedro receberia só o erro |
| profundo-general-academico | 8/2/0/0 | 1 1 1 1 | 0 | general: 26 de 33 resultados relevantes, 8 páginas (arXiv, survey, Wikipedia-like); números conferidos (10–20%, 97% em SNN); IMP com máscara e reinicialização em θ0 e rewinding; único relatório com os 4 aspectos e Frankle et al. ICLR 2019 |
| iterativo-academico | 4/4/2/0 | 0.5 1 1 1 | 1 | general+en acha arXiv, LessWrong e Raschka; 7 fontes < 10, então a numeração [Descoberta n] é direta, mas 4 das 10 citam a fonte errada para algo que outra fonte diz; 'navega sem ficar preso em mínimos locais de sobreajuste' não está em nenhuma; não cita Frankle e Carbin |
| rapido-brasil | 7/2/1/0 | 1 0 0 1 | 0 | news,it serve para tema brasileiro em pauta: 12 trechos de notícia (set/2026, conta-salário); fiel aos trechos, mas nada sobre autorização, cancelamento ou limites |
| rapido-general-brasil | 8/2/0/0 | 1 1 0 0.5 | 0 | general traz bcb.gov.br (página e FAQ para participantes), Wise, BTG, Sicredi; fiel aos trechos; sem prazos nem limites (os trechos de 400 caracteres não chegam lá) |
| profundo-brasil | 9/0/0/0 | 1 1 1 1 | 0 | news,it achou notícia de set/2026: 27 de 35 relevantes, 14 páginas; só 9 frases citam (o qwen escreveu Resumo + Lacunas e pulou as seções por subpergunta); todos os números conferidos nas páginas (2–10 dias, 23h59, R$ 1.000 noturno, IN BCB 746) |
| profundo-general-brasil | 10/0/0/0 | 1 1 1 1 | 0 | general: 35 de 36 relevantes, 15 páginas (g1, iugu, bcb); 1.617 palavras com seções por subpergunta; todos os números da amostra conferidos nas páginas (16/06/2025, 6 meses de empresa ativa, 0,4–1,2%, 182%, 160 e 60 milhões) |
| iterativo-brasil | 10/0/0/0 | 1 1 1 0.5 | 0 | 16 fontes de fintechs e notícia; todas as 10 afirmações estão em alguma fonte, mas os números [n] misturam duas numerações (absoluta e da janela de 10) e o relatório não traz lista nem URL — o leitor não consegue conferir; não diz a data de lançamento (jun/2025) e chama o Pix Automático de 'em implementação gradual' |
