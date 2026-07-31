(() => {
  const state = {
    symbol: "LONDON_GOLD",
    view: "product",
    quotes: [],
    history: null,
    londonHistory: null,
    zheshangHistory: null,
    londonPrediction: null,
    zheshangPrediction: null,
    prediction: null,
    entryExit: null,
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

  /** 预测历史 / 回测固定用积存金，不跟上方品种 Tab 切换 */
  const FORECAST_HISTORY_SYMBOL = "ZHESHANG_GOLD";

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

  async function api(path, options = {}) {
    const res = await fetch(path, options);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      const detail = err.detail;
      throw new Error(
        typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : res.statusText
      );
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

  function renderEntryExit(data) {
    const board = document.getElementById("entryExitCard");
    if (!board) return;

    if (!data || data.entry == null || data.exit == null) {
      board.innerHTML = `
        <div class="quote-board-head">
          <span>浙商积存金交易点</span>
        </div>
        <div class="muted">生成预测后显示上车/下车点</div>
      `;
      return;
    }

    board.innerHTML = `
      <div class="quote-board-head">
        <span>浙商积存金交易点</span>
      </div>
      <div class="quote-rows">
        <div class="quote-row">
          <div class="name">上车点</div>
          <div class="price entry-price">${fmt(data.entry)} <small style="font-size:0.85rem;color:#6b7785">元/克</small></div>
          <div class="meta">
            <span class="muted">预测最低</span>
            <span class="muted">中枢 ${fmt(data.mid)}</span>
          </div>
        </div>
        <div class="quote-row">
          <div class="name">下车点</div>
          <div class="price exit-price">${fmt(data.exit)} <small style="font-size:0.85rem;color:#6b7785">元/克</small></div>
          <div class="meta">
            <span class="muted">预测最高</span>
            <span class="muted">现价 ${data.live == null ? "--" : fmt(data.live)}</span>
          </div>
        </div>
      </div>
    `;
    // 现价仅作对照展示；每次行情刷新时只更新现价文案，不改交易点
    const liveEl = board.querySelector(".quote-row:last-child .meta span:last-child");
    if (liveEl) liveEl.dataset.role = "live-price";
  }

  function patchEntryExitLivePrice() {
    const board = document.getElementById("entryExitCard");
    if (!board || !state.entryExit) return;
    const zs = (state.quotes || []).find((q) => q.symbol === "ZHESHANG_GOLD");
    if (!zs) return;
    state.entryExit.live = zs.price;
    const liveEl = board.querySelector('[data-role="live-price"]');
    if (liveEl) liveEl.textContent = `现价 ${fmt(zs.price)}`;
  }

  async function loadEntryExit({ silent = false } = {}) {
    try {
      const data = await api("/api/entry-exit");
      state.entryExit = data;
      renderEntryExit(data);
    } catch (e) {
      if (!silent) {
        renderEntryExit(null);
      }
    }
  }

  function renderQuotes(items, { silent = false } = {}) {
    const prevMap = Object.fromEntries((state.quotes || []).map((q) => [q.symbol, q.price]));
    state.quotes = items || [];
    const rows = state.quotes
      .map((q) => {
        const pct = q.change_pct;
        const sign = pct != null && pct >= 0 ? "+" : "";
        const prev = prevMap[q.symbol];
        let flash = "";
        if (prev != null && q.price != null && Number(prev) !== Number(q.price)) {
          flash = Number(q.price) > Number(prev) ? "flash-up" : "flash-down";
        }
        return `
          <div class="quote-row">
            <div class="name">${q.name}</div>
            <div class="price ${flash}">${fmt(q.price)} <small style="font-size:0.85rem;color:#6b7785">${q.unit || ""}</small></div>
            <div class="meta">
              <span class="${clsChange(pct)}">${sign}${fmt(pct, 2)}%</span>
              <span>${new Date(q.ts).toLocaleTimeString("zh-CN")}</span>
            </div>
          </div>`;
      })
      .join("");

    const latestTs = state.quotes[0]?.ts
      ? new Date(state.quotes[0].ts).toLocaleTimeString("zh-CN")
      : "--";

    els.quoteCards.innerHTML = `
      <article class="quote-board" style="animation-delay:${silent ? 0 : 0.05}s">
        <div class="quote-board-head">
          <span>实时行情</span>
          <span class="live-tag">LIVE · ${latestTs}</span>
        </div>
        <div class="quote-rows">
          ${rows || `<div class="muted">暂无行情</div>`}
        </div>
      </article>
      <article class="quote-board" id="entryExitCard" style="animation-delay:${silent ? 0 : 0.12}s">
        <div class="quote-board-head">
          <span>浙商积存金交易点</span>
        </div>
        <div class="muted">生成预测后显示上车/下车点</div>
      </article>
    `;
    // 上车/下车点只展示缓存的预测结果，不随实时行情重算
    if (state.entryExit) {
      renderEntryExit(state.entryExit);
      patchEntryExitLivePrice();
    }
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

    const londonHist = ((state.londonHistory && state.londonHistory.items) || []).slice(-60);
    const zsHist = ((state.zheshangHistory && state.zheshangHistory.items) || []).slice(-60);
    const baseDates = (londonHist.length ? londonHist : zsHist).map((d) => d.date);

    const londonPred = state.londonPrediction || (state.symbol === "LONDON_GOLD" ? data : null);
    const zsPred = state.zheshangPrediction || (state.symbol === "ZHESHANG_GOLD" ? data : null);
    const mainPred = state.symbol === "ZHESHANG_GOLD" ? zsPred || data : londonPred || data;
    const predDates = (mainPred?.points || data.points || []).map((p) => p.date);
    const allDates = [...baseDates, ...predDates];

    const londonMap = Object.fromEntries(londonHist.map((d) => [d.date, d.close]));
    const zsMap = Object.fromEntries(zsHist.map((d) => [d.date, d.close]));
    const londonPredPts = Object.fromEntries((londonPred?.points || []).map((p) => [p.date, p.predicted]));
    const zsPredPts = Object.fromEntries((zsPred?.points || []).map((p) => [p.date, p.predicted]));
    const londonUpper = Object.fromEntries((londonPred?.points || []).map((p) => [p.date, p.upper]));
    const londonLower = Object.fromEntries((londonPred?.points || []).map((p) => [p.date, p.lower]));
    const zsUpper = Object.fromEntries((zsPred?.points || []).map((p) => [p.date, p.upper]));
    const zsLower = Object.fromEntries((zsPred?.points || []).map((p) => [p.date, p.lower]));

    // 主曲线：历史收盘 + 预测价连贯
    const londonSeries = allDates.map((d) =>
      londonMap[d] != null ? londonMap[d] : londonPredPts[d] != null ? londonPredPts[d] : null
    );
    const zsSeries = allDates.map((d) =>
      zsMap[d] != null ? zsMap[d] : zsPredPts[d] != null ? zsPredPts[d] : null
    );
    const londonUpperSeries = allDates.map((d) => (londonUpper[d] != null ? londonUpper[d] : null));
    const londonLowerSeries = allDates.map((d) => (londonLower[d] != null ? londonLower[d] : null));
    const zsUpperSeries = allDates.map((d) => (zsUpper[d] != null ? zsUpper[d] : null));
    const zsLowerSeries = allDates.map((d) => (zsLower[d] != null ? zsLower[d] : null));

    predictChart.setOption(
      {
        animationDuration: 650,
        color: ["#132033", "#c45c26", "#5b7c99", "#5b7c99", "#d4a574", "#d4a574"],
        tooltip: { trigger: "axis" },
        legend: {
          type: "plain",
          orient: "horizontal",
          bottom: 4,
          left: "center",
          width: "92%",
          itemWidth: 14,
          itemHeight: 8,
          itemGap: 14,
          textStyle: { fontSize: 11, color: "#3a4a5c" },
          data: ["伦敦金", "浙商积存金", "伦敦金上限", "伦敦金下限", "积存金上限", "积存金下限"],
          selectedMode: true,
        },
        grid: { left: 52, right: 52, top: 12, bottom: 64 },
        xAxis: {
          type: "category",
          data: allDates,
          axisLabel: { hideOverlap: true, fontSize: 10 },
        },
        yAxis: [
          {
            type: "value",
            scale: true,
            splitLine: { lineStyle: { color: "#e6ebf0" } },
            axisLabel: { color: "#132033", fontSize: 10 },
          },
          {
            type: "value",
            scale: true,
            position: "right",
            splitLine: { show: false },
            axisLabel: { color: "#c45c26", fontSize: 10 },
          },
        ],
        series: [
          {
            name: "伦敦金",
            type: "line",
            yAxisIndex: 0,
            data: londonSeries,
            showSymbol: false,
            connectNulls: true,
            lineStyle: { color: "#132033", width: 2.2 },
            itemStyle: { color: "#132033" },
          },
          {
            name: "浙商积存金",
            type: "line",
            yAxisIndex: 1,
            data: zsSeries,
            showSymbol: false,
            connectNulls: true,
            lineStyle: { color: "#c45c26", width: 2 },
            itemStyle: { color: "#c45c26" },
          },
          {
            name: "伦敦金上限",
            type: "line",
            yAxisIndex: 0,
            data: londonUpperSeries,
            showSymbol: false,
            lineStyle: { color: "#5b7c99", type: "dashed", width: 1.2 },
            itemStyle: { color: "#5b7c99" },
          },
          {
            name: "伦敦金下限",
            type: "line",
            yAxisIndex: 0,
            data: londonLowerSeries,
            showSymbol: false,
            lineStyle: { color: "#5b7c99", type: "dashed", width: 1.2 },
            itemStyle: { color: "#5b7c99" },
          },
          {
            name: "积存金上限",
            type: "line",
            yAxisIndex: 1,
            data: zsUpperSeries,
            showSymbol: false,
            lineStyle: { color: "#d4a574", type: "dashed", width: 1.2 },
            itemStyle: { color: "#d4a574" },
          },
          {
            name: "积存金下限",
            type: "line",
            yAxisIndex: 1,
            data: zsLowerSeries,
            showSymbol: false,
            lineStyle: { color: "#d4a574", type: "dashed", width: 1.2 },
            itemStyle: { color: "#d4a574" },
          },
        ],
      },
      true
    );
    predictChart.resize();

    loadForecastHistory({ silent: true }).catch(() => {});
  }

  async function ensurePeerHistories() {
    const tasks = [];
    if (!state.londonHistory?.items?.length) {
      tasks.push(
        api("/api/history/LONDON_GOLD?days=180&with_indicators=false").then((d) => {
          state.londonHistory = d;
        })
      );
    }
    if (!state.zheshangHistory?.items?.length) {
      tasks.push(
        api("/api/history/ZHESHANG_GOLD?days=180&with_indicators=false").then((d) => {
          state.zheshangHistory = d;
        })
      );
    }
    if (tasks.length) await Promise.all(tasks);
  }

  function defaultForecastRange() {
    // 默认看本月目标日，方便回测准确率；未到月末时 end 用今天
    const now = new Date();
    const start = new Date(now.getFullYear(), now.getMonth(), 1);
    const end = new Date(now.getFullYear(), now.getMonth() + 1, 0);
    if (end > now) end.setTime(now.getTime());
    const toInput = (d) => {
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, "0");
      const day = String(d.getDate()).padStart(2, "0");
      return `${y}-${m}-${day}`;
    };
    if (els.forecastStart && !els.forecastStart.value) els.forecastStart.value = toInput(start);
    if (els.forecastEnd && !els.forecastEnd.value) els.forecastEnd.value = toInput(end);
  }

  function renderForecastHistory(data) {
    const scored = Number(data.scored_count ?? 0);
    const hits = Number(data.hit_count ?? 0);
    const rate =
      data.accuracy_rate == null && data.hit_rate == null
        ? null
        : (data.accuracy_rate ?? data.hit_rate);
    const rateText = rate == null ? "--" : `${(rate * 100).toFixed(1)}%`;
    els.forecastMeta.textContent =
      rate == null
        ? `积存金 · 共 ${data.count || 0} 条 · 准确率 --（尚无已收盘目标日）`
        : `积存金 · 共 ${data.count || 0} 条 · 准确率 ${rateText}（${hits}/${scored}）`;
    const rows = data.items || [];
    if (!rows.length) {
      els.forecastBody.innerHTML =
        `<tr><td colspan="8" class="muted">该区间暂无预测归档</td></tr>`;
      return;
    }
    els.forecastBody.innerHTML = rows
      .map((r) => {
        const accurate =
          r.accurate == null && r.close_in_band == null
            ? null
            : (r.accurate ?? r.close_in_band);
        const band =
          accurate == null
            ? `<span class="badge-na">—</span>`
            : accurate
              ? `<span class="badge-yes">准确</span>`
              : `<span class="badge-no">偏差</span>`;
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
      const data = await api(`/api/forecasts/${FORECAST_HISTORY_SYMBOL}?${qs.toString()}`);
      renderForecastHistory(data);
    } catch (e) {
      if (!silent) throw e;
      els.forecastBody.innerHTML =
        `<tr><td colspan="8" class="muted">加载失败：${e.message}</td></tr>`;
    }
  }

  async function loadHistory() {
    const data = await api(`/api/history/${state.symbol}?days=180`);
    state.history = data;
    if (state.symbol === "LONDON_GOLD") state.londonHistory = data;
    if (state.symbol === "ZHESHANG_GOLD") state.zheshangHistory = data;
    renderHistory(data);
    try {
      await ensurePeerHistories();
    } catch (_) {
      /* ignore */
    }
  }

  async function loadPrediction({ persist = false } = {}) {
    const horizon = els.horizon.value;
    els.predictSummary.classList.add("empty");
    els.predictSummary.textContent = "模型计算中…";
    try {
      await ensurePeerHistories();
    } catch (_) {
      /* ignore */
    }
    const persistFlag = persist ? "true" : "false";
    const [londonPred, zsPred] = await Promise.all([
      api(`/api/predict/LONDON_GOLD?horizon=${horizon}&persist=${persistFlag}`),
      api(`/api/predict/ZHESHANG_GOLD?horizon=${horizon}&persist=${persistFlag}`),
    ]);
    state.londonPrediction = londonPred;
    state.zheshangPrediction = zsPred;
    const main = state.symbol === "ZHESHANG_GOLD" ? zsPred : londonPred;
    renderPrediction(main);
    // 只有落库重算后才刷新交易点；否则只读已缓存/库里的交易点
    if (persist) {
      await loadEntryExit({ silent: true });
    } else if (!state.entryExit) {
      await loadEntryExit({ silent: true });
    } else {
      renderEntryExit(state.entryExit);
    }
  }

  async function loadCompare() {
    const data = await api("/api/compare?days=90");
    if (els.corrMeta) {
      els.corrMeta.textContent = `真实价格双轴 · 日收益相关性 ${data.correlation ?? "--"}`;
    }
    const london = data.series.LONDON_GOLD || [];
    const zs = data.series.ZHESHANG_GOLD || [];
    const dates = london.map((d) => d.date);
    compareChart.setOption(
      {
        animationDuration: 700,
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "cross" },
        },
        legend: { data: ["伦敦金", "浙商积存金"] },
        grid: { left: 56, right: 56, top: 40, bottom: 36 },
        xAxis: {
          type: "category",
          data: dates,
          boundaryGap: false,
        },
        yAxis: [
          {
            type: "value",
            name: "伦敦金(美元/盎司)",
            scale: true,
            position: "left",
            splitLine: { lineStyle: { color: "#e6ebf0" } },
            axisLabel: { color: "#132033" },
            nameTextStyle: { color: "#132033", fontSize: 11 },
          },
          {
            type: "value",
            name: "积存金(元/克)",
            scale: true,
            position: "right",
            splitLine: { show: false },
            axisLabel: { color: "#b08d3e" },
            nameTextStyle: { color: "#b08d3e", fontSize: 11 },
          },
        ],
        series: [
          {
            name: "伦敦金",
            type: "line",
            yAxisIndex: 0,
            showSymbol: false,
            data: london.map((d) => d.close),
            lineStyle: { width: 2.2, color: "#132033" },
            itemStyle: { color: "#132033" },
          },
          {
            name: "浙商积存金",
            type: "line",
            yAxisIndex: 1,
            showSymbol: false,
            data: zs.map((d) => d.close),
            lineStyle: { width: 2.2, color: "#b08d3e" },
            itemStyle: { color: "#b08d3e" },
          },
        ],
      },
      true
    );
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
      els.heroLead.textContent = "直接对照伦敦金与浙商积存金的真实价格走势（左右双轴，单位不同）。";
      loadCompare().catch((e) => alert(e.message));
    } else {
      els.comparePanel.classList.add("hidden");
      els.chartPanel.classList.remove("hidden");
      document.querySelector(".grid-2").classList.remove("hidden");
      Promise.all([loadHistory(), loadPrediction({ persist: false }), loadForecastHistory({ silent: true })]).catch((e) =>
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
    loadPrediction({ persist: true }).catch((e) => alert(e.message));
  });
  document.getElementById("btnForecastQuery").addEventListener("click", () => {
    loadForecastHistory().catch((e) => alert(e.message));
  });
  document.getElementById("btnBackfillRange").addEventListener("click", async () => {
    const btn = document.getElementById("btnBackfillRange");
    const start = els.forecastStart?.value;
    const end = els.forecastEnd?.value;
    if (!start || !end) {
      alert("请先选择目标日起止日期");
      return;
    }
    if (start > end) {
      alert("开始日期不能晚于结束日期");
      return;
    }
    btn.disabled = true;
    btn.textContent = "回测中…";
    try {
      const data = await api(
        `/api/forecasts/${FORECAST_HISTORY_SYMBOL}/backfill?start=${start}&end=${end}&horizon=7`,
        { method: "POST" }
      );
      const rate = data.accuracy_rate ?? data.hit_rate;
      const hit = rate == null ? "--" : `${(rate * 100).toFixed(1)}%`;
      const scored = data.preview_scored ?? data.scored_count;
      const hits = data.preview_hits ?? data.hit_count;
      const detail =
        scored != null && hits != null ? `（${hits}/${scored}）` : "";
      alert(
        `积存金回测完成：回放 ${data.runs} 个交易日，写入 ${data.saved_points} 条点位。\n区间准确率：${hit}${detail}`
      );
      await loadForecastHistory();
    } catch (e) {
      alert(e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "按区间回测重算";
    }
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
      await loadEntryExit({ silent: true });
      await loadPrediction({ persist: false });
      await loadForecastHistory({ silent: true });
    } catch (e) {
      setLiveStatus("error", "连接失败");
      els.quoteCards.innerHTML = `<div class="quote-card">加载失败：${e.message}<br/>可稍后刷新，或检查网络与数据源。</div>`;
      startQuotePolling();
    }
  })();
})();
