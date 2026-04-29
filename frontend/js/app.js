(function () {
  "use strict";

  const REGIONS = [
    "us-east-1",
    "us-east-2",
    "us-west-2",
    "eu-west-1",
    "eu-central-1",
    "eu-west-3",
    "sa-east-1",
    "ap-southeast-1",
    "ap-northeast-1",
  ];

  const CHARS_WARN = 48000;

  function apiBase() {
    const meta = document.querySelector('meta[name="portal-api-base"]');
    const fromMeta = meta && meta.getAttribute("content");
    if (fromMeta && fromMeta.trim()) return fromMeta.replace(/\/$/, "");
    if (typeof window.PORTAL_API_BASE === "string" && window.PORTAL_API_BASE.trim()) {
      return window.PORTAL_API_BASE.replace(/\/$/, "");
    }
    return "";
  }

  function url(path) {
    const base = apiBase();
    const p = path.startsWith("/") ? path : "/" + path;
    return base ? base + p : p;
  }

  async function apiPost(path, body) {
    const res = await fetch(url(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const text = await res.text();
    let data;
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      throw new Error(text || "Resposta inválida da API");
    }
    if (!res.ok) {
      throw new Error(data.error || res.statusText || "Erro HTTP");
    }
    if (data.error) throw new Error(data.error);
    return data;
  }

  function bedrockBase(region) {
    return `https://bedrock-mantle.${region}.api.aws/v1`;
  }

  function trimMessages(msgs, maxN) {
    let n = Math.max(2, parseInt(maxN, 10) || 30);
    if (msgs.length <= n) return { list: msgs.slice(), truncated: false };
    let slice = msgs.slice(-n);
    while (slice.length && slice[0].role !== "user") slice = slice.slice(1);
    return { list: slice.length ? slice : msgs.slice(-n), truncated: true };
  }

  function approxChars(msgs) {
    return msgs.reduce((a, m) => a + String(m.content || "").length, 0);
  }

  // --- DOM ---
  const regionEl = document.getElementById("region");
  const endpointHint = document.getElementById("endpointHint");
  const apiKeyEl = document.getElementById("apiKey");
  const btnModels = document.getElementById("btnModels");
  const modelsErr = document.getElementById("modelsErr");
  const modelSelect = document.getElementById("modelSelect");

  REGIONS.forEach((r) => {
    const opt = document.createElement("option");
    opt.value = r;
    opt.textContent = r;
    if (r === "us-east-1") opt.selected = true;
    regionEl.appendChild(opt);
  });

  function syncEndpoint() {
    endpointHint.textContent = "Endpoint: " + bedrockBase(regionEl.value);
  }
  regionEl.addEventListener("change", syncEndpoint);
  syncEndpoint();

  const menuBtns = document.querySelectorAll(".menu__btn");
  const panels = {
    simple: document.getElementById("panel-simple"),
    stream: document.getElementById("panel-stream"),
    persona: document.getElementById("panel-persona"),
  };

  menuBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-panel");
      menuBtns.forEach((b) => b.classList.toggle("is-active", b === btn));
      Object.keys(panels).forEach((k) => {
        panels[k].classList.toggle("is-visible", k === id);
      });
    });
  });

  let modelsList = [];

  btnModels.addEventListener("click", async () => {
    modelsErr.hidden = true;
    modelsErr.textContent = "";
    const key = apiKeyEl.value.trim();
    if (!key) {
      modelsErr.textContent = "Informe a chave da API antes de carregar os modelos.";
      modelsErr.hidden = false;
      return;
    }
    btnModels.disabled = true;
    try {
      const data = await apiPost("/api/models", {
        api_key: key,
        region: regionEl.value,
      });
      modelsList = data.models || [];
      modelSelect.innerHTML = "";
      if (!modelsList.length) {
        modelsErr.textContent = "Nenhum modelo retornado.";
        modelsErr.hidden = false;
        modelSelect.disabled = true;
        return;
      }
      modelsList.forEach((id) => {
        const opt = document.createElement("option");
        opt.value = id;
        opt.textContent = id;
        modelSelect.appendChild(opt);
      });
      modelSelect.disabled = false;
    } catch (e) {
      modelsErr.textContent = String(e.message || e);
      modelsErr.hidden = false;
      modelSelect.innerHTML = "";
      modelSelect.disabled = true;
    } finally {
      btnModels.disabled = false;
    }
  });

  function cfg() {
    return {
      api_key: apiKeyEl.value.trim(),
      region: regionEl.value,
      model_id: modelSelect.value || null,
    };
  }

  // --- Chat simples ---
  document.getElementById("btnSimple").addEventListener("click", async () => {
    const out = document.getElementById("outSimple");
    const c = cfg();
    if (!c.api_key) {
      out.textContent = "Informe a BEDROCK_API_KEY.";
      return;
    }
    if (!c.model_id) {
      out.textContent = "Carregue os modelos e selecione um modelo.";
      return;
    }
    const q =
      document.getElementById("taSimple").value.trim() ||
      "Explique em 2 linhas o que é o Amazon Bedrock.";
    const max_tokens = parseInt(document.getElementById("maxTokSimple").value, 10) || 200;
    out.textContent = "Gerando…";
    try {
      const data = await apiPost("/api/completion", {
        ...c,
        messages: [{ role: "user", content: q }],
        max_tokens,
        stream_collect: false,
      });
      let text = data.content || "";
      if (data.usage) {
        text +=
          "\n\n— Tokens: entrada " +
          data.usage.prompt_tokens +
          ", saída " +
          data.usage.completion_tokens +
          ", total " +
          data.usage.total_tokens;
      }
      out.textContent = text;
    } catch (e) {
      out.textContent = "Erro: " + e.message;
    }
  });

  // --- Chat streaming (histórico) ---
  let chatMessages = [];

  const chatBox = document.getElementById("chatBox");
  const chatForm = document.getElementById("chatForm");
  const chatInput = document.getElementById("chatInput");
  const streamMeta = document.getElementById("streamMeta");
  const streamWarn = document.getElementById("streamWarn");

  function renderChat() {
    chatBox.innerHTML = "";
    chatMessages.forEach((m) => {
      const div = document.createElement("div");
      div.className = "chat-msg chat-msg--" + (m.role === "user" ? "user" : "assistant");
      const pre = document.createElement("pre");
      pre.textContent = m.content;
      div.appendChild(pre);
      chatBox.appendChild(div);
    });
    chatBox.scrollTop = chatBox.scrollHeight;
    streamMeta.textContent =
      "Mensagens na tela (histórico completo): " + chatMessages.length;
  }

  document.getElementById("btnClearChat").addEventListener("click", () => {
    chatMessages = [];
    renderChat();
    streamWarn.hidden = true;
  });

  chatForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const c = cfg();
    const text = chatInput.value.trim();
    if (!text) return;
    chatInput.value = "";

    if (!c.api_key) {
      chatMessages.push({
        role: "assistant",
        content: "Informe a BEDROCK_API_KEY na configuração ao lado.",
      });
      renderChat();
      return;
    }
    if (!c.model_id) {
      chatMessages.push({
        role: "assistant",
        content: "Carregue os modelos e selecione um modelo.",
      });
      renderChat();
      return;
    }

    chatMessages.push({ role: "user", content: text });
    renderChat();

    const max_ctx = parseInt(document.getElementById("maxCtxMsgs").value, 10) || 30;
    const max_tokens = parseInt(document.getElementById("maxTokStream").value, 10) || 200;
    const trimmed = trimMessages(chatMessages, max_ctx);
    streamWarn.hidden = true;
    if (trimmed.truncated) {
      streamWarn.textContent =
        "Apenas as últimas " +
        trimmed.list.length +
        " mensagens foram incluídas no pedido à API (limite configurado).";
      streamWarn.hidden = false;
    }
    const chars = approxChars(trimmed.list);
    if (chars >= CHARS_WARN) {
      streamWarn.textContent =
        (streamWarn.hidden ? "" : streamWarn.textContent + " ") +
        "Contexto alto (~" +
        chars.toLocaleString() +
        " caracteres). Considere limpar a conversa.";
      streamWarn.hidden = false;
    }

    try {
      const data = await apiPost("/api/completion", {
        ...c,
        messages: trimmed.list,
        max_tokens,
        stream_collect: true,
      });
      const full =
        data.content ||
        (Array.isArray(data.chunks) ? data.chunks.join("") : "") ||
        "(Resposta vazia)";
      chatMessages.push({ role: "assistant", content: full });
    } catch (e) {
      chatMessages.push({ role: "assistant", content: "[Erro] " + e.message });
    }
    renderChat();
  });

  renderChat();

  // --- Personalidade ---
  document.getElementById("btnPersona").addEventListener("click", async () => {
    const out = document.getElementById("outPersona");
    const c = cfg();
    if (!c.api_key) {
      out.textContent = "Informe a BEDROCK_API_KEY.";
      return;
    }
    if (!c.model_id) {
      out.textContent = "Carregue os modelos e selecione um modelo.";
      return;
    }
    const sys =
      document.getElementById("taSystem").value.trim() ||
      "Você é um assistente prestativo. Responda de forma clara e objetiva em português.";
    const usr = document.getElementById("taUserPersona").value.trim();
    if (!usr) {
      out.textContent = "Digite uma mensagem do usuário.";
      return;
    }
    const max_tokens = parseInt(document.getElementById("maxTokPersona").value, 10) || 200;
    const mode = document.querySelector('input[name="personaMode"]:checked').value;
    const messages = [
      { role: "system", content: sys },
      { role: "user", content: usr },
    ];

    out.textContent = mode === "stream" ? "Gerando (chunks)…" : "Gerando…";
    try {
      const data = await apiPost("/api/completion", {
        ...c,
        messages,
        max_tokens,
        stream_collect: mode === "stream",
      });
      let text =
        data.content ||
        (Array.isArray(data.chunks) ? data.chunks.join("") : "") ||
        "";
      if (data.usage && mode !== "stream") {
        text +=
          "\n\n— Tokens: entrada " +
          data.usage.prompt_tokens +
          ", saída " +
          data.usage.completion_tokens +
          ", total " +
          data.usage.total_tokens;
      }
      out.textContent = text || "(vazio)";
    } catch (e) {
      out.textContent = "Erro: " + e.message;
    }
  });
})();
