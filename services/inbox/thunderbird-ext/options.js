"use strict";
const CAMPOS = ["urlAlfred", "token", "modoTeste", "pastaTeste", "executarDeVerdade", "intervaloMin"];
const PADRAO = { urlAlfred: "http://127.0.0.1:7072", token: "", contasPermitidas: [], modoTeste: true,
  pastaTeste: "Alfred-Teste", executarDeVerdade: false, intervaloMin: 10 };

async function carregar() {
  const cfg = { ...PADRAO, ...(await messenger.storage.local.get([...CAMPOS, "contasPermitidas", "ultimoCiclo"])) };
  for (const c of CAMPOS) {
    const el = document.getElementById(c);
    if (el.type === "checkbox") el.checked = !!cfg[c]; else el.value = cfg[c];
  }
  const div = document.getElementById("contas");
  for (const conta of await messenger.accounts.list(false)) {
    const l = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.value = conta.id; cb.checked = cfg.contasPermitidas.includes(conta.id);
    l.append(cb, ` ${conta.name} (${conta.type})`);
    div.append(l);
  }
  if (cfg.ultimoCiclo) document.getElementById("saida").textContent = JSON.stringify(cfg.ultimoCiclo, null, 1);
}

async function salvar() {
  const cfg = {};
  for (const c of CAMPOS) {
    const el = document.getElementById(c);
    cfg[c] = el.type === "checkbox" ? el.checked : (el.type === "number" ? Number(el.value) : el.value.trim());
  }
  cfg.contasPermitidas = [...document.querySelectorAll("#contas input:checked")].map((cb) => cb.value);
  await messenger.storage.local.set(cfg);
  await messenger.runtime.sendMessage({ tipo: "reagendar" });
  document.getElementById("saida").textContent = "Salvo.";
}

document.getElementById("salvar").addEventListener("click", salvar);
document.getElementById("agora").addEventListener("click", async () => {
  const r = await messenger.runtime.sendMessage({ tipo: "verificar-agora" });
  document.getElementById("saida").textContent = JSON.stringify(r, null, 1);
});
carregar();
