(() => {
  const state = {
    symbol: "LONDON_GOLD",
    view: "product",
    quotes: [],
    history: null,
    prediction: null,
    quoteTimer: null,
    quoteRefreshing: false,
    quoteIntervalMs: 3000,
  };

  const els = {
    quoteCards: document.getElementById("quoteCards"),
    heroTitle: document.getElementById("heroTitle"),
    heroLead: document.getElementById("heroLead"),
    chartTitle: document.getElementById("chartTitle"),
    signalChips: document.getElementById("signalChips"),
    signalNotes: document.getElementById("signalNotes"),
    predictSummary: document.getElementById("predictSummary"),
    predictMeta: document.getElementById("predictMeta"),
    horizon: document.getElementById("horizon"),
    chartPanel: document.getElementById("chartPanel"),
    comparePanel: document.getElementById("comparePanel"),
    topDisclaimer: document.getElementById("topDisclaimer"),
    liveStatus: document.getElementById("liveStatus"),
    liveText: document.getElementById("liveText"),
    forecastMeta: document.getElementById("forecastMeta"),
    forecastStart: document.getElementById("forecastStart"),
    forecastEnd: document.getElementById("forecastEnd"),
    forecastBody: document.getElementById("forecastBody"),
    corrMeta: document.getElementById("corrMeta"),
  };

  const priceChart = echarts.init(document.getElementById("priceChart"));
  const predictChart = echarts.init(document.getElementById("predictChart"));
  const compareChart = echarts.init(document.getElementById("compareChart"));

  const productMeta = {
    LONDON_GOLD: {
      title: "伦敦金",
      lead: "跟踪国际金价基准，用技术面与统计模型观察美元计价黄金的中短周期走势。",
    },
    ZHESHANG_GOLD: {
      title: "浙商积存金",
      lead: "面向人民币投资者的银行积存金报价洞察，结合校准后的历史序列做趋势预测。",
    },
  };

  async function api(path) {
    const res = await fetch(path);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    return res.json();
  }

  function fmt(n, digits = 2) {
    if (n == null || Number.isNaN(n)) return "--";
    return Number(n).toLocaleString("zh-CN", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function clsChange(v) {
    if (v == null) return "";
    return v >= 0 ? "up" : "down";
  }

  function setLiveStatus(mode, text) {
    if (!els.liveStatus || !els.liveText) return;
    els.liveStatus.classList.remove("is-error", "is-paused");
    if (mode === "error") els.liveStatus.classList.add("is-error");
    if (mode === "paused") els.liveStatus.classList.add("is-paused");
    els.liveText.textContent = text;
  }

  function renderQuotes(items, { silent = false } = {}) {
    const prevMap = Object.fromEntries((state.quotes || []).map((q) => [q.symbol, q.price]));
    state.quotes = items || [];
    els.quoteCards.innerHTML = state.quotes
      .map((q, i) => {
        const pct = q.change_pct;
        const sign = pct != null && pct >= 0 ? "+" : "";
        const prev = prevMap[q.symbol];
        let flash = "";
        if (prev != null && q.price != null && Number(prev) !== Number(q.price)) {
          flash = Number(q.price) > Number(prev) ? "flash-up" : "flash-down";
        }
        return `
          <article class="quote-card" style="animation-delay:${silent ? 0 : i * 0.08}s">
            <div class="name">${q.name}<span class="live-tag">LIVE</span></div>
            <div class="price ${flash}">${fmt(q.price)} <small style="font-size:0.9rem;color:#6b7785">${q.unit || ""}</small></div>
            <div class="meta">
              <span class="${clsChange(pct)}">${sign}${fmt(pct, 2)}%</span>
              <span>${new Date(q.ts).toLocaleTimeString("zh-CN")}</span>
            </div>
          </article>`;
      })
      .join("");
  }

  async function loadQuotes({ silent = false } = {}) {
    if (state.quoteRefreshing) return;
    state.quoteRefreshing = true;
    try {
      const data = await api("/api/quotes?refresh=true");
      renderQuotes(data.items, { silent });
      const now = new Date().toLocaleTimeString("zh-CN");
      setLiveStatus("live", `实时 · ${now}`);
    } catch (e) {
      setLiveStatus("error", "刷新失败");
      if (!silent) throw e;
    } finally {
      state.quoteRefreshing = false;
    }
  }

  function startQuotePolling() {
    stopQuotePolling();
    state.quoteTimer = setInterval(() => {
      if (document.hidden) return;
      loadQuotes({ silent: true }).catch(() => {});
    }, state.quoteIntervalMs);
    setLiveStatus("live", "实时刷新中");
  }

  function stopQuotePolling() {
    if (state.quoteTimer) {
      clearInterval(state.quoteTimer);
      state.quoteTimer = null;
    }
  }

  function biasLabel(bias) {
    if (bias === "bullish") return "偏多";
    if (bias === "bearish") return "偏空";
    if (bias === "neutral") return "中性";
    return "数据不足";
  }

  function renderHistory(data) {
    state.history = data;
    const meta = productMeta[state.symbol];
    els.heroTitle.textContent = meta.title;
    els.heroLead.textContent = meta.lead;
    els.chartTitle.textContent = `${data.name} · 走势与技术面`;

    const signal = data.signal || {};
    els.signalChips.innerHTML = `
      <span class="chip ${signal.bias || "neutral"}">倾向：${biasLabel(signal.bias)}</span>
      <span class="chip">评分 ${signal.score ?? "--"}</span>
      <span class="chip">RSI ${fmt(signal.rsi14, 1)}</span>
    `;
    els.signalNotes.innerHTML = (signal.notes || []).map((n) => `<li>${n}</li>`).join("");

    const dates = data.items.map((d) => d.date);
    const closes = data.items.map((d) => d.close);
    const ma5 = data.items.map((d) => d.ma5);
    const ma20 = data.items.map((d) => d.ma20);
    const bollU = data.items.map((d) => d.boll_upper);
    const bollL = data.items.map((d) => d.boll_lower);

    priceChart.setOption({
      animationDuration: 700,
      tooltip: { trigger: "axis" },
      legend: { data: ["收盘", "MA5", "MA20", "布林上轨", "布林下轨"], top: 0 },
      grid: { left: 48, right: 24, top: 40, bottom: 36 },
      xAxis: { type: "category", data: dates, boundaryGap: false },
      yAxis: { type: "value", scale: true, splitLine: { lineStyle: { color: "#e6ebf0" } } },
      series: [
        {
          name: "收盘",
          type: "line",
          data: closes,
          showSymbol: false,
          lineStyle: { width: 2.4, color: "#132033" },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: "rgba(176,141,62,0.28)" },
              { offset: 1, color: "rgba(176,141,62,0.02)" },
            ]),
          },
        },
        { name: "MA5", type: "line", data: ma5, showSymbol: false, lineStyle: { width: 1.4, color: "#b08d3e" } },
        { name: "MA20", type: "line", data: ma20, showSymbol: false, lineStyle: { width: 1.4, color: "#3d6f99" } },
        { name: "布林上轨", type: "line", data: bollU, showSymbol: false, lineStyle: { width: 1, type: "dashed", color: "#9aa7b5" } },
        { name: "布林下轨", type: "line", data: bollL, showSymbol: false, lineStyle: { width: 1, type: "dashed", color: "#9aa7b5" } },
      ],
    });
  }

  function renderPrediction(data) {
    state.prediction = data;
    const change = data.change_pct;
    els.predictMeta.textContent = `${data.model} · 置信 ${fmt(data.confidence * 100, 1)}%`;
    els.predictSummary.classList.remove("empty");
    els.predictSummary.innerHTML = `
      <div class="stat"><div class="label">当前价</div><div class="value">${fmt(data.current_price)}</div></div>
      <div class="stat"><div class="label">${data.horizon_days} 日后预测</div><div class="value">${fmt(data.predicted_price)}</div></div>
      <div class="stat"><div class="label">预期涨跌</div><div class="value ${clsChange(change)}">${change >= 0 ? "+" : ""}${fmt(change, 2)}%</div></div>
    `;
    els.topDisclaimer.textContent = data.disclaimer;

    const hist = (state.history?.items || []).slice(-60);
    const histDates = hist.map((d) => d.date);
    const histClose = hist.map((d) => d.close);
    const predDates = data.points.map((p) => p.date);
    const pred = data.points.map((p) => p.predicted);
    const lower = data.points.map((p) => p.lower);
    const upper = data.points.map((p) => p.upper);

    predictChart.setOption({
      animationDuration: 650,
      tooltip: { trigger: "axis" },
      legend: { data: ["历史", "预测", "上限", "下限"] },
      grid: { left: 48, right: 20, top: 36, bottom: 28 },
      xAxis: { type: "category", data: [...histDates, ...predDates] },
      yAxis: { type: "value", scale: true, splitLine: { lineStyle: { color: "#e6ebf0" } } },
      series: [
        {
          name: "历史",
          type: "line",
          data: [...histClose, ...predDates.map(() => null)],
          showSymbol: false,
          lineStyle: { color: "#132033", width: 2 },
        },
        {
          name: "预测",
          type: "line",
          data: [...histClose.map(() => null).slice(0, -1), histClose.at(-1), ...pred],
          showSymbol: false,
          lineStyle: { color: "#b08d3e", width: 2.2, type: "solid" },
        },
        {
          name: "上限",
          type: "line",
          data: [...histDates.map(() => null), ...upper],
          showSymbol: false,
          lineStyle: { color: "#9aa7b5", type: "dashed", width: 1 },
        },
        {
          name: "下限",
          type: "line",
          data: [...histDates.map(() => null), ...lower],
          showSymbol: false,
          lineStyle: { color: "#9aa7b5", type: "dashed", width: 1 },
        },
      ],
    });

    // 生成预测后刷新归档表
    loadForecastHistory({ silent: true }).catch(() => {});
  }

  function defaultForecastRange() {
    const end = new Date();
    const start = new Date();
    start.setDate(end.getDate() - 7);
    const end2 = new Date();
    end2.setDate(end.getDate() + 14);
    const toInput = (d) => d.toISOString().slice(0, 10);
    if (els.forecastStart && !els.forecastStart.value) els.forecastStart.value = toInput(start);
    if (els.forecastEnd && !els.forecastEnd.value) els.forecastEnd.value = toInput(end2);
  }

  function renderForecastHistory(data) {
    const hit =
      data.hit_rate == null ? "--" : `${(data.hit_rate * 100).toFixed(1)}%`;
    els.forecastMeta.textContent = `共 ${data.count || 0} 条 · 收盘落带率 ${hit}`;
    const rows = data.items || [];
    if (!rows.length) {
      els.forecastBody.innerHTML =
        `<tr><td colspan="8" class="muted">该区间暂无预测归档</td></tr>`;
      return;
    }
    els.forecastBody.innerHTML = rows
      .map((r) => {
        const band =
          r.close_in_band == null
            ? `<span class="badge-na">—</span>`
            : r.close_in_band
              ? `<span class="badge-yes">是</span>`
              : `<span class="badge-no">否</span>`;
        const err =
          r.error == null
            ? "—"
            : `<span class="${clsChange(r.error)}">${r.error >= 0 ? "+" : ""}${fmt(r.error)}</span>`;
        return `<tr>
          <td>${r.target_date}</td>
          <td>${fmt(r.high)}</td>
          <td>${fmt(r.low)}</td>
          <td>${fmt(r.predicted)}</td>
          <td>${r.actual_close == null ? "—" : fmt(r.actual_close)}</td>
          <td>${err}</td>
          <td>${band}</td>
          <td>${r.made_on}</td>
        </tr>`;
      })
      .join("");
  }

  async function loadForecastHistory({ silent = false } = {}) {
    defaultForecastRange();
    const start = els.forecastStart?.value;
    const end = els.forecastEnd?.value;
    const qs = new URLSearchParams();
    if (start) qs.set("start", start);
    if (end) qs.set("end", end);
    try {
      const data = await api(`/api/forecasts/${state.symbol}?${qs.toString()}`);
      renderForecastHistory(data);
    } catch (e) {
      if (!silent) throw e;
      els.forecastBody.innerHTML =
        `<tr><td colspan="8" class="muted">加载失败：${e.message}</td></tr>`;
    }
  }

  async function loadHistory() {
    const data = await api(`/api/history/${state.symbol}?days=180`);
    renderHistory(data);
  }

  async function loadPrediction() {
    const horizon = els.horizon.value;
    els.predictSummary.classList.add("empty");
    els.predictSummary.textContent = "模型计算中…";
    const data = await api(`/api/predict/${state.symbol}?horizon=${horizon}`);
    renderPrediction(data);
  }

  async function loadCompare() {
    const data = await api("/api/compare?days=90");
    if (els.corrMeta) {
      els.corrMeta.textContent = `相对走势（首日=100） · 相关性 ${data.correlation ?? "--"}`;
    }    const london = data.series.LONDON_GOLD || [];
    const zs = data.series.ZHESHANG_GOLD || [];
    compareChart.setOption({
      animationDuration: 700,
      tooltip: { trigger: "axis" },
      legend: { data: ["伦敦金", "浙商积存金"] },
      grid: { left: 48, right: 24, top: 40, bottom: 36 },
      xAxis: {
        type: "category",
        data: london.map((d) => d.date),
        boundaryGap: false,
      },
      yAxis: { type: "value", scale: true, splitLine: { lineStyle: { color: "#e6ebf0" } } },
      series: [
        {
          name: "伦敦金",
          type: "line",
          showSymbol: false,
          data: london.map((d) => d.indexed),
          lineStyle: { width: 2.2, color: "#132033" },
        },
        {
          name: "浙商积存金",
          type: "line",
          showSymbol: false,
          data: zs.map((d) => d.indexed),
          lineStyle: { width: 2.2, color: "#b08d3e" },
        },
      ],
    });
    compareChart.resize();
  }

  function setView(view, symbol) {
    state.view = view;
    if (symbol) state.symbol = symbol;
    document.querySelectorAll(".nav-btn").forEach((btn) => {
      const active =
        (view === "compare" && btn.dataset.view === "compare") ||
        (view === "product" && btn.dataset.symbol === state.symbol);
      btn.classList.toggle("active", !!active);
    });

    if (view === "compare") {
      els.chartPanel.classList.add("hidden");
      document.querySelector(".grid-2").classList.add("hidden");
      els.comparePanel.classList.remove("hidden");
      els.heroTitle.textContent = "Gold Insight";
      els.heroLead.textContent = "把伦敦金与浙商积存金放在同一相对尺度上，观察联动与背离。";
      loadCompare().catch((e) => alert(e.message));
    } else {
      els.comparePanel.classList.add("hidden");
      els.chartPanel.classList.remove("hidden");
      document.querySelector(".grid-2").classList.remove("hidden");
      Promise.all([loadHistory(), loadPrediction(), loadForecastHistory({ silent: true })]).catch((e) =>
        alert(e.message)
      );
    }
  }

  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.view === "compare") setView("compare");
      else setView("product", btn.dataset.symbol);
    });
  });
  document.getElementById("btnRefresh").addEventListener("click", () => {
    loadQuotes({ silent: true }).catch((e) => alert(e.message));
  });
  document.getElementById("btnPredict").addEventListener("click", () => {
    loadPrediction().catch((e) => alert(e.message));
  });
  document.getElementById("btnForecastQuery").addEventListener("click", () => {
    loadForecastHistory().catch((e) => alert(e.message));
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      setLiveStatus("paused", "已暂停");
    } else {
      loadQuotes({ silent: true }).catch(() => {});
      setLiveStatus("live", "实时刷新中");
    }
  });

  window.addEventListener("resize", () => {
    priceChart.resize();
    predictChart.resize();
    compareChart.resize();
  });

  (async function boot() {
    defaultForecastRange();
    try {
      await loadQuotes();
      startQuotePolling();
      await loadHistory();
      await loadPrediction();
      await loadForecastHistory({ silent: true });
    } catch (e) {
      setLiveStatus("error", "连接失败");
      els.quoteCards.innerHTML = `<div class="quote-card">加载失败：${e.message}<br/>可稍后刷新，或检查网络与数据源。</div>`;
      startQuotePolling();
    }
  })();
})();
