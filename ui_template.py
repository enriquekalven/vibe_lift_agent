import logo_asset

# pylint: disable=line-too-long
_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>VibeLift | Analytics Platform for Agent Optimization</title>
  <link rel="icon" type="image/jpeg" href="__VIBELIFT_GOOGLEY_LOGO_DATA_URI__" />
  <style>
    :root {
      --bg: #f8f9fa;
      --surface: #ffffff;
      --border: #dadce0;
      --text-primary: #202124;
      --text-secondary: #5f6368;
      --g-blue: #1a73e8;
      --g-blue-bg: #e8f0fe;
      --g-green: #1e8e3e;
      --g-green-bg: #e6f4ea;
      --g-yellow: #f9ab00;
      --g-yellow-bg: #fef7e0;
      --g-red: #d93025;
      --g-red-bg: #fce8e6;
      --font-sans: 'Google Sans', 'Roboto', -apple-system, BlinkMacSystemFont, sans-serif;
      --font-mono: 'Roboto Mono', monospace;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: var(--font-sans);
      background: var(--bg);
      color: var(--text-primary);
      line-height: 1.5;
    }
    .app-bar {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 12px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 20;
      box-shadow: 0 1px 3px rgba(60,64,67,0.06);
    }
    .brand-row {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .brand-logo-img {
      width: 48px;
      height: 48px;
      border-radius: 10px;
      object-fit: cover;
      border: 1px solid var(--border);
      box-shadow: 0 2px 6px rgba(60,64,67,0.14);
    }
    .brand-title {
      font-size: 19px;
      font-weight: 700;
      color: var(--text-primary);
      letter-spacing: -0.2px;
    }
    .brand-subtitle {
      font-size: 12px;
      color: var(--text-secondary);
    }
    .action-bar {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .btn {
      font-family: var(--font-sans);
      font-size: 13px;
      font-weight: 600;
      padding: 8px 16px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text-primary);
      cursor: pointer;
      transition: all 0.15s ease;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .btn:hover { background: #f1f3f4; }
    .btn-primary {
      background: var(--g-blue);
      color: #ffffff;
      border-color: var(--g-blue);
    }
    .btn-primary:hover { background: #1557b0; }
    .btn-green {
      background: var(--g-green);
      color: #ffffff;
      border-color: var(--g-green);
    }
    .btn-green:hover { background: #137333; }
    .btn-danger {
      background: var(--g-red-bg);
      color: var(--g-red);
      border-color: #f6aea9;
    }
    .btn-danger:hover { background: #fad2cf; }
    .tabs-bar {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 0 24px;
      display: flex;
      gap: 28px;
    }
    .tab-btn {
      background: none;
      border: none;
      padding: 14px 4px;
      font-size: 14px;
      font-weight: 600;
      color: var(--text-secondary);
      cursor: pointer;
      border-bottom: 3px solid transparent;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .tab-btn.active {
      color: var(--g-blue);
      border-bottom-color: var(--g-blue);
    }
    .tab-step-pill {
      font-size: 11px;
      padding: 2px 8px;
      border-radius: 99px;
      background: var(--g-blue-bg);
      color: var(--g-blue);
      font-weight: 700;
    }
    .container {
      max-width: 1400px;
      margin: 20px auto;
      padding: 0 24px 40px 24px;
    }
    .selector-banner {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 20px 24px;
      margin-bottom: 20px;
      display: grid;
      grid-template-columns: 1.2fr 1.8fr;
      gap: 24px;
      align-items: center;
      box-shadow: 0 1px 2px rgba(60,64,67,0.05);
    }
    .selector-label {
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      color: var(--g-blue);
      letter-spacing: 0.6px;
      margin-bottom: 6px;
    }
    .agent-select {
      width: 100%;
      padding: 11px 14px;
      font-size: 15px;
      font-weight: 600;
      font-family: var(--font-sans);
      color: var(--text-primary);
      background: var(--bg);
      border: 2px solid var(--g-blue);
      border-radius: 8px;
      cursor: pointer;
    }
    .agent-meta-box {
      display: flex;
      flex-direction: column;
      gap: 8px;
      border-left: 1px solid var(--border);
      padding-left: 24px;
    }
    .agent-meta-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 10px;
    }
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 16px;
      margin-bottom: 20px;
    }
    .kpi-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px 18px;
      position: relative;
      overflow: hidden;
    }
    .kpi-card::top-bar {
      height: 4px;
    }
    .kpi-label {
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      color: var(--text-secondary);
      letter-spacing: 0.5px;
    }
    .kpi-value {
      font-size: 26px;
      font-weight: 700;
      margin: 6px 0 4px 0;
    }
    .kpi-sub {
      font-size: 12px;
      color: var(--text-secondary);
    }
    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 20px 24px;
      margin-bottom: 20px;
    }
    .panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 14px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--border);
      flex-wrap: wrap;
      gap: 12px;
    }
    .panel-title {
      font-size: 16px;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      padding: 3px 10px;
      border-radius: 99px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.2px;
    }
    .badge-blue { background: var(--g-blue-bg); color: var(--g-blue); }
    .badge-green { background: var(--g-green-bg); color: var(--g-green); }
    .badge-yellow { background: var(--g-yellow-bg); color: #b06000; }
    .badge-red { background: var(--g-red-bg); color: var(--g-red); }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      text-align: left;
      padding: 12px 12px;
      border-bottom: 1px solid var(--border);
    }
    th {
      color: var(--text-secondary);
      font-weight: 700;
      font-size: 11px;
      text-transform: uppercase;
      background: #f8f9fa;
    }
    .mono { font-family: var(--font-mono); font-size: 12px; }
    .weight-bar-bg {
      width: 90px;
      height: 8px;
      background: #e8eaed;
      border-radius: 99px;
      overflow: hidden;
      display: inline-block;
      vertical-align: middle;
      margin-right: 6px;
    }
    .weight-bar-fill {
      height: 100%;
      background: var(--g-blue);
      border-radius: 99px;
    }
    .add-param-form {
      background: #f8f9fa;
      border: 1px dashed var(--border);
      border-radius: 8px;
      padding: 14px 16px;
      margin-top: 16px;
      display: grid;
      grid-template-columns: 1.6fr 0.7fr 1.1fr 0.9fr 0.9fr 0.8fr auto;
      gap: 10px;
      align-items: end;
    }
    .field-group label {
      display: block;
      font-size: 11px;
      font-weight: 600;
      color: var(--text-secondary);
      margin-bottom: 4px;
    }
    .field-group input, .field-group select {
      width: 100%;
      padding: 7px 10px;
      font-size: 13px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #ffffff;
    }
    .charts-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 18px;
      margin-bottom: 20px;
    }
    .chart-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px 20px;
    }
    .action-card {
      border: 1px solid var(--border);
      border-left: 5px solid var(--g-green);
      border-radius: 8px;
      padding: 16px 20px;
      margin-bottom: 14px;
      background: #ffffff;
    }
    .action-card.rejected {
      border-left-color: var(--g-red);
      background: #fffafa;
    }
    .action-card.baseline {
      border-left-color: var(--text-secondary);
      background: #fafafa;
    }
    .action-grid {
      display: grid;
      grid-template-columns: 1.1fr 1.2fr 1.2fr;
      gap: 16px;
      margin-top: 10px;
    }
    .action-box {
      background: #f8f9fa;
      border: 1px solid #eceff1;
      border-radius: 6px;
      padding: 10px 12px;
      font-size: 12.5px;
    }
    .action-box-title {
      font-size: 10.5px;
      font-weight: 700;
      text-transform: uppercase;
      color: var(--text-secondary);
      margin-bottom: 4px;
    }
    .diff-pre {
      background: #1e1e1e;
      color: #e8eaed;
      font-family: var(--font-mono);
      font-size: 11.5px;
      padding: 10px 12px;
      border-radius: 6px;
      overflow-x: auto;
      white-space: pre-wrap;
    }
    .live-pill { background: var(--g-green-bg); color: var(--g-green); }
    .live-dot {
      display: inline-block; width: 8px; height: 8px; border-radius: 50%;
      background: var(--g-green); margin-right: 4px; animation: livePulse 2s infinite;
    }
    @keyframes livePulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
    .fleet-controls { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 12px; color: var(--text-secondary); }
    .fleet-controls select { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; font-family: var(--font-sans); font-size: 12px; }
    .fleet-meta { font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; }
    .fleet-sources { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
    .chip { font-size: 11px; font-weight: 600; padding: 3px 9px; border-radius: 99px; border: 1px solid var(--border); }
    .chip-ok { background: var(--g-green-bg); color: var(--g-green); border-color: transparent; }
    .chip-error { background: var(--g-red-bg); color: var(--g-red); border-color: transparent; }
    .chip-na { background: #f1f3f4; color: var(--text-secondary); }
    .fleet-kpis { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
    .fleet-kpis .kpi-value { font-size: 22px; }
    .table-scroll { overflow-x: auto; }
    .fleet-agent-name { font-weight: 700; }
    .fleet-agent-desc { font-size: 11px; color: var(--text-secondary); margin-top: 2px; max-width: 360px; }
    .fleet-agent-tags { display: flex; gap: 6px; margin-top: 6px; flex-wrap: wrap; }
    .fleet-row-muted td { color: var(--text-secondary); }
    .fleet-empty { color: var(--text-secondary); text-align: center; }
    .fleet-notice { font-size: 12px; padding: 8px 12px; border-radius: 6px; background: var(--g-blue-bg); color: var(--g-blue); margin-bottom: 12px; }
    .fleet-notice-error { background: var(--g-yellow-bg); color: #b06000; }
    .fleet-errors { margin-top: 12px; font-size: 12px; }
    .fleet-error { background: var(--g-red-bg); color: var(--g-red); padding: 6px 10px; border-radius: 6px; margin-bottom: 6px; word-break: break-word; }
    .fleet-notes { font-size: 11px; color: var(--text-secondary); margin: 12px 0 0 16px; padding: 0; }
    .hidden { display: none !important; }
  </style>
</head>
<body>
  <header class="app-bar">
    <div class="brand-row">
      <img
        id="vibeliftGoogleyLogo"
        class="brand-logo-img"
        src="__VIBELIFT_GOOGLEY_LOGO_DATA_URI__"
        alt="vibelift_googley_logo_1789766299532.jpg"
      />
      <div>
        <div class="brand-title">VibeLift | Analytics Platform for Agent Optimization</div>
        <div class="brand-subtitle">
          Autonomous Multi-Objective Agent Optimization &bull; Powered by AlphaEvolve Closed-Loop Telemetry
        </div>
      </div>
      <div style="display:inline-flex;align-items:center;gap:8px;margin-left:16px;padding:4px 12px;background:#e8f0fe;border-radius:16px;border:1px solid #c2e7ff;font-size:12px;font-weight:600;color:#174ea6;">
        <span>&#x2601;&#xFE0F; GCP Project: <strong id="gcpProjectText" class="mono">&#x2026;</strong> (<span id="gcpRegionText" class="mono">&#x2026;</span>)</span>
        <span class="badge badge-green" style="font-size:10px;">Cloud Run ACTIVE</span>
      </div>
    </div>
    <div class="action-bar">
      <span id="activeGenBadge" class="badge badge-green">ACTIVE GENOME: GEN 14</span>
      <button class="btn btn-blue" id="syncGcpBtn" onclick="syncGcpTelemetry()" title="Fetch live telemetry from Cloud Logging & Gemini Enterprise">
        &#x2601;&#xFE0F; Sync GCP Telemetry
      </button>
      <button class="btn btn-danger" onclick="triggerApi('/api/inject_anomaly')">
        &#x1F525; Inject Production Log Anomaly
      </button>
      <button class="btn btn-green" onclick="triggerApi('/api/evolve_generation')">
        &#x1F9EC; Run AlphaEvolve Optimization Cycle
      </button>
      <button class="btn" onclick="triggerApi('/api/reset')">
        Reset Demo
      </button>
      <button id="btnModeFullscreen" class="btn" onclick="toggleDisplayMode()" title="Expand to fullscreen or side panel" style="display:none;">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg> Fullscreen
      </button>
    </div>
  </header>

  <nav class="tabs-bar">
    <button id="tabBtn0" class="tab-btn active" onclick="switchTab(0)">
      <span class="tab-step-pill live-pill"><span class="live-dot"></span>LIVE</span>
      Gemini Enterprise Agent Fleet
    </button>
    <button id="tabBtn1" class="tab-btn" onclick="switchTab(1)">
      <span class="tab-step-pill">STEP 1</span>
      Select Agent &amp; Configure Optimization Parameters
    </button>
    <button id="tabBtn2" class="tab-btn" onclick="switchTab(2)">
      <span class="tab-step-pill">STEP 2</span>
      Parameter Performance Over Time &amp; AlphaEvolve Actions Taken
    </button>
  </nav>

  <main class="container">
    <!-- Persistent Agent Selector Banner across both tabs -->
    <section id="demoSelectorBanner" class="selector-banner hidden">
      <div>
        <div class="selector-label">1. Select Your Production Agent to Optimize</div>
        <select id="agentDropdown" class="agent-select" onchange="onSelectAgent(this.value)">
          <option value="mortgage_assistant">Mortgage Underwriting Assistant (gemini-3.1-flash-lite)</option>
          <option value="forecast_engine">Financial Grid &amp; Rate Forecast Engine (gemini-3.1-flash)</option>
          <option value="stock_market_updates">Stock Market &amp; Portfolio Intelligence Agent (Multi-Subagent)</option>
        </select>
      </div>
      <div class="agent-meta-box">
        <div class="agent-meta-top">
          <div>
            <strong id="agentTitleText" style="font-size:16px;">Mortgage Underwriting Assistant</strong>
            <span id="agentDomainBadge" class="badge badge-blue" style="margin-left:8px;">Consumer Lending &amp; Risk</span>
          </div>
          <span id="agentHealthBadge" class="badge badge-green">OPTIMIZED (GEN 14)</span>
        </div>
        <div id="agentDescText" style="font-size:13px;color:var(--text-secondary);"></div>
        <div style="font-size:12px;color:var(--text-secondary);display:flex;gap:18px;flex-wrap:wrap;">
          <span><strong>Foundation Model:</strong> <span id="agentModelText" class="mono"></span></span>
          <span><strong>Optimization Engine:</strong> <span class="mono">AlphaEvolve Pareto Multi-Objective Loop</span></span>
          <span><strong>Zero-Code Mode:</strong> <span style="color:var(--g-green);font-weight:600;">Log-Driven Genome Mutation</span></span>
        </div>
      </div>
    </section>

    <!-- TAB 0: LIVE GEMINI ENTERPRISE AGENT FLEET (real inventory joined with real telemetry) -->
    <section id="tabPanel0">
      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <span>Agents deployed on Gemini Enterprise</span>
            <span id="fleetEngineBadge" class="badge badge-blue">Loading&hellip;</span>
          </div>
          <div class="fleet-controls">
            <label for="fleetWindow">Window</label>
            <select id="fleetWindow" onchange="onFleetWindowChange()">
              <option value="1">Last hour</option>
              <option value="6">Last 6 hours</option>
              <option value="24" selected>Last 24 hours</option>
              <option value="168">Last 7 days</option>
            </select>
            <label><input type="checkbox" id="fleetAuto" checked onchange="scheduleFleetRefresh()" /> Auto-refresh (60 s)</label>
            <button class="btn btn-blue" id="fleetRefreshBtn" onclick="refreshFleet(true)">Refresh now</button>
          </div>
        </div>
        <div id="fleetNotice" class="fleet-notice hidden"></div>
        <div id="fleetMeta" class="fleet-meta">Waiting for live data from Google Cloud&hellip;</div>
        <div id="fleetSources" class="fleet-sources"></div>
        <div id="fleetKpis" class="kpi-grid fleet-kpis"></div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Agent</th>
                <th>Type</th>
                <th>Runs on</th>
                <th>Requests</th>
                <th>4xx / 5xx</th>
                <th>Latency p50 / p95</th>
                <th>LLM calls</th>
                <th>Tokens in / out</th>
                <th>Conversations</th>
                <th>Last activity</th>
              </tr>
            </thead>
            <tbody id="fleetAgentsBody"></tbody>
          </table>
        </div>
        <div id="fleetErrors" class="fleet-errors hidden"></div>
      </div>

      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <span>Vertex AI model usage</span>
            <span id="modelCostBadge" class="badge badge-green">&mdash;</span>
          </div>
          <div id="modelScopeText" style="font-size:12px;color:var(--text-secondary);"></div>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Model</th>
                <th>Calls</th>
                <th>Input</th>
                <th>Output</th>
                <th>Cache read</th>
                <th>Cache write</th>
                <th>Cache share</th>
                <th>Est. cost (list price)</th>
              </tr>
            </thead>
            <tbody id="modelUsageBody"></tbody>
          </table>
        </div>
        <ul id="fleetNotes" class="fleet-notes"></ul>
      </div>
    </section>

    <!-- TAB 1: AGENT SELECTION & OPTIMIZATION PARAMETERS -->
    <section id="tabPanel1" class="hidden">
      <div class="kpi-grid" id="tab1KpiCards"></div>

      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <span>2. Agent Optimization Parameters &amp; Current Live Status</span>
            <span class="badge badge-blue">Multi-Objective Pareto Frontier</span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);">
            Each parameter is continuously monitored from agent logs and weighted in the AlphaEvolve fitness objective.
          </div>
        </div>

        <table>
          <thead>
            <tr>
              <th>Optimization Parameter</th>
              <th>Direction</th>
              <th>Baseline (Gen 0)</th>
              <th>Current Live Value</th>
              <th>Improvement vs. Baseline</th>
              <th>Target Goal (SLO)</th>
              <th>Priority Weight</th>
              <th>Current Status</th>
            </tr>
          </thead>
          <tbody id="parametersTableBody"></tbody>
        </table>

        <div class="add-param-form">
          <div class="field-group">
            <label>Add User-Defined Parameter</label>
            <input id="newParamLabel" type="text" placeholder="e.g., Hallucination Guardrail Score" value="Tool Selection Precision" />
          </div>
          <div class="field-group">
            <label>Unit</label>
            <input id="newParamUnit" type="text" placeholder="%, ms, $" value="%" />
          </div>
          <div class="field-group">
            <label>Optimization Goal</label>
            <select id="newParamDirection">
              <option value="higher_is_better">Maximize (&uarr; Higher is Better)</option>
              <option value="lower_is_better">Minimize (&darr; Lower is Better)</option>
            </select>
          </div>
          <div class="field-group">
            <label>Baseline Value</label>
            <input id="newParamBaseline" type="number" step="0.1" value="84.0" />
          </div>
          <div class="field-group">
            <label>Target Goal</label>
            <input id="newParamTarget" type="number" step="0.1" value="97.5" />
          </div>
          <div class="field-group">
            <label>Weight (%)</label>
            <input id="newParamWeight" type="number" step="1" value="15" />
          </div>
          <div>
            <button class="btn btn-primary" onclick="onAddCustomParameter()">
              + Add Parameter
            </button>
          </div>
        </div>
      </div>
    </section>

    <!-- TAB 2: PARAMETER PERFORMANCE OVER TIME & ALPHAEVOLVE ACTIONS -->
    <section id="tabPanel2" class="hidden">
      <div class="charts-grid">
        <div class="chart-card">
          <div class="panel-header" style="margin-bottom:8px;padding-bottom:6px;">
            <div class="panel-title" style="font-size:14px;">
              P95 Latency Performance Over Time (ms &darr;)
            </div>
            <span id="latencyDeltaBadge" class="badge badge-green"></span>
          </div>
          <div id="chartLatencySvg"></div>
        </div>

        <div class="chart-card">
          <div class="panel-header" style="margin-bottom:8px;padding-bottom:6px;">
            <div class="panel-title" style="font-size:14px;">
              Net Cost per 1k Turns Over Time ($ USD &darr;)
            </div>
            <span id="costDeltaBadge" class="badge badge-green"></span>
          </div>
          <div id="chartCostSvg"></div>
        </div>

        <div class="chart-card">
          <div class="panel-header" style="margin-bottom:8px;padding-bottom:6px;">
            <div class="panel-title" style="font-size:14px;">
              Task Accuracy Score Over Time (% &uarr; with Safety Guardrail)
            </div>
            <span id="accuracyDeltaBadge" class="badge badge-blue"></span>
          </div>
          <div id="chartAccuracySvg"></div>
        </div>

        <div class="chart-card">
          <div class="panel-header" style="margin-bottom:8px;padding-bottom:6px;">
            <div class="panel-title" style="font-size:14px;">
              Prompt Cache Hit Ratio Over Time (% &uarr;)
            </div>
            <span id="cacheDeltaBadge" class="badge badge-green"></span>
          </div>
          <div id="chartCacheSvg"></div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <span>What Actions AlphaEvolve Took to Improve These Parameters</span>
            <span class="badge badge-green">Closed-Loop Log Diagnosis &rarr; Genome Action &rarr; Verified Impact</span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);">
            Includes automatic Reviewer Gatekeeper rollback when a candidate mutation violates accuracy guardrails (e.g. Gen 9).
          </div>
        </div>
        <div id="actionsTimelineContainer"></div>
      </div>

    </section>
  </main>

  <script>
    function switchTab(tabIndex) {
      const tab = [0, 1, 2].indexOf(Number(tabIndex)) >= 0 ? Number(tabIndex) : 0;
      [0, 1, 2].forEach(function(i) {
        document.getElementById('tabBtn' + i).classList.toggle('active', tab === i);
        document.getElementById('tabPanel' + i).classList.toggle('hidden', tab !== i);
      });
      const banner = document.getElementById('demoSelectorBanner');
      if (banner) banner.classList.toggle('hidden', tab === 0);
    }

    function renderMiniChart(points, key, unit, color, targetVal, lowerIsBetter) {
      if (!points || points.length === 0) return '';
      const vals = points.map(p => Number(p[key]));
      const allVals = [...vals, targetVal];
      const minV = Math.min(...allVals) * 0.9;
      const maxV = Math.max(...allVals) * 1.08 || 1;
      const w = 580;
      const h = 150;
      const padL = 44;
      const padR = 24;
      const padT = 22;
      const padB = 32;
      const plotW = w - padL - padR;
      const plotH = h - padT - padB;

      const xFor = (i) => padL + (points.length === 1 ? plotW / 2 : (i / (points.length - 1)) * plotW);
      const yFor = (v) => padT + plotH - ((v - minV) / (maxV - minV)) * plotH;

      const polyPoints = points.map((p, i) => `${xFor(i).toFixed(1)},${yFor(Number(p[key])).toFixed(1)}`).join(' ');
      const targetY = yFor(targetVal).toFixed(1);

      let circlesHtml = '';
      points.forEach((p, i) => {
        const cx = xFor(i).toFixed(1);
        const cy = yFor(Number(p[key])).toFixed(1);
        const isRejected =
          String(p.timestamp_label || '').includes('Rej') ||
          String(p.event_marker || '').includes('Rejected') ||
          String(p.event_marker || '').includes('Blocked');
        const dotColor = isRejected ? '#d93025' : color;
        circlesHtml += `
          <circle cx="${cx}" cy="${cy}" r="${isRejected ? 5.5 : 4.5}" fill="${dotColor}" stroke="#fff" stroke-width="1.5"/>
          <text x="${cx}" y="${(Number(cy) - 8).toFixed(1)}" text-anchor="middle" font-size="10.5" font-weight="700" fill="${dotColor}">
            ${unit === '$' ? '$' + Number(p[key]).toFixed(1) : Number(p[key]) + unit}
          </text>
          <text x="${cx}" y="${h - 8}" text-anchor="middle" font-size="10" fill="#5f6368">
            ${p.timestamp_label}
          </text>
        `;
      });

      return `
        <svg viewBox="0 0 ${w} ${h}" width="100%" height="155">
          <line x1="${padL}" y1="${targetY}" x2="${w - padR}" y2="${targetY}" stroke="#1e8e3e" stroke-dasharray="4,4" stroke-width="1.2"/>
          <text x="${w - padR}" y="${(Number(targetY) - 4).toFixed(1)}" text-anchor="end" font-size="9.5" fill="#1e8e3e" font-weight="600">
            Target Goal: ${unit === '$' ? '$' + targetVal : targetVal + unit}
          </text>
          <polyline fill="none" stroke="${color}" stroke-width="2.6" points="${polyPoints}"/>
          ${circlesHtml}
        </svg>
      `;
    }

    function renderState(state) {
      if (!state) return;
      if (state.ge_fleet) renderFleet(state.ge_fleet);
      const agent = state.active_agent;
      if (!agent) return;

      const ts = agent.timeline || [];
      const activeGen = ts.length > 0 ? ts[ts.length - 1].generation : 14;

      document.getElementById('agentDropdown').value = agent.agent_id;
      document.getElementById('agentTitleText').textContent = agent.display_name;
      document.getElementById('agentDomainBadge').textContent = agent.domain;
      document.getElementById('agentDescText').textContent =
        'Estimated Monthly AlphaEvolve Savings: $' + Number(agent.monthly_savings_usd || 0).toLocaleString() + '/mo';
      document.getElementById('agentModelText').textContent = agent.model;

      const healthEl = document.getElementById('agentHealthBadge');
      healthEl.textContent = agent.health_status;
      healthEl.className = 'badge ' + (
        agent.health_status.includes('CRITICAL') ? 'badge-red' :
        agent.health_status.includes('NEEDS') ? 'badge-yellow' : 'badge-green'
      );

      document.getElementById('activeGenBadge').textContent =
        'ACTIVE GENOME: GEN ' + activeGen;

      if (state.gcp_project) {
        const projEl = document.getElementById('gcpProjectText');
        if (projEl) projEl.textContent = state.gcp_project;
      }
      if (state.gcp_region) {
        const regEl = document.getElementById('gcpRegionText');
        if (regEl) regEl.textContent = state.gcp_region;
      }

      // Render top 4 KPI cards on Tab 1 from key parameters
      const params = agent.parameters || [];
      const topFour = params.slice(0, 4);
      document.getElementById('tab1KpiCards').innerHTML = topFour.map(p => {
        const isLower = p.direction === 'LOWER';
        const diff = isLower
          ? ((p.baseline_value - p.current_value) / (p.baseline_value || 1)) * 100
          : ((p.current_value - p.baseline_value) / (p.baseline_value || 1)) * 100;
        const sign = diff >= 0 ? 'Improved ' + diff.toFixed(1) + '%' : 'Regressed ' + Math.abs(diff).toFixed(1) + '%';
        const badgeCls = (p.status.includes('BREACH') || p.status.includes('⚠️'))
          ? 'badge-red'
          : 'badge-green';
        const valText = p.unit.startsWith('$') ? '$' + Number(p.current_value).toFixed(2) : p.current_value + ' ' + p.unit;
        const baseText = p.unit.startsWith('$') ? '$' + Number(p.baseline_value).toFixed(2) : p.baseline_value + ' ' + p.unit;
        return `
          <div class="kpi-card">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
              <span class="kpi-label">${esc(p.label)}</span>
              <span class="badge ${badgeCls}">${esc(p.status)}</span>
            </div>
            <div class="kpi-value">${esc(valText)}</div>
            <div class="kpi-sub">
              Baseline: <strong>${esc(baseText)}</strong> &bull; <span style="color:var(--g-green);font-weight:600;">${sign}</span>
            </div>
          </div>
        `;
      }).join('');

      // Render Parameters Table
      document.getElementById('parametersTableBody').innerHTML = params.map(p => {
        const isLower = p.direction === 'LOWER';
        const dirBadge = isLower
          ? '<span class="badge badge-blue">&darr; Minimize</span>'
          : '<span class="badge badge-blue">&uarr; Maximize</span>';
        const baseFormatted = p.unit.startsWith('$') ? '$' + Number(p.baseline_value).toFixed(2) : p.baseline_value + ' ' + p.unit;
        const currFormatted = p.unit.startsWith('$') ? '$' + Number(p.current_value).toFixed(2) : p.current_value + ' ' + p.unit;
        const targetFormatted = p.unit.startsWith('$') ? '$' + Number(p.target_value).toFixed(2) : p.target_value + ' ' + p.unit;

        const deltaPct = isLower
          ? ((p.baseline_value - p.current_value) / (p.baseline_value || 1)) * 100
          : ((p.current_value - p.baseline_value) / (p.baseline_value || 1)) * 100;
        const deltaHtml = deltaPct >= 0
          ? `<span style="color:var(--g-green);font-weight:700;">+${deltaPct.toFixed(1)}% better</span>`
          : `<span style="color:var(--g-red);font-weight:700;">${deltaPct.toFixed(1)}% worse</span>`;

        const statusBadge = (p.status.includes('BREACH') || p.status.includes('⚠️'))
          ? `<span class="badge badge-red">${esc(p.status)}</span>`
          : `<span class="badge badge-green">${esc(p.status)}</span>`;

        return `
          <tr>
            <td><strong>${esc(p.label)}</strong></td>
            <td>${dirBadge}</td>
            <td class="mono">${esc(baseFormatted)}</td>
            <td class="mono" style="font-weight:700;font-size:14px;">${esc(currFormatted)}</td>
            <td>${deltaHtml}</td>
            <td class="mono">${esc(targetFormatted)}</td>
            <td>
              <div class="weight-bar-bg">
                <div class="weight-bar-fill" style="width:${Math.min(100, Number(p.weight_pct) * 2)}%;"></div>
              </div>
              <span class="mono">${Number(p.weight_pct)}%</span>
            </td>
            <td>${statusBadge}</td>
          </tr>
        `;
      }).join('');

      // Render Tab 2 Charts
      const findParam = (k, fallback) => {
        const found = params.find(x => x.key === k);
        return found ? Number(found.target_value) : fallback;
      };
      document.getElementById('chartLatencySvg').innerHTML =
        renderMiniChart(ts, 'latency_ms', 'ms', '#1a73e8', findParam('latency_ms', 800), true);
      document.getElementById('chartCostSvg').innerHTML =
        renderMiniChart(ts, 'cost_usd', '$', '#1e8e3e', findParam('cost_usd', 4.0), true);
      document.getElementById('chartAccuracySvg').innerHTML =
        renderMiniChart(ts, 'accuracy_pct', '%', '#9334e6', findParam('accuracy_pct', 95.0), false);
      document.getElementById('chartCacheSvg').innerHTML =
        renderMiniChart(ts, 'cache_hit_pct', '%', '#e37400', findParam('cache_hit_pct', 90.0), false);

      if (ts.length >= 2) {
        const first = ts[0];
        const last = ts[ts.length - 1];
        document.getElementById('latencyDeltaBadge').textContent =
          `${first.latency_ms}ms \u2192 ${last.latency_ms}ms`;
        document.getElementById('costDeltaBadge').textContent =
          `$${first.cost_usd} \u2192 $${last.cost_usd}/1k`;
        document.getElementById('accuracyDeltaBadge').textContent =
          `${first.accuracy_pct}% \u2192 ${last.accuracy_pct}%`;
        document.getElementById('cacheDeltaBadge').textContent =
          `${first.cache_hit_pct}% \u2192 ${last.cache_hit_pct}%`;
      }

      // Render Tab 2 AlphaEvolve Action Cards
      const actions = agent.actions || [];
      document.getElementById('actionsTimelineContainer').innerHTML = actions.map(a => {
        const isRej = a.status.includes('REJECTED');
        const isBase = a.status.includes('BASELINE');
        const cardCls = isRej ? 'action-card rejected' : (isBase ? 'action-card baseline' : 'action-card');
        const badgeCls = isRej ? 'badge-red' : (isBase ? 'badge-yellow' : 'badge-green');
        return `
          <div class="${cardCls}">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
              <div>
                <span class="badge badge-blue" style="margin-right:8px;">Gen ${a.generation} &bull; ${a.timestamp}</span>
                <strong style="font-size:15px;">${a.action_title}</strong>
                <span class="badge badge-blue" style="margin-left:6px;">Target: ${a.parameter_targeted}</span>
              </div>
              <span class="badge ${badgeCls}">${a.status}</span>
            </div>
            <div class="action-grid">
              <div class="action-box">
                <div class="action-box-title">1. Root Cause Detected in Logs</div>
                <div>${a.root_cause_from_logs}</div>
              </div>
              <div class="action-box">
                <div class="action-box-title">2. Action Taken by AlphaEvolve</div>
                <div><strong>${a.action_taken}</strong></div>
                <div style="margin-top:6px;color:var(--g-green);font-weight:700;font-size:12px;">
                  Impact: ${a.impact_summary}
                </div>
              </div>
              <div>
                <div class="action-box-title" style="margin-bottom:4px;">3. Genome Prompt / Config Diff</div>
                <pre class="diff-pre">${a.diff_snippet}</pre>
              </div>
            </div>
          </div>
        `;
      }).join('');
    }

    // ---------------------------------------------------------------------------
    // Live Gemini Enterprise agent fleet: real inventory joined with real telemetry.
    // All API-sourced strings are inserted with textContent / createElement (no innerHTML).
    // TODO(security): the demo renderers above still build HTML strings from server-generated
    // demo data; user-supplied fields are escaped with esc().
    // ---------------------------------------------------------------------------
    const FLEET_TOOL = 'query_ge_agent_fleet';
    const FLEET_REFRESH_MS = 60000;
    let fleetWindowHours = 24;
    let fleetTimer = null;
    let fleetRefreshMode = 'pending';  // 'host' (MCP App bridge), 'http' (direct API), 'snapshot'
    let lastFleet = null;

    function isEmbedded() {
      return window.parent && window.parent !== window;
    }

    function esc(value) {
      return String(value === null || value === undefined ? '' : value)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function el(tag, className, children) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      (children || []).forEach(function(child) {
        if (child === null || child === undefined || child === false) return;
        node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
      });
      return node;
    }

    function badge(text, cls) { return el('span', 'badge ' + cls, [text]); }
    function fmtInt(v) { return v == null ? '—' : Number(v).toLocaleString(); }
    function fmtTokens(v) {
      if (v == null) return '—';
      const n = Number(v);
      if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
      if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
      return String(n);
    }
    function fmtMs(v) {
      if (v == null) return '—';
      return v >= 1000 ? (v / 1000).toFixed(1) + ' s' : Math.round(v) + ' ms';
    }
    function fmtUsd(v) {
      if (v == null) return 'no rate card';
      return '$' + Number(v).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    }
    function fmtAgo(iso) {
      if (!iso) return '—';
      const t = Date.parse(iso);
      if (isNaN(t)) return String(iso);
      const secs = Math.max(0, (Date.now() - t) / 1000);
      if (secs < 90) return Math.round(secs) + ' s ago';
      if (secs < 5400) return Math.round(secs / 60) + ' min ago';
      if (secs < 172800) return Math.round(secs / 3600) + ' h ago';
      return Math.round(secs / 86400) + ' d ago';
    }
    function windowLabel(hours) {
      const h = Number(hours) || fleetWindowHours;
      if (h === 168) return 'last 7 days';
      if (h >= 48 && h % 24 === 0) return 'last ' + (h / 24) + ' days';
      return h === 1 ? 'last hour' : 'last ' + h + ' hours';
    }

    const SOURCE_LABELS = {
      inventory: 'GE agent inventory',
      agent_engine_metrics: 'Agent Engine metrics',
      agent_token_logs: 'Agent token logs',
      cloud_run_metrics: 'Cloud Run metrics',
      model_usage: 'Vertex AI model usage',
      ge_traffic: 'GE assistant traffic',
    };

    function kpiCard(label, value, sub) {
      return el('div', 'kpi-card', [
        el('div', 'kpi-label', [label]),
        el('div', 'kpi-value mono', [value]),
        el('div', 'kpi-sub', [sub]),
      ]);
    }

    function setFleetNotice(text, isWarning) {
      const node = document.getElementById('fleetNotice');
      if (!node) return;
      node.textContent = text || '';
      node.className = 'fleet-notice' + (text ? '' : ' hidden') + (isWarning ? ' fleet-notice-error' : '');
    }

    function emptyRow(colspan, text) {
      const td = el('td', 'fleet-empty', [text]);
      td.colSpan = colspan;
      return el('tr', null, [td]);
    }

    function renderFleet(fleet) {
      if (!fleet || !Array.isArray(fleet.agents)) return;
      lastFleet = fleet;
      if (fleet.window_hours) {
        fleetWindowHours = Number(fleet.window_hours);
        const sel = document.getElementById('fleetWindow');
        if (sel && Array.from(sel.options).some(function(o) { return Number(o.value) === fleetWindowHours; })) {
          sel.value = String(fleetWindowHours);
        }
      }
      const win = windowLabel(fleet.window_hours);
      document.getElementById('fleetEngineBadge').textContent =
        (fleet.engines || []).map(function(e) { return e.display_name || e.engine_id; }).join(', ') || 'Gemini Enterprise';

      const meta = ['Project ' + (fleet.project_id || '—'), win];
      if (fleet.generated_at) meta.push('updated ' + new Date(fleet.generated_at).toLocaleTimeString());
      if (fleet.collection_ms != null) meta.push('collected in ' + (fleet.collection_ms / 1000).toFixed(1) + ' s');
      if (fleet.cache_age_seconds) meta.push('cache age ' + Math.round(fleet.cache_age_seconds) + ' s');
      meta.push({host: 'live via Gemini Enterprise', http: 'live via VibeLift API', snapshot: 'snapshot'}[fleetRefreshMode] || 'snapshot from dashboard open');
      document.getElementById('fleetMeta').textContent = meta.join(' · ');

      const sources = document.getElementById('fleetSources');
      sources.replaceChildren();
      Object.keys(fleet.source_status || {}).forEach(function(key) {
        const st = fleet.source_status[key];
        const cls = st === 'ok' ? 'chip chip-ok' : (st === 'error' ? 'chip chip-error' : 'chip chip-na');
        const suffix = st === 'ok' ? ' ✓' : (st === 'error' ? ' · unavailable' : ' · n/a');
        sources.appendChild(el('span', cls, [(SOURCE_LABELS[key] || key) + suffix]));
      });

      const t = fleet.totals || {};
      const byType = Object.keys(t.by_type || {}).sort().map(function(k) { return t.by_type[k] + ' ' + k; }).join(' · ');
      const usage = (fleet.model_usage && fleet.model_usage.totals) || null;
      const traffic = fleet.ge_traffic || null;
      const unrated = usage && (usage.models_without_rate_card || []).length;
      document.getElementById('fleetKpis').replaceChildren(
        kpiCard('Agents on Gemini Enterprise', fmtInt(t.agents), (t.enabled || 0) + ' enabled' + (byType ? ' · ' + byType : '')),
        kpiCard('Requests · ' + win, fmtInt(t.requests), t.requests == null ? 'no runtime telemetry'
          : fmtInt(t.errors_4xx) + ' 4xx · ' + fmtInt(t.errors_5xx) + ' 5xx' + (t.error_rate_pct != null ? ' (' + t.error_rate_pct + '% 5xx)' : '')),
        kpiCard('Agent tokens in / out', fmtTokens(t.input_tokens) + ' / ' + fmtTokens(t.output_tokens),
          fmtInt(t.llm_calls) + ' LLM calls · ' + fmtInt(t.conversations) + ' conversations'),
        kpiCard('Model spend · project (est.)', usage ? fmtUsd(usage.est_cost_usd) : '—', usage
          ? fmtTokens(usage.input_tokens) + ' in / ' + fmtTokens(usage.output_tokens) + ' out · ' + fmtInt(usage.invocations) + ' calls'
            + (unrated ? ' · ' + unrated + ' model(s) without rate card' : '')
          : 'unavailable'),
        kpiCard('GE assistant calls · project', traffic ? fmtInt(traffic.assistant_requests) : '—', 'StreamAssist requests, ' + win)
      );

      const body = document.getElementById('fleetAgentsBody');
      body.replaceChildren();
      if (!fleet.agents.length) body.appendChild(emptyRow(10, 'No agents found on this Gemini Enterprise app.'));
      fleet.agents.forEach(function(a) {
        const m = a.metrics || {};
        const b = a.backend || {};
        let runsOn = 'Google-managed';
        let runsOnSub = '';
        if (b.kind === 'agent_engine') {
          runsOn = 'Agent Engine · ' + (b.display_name || b.reasoning_engine_id || '—');
          runsOnSub = [b.framework, b.location].filter(Boolean).join(' · ');
        } else if (b.kind === 'cloud_run') {
          runsOn = 'Cloud Run · ' + b.service;
          runsOnSub = b.region || '';
        } else if (b.kind === 'external_endpoint') {
          runsOn = 'External endpoint';
          runsOnSub = b.url || '';
        } else if (b.kind === 'gemini_enterprise_hosted') {
          runsOn = 'Gemini Enterprise hosted';
        } else if (b.kind === 'dialogflow') {
          runsOn = 'Dialogflow';
        }
        const scope = {agent: 'per-agent telemetry', service: 'service-level telemetry'}[a.telemetry_scope] || 'inventory only';
        const lastCell = el('td', 'mono', [m.last_activity ? fmtAgo(m.last_activity) : '—']);
        if (m.last_activity) lastCell.title = m.last_activity;
        body.appendChild(el('tr', a.telemetry_scope === 'none' ? 'fleet-row-muted' : null, [
          el('td', null, [
            el('div', 'fleet-agent-name', [a.display_name || a.agent_id]),
            a.description ? el('div', 'fleet-agent-desc', [a.description]) : null,
            el('div', 'fleet-agent-tags', [
              badge(a.state || 'UNKNOWN', a.state === 'ENABLED' ? 'badge-green' : 'badge-yellow'),
              a.sharing_scope ? badge(String(a.sharing_scope).replace(/_/g, ' ').toLowerCase(), 'badge-blue') : null,
            ]),
          ]),
          el('td', null, [badge(a.type_label || a.type, 'badge-blue')]),
          el('td', null, [el('div', null, [runsOn]), el('div', 'fleet-agent-desc', [[runsOnSub, scope].filter(Boolean).join(' · ')])]),
          el('td', 'mono', [fmtInt(m.requests)]),
          el('td', 'mono', [m.requests == null ? '—' : fmtInt(m.errors_4xx) + ' / ' + fmtInt(m.errors_5xx)]),
          el('td', 'mono', [m.latency_p50_ms == null && m.latency_p95_ms == null ? '—' : fmtMs(m.latency_p50_ms) + ' / ' + fmtMs(m.latency_p95_ms)]),
          el('td', 'mono', [fmtInt(m.llm_calls)]),
          el('td', 'mono', [m.input_tokens == null ? '—' : fmtTokens(m.input_tokens) + ' / ' + fmtTokens(m.output_tokens)]),
          el('td', 'mono', [fmtInt(m.conversations)]),
          lastCell,
        ]));
      });

      const errBox = document.getElementById('fleetErrors');
      errBox.replaceChildren();
      (fleet.errors || []).forEach(function(e) {
        errBox.appendChild(el('div', 'fleet-error', [el('strong', null, [String(e.source) + ': ']), String(e.detail || '')]));
      });
      errBox.classList.toggle('hidden', !(fleet.errors || []).length);

      const mu = fleet.model_usage;
      document.getElementById('modelScopeText').textContent = mu ? mu.scope + ' · ' + win : 'Model usage unavailable.';
      document.getElementById('modelCostBadge').textContent = mu && mu.totals ? 'Est. ' + fmtUsd(mu.totals.est_cost_usd) : '—';
      const mBody = document.getElementById('modelUsageBody');
      mBody.replaceChildren();
      ((mu && mu.models) || []).forEach(function(r) {
        mBody.appendChild(el('tr', null, [
          el('td', 'mono', [r.model]),
          el('td', 'mono', [fmtInt(r.invocations)]),
          el('td', 'mono', [fmtTokens(r.input_tokens)]),
          el('td', 'mono', [fmtTokens(r.output_tokens)]),
          el('td', 'mono', [fmtTokens(r.cache_read_tokens)]),
          el('td', 'mono', [fmtTokens(r.cache_write_tokens)]),
          el('td', 'mono', [r.cache_read_share_pct == null ? '—' : r.cache_read_share_pct + '%']),
          el('td', 'mono', [fmtUsd(r.est_cost_usd)]),
        ]));
      });
      if (mu && !(mu.models || []).length) mBody.appendChild(emptyRow(8, 'No Vertex AI model calls in this window.'));

      const notes = document.getElementById('fleetNotes');
      notes.replaceChildren();
      (fleet.notes || []).forEach(function(n) { notes.appendChild(el('li', null, [n])); });
      if (fleet.token_log_scan && fleet.token_log_scan.truncated) {
        notes.appendChild(el('li', null, ['Token log scan reached its cap (' + fleet.token_log_scan.entries_scanned
          + ' entries); agent token totals are lower bounds for this window.']));
      }
    }

    async function refreshFleet(force) {
      const btn = document.getElementById('fleetRefreshBtn');
      if (btn) { btn.disabled = true; btn.textContent = 'Refreshing…'; }
      try {
        let fleet = null;
        if (isEmbedded()) {
          // Inside Gemini Enterprise the dashboard is a sandboxed MCP App: the host proxies tools/call.
          const result = await callHost('tools/call', {
            name: FLEET_TOOL,
            arguments: {window_hours: fleetWindowHours, force_refresh: !!force},
          }, 45000);
          if (result && result.isError) throw new Error('tool error');
          fleet = result && result.structuredContent;
          if (fleet) fleetRefreshMode = 'host';
        } else {
          const url = '/api/ge_fleet?window_hours=' + encodeURIComponent(fleetWindowHours) + (force ? '&force_refresh=1' : '');
          const res = await fetch(url, {cache: 'no-store', credentials: 'same-origin'});
          if (!res.ok) throw new Error('HTTP ' + res.status);
          fleet = await res.json();
          fleetRefreshMode = 'http';
        }
        if (!fleet || !Array.isArray(fleet.agents)) throw new Error('no fleet data');
        setFleetNotice('');
        renderFleet(fleet);
      } catch (err) {
        fleetRefreshMode = 'snapshot';
        const when = lastFleet && lastFleet.generated_at ? ' from ' + new Date(lastFleet.generated_at).toLocaleTimeString() : '';
        setFleetNotice('Live refresh is not available from this view, so this is the snapshot' + when
          + '. Ask the assistant to open the VibeLift dashboard again for newer data.', true);
        if (lastFleet) renderFleet(lastFleet);
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Refresh now'; }
      }
    }

    function onFleetWindowChange() {
      const sel = document.getElementById('fleetWindow');
      fleetWindowHours = Number(sel.value) || 24;
      if (fleetRefreshMode === 'snapshot') fleetRefreshMode = 'pending';
      refreshFleet(false);
    }

    function scheduleFleetRefresh() {
      if (fleetTimer) { clearInterval(fleetTimer); fleetTimer = null; }
      const auto = document.getElementById('fleetAuto');
      if (!auto || !auto.checked) return;
      fleetTimer = setInterval(function() {
        if (document.hidden || fleetRefreshMode === 'snapshot') return;
        refreshFleet(false);
      }, FLEET_REFRESH_MS);
    }

    async function fetchState() {
      const res = await fetch('/api/state', {cache: 'no-store', credentials: 'same-origin'});
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      fleetRefreshMode = 'http';
      renderState(data);
    }

    async function syncGcpTelemetry() {
      const btn = document.getElementById('syncGcpBtn');
      const originalHtml = btn ? btn.innerHTML : '';
      if (btn) {
        btn.innerHTML = '&#x23F3; Syncing GCP Telemetry...';
        btn.disabled = true;
      }
      try {
        const res = await fetch('/api/sync_gcp_telemetry', {method: 'POST'});
        const data = await res.json();
        renderState(data.state || data);
      } catch (err) {
        console.error('Failed to sync GCP telemetry:', err);
      } finally {
        if (btn) {
          btn.innerHTML = originalHtml;
          btn.disabled = false;
        }
      }
    }

    async function triggerApi(endpoint, payload = {}) {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      renderState(data);
    }

    function onSelectAgent(agentId) {
      triggerApi('/api/select_agent', {agent_id: agentId});
    }

    function onAddCustomParameter() {
      const label = document.getElementById('newParamLabel').value.trim() || 'Custom Metric';
      const unit = document.getElementById('newParamUnit').value.trim() || '%';
      const rawDir = document.getElementById('newParamDirection').value;
      const direction = rawDir === 'lower_is_better' ? 'LOWER' : 'HIGHER';
      const baseline_val = parseFloat(document.getElementById('newParamBaseline').value) || 80.0;
      const target_val = parseFloat(document.getElementById('newParamTarget').value) || 95.0;
      const weight_pct = parseInt(document.getElementById('newParamWeight').value, 10) || 10;
      triggerApi('/api/add_parameter', {
        label,
        unit,
        direction,
        baseline_val,
        target_val,
        weight_pct,
      });
    }

    // ---------------------------------------------------------------------------
    // Gemini Enterprise AppBridge & Streamable MCP postMessage protocol
    // ---------------------------------------------------------------------------
    const pending = {};
    let seq = 0;
    let isBridgeInitialized = false;
    let currentDisplayMode = 'pip';

    function setDisplayMode(mode) {
      return callHost('ui/request-display-mode', { mode: mode }).then(function(res) {
        if (res && res.mode) {
          currentDisplayMode = res.mode;
          syncDisplayModeButton();
        }
      }).catch(function(err) {
        console.warn('Display mode request failed:', err);
      });
    }

    function toggleDisplayMode() {
      const target = currentDisplayMode === 'fullscreen' ? 'pip' : 'fullscreen';
      setDisplayMode(target);
    }

    function syncDisplayModeButton() {
      const btn = document.getElementById('btnModeFullscreen');
      if (!btn) return;
      if (window.parent && window.parent !== window) {
        btn.style.display = 'inline-flex';
      }
      if (currentDisplayMode === 'fullscreen') {
        btn.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z"/></svg> Side Panel';
        btn.title = 'Return to side panel';
      } else {
        btn.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg> Fullscreen';
        btn.title = 'Expand to fullscreen';
      }
    }

    function post(msg) {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage(msg, '*');
        return;
      }
      // Standalone mode: simulated local handling
      if (msg.method === 'ui/initialize' || msg.method === 'initialize') {
        setTimeout(function() {
          if (msg.id && pending[msg.id]) {
            const p = pending[msg.id];
            delete pending[msg.id];
            p.resolve({ protocolVersion: '2025-06-18', capabilities: {} });
          }
        }, 10);
        return;
      }
      if (msg.method === 'ui/request-display-mode') {
        const mode = (msg.params && msg.params.mode) || 'pip';
        setTimeout(function() {
          if (msg.id && pending[msg.id]) {
            const p = pending[msg.id];
            delete pending[msg.id];
            p.resolve({ mode: mode });
          }
        }, 10);
        return;
      }
    }

    function callHost(method, params, timeoutMs) {
      return new Promise(function(resolve, reject) {
        const id = 'vl_' + (++seq);
        const timerId = setTimeout(function() {
          if (pending[id]) {
            delete pending[id];
            reject(new Error('Timeout waiting for ' + method));
          }
        }, timeoutMs || 10000);

        pending[id] = {
          resolve: function(val) { clearTimeout(timerId); resolve(val); },
          reject: function(err) { clearTimeout(timerId); reject(err); },
        };
        post({ jsonrpc: '2.0', id: id, method: method, params: params || {} });
      });
    }

    window.addEventListener('message', function(event) {
      const data = event.data;
      if (!data || typeof data !== 'object') return;

      if (data.id && pending[data.id]) {
        const p = pending[data.id];
        delete pending[data.id];
        if (data.error) {
          p.reject(new Error(data.error.message || 'Call failed'));
        } else {
          p.resolve(data.result);
        }
      }

      if (data.method === 'ui/notifications/initialized' || data.method === 'notifications/initialized') {
        isBridgeInitialized = true;
      }

      if (data.method === 'ui/notifications/host-context-changed' && data.params) {
        if (data.params.displayMode) {
          currentDisplayMode = data.params.displayMode;
          syncDisplayModeButton();
        }
      }

      if (data.method === 'ui/notifications/tool-result' && data.params) {
        const structured = data.params.structuredContent || data.params;
        if (structured && structured.state) {
          renderState(structured.state);
          if (structured.focus_tab != null) switchTab(structured.focus_tab);
        } else if (structured && structured.active_agent) {
          renderState(structured);
        } else if (structured && structured.source === 'gemini_enterprise') {
          renderFleet(structured);
        }
      }
    });

    function emitAppInitialized() {
      post({ jsonrpc: '2.0', method: 'ui/notifications/initialized', params: {} });
      post({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} });
    }

    // 1. Instantly notify host that UI is ready
    emitAppInitialized();

    // 2. Perform ui/initialize handshake per MCP UI specification
    callHost('ui/initialize', {
      protocolVersion: '2025-06-18',
      capabilities: {},
      clientInfo: { name: 'vibelift-analytics-dashboard', version: '1.0.0' },
    }, 2000).catch(function() {
      return callHost('initialize', {
        protocolVersion: '2025-06-18',
        capabilities: {},
        clientInfo: { name: 'vibelift-analytics-dashboard', version: '1.0.0' },
      }, 1000).catch(function() { return {}; });
    }).then(function() {
      emitAppInitialized();
      syncDisplayModeButton();
    }).catch(function(err) {
      console.warn('AppBridge handshake notice:', err);
      emitAppInitialized();
    });

    setTimeout(emitAppInitialized, 150);

    // Keepalive ping for Gemini Enterprise iframe host (every 45s)
    setInterval(function() {
      if (window.parent && window.parent !== window) {
        callHost('ping', {}, 2000).catch(function() {});
      }
    }, 45000);

    // Standalone (direct Cloud Run URL): load over the same-origin API. Embedded in Gemini
    // Enterprise: data arrives in the open_dashboard tool result; refreshes use the host bridge.
    if (!isEmbedded()) {
      fetchState().catch(function() {
        setFleetNotice('Could not load dashboard data from the VibeLift API.', true);
      });
    }
    scheduleFleetRefresh();
  </script>
</body>
</html>
"""


def render_dashboard_html() -> str:
  """Returns the self-contained Google Cloud 2-tab HTML UI."""
  return _DASHBOARD_HTML.replace(
      '__VIBELIFT_GOOGLEY_LOGO_DATA_URI__',
      logo_asset.VIBELIFT_GOOGLEY_LOGO_DATA_URI,
  )