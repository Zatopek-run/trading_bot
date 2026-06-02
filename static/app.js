const eur = v => Number(v).toLocaleString("it-IT", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + " USDC";
const pct = v => (v >= 0 ? "+" : "") + Number(v).toFixed(1) + "%";

async function getJSON(url) {
  const r = await fetch(url);
  if (r.status === 401) { window.location = "/login"; return null; }
  return r.json();
}

let equityChart, dailyChart;

async function loadStats() {
  const s = await getJSON("/api/stats");
  if (!s) return;

  document.getElementById("initial").textContent = eur(s.initial_capital);
  document.getElementById("final").textContent   = eur(s.final_capital);
  document.getElementById("goal").textContent    = eur(s.goal_capital);
  document.getElementById("btc-bh").textContent  = eur(s.btc_buyhold);

  const fp = document.getElementById("final-pct");
  fp.textContent = pct(s.pnl_pct);
  fp.className = "stat-sub " + (s.pnl_pct >= 0 ? "pos" : "neg");

  const gs = document.getElementById("goal-status");
  gs.textContent = s.goal_reached ? "RAGGIUNTO" : "NON raggiunto";
  gs.className = "stat-sub " + (s.goal_reached ? "pos" : "neg");

  const bp = document.getElementById("btc-bh-pct");
  bp.textContent = pct(s.btc_buyhold_pct);
  bp.className = "stat-sub " + (s.btc_buyhold_pct >= 0 ? "pos" : "neg");

  document.getElementById("m-trades").textContent  = s.trades;
  document.getElementById("m-wins").textContent    = s.wins;
  document.getElementById("m-losses").textContent  = s.losses;
  document.getElementById("m-winrate").textContent = s.win_rate.toFixed(0) + "%";
  document.getElementById("m-best").textContent    = pct(s.best_trade);
  document.getElementById("m-worst").textContent   = pct(s.worst_trade);

  const start = new Date(s.start_ts * 1000);
  const end = new Date((s.start_ts + s.experiment_days * 86400) * 1000);
  const fmt = d => d.toLocaleDateString("it-IT", {day: "numeric", month: "short"});
  document.getElementById("experiment-sub").textContent =
    `Esperimento ${s.experiment_days} giorni — ${eur(s.initial_capital)} → ${eur(s.goal_capital)}`;
  document.getElementById("experiment-dates").textContent = `${fmt(start)} - ${fmt(end)}`;

  const now = Date.now() / 1000;
  const badge = document.getElementById("status-badge");
  if (now > s.start_ts + s.experiment_days * 86400) {
    badge.textContent = "COMPLETATO"; badge.className = "badge done";
  } else {
    badge.textContent = "IN CORSO"; badge.className = "badge running";
  }
}

async function loadEquity() {
  const data = await getJSON("/api/equity");
  if (!data) return;
  const labels = data.map(p => new Date(p.ts * 1000).toLocaleString("it-IT", {day:"2-digit", hour:"2-digit", minute:"2-digit"}));
  const portfolio = data.map(p => p.portfolio);
  const btc = data.map(p => p.btc);

  const ctx = document.getElementById("equityChart");
  if (equityChart) equityChart.destroy();
  equityChart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {label: "Portfolio AI Trading", data: portfolio, borderColor: "#5b8cff",
         backgroundColor: "rgba(91,140,255,.08)", borderWidth: 2, fill: true, tension: .3, pointRadius: 0},
        {label: "BTC Buy & Hold", data: btc, borderColor: "#f5b942", borderDash: [6,4],
         borderWidth: 2, fill: false, tension: .3, pointRadius: 0},
      ],
    },
    options: chartOpts(true),
  });
}

async function loadDaily() {
  const data = await getJSON("/api/daily");
  if (!data) return;
  const ctx = document.getElementById("dailyChart");
  if (dailyChart) dailyChart.destroy();
  dailyChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels: data.map(d => d.day),
      datasets: [{
        data: data.map(d => d.pnl),
        backgroundColor: data.map(d => d.pnl >= 0 ? "#22d39a" : "#f0556d"),
        borderRadius: 4,
      }],
    },
    options: chartOpts(false),
  });
}

async function loadTrades() {
  const data = await getJSON("/api/trades");
  if (!data) return;
  const body = document.getElementById("trades-body");
  if (!data.length) { body.innerHTML = `<tr><td colspan="9" class="muted">Nessun trade ancora.</td></tr>`; return; }
  body.innerHTML = data.map(t => {
    const dirClass = t.direction === "LONG" ? "long" : "short";
    const pnlClass = (t.pnl ?? 0) >= 0 ? "win" : "loss";
    const sl = t.sl_price ? Number(t.sl_price).toFixed(4) : "—";
    const tp = t.tp_price ? Number(t.tp_price).toFixed(4) : "—";
    return `<tr>
      <td>${t.symbol}</td>
      <td><span class="tag ${dirClass}">${t.direction}</span></td>
      <td>${Number(t.entry_price).toFixed(4)}</td>
      <td class="loss">${sl}</td>
      <td class="win">${tp}</td>
      <td>${t.exit_price ? Number(t.exit_price).toFixed(4) : "—"}</td>
      <td class="${pnlClass}">${t.status==="closed" ? (t.pnl>=0?"+":"")+Number(t.pnl).toFixed(2) : "—"}</td>
      <td class="${pnlClass}">${t.status==="closed" ? pct(t.pnl_pct) : "—"}</td>
      <td><span class="tag ${t.status}">${t.status==="open"?"APERTO":"CHIUSO"}</span></td>
    </tr>`;
  }).join("");
}

function chartOpts(showLegend) {
  return {
    responsive: true,
    plugins: {legend: {display: false}},
    scales: {
      x: {grid: {color: "#1c2333"}, ticks: {color: "#7a8499", maxTicksLimit: 8}},
      y: {grid: {color: "#1c2333"}, ticks: {color: "#7a8499"}},
    },
  };
}

async function refresh() {
  await Promise.all([loadStats(), loadEquity(), loadDaily(), loadTrades()]);
}

refresh();
setInterval(refresh, 30000);
