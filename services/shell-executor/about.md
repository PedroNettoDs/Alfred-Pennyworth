# shell-executor — Gateway de comandos do host

API REST mínima que permite ao Open WebUI executar comandos shell no host sob uma whitelist estrita. Resolve o problema de o Open WebUI (Docker) não ter acesso direto ao sistema operacional do host.

## Como rodar

```bash
./run.sh          # inicia na porta PORT_SHELL_EXECUTOR (default: 7070)
```

Registrado como `alfred-executor.service` no systemd:

```bash
sudo systemctl status alfred-executor
sudo systemctl restart alfred-executor
journalctl -u alfred-executor -f
```

## Endpoints

### `POST /execute`
Executa um comando da whitelist e retorna stdout/stderr.

**Request:**
```json
{
  "command": "ollama list",
  "timeout": 15
}
```

**Response (sucesso):**
```json
{
  "stdout": "NAME                    ID      SIZE   MODIFIED\nllama3.1:8b  ...",
  "stderr": "",
  "returncode": 0
}
```

**Response (comando não permitido):**
```json
{
  "detail": "Comando não permitido: 'rm -rf /'. Consulte ALLOWED_PREFIXES."
}
```
HTTP 403.

### `GET /health`
Retorna `{"status": "ok", "version": "2.0.0"}`.

## Autenticação

Todas as requisições exigem `Authorization: Bearer <SHELL_EXECUTOR_TOKEN>`. O token é definido no `.env` da raiz do projeto. Sem token correto: HTTP 401.

## Whitelist de comandos (`ALLOWED_PREFIXES`)

Apenas comandos cujo início bate com um dos prefixos abaixo são aceitos:

```
docker, df, free, uptime, nvidia-smi, ollama
systemctl status, systemctl is-active
ps aux, top -bn1, cat /proc, lsblk
ip addr, ss -tlnp, uname, lscpu, sensors
du -sh, wc -l, head, tail, grep, find, ls, pwd, whoami, date
curl http://localhost, curl http://127.0.0.1
/home/netto/Alfred-Pennyworth/services/briefing/run.sh
```

A verificação é `cmd.strip().startswith(prefix)` — simples e intencional. Para adicionar um comando novo, edite `ALLOWED_PREFIXES` em `main.py`.

## Casos de uso no Open WebUI

O Open WebUI usa este serviço via tool calling. Exemplos de tools que chamam o executor:

- **`alfred_system_monitor.py`** — `df -h`, `free -h`, `nvidia-smi`, `uptime` → status do servidor
- **`alfred_shell_executor.py`** — execução direta de comandos permitidos
- **`alfred_briefing.py`** (indireto) — dispara `./services/briefing/run.sh` para gerar briefing sob demanda

## Segurança

- **Whitelist por prefixo:** nenhum comando fora da lista passa.
- **Token Bearer:** mesmo dentro da rede local, o caller precisa do token.
- **Timeout:** padrão de 15s, configurável por request (nunca deixa um comando travar indefinidamente).
- **Shell=True intencional:** necessário para suportar flags e pipes nos comandos da whitelist. O risco é mitigado pela whitelist estrita.

## Dependências

```
fastapi, uvicorn
```

Nenhuma lib compartilhada — serviço propositalmente mínimo.

## Variáveis de ambiente

| Variável | Uso |
|----------|-----|
| `SHELL_EXECUTOR_TOKEN` | Token Bearer obrigatório para autenticação |
| `PORT_SHELL_EXECUTOR` | Porta do serviço (default: `7070`) |
