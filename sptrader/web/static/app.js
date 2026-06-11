"use strict";

const els = {
  runSelect: document.getElementById("runSelect"),
  winRate: document.getElementById("winRate"),
  threshValue: document.getElementById("threshValue"),
  minTrades: document.getElementById("minTrades"),
  minOosTrades: document.getElementById("minOosTrades"),
  orderBy: document.getElementById("orderBy"),
  refreshBtn: document.getElementById("refreshBtn"),
  countLabel: document.getElementById("countLabel"),
  runMeta: document.getElementById("runMeta"),
  tbody: document.getElementById("tbody"),
  table: document.getElementById("table"),
  empty: document.getElementById("empty"),
};

const CAT = {
  trend: "#79c0ff",
  momentum: "#d2a8ff",
  volatility: "#f0883e",
  volume: "#56d364",
};

// Best-effort category guess from the indicator factory name (UI colouring only).
function guessCategory(factory) {
  const f = factory.toLowerCase();
  if (/(ema|sma|macd|adx|supertrend|aroon|psar|ichimoku|price_vs)/.test(f)) return "trend";
  if (/(rsi|stoch|cci|williams|roc|tsi|mom)/.test(f)) return "momentum";
  if (/(bollinger|keltner|donchian|atr)/.test(f)) return "volatility";
  if (/(obv|cmf|mfi|vwap|ad_)/.test(f)) return "volume";
  return "trend";
}

function pct(x) {
  return (100 * (x || 0)).toFixed(1) + "%";
}
function pct0(x) {
  return Math.round(100 * (x || 0)) + "%";
}

function winClass(wr) {
  if (wr >= 0.7) return "high";
  if (wr >= 0.55) return "mid";
  return "low";
}

function catOf(ind) {
  return ind.category && CAT[ind.category] ? ind.category : guessCategory(ind.factory);
}

function indicatorChips(indicators) {
  return indicators
    .map((ind) => {
      const cat = catOf(ind);
      const params = Object.entries(ind.params || {})
        .map(([k, v]) => `${k}=${v}`)
        .join(" ");
      return (
        `<span class="chip" style="border-left:3px solid ${CAT[cat]}">` +
        `<b>${ind.factory}</b>` +
        (params ? ` <span class="params">${params}</span>` : "") +
        `</span>`
      );
    })
    .join("");
}

function signed(x) {
  const cls = x >= 0 ? "pos" : "neg";
  return `<span class="${cls}">${pct(x)}</span>`;
}

async function loadRuns() {
  const res = await fetch("/api/runs");
  const data = await res.json();
  els.runSelect.innerHTML = "";
  if (!data.runs.length) {
    const opt = document.createElement("option");
    opt.textContent = "(no runs yet — run `optimize`)";
    els.runSelect.appendChild(opt);
    return;
  }
  data.runs.forEach((r) => {
    const opt = document.createElement("option");
    opt.value = r.run_id;
    opt.textContent = `#${r.run_id} ${r.symbol}@${r.interval} (${r.status}, ${r.completed}/${r.total})`;
    els.runSelect.appendChild(opt);
  });
}

async function loadStrategies() {
  const runId = els.runSelect.value;
  const minWin = (parseFloat(els.winRate.value) || 0) / 100;
  const minTrades = parseInt(els.minTrades.value, 10) || 0;
  const minOosTrades = parseInt(els.minOosTrades.value, 10) || 0;
  const orderBy = els.orderBy.value;

  const params = new URLSearchParams({
    min_win_rate: String(minWin),
    min_trades: String(minTrades),
    min_oos_trades: String(minOosTrades),
    order_by: orderBy,
    top: "300",
  });
  if (runId) params.set("run_id", runId);

  const res = await fetch("/api/strategies?" + params.toString());
  const data = await res.json();

  renderMeta(data.status);
  renderRows(data.strategies);
  els.countLabel.textContent = `${data.count} strategies ≥ ${pct0(data.min_win_rate)}`;
}

function renderMeta(status) {
  if (!status) {
    els.runMeta.innerHTML = "";
    return;
  }
  els.runMeta.innerHTML =
    `<strong>${status.symbol}</strong> @ ${status.interval} &nbsp;·&nbsp; ` +
    `run #${status.run_id} (${status.status}) &nbsp;·&nbsp; ` +
    `${status.completed}/${status.total} tested (${(status.pct || 0).toFixed(0)}%)`;
}

