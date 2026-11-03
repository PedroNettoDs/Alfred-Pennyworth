"use strict";
const CAMPOS = ["urlAlfred", "token", "identidade", "anexarAudio", "enviarDeVerdade", "intervaloMin"];
const PADRAO = { urlAlfred: "http://127.0.0.1:7073", token: "", identidade: "", anexarAudio: true,
  enviarDeVerdade: false, intervaloMin: 10 };

async function carregar() {
  const cfg = { ...PADRAO, ...(await messenger.storage.local.get([...CAMPOS, "ultimoCiclo"])) };
  const sel = document.getElementById("identidade");
  for (const conta of await messenger.accounts.list(false)) {
    for (const ident of conta.identities || []) {
      const op = document.createElement("option");
      op.value = ident.id; op.textContent = `${ident.name || ""} <${ident.email}> — ${conta.name}`;
      sel.append(op);
    }
  }
  for (const c of CAMPOS) {
    const el = document.getElementById(c);
    if (el.type === "checkbox") el.checked = !!cfg[c]; else el.value = cfg[c];
  }
  if (cfg.ultimoCiclo) document.getElementById("saida").textContent = JSON.stringify(cfg.ultimoCiclo, null, 1);
}

async function salvar() {
  const cfg = {};
  for (const c of CAMPOS) {
    const el = document.getElementById(c);
    cfg[c] = el.type === "checkbox" ? el.checked : (el.type === "number" ? Number(el.value) : el.value.trim());
  }
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
