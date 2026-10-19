(function () {
  "use strict";

  // ---------- ordenação muda e já busca ----------

  const form = document.getElementById("search-form");

  if (form) {
    form.querySelectorAll(".results-controls select, .scope input").forEach((el) => {
      el.addEventListener("change", () => form.requestSubmit());
    });

    // Não envia campos vazios (URL mais limpa)
    form.addEventListener("submit", () => {
      form.querySelectorAll("input, select").forEach((el) => {
        if ((el.type === "text" || el.type === "number" || el.tagName === "SELECT") && !el.value) {
          el.disabled = true;
        }
      });
    });
  }

  // ---------- tabela de sintaxe ----------

  document.querySelectorAll("[data-toggle]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const target = document.getElementById(link.dataset.toggle);
      if (target) target.hidden = !target.hidden;
    });
  });

  // ---------- atalhos de preço ----------

  document.querySelectorAll(".presets").forEach((box) => {
    const min = document.getElementById(box.dataset.targetMin);
    const max = document.getElementById(box.dataset.targetMax);

    box.querySelectorAll("button").forEach((button) => {
      button.addEventListener("click", () => {
        min.value = button.dataset.min || "";
        max.value = button.dataset.max || "";
      });
    });
  });

  // ---------- virar carta de duas faces ----------

  document.querySelectorAll("[data-flip]").forEach((button) => {
    button.addEventListener("click", () => {
      document.getElementById(button.dataset.flip).classList.toggle("flipped");
    });
  });

  // ---------- upload arrastando arquivos ----------

  const dropzone = document.getElementById("dropzone");

  if (dropzone) {
    const input = dropzone.querySelector("input[type=file]");
    const names = document.getElementById("file-names");

    const showNames = () => {
      names.textContent = Array.from(input.files).map((f) => f.name).join(", ");
    };

    input.addEventListener("change", showNames);

    ["dragenter", "dragover"].forEach((name) =>
      dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        dropzone.classList.add("dragover");
      })
    );

    ["dragleave", "drop"].forEach((name) =>
      dropzone.addEventListener(name, () => dropzone.classList.remove("dragover"))
    );

    dropzone.addEventListener("drop", (event) => {
      event.preventDefault();
      input.files = event.dataTransfer.files;
      showNames();
    });
  }

  // ---------- limpar coleção ----------

  const clearForm = document.getElementById("clear-form");

  if (clearForm) {
    const confirmInput = document.getElementById("clear-confirm");
    const clearButton = document.getElementById("clear-button");

    confirmInput.addEventListener("input", () => {
      clearButton.disabled = confirmInput.value.trim() !== "LIMPAR";
    });

    clearForm.addEventListener("submit", (event) => {
      if (!window.confirm("Remover todas as cartas da coleção? Isso não pode ser desfeito.")) {
        event.preventDefault();
      }
    });
  }

  // ---------- autocompletar nome (Scryfall + coleção) ----------

  const datalist = document.getElementById("card-suggestions");

  if (datalist) {
    let timer = null;

    document.querySelectorAll("input[list=card-suggestions]").forEach((input) => {
      input.addEventListener("input", () => {
        clearTimeout(timer);

        timer = setTimeout(async () => {
          if (input.value.length < 2) return;

          try {
            const response = await fetch(`${datalist.dataset.url}?q=${encodeURIComponent(input.value)}`);
            const { data } = await response.json();

            datalist.replaceChildren(
              ...data.map((name) => Object.assign(document.createElement("option"), { value: name }))
            );
          } catch (error) {
            /* sugestão é opcional */
          }
        }, 250);
      });
    });
  }

  // ---------- relatório: radar de proporções (arrastar "Seu alvo") ----------

  const radarData = document.getElementById("radar-data");
  const radarSvg = document.getElementById("radar-chart");

  if (radarData && radarSvg) {
    const data = JSON.parse(radarData.textContent);
    const form = document.getElementById("radar-form");
    const tooltip = document.getElementById("radar-tooltip");
    const totalEl = document.getElementById("radar-total");
    const NS = "http://www.w3.org/2000/svg";

    const axes = data.axes;
    const slots = data.slots;
    const SERIES = [
      { key: "current", label: "Deck atual", color: "var(--series-current)" },
      { key: "profile", label: "Perfil (EDHREC)", color: "var(--series-profile)" },
      { key: "target", label: "Seu alvo", color: "var(--series-target)" },
    ];
    const values = {
      current: { ...data.series.current },
      profile: { ...data.series.profile },
      target: { ...data.series.target },
    };

    // Escala do raio em raiz quadrada: valores pequenos (board wipe, ramp)
    // não ficam espremidos no centro quando a sinergia é grande
    const cx = 260, cy = 190, R = 130;
    const angle = (i) => -Math.PI / 2 + (i * 2 * Math.PI) / axes.length;
    const niceMax = () => {
      const all = SERIES.flatMap((s) => axes.map((a) => Number(values[s.key][a.key]) || 0));
      return Math.max(10, Math.ceil((Math.max(...all) * 1.25) / 5) * 5);
    };
    let max = niceMax();

    const radius = (value) => Math.sqrt(Math.max(Math.min(value, max), 0) / max) * R;
    const valueAt = (r) => Math.pow(Math.max(Math.min(r, R), 0) / R, 2) * max;
    const point = (i, value) => {
      const r = radius(value);
      return [cx + r * Math.cos(angle(i)), cy + r * Math.sin(angle(i))];
    };

    const el = (name, attrs, parent) => {
      const node = document.createElementNS(NS, name);
      Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
      (parent || radarSvg).appendChild(node);
      return node;
    };

    function showTooltip(event, axisLabel, seriesLabel, value) {
      tooltip.hidden = false;
      tooltip.textContent = `${axisLabel} · ${seriesLabel}: ${value}`;
      const box = radarSvg.parentElement.getBoundingClientRect();
      tooltip.style.left = `${event.clientX - box.left + 12}px`;
      tooltip.style.top = `${event.clientY - box.top - 30}px`;
    }
    const hideTooltip = () => { tooltip.hidden = true; };

    function render() {
      radarSvg.replaceChildren();

      // Grade recessiva: 4 anéis igualmente espaçados (rotulados com o valor
      // real, na escala raiz) + raios; números à esquerda do eixo de cima
      for (let ring = 1; ring <= 4; ring++) {
        const ringValue = valueAt((R * ring) / 4);
        const pts = axes.map((_, i) => point(i, ringValue).join(",")).join(" ");
        el("polygon", { points: pts, class: "radar-ring" });
        const [, y] = point(0, ringValue);
        el("text", { x: cx - 6, y: y + 3, "text-anchor": "end", class: "radar-tick" }).textContent = Math.round(ringValue);
      }
      axes.forEach((axis, i) => {
        const [x, y] = point(i, max);
        el("line", { x1: cx, y1: cy, x2: x, y2: y, class: "radar-spoke" });
        const lx = cx + (R + 22) * Math.cos(angle(i));
        const ly = cy + (R + 22) * Math.sin(angle(i));
        const anchor = Math.abs(lx - cx) < 5 ? "middle" : lx > cx ? "start" : "end";
        el("text", { x: lx, y: ly + 4, "text-anchor": anchor, class: "radar-axis-label" }).textContent = axis.label;
      });

      // Séries: atual e perfil embaixo, alvo (editável) por cima
      SERIES.forEach((series) => {
        const pts = axes.map((a, i) => point(i, Number(values[series.key][a.key]) || 0));
        el("polygon", {
          points: pts.map((p) => p.join(",")).join(" "),
          class: `radar-area ${series.key}`,
          style: `stroke: ${series.color}; fill: ${series.color}`,
        });

        pts.forEach(([x, y], i) => {
          const axis = axes[i];
          const value = Number(values[series.key][axis.key]) || 0;
          const editable = series.key === "target";

          const marker = el("circle", {
            cx: x, cy: y, r: editable ? 6 : 4,
            class: `radar-marker ${series.key}`,
            style: `fill: ${series.color}`,
          });

          // Área de toque maior que a marca
          const hit = el("circle", { cx: x, cy: y, r: editable ? 16 : 10, class: `radar-hit${editable ? " draggable" : ""}` });
          hit.addEventListener("pointerenter", (e) => showTooltip(e, axis.label, series.label, value));
          hit.addEventListener("pointermove", (e) => { if (!dragging) showTooltip(e, axis.label, series.label, value); });
          hit.addEventListener("pointerleave", () => { if (!dragging) hideTooltip(); });

          if (editable) {
            hit.addEventListener("pointerdown", (e) => startDrag(e, i));
            marker.setAttribute("aria-hidden", "true");
          }
        });
      });

      syncInputs();
    }

    // ---- arrastar um ponto do alvo ao longo do eixo dele
    let dragging = null;

    function startDrag(event, index) {
      event.preventDefault();
      dragging = { index };
      radarSvg.setPointerCapture(event.pointerId);
    }

    radarSvg.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      const matrix = radarSvg.getScreenCTM().inverse();
      const pt = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix);
      const i = dragging.index;
      const projection = (pt.x - cx) * Math.cos(angle(i)) + (pt.y - cy) * Math.sin(angle(i));
      const value = Math.max(0, Math.min(slots, Math.round(valueAt(projection))));
      const axis = axes[i];
      if (values.target[axis.key] !== value) {
        values.target[axis.key] = value;
        render();
      }
      showTooltip(event, axis.label, "Seu alvo", value);
    });

    const endDrag = () => {
      if (!dragging) return;
      dragging = null;
      hideTooltip();
      const newMax = niceMax();
      if (newMax !== max) { max = newMax; render(); }
    };
    radarSvg.addEventListener("pointerup", endDrag);
    radarSvg.addEventListener("pointercancel", endDrag);

    // ---- tabela: valores atuais/perfil e campos do alvo
    function syncInputs() {
      axes.forEach((axis) => {
        form.querySelector(`[data-current="${axis.key}"]`).textContent = values.current[axis.key] ?? "—";
        form.querySelector(`[data-profile="${axis.key}"]`).textContent = values.profile[axis.key] ?? "—";
        const input = form.querySelector(`input[data-axis="${axis.key}"]`);
        if (document.activeElement !== input) input.value = values.target[axis.key] ?? 0;
      });

      const shared = axes.filter((a) => a.key !== "creatures")
        .reduce((sum, a) => sum + (Number(values.target[a.key]) || 0), 0);
      totalEl.textContent = shared === slots
        ? `Sinergia + papéis: ${shared} de ${slots} vagas.`
        : `Sinergia + papéis: ${shared} de ${slots} vagas — o construtor ajusta proporcionalmente para ${slots}.`;
      totalEl.classList.toggle("warn", shared !== slots);
    }

    form.querySelectorAll("input[data-axis]").forEach((input) => {
      input.addEventListener("input", () => {
        values.target[input.dataset.axis] = Math.max(0, Math.min(slots, Number(input.value) || 0));
        max = niceMax();
        render();
      });
    });

    render();
  }

  // ---------- decks: menus (⋯, Ações, Novo deck) ----------

  const menus = () => document.querySelectorAll("details.deck-menu[open], details.new-deck[open]");

  document.querySelectorAll("details.deck-menu, details.new-deck").forEach((menu) => {
    menu.addEventListener("toggle", () => {
      if (!menu.open) return;
      menus().forEach((other) => { if (other !== menu) other.open = false; });
      menu.querySelector("input[type=text]")?.focus();
    });
  });

  document.addEventListener("click", (event) => {
    menus().forEach((menu) => { if (!menu.contains(event.target)) menu.open = false; });
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") menus().forEach((menu) => { menu.open = false; });
  });

  // ---------- construtor ----------

  // Envio demorado (montar / reconstruir deck): trava o botão e avisa
  document.querySelectorAll("form").forEach((form) => {
    const button = form.querySelector("[data-building]");
    if (!button) return;

    form.addEventListener("submit", (event) => {
      if (event.defaultPrevented) return;
      button.disabled = true;
      button.textContent = "Montando o deck…";
      form.querySelector(".building-hint")?.removeAttribute("hidden");
    });
  });

  const builderForm = document.getElementById("builder-form");

  if (builderForm) {
    // Segundo comandante: escondido até clicar no "+"
    const togglePartner = document.getElementById("toggle-partner");
    const partnerField = document.getElementById("partner-field");
    const partnerInput = partnerField.querySelector("input");

    const setPartnerOpen = (open) => {
      partnerField.hidden = !open;
      togglePartner.textContent = open ? "−" : "+";
      togglePartner.setAttribute("aria-expanded", String(open));
      togglePartner.title = open
        ? "Remover o segundo comandante"
        : "Adicionar um segundo comandante (partner, background…)";
      if (open) {
        partnerInput.focus();
      } else {
        partnerInput.value = "";
        partnerInput.dispatchEvent(new Event("input"));
      }
    };

    togglePartner.addEventListener("click", () => setPartnerOpen(partnerField.hidden));

    const budget = builderForm.querySelector("input[name=budget]");
    const partner = builderForm.querySelector("input[name=partner]");
    const average = document.getElementById("budget-average");
    const moneyFmt = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });

    const showAverage = () => {
      const value = Number(budget.value);
      const slots = partner.value.trim() ? 58 : 59;
      average.textContent = budget.value && value > 0
        ? `≈ ${moneyFmt.format(value / slots)} por carta, em média (${slots} mágicas)`
        : "";
    };

    budget.addEventListener("input", showAverage);
    partner.addEventListener("input", showAverage);
    showAverage();
  }

  // ---------- preços em reais chegando da LigaMagic ----------

  const pricesApi = document.getElementById("prices-api");

  let polls = 0;
  let pollTimer = null;

  // Pode ser chamada de novo quando os resultados são trocados na página
  function startPricePolling() {
    if (!pricesApi) return;
    polls = 0;
    clearTimeout(pollTimer);
    pollTimer = setTimeout(pollPrices, 1500);
  }

  async function pollPrices() {
    const pending = Array.from(document.querySelectorAll(".js-brl"));

    if (!pending.length || polls++ > 150) return;

    const names = [...new Set(pending.map((el) => el.dataset.name))];
    const query = names.map((n) => `n=${encodeURIComponent(n)}`).join("&");

    try {
      const response = await fetch(`${pricesApi.dataset.url}?${query}`);
      const { prices } = await response.json();

      pending.forEach((el) => {
        const info = prices[el.dataset.name];
        if (!info || info.status === "pending") return;

        el.classList.remove("js-brl");

        if (info.status === "ok") {
          el.textContent = info.display;
          el.classList.remove("estimate");
          el.classList.add("arrived");
          el.title = "Menor preço na LigaMagic";
          document.dispatchEvent(new CustomEvent("c10:price", { detail: { name: el.dataset.name, brl: info.brl } }));
        } else {
          el.classList.add("estimate");
          el.title = info.status === "none"
            ? "Sem ofertas na LigaMagic; estimativa pelo dólar"
            : "LigaMagic indisponível; estimativa pelo dólar";
          if (el.textContent.trim() === "…") el.textContent = "—";
        }
      });
    } catch (error) {
      /* tenta de novo no próximo ciclo */
    }

    pollTimer = setTimeout(pollPrices, 2500);
  }

  startPricePolling();

  // ---------- especialista ----------

  const money = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });

  // A barra lateral do pool começa logo abaixo do menu do topo
  const topbar = document.querySelector(".topbar");

  if (topbar && document.querySelector(".pool-sidebar")) {
    const setTopbarHeight = () => {
      document.documentElement.style.setProperty("--topbar-h", `${topbar.offsetHeight}px`);
    };
    setTopbarHeight();
    window.addEventListener("resize", setTopbarHeight);
  }

  // Abas do pool do EDHREC
  document.querySelectorAll("[data-pool-tab]").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll("[data-pool-tab]").forEach((t) => t.classList.toggle("active", t === tab));
      document.querySelectorAll("[data-pool-panel]").forEach((panel) => {
        panel.hidden = panel.dataset.poolPanel !== tab.dataset.poolTab;
      });
    });
  });

  // Total do deck: recalcula quando chegam preços da LigaMagic
  const budgetBox = document.getElementById("budget-box");

  if (budgetBox) {
    const totalEl = document.getElementById("deck-total");
    const estimatedEl = document.getElementById("deck-estimated");
    const bar = document.getElementById("budget-bar");
    const remainingEl = document.getElementById("budget-remaining");
    const budget = budgetBox.dataset.budget ? Number(budgetBox.dataset.budget) : null;

    const budgetTotalEl = document.getElementById("deck-budget-total");

    const recompute = () => {
      let total = 0;
      let budgetTotal = 0;
      let estimated = 0;

      document.querySelectorAll(".deck-card").forEach((card) => {
        const value = Number(card.dataset.unit || 0) * Number(card.dataset.qty || 1);
        total += value;
        // O comandante não conta para o orçamento
        if (!card.dataset.commander) budgetTotal += value;
        if (card.dataset.estimated && Number(card.dataset.unit)) estimated += 1;
      });

      totalEl.textContent = money.format(total);
      if (budgetTotalEl) budgetTotalEl.textContent = money.format(budgetTotal);
      estimatedEl.textContent = estimated
        ? `${estimated} carta${estimated > 1 ? "s" : ""} com preço estimado pelo dólar`
        : "";

      if (budget !== null && bar) {
        const remaining = budget - budgetTotal;
        bar.style.width = `${budget ? Math.min((budgetTotal / budget) * 100, 100) : 100}%`;
        bar.classList.toggle("over", remaining < 0);
        remainingEl.innerHTML = remaining < 0
          ? `<span class="error">${money.format(-remaining)} acima do orçamento</span>`
          : `<span class="muted">Restam</span> ${money.format(remaining)}`;
      }
    };

    document.addEventListener("c10:price", ({ detail }) => {
      document.querySelectorAll(".deck-card").forEach((card) => {
        if (card.dataset.name === detail.name) {
          card.dataset.unit = detail.brl;
          card.dataset.estimated = "";
        }
      });
      recompute();
    });
  }

  // Modal de sugestões
  const modal = document.getElementById("suggest-modal");

  if (modal) {
    const body = document.getElementById("suggest-body");

    // Último preço máximo usado no modal, lembrado entre aberturas
    const MAX_PRICE_KEY = "c10magic:suggest-max-price";
    const savedMaxPrice = () => {
      try { return localStorage.getItem(MAX_PRICE_KEY) || ""; } catch (error) { return ""; }
    };
    const saveMaxPrice = (value) => {
      try {
        if (value === "") localStorage.removeItem(MAX_PRICE_KEY);
        else localStorage.setItem(MAX_PRICE_KEY, value);
      } catch (error) { /* armazenamento indisponível: só não lembra */ }
    };

    let baseUrl = "";
    let controller = null;
    let timer = null;

    async function loadSuggestions(params, { showSpinner = true } = {}) {
      for (const [key, value] of [...params]) {
        if (value === "") params.delete(key);
      }
      const query = params.toString();
      const url = query ? `${baseUrl}?${query}` : baseUrl;

      controller?.abort();
      controller = new AbortController();

      if (showSpinner) {
        body.innerHTML = '<p class="muted"><span class="spinner inline"></span> Procurando cartas similares…</p>';
      } else {
        body.classList.add("loading");
      }

      try {
        const response = await fetch(url, { signal: controller.signal });
        if (!response.ok) throw new Error(response.status);
        body.innerHTML = await response.text();
        bindFilter();
        startPricePolling();
      } catch (error) {
        if (error.name === "AbortError") return;
        body.innerHTML = '<p class="error">Não foi possível carregar as sugestões. Tente de novo.</p>';
      }

      body.classList.remove("loading");
    }

    function bindFilter() {
      const form = body.querySelector("#suggest-filter");
      if (!form) return;

      const input = form.querySelector("input[name=max_price]");

      // Preço e tags vão juntos; só o preço é lembrado para o próximo modal
      const apply = (delay) => {
        clearTimeout(timer);
        timer = setTimeout(() => {
          saveMaxPrice(input.value.trim());
          loadSuggestions(new URLSearchParams(new FormData(form)), { showSpinner: false });
        }, delay);
      };

      input.addEventListener("input", () => apply(500));
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") apply(0);
      });

      form.querySelectorAll("input[name=tags], input[name=types]").forEach((checkbox) => {
        checkbox.addEventListener("change", () => apply(250));
      });

      body.querySelectorAll("[data-max-price]").forEach((button) => {
        button.addEventListener("click", () => {
          input.value = button.dataset.maxPrice;
          apply(0);
        });
      });
    }

    document.getElementById("suggest-close").addEventListener("click", () => modal.close());
    modal.addEventListener("click", (event) => {
      if (event.target === modal) modal.close();
    });

    document.querySelectorAll("[data-suggest]").forEach((button) => {
      button.addEventListener("click", () => {
        baseUrl = button.dataset.suggest;
        modal.showModal();
        // Ao abrir: tags no padrão da carta, preço máximo do último uso
        loadSuggestions(new URLSearchParams({ max_price: savedMaxPrice() }));
      });
    });
  }

  // ---------- cartas similares: atualiza ao mexer nos controles ----------

  const similarForm = document.getElementById("similar-form");

  if (similarForm) {
    const results = document.getElementById("similar-results");
    const range = document.getElementById("price-range");
    const priceInput = similarForm.querySelector("input[name=max_price]");
    let timer = null;
    let controller = null;

    async function refresh() {
      const params = new URLSearchParams(new FormData(similarForm));
      for (const [key, value] of [...params]) {
        if (value === "") params.delete(key);
      }

      const url = `${similarForm.dataset.url}?${params}`;
      history.replaceState(null, "", url);

      controller?.abort();
      controller = new AbortController();
      results.classList.add("loading");

      try {
        const response = await fetch(`${url}&partial=1`, { signal: controller.signal });
        results.innerHTML = await response.text();
        startPricePolling();
      } catch (error) {
        if (error.name !== "AbortError") results.classList.remove("loading");
        return;
      }

      results.classList.remove("loading");
    }

    const schedule = (delay = 450) => {
      clearTimeout(timer);
      timer = setTimeout(refresh, delay);
    };

    similarForm.addEventListener("change", () => schedule(150));
    similarForm.addEventListener("input", (event) => {
      if (event.target.type === "number") schedule();
    });
    similarForm.addEventListener("submit", (event) => {
      event.preventDefault();
      schedule(0);
    });

    // Barra e campo de preço andam juntos
    const syncRange = () => {
      if (priceInput.value === "") {
        range.value = range.max;
        range.classList.add("unlimited");
      } else {
        range.classList.remove("unlimited");
        if (Number(priceInput.value) > Number(range.max)) range.max = Math.ceil(priceInput.value);
        range.value = priceInput.value;
      }
    };

    range.addEventListener("input", () => {
      priceInput.value = Number(range.value).toFixed(2);
      schedule();
    });
    priceInput.addEventListener("input", syncRange);
    syncRange();

    similarForm.querySelectorAll("[data-price], [data-price-factor]").forEach((button) => {
      button.addEventListener("click", () => {
        if (button.dataset.priceFactor) {
          priceInput.value = (Number(button.dataset.base) * Number(button.dataset.priceFactor)).toFixed(2);
        } else {
          priceInput.value = button.dataset.price;
        }
        syncRange();
        schedule(0);
      });
    });
  }

  // ---------- progresso da atualização ----------

  const pill = document.getElementById("updater-pill");
  const pillText = document.getElementById("updater-pill-text");
  const panel = document.getElementById("updater-panel");
  const bar = document.getElementById("progress-bar");
  const panelText = document.getElementById("updater-text");

  let wasRunning = false;

  async function poll() {
    try {
      const response = await fetch(pill.dataset.url);
      const status = await response.json();

      const percent = status.total ? Math.round((status.done / status.total) * 100) : 0;

      if (status.running) {
        pill.hidden = false;
        pillText.textContent = `Atualizando ${status.done}/${status.total}`;
        pill.title = status.current;

        if (bar) bar.style.width = `${percent}%`;
        if (panelText) {
          panelText.textContent = `Atualizando ${status.done} de ${status.total}… ${status.current}`;
        }

        wasRunning = true;
        setTimeout(poll, 2000);
      } else {
        pill.hidden = true;

        if (bar && status.total) bar.style.width = "100%";

        // Terminou enquanto a página estava aberta: recarrega para mostrar os dados
        if (wasRunning && panel) {
          window.location.reload();
        }
      }
    } catch (error) {
      setTimeout(poll, 5000);
    }
  }

  if (pill) poll();
})();