function renderRows(rows) {
  if (!rows || !rows.length) {
    els.table.classList.add("hidden");
    els.empty.classList.remove("hidden");
    els.empty.textContent =
      "No strategy cleared this success-rate threshold for this run. " +
      "Lower the threshold, reduce min trades, or run a longer search (more history / more permutations).";
    return;
  }
  els.empty.classList.add("hidden");
  els.table.classList.remove("hidden");

  els.tbody.innerHTML = rows
    .map((r, i) => {
      const wc = winClass(r.win_rate);
      const d = r.detail || { is: {}, oos: {} };
      const totW = (d.is.wins || 0) + (d.oos.wins || 0);
      const totT = (d.is.trades || 0) + (d.oos.trades || 0);
      const main =
        `<tr class="main-row" data-row="${i}">` +
        `<td class="caret" data-row="${i}">▶</td>` +
        `<td class="rank">${i + 1}</td>` +
        `<td><span class="winrate ${wc}">${pct0(r.win_rate)}</span>` +
        `<div class="wr-split">IS ${pct0(r.is_win_rate)} · OOS ${pct0(r.oos_win_rate)}</div></td>` +
        `<td><div class="chips">${indicatorChips(r.indicators)}</div></td>` +
        `<td><span class="mode-tag">${r.combine || "and"}</span></td>` +
        `<td><span class="mode-tag">${r.mode}</span></td>` +
        `<td>${r.adx_min == null ? "—" : r.adx_min}</td>` +
        `<td>${(r.score || 0).toFixed(2)}</td>` +
        `<td>${(r.is_sharpe || 0).toFixed(2)}</td>` +
        `<td>${(r.oos_sharpe || 0).toFixed(2)}</td>` +
        `<td>${signed(r.oos_return)}</td>` +
        `<td class="neg">${pct(r.oos_mdd)}</td>` +
        `<td>${totW}/${totT}</td>` +
        "</tr>";
      const detail =
        `<tr class="detail-row hidden" data-detail="${i}"><td></td><td colspan="12">` +
        detailHtml(r) +
        "</td></tr>";
      return main + detail;
    })
    .join("");

  els.tbody.querySelectorAll(".main-row").forEach((tr) => {
    tr.addEventListener("click", () => toggleDetail(tr.getAttribute("data-row")));
  });
}

function tradeBlock(title, h) {
  h = h || {};
  const pf = (h.profit_factor || 0) === 0 ? "—" : (h.profit_factor || 0).toFixed(2);
  return (
    `<div class="tblock"><h4>${title}</h4>` +
    `<dl>` +
    `<dt>Trades</dt><dd>${h.trades || 0}</dd>` +
    `<dt>Won</dt><dd class="pos">${h.wins || 0}</dd>` +
    `<dt>Lost</dt><dd class="neg">${h.losses || 0}</dd>` +
    `<dt>Success rate</dt><dd>${pct0(h.win_rate)}</dd>` +
    `<dt>Return</dt><dd>${signed(h.total_return)}</dd>` +
    `<dt>Sharpe</dt><dd>${(h.sharpe || 0).toFixed(2)}</dd>` +
    `<dt>Sortino</dt><dd>${(h.sortino || 0).toFixed(2)}</dd>` +
    `<dt>Max drawdown</dt><dd class="neg">${pct(h.max_drawdown)}</dd>` +
    `<dt>Profit factor</dt><dd>${pf}</dd>` +
    `<dt>Exposure</dt><dd>${pct0(h.exposure)}</dd>` +
    `<dt>Bars</dt><dd>${h.n_bars || 0}</dd>` +
    `</dl></div>`
  );
}

function detailHtml(r) {
  const d = r.detail || { is: {}, oos: {} };
  const indicators = r.indicators
    .map((ind) => {
      const cat = catOf(ind);
      const params = Object.entries(ind.params || {})
        .map(([k, v]) => `${k}=${v}`)
        .join(", ");
      return (
        `<li><span class="cat-dot" style="background:${CAT[cat]}"></span>` +
        `<b>${ind.factory}</b> <span class="cat-name">${cat}</span>` +
        (params ? `<span class="params"> · ${params}</span>` : "") +
        `</li>`
      );
    })
    .join("");
  return (
    `<div class="detail">` +
    `<div class="detail-indicators"><h4>Indicators used (${r.indicators.length}) · ` +
    `combine = ${r.combine || "and"}, ${r.mode}</h4><ul>${indicators}</ul></div>` +
    `<div class="detail-trades">${tradeBlock("In-sample (train)", d.is)}${tradeBlock("Out-of-sample (test)", d.oos)}</div>` +
    `</div>`
  );
}

function toggleDetail(i) {
  const row = els.tbody.querySelector(`[data-detail="${i}"]`);
  const caret = els.tbody.querySelector(`.caret[data-row="${i}"]`);
  if (!row) return;
  const open = row.classList.toggle("hidden");
  if (caret) caret.textContent = open ? "▶" : "▼";
}

els.winRate.addEventListener("input", () => {
  els.threshValue.textContent = els.winRate.value + "%";
});
els.winRate.addEventListener("change", loadStrategies);
els.minTrades.addEventListener("change", loadStrategies);
els.minOosTrades.addEventListener("change", loadStrategies);
els.orderBy.addEventListener("change", loadStrategies);
els.runSelect.addEventListener("change", loadStrategies);
els.refreshBtn.addEventListener("click", loadStrategies);

(async function init() {
  await loadRuns();
  await loadStrategies();
})();
