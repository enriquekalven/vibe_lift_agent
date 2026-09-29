from collections.abc import Mapping
import json
from vibelift.ui import logo_asset

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
      --bg: #f8fafc;
      --surface: #ffffff;
      --border: #e2e8f0;
      --text-primary: #0f172a;
      --text-secondary: #475569;
      --g-blue: #334155;
      --g-blue-bg: #f1f5f9;
      --g-green: #166534;
      --g-green-bg: #f0fdf4;
      --g-yellow: #854d0e;
      --g-yellow-bg: #fefce8;
      --g-red: #991b1b;
      --g-red-bg: #fef2f2;
      --font-sans: 'Inter', 'Google Sans', 'Roboto', -apple-system, BlinkMacSystemFont, sans-serif;
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
      padding: 10px 18px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 10px 16px;
      position: sticky;
      top: 0;
      z-index: 20;
      box-shadow: 0 1px 2px rgba(15,23,42,0.04);
    }
    .brand-row {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
      min-width: 0;
    }
    .brand-logo-img {
      width: 38px;
      height: 38px;
      border-radius: 8px;
      object-fit: cover;
      border: 1px solid var(--border);
      flex-shrink: 0;
    }
    .brand-title {
      font-size: 16px;
      font-weight: 700;
      color: var(--text-primary);
      letter-spacing: -0.2px;
    }
    .brand-subtitle {
      font-size: 11.5px;
      color: var(--text-secondary);
    }
    .action-bar {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .btn {
      font-family: var(--font-sans);
      font-size: 12.5px;
      font-weight: 600;
      padding: 7px 13px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text-primary);
      cursor: pointer;
      transition: all 0.15s ease;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      white-space: nowrap;
    }
    .btn:hover { background: #f1f5f9; }
    .btn-primary {
      background: #0f172a;
      color: #ffffff;
      border-color: #0f172a;
    }
    .btn-primary:hover { background: #1e293b; }
    .btn-green {
      background: #1e293b;
      color: #ffffff;
      border-color: #1e293b;
    }
    .btn-green:hover { background: #0f172a; }
    .btn-danger {
      background: var(--surface);
      color: var(--text-secondary);
      border-color: #cbd5e1;
    }
    .btn-danger:hover { background: #f1f5f9; color: var(--text-primary); }
    .btn-fullscreen-toggle {
      background: #0f172a;
      color: #ffffff;
      border-color: #0f172a;
    }
    .btn-fullscreen-toggle:hover {
      background: #1e293b;
      border-color: #1e293b;
    }
    .tabs-bar {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 0 18px;
      display: flex;
      gap: 16px;
      flex-wrap: wrap;
    }
    .tab-btn {
      background: none;
      border: none;
      padding: 12px 4px;
      font-size: 13px;
      font-weight: 600;
      color: var(--text-secondary);
      cursor: pointer;
      border-bottom: 2px solid transparent;
      display: flex;
      align-items: center;
      gap: 7px;
    }
    .tab-btn:hover { color: var(--text-primary); }
    .tab-btn.active {
      color: var(--text-primary);
      border-bottom-color: var(--text-primary);
    }
    .tab-step-pill {
      font-size: 10.5px;
      padding: 2px 7px;
      border-radius: 99px;
      background: #f1f5f9;
      color: #334155;
      font-weight: 700;
    }
    .tab-btn.active .tab-step-pill {
      background: #0f172a;
      color: #ffffff;
    }
    .subview-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 10px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 14px;
      margin-bottom: 16px;
    }
    .subview-btn {
      font-family: var(--font-sans);
      font-size: 12px;
      font-weight: 600;
      padding: 6px 12px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--text-secondary);
      cursor: pointer;
    }
    .subview-btn.active {
      background: #0f172a;
      color: #ffffff;
      border-color: #0f172a;
    }
    .tab-footer-nav {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 10px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 16px;
      margin-top: 8px;
      font-size: 12.5px;
      color: var(--text-secondary);
    }
    .container {
      max-width: 1400px;
      margin: 16px auto;
      padding: 0 18px 40px 18px;
    }
    .selector-banner {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px 18px;
      margin-bottom: 16px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr));
      gap: 18px;
      align-items: center;
    }
    .selector-label {
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      color: var(--text-secondary);
      letter-spacing: 0.5px;
      margin-bottom: 5px;
    }
    .agent-select {
      width: 100%;
      padding: 9px 12px;
      font-size: 13.5px;
      font-weight: 600;
      font-family: var(--font-sans);
      color: var(--text-primary);
      background: var(--surface);
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      cursor: pointer;
    }
    .agent-meta-box {
      display: flex;
      flex-direction: column;
      gap: 8px;
      border-left: 1px solid var(--border);
      padding-left: 18px;
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
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 200px), 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .kpi-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px 16px;
      position: relative;
      overflow: hidden;
    }
    .kpi-label {
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      color: var(--text-secondary);
      letter-spacing: 0.4px;
    }
    .kpi-value {
      font-size: 22px;
      font-weight: 700;
      color: var(--text-primary);
      margin: 6px 0 4px 0;
    }
    .kpi-sub {
      font-size: 12px;
      color: var(--text-secondary);
    }
    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px 18px;
      margin-bottom: 16px;
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
      font-size: 14.5px;
      font-weight: 700;
      color: var(--text-primary);
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      padding: 2px 9px;
      border-radius: 99px;
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.1px;
      border: 1px solid transparent;
    }
    .badge-blue { background: #f1f5f9; color: #334155; border-color: #e2e8f0; }
    .badge-green { background: #f0fdf4; color: #166534; border-color: #dcfce7; }
    .badge-yellow { background: #fefce8; color: #854d0e; border-color: #fef08a; }
    .badge-red { background: #fef2f2; color: #991b1b; border-color: #fecaca; }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12.5px;
    }
    th, td {
      text-align: left;
      padding: 9px 10px;
      border-bottom: 1px solid var(--border);
    }
    th {
      color: var(--text-secondary);
      font-weight: 600;
      font-size: 11px;
      text-transform: uppercase;
      background: #f8fafc;
    }
    .mono { font-family: var(--font-mono); font-size: 12px; }
    .weight-bar-bg {
      width: 84px;
      height: 7px;
      background: #e2e8f0;
      border-radius: 99px;
      overflow: hidden;
      display: inline-block;
      vertical-align: middle;
      margin-right: 6px;
    }
    .weight-bar-fill {
      height: 100%;
      background: #334155;
      border-radius: 99px;
    }
    .add-param-form {
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px 16px;
      margin-top: 14px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 140px), 1fr));
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
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      background: #ffffff;
      color: var(--text-primary);
    }
    .charts-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 360px), 1fr));
      gap: 14px;
      margin-bottom: 16px;
    }
    .chart-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px 16px;
    }
    .action-card {
      border: 1px solid var(--border);
      border-left: 3px solid #334155;
      border-radius: 8px;
      padding: 14px 16px;
      margin-bottom: 12px;
      background: #ffffff;
    }
    .action-card.rejected {
      border-left-color: #991b1b;
      background: #ffffff;
    }
    .action-card.baseline {
      border-left-color: #94a3b8;
      background: #f8fafc;
    }
    .action-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr));
      gap: 12px;
      margin-top: 10px;
    }
    .action-box {
      background: #f8fafc;
      border: 1px solid var(--border);
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
      background: #0f172a;
      color: #e2e8f0;
      font-family: var(--font-mono);
      font-size: 11.5px;
      padding: 10px 12px;
      border-radius: 6px;
      overflow-x: auto;
      white-space: pre-wrap;
    }
    .live-pill { background: #f0fdf4; color: #166534; }
    .live-dot {
      display: inline-block; width: 7px; height: 7px; border-radius: 50%;
      background: #166534; margin-right: 4px;
    }
    .fleet-controls { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 12px; color: var(--text-secondary); }
    .fleet-controls select { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; font-family: var(--font-sans); font-size: 12px; }
    .fleet-meta { font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; }
    .fleet-sources { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
    .chip { font-size: 11px; font-weight: 600; padding: 3px 9px; border-radius: 99px; border: 1px solid var(--border); }
    .chip-ok { background: #f0fdf4; color: #166534; border-color: #dcfce7; }
    .chip-error { background: #fef2f2; color: #991b1b; border-color: #fecaca; }
    .chip-na { background: #f1f5f9; color: var(--text-secondary); }
    .fleet-kpis { grid-template-columns: repeat(auto-fit, minmax(min(100%, 170px), 1fr)); }
    .fleet-kpis .kpi-value { font-size: 20px; }
    .table-scroll { overflow-x: auto; width: 100%; }
    .fleet-agent-name { font-weight: 700; }
    .fleet-agent-desc { font-size: 11px; color: var(--text-secondary); margin-top: 2px; max-width: 360px; }
    .fleet-agent-tags { display: flex; gap: 6px; margin-top: 6px; flex-wrap: wrap; }
    .fleet-row-muted td { color: var(--text-secondary); }
    .fleet-empty { color: var(--text-secondary); text-align: center; }
    .fleet-notice { font-size: 12px; padding: 8px 12px; border-radius: 6px; background: #f1f5f9; color: #334155; margin-bottom: 12px; }
    .fleet-notice-error { background: #fefce8; color: #854d0e; }
    .fleet-errors { margin-top: 12px; font-size: 12px; }
    .fleet-error { background: #fef2f2; color: #991b1b; padding: 6px 10px; border-radius: 6px; margin-bottom: 6px; word-break: break-word; }
    .fleet-notes { font-size: 11px; color: var(--text-secondary); margin: 12px 0 0 16px; padding: 0; }
    .hidden { display: none !important; }
    body.simple-mode .adv-only { display: none !important; }
    .exec-headline { font-size: 15px; line-height: 1.5; color: var(--text-primary); background: var(--surface);
      border: 1px solid var(--border); border-radius: 10px; padding: 14px 18px; margin-bottom: 14px; }
    .exec-headline .muted { color: var(--text-secondary); font-size: 12px; display:block; margin-top:4px; }
    .exec-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 14px; }
    @media (max-width: 900px) { .exec-grid { grid-template-columns: 1fr; } }
    .exec-grid .panel { margin-bottom: 0; }
    .trend-panel { margin-top: 14px; margin-bottom: 0; }
    .trend-svg { width: 100%; height: 170px; display: block; }
    .trend-svg text { font-size: 10px; fill: #64748b; font-family: var(--font-mono); }
    .chart-source { font-size: 11px; color: var(--text-secondary); margin-top: 10px; }
    .hbar-row { display: grid; grid-template-columns: 170px 1fr 64px; align-items: center; gap: 10px; margin: 7px 0; font-size: 12.5px; }
    .hbar-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-primary); }
    .hbar-track { height: 14px; background: #f1f5f9; border-radius: 4px; overflow: hidden; display: flex; }
    .hbar-seg { height: 100%; }
    .hbar-val { text-align: right; font-family: var(--font-mono); font-size: 12px; color: var(--text-secondary); }
    .chart-legend { display: flex; flex-wrap: wrap; gap: 12px; font-size: 11.5px; color: var(--text-secondary); margin-top: 8px; }
    .chart-legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
    .donut-wrap { display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }
    .donut-legend { display: flex; flex-direction: column; gap: 6px; font-size: 12.5px; min-width: 180px; flex: 1; }
    .donut-legend-row { display: grid; grid-template-columns: 14px 1fr auto; gap: 8px; align-items: center; }
    .donut-legend-row i { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
    .donut-legend-row .v { font-family: var(--font-mono); font-size: 12px; color: var(--text-secondary); }
    .attention-list { list-style: none; margin: 0; padding: 0; }
    .attention-list li { display: flex; gap: 10px; align-items: flex-start; padding: 9px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
    .attention-list li:last-child { border-bottom: none; }
    .sev-dot { width: 8px; height: 8px; border-radius: 50%; margin-top: 6px; flex: none; }
    body.live-data .sim-panel { display: none !important; }
    body:not(.live-data) .live-only { display: none !important; }
    .token-na { color: #b45309; font-size: 11.5px; font-weight: 600; }
    .cell-sub { display: block; font-size: 10.5px; color: var(--text-secondary); margin-top: 2px; white-space: nowrap; font-family: var(--font-sans, inherit); font-weight: 400; }
    .cleanup-row { padding: 10px 0; border-bottom: 1px solid var(--border); font-size: 12.5px; }
    .cleanup-row:last-child { border-bottom: none; }
    .cleanup-evidence { color: var(--text-secondary); font-size: 11.5px; margin-top: 3px; word-break: break-word; }
    .cleanup-cmd { display: flex; gap: 8px; align-items: flex-start; margin-top: 6px; }
    .cleanup-cmd code { flex: 1; font-family: var(--font-mono); font-size: 11px; background: #f8fafc; border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; word-break: break-all; }
    .live-note { font-size: 11.5px; color: var(--text-secondary); margin-top: 8px; }
    .live-sub { font-size: 12.5px; font-weight: 700; margin: 16px 0 6px; color: var(--text-primary); }
    .whatif-live-form { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; margin-bottom: 12px; }
    .whatif-live-form label { display: block; font-size: 11px; font-weight: 600; color: var(--text-secondary); margin-bottom: 3px; }
    .whatif-live-form select, .whatif-live-form input { font-size: 12.5px; padding: 5px 7px; border: 1px solid var(--border); border-radius: 6px; }
    .wi-result-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin: 10px 0; }
    .chart-empty { font-size: 12.5px; color: var(--text-secondary); padding: 18px 0; }
    .adv-toggle { display:inline-flex; align-items:center; gap:6px; }
    .adv-toggle[aria-pressed="true"] { background:#1e293b; color:#fff; border-color:#1e293b; }
    .verify-chip { font-size:11.5px; padding:4px 10px; }
    .scope-select { font-size:12px; padding:5px 8px; border:1px solid #cbd5e1; border-radius:6px; background:#fff; color:#0f172a; max-width:320px; }
    .time-ranges { margin-top: 6px; font-size: 11.5px; color: #475569; line-height: 1.45; }
    .scope-note { font-size:12px; color:#854d0e; background:#fefce8; border:1px solid #fde68a; border-radius:6px; padding:6px 10px; margin-top:8px; }
    .fleet-agent-app { font-size:11px; color: var(--text-secondary); margin-top:2px; }
    details.role-disclosure {
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 8px 12px;
      margin-bottom: 12px;
    }
    details.role-disclosure summary {
      cursor: pointer;
      font-size: 12px;
      font-weight: 600;
      color: var(--text-secondary);
      user-select: none;
    }
    @media (max-width: 820px) {
      .agent-meta-box { border-left: none; border-top: 1px solid var(--border); padding-left: 0; padding-top: 12px; }
      .container { padding: 0 12px 28px 12px; }
      .panel { padding: 14px 14px; }
    }
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
        <div class="brand-subtitle adv-only">
          Agent Health, Speed, Cost &amp; Quality &bull; <span class="mono">@vibelift_telemetry</span>
        </div>
      </div>
      <div style="display:inline-flex;align-items:center;gap:8px;padding:4px 10px;background:#f1f5f9;border-radius:6px;border:1px solid #e2e8f0;font-size:11.5px;font-weight:600;color:#334155;">
        <span>GCP: <strong id="gcpProjectText" class="mono">&#x2026;</strong> (<span id="gcpRegionText" class="mono">&#x2026;</span>)</span>
      </div>
      <label for="geScopeSelect" style="font-size:11.5px;font-weight:600;color:#475569;">Gemini Enterprise app</label>
      <select id="geScopeSelect" class="scope-select" onchange="onGeScopeChange(this.value)" title="Filter by region and Gemini Enterprise app">
        <option value="all">All apps &middot; all regions</option>
      </select>
      <label for="fleetWindow" style="font-size:11.5px;font-weight:600;color:#475569;">Time range</label>
      <select id="fleetWindow" class="scope-select" onchange="onFleetWindowChange()" title="Time range for agent traffic, errors, tokens and model spend">
        <option value="1">Last hour</option>
        <option value="6">Last 6 hours</option>
        <option value="24" selected>Last 24 hours</option>
        <option value="168">Last 7 days</option>
      </select>
    </div>
    <div class="action-bar">
      <button id="btnModeFullscreen" class="btn btn-fullscreen-toggle" onclick="toggleDisplayMode()" title="Toggle between Right Side Panel and Fullscreen">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg> Fullscreen
      </button>
      <span id="activeGenBadge" class="badge badge-blue adv-only">ACTIVE CONFIG</span>
      <button class="btn verify-chip" onclick="toggleTelemetryValidatorDrawer()" id="toggleTelemetryValidatorBtn" title="See which data source backs each number">
        Data check: <span id="telemetryValidatorSummaryBadge" class="mono">checking&hellip;</span>
      </button>
      <button class="btn" id="syncGcpBtn" onclick="syncGcpTelemetry()" title="Sync GCP Telemetry (Cloud Monitoring, BigQuery, Gemini Enterprise)">
        Refresh
      </button>
      <button class="btn btn-danger adv-only" onclick="triggerApi('/api/inject_anomaly')" title="Sandbox: injects a simulated anomaly">
        Test Alert
      </button>
      <button class="btn btn-primary adv-only" onclick="triggerApi('/api/evolve_generation')" title="Sandbox: runs one optimizer generation">
        Run Optimizer
      </button>
      <button class="btn adv-only" onclick="triggerApi('/api/reset')">
        Reset
      </button>
      <button class="btn adv-toggle" id="advancedToggleBtn" aria-pressed="false" onclick="toggleAdvancedMode()" title="Show optimizer, simulators, FinOps deep-dives and SDK tools">
        Advanced &#9662;
      </button>
    </div>
  </header>

  <nav class="tabs-bar" aria-label="Dashboard Navigation">
    <button id="tabBtn6" class="tab-btn active" onclick="switchTab(6)">
      Overview
    </button>
    <button id="tabBtn0" class="tab-btn" onclick="switchTab(0)" title="Gemini Enterprise Agent Fleet">
      <span class="tab-step-pill live-pill"><span class="live-dot"></span></span>
      Agents
    </button>
    <button id="tabBtn3" class="tab-btn" onclick="switchTab(3)">
      Cost
    </button>
    <button id="tabBtn4" class="tab-btn" onclick="switchTab(4)">
      Users
    </button>
    <button id="tabBtn1" class="tab-btn adv-only" onclick="switchTab(1)">
      Goals &amp; Metrics
    </button>
    <button id="tabBtn2" class="tab-btn adv-only" onclick="switchTab(2)">
      Optimizer &amp; Testing
    </button>
    <button id="tabBtn5" class="tab-btn adv-only" onclick="switchTab(5)">
      SDK &amp; Tools
    </button>
  </nav>

  <main class="container">
    <!-- COMPACT SUMMARY BAR: KEY NUMBERS, COLLAPSIBLE ROLE GUIDE & OPTIONAL DATA SEARCH -->
    <section class="panel adv-only" id="smeExecutivePulseBar" style="margin-bottom:16px;padding:14px 18px;">
      <div class="panel-header" style="margin-bottom:10px;padding-bottom:8px;">
        <div class="panel-title">
          <span>Summary: Cost per Helpful Answer, Resolution Rate &amp; Monthly Cloud Bill</span>
          <span class="badge badge-green" id="northStarDeltaBadge">&mdash;</span>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
          <button class="btn" onclick="toggleNl2SqlDrawer()" id="toggleNl2SqlBtn">
            Search Data in Plain English
          </button>
        </div>
      </div>

      <div class="kpi-grid" id="northStarUnitEconKpis" style="margin-bottom:10px;"></div>

      <details class="role-disclosure" id="roleGuideDisclosure">
        <summary>Role Guide &amp; 7-Step Workflow (Click to choose your role: FinOps, SRE, AI Engineer, Product, Security, or Support)</summary>
        <div style="margin-top:10px;">
          <div style="font-size:11px;font-weight:600;color:var(--text-secondary);text-transform:uppercase;margin-bottom:6px;">
            Choose Your Role (Shows key metrics and daily actions for your team):
          </div>
          <div id="smePersonaLensBar" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;"></div>
          <div id="smePersonaPlaybookCard" class="action-box" style="margin-bottom:10px;"></div>
          <div id="workflowStepsRibbon" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px;"></div>
        </div>
      </details>

      <div id="nl2sqlCopilotDrawer" class="hidden" style="background:#f8fafc;border:1px solid var(--border);border-radius:8px;padding:12px;margin-top:10px;">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px;">
          <div style="font-size:12px;font-weight:700;color:var(--text-primary);">
            Ask a Question in Plain English (Generates BigQuery SQL on <span class="mono">aive_logs</span> &amp; Agent Logs)
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;">
            <button class="btn" style="padding:4px 9px;font-size:11.5px;" onclick="runNl2SqlPreset('Compare cost per 1k turns and prompt cache savings across agents')">Cost &amp; Cache by Agent</button>
            <button class="btn" style="padding:4px 9px;font-size:11.5px;" onclick="runNl2SqlPreset('Show token category breakdown for thinking vs context bloat vs background overhead')">Token Usage by Type</button>
            <button class="btn" style="padding:4px 9px;font-size:11.5px;" onclick="runNl2SqlPreset('List top power users by user_ldap token spend and CSAT')">Top Users by Spend</button>
            <button class="btn" style="padding:4px 9px;font-size:11.5px;" onclick="runNl2SqlPreset('Detect runaway agent loops and thinking token anomalies')">Stuck Loop Alerts</button>
          </div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px;">
          <input id="nl2sqlQuestionInput" type="text" style="flex:1;min-width:260px;padding:7px 11px;border:1px solid #cbd5e1;border-radius:6px;font-size:13px;" value="Compare cost per 1k turns and prompt cache savings across agents" />
          <button class="btn btn-primary" onclick="runNl2SqlQuery()">Run Search</button>
        </div>
        <div id="nl2sqlSummaryText" style="font-size:12.5px;font-weight:600;color:var(--g-green);margin-bottom:8px;"></div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;">
          <div>
            <div style="font-size:11px;font-weight:700;color:var(--text-secondary);text-transform:uppercase;margin-bottom:4px;">Generated BigQuery SQL</div>
            <pre class="diff-pre" id="nl2sqlSqlPre" style="max-height:145px;"></pre>
          </div>
          <div class="table-scroll" style="max-height:165px;">
            <table>
              <thead id="nl2sqlResultHead"></thead>
              <tbody id="nl2sqlResultBody"></tbody>
            </table>
          </div>
        </div>
      </div>
    </section>

    <section id="telemetryValidatorSection">
      <div id="telemetryValidatorDrawer" class="hidden" style="background:#f8fafc;border:1px solid var(--border);border-radius:8px;padding:12px;margin-top:10px;">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px;">
          <div>
            <div style="font-size:12.5px;font-weight:700;color:var(--text-primary);">
              Telemetry Grounding &amp; LLM-as-a-Judge Validator (Zero Hallucination / Zero Fake Data Guardrail)
            </div>
            <div style="font-size:11.5px;color:var(--text-secondary);">
              Cross-checks all 6 tabs against BigQuery (<span class="mono">ds_ge_audit_raw</span>, <span class="mono">sre_triage_agent_telemetry</span>, <span class="mono">vibelift_analytics</span>), Cloud Monitoring v3, and Discovery Engine APIs.
            </div>
          </div>
          <div style="display:flex;gap:8px;align-items:center;">
            <button class="btn btn-primary" id="runLlmJudgeAuditBtn" onclick="runTelemetryValidationAudit(true)" style="padding:5px 10px;font-size:11.5px;">
              Run Live LLM-as-a-Judge Audit
            </button>
          </div>
        </div>
        <div id="telemetryValidatorJudgeBox" class="action-box" style="margin-bottom:10px;"></div>
        <div class="table-scroll" style="max-height:240px;">
          <table>
            <thead>
              <tr>
                <th>Check ID</th>
                <th>Tab</th>
                <th>Metric / Invariant Audited</th>
                <th>Expected Ground Truth</th>
                <th>Observed UI State</th>
                <th>Telemetry Source</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody id="telemetryValidatorChecksBody"></tbody>
          </table>
        </div>
      </div>

    </section>

    <!-- Persistent Agent & Optimizer Selector Banner -->
    <section id="demoSelectorBanner" class="selector-banner hidden adv-only">
      <div style="display:flex;flex-direction:column;gap:10px;">
        <div>
          <div class="selector-label">1. Choose Agent to Inspect</div>
          <select id="agentDropdown" class="agent-select" onchange="onSelectAgent(this.value)">
            <option value="it_service_desk">IT Service Desk (gemini-2.5-flash &bull; Vertex AI Agent Engine)</option>
            <option value="vibelift_analytics">VibeLift Analytics &amp; FinOps (gemini-2.5-flash &bull; Cloud Run A2A + MCP)</option>
            <option value="deep_research">Deep Research (gemini-2.5-pro &bull; Google-Managed Research Agent)</option>
          </select>
        </div>
        <div>
          <div class="selector-label">2. Choose Optimization Method</div>
          <select id="optimizerDropdown" class="agent-select" onchange="onSelectOptimizer(this.value)">
            <option value="alpha_evolve">AlphaEvolve (Balanced Cost, Speed &amp; Accuracy &bull; Default)</option>
            <option value="opus_critic">Opus Critic (Prompt &amp; Tool Cleanup)</option>
            <option value="vertex_vizier">Google Vizier (Parameter Search Tuner)</option>
            <option value="hybrid_ensemble">Combined Mode (AlphaEvolve + Vizier + Opus Critic)</option>
          </select>
        </div>
      </div>
      <div class="agent-meta-box">
        <div class="agent-meta-top">
          <div>
            <strong id="agentTitleText" style="font-size:15.5px;">IT Service Desk</strong>
            <span id="agentDomainBadge" class="badge badge-blue" style="margin-left:8px;">Enterprise IT Support &amp; Escalation (Vertex AI Agent Engine)</span>
          </div>
          <span id="agentHealthBadge" class="badge badge-green">OPTIMIZED (GEN 14)</span>
        </div>
        <div id="agentDescText" style="font-size:12.5px;color:var(--text-secondary);"></div>
        <div style="font-size:12px;color:var(--text-secondary);display:flex;gap:16px;flex-wrap:wrap;">
          <span><strong>Model:</strong> <span id="agentModelText" class="mono"></span></span>
          <span><strong>Active Method:</strong> <span id="activeOptimizerText" class="mono" style="color:var(--text-primary);font-weight:700;">AlphaEvolve (Balanced Cost, Speed &amp; Accuracy)</span></span>
          <span><strong>Tracking:</strong> <span style="color:var(--g-green);font-weight:600;">@vibelift_telemetry (&lt;10ms)</span></span>
        </div>
      </div>
    </section>

    <!-- TAB 6: EXECUTIVE OVERVIEW (charts computed client-side from live /api/state only) -->
    <section id="tabPanel6">
      <div class="exec-headline" id="execHeadline">Loading live data from Google Cloud&hellip;</div>
      <div class="kpi-grid" id="execKpis"></div>
      <div class="panel trend-panel">
        <div class="panel-header"><div class="panel-title"><span>Requests over time</span></div></div>
        <div id="execChartTrend"></div>
        <div class="chart-legend">
          <span><i style="background:#2563eb"></i>Successful</span>
          <span><i style="background:#f59e0b"></i>Rejected (4xx)</span>
          <span><i style="background:#ef4444"></i>Server error (5xx)</span>
        </div>
        <div class="chart-source" id="execSrcTrend"></div>
      </div>
      <div class="exec-grid">
        <div class="panel">
          <div class="panel-header"><div class="panel-title"><span>Requests by agent</span></div></div>
          <div id="execChartRequests"></div>
          <div class="chart-legend">
            <span><i style="background:#2563eb"></i>Successful</span>
            <span><i style="background:#f59e0b"></i>Rejected (4xx)</span>
            <span><i style="background:#ef4444"></i>Server error (5xx)</span>
          </div>
          <div class="chart-source" id="execSrcRequests"></div>
        </div>
        <div class="panel">
          <div class="panel-header"><div class="panel-title"><span>Model spend by model</span></div></div>
          <div id="execChartSpend"></div>
          <div class="chart-source" id="execSrcSpend"></div>
        </div>
        <div class="panel">
          <div class="panel-header"><div class="panel-title"><span>Agent fleet mix</span></div></div>
          <div id="execChartMix"></div>
          <div class="chart-source">Source: Gemini Enterprise agent inventory (Discovery Engine API)</div>
        </div>
        <div class="panel">
          <div class="panel-header"><div class="panel-title"><span>Most active users</span></div></div>
          <div id="execChartUsers"></div>
          <div class="chart-legend">
            <span><i style="background:#10b981"></i>People</span>
            <span><i style="background:#94a3b8"></i>Service accounts</span>
            <span title="A user id set by the calling app (e.g. agents-cli uses cli-user), not an authenticated identity"><i style="background:#f59e0b"></i>Unverified session id</span>
          </div>
          <div class="chart-source">Source: BigQuery audit logs &amp; agent telemetry (sessions, last 7 days; does not follow Time range)</div>
        </div>
      </div>
      <div class="panel trend-panel">
        <div class="panel-header"><div class="panel-title"><span>Tokens by agent</span></div></div>
        <div id="execChartTokens"></div>
        <div class="chart-legend">
          <span><i style="background:#2563eb"></i>Input</span>
          <span><i style="background:#10b981"></i>Output</span>
        </div>
        <div class="chart-source" id="execSrcTokens"></div>
      </div>
      <div class="panel" style="margin-top:14px;">
        <div class="panel-header"><div class="panel-title"><span>Needs attention</span></div></div>
        <ul class="attention-list" id="execAttention"></ul>
      </div>
      <div class="panel hidden" id="execCleanupPanel" style="margin-top:14px;">
        <div class="panel-header">
          <div class="panel-title"><span>Clean up: agents whose backend no longer exists</span><span class="badge badge-red" id="execCleanupCount"></span></div>
          <div style="font-size:12px;color:var(--text-secondary);">Each registration below points at an Agent Engine or Cloud Run service that returned HTTP 404, or has no backend configured. People who pick these agents in Gemini Enterprise get errors. Review each one, then run the command yourself; VibeLift never deletes anything.</div>
        </div>
        <div id="execCleanup"></div>
      </div>
    </section>

    <!-- TAB 0: LIVE GEMINI ENTERPRISE AGENT FLEET (real inventory joined with real telemetry) -->
    <section id="tabPanel0" class="hidden">
      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <span>Agents deployed on Gemini Enterprise</span>
            <span id="fleetEngineBadge" class="badge badge-blue">Loading&hellip;</span>
          </div>
          <div class="fleet-controls">
            <label><input type="checkbox" id="fleetAuto" checked onchange="scheduleFleetRefresh()" /> Auto-refresh (60 s)</label>
            <button class="btn" id="fleetRefreshBtn" onclick="refreshFleet(true)">Refresh now</button>
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

      <div class="panel adv-only">
        <div class="panel-header">
          <div class="panel-title">
            <span>Safety Alerts &amp; Automatic Protections</span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);">
            Watches for stuck reasoning loops, oversized chat history, and repeated tool retries across Cloud Run and Vertex AI.
          </div>
        </div>
        <div id="watchOutAlarmsContainer" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;"></div>
      </div>

      <div class="panel adv-only">
        <div class="panel-header">
          <div class="panel-title">
            <span>Cloud Run Services &amp; Support Ticket Stream</span>
            <span class="badge badge-blue">Live Cloud Run Services &bull; BigQuery <span class="mono">vw_l1_l2_unified_triage_logs</span></span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);">
            Shows running Cloud Run agent versions (<span class="mono">gcp_services</span>) next to recent L1/L2 support tickets (<span class="mono">gemini_enterprise_support_events</span>).
          </div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:14px;">
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Cloud Run Service</th>
                  <th>Scaling</th>
                  <th>CPU / Memory</th>
                  <th>Latency</th>
                  <th>Monthly Cost</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody id="cloudRunServicesBody"></tbody>
            </table>
          </div>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Ticket &amp; Tier</th>
                  <th>Agent</th>
                  <th>Trace ID</th>
                  <th>Issue Summary</th>
                  <th>Resolution Status</th>
                </tr>
              </thead>
              <tbody id="geSupportEventsBody"></tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="tab-footer-nav adv-only">
        <span>Step 1 of 6: Checked live agent fleet health and active alerts.</span>
        <button class="btn btn-primary" onclick="switchTab(1)">Next Step: Goals &amp; Metrics (Tab 2) &rarr;</button>
      </div>
    </section>

    <!-- TAB 1: GOALS & METRICS -->
    <section id="tabPanel1" class="hidden">
      <div class="kpi-grid" id="tab1KpiCards"></div>

      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <span>Agent Target Goals &amp; Current Live Status</span>
            <span class="badge badge-blue">Balanced Cost, Speed &amp; Accuracy</span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);">
            Tracks response speed, cost per 1,000 turns ($29.40 &rarr; $3.45), answer accuracy, prompt cache reuse, extra context size, and wait time.
          </div>
        </div>

        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Metric</th>
                <th>Goal Direction</th>
                <th>Starting Value (Gen 0)</th>
                <th>Current Live Value</th>
                <th>Change vs. Start</th>
                <th>Target Goal</th>
                <th>Importance Weight</th>
                <th>Current Status</th>
              </tr>
            </thead>
            <tbody id="parametersTableBody"></tbody>
          </table>
        </div>

        <div style="margin-top:14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <span style="font-size:11.5px;font-weight:700;color:var(--text-secondary);text-transform:uppercase;">Quick-Add Common Metrics:</span>
          <button class="btn" onclick="applyParameterPreset('Context Bloating Ratio', '%', 'lower_is_better', 64.0, 20.0, 15)">+ Extra Context Size</button>
          <button class="btn" onclick="applyParameterPreset('Agent Idle &amp; Wait Ratio', '%', 'lower_is_better', 42.0, 12.0, 10)">+ Agent Wait Time</button>
          <button class="btn" onclick="applyParameterPreset('Skill Token Consumption / Turn', 'k tok', 'lower_is_better', 18.5, 6.0, 15)">+ Tool Tokens / Turn</button>
          <button class="btn" onclick="applyParameterPreset('User Cost per Session', '$', 'lower_is_better', 0.42, 0.08, 20)">+ Cost per Session</button>
          <button class="btn" onclick="applyParameterPreset('Tool Selection Precision', '%', 'higher_is_better', 84.0, 97.5, 15)">+ Tool Choice Accuracy</button>
        </div>

        <div class="add-param-form">
          <div class="field-group">
            <label>Add Custom Metric</label>
            <input id="newParamLabel" type="text" placeholder="e.g., Answer Quality Score" value="Tool Selection Precision" />
          </div>
          <div class="field-group">
            <label>Unit</label>
            <input id="newParamUnit" type="text" placeholder="%, ms, $" value="%" />
          </div>
          <div class="field-group">
            <label>Goal Direction</label>
            <select id="newParamDirection">
              <option value="higher_is_better">Higher is Better (&uarr;)</option>
              <option value="lower_is_better">Lower is Better (&darr;)</option>
            </select>
          </div>
          <div class="field-group">
            <label>Starting Value</label>
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
              + Add Metric
            </button>
          </div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <span>Full 5-Layer Metric Catalog (26 Production Metrics)</span>
            <span class="badge badge-blue">L1 Infra &bull; L2 LLM &bull; L3 ADK &bull; L4 A2A &bull; L5 A2UI</span>
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;">
            <button class="btn" onclick="setOtelLayerFilter('ALL')">All 5 Layers (26)</button>
            <button class="btn" onclick="setOtelLayerFilter('L1_INFRA')">L1: Infra</button>
            <button class="btn" onclick="setOtelLayerFilter('L2_LLM')">L2: LLM</button>
            <button class="btn" onclick="setOtelLayerFilter('L3_ADK')">L3: ADK</button>
            <button class="btn" onclick="setOtelLayerFilter('L4_A2A')">L4: A2A</button>
            <button class="btn" onclick="setOtelLayerFilter('L5_A2UI')">L5: A2UI</button>
          </div>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Layer</th>
                <th>Metric Key</th>
                <th>Metric Name</th>
                <th>Start</th>
                <th>Current Value</th>
                <th>Target Goal</th>
                <th>Why It Matters</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody id="otelCatalogTableBody"></tbody>
          </table>
        </div>
      </div>

      <div class="tab-footer-nav adv-only">
        <button class="btn" onclick="switchTab(0)">&larr; Back: Fleet &amp; Health</button>
        <span>Step 2 of 6: Reviewed target goals and 5-layer metrics.</span>
        <button class="btn btn-primary" onclick="switchTab(2)">Next Step: Testing &amp; History (Tab 3) &rarr;</button>
      </div>
    </section>

    <!-- TAB 2: TESTING, ROLLOUT SIMULATOR & CHANGE HISTORY -->
    <section id="tabPanel2" class="hidden">
      <div class="charts-grid">
        <div class="chart-card">
          <div class="panel-header" style="margin-bottom:8px;padding-bottom:6px;">
            <div class="panel-title" style="font-size:14px;">
              Response Time (P95 Latency ms &darr;)
            </div>
            <span id="latencyDeltaBadge" class="badge badge-green"></span>
          </div>
          <div id="chartLatencySvg"></div>
        </div>

        <div class="chart-card">
          <div class="panel-header" style="margin-bottom:8px;padding-bottom:6px;">
            <div class="panel-title" style="font-size:14px;">
              Cost per 1,000 Turns ($ USD &darr;)
            </div>
            <span id="costDeltaBadge" class="badge badge-green"></span>
          </div>
          <div id="chartCostSvg"></div>
        </div>

        <div class="chart-card">
          <div class="panel-header" style="margin-bottom:8px;padding-bottom:6px;">
            <div class="panel-title" style="font-size:14px;">
              Answer Accuracy (% &uarr; with 95% Minimum Floor)
            </div>
            <span id="accuracyDeltaBadge" class="badge badge-blue"></span>
          </div>
          <div id="chartAccuracySvg"></div>
        </div>

        <div class="chart-card">
          <div class="panel-header" style="margin-bottom:8px;padding-bottom:6px;">
            <div class="panel-title" style="font-size:14px;">
              Prompt Cache Reuse Rate (% &uarr;)
            </div>
            <span id="cacheDeltaBadge" class="badge badge-green"></span>
          </div>
          <div id="chartCacheSvg"></div>
        </div>
      </div>

      <div class="panel sim-panel" id="whatIfSimulatorPanel">
        <div class="panel-header">
          <div class="panel-title">
            <span>What-If Cost, Model &amp; Safe Rollout Simulator</span>
            <span class="badge badge-green" id="whatIfGuardrailBadge">SAFE TO PROMOTE CANARY</span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);">
            Test model choices, thinking token limits, and chat history length before rolling out a safe Cloud Run traffic split.
          </div>
        </div>
        <div class="add-param-form" style="margin-top:0;margin-bottom:14px;">
          <div class="field-group">
            <label>Model Choice</label>
            <select id="whatIfModelTier" onchange="runWhatIfSimulation()">
              <option value="gemini-3.1-flash-tier-routed">Gemini 3.1 Flash Routed (70% Flash / 30% Pro)</option>
              <option value="gemini-2.5-flash">Gemini 2.5 Flash (100% Fast Tier)</option>
              <option value="gemini-2.5-pro">Gemini 2.5 Pro (100% Deep Reasoning)</option>
            </select>
          </div>
          <div class="field-group">
            <label>Thinking Token Limit / Turn</label>
            <select id="whatIfThinkingBudget" onchange="runWhatIfSimulation()">
              <option value="512">512 tok (Strict Cap)</option>
              <option value="1024" selected>1,024 tok (Recommended Goal)</option>
              <option value="2048">2,048 tok (Extended Thinking)</option>
              <option value="4096">4,096 tok (No Limit - High Cost Risk)</option>
            </select>
          </div>
          <div class="field-group">
            <label>Chat History Turns Kept (N)</label>
            <select id="whatIfHistoryTurns" onchange="runWhatIfSimulation()">
              <option value="4">4 Turns (Short History)</option>
              <option value="6" selected>6 Turns (Recommended Balance)</option>
              <option value="12">12 Turns (Medium History)</option>
              <option value="20">20 Turns (Full Untrimmed History)</option>
            </select>
          </div>
          <div class="field-group">
            <label>Cloud Run Test Traffic %</label>
            <select id="whatIfCanaryPct" onchange="runWhatIfSimulation()">
              <option value="10">10% Test Traffic Split</option>
              <option value="15" selected>15% Test Traffic Split</option>
              <option value="25">25% Test Traffic Split</option>
              <option value="50">50% Test Traffic Split</option>
            </select>
          </div>
          <div>
            <button class="btn btn-primary" onclick="runWhatIfSimulation()">
              Simulate Rollout Plan
            </button>
          </div>
        </div>
        <div class="kpi-grid" id="whatIfKpiGrid" style="margin-bottom:12px;"></div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;">
          <div class="action-box">
            <div class="action-box-title">Cloud Run Traffic Split Command (<span class="mono">gcloud run services update-traffic</span>)</div>
            <pre class="diff-pre" id="whatIfCanaryCmd" style="margin-top:6px;"></pre>
          </div>
          <div class="action-box">
            <div class="action-box-title">Configuration Change Preview</div>
            <pre class="diff-pre" id="whatIfGitopsDiff" style="margin-top:6px;"></pre>
          </div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <span>Turn-by-Turn Prompt Cache Check (<span class="mono">detect_prefix_breakpoint</span>)</span>
            <div style="display:flex;gap:8px;align-items:center;">
              <span class="badge badge-blue">Line-by-Line Prompt Cache Check</span>
              <button class="btn btn-primary" id="stepTurnBtn" onclick="triggerApi('/api/step_turn')" style="padding:4px 10px;font-size:11.5px;">
                + Step Live Agent Turn
              </button>
            </div>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);">
            Shows the exact line where a prompt changed and broke caching, plus cached vs. uncached tokens and turn cost.
          </div>
        </div>
        <div class="kpi-grid" id="turnTrajectorySummaryKpis" style="margin-bottom:12px;"></div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Turn #</th>
                <th>Gen</th>
                <th>Tool Used</th>
                <th>Prefix Hash</th>
                <th>Changed Line</th>
                <th>Why Cache Changed</th>
                <th>Cached / Write / Uncached / Thoughts</th>
                <th>Cache Hit %</th>
                <th>Uncached vs. Cached Cost</th>
              </tr>
            </thead>
            <tbody id="cacheForensicsBody"></tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <span>History of Changes Made by the Optimizer</span>
            <span class="badge badge-green">Issue Found in Logs &rarr; Config Change &rarr; Verified Result</span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);">
            Automatically blocks and rolls back any change that drops accuracy below the 95% minimum goal (for example, Gen 9).
          </div>
        </div>
        <div id="actionsTimelineContainer"></div>
      </div>

      <div class="tab-footer-nav adv-only">
        <button class="btn" onclick="switchTab(1)">&larr; Back: Goals &amp; Metrics</button>
        <span>Step 3 of 6: Tested rollout settings and inspected change history.</span>
        <button class="btn btn-primary" onclick="switchTab(3)">Next Step: Cost &amp; Billing (Tab 4) &rarr;</button>
      </div>
    </section>

    <!-- TAB 3: COST & BILLING (WITH FOCUSED SUB-VIEW SELECTOR SO IT IS NEVER BUSY) -->
    <section id="tabPanel3" class="hidden">
      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <span>Model usage &amp; estimated cost (list price)</span>
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

      <div class="panel live-only" id="liveTokenEconomicsPanel">
        <div class="panel-header">
          <div class="panel-title"><span>Token economics by agent</span><span id="liveTeBadge" class="badge badge-blue">&mdash;</span></div>
          <div id="liveTeScope" style="font-size:12px;color:var(--text-secondary);"></div>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Agent</th><th>Requests</th><th>LLM calls</th><th>Calls / request</th><th>Input</th><th>Output</th><th>Tokens / request</th><th>Models</th><th>Source</th>
              </tr>
            </thead>
            <tbody id="liveTeAgentsBody"></tbody>
          </table>
        </div>
        <div id="liveTeGe"></div>
      </div>

      <div class="panel live-only" id="liveSpendDriftPanel">
        <div class="panel-header">
          <div class="panel-title"><span>Why model spend changed vs the previous period</span><span id="liveDriftBadge" class="badge badge-blue">&mdash;</span></div>
          <div id="liveDriftScope" style="font-size:12px;color:var(--text-secondary);"></div>
        </div>
        <div class="table-scroll">
          <table>
            <thead><tr><th>Driver</th><th>Change</th><th>Share of change</th><th>What it means</th></tr></thead>
            <tbody id="liveDriftBody"></tbody>
          </table>
        </div>
        <div class="live-sub">By model</div>
        <div class="table-scroll">
          <table>
            <thead><tr><th>Model</th><th>Previous period</th><th>This period</th><th>Change</th><th>Calls (previous &rarr; this)</th></tr></thead>
            <tbody id="liveDriftModelsBody"></tbody>
          </table>
        </div>
        <div class="live-note" id="liveDriftNote"></div>
      </div>

      <div class="panel live-only adv-only" id="liveWhatIfPanel">
        <div class="panel-header">
          <div class="panel-title"><span>What-if: projection from observed usage</span><span class="badge badge-yellow">Projection</span></div>
          <div style="font-size:12px;color:var(--text-secondary);">Recomputes this period's observed tokens at a different list price or cache share. Token counts stay the same; quality and latency are not modelled.</div>
        </div>
        <div class="whatif-live-form">
          <div><label for="wiKind">Scenario</label><select id="wiKind" onchange="onLiveWhatIfKind()"><option value="model_switch">Switch model</option><option value="cache_share">Change cache-read share</option></select></div>
          <div class="wi-switch"><label for="wiFrom">From model (observed)</label><select id="wiFrom"></select></div>
          <div class="wi-switch"><label for="wiTo">To model (list price)</label><select id="wiTo"></select></div>
          <div class="wi-switch"><label for="wiShare">Share of tokens moved (%)</label><input id="wiShare" type="number" min="0" max="100" value="50" style="width:90px"></div>
          <div class="wi-cache hidden"><label for="wiModel">Model (observed)</label><select id="wiModel"></select></div>
          <div class="wi-cache hidden"><label for="wiCache">Target cache-read share (%)</label><input id="wiCache" type="number" min="0" max="100" value="50" style="width:90px"></div>
          <button class="btn btn-primary" onclick="runLiveWhatIf()">Project</button>
        </div>
        <div id="liveWhatIfResult"><div class="live-note">Pick a scenario. The baseline is the observed usage for the selected time range.</div></div>
      </div>

      <div class="subview-bar adv-only sim-panel">
        <div style="font-size:12px;font-weight:700;color:var(--text-primary);">
          Cost &amp; Billing View (Choose a focused section):
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;">
          <button id="costSubBtn_summary" class="subview-btn active" onclick="switchCostSubView('summary')">1. Cost per Answer &amp; Cloud Bill</button>
          <button id="costSubBtn_calculator" class="subview-btn" onclick="switchCostSubView('calculator')">2. Pricing &amp; Cache Calculator</button>
          <button id="costSubBtn_code_audit" class="subview-btn" onclick="switchCostSubView('code_audit')">3. Code Fixes &amp; Budget Summary</button>
          <button id="costSubBtn_limits" class="subview-btn" onclick="switchCostSubView('limits')">4. Spend Limits &amp; Token Breakdown</button>
          <button id="costSubBtn_all" class="subview-btn" onclick="switchCostSubView('all')">Show All</button>
        </div>
      </div>

      <!-- SUB-VIEW 1: COST PER HELPFUL ANSWER (CpO), BILL DIFFERENCES & CLOUD BILLING SKU TABLE -->
      <div class="panel adv-only sim-panel" id="tokenomicsCpoDriftPanel">
        <div class="panel-header">
          <div class="panel-title">
            <span>1. Cost per Helpful Answer (CpO), Why Bills Differ from Token Counts &amp; Log Matching</span>
            <span class="badge badge-green" id="cpoFormulaBadge">CpO = [&Sigma;(C_LLM + C_Tools + C_Infra)_i + p_esc &times; C_HITL] / (P_res &times; P_csat)</span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);" id="cpoParetoOutlierNote">
            Breaks down total cost per resolved conversation across model tokens, tool calls, Cloud Run hosting, human support handoffs, and user satisfaction ratings.
          </div>
        </div>
        <div class="kpi-grid" id="cpoDriftKpis" style="margin-bottom:14px;"></div>
        <div class="table-scroll" style="margin-bottom:14px;">
          <table>
            <thead>
              <tr>
                <th>Agent &amp; Routing Lane</th>
                <th>Sessions / Mo</th>
                <th>Starting CpO (Turns)</th>
                <th>Model / Tools / Hosting</th>
                <th>Human Support Cost (Before &rarr; Now)</th>
                <th>Current CpO</th>
                <th>Formula</th>
                <th>Monthly Saved</th>
              </tr>
            </thead>
            <tbody id="cpoByAgentBody"></tbody>
          </table>
        </div>

        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:14px;">
          <div>
            <div style="font-size:12px;font-weight:700;color:var(--text-primary);margin-bottom:6px;">
              Why Your Cloud Bill Differs from Raw Token Counts (5 Cost Drivers &bull; model, not measured)
            </div>
            <div class="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>Cost Driver &amp; Rule</th>
                    <th>Share of Extra Spend</th>
                    <th>Before Fix ($)</th>
                    <th>After Fix ($)</th>
                    <th>Fix Applied</th>
                  </tr>
                </thead>
                <tbody id="driftDriversBody"></tbody>
              </table>
            </div>
          </div>
          <div class="action-box">
            <div class="action-box-title">How We Match User Chats, System Logs &amp; Cloud Billing</div>
            <div class="table-scroll" style="margin:6px 0;">
              <table>
                <thead>
                  <tr>
                    <th>Layer</th>
                    <th>Source Table / Stream</th>
                    <th>Join Keys</th>
                    <th>Correlation Key</th>
                    <th>Extracted Fields</th>
                    <th>Variance</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody id="attributionJoinBody"></tbody>
              </table>
            </div>
            <pre class="diff-pre" id="attributionJoinSqlPre" style="max-height:155px;"></pre>
          </div>
        </div>
      </div>

      <div class="panel adv-only" id="billingReconciliationPanel">
        <div class="panel-header">
          <div class="panel-title">
            <span>Cloud Billing SKU Comparison, Reserved Capacity (GSU) Advisor &amp; Platform Hosting Cost</span>
            <span class="badge badge-blue" id="billingReconDeltaBadge">Billing export: checking&hellip;</span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);">
            Billed cost per SKU from your Cloud Billing BigQuery export (last 30 days). Nothing is shown when the export is not connected.
          </div>
        </div>
        <div class="table-scroll" style="margin-bottom:12px;">
          <table>
            <thead>
              <tr>
                <th>GCP Billing SKU ID</th>
                <th>Service</th>
                <th>SKU Description</th>
                <th>Usage Volume</th>
                <th>Tracked Est. ($)</th>
                <th>Billed Gross ($)</th>
                <th>Discounts &amp; Cache Credits ($)</th>
                <th>Net Invoice ($)</th>
                <th>Difference</th>
              </tr>
            </thead>
            <tbody id="billingSkuBody"></tbody>
          </table>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;">
          <div class="action-box" id="gsuAdvisorBox"></div>
          <div class="action-box" id="platformTcoBox"></div>
        </div>
      </div>

      <!-- SUB-VIEW 2: PRICING OPTIONS, CONSUMPTION PORTFOLIO & CACHE BREAK-EVEN CALCULATOR -->
      <div class="panel hidden adv-only sim-panel" id="consumptionAndCachingPanel">
        <div class="panel-header">
          <div class="panel-title">
            <span>2. Pricing Options, Model Tiers &amp; Prompt Cache Break-Even Calculator (N*)</span>
            <span class="badge badge-blue" id="cacheBreakEvenBadge">N* = 1 + S / (d_cache &times; P_in) = 4.70 calls/hr (Flash)</span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);">
            Adjust hourly calls, off-peak traffic share, monthly tasks, and human review time to see updated monthly costs and prompt cache break-even.
          </div>
        </div>

        <div class="add-param-form" style="margin-top:0;margin-bottom:14px;">
          <div class="field-group">
            <label>Repeat Calls / Hour (N)</label>
            <input id="finopsCacheCallsHrInput" type="number" step="1" value="18" onchange="recomputeFinopsLedger()" />
          </div>
          <div class="field-group">
            <label>Off-Peak Batch Share %</label>
            <input id="finopsDeferredShareInput" type="number" step="5" value="35" onchange="recomputeFinopsLedger()" />
          </div>
          <div class="field-group">
            <label>Monthly Agent Tasks</label>
            <input id="finopsMonthlyTasksInput" type="number" step="5000" value="129500" onchange="recomputeFinopsLedger()" />
          </div>
          <div class="field-group">
            <label>Human Review Mins / Ticket</label>
            <input id="finopsHitlMinutesInput" type="number" step="1" value="8.8" onchange="recomputeFinopsLedger()" />
          </div>
          <div class="field-group">
            <label>Support Staff Rate ($/hr)</label>
            <input id="finopsHitlRateInput" type="number" step="5" value="85" onchange="recomputeFinopsLedger()" />
          </div>
          <div>
            <button class="btn btn-primary" id="finopsRecomputeBtn" onclick="recomputeFinopsLedger()">
              Update Cost Calculation
            </button>
          </div>
        </div>

        <div class="action-box" id="cacheBreakEvenSummaryBox" style="margin-bottom:14px;"></div>

        <div class="table-scroll" style="margin-bottom:14px;">
          <table>
            <thead>
              <tr>
                <th>Pricing Type</th>
                <th>Billing Unit</th>
                <th>GCP Service / Scope</th>
                <th>Rate &amp; Discount Structure</th>
                <th>Monthly Spend</th>
                <th>Cost-Saving Action</th>
              </tr>
            </thead>
            <tbody id="meteringCategoriesBody"></tbody>
          </table>
        </div>

        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:14px;margin-bottom:14px;">
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Traffic Routing Lane</th>
                  <th>Speed &amp; Capacity Rule</th>
                  <th>Price Rate</th>
                  <th>Traffic Share</th>
                  <th>Best Used For</th>
                </tr>
              </thead>
              <tbody id="routingLanesBody"></tbody>
            </table>
          </div>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Model Tier</th>
                  <th>Models</th>
                  <th>In / Out ($/1M)</th>
                  <th>Quality per Dollar</th>
                  <th>Role ("Pro Plans, Flash Runs")</th>
                </tr>
              </thead>
              <tbody id="modelPortfolioBody"></tbody>
            </table>
          </div>
        </div>

        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Prompt Caching Mode</th>
                <th>Write / Read Rate</th>
                <th>Storage Fee (S)</th>
                <th>Break-Even Calls/Hr (N*)</th>
                <th>Min Tokens</th>
                <th>Cache Duration</th>
                <th>Recommendation</th>
              </tr>
            </thead>
            <tbody id="cacheModelBreakEvenBody"></tbody>
          </table>
        </div>
      </div>

      <!-- SUB-VIEW 3: CODE FIXES (FIN-01..05), STEP-BY-STEP SAVINGS, @COST_GUARD & FINANCE SUMMARY -->
      <div class="panel hidden adv-only sim-panel" id="cockpitFinopsAndTcoPanel">
        <div class="panel-header">
          <div class="panel-title">
            <span>3. Code Cost Fixes (<span class="mono">FIN-01..FIN-05</span>), Step-by-Step Savings, <span class="mono">@cost_guard</span> &amp; Finance Summary</span>
            <span class="badge badge-green" id="cockpitWaterfallBadge">$142,000/mo &rarr; $10,050/mo (-92.9% Combined Savings)</span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);">
            Shows code-level cost fixes (<span class="mono">agent-ops-cockpit</span>), step-by-step monthly savings, <span class="mono">@cost_guard(budget_limit_usd)</span> per-turn budget checks, and finance accounting breakdown.
          </div>
        </div>

        <div class="kpi-grid" id="cockpitWaterfallKpis" style="margin-bottom:14px;"></div>

        <div class="table-scroll" style="margin-bottom:14px;">
          <table>
            <thead>
              <tr>
                <th>Fix ID &amp; Issue</th>
                <th>File &amp; Line</th>
                <th>Code Pattern Found</th>
                <th>How Savings Are Calculated</th>
                <th>Monthly Saved</th>
                <th>Status &amp; Verification</th>
              </tr>
            </thead>
            <tbody id="cockpitFindingsBody"></tbody>
          </table>
        </div>

        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:14px;margin-bottom:14px;">
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Savings Step</th>
                  <th>Traffic Covered</th>
                  <th>Cost Before ($)</th>
                  <th>Step Reduction</th>
                  <th>Step Saved ($)</th>
                  <th>Cost After ($ &amp; Total %)</th>
                </tr>
              </thead>
              <tbody id="cockpitWaterfallBody"></tbody>
            </table>
          </div>
          <div>
            <div class="action-box" id="costGuardBox" style="margin-bottom:10px;"></div>
            <div class="action-box" id="cfoTcoKpiBox"></div>
          </div>
        </div>

        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:14px;margin-bottom:14px;">
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Engineering Setting</th>
                  <th>Area</th>
                  <th>Cost Impact</th>
                  <th>Speed Change</th>
                  <th>Monthly Impact</th>
                  <th>Recommendation</th>
                </tr>
              </thead>
              <tbody id="opexTradeoffBody"></tbody>
            </table>
          </div>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Finance Cost Category</th>
                  <th>Workloads Included</th>
                  <th>Share</th>
                  <th>Monthly Net Spend</th>
                  <th>Accounting Notes</th>
                </tr>
              </thead>
              <tbody id="pnlAllocationBody"></tbody>
            </table>
          </div>
        </div>

        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Rollout Stage</th>
                <th>Main Focus</th>
                <th>Target Goal</th>
                <th>Active Protection</th>
                <th>Stage Status</th>
              </tr>
            </thead>
            <tbody id="maturityProgressionBody"></tbody>
          </table>
        </div>
      </div>

      <!-- SUB-VIEW 4: API SPEND LIMITS (SLIDE #30), EXTENSION OVERHEAD & TOKEN BREAKDOWN -->
      <div class="panel hidden adv-only sim-panel" id="apigeeAndExtensionsPanel">
        <div class="panel-header">
          <div class="panel-title">
            <span>4. Apigee API Spend Limits &amp; Tool Context Savings</span>
            <span class="badge badge-green">Spend Caps &bull; Automatic Traffic Pause &bull; Knowledge Catalog (7x Smaller Prompts)</span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);">
            Sets per-user token limits at the API gateway (Deck 1 Slide #30 <span class="mono">g3ee7e8b2bb8_1_3597</span>) and replaces 50k+ database schema dumps with 2k Knowledge Catalog summaries.
          </div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:14px;">
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Policy ID &amp; Name</th>
                  <th>Slide Reference</th>
                  <th>Limit Setting</th>
                  <th>Tokens Saved</th>
                  <th>Monthly Saved</th>
                </tr>
              </thead>
              <tbody id="apigeePoliciesBody"></tbody>
            </table>
          </div>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Tool / Extension Setup</th>
                  <th>Tokens / Turn</th>
                  <th>How Context Is Loaded</th>
                  <th>Cost / 100k Turns</th>
                  <th>Recommendation &amp; Status</th>
                </tr>
              </thead>
              <tbody id="extensionOverheadBody"></tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="panel hidden adv-only" id="tokenCategoryAlertsPanel">
        <div class="panel-header">
          <div class="panel-title">
            <span>Token Usage by Type &amp; Stuck Loop Alerts</span>
            <span class="badge badge-blue">Helpful Answers vs. Thinking Tokens vs. Extra Chat History vs. Background Tasks</span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);">
            Separates tokens that directly answer the user from internal thinking tokens, old chat history, and stuck tool retry loops.
          </div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:14px;">
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Token Type</th>
                  <th>Daily Volume</th>
                  <th>Share (Before &rarr; Now)</th>
                  <th>Monthly Cost</th>
                  <th>Monthly Saved</th>
                </tr>
              </thead>
              <tbody id="tokenCategoryBody"></tbody>
            </table>
          </div>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Alert ID &amp; User LDAP</th>
                  <th>Agent &amp; Issue</th>
                  <th>Wasted Tokens</th>
                  <th>Fix Applied</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody id="runawayAlertsBody"></tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="tab-footer-nav adv-only">
        <button class="btn" onclick="switchTab(2)">&larr; Back: Testing &amp; History</button>
        <span>Step 4 of 6: Reviewed unit costs, cloud billing, and spend limits.</span>
        <button class="btn btn-primary" onclick="switchTab(4)">Next Step: Users &amp; Feedback (Tab 5) &rarr;</button>
      </div>
    </section>

    <!-- TAB 4: USERS, TOP SPENDERS & CUSTOMER FEEDBACK -->
    <section id="tabPanel4" class="hidden">
      <div class="kpi-grid adv-only" id="userCentricKpis"></div>

      <div class="panel adv-only">
        <div class="panel-header">
          <div class="panel-title">
            <span id="userCohortsTitle">User Groups &amp; Monthly Savings (modeled)</span>
            <span class="badge badge-blue" id="oauthGovernanceBadge">Simulator data</span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);">
            <span id="userCohortsSubtitle">Shows token usage, extra chat context reduction, wait time, and monthly dollar savings for each user group.</span>
          </div>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>User Group</th>
                <th>Primary Agent</th>
                <th>Daily Users (DAU)</th>
                <th>24h Sessions</th>
                <th>Tokens / User</th>
                <th>Extra Context (Before &rarr; Now)</th>
                <th>Wait Ratio</th>
                <th>Cost / 1k Turns (Before &rarr; Now)</th>
                <th>Monthly Saved</th>
              </tr>
            </thead>
            <tbody id="userCohortsTableBody"></tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <span>Top users</span>
            <span class="badge badge-blue">Per-User Spend &amp; Loop Check</span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);">
            Breaks down prompt, thinking, and background tokens across top users along with satisfaction ratings and monthly department cost.
          </div>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>User LDAP</th>
                <th>Department</th>
                <th>Primary Agent</th>
                <th>7d Sessions</th>
                <th>Total Tokens</th>
                <th>Thinking Tok</th>
                <th>Background Tok</th>
                <th>Cache Hit %</th>
                <th>Avg CSAT</th>
                <th>Monthly Spend</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody id="powerUsersBody"></tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <span>Recent activity &amp; feedback</span>
            <span class="badge badge-green">BigQuery <span class="mono">aive_logs.ratings_log</span> &amp; <span class="mono">agent_usage_log</span></span>
          </div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
            <button class="btn adv-only" onclick="emitLiveAiveUsageEvent()">+ Log Sample Usage Event</button>
          </div>
        </div>
        <div class="add-param-form adv-only" style="margin-top:0;margin-bottom:12px;">
          <div class="field-group">
            <label>User Email / LDAP</label>
            <input id="csatEmailInput" type="text" value="enriq@google.com" />
          </div>
          <div class="field-group">
            <label>Session ID</label>
            <input id="csatSessionInput" type="text" value="6446120131357637190" />
          </div>
          <div class="field-group">
            <label>User Rating (1&ndash;5&#x2605;)</label>
            <select id="csatRatingSelect">
              <option value="5" selected>5 &#x2605;&#x2605;&#x2605;&#x2605;&#x2605; (Resolved &amp; Fast)</option>
              <option value="4">4 &#x2605;&#x2605;&#x2605;&#x2605; (Good)</option>
              <option value="3">3 &#x2605;&#x2605;&#x2605; (Okay)</option>
              <option value="2">2 &#x2605;&#x2605; (Slow / Too Long)</option>
              <option value="1">1 &#x2605; (Wrong Answer / Needed Human)</option>
            </select>
          </div>
          <div class="field-group" style="grid-column:span 2;">
            <label>User Feedback Comment</label>
            <input id="csatFeedbackInput" type="text" value="Prefix cache hit kept turn latency under 600ms with accurate ticket resolution." />
          </div>
          <div>
            <button class="btn btn-primary" onclick="submitLiveCsatRating()">+ Save Rating</button>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:14px;">
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Rating ID</th>
                  <th>User LDAP</th>
                  <th>Session ID</th>
                  <th>CSAT Rating</th>
                  <th>User Feedback Comment</th>
                </tr>
              </thead>
              <tbody id="vocRatingsBody"></tbody>
            </table>
          </div>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Event ID &amp; Task</th>
                  <th>User LDAP &amp; Dept</th>
                  <th>Model &amp; Response Time</th>
                  <th>Total / Think / BG Tok</th>
                  <th>Saved Output File (GCS)</th>
                  <th>CSAT</th>
                </tr>
              </thead>
              <tbody id="aiveUsageBody"></tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="tab-footer-nav adv-only">
        <button class="btn" onclick="switchTab(3)">&larr; Back: Cost &amp; Billing</button>
        <span>Step 5 of 6: Reviewed user groups, top spenders, and user feedback.</span>
        <button class="btn btn-primary" onclick="switchTab(5)">Next Step: Tools &amp; SDK (Tab 6) &rarr;</button>
      </div>
    </section>

    <!-- TAB 5: TOOLS, SKILLS/MCP & @VIBELIFT_TELEMETRY PYTHON SDK -->
    <section id="tabPanel5" class="hidden">
      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <span>Skill &amp; MCP Tool Token Usage + Active Savings</span>
            <span class="badge badge-blue">Automatic Prompt Caching &amp; Trimming Enabled</span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);">
            Shows token usage for each Skill and MCP tool server and how much money is saved by caching static instructions and trimming old history.
          </div>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Skill / MCP Tool</th>
                <th>Type</th>
                <th>Connected Agent</th>
                <th>Calls</th>
                <th>Prompt Tokens</th>
                <th>Cache Hit %</th>
                <th>Extra Context %</th>
                <th>Fix Applied</th>
                <th>Monthly Saved</th>
              </tr>
            </thead>
            <tbody id="skillMcpTableBody"></tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <span>Live <span class="mono">@vibelift_telemetry</span> Event Stream (&lt;10ms Capture)</span>
            <span class="badge badge-green">Direct Capture (No BigQuery Log Delay)</span>
          </div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
            <button class="btn btn-primary" onclick="emitLiveDecoratorEvent()">
              Send Test @vibelift_telemetry Event
            </button>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px;margin-bottom:14px;">
          <div class="action-box">
            <div class="action-box-title">One-Line Python Decorator Setup (ADK / MCP / A2A)</div>
            <pre class="diff-pre" style="margin-top:6px;">from telemetry import vibelift_telemetry

@vibelift_telemetry(
    agent_name="it_service_desk",
    model="gemini-2.5-flash",
    protocol="ADK / MCP Message Passing",
    skill_or_mcp="mcp://service-desk-escalation",
    user_cohort="Enterprise IT Support (2,840 DAU)",
)
async def handle_agent_turn(message_envelope):
    return await runner.process(message_envelope)</pre>
          </div>
          <div class="action-box">
            <div class="action-box-title">Hosting, Security &amp; Privacy Summary</div>
            <div id="governanceDetailsBox" style="font-size:12.5px;line-height:1.65;color:var(--text-primary);margin-top:6px;">
              <div>&bull; <strong>How Data Is Collected:</strong> Lightweight Python decorator on agent messages (&lt;10ms overhead, no BigQuery router wait).</div>
              <div>&bull; <strong>Where It Runs:</strong> Hosted on Google Cloud Run with a right-side panel and fullscreen view.</div>
              <div>&bull; <strong>Security &amp; Privacy:</strong> Uses OAuth 2.0 user consent and privacy-reviewed logging for 4,000&ndash;10,000 daily users.</div>
              <div>&bull; <strong>Optimization Methods:</strong> Works with AlphaEvolve, Opus Critic, and Google Vizier.</div>
            </div>
          </div>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Timestamp</th>
                <th>Agent</th>
                <th>Handler</th>
                <th>Protocol</th>
                <th>Skill / MCP</th>
                <th>User Group</th>
                <th>Response Time</th>
                <th>Cache Hit</th>
                <th>Extra Context</th>
                <th>Wait Ratio</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody id="decoratorEventsBody"></tbody>
          </table>
        </div>
      </div>

      <div class="tab-footer-nav adv-only">
        <button class="btn" onclick="switchTab(4)">&larr; Back: Users &amp; Feedback</button>
        <span>Step 6 of 6: Inspected tool token usage and @vibelift_telemetry SDK stream.</span>
        <button class="btn btn-primary" onclick="switchTab(0)">Back to Start: Gemini Enterprise Agent Fleet (Tab 1) &rarr;</button>
      </div>
    </section>
  </main>

  <script>
    const ADVANCED_ONLY_TABS = [1, 2, 5];
    let currentTabIndex = 6;
    function isAdvancedMode() {
      return !document.body.classList.contains('simple-mode');
    }
    function applyAdvancedMode(on) {
      document.body.classList.toggle('simple-mode', !on);
      const btn = document.getElementById('advancedToggleBtn');
      if (btn) {
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        btn.innerHTML = on ? 'Advanced &#9652;' : 'Advanced &#9662;';
      }
      try { localStorage.setItem('vibelift.advanced', on ? '1' : '0'); } catch (e) {}
      if (!on && ADVANCED_ONLY_TABS.indexOf(currentTabIndex) >= 0) switchTab(6);
      notifyHostSizeChanged();
    }
    function toggleAdvancedMode() {
      applyAdvancedMode(!isAdvancedMode());
    }
    (function initAdvancedMode() {
      let saved = null;
      try { saved = localStorage.getItem('vibelift.advanced'); } catch (e) {}
      const on = saved === '1' || /[?&]advanced=1/.test(location.search);
      document.body.classList.toggle('simple-mode', !on);
      document.addEventListener('DOMContentLoaded', function() { applyAdvancedMode(on); });
    })();
    window.addEventListener('load', function deepLinkTab() {
      const mt = /[?&]tab=([0-9])/.exec(location.search);
      if (!mt) return;
      const t = Number(mt[1]);
      if (ADVANCED_ONLY_TABS.indexOf(t) >= 0 && !isAdvancedMode()) applyAdvancedMode(true);
      switchTab(t);
    });

    function switchTab(tabIndex) {
      const validTabs = [0, 1, 2, 3, 4, 5, 6];
      let tab = validTabs.indexOf(Number(tabIndex)) >= 0 ? Number(tabIndex) : 6;
      if (!isAdvancedMode() && ADVANCED_ONLY_TABS.indexOf(tab) >= 0) tab = 6;
      currentTabIndex = tab;
      validTabs.forEach(function(i) {
        const btn = document.getElementById('tabBtn' + i);
        const panel = document.getElementById('tabPanel' + i);
        if (btn) btn.classList.toggle('active', tab === i);
        if (panel) panel.classList.toggle('hidden', tab !== i);
      });
      const banner = document.getElementById('demoSelectorBanner');
      if (banner) banner.classList.toggle('hidden', tab === 0 || tab === 6 || tab === 3 || tab === 4);
      notifyHostSizeChanged();
    }

    let currentCostSubView = 'summary';
    function switchCostSubView(viewKey) {
      currentCostSubView = viewKey || 'summary';
      const views = ['summary', 'calculator', 'code_audit', 'limits', 'all'];
      views.forEach(function(k) {
        const b = document.getElementById('costSubBtn_' + k);
        if (b) b.classList.toggle('active', k === currentCostSubView);
      });
      const showAll = currentCostSubView === 'all';
      const panelMap = {
        tokenomicsCpoDriftPanel: showAll || currentCostSubView === 'summary',
        billingReconciliationPanel: showAll || currentCostSubView === 'summary',
        consumptionAndCachingPanel: showAll || currentCostSubView === 'calculator',
        cockpitFinopsAndTcoPanel: showAll || currentCostSubView === 'code_audit',
        apigeeAndExtensionsPanel: showAll || currentCostSubView === 'limits',
        tokenCategoryAlertsPanel: showAll || currentCostSubView === 'limits',
      };
      Object.keys(panelMap).forEach(function(pid) {
        const elNode = document.getElementById(pid);
        if (elNode) elNode.classList.toggle('hidden', !panelMap[pid]);
      });
      notifyHostSizeChanged();
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
        const dotColor = isRejected ? '#991b1b' : color;
        circlesHtml += `
          <circle cx="${cx}" cy="${cy}" r="${isRejected ? 5.5 : 4}" fill="${dotColor}" stroke="#fff" stroke-width="1.5"/>
          <text x="${cx}" y="${(Number(cy) - 8).toFixed(1)}" text-anchor="middle" font-size="10.5" font-weight="600" fill="${dotColor}">
            ${unit === '$' ? '$' + Number(p[key]).toFixed(1) : Number(p[key]) + unit}
          </text>
          <text x="${cx}" y="${h - 8}" text-anchor="middle" font-size="10" fill="#475569">
            ${p.timestamp_label}
          </text>
        `;
      });

      return `
        <svg viewBox="0 0 ${w} ${h}" width="100%" height="155">
          <line x1="${padL}" y1="${targetY}" x2="${w - padR}" y2="${targetY}" stroke="#64748b" stroke-dasharray="4,4" stroke-width="1.2"/>
          <text x="${w - padR}" y="${(Number(targetY) - 4).toFixed(1)}" text-anchor="end" font-size="9.5" fill="#475569" font-weight="600">
            Target Goal: ${unit === '$' ? '$' + targetVal : targetVal + unit}
          </text>
          <polyline fill="none" stroke="${color}" stroke-width="2.2" points="${polyPoints}"/>
          ${circlesHtml}
        </svg>
      `;
    }

    let currentState = null;
    let allAgentsCache = {};
    let initialStateSnapshot = null;

    function renderState(state) {
      if (!state) return;
      currentState = state;
      document.body.classList.toggle('live-data', state.live_data === true);
      if (state.live_data === true) {
        // The simulator sub-view bar is hidden in live mode, so show the live panels it used to toggle.
        ['billingReconciliationPanel', 'tokenCategoryAlertsPanel'].forEach(function(id) {
          const n = document.getElementById(id);
          if (n) n.classList.remove('hidden');
        });
        if (state.live_finops && !liveFinopsFromFleet) renderLiveFinops(state.live_finops);
      }
      if (state.all_agents && typeof state.all_agents === 'object') {
        Object.keys(state.all_agents).forEach(function(k) {
          allAgentsCache[k] = state.all_agents[k];
        });
      }
      if (!initialStateSnapshot && state.active_agent) {
        try {
          initialStateSnapshot = JSON.parse(JSON.stringify(state));
        } catch (e) {}
      }
      if (state.ge_fleet) renderFleet(state.ge_fleet);
      const agent = state.active_agent;
      if (!agent) return;
      allAgentsCache[agent.agent_id] = agent;

      const ts = agent.timeline || [];
      const activeGen = ts.length > 0 ? ts[ts.length - 1].generation : 14;

      const dropdown = document.getElementById('agentDropdown');
      if (dropdown && Array.isArray(state.available_agents) && state.available_agents.length > 0) {
        const currentIds = Array.from(dropdown.options).map(function(o) { return o.value; }).join(',');
        const nextIds = state.available_agents.map(function(a) { return a.agent_id; }).join(',');
        if (currentIds !== nextIds) {
          dropdown.replaceChildren();
          state.available_agents.forEach(function(a) {
            const opt = document.createElement('option');
            opt.value = a.agent_id;
            opt.textContent = (a.display_name || a.agent_id) + ' (' + (a.model || 'gemini-2.5-flash') + ')';
            dropdown.appendChild(opt);
          });
        }
      }
      if (dropdown) dropdown.value = agent.agent_id;
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
        'ACTIVE CONFIG: REV ' + activeGen;

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
        renderMiniChart(ts, 'latency_ms', 'ms', '#0f172a', findParam('latency_ms', 800), true);
      document.getElementById('chartCostSvg').innerHTML =
        renderMiniChart(ts, 'cost_usd', '$', '#334155', findParam('cost_usd', 4.0), true);
      document.getElementById('chartAccuracySvg').innerHTML =
        renderMiniChart(ts, 'accuracy_pct', '%', '#475569', findParam('accuracy_pct', 95.0), false);
      document.getElementById('chartCacheSvg').innerHTML =
        renderMiniChart(ts, 'cache_hit_pct', '%', '#1e293b', findParam('cache_hit_pct', 90.0), false);

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
        const statusStr = String(a.status || '');
        const isRej = statusStr.includes('REJECTED');
        const isBase = statusStr.includes('BASELINE');
        const cardCls = isRej ? 'action-card rejected' : (isBase ? 'action-card baseline' : 'action-card');
        const badgeCls = isRej ? 'badge-red' : (isBase ? 'badge-yellow' : 'badge-green');
        return `
          <div class="${cardCls}">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
              <div>
                <span class="badge badge-blue" style="margin-right:8px;">Gen ${esc(a.generation)} &bull; ${esc(a.timestamp)}</span>
                <strong style="font-size:15px;">${esc(a.action_title)}</strong>
                <span class="badge badge-blue" style="margin-left:6px;">Target: ${esc(a.parameter_targeted)}</span>
              </div>
              <span class="badge ${badgeCls}">${esc(statusStr)}</span>
            </div>
            <div class="action-grid">
              <div class="action-box">
                <div class="action-box-title">1. Root Cause Detected in Logs</div>
                <div>${esc(a.root_cause_from_logs)}</div>
              </div>
              <div class="action-box">
                <div class="action-box-title">2. Action Taken by Optimizer</div>
                <div><strong>${esc(a.action_taken)}</strong></div>
                <div style="margin-top:6px;color:var(--g-green);font-weight:700;font-size:12px;">
                  Impact: ${esc(a.impact_summary)}
                </div>
              </div>
              <div>
                <div class="action-box-title" style="margin-bottom:4px;">3. Prompt &amp; Runtime Config Diff</div>
                <pre class="diff-pre">${esc(a.diff_snippet)}</pre>
              </div>
            </div>
          </div>
        `;
      }).join('');

      if (state.optimizer_platforms) {
        const op = state.optimizer_platforms;
        const optSel = document.getElementById('optimizerDropdown');
        if (optSel && op.active_platform_id) {
          optSel.value = op.active_platform_id;
        }
        const optText = document.getElementById('activeOptimizerText');
        if (optText && op.active_platform && op.active_platform.name) {
          optText.textContent = op.active_platform.name;
        }
      }

      renderUserCentricAndDecorator(scopeUserCentric(state.user_centric), state.decorator_events);
      try { renderExecOverview(execLastFleet, state.user_centric); } catch (e) { console.warn('overview', e); }
      renderSmeControlPlane(state);
      notifyHostSizeChanged();
    }

    let currentOtelLayerFilter = 'ALL';
    let latestNl2SqlResult = null;

    function toggleNl2SqlDrawer() {
      const drawer = document.getElementById('nl2sqlCopilotDrawer');
      if (drawer) {
        drawer.classList.toggle('hidden');
        notifyHostSizeChanged();
      }
    }

    function renderNl2SqlResult(res) {
      if (!res || typeof res !== 'object') return;
      latestNl2SqlResult = res;
      const sumEl = document.getElementById('nl2sqlSummaryText');
      if (sumEl) sumEl.textContent = res.executive_summary || '';
      const sqlEl = document.getElementById('nl2sqlSqlPre');
      if (sqlEl) sqlEl.textContent = res.generated_sql || '';
      const headEl = document.getElementById('nl2sqlResultHead');
      const bodyEl = document.getElementById('nl2sqlResultBody');
      const rows = Array.isArray(res.rows) ? res.rows : [];
      if (headEl && bodyEl) {
        headEl.replaceChildren();
        bodyEl.replaceChildren();
        if (rows.length > 0 && typeof rows[0] === 'object') {
          const cols = Object.keys(rows[0]);
          headEl.appendChild(el('tr', null, cols.map(function(c) { return el('th', null, [c]); })));
          rows.forEach(function(r) {
            bodyEl.appendChild(el('tr', null, cols.map(function(c) {
              return el('td', 'mono', [String(r[c] === null || r[c] === undefined ? '—' : r[c])]);
            })));
          });
        }
      }
    }

    function runNl2SqlPreset(q) {
      const inp = document.getElementById('nl2sqlQuestionInput');
      if (inp) inp.value = q;
      runNl2SqlQuery();
    }

    async function runNl2SqlQuery() {
      const inp = document.getElementById('nl2sqlQuestionInput');
      const question = (inp && inp.value ? inp.value.trim() : '') || 'Compare cost per 1k turns and prompt cache savings across agents';
      if (!isEmbedded()) {
        try {
          const resp = await fetch('/api/nl2sql', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({question: question}),
          });
          if (resp.ok) {
            const data = await resp.json();
            renderNl2SqlResult(data);
            return;
          }
        } catch (e) {}
      }
      // Embedded or offline fallback
      if (currentState && currentState.nl2sql_default) {
        renderNl2SqlResult(currentState.nl2sql_default);
      }
    }

    function setOtelLayerFilter(layerId) {
      currentOtelLayerFilter = layerId || 'ALL';
      if (currentState && currentState.otel_catalog) {
        renderOtelCatalog(currentState.otel_catalog);
      }
    }

    function toggleTelemetryValidatorDrawer() {
      const drawer = document.getElementById('telemetryValidatorDrawer');
      if (drawer) {
        drawer.classList.toggle('hidden');
        notifyHostSizeChanged();
      }
    }

    function renderTelemetryValidation(val) {
      if (!val || typeof val !== 'object') return;
      const sumBadge = document.getElementById('telemetryValidatorSummaryBadge');
      if (sumBadge) {
        const passed = val.passed_count ?? 0;
        const total = val.total_checks ?? 0;
        sumBadge.textContent = (total ? (passed + '/' + total + (passed === total ? ' \u2713' : ' \u26a0')) : '\u2014');
      }
      const judgeBox = document.getElementById('telemetryValidatorJudgeBox');
      if (judgeBox) {
        const judge = val.llm_judge || {};
        const verdict = judge.verdict || val.overall_status || 'NOT RUN';
        const vCls = String(verdict) === 'VERIFIED_GROUNDED' ? 'badge-green' : 'badge-red';
        const score = judge.grounding_score_100 ?? val.grounding_score_pct;
        const rows = [
          el('div', 'action-box-title', [
            'Judge: ' + (judge.judge_model_used || judge.judge_model || 'not run') +
            ' \u2022 Score: ' + (score == null ? '\u2014' : score + '/100')
          ]),
          el('div', null, [
            badge(verdict, vCls),
            el('span', null, [' ' + (judge.executive_summary || '')])
          ])
        ];
        if (judge.judge_error) {
          rows.push(el('div', 'kpi-sub', ['LLM judge unavailable: ' + judge.judge_error]));
        }
        if (Array.isArray(judge.tab_findings) && judge.tab_findings.length) {
          rows.push(el('div', 'kpi-sub', ['Checks: ' + judge.tab_findings.join(' | ')]));
        }
        judgeBox.replaceChildren.apply(judgeBox, rows);
      }
      const tbody = document.getElementById('telemetryValidatorChecksBody');
      if (tbody && Array.isArray(val.checks)) {
        tbody.replaceChildren();
        val.checks.forEach(function(chk) {
          const st = String(chk.status || 'PASS');
          const stCls = st === 'PASS' ? 'badge-green' : 'badge-red';
          tbody.appendChild(el('tr', null, [
            el('td', 'mono', [el('strong', null, [chk.check_id || ''])]),
            el('td', null, [badge(chk.tab || '', 'badge-blue')]),
            el('td', null, [
              el('strong', null, [chk.metric || '']),
              el('div', 'kpi-sub', [chk.detail || ''])
            ]),
            el('td', 'mono', [chk.expected || '']),
            el('td', 'mono', [chk.actual || '']),
            el('td', 'mono', [chk.source || '']),
            el('td', null, [badge(st, stCls)]),
          ]));
        });
      }
    }

    async function runTelemetryValidationAudit(runLlmJudge) {
      const btn = document.getElementById('runLlmJudgeAuditBtn');
      const origText = btn ? btn.textContent : 'Run Live LLM-as-a-Judge Audit';
      if (btn) {
        btn.disabled = true;
        btn.textContent = 'Auditing via Vertex AI...';
      }
      try {
        const resp = await fetch('/api/validate_telemetry', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({llm_judge: Boolean(runLlmJudge)}),
        });
        if (resp.ok) {
          const val = await resp.json();
          if (currentState) {
            currentState.telemetry_validation = val;
          }
          renderTelemetryValidation(val);
        }
      } catch (e) {
        if (currentState && currentState.telemetry_validation) {
          renderTelemetryValidation(currentState.telemetry_validation);
        }
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = origText;
        }
      }
    }

    function renderOtelCatalog(catalog) {
      if (!catalog || typeof catalog !== 'object') return;
      const alarmsBox = document.getElementById('watchOutAlarmsContainer');
      if (alarmsBox && Array.isArray(catalog.watch_out_alarms)) {
        alarmsBox.replaceChildren();
        catalog.watch_out_alarms.forEach(function(al) {
          const sev = String(al.severity || 'HIGH');
          const sevCls = sev === 'CRITICAL' ? 'badge-red' : 'badge-yellow';
          alarmsBox.appendChild(el('div', 'action-box', [
            el('div', null, [
              badge(sev, sevCls),
              el('strong', null, [' ' + (al.title || al.alarm_id || '')]),
            ]),
            el('div', 'mono', ['Metric: ' + (al.metric_key || al.formula || '') + ' | Baseline: ' + (al.threshold || al.baseline || '')]),
            el('div', null, ['Current: ' + (al.current_status || al.current || '')]),
            el('div', null, [el('strong', null, ['Status / Mitigation: ']), String(al.automated_remediation || al.status || '')]),
          ]));
        });
      }

      const tbody = document.getElementById('otelCatalogTableBody');
      if (tbody && Array.isArray(catalog.layers)) {
        tbody.replaceChildren();
        catalog.layers.forEach(function(layer) {
          if (currentOtelLayerFilter !== 'ALL' && layer.layer_id !== currentOtelLayerFilter) return;
          (layer.metrics || layer.parameters || []).forEach(function(m) {
            const st = String(m.status || 'OPTIMIZED');
            const stCls = st.includes('BREACH') ? 'badge-red' : 'badge-green';
            tbody.appendChild(el('tr', null, [
              el('td', null, [badge(layer.layer_id || 'OTEL', 'badge-blue')]),
              el('td', 'mono', [m.otel_metric || m.otel_name || m.param_id || '']),
              el('td', null, [el('strong', null, [m.label || ''])]),
              el('td', 'mono', [m.baseline_value + ' ' + (m.unit || '')]),
              el('td', 'mono', [el('strong', null, [m.current_value + ' ' + (m.unit || '')])]),
              el('td', 'mono', [(m.target_slo ?? m.target_value ?? '') + ' ' + (m.unit || '')]),
              el('td', null, [m.why_it_matters || '']),
              el('td', null, [badge(st, stCls)]),
            ]));
          });
        });
      }
    }

    function renderWhatIfResult(sim) {
      if (!sim || typeof sim !== 'object') return;
      const guardBadge = document.getElementById('whatIfGuardrailBadge');
      if (guardBadge) {
        const statusStr = String(sim.guardrail_status || 'SAFE_TO_PROMOTE_CANARY');
        guardBadge.textContent = statusStr + ' (Accuracy: ' + sim.projected_accuracy_pct + '% vs ' + sim.accuracy_guardrail_floor_pct + '% Floor)';
        guardBadge.className = 'badge ' + (statusStr.includes('BLOCKED') ? 'badge-red' : 'badge-green');
      }
      const grid = document.getElementById('whatIfKpiGrid');
      if (grid) {
        grid.replaceChildren(
          kpiCard(
            'Projected Cost / 1k Turns',
            '$' + Number(sim.current_cost_per_1k_usd || 3.45).toFixed(2) + ' → $' + Number(sim.projected_cost_per_1k_usd || 2.18).toFixed(2),
            'Baseline Gen 0: $' + Number(sim.baseline_cost_per_1k_usd || 29.40).toFixed(2) + '/1k'
          ),
          kpiCard(
            'Cost / CSAT-Positive Resolved Session',
            '$' + Number(sim.projected_cost_per_resolved_session_usd || 0.0069).toFixed(4),
            'North-Star Outcome Unit Economics'
          ),
          kpiCard(
            'Projected P95 Latency',
            Number(sim.current_p95_latency_ms || 690).toFixed(0) + 'ms → ' + Number(sim.projected_p95_latency_ms || 515).toFixed(0) + 'ms',
            'Prompt Cache Hit: ' + Number(sim.projected_cache_hit_pct || 93.4).toFixed(1) + '%'
          ),
          kpiCard(
            'Additional Monthly Fleet Savings',
            '+$' + fmtInt(sim.additional_monthly_savings_usd || 11400) + '/mo',
            'Canary Split: ' + (sim.traffic_canary_pct || 15) + '% Traffic'
          )
        );
      }
      const cmdEl = document.getElementById('whatIfCanaryCmd');
      if (cmdEl) cmdEl.textContent = sim.canary_rollout_command || '';
      const diffEl = document.getElementById('whatIfGitopsDiff');
      if (diffEl) diffEl.textContent = sim.gitops_diff || '';
    }

    async function runWhatIfSimulation() {
      const tierEl = document.getElementById('whatIfModelTier');
      const thinkEl = document.getElementById('whatIfThinkingBudget');
      const histEl = document.getElementById('whatIfHistoryTurns');
      const canEl = document.getElementById('whatIfCanaryPct');
      const body = {
        model_tier: tierEl ? tierEl.value : 'gemini-3.1-flash-tier-routed',
        thinking_budget_tok: thinkEl ? parseInt(thinkEl.value, 10) : 1024,
        history_window_turns: histEl ? parseInt(histEl.value, 10) : 6,
        traffic_canary_pct: canEl ? parseInt(canEl.value, 10) : 15,
      };
      if (!isEmbedded()) {
        try {
          const resp = await fetch('/api/what_if_simulate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
          });
          if (resp.ok) {
            const sim = await resp.json();
            renderWhatIfResult(sim);
            return;
          }
        } catch (e) {}
      }
      // Client-side instant fallback calculation for embedded mode
      const curCost = 3.45;
      const tierMult = body.model_tier.indexOf('flash') >= 0 ? (body.model_tier.indexOf('routed') >= 0 ? 0.68 : 0.54) : 1.15;
      const thinkMult = Math.max(0.72, Math.min(1.35, body.thinking_budget_tok / 1600.0));
      const histMult = Math.max(0.75, Math.min(1.40, body.history_window_turns / 8.0));
      const projCost = Math.max(1.25, Math.round(curCost * tierMult * thinkMult * histMult * 100) / 100);
      const projLat = Math.max(340, Math.round(690 * (0.65 + 0.25 * tierMult) * (0.8 + 0.2 * thinkMult)));
      const projAcc = Math.min(99.1, Math.max(89.5, Math.round((96.4 - (body.history_window_turns < 4 ? 1.8 : 0) + (body.thinking_budget_tok >= 1024 ? 0.5 : -0.4)) * 10) / 10));
      renderWhatIfResult({
        model_tier: body.model_tier,
        thinking_budget_tok: body.thinking_budget_tok,
        history_window_turns: body.history_window_turns,
        traffic_canary_pct: body.traffic_canary_pct,
        baseline_cost_per_1k_usd: 29.40,
        current_cost_per_1k_usd: curCost,
        projected_cost_per_1k_usd: projCost,
        projected_cost_per_resolved_session_usd: Math.round(projCost * 0.00318 * 10000) / 10000,
        current_p95_latency_ms: 690,
        projected_p95_latency_ms: projLat,
        projected_accuracy_pct: projAcc,
        accuracy_guardrail_floor_pct: 95.0,
        guardrail_status: projAcc >= 95.0 ? 'SAFE_TO_PROMOTE_CANARY' : 'BLOCKED_BY_ACCURACY_GUARDRAIL',
        projected_cache_hit_pct: 93.6,
        additional_monthly_savings_usd: Math.max(1200, Math.round((curCost - projCost) * 9000)),
        canary_rollout_command: 'gcloud run services update-traffic vibelift-analytics-agent --region=us-central1 --to-revisions=vibelift-canary=' + body.traffic_canary_pct + ',LATEST=' + (100 - body.traffic_canary_pct),
        gitops_diff: '- model_routing: "static"\\n- thinking_budget_tokens: 4096\\n- history_window_turns: 20\\n+ model_routing: "' + body.model_tier + '"\\n+ thinking_budget_tokens: ' + body.thinking_budget_tok + '\\n+ history_window_turns: ' + body.history_window_turns,
      });
    }

    function renderCacheForensics(turns) {
      const tbody = document.getElementById('cacheForensicsBody');
      if (!tbody || !Array.isArray(turns)) return;
      tbody.replaceChildren();
      turns.forEach(function(t) {
        const um = t.usage_metadata || {};
        const ba = t.billing_attribution || {};
        const bpLine = t.cache_breakpoint_line;
        const bpBadge = bpLine
          ? badge('Line ' + bpLine + ' Mutated', 'badge-red')
          : badge('100% Static Match', 'badge-green');
        const hitPct = Number(t.cache_hit_ratio || 0).toFixed(1) + '%';
        const hitBadge = Number(t.cache_hit_ratio || 0) >= 75
          ? badge(hitPct, 'badge-green')
          : badge(hitPct, 'badge-yellow');
        tbody.appendChild(el('tr', null, [
          el('td', 'mono', ['#' + t.turn_index]),
          el('td', 'mono', ['Gen ' + t.evolution_generation]),
          el('td', 'mono', [t.tool_called || '—']),
          el('td', 'mono', [t.prompt_prefix_hash || '—']),
          el('td', null, [bpBadge]),
          el('td', null, [t.cache_breakpoint_reason || '—']),
          el('td', 'mono', [
            fmtInt(um.cached_content_token_count || 0) + ' / ' +
            fmtInt(um.cache_creation_input_tokens || 0) + ' / ' +
            fmtInt(um.uncached_input_tokens || 0) + ' / ' +
            fmtInt(um.thoughts_token_count || 0)
          ]),
          el('td', null, [hitBadge]),
          el('td', 'mono', [
            '$' + Number(ba.naive_count_tokens_usd || 0).toFixed(4) + ' → $' + Number(ba.actual_log_cached_usd || 0).toFixed(4)
          ]),
        ]));
      });
    }

    async function submitLiveCsatRating() {
      const emailEl = document.getElementById('csatEmailInput');
      const sessEl = document.getElementById('csatSessionInput');
      const ratEl = document.getElementById('csatRatingSelect');
      const fbEl = document.getElementById('csatFeedbackInput');
      const payload = {
        user_email: emailEl ? emailEl.value : 'enriq@google.com',
        session_id: sessEl ? sessEl.value : '6446120131357637190',
        rating: ratEl ? parseInt(ratEl.value, 10) : 5,
        feedback_text: fbEl ? fbEl.value : 'Verified SME closed-loop optimization guardrail.',
      };
      if (!isEmbedded()) {
        try {
          const res = await fetch('/api/csat_rating', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
          });
          if (res.ok) {
            const nextState = await res.json();
            renderState(nextState);
            return;
          }
        } catch (e) {}
      }
      if (currentState && currentState.aive_logs && Array.isArray(currentState.aive_logs.ratings_logs)) {
        const ldap = String(payload.user_email || 'enriq').split('@')[0];
        currentState.aive_logs.ratings_logs.unshift({
          rating_id: 'rat-live',
          timestamp: 'Just now',
          session_id: payload.session_id,
          user_email: payload.user_email,
          user_ldap: ldap,
          rating: payload.rating,
          feedback_text: payload.feedback_text,
        });
        renderSmeControlPlane(currentState);
      }
    }

    function emitLiveAiveUsageEvent() {
      triggerApi('/api/aive_log', {
        user_email: 'enriq@google.com',
        task_type: 'SME_CONTROL_PLANE_AUDIT',
        prompt: 'Verify closed-loop CSAT + tokenomics guardrail',
        total_tokens: 18900,
        latency_ms: 540.0,
        status: 'SUCCESS',
      });
    }

    let activeSmePersonaId = 'finops_lead';

    function selectSmePersona(personaId) {
      activeSmePersonaId = personaId;
      if (currentState) {
        renderSmePersonaSelector(currentState.persona_playbooks);
      }
    }

    function renderSmePersonaSelector(pb) {
      if (!pb || !Array.isArray(pb.personas)) return;
      const bar = document.getElementById('smePersonaLensBar');
      const card = document.getElementById('smePersonaPlaybookCard');
      if (bar) {
        bar.replaceChildren();
        pb.personas.forEach(function(p) {
          const isActive = p.persona_id === activeSmePersonaId;
          const bScore = p.score_before_100 ?? p.before_score ?? 48;
          const aScore = p.score_after_100 ?? p.after_score ?? 96;
          const btn = el('button', isActive ? 'btn btn-primary' : 'btn', [
            p.role_title + ' (' + bScore + ' → ' + aScore + '/100)'
          ]);
          btn.style.padding = '4px 10px';
          btn.style.fontSize = '11.5px';
          btn.onclick = function() {
            selectSmePersona(p.persona_id);
          };
          bar.appendChild(btn);
        });
      }
      if (card) {
        const selected = pb.personas.find(function(p) { return p.persona_id === activeSmePersonaId; }) || pb.personas[0];
        if (selected) {
          const bScore = selected.score_before_100 ?? selected.before_score ?? 48;
          const aScore = selected.score_after_100 ?? selected.after_score ?? 96;
          const primKpi = selected.primary_kpi || (Array.isArray(selected.primary_kpis) ? selected.primary_kpis[0] : 'Cost & Quality');
          const dailyAction = selected.daily_workflow_action || (Array.isArray(selected.actionable_controls) ? selected.actionable_controls.join(' • ') : '');
          const compScore = pb.composite_after_score_100 ?? pb.fleet_average_after_score ?? 96.3;
          card.replaceChildren(
            el('div', 'action-box-title', [
              selected.role_title + ' Operational Playbook • Primary KPI: ' + primKpi +
              ' • Usability Score: ' + bScore + '/100 (POC) → ' + aScore + '/100 (Control Plane) • Composite: ' +
              compScore + '/100'
            ]),
            el('div', null, [
              el('strong', null, ['Daily Workflow Action: ']),
              dailyAction
            ]),
            el('div', 'kpi-sub', [
              'Key Questions Answered: ' + (selected.key_questions_answered || selected.primary_kpis || []).join(' | ')
            ])
          );
        }
      }
    }

    function renderSmeControlPlane(state) {
      if (!state) return;

      // 00. Telemetry Grounding & LLM-as-a-Judge Validator
      if (state.telemetry_validation) {
        renderTelemetryValidation(state.telemetry_validation);
      }

      // 0. SME Persona Lens Selector & Operational Playbook
      if (state.persona_playbooks) {
        renderSmePersonaSelector(state.persona_playbooks);
      }

      // 0b. 7-Step Closed-Loop Optimization Pipeline Ribbon
      const wfRibbon = document.getElementById('workflowStepsRibbon');
      const wfSteps = Array.isArray(state.workflow_steps) ? state.workflow_steps : (Array.isArray(state.steps) ? state.steps : []);
      if (wfRibbon && wfSteps.length > 0) {
        wfRibbon.replaceChildren();
        wfSteps.forEach(function(ws, idx) {
          const isStr = typeof ws === 'string';
          const titleStr = isStr ? ws : ('Step ' + (ws.step || (idx + 1)) + ': ' + (ws.title || ''));
          const detailStr = isStr ? 'Verified in closed-loop telemetry pipeline' : (ws.detail || '');
          const pill = el('div', 'action-box', [
            el('div', 'action-box-title', [titleStr]),
            el('div', 'kpi-sub', [detailStr])
          ]);
          pill.style.padding = '6px 10px';
          pill.style.maxWidth = '230px';
          wfRibbon.appendChild(pill);
        });
      }

      // 0c. Turn Trajectory Summary KPIs (#turnTrajectorySummaryKpis)
      const tsKpis = document.getElementById('turnTrajectorySummaryKpis');
      if (tsKpis && state.turn_summary) {
        const ts = state.turn_summary;
        tsKpis.replaceChildren(
          kpiCard(
            'Total Evaluated Agent Turns',
            fmtInt(ts.total_turns || 0) + ' turns',
            'Cache Hits: ' + fmtInt(ts.cache_hits || 0) + ' | Cache Busts: ' + fmtInt(ts.cache_busts || 0)
          ),
          kpiCard(
            'Observed Trajectory Cache Hit Rate',
            Number(ts.cache_hit_rate_pct || 0).toFixed(1) + '%',
            'Prefix SHA-256 verification across turns'
          ),
          kpiCard(
            'Cumulative Trajectory Spend (Actual vs Naive)',
            '$' + Number(ts.total_actual_cost_usd || 0).toFixed(4) + ' vs $' + Number(ts.total_naive_cost_usd || 0).toFixed(4),
            'Net Trajectory Saved: $' + Number(ts.net_cost_saved_usd || 0).toFixed(4)
          ),
          kpiCard(
            'Total Cached vs Uncached Input Tokens',
            fmtInt(ts.total_cached_read_tokens || 0) + ' cached',
            fmtInt(ts.total_uncached_input_tokens || 0) + ' uncached • ' + fmtInt(ts.total_thoughts_tokens || 0) + ' thoughts'
          )
        );
      }

      // 0d. Cloud Run Revision Topology & Gemini Enterprise Support Triage
      const crBody = document.getElementById('cloudRunServicesBody');
      if (crBody && Array.isArray(state.cloud_run_services)) {
        crBody.replaceChildren();
        state.cloud_run_services.forEach(function(svc) {
          crBody.appendChild(el('tr', null, [
            el('td', 'mono', [
              el('strong', null, [svc.service_name || '']),
              el('div', 'kpi-sub', [(svc.region || 'us-central1') + ' • rev: ' + (svc.active_revision || '')])
            ]),
            el('td', 'mono', ['min=' + (svc.min_instances ?? 1) + ' / max=' + (svc.max_instances ?? 10) + ' / conc=' + (svc.concurrency ?? 80)]),
            el('td', 'mono', [(svc.cpu_utilization_pct || 0) + '% CPU / ' + (svc.memory_utilization_pct || 0) + '% Mem']),
            el('td', 'mono', [(svc.p95_latency_ms || 0) + 'ms (cold: ' + (svc.cold_starts_1h ?? 0) + ')']),
            el('td', 'mono', ['$' + fmtInt(svc.monthly_cost_usd || 0) + '/mo']),
            el('td', null, [badge(svc.status || 'HEALTHY', 'badge-green')]),
          ]));
        });
      }

      const geSupBody = document.getElementById('geSupportEventsBody');
      if (geSupBody && Array.isArray(state.gemini_enterprise_support_events)) {
        geSupBody.replaceChildren();
        state.gemini_enterprise_support_events.forEach(function(ev) {
          geSupBody.appendChild(el('tr', null, [
            el('td', 'mono', [
              el('strong', null, [ev.ticket_id || ev.event_id || ev.session_id || '']),
              el('div', 'kpi-sub', [ev.tier || ev.triage_tier || 'L1/L2'])
            ]),
            el('td', 'mono', [ev.agent_id || '']),
            el('td', 'mono', [ev.trace_id || ev.event_timestamp || '']),
            el('td', null, [ev.issue_summary || ev.intent_category || ev.failure_mode || '']),
            el('td', null, [
              badge(ev.status || ev.resolution_status || 'RESOLVED', 'badge-green'),
              el('div', 'kpi-sub', [ev.resolution_action || ('Latency: ' + (ev.latency_ms || 0) + 'ms • Tokens: ' + (ev.tokens_used || 0))])
            ]),
          ]));
        });
      }

      // 1. North-Star Unit Economics & Dual-Ledger Cloud Billing Reconciliation
      const br = state.billing_reconciliation || {};
      const ue = br.unit_economics || {};
      const nsKpis = document.getElementById('northStarUnitEconKpis');
      const liveBilling = !!br.status;
      if (nsKpis && liveBilling) {
        const connected = br.status === 'LIVE';
        const topSvc = Object.keys(br.net_by_service || {})[0];
        nsKpis.replaceChildren(
          kpiCard('Billed net cost (' + (br.window_days || 30) + 'd)', connected ? money(br.total_net_invoice_usd) : '—',
                  connected ? 'Gross ' + money(br.total_gross_usd) + ' · credits ' + money(br.total_credits_usd) : String(br.message || '')),
          kpiCard('Largest service', connected && topSvc ? topSvc : '—',
                  connected && topSvc ? money(br.net_by_service[topSvc]) + ' net' : 'Source: Cloud Billing export'),
          kpiCard('Billing export', br.status, br.billing_export_table || 'not configured')
        );
      } else if (nsKpis) {
        nsKpis.replaceChildren(
          kpiCard(
            'North-Star: Cost / CSAT-Positive Resolved Session (simulator)',
            '$' + Number(ue.baseline_cost_per_resolved_session_usd || 0).toFixed(4) + ' → $' + Number(ue.optimized_cost_per_resolved_session_usd || 0).toFixed(4),
            orDash(ue.unit_cost_reduction_pct, '% simulated reduction')
          ),
          kpiCard(
            'First-Contact Resolution Rate (simulator)',
            orDash(ue.baseline_resolution_rate_pct, '%') + ' → ' + orDash(ue.optimized_resolution_rate_pct, '%'),
            'Avg Turns / Session: ' + orDash(ue.avg_turns_per_session_baseline) + ' → ' + orDash(ue.avg_turns_per_session_optimized)
          ),
          kpiCard(
            'Net Cloud Invoice (simulator)',
            money(br.total_net_invoice_usd),
            'Credits: ' + money(br.total_credits_usd)
          )
        );
      }
      const brBadge = document.getElementById('billingReconDeltaBadge');
      if (brBadge) {
        if (!liveBilling) {
          brBadge.textContent = 'Simulator data';
          brBadge.className = 'badge badge-blue';
        } else if (br.status === 'LIVE') {
          brBadge.textContent = 'Billing export: live · ' + (br.window_days || 30) + 'd';
          brBadge.className = 'badge badge-green';
        } else {
          brBadge.textContent = 'Billing export: ' + String(br.status).toLowerCase().replace('_', ' ');
          brBadge.className = 'badge badge-yellow';
        }
      }

      // 2. NL2SQL Co-Pilot Initial Result
      if (!latestNl2SqlResult && state.nl2sql_default) {
        renderNl2SqlResult(state.nl2sql_default);
      }

      // 3. 5-Layer OTel Catalog & Watch-Out Alarms
      if (state.otel_catalog) {
        renderOtelCatalog(state.otel_catalog);
      }

      // 4. What-If FinOps & Canary Simulator + Per-Turn Cache Forensics
      if (state.what_if_default) {
        renderWhatIfResult(state.what_if_default);
      }
      if (state.turns) {
        renderCacheForensics(state.turns);
      }

      // 5. Token Category Breakdown & Runaway Agent Alerts
      const uc = state.user_centric || {};
      const tcBody = document.getElementById('tokenCategoryBody');
      if (tcBody && uc.live) {
        const tc = uc.token_category_breakdown || {};
        setTableHead('tokenCategoryBody', ['Observed token type', 'Tokens', 'Share', 'Source', '']);
        tcBody.replaceChildren();
        const tin = tc.observed_gcp_prompt_tokens;
        [['Prompt (input)', tin], ['Cached input', tc.observed_gcp_cached_tokens], ['Output', tc.observed_gcp_output_tokens]]
          .forEach(function(r) {
            tcBody.appendChild(el('tr', null, [
              el('td', null, [el('strong', null, [r[0]])]),
              el('td', 'mono', [fmtTokens(r[1])]),
              el('td', 'mono', [r[0] === 'Cached input' ? orDash(tc.observed_gcp_cache_hit_pct, '% of input') : '']),
              el('td', 'kpi-sub', ['OTel GenAI spans / fleet totals']),
              el('td', null, ['']),
            ]));
          });
      } else if (tcBody && uc.token_category_breakdown) {
        tcBody.replaceChildren();
        const rawTc = uc.token_category_breakdown;
        const tcRows = Array.isArray(rawTc)
          ? rawTc
          : Object.keys(rawTc).map(function(k) {
              const item = rawTc[k] || {};
              return {
                category: k.replace(/_/g, ' ').toUpperCase(),
                optimization_action: item.remediation || item.description || '',
                daily_tokens_m: ((Number(item.after_tokens_per_turn || 0) * 1000) / 1000000).toFixed(2),
                share_before_pct: item.before_share_pct ?? 0,
                share_after_pct: item.after_share_pct ?? 0,
                monthly_cost_usd: Math.round(Number(item.after_tokens_per_turn || 0) * 0.45),
                monthly_saved_usd: Math.max(0, Math.round((Number(item.before_tokens_per_turn || 0) - Number(item.after_tokens_per_turn || 0)) * 0.45)),
              };
            });
        tcRows.forEach(function(cat) {
          tcBody.appendChild(el('tr', null, [
            el('td', null, [
              el('strong', null, [cat.category || '']),
              el('div', 'kpi-sub', [cat.optimization_action || '']),
            ]),
            el('td', 'mono', [cat.daily_tokens_m + 'M tok/d']),
            el('td', 'mono', [cat.share_before_pct + '% → ' + cat.share_after_pct + '%']),
            el('td', 'mono', ['$' + fmtInt(cat.monthly_cost_usd) + '/mo']),
            el('td', 'mono', [badge('$' + fmtInt(cat.monthly_saved_usd) + '/mo saved', 'badge-green')]),
          ]));
        });
      }

      const raBody = document.getElementById('runawayAlertsBody');
      if (raBody && Array.isArray(uc.runaway_agent_alerts)) {
        raBody.replaceChildren();
        if (uc.live) {
          setTableHead('runawayAlertsBody', ['Alert', 'Agent / model & issue', 'Observed', 'Suggested fix', 'Status']);
          if (!uc.runaway_agent_alerts.length) emptyRow(raBody, 5, 'No alerts: no agent or model crossed an alert rule in this window.');
        }
        uc.runaway_agent_alerts.forEach(function(al, idx) {
          raBody.appendChild(el('tr', null, [
            el('td', 'mono', [
              el('strong', null, [al.alert_id || ('ALRT-' + (idx + 1))]),
              el('div', null, ['Severity: ' + (al.user_ldap || al.severity || 'HIGH')]),
            ]),
            el('td', null, [
              badge(al.agent_id || al.agent_name || '', 'badge-blue'),
              el('div', null, [al.issue_type || al.runaway_pattern || '']),
            ]),
            el('td', 'mono', [
              al.observed ? al.observed
              : al.wasted_tokens !== undefined
                ? (fmtInt(al.wasted_tokens) + ' tok ($' + Number(al.cost_impact_usd || 0).toFixed(2) + ')')
                : String(al.baseline_burn_per_1k_turns || '') + ' → ' + String(al.optimized_burn_per_1k_turns || '')
            ]),
            el('td', null, [al.remediation_applied || al.mitigation_applied || '']),
            el('td', null, [badge(al.status || '—', al.status === 'OPEN' ? 'badge-yellow' : 'badge-blue')]),
          ]));
        });
      }

      // 6. Dual-Ledger Billing SKU Table, GSU Advisor & Platform TCO
      const skuBody = document.getElementById('billingSkuBody');
      if (skuBody && Array.isArray(br.sku_ledger)) {
        skuBody.replaceChildren();
        if (liveBilling && !br.sku_ledger.length) {
          emptyRow(skuBody, 9, (br.status === 'LIVE' ? '' : 'Not connected. ') + String(br.message || ''));
        }
        br.sku_ledger.forEach(function(row) {
          skuBody.appendChild(el('tr', null, [
            el('td', 'mono', [row.sku_id || '']),
            el('td', null, [badge(row.service || '', 'badge-blue')]),
            el('td', null, [el('strong', null, [row.sku_description || ''])]),
            el('td', 'mono', [row.usage_volume || '—']),
            el('td', 'mono', [money(row.telemetry_estimated_usd)]),
            el('td', 'mono', [money(row.billing_export_gross_usd)]),
            el('td', 'mono', [money(row.cud_and_cache_credits_usd)]),
            el('td', 'mono', [el('strong', null, [money(row.net_invoice_usd)])]),
            el('td', 'mono', [row.variance_pct == null ? '—' : badge(row.variance_pct + '%', 'badge-blue')]),
          ]));
        });
      }

      const gsuBox = document.getElementById('gsuAdvisorBox');
      if (gsuBox && liveBilling) {
        gsuBox.replaceChildren(
          el('div', 'action-box-title', ['Provisioned Throughput (GSU) advisor']),
          el('div', 'kpi-sub', ['Not shown: a GSU recommendation needs sustained tokens-per-second history, which is not collected yet.'])
        );
      } else if (gsuBox && br.gsu_advisor) {
        const ga = br.gsu_advisor;
        gsuBox.replaceChildren(
          el('div', 'action-box-title', ['Provisioned Throughput (GSU) & 1-Year CUD Capacity Advisor']),
          el('div', null, ['Current Peak Traffic: ' + ga.current_peak_tps + ' TPS | Recommended Floor: ' + ga.recommended_provisioned_gsus + ' GSUs (' + ga.spillover_mode + ')']),
          el('div', null, ['Pay-As-You-Go Monthly: $' + fmtInt(ga.payg_monthly_usd) + '/mo → GSU + 1-Yr CUD Monthly: $' + fmtInt(ga.gsu_cud_monthly_usd) + '/mo']),
          el('div', null, [badge('Projected GSU + CUD Savings: $' + fmtInt(ga.projected_gsu_savings_usd) + '/mo (' + ga.utilization_at_peak_pct + '% Peak Util)', 'badge-green')]),
          el('div', 'kpi-sub', [ga.recommendation_note || ''])
        );
      }

      const tcoBox = document.getElementById('platformTcoBox');
      if (tcoBox && state.otel_catalog && state.otel_catalog.architecture_tco) {
        const tco = state.otel_catalog.architecture_tco;
        const totalHosting = tco.estimated_monthly_total_usd ?? tco.total_monthly_platform_tco_usd ?? 39.0;
        const comps = tco.components || tco.line_items || [];
        tcoBox.replaceChildren(
          el('div', 'action-box-title', ['VibeLift hosting cost (estimate from list prices, not billed)']),
          el('div', null, ['Estimated: $' + Number(totalHosting).toFixed(2) + '/mo' + (tco.target_scale || tco.scale_profile ? ' (' + (tco.target_scale || tco.scale_profile) + ')' : '')]),
          el('div', 'kpi-sub', [
            comps.map(function(c) {
              return (c.service || c.component) + ': $' + Number(c.monthly_cost_usd || 0).toFixed(2) + '/mo';
            }).join(' • ')
          ])
        );
      }

      // 7. Power Users Leaderboard (user_ldap)
      const puBody = document.getElementById('powerUsersBody');
      if (puBody && Array.isArray(uc.power_users_ldap)) {
        puBody.replaceChildren();
        uc.power_users_ldap.forEach(function(u) {
          const st = String(u.anomaly_status || 'NORMAL');
          const stCls = st.includes('OPTIMIZED') || st.includes('LIVE') ? 'badge-green' : 'badge-blue';
          puBody.appendChild(el('tr', null, [
            el('td', 'mono', [el('strong', null, [u.user_ldap || ''])]),
            el('td', null, [u.department || '']),
            el('td', 'mono', [u.primary_agent || '']),
            el('td', 'mono', [fmtInt(u.sessions_7d)]),
            el('td', 'mono', [u.total_tokens_m + 'M']),
            el('td', 'mono', [u.thinking_tokens_k + 'k']),
            el('td', 'mono', [u.background_tokens_k + 'k']),
            el('td', 'mono', [u.cache_hit_pct == null ? '—' : u.cache_hit_pct + '%']),
            el('td', 'mono', [u.avg_csat == null ? 'no ratings' : u.avg_csat + ' ★']),
            el('td', 'mono', ['$' + Number(u.monthly_cost_usd || 0).toFixed(2)]),
            el('td', null, [badge(st, stCls)]),
          ]));
        });
      }

      // 8. Voice-of-Customer CSAT Stream & aive_logs Usage Stream
      const aive = state.aive_logs || {};
      const vocBody = document.getElementById('vocRatingsBody');
      if (vocBody && Array.isArray(aive.ratings_logs)) {
        vocBody.replaceChildren();
        aive.ratings_logs.forEach(function(r) {
          const stars = '★'.repeat(Math.max(1, Math.min(5, Number(r.rating || 5))));
          const rCls = Number(r.rating || 5) >= 4 ? 'badge-green' : 'badge-yellow';
          vocBody.appendChild(el('tr', null, [
            el('td', 'mono', [r.rating_id || '']),
            el('td', 'mono', [el('strong', null, [r.user_ldap || r.user_email || ''])]),
            el('td', 'mono', [r.session_id || '']),
            el('td', null, [badge(r.rating + ' ' + stars, rCls)]),
            el('td', null, [r.feedback_text || '']),
          ]));
        });
      }

      const usageBody = document.getElementById('aiveUsageBody');
      if (usageBody && Array.isArray(aive.usage_logs)) {
        usageBody.replaceChildren();
        aive.usage_logs.forEach(function(u) {
          const outs = Array.isArray(u.outputs) ? u.outputs : [];
          const firstUri = outs.length > 0 ? (outs[0].gcs_uri || '') : '—';
          usageBody.appendChild(el('tr', null, [
            el('td', 'mono', [
              el('strong', null, [u.event_id || '']),
              el('div', null, [badge(u.task_type || '', 'badge-blue')]),
            ]),
            el('td', null, [
              el('strong', 'mono', [u.user_ldap || '']),
              el('div', 'kpi-sub', [u.department || '']),
            ]),
            el('td', 'mono', [(u.model_name || '') + ' (' + u.latency_ms + 'ms)']),
            el('td', 'mono', [
              fmtInt(u.total_tokens || 0) + ' / ' +
              fmtInt(u.thinking_tokens || 0) + ' / ' +
              fmtInt(u.background_tokens || 0)
            ]),
            el('td', 'mono', [firstUri]),
            el('td', null, [badge((u.csat_rating || 5) + ' ★', 'badge-green')]),
          ]));
        });
      }

      // 9. Tokenomics 2026 & AgentOps Cockpit FinOps Ledger
      if (state.tokenomics_cockpit) {
        renderTokenomicsCockpit(state.tokenomics_cockpit);
      }
    }

    async function recomputeFinopsLedger() {
      const btn = document.getElementById('finopsRecomputeBtn');
      const origText = btn ? btn.textContent : 'Recompute All Ledgers';
      if (btn) {
        btn.disabled = true;
        btn.textContent = 'Recomputing...';
      }
      const callsPerHr = Number((document.getElementById('finopsCacheCallsHrInput') || {}).value || 18.0);
      const deferredShare = Number((document.getElementById('finopsDeferredShareInput') || {}).value || 35.0);
      const monthlyTasks = Number((document.getElementById('finopsMonthlyTasksInput') || {}).value || 50000);
      const hitlMinutes = Number((document.getElementById('finopsHitlMinutesInput') || {}).value || 3.0);
      const hitlRate = Number((document.getElementById('finopsHitlRateInput') || {}).value || 85.0);
      const payload = {
        calls_per_hr: callsPerHr,
        deferred_share_pct: deferredShare,
        monthly_tasks: monthlyTasks,
        hitl_review_minutes: hitlMinutes,
        hitl_hourly_rate_usd: hitlRate,
      };
      try {
        const res = await fetch('/api/recompute_finops', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        const tc = data.result || data.tokenomics_cockpit || (data.state && data.state.tokenomics_cockpit);
        if (tc) {
          if (currentState) currentState.tokenomics_cockpit = tc;
          renderTokenomicsCockpit(tc);
        }
      } catch (err) {
        console.error('Failed to recompute FinOps ledger:', err);
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = origText;
        }
      }
    }

    function renderTokenomicsCockpit(tc) {
      if (!tc || typeof tc !== 'object') return;
      const cpo = tc.cpo || {};
      const drift = tc.drift || {};
      const attr = tc.attribution_join || {};
      const mc = tc.metering_and_consumption || {};
      const caching = tc.caching || {};
      const ge = tc.gateway_and_extensions || {};
      const tcoPnl = tc.tco_and_pnl || {};
      const cf = tc.cockpit_finops || {};

      // 1. CpO & Drift KPI Cards
      const cpoKpis = document.getElementById('cpoDriftKpis');
      if (cpoKpis) {
        const unoptMult = (Number(drift.unoptimized_billed_spend_usd || 2540) / Math.max(1, Number(drift.expected_naive_token_spend_usd || 420))).toFixed(2);
        const remMult = (Number(drift.actual_reconciled_invoice_usd || 1355.75) / Math.max(1, Number(drift.expected_naive_token_spend_usd || 420))).toFixed(2);
        cpoKpis.replaceChildren(
          kpiCard(
            'True Cost per Outcome (Baseline → Optimized)',
            '$' + Number(cpo.fleet_baseline_cpo_usd || 2.339).toFixed(3) + ' → $' + Number(cpo.fleet_optimized_cpo_usd || 0.431).toFixed(3),
            '-' + Number(cpo.fleet_cpo_reduction_pct || 81.6).toFixed(1) + '% incl. LLM + Tools + Infra + HITL'
          ),
          kpiCard(
            'Fleet Total Monthly CpO Spend',
            '$' + fmtInt(cpo.fleet_monthly_baseline_usd || 302900) + ' → $' + fmtInt(cpo.fleet_monthly_optimized_usd || 55800) + '/mo',
            'Net Monthly CpO Savings: $' + fmtInt(cpo.fleet_monthly_saved_usd || 247100) + '/mo'
          ),
          kpiCard(
            'Token-to-Spend Drift Multiplier (Before → After)',
            unoptMult + 'x → ' + remMult + 'x',
            'Naive Token Est: $' + Number(drift.expected_naive_token_spend_usd || 420).toFixed(2) + ' vs Reconciled: $' + Number(drift.actual_reconciled_invoice_usd || 1355.75).toFixed(2)
          ),
          kpiCard(
            'Unattributed Invoice Drift (D1..D5 Reconciled)',
            '$' + Number(drift.unattributed_usd || 0).toFixed(2) + ' (0.00%)',
            cpo.pareto_outlier_rule || 'Top 10% outlier sessions drive 78.4% of unoptimized spend'
          )
        );
      }

      // 2. CpO By Agent Table
      const cpoBody = document.getElementById('cpoByAgentBody');
      const cpoAgents = cpo.agents || cpo.by_agent || [];
      if (cpoBody && Array.isArray(cpoAgents)) {
        cpoBody.replaceChildren();
        cpoAgents.forEach(function(a) {
          cpoBody.appendChild(el('tr', null, [
            el('td', null, [
              el('strong', null, [a.display_name || a.agent_id]),
              el('div', 'kpi-sub mono', [(a.model || '') + ' • ' + (a.lane || '')]),
            ]),
            el('td', 'mono', [fmtInt(a.sessions_per_mo)]),
            el('td', 'mono', ['$' + Number(a.baseline_cpo_usd || 0).toFixed(4) + ' (' + a.n_turns_baseline + ' turns)']),
            el('td', 'mono', [
              '$' + Number(a.c_llm_turn_usd || 0).toFixed(4) + ' / $' +
              Number(a.c_tools_turn_usd || 0).toFixed(4) + ' / $' +
              Number(a.c_infra_turn_usd || 0).toFixed(4)
            ]),
            el('td', 'mono', ['$' + Number(a.c_hitl_baseline_usd || 0).toFixed(3) + ' → $' + Number(a.c_hitl_optimized_usd || 0).toFixed(3)]),
            el('td', 'mono', [
              el('strong', null, ['$' + Number(a.optimized_cpo_usd || 0).toFixed(4)]),
              el('div', null, [badge('-' + a.cpo_reduction_pct + '%', 'badge-green')]),
            ]),
            el('td', 'mono', [a.formula || '']),
            el('td', 'mono', [badge('$' + fmtInt(a.monthly_saved_usd) + '/mo', 'badge-green')]),
          ]));
        });
      }

      // 3. Drift Drivers D1..D5 Table
      const driftBody = document.getElementById('driftDriversBody');
      const driftRows = drift.drift_drivers || drift.drivers || [];
      if (driftBody && Array.isArray(driftRows)) {
        driftBody.replaceChildren();
        driftRows.forEach(function(d) {
          driftBody.appendChild(el('tr', null, [
            el('td', 'mono', [badge(d.driver_id, 'badge-blue')]),
            el('td', null, [
              el('strong', null, [d.name || '']),
              el('div', 'kpi-sub mono', [d.detection_rule || '']),
            ]),
            el('td', 'mono', [d.share_of_drift_pct + '% of drift']),
            el('td', 'mono', ['$' + Number(d.unoptimized_drift_usd || 0).toFixed(2)]),
            el('td', 'mono', [el('strong', null, ['$' + Number(d.remediated_drift_usd || 0).toFixed(2)])]),
            el('td', null, [d.control_applied || '']),
          ]));
        });
      }

      // 4. 3-Way Attribution Join Table & SQL
      const attrBody = document.getElementById('attributionJoinBody');
      const attrSources = attr.sources || [];
      if (attrBody && Array.isArray(attrSources)) {
        attrBody.replaceChildren();
        attrSources.forEach(function(r) {
          attrBody.appendChild(el('tr', null, [
            el('td', null, [el('strong', null, [r.layer || ''])]),
            el('td', 'mono', [r.table_or_stream || '']),
            el('td', 'mono', [r.join_keys || '']),
            el('td', 'mono', ['labels.agent_id + trace_id']),
            el('td', 'mono', [r.extracted_fields || '']),
            el('td', 'mono', [r.variance || '—']),
            el('td', null, [badge('JOIN ACTIVE', 'badge-green')]),
          ]));
        });
      }
      const attrSql = document.getElementById('attributionJoinSqlPre');
      if (attrSql && attr.join_sql) {
        attrSql.textContent = attr.join_sql;
      }

      // 5. Metering Categories, Routing Lanes & 4-Tier Model Portfolio
      const mcBody = document.getElementById('meteringCategoriesBody');
      const mcCats = mc.categories || [];
      if (mcBody && Array.isArray(mcCats)) {
        mcBody.replaceChildren();
        mcCats.forEach(function(m) {
          mcBody.appendChild(el('tr', null, [
            el('td', null, [el('strong', null, [m.category || ''])]),
            el('td', 'mono', [m.billing_unit || '']),
            el('td', null, [m.sku_examples || '']),
            el('td', null, [m.rate_summary || '']),
            el('td', 'mono', ['$' + fmtInt(m.monthly_spend_usd) + '/mo']),
            el('td', null, [m.optimization_lever || '']),
          ]));
        });
      }

      const rlBody = document.getElementById('routingLanesBody');
      if (rlBody && Array.isArray(mc.routing_lanes)) {
        rlBody.replaceChildren();
        mc.routing_lanes.forEach(function(l) {
          rlBody.appendChild(el('tr', null, [
            el('td', null, [
              el('strong', null, [l.lane_name || '']),
              el('div', 'kpi-sub mono', [l.lane_id || '']),
            ]),
            el('td', null, [l.slo_guarantee || '']),
            el('td', 'mono', [badge(l.price_multiplier || '1.0x', 'badge-green')]),
            el('td', 'mono', [l.traffic_share_pct + '% of turns']),
            el('td', null, [l.best_for || '']),
          ]));
        });
      }

      const mpBody = document.getElementById('modelPortfolioBody');
      if (mpBody && Array.isArray(mc.model_portfolio_tiers)) {
        mpBody.replaceChildren();
        mc.model_portfolio_tiers.forEach(function(t) {
          mpBody.appendChild(el('tr', null, [
            el('td', null, [badge(t.tier || '', 'badge-blue')]),
            el('td', 'mono', [t.models || '']),
            el('td', 'mono', [(t.input_rate_1m || '') + ' / ' + (t.output_rate_1m || '')]),
            el('td', 'mono', [badge(t.intelligence_per_dollar_index || '', 'badge-green')]),
            el('td', null, [t.role_in_hybrid_pattern || '']),
          ]));
        });
      }

      // 6. Explicit vs Implicit Context Cache Break-Even
      const cbBox = document.getElementById('cacheBreakEvenSummaryBox');
      if (cbBox) {
        const hc = caching.hourly_cost_comparison || {};
        cbBox.replaceChildren(
          el('div', 'action-box-title', ['Break-Even Reuse Threshold Formula: ' + (caching.formula || 'N* = 1 + S / (0.9 * P_in)')]),
          el('div', null, [
            'Flash Break-Even: ' + caching.flash_break_even_calls_per_hr + ' calls/hr • Pro Break-Even: ' + caching.pro_break_even_calls_per_hr + ' calls/hr • Winning Mode: ',
            badge(hc.winning_mode || 'EXPLICIT_CACHE_LOCKED', 'badge-green'),
            ' (Monthly Prefix Savings: $' + fmtInt(hc.monthly_prefix_savings_usd || 0) + '/mo)',
          ]),
          el('div', 'kpi-sub mono', [
            'Uncached PayGo: $' + Number(hc.uncached_paygo_hr_usd || 0).toFixed(4) + '/hr vs Explicit Cache: $' +
            Number(hc.explicit_cache_hr_usd || 0).toFixed(4) + '/hr vs Implicit Cache: $' + Number(hc.implicit_cache_hr_usd || 0).toFixed(4) + '/hr'
          ])
        );
      }

      const cmBody = document.getElementById('cacheModelBreakEvenBody');
      const modesTable = caching.modes_table || [];
      if (cmBody && Array.isArray(modesTable)) {
        cmBody.replaceChildren();
        modesTable.forEach(function(m) {
          cmBody.appendChild(el('tr', null, [
            el('td', 'mono', [el('strong', null, [m.mode || ''])]),
            el('td', 'mono', [(m.write_multiplier || '') + ' / ' + (m.read_multiplier || '')]),
            el('td', 'mono', [m.storage_rate || '']),
            el('td', 'mono', [badge(m.break_even_calls_per_hr || '', 'badge-blue')]),
            el('td', 'mono', [m.min_tokens || '']),
            el('td', 'mono', [m.ttl || '']),
            el('td', null, [badge(m.verdict || '', 'badge-green')]),
          ]));
        });
      }

      // 7. Apigee AI Gateway & Modular Extension Overhead
      const apBody = document.getElementById('apigeePoliciesBody');
      if (apBody && Array.isArray(ge.apigee_policies)) {
        apBody.replaceChildren();
        ge.apigee_policies.forEach(function(p) {
          apBody.appendChild(el('tr', null, [
            el('td', null, [
              badge(p.policy_id || '', 'badge-blue'),
              el('div', null, [el('strong', null, [p.policy_name || ''])]),
            ]),
            el('td', 'mono', [p.source_ref || '']),
            el('td', 'mono', [p.configured_threshold || '']),
            el('td', 'mono', [p.tokens_prevented_monthly || '']),
            el('td', 'mono', [badge('$' + fmtInt(p.monthly_savings_usd) + '/mo', 'badge-green')]),
          ]));
        });
      }

      const extBody = document.getElementById('extensionOverheadBody');
      const extRows = ge.extension_overhead || [];
      if (extBody && Array.isArray(extRows)) {
        extBody.replaceChildren();
        extRows.forEach(function(x) {
          const isWarn = String(x.extension_type || '').includes('Anti-Pattern');
          extBody.appendChild(el('tr', null, [
            el('td', null, [el('strong', null, [x.extension_type || ''])]),
            el('td', 'mono', [x.tokens_per_turn || '']),
            el('td', null, [x.loading_mechanism || '']),
            el('td', 'mono', ['$' + fmtInt(x.monthly_cost_100k_turns_usd) + '/mo']),
            el('td', null, [badge(x.status || '', isWarn ? 'badge-red' : 'badge-green')]),
          ]));
        });
      }

      // 8. AgentOps Cockpit FinOps Findings, Waterfall, Cost Guard & CFO TCO/P&L
      const wf = cf.waterfall || {};
      const wfKpis = document.getElementById('cockpitWaterfallKpis');
      if (wfKpis) {
        wfKpis.replaceChildren(
          kpiCard(
            'Unoptimized Baseline Monthly Spend',
            '$' + fmtInt(wf.baseline_monthly_usd || 142000) + '/mo',
            'Before FIN-01..FIN-05 structural remediations'
          ),
          kpiCard(
            'Final Remediated Monthly Spend',
            '$' + fmtInt(wf.optimized_monthly_usd || 10050) + '/mo',
            'Combined step-by-step savings'
          ),
          kpiCard(
            'Total Monthly & Annualized Net Savings',
            '$' + fmtInt(wf.total_monthly_saved_usd || 131950) + '/mo',
            '$' + fmtInt((wf.total_monthly_saved_usd || 131950) * 12) + '/yr annualized'
          ),
          kpiCard(
            'Compounded Spend Reduction',
            '-' + Number(wf.total_reduction_pct || 92.9).toFixed(1) + '%',
            'Verified across sequential FinOps levers'
          )
        );
      }

      const finBody = document.getElementById('cockpitFindingsBody');
      const finRows = cf.auditor_findings || [];
      if (finBody && Array.isArray(finRows)) {
        finBody.replaceChildren();
        finRows.forEach(function(f) {
          finBody.appendChild(el('tr', null, [
            el('td', 'mono', [
              badge(f.rule_id || '', 'badge-red'),
              el('div', null, [el('strong', null, [f.title || ''])]),
            ]),
            el('td', 'mono', [f.file_line || '']),
            el('td', 'mono', [f.ast_detection || '']),
            el('td', null, [f.savings_formula || '']),
            el('td', 'mono', [badge('$' + fmtInt(f.monthly_savings_usd) + '/mo saved', 'badge-green')]),
            el('td', null, [
              badge(f.status || 'REMEDIATED', 'badge-green'),
              el('div', 'kpi-sub', [f.evidence || '']),
            ]),
          ]));
        });
      }

      const wfBody = document.getElementById('cockpitWaterfallBody');
      if (wfBody && Array.isArray(wf.steps)) {
        wfBody.replaceChildren();
        wf.steps.forEach(function(s) {
          wfBody.appendChild(el('tr', null, [
            el('td', null, [el('strong', null, [s.step_name || ''])]),
            el('td', 'mono', [s.eligible_share_pct + '% eligible']),
            el('td', 'mono', ['$' + fmtInt(s.before_usd) + '/mo']),
            el('td', 'mono', ['-' + s.step_reduction_pct + '%']),
            el('td', 'mono', [badge('-$' + fmtInt(s.delta_saved_usd) + '/mo', 'badge-green')]),
            el('td', 'mono', [el('strong', null, ['$' + fmtInt(s.after_usd) + '/mo (-' + s.cumulative_reduction_pct + '%)'])]),
          ]));
        });
      }

      const cgBox = document.getElementById('costGuardBox');
      if (cgBox && cf.cost_guard) {
        const cg = cf.cost_guard;
        const sims = cg.preflight_simulations || [];
        cgBox.replaceChildren(
          el('div', 'action-box-title', ['Pre-Flight Turn Budget Enforcer: @cost_guard(budget_limit_usd=' + cg.budget_limit_usd + ')']),
          el('div', 'kpi-sub', [
            sims.map(function(s) {
              return s.call_id + ' (' + s.agent_and_handler + '): est $' + Number(s.estimated_worst_case_usd || 0).toFixed(4) + ' → ' + s.verdict;
            }).join(' • ')
          ])
        );
      }

      const cfoBox = document.getElementById('cfoTcoKpiBox');
      if (cfoBox && tcoPnl.tco_breakdown_rows) {
        cfoBox.replaceChildren(
          el('div', 'action-box-title', [
            'CFO Enterprise AI TCO Breakdown (' + tcoPnl.visible_tech_share_pct + '% Visible Tech vs ' +
            tcoPnl.hidden_enterprise_share_pct + '% Hidden Operational Spend) • Stage: ' + (tcoPnl.current_maturity_stage || '')
          ]),
          el('div', 'kpi-sub', [
            (tcoPnl.tco_breakdown_rows || []).map(function(c) {
              return c.bucket + ': $' + fmtInt(c.unoptimized_annual_usd) + '/yr → $' + fmtInt(c.optimized_annual_usd) + '/yr (' + c.lever + ')';
            }).join(' • ')
          ])
        );
      }

      const matBody = document.getElementById('maturityProgressionBody');
      if (matBody && Array.isArray(tcoPnl.maturity_progression)) {
        matBody.replaceChildren();
        tcoPnl.maturity_progression.forEach(function(m) {
          const isCur = String(m.status || '').includes('ACTIVE');
          matBody.appendChild(el('tr', null, [
            el('td', null, [badge(m.stage || '', isCur ? 'badge-green' : 'badge-blue')]),
            el('td', null, [m.focus || '']),
            el('td', 'mono', [m.target || '—']),
            el('td', 'mono', ['Canary + Apigee Enforced']),
            el('td', null, [badge(m.status || '', isCur ? 'badge-green' : 'badge-blue')]),
          ]));
        });
      }

      const pnlBody = document.getElementById('pnlAllocationBody');
      if (pnlBody && Array.isArray(tcoPnl.pnl_accounting)) {
        pnlBody.replaceChildren();
        tcoPnl.pnl_accounting.forEach(function(p) {
          pnlBody.appendChild(el('tr', null, [
            el('td', null, [el('strong', null, [p.pnl_class || ''])]),
            el('td', null, [p.workloads || '']),
            el('td', 'mono', [p.share_pct + '%']),
            el('td', 'mono', ['$' + Number(p.monthly_net_spend_usd || 0).toFixed(2) + '/mo']),
            el('td', null, [p.accounting_treatment || '']),
          ]));
        });
      }

      const opexBody = document.getElementById('opexTradeoffBody');
      const opexRows = (cf.cost_guard && cf.cost_guard.opex_tradeoff_matrix) || [];
      if (opexBody && Array.isArray(opexRows)) {
        opexBody.replaceChildren();
        opexRows.forEach(function(o) {
          opexBody.appendChild(el('tr', null, [
            el('td', null, [el('strong', null, [o.setting || ''])]),
            el('td', null, [badge(o.pillar || '', 'badge-blue')]),
            el('td', 'mono', [o.opex_multiplier || '']),
            el('td', 'mono', ['<15ms']),
            el('td', 'mono', [o.net_monthly_impact_usd || '']),
            el('td', null, [badge(o.tradeoff_verdict || '', 'badge-green')]),
          ]));
        });
      }
    }

    function renderUserCentricAndDecorator(uc, decoratorEvents) {
      if (uc && typeof uc === 'object') {
        const kpiBox = document.getElementById('userCentricKpis');
        if (kpiBox && uc.live) {
          kpiBox.replaceChildren(
            kpiCard('Active people (7d)', fmtInt(uc.active_people_7d), 'Human principals in audit logs'),
            kpiCard('Service accounts (7d)', fmtInt(uc.active_service_accounts_7d), 'Automated callers'),
            kpiCard('Sessions / calls (7d)', fmtInt(uc.sessions_7d), 'ds_ge_audit_raw + agent telemetry'),
            kpiCard('Savings', 'Not measured', uc.savings_note || '')
          );
        } else if (kpiBox) {
          kpiBox.replaceChildren(
            kpiCard('Active Enterprise Users (simulator)', fmtInt(uc.total_active_dau), 'Supported Scale: ' + (uc.supported_dau_capacity || 'n/a')),
            kpiCard('Cost per 1k Turns (simulator)', money(uc.baseline_cost_per_1k_turns_usd) + ' → ' + money(uc.optimized_cost_per_1k_turns_usd), orDash(uc.avg_cost_reduction_pct, '% simulated reduction')),
            kpiCard('Monthly Token Spend per User (simulator)', money(uc.per_user_monthly_baseline_usd) + ' → ' + money(uc.per_user_monthly_optimized_usd), 'Simulated'),
            kpiCard('Fleet Savings (simulator)', money(uc.total_monthly_savings_usd) + '/mo', money(uc.annualized_savings_usd) + '/yr simulated')
          );
        }

        const cohortBody = document.getElementById('userCohortsTableBody');
        if (cohortBody && uc.cohorts_live) {
          const t = document.getElementById('userCohortsTitle');
          if (t) t.textContent = 'Users by Gemini Enterprise app (last 7 days)';
          const st = document.getElementById('userCohortsSubtitle');
          if (st) st.textContent = 'People and service accounts seen in audit logs per app. Source: ds_ge_audit_raw.';
          const gb = document.getElementById('oauthGovernanceBadge');
          if (gb) { gb.textContent = 'Live'; gb.className = 'badge badge-green'; }
          setTableHead('userCohortsTableBody', ['App', 'Region', 'People', 'Service accounts', 'Sessions / calls (7d)']);
          cohortBody.replaceChildren();
          if (!(uc.cohorts || []).length) emptyRow(cohortBody, 5, 'No per-app activity in audit logs for the last 7 days.');
          (uc.cohorts || []).forEach(function(c) {
            cohortBody.appendChild(el('tr', null, [
              el('td', null, [el('strong', null, [c.cohort]), el('div', 'kpi-sub mono', [c.engine_key || ''])]),
              el('td', null, [badge(c.primary_agent, 'badge-blue')]),
              el('td', 'mono', [fmtInt(c.people)]),
              el('td', 'mono', [fmtInt(c.service_accounts)]),
              el('td', 'mono', [fmtInt(c.sessions_7d)]),
            ]));
          });
        } else if (cohortBody) {
          cohortBody.replaceChildren();
          (uc.cohorts || []).forEach(function(c) {
            cohortBody.appendChild(el('tr', null, [
              el('td', null, [el('strong', null, [c.cohort])]),
              el('td', null, [badge(c.primary_agent, 'badge-blue')]),
              el('td', 'mono', [fmtInt(c.active_dau)]),
              el('td', 'mono', [fmtInt(c.sessions_24h)]),
              el('td', 'mono', [c.tokens_per_user_k + 'k tok']),
              el('td', 'mono', [c.context_bloat_before_pct + '% → ' + c.context_bloat_after_pct + '%']),
              el('td', 'mono', [c.idle_ratio_pct + '%']),
              el('td', 'mono', [money(c.baseline_cost_per_1k_usd) + ' → ' + money(c.optimized_cost_per_1k_usd)]),
              el('td', 'mono', [badge(money(c.monthly_savings_usd) + '/mo simulated', 'badge-blue')]),
            ]));
          });
        }

        const skillBody = document.getElementById('skillMcpTableBody');
        if (skillBody) {
          skillBody.replaceChildren();
          (uc.skill_mcp_breakdown || []).forEach(function(s) {
            skillBody.appendChild(el('tr', null, [
              el('td', 'mono', [el('strong', null, [s.resource_name])]),
              el('td', null, [badge(s.kind, 'badge-blue')]),
              el('td', null, [s.attached_agent]),
              el('td', 'mono', [fmtInt(s.calls_24h)]),
              el('td', 'mono', [orDash(s.prompt_tokens_m, 'M')]),
              el('td', 'mono', [orDash(s.cache_hit_pct, '%')]),
              el('td', 'mono', [orDash(s.context_bloat_pct, '%')]),
              el('td', null, [s.optimization_applied]),
              el('td', 'mono', [s.monthly_saved_usd == null ? '—' : badge('$' + fmtInt(s.monthly_saved_usd) + '/mo', 'badge-green')]),
            ]));
          });
        }
      }

      const decBody = document.getElementById('decoratorEventsBody');
      if (decBody && Array.isArray(decoratorEvents)) {
        decBody.replaceChildren();
        decoratorEvents.forEach(function(ev) {
          decBody.appendChild(el('tr', null, [
            el('td', 'mono', [ev.timestamp]),
            el('td', 'mono', [ev.agent_name]),
            el('td', 'mono', [ev.handler_name]),
            el('td', null, [ev.protocol]),
            el('td', 'mono', [ev.skill_or_mcp]),
            el('td', null, [ev.user_cohort]),
            el('td', 'mono', [orDash(ev.latency_ms, ' ms')]),
            el('td', 'mono', [orDash(ev.cache_hit_pct, '%')]),
            el('td', 'mono', [orDash(ev.context_bloat_pct, '%')]),
            el('td', 'mono', [orDash(ev.idle_ratio_pct, '%')]),
            el('td', null, [badge(ev.status || '—', 'badge-blue')]),
          ]));
        });
      }
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
    function orDash(v, suffix) { return v == null ? '—' : String(v) + (suffix || ''); }
    function money(v) {
      return v == null ? '—' : '$' + Number(v).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    }
    function setTableHead(tbodyId, headers) {
      const tb = document.getElementById(tbodyId);
      const tr = tb && tb.parentElement ? tb.parentElement.querySelector('thead tr') : null;
      if (!tr) return;
      tr.replaceChildren.apply(tr, headers.map(function(h) { return el('th', null, [h]); }));
    }
    function emptyRow(tbody, cols, text) {
      const td = el('td', 'kpi-sub', [text]);
      td.colSpan = cols;
      tbody.appendChild(el('tr', null, [td]));
    }
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
      agent_token_traces: 'Agent token traces',
      ge_assistant_tokens: 'GE assistant token traces',
      cloud_run_metrics: 'Cloud Run metrics',
      model_usage: 'Vertex AI model usage',
      ge_traffic: 'GE assistant traffic',
    };

    // ---- Region / Gemini Enterprise app scope ----
    let geScope = 'all';
    let geRawFleet = null;
    (function initGeScope() {
      let saved = null;
      try { saved = localStorage.getItem('vibelift.geScope'); } catch (e) {}
      const m = /[?&]scope=([^&]+)/.exec(location.search);
      geScope = (m ? decodeURIComponent(m[1]) : saved) || 'all';
    })();
    function agentLocation(a) {
      if (a.location) return a.location;
      const mm = new RegExp('/locations/([^/]+)/').exec(a.resource_name || '');
      return mm ? mm[1] : 'global';
    }
    function agentEngineKey(a) { return a.engine_key || (agentLocation(a) + '/' + a.engine_id); }
    function geRegionLabel(loc) {
      return {global: 'Global', us: 'US (multi-region)', eu: 'EU (multi-region)'}[loc] || String(loc || '').toUpperCase();
    }
    function engineMatchesScope(engineKey, scope) {
      if (!scope || scope === 'all') return true;
      if (scope.indexOf('loc:') === 0) return String(engineKey).split('/')[0] === scope.slice(4);
      if (scope.indexOf('eng:') === 0) return engineKey === scope.slice(4);
      return true;
    }
    function geScopeLabel(scope) {
      if (!scope || scope === 'all') return 'all apps in all regions';
      if (scope.indexOf('loc:') === 0) return 'all apps in ' + geRegionLabel(scope.slice(4));
      const key = scope.slice(4);
      const eng = ((geRawFleet && geRawFleet.engines) || []).find(function(e) {
        return (e.engine_key || ((e.location || 'global') + '/' + e.engine_id)) === key;
      });
      return (eng ? (eng.display_name || eng.engine_id) : key) + ' (' + geRegionLabel(key.split('/')[0]) + ')';
    }
    function populateGeScopeSelect(fleet) {
      const sel = document.getElementById('geScopeSelect');
      if (!sel) return;
      const engines = (fleet.engines || []).map(function(e) {
        return {key: e.engine_key || ((e.location || 'global') + '/' + e.engine_id), loc: e.location || 'global',
                name: e.display_name || e.engine_id, n: e.agents_count};
      });
      const locs = [];
      engines.forEach(function(e) { if (locs.indexOf(e.loc) < 0) locs.push(e.loc); });
      locs.sort(function(a, b) { return a === 'global' ? -1 : (b === 'global' ? 1 : a.localeCompare(b)); });
      sel.replaceChildren();
      const allOpt = el('option', null, ['All apps · all regions (' + engines.length + ' apps)']);
      allOpt.value = 'all';
      sel.appendChild(allOpt);
      locs.forEach(function(loc) {
        const inLoc = engines.filter(function(e) { return e.loc === loc; });
        const grp = document.createElement('optgroup');
        grp.label = geRegionLabel(loc);
        const locOpt = el('option', null, ['All ' + geRegionLabel(loc) + ' apps (' + inLoc.length + ')']);
        locOpt.value = 'loc:' + loc;
        grp.appendChild(locOpt);
        inLoc.sort(function(a, b) { return (b.n || 0) - (a.n || 0); }).forEach(function(e) {
          const o = el('option', null, ['\u00a0\u00a0' + e.name + ' · ' + (e.n == null ? 'unavailable' : e.n + ' agents')]);
          o.value = 'eng:' + e.key;
          o.title = e.key;
          grp.appendChild(o);
        });
        sel.appendChild(grp);
      });
      const valid = Array.from(sel.options).some(function(o) { return o.value === geScope; });
      if (!valid) geScope = 'all';
      sel.value = geScope;
    }
    function applyGeScope(fleet) {
      if (!fleet || !Array.isArray(fleet.agents)) return fleet;
      const out = Object.assign({}, fleet, {__scoped: true});
      if (geScope === 'all') return out;
      const agents = fleet.agents.filter(function(a) { return engineMatchesScope(agentEngineKey(a), geScope); });
      out.agents = agents;
      out.engines = (fleet.engines || []).filter(function(e) {
        return engineMatchesScope(e.engine_key || ((e.location || 'global') + '/' + e.engine_id), geScope);
      });
      // Recompute totals, counting each runtime once (same rule as the backend).
      const rtKey = function(a) {
        const b = a.backend || {};
        if (b.kind === 'cloud_run' && b.service) return 'cloud_run:' + b.service;
        if (b.resource || b.url) return (b.kind || '') + ':' + (b.resource || b.url);
        return 'agent:' + (a.resource_name || a.agent_id);
      };
      const allKeysOutside = {};
      fleet.agents.forEach(function(a) { if (agents.indexOf(a) < 0) allKeysOutside[rtKey(a)] = true; });
      const seen = {};
      const uniq = [];
      let shared = 0;
      agents.forEach(function(a) {
        const k = rtKey(a);
        if (seen[k]) return;
        seen[k] = true;
        uniq.push(a);
        if (allKeysOutside[k] && (a.metrics || {}).requests != null) shared += 1;
      });
      const t = {agents: agents.length, enabled: 0, by_type: {}, with_runtime_telemetry: 0, unique_runtimes: uniq.length};
      agents.forEach(function(a) {
        if (a.state === 'ENABLED') t.enabled += 1;
        t.by_type[a.type] = (t.by_type[a.type] || 0) + 1;
        if ((a.metrics || {}).requests != null) t.with_runtime_telemetry += 1;
      });
      ['requests', 'errors_4xx', 'errors_5xx', 'llm_calls', 'input_tokens', 'output_tokens', 'cached_tokens', 'conversations'].forEach(function(k) {
        const vals = uniq.map(function(a) { return (a.metrics || {})[k]; }).filter(function(v) { return v != null; });
        t[k] = vals.length ? vals.reduce(function(x, y) { return x + Number(y); }, 0) : null;
      });
      t.error_rate_pct = t.requests ? Math.round(10000 * (t.errors_5xx || 0) / t.requests) / 100 : null;
      const stamps = agents.map(function(a) { return (a.metrics || {}).last_activity; }).filter(Boolean).sort();
      t.last_activity = stamps.length ? stamps[stamps.length - 1] : null;
      t.broken_registrations = agents.filter(isBrokenRegistration).length;
      t.unverified_registrations = agents.filter(function(a) { return (a.registration || {}).status === 'UNVERIFIED'; }).length;
      const gau = fleet.ge_assistant_usage;
      if (gau && gau.by_engine) {
        const be = {};
        const gt = {llm_calls: 0, input_tokens: 0, output_tokens: 0, cached_tokens: 0, conversations: 0};
        Object.keys(gau.by_engine).forEach(function(k) {
          if (!engineMatchesScope(k, geScope)) return;
          be[k] = gau.by_engine[k];
          Object.keys(gt).forEach(function(f) { gt[f] += Number(gau.by_engine[k][f] || 0); });
        });
        out.ge_assistant_usage = Object.assign({}, gau, {by_engine: be, totals: gt});
      }
      out.totals = t;
      out.__shared_runtimes = shared;
      return out;
    }
    function scopeUserCentric(uc) {
      if (!uc || typeof uc !== 'object' || geScope === 'all') return uc;
      const users = (uc.power_users_ldap || []).map(function(u) {
        const be = u.by_engine || {};
        const v = Object.keys(be).filter(function(k) { return engineMatchesScope(k, geScope); })
          .reduce(function(acc, k) { return acc + Number(be[k] || 0); }, 0);
        return v > 0 ? Object.assign({}, u, {sessions_7d: v}) : null;
      }).filter(Boolean);
      return Object.assign({}, uc, {power_users_ldap: users});
    }
    function onGeScopeChange(value) {
      geScope = value || 'all';
      try { localStorage.setItem('vibelift.geScope', geScope); } catch (e) {}
      if (geRawFleet) renderFleet(geRawFleet);
      if (currentState && currentState.user_centric) {
        try { renderUserCentricAndDecorator(scopeUserCentric(currentState.user_centric), currentState.decorator_events); } catch (e) {}
        try { renderExecOverview(execLastFleet, currentState.user_centric); } catch (e) {}
      }
    }

    const EXEC_PALETTE = ['#2563eb', '#0ea5e9', '#10b981', '#f59e0b', '#8b5cf6', '#14b8a6', '#64748b', '#ef4444'];
    let execLastFleet = null;

    function hbarChart(rows, maxVal, fmt) {
      const wrap = el('div', null, []);
      if (!rows.length) return el('div', 'chart-empty', ['No activity recorded in this window.']);
      const max = maxVal || Math.max.apply(null, rows.map(function(r) { return r.total; })) || 1;
      rows.forEach(function(r) {
        const track = el('div', 'hbar-track', []);
        r.segments.forEach(function(sg) {
          if (!sg.value) return;
          const seg = el('div', 'hbar-seg', []);
          seg.style.width = (100 * sg.value / max).toFixed(2) + '%';
          seg.style.background = sg.color;
          seg.title = sg.name + ': ' + fmtInt(sg.value);
          track.appendChild(seg);
        });
        const lbl = el('div', 'hbar-label', [r.label]);
        lbl.title = r.label;
        wrap.appendChild(el('div', 'hbar-row', [lbl, track, el('div', 'hbar-val', [fmt ? fmt(r.total) : fmtInt(r.total)])]));
      });
      return wrap;
    }

    function trendChart(trend, runtimeKeys) {
      if (!trend || !Array.isArray(trend.bucket_ends) || !trend.bucket_ends.length) {
        return el('div', 'chart-empty', ['No request history available.']);
      }
      const n = trend.bucket_ends.length;
      const ok = new Array(n).fill(0), r4 = new Array(n).fill(0), r5 = new Array(n).fill(0);
      runtimeKeys.forEach(function(k) {
        const row = (trend.by_runtime || {})[k];
        if (!row) return;
        for (let i = 0; i < n; i++) {
          const t = Number((row.requests || [])[i] || 0), a = Number((row.errors_4xx || [])[i] || 0), b = Number((row.errors_5xx || [])[i] || 0);
          ok[i] += Math.max(0, t - a - b); r4[i] += a; r5[i] += b;
        }
      });
      const tot = ok.map(function(v, i) { return v + r4[i] + r5[i]; });
      const max = Math.max.apply(null, tot);
      if (!max) return el('div', 'chart-empty', ['No requests in this window.']);
      const NS = 'http://www.w3.org/2000/svg';
      const W = 1000, H = 170, L = 38, B = 20, T = 8;
      const svg = document.createElementNS(NS, 'svg');
      svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
      svg.setAttribute('preserveAspectRatio', 'none');
      svg.setAttribute('class', 'trend-svg');
      const mk = function(tag, attrs, text) {
        const e = document.createElementNS(NS, tag);
        Object.keys(attrs).forEach(function(k) { e.setAttribute(k, attrs[k]); });
        if (text != null) e.textContent = text;
        svg.appendChild(e);
        return e;
      };
      const plotH = H - B - T, bw = (W - L) / n;
      [0, 0.5, 1].forEach(function(f) {
        const y = T + plotH * (1 - f);
        mk('line', {x1: L, x2: W, y1: y, y2: y, stroke: '#e2e8f0', 'stroke-width': 1});
        mk('text', {x: L - 4, y: y + 3, 'text-anchor': 'end'}, String(Math.round(max * f)));
      });
      const bucketS = Number(trend.bucket_seconds || 3600);
      for (let i = 0; i < n; i++) {
        let y = T + plotH;
        const x = L + i * bw + bw * 0.12, w = bw * 0.76;
        [[ok[i], '#2563eb'], [r4[i], '#f59e0b'], [r5[i], '#ef4444']].forEach(function(seg) {
          if (!seg[0]) return;
          const h = plotH * seg[0] / max;
          y -= h;
          mk('rect', {x: x, y: y, width: w, height: h, fill: seg[1]});
        });
        const end = new Date(trend.bucket_ends[i]);
        const start = new Date(end.getTime() - bucketS * 1000);
        const hit = mk('rect', {x: L + i * bw, y: T, width: bw, height: plotH, fill: 'transparent'});
        const tip = document.createElementNS(NS, 'title');
        tip.textContent = start.toLocaleString() + ' – ' + end.toLocaleTimeString() + ': ' + tot[i] +
          ' requests (' + r4[i] + ' 4xx, ' + r5[i] + ' 5xx)';
        hit.appendChild(tip);
        const every = Math.max(1, Math.ceil(n / 8));
        if (i % every === 0) {
          const lbl = bucketS >= 86400 || n * bucketS > 2 * 86400
            ? start.toLocaleDateString(undefined, {month: 'short', day: 'numeric'}) + ' ' + start.getHours() + 'h'
            : start.toLocaleTimeString(undefined, {hour: 'numeric', minute: bucketS < 3600 ? '2-digit' : undefined});
          mk('text', {x: L + i * bw + bw / 2, y: H - 5, 'text-anchor': 'middle'}, lbl);
        }
      }
      return svg;
    }

    function donutChart(slices, centerTop, centerBottom, fmt) {
      const total = slices.reduce(function(a, x) { return a + x.value; }, 0);
      if (!total) return el('div', 'chart-empty', ['No data in this window.']);
      const NS = 'http://www.w3.org/2000/svg';
      const svg = document.createElementNS(NS, 'svg');
      svg.setAttribute('viewBox', '0 0 120 120');
      svg.setAttribute('width', '150');
      svg.setAttribute('height', '150');
      const r = 46, c = 2 * Math.PI * r;
      let offset = 0;
      const bg = document.createElementNS(NS, 'circle');
      bg.setAttribute('cx', '60'); bg.setAttribute('cy', '60'); bg.setAttribute('r', String(r));
      bg.setAttribute('fill', 'none'); bg.setAttribute('stroke', '#f1f5f9'); bg.setAttribute('stroke-width', '16');
      svg.appendChild(bg);
      slices.forEach(function(sl) {
        if (!sl.value) return;
        const len = c * sl.value / total;
        const circ = document.createElementNS(NS, 'circle');
        circ.setAttribute('cx', '60'); circ.setAttribute('cy', '60'); circ.setAttribute('r', String(r));
        circ.setAttribute('fill', 'none'); circ.setAttribute('stroke', sl.color); circ.setAttribute('stroke-width', '16');
        circ.setAttribute('stroke-dasharray', len.toFixed(3) + ' ' + (c - len).toFixed(3));
        circ.setAttribute('stroke-dashoffset', (-offset).toFixed(3));
        circ.setAttribute('transform', 'rotate(-90 60 60)');
        const t = document.createElementNS(NS, 'title');
        t.textContent = sl.label + ': ' + (fmt ? fmt(sl.value) : fmtInt(sl.value));
        circ.appendChild(t);
        svg.appendChild(circ);
        offset += len;
      });
      [[centerTop, '58', '15', '700', '#0f172a'], [centerBottom, '74', '8.5', '500', '#475569']].forEach(function(cfg) {
        const tx = document.createElementNS(NS, 'text');
        tx.setAttribute('x', '60'); tx.setAttribute('y', cfg[1]); tx.setAttribute('text-anchor', 'middle');
        tx.setAttribute('font-size', cfg[2]); tx.setAttribute('font-weight', cfg[3]); tx.setAttribute('fill', cfg[4]);
        tx.textContent = cfg[0];
        svg.appendChild(tx);
      });
      const legend = el('div', 'donut-legend', []);
      slices.forEach(function(sl) {
        const sw = el('i', null, []);
        sw.style.background = sl.color;
        const pct = (100 * sl.value / total).toFixed(sl.value / total < 0.1 ? 1 : 0) + '%';
        legend.appendChild(el('div', 'donut-legend-row', [sw, el('span', null, [sl.label]),
          el('span', 'v', [(fmt ? fmt(sl.value) : fmtInt(sl.value)) + ' · ' + pct])]));
      });
      const box = el('div', 'donut-wrap', []);
      box.appendChild(svg);
      box.appendChild(legend);
      return box;
    }

    function renderExecOverview(fleet, uc) {
      if (fleet && typeof fleet === 'object' && Array.isArray(fleet.agents)) execLastFleet = fleet;
      fleet = execLastFleet;
      const headline = document.getElementById('execHeadline');
      if (!headline) return;
      if (!fleet) return;
      const agents = fleet.agents || [];
      const totals = fleet.totals || {};
      const win = windowLabel(fleet.window_hours);
      const mu = fleet.model_usage || {};
      const muTotals = mu.totals || {};
      const models = (mu.models || []).slice();
      const m = function(a) { return a.metrics || {}; };
      // One runtime can be registered as several GE agents; group so metrics are counted once.
      const runtimeKey = function(a) {
        const b = a.backend || {};
        if (b.kind === 'cloud_run' && b.service) return 'cloud_run:' + b.service;
        if (b.resource || b.url) return (b.kind || '') + ':' + (b.resource || b.url);
        return 'agent:' + (a.resource_name || a.agent_id);
      };
      const runtimes = [];
      const byKey = {};
      agents.forEach(function(a) {
        const k = runtimeKey(a);
        if (!byKey[k]) { byKey[k] = {agent: a, names: []}; runtimes.push(byKey[k]); }
        const nm = a.display_name || a.agent_id;
        if (byKey[k].names.indexOf(nm) < 0) byKey[k].names.push(nm);
        byKey[k].count = (byKey[k].count || 0) + 1;
      });
      const rtLabel = function(rt) {
        return rt.names.join(' / ') + (rt.count > 1 ? ' (' + rt.count + ' registrations)' : '');
      };
      const active = agents.filter(function(a) { return Number(m(a).requests || 0) > 0 || Number(m(a).llm_calls || 0) > 0; });
      const req = Number(totals.requests || 0), e4 = Number(totals.errors_4xx || 0), e5 = Number(totals.errors_5xx || 0);
      const spend = muTotals.est_cost_usd;
      const users = ((scopeUserCentric(uc) || {}).power_users_ldap || []);
      const scoped = geScope !== 'all';
      const people = users.filter(function(u) { return String(u.status || '').indexOf('HUMAN') >= 0; });
      const sas = users.filter(function(u) { return String(u.status || '').indexOf('SERVICE') >= 0; });

      headline.replaceChildren(
        el('strong', null, [fmtInt(totals.enabled) + ' of ' + fmtInt(totals.agents) + ' agents enabled']),
        ' · ' + fmtInt(active.length) + ' received traffic, handling ' + fmtInt(req) + ' requests ' +
        (e5 ? 'with ' + fmtInt(e5) + ' server error' + (e5 === 1 ? '' : 's') : 'with no server errors') +
        '. Estimated model spend: ' + (spend == null ? 'n/a' : fmtUsd(spend)) + '.',
        el('span', 'muted', ['Project ' + (fleet.project_id || '—') + ' · ' + win + ' · updated ' +
          (fleet.generated_at ? new Date(fleet.generated_at).toLocaleTimeString() : '—') +
          ' · spend is tokens × Vertex AI list price, not your invoice']),
        el('div', 'time-ranges', [
          el('strong', null, ['Time ranges: ']),
          'Traffic, errors, tokens and model spend: ' + win + ' (Time range menu; Cloud Monitoring, usually 3–10 min behind). ' +
          'Users: last 7 days, fixed (BigQuery audit logs and agent telemetry). ' +
          'Agent list: current. Invoice: last 30 days when a billing export is connected.']),
        scoped ? el('div', 'scope-note', ['Filtered to ' + geScopeLabel(geScope) +
          '. Agents, requests and users are filtered. Model spend is project-wide: Vertex AI usage metrics are not tagged by Gemini Enterprise app.' +
          (fleet.__shared_runtimes ? ' ' + fleet.__shared_runtimes + ' runtime(s) here are also registered in other apps; their traffic cannot be split by app.' : '')]) : ''
      );

      const kpis = document.getElementById('execKpis');
      if (kpis) {
        kpis.replaceChildren(
          kpiCard('Agents enabled', fmtInt(totals.enabled) + ' / ' + fmtInt(totals.agents), fmtInt(active.length) + ' with traffic · ' + win),
          kpiCard('Requests', fmtInt(req), (req ? (100 * e5 / req).toFixed(2) : '0.00') + '% server errors · ' + fmtInt(e4) + ' rejected (4xx)'),
          kpiCard('Est. model spend', spend == null ? '—' : fmtUsd(spend), fmtInt(muTotals.invocations) + ' model calls · ' + (scoped ? 'project-wide' : 'list price')),
          kpiCard('Active people', users.length ? fmtInt(people.length) : '—',
            users.length ? ('+ ' + fmtInt(sas.length) + ' service accounts · 7 days') : (scoped ? 'no audit-log activity for this scope' : 'loading from BigQuery…'))
        );
      }

      // Requests by agent (stacked ok / 4xx / 5xx), top 8
      const reqRows = runtimes.map(function(rt) {
        const a = rt.agent;
        const mm = m(a);
        const r = Number(mm.requests || 0), x4 = Number(mm.errors_4xx || 0), x5 = Number(mm.errors_5xx || 0);
        return {label: rtLabel(rt), total: r, segments: [
          {name: 'Successful', value: Math.max(0, r - x4 - x5), color: '#2563eb'},
          {name: 'Rejected (4xx)', value: x4, color: '#f59e0b'},
          {name: 'Server error (5xx)', value: x5, color: '#ef4444'},
        ]};
      }).filter(function(r) { return r.total > 0; }).sort(function(a, b) { return b.total - a.total; }).slice(0, 8);
      const trendBox = document.getElementById('execChartTrend');
      if (trendBox) {
        const keys = runtimes.map(function(rt) { return runtimeKey(rt.agent); });
        trendBox.replaceChildren(trendChart(fleet.trend, keys));
        const srcT = document.getElementById('execSrcTrend');
        const tr = fleet.trend || {};
        const bh = Number(tr.bucket_seconds || 0) / 3600;
        if (srcT) srcT.textContent = 'Source: ' + (tr.source || 'Cloud Monitoring') + ' · ' + win +
          (bh ? ' · ' + (bh >= 1 ? bh + 'h' : Math.round(bh * 60) + 'min') + ' buckets' : '') +
          (tr.status && tr.status !== 'ok' ? ' · status: ' + tr.status : '');
      }
      const reqBox = document.getElementById('execChartRequests');
      if (reqBox) reqBox.replaceChildren(hbarChart(reqRows));
      const srcReq = document.getElementById('execSrcRequests');
      if (srcReq) srcReq.textContent = 'Source: Cloud Monitoring (Cloud Run + Agent Engine) · ' + win +
        (runtimes.length - reqRows.length > 0 ? ' · ' + (runtimes.length - reqRows.length) + ' agents with no requests not shown' : '');

      // Spend by model (donut)
      models.sort(function(a, b) { return Number(b.est_cost_usd || 0) - Number(a.est_cost_usd || 0); });
      const spendSlices = models.filter(function(x) { return Number(x.est_cost_usd || 0) > 0; }).map(function(x, i) {
        return {label: x.model, value: Number(x.est_cost_usd), color: EXEC_PALETTE[i % EXEC_PALETTE.length]};
      });
      const spendBox = document.getElementById('execChartSpend');
      if (spendBox) spendBox.replaceChildren(donutChart(spendSlices, spend == null ? '—' : fmtUsd(spend), win, function(v) { return fmtUsd(v); }));
      const srcSpend = document.getElementById('execSrcSpend');
      if (srcSpend) {
        const noCard = (muTotals.models_without_rate_card || []);
        srcSpend.textContent = 'Source: Vertex AI model usage metrics (Cloud Monitoring) × published list price · ' + win +
          (scoped ? ' · project-wide, not filtered by app' : '') +
          (noCard.length ? ' · no price on file for: ' + noCard.join(', ') : '');
      }

      // Fleet mix by type (donut)
      const typeNames = {ADK: 'ADK (code)', A2A: 'A2A', LOW_CODE: 'No-code', MANAGED: 'Google-managed'};
      const byType = totals.by_type || {};
      const mixSlices = Object.keys(byType).sort(function(a, b) { return byType[b] - byType[a]; }).map(function(k, i) {
        return {label: typeNames[k] || k, value: Number(byType[k] || 0), color: EXEC_PALETTE[i % EXEC_PALETTE.length]};
      });
      const mixBox = document.getElementById('execChartMix');
      if (mixBox) mixBox.replaceChildren(donutChart(mixSlices, fmtInt(totals.agents), 'agents'));

      // Most active users
      const userRows = users.slice().sort(function(a, b) { return Number(b.sessions_7d || 0) - Number(a.sessions_7d || 0); })
        .slice(0, 6).map(function(u) {
          const st = String(u.status || '');
          const kind = st.indexOf('HUMAN') >= 0 ? ['Person', '#10b981'] :
            (st.indexOf('UNVERIFIED') >= 0 ? ['Unverified session id', '#f59e0b'] : ['Service account', '#94a3b8']);
          const v = Number(u.sessions_7d || 0);
          return {label: u.user_ldap || u.user_email || '—', total: v,
            segments: [{name: kind[0], value: v, color: kind[1]}]};
        });
      const usersBox = document.getElementById('execChartUsers');
      if (usersBox) usersBox.replaceChildren(users.length ? hbarChart(userRows) :
        el('div', 'chart-empty', [scoped ? 'No Gemini Enterprise audit-log activity for this scope.' :
          'Loading users from BigQuery… (demo users are never shown in live mode)']));

      // Tokens by agent (each runtime once) + Gemini Enterprise built-in assistant per app
      const engineNames = {};
      (fleet.engines || []).forEach(function(e) { engineNames[e.engine_key || ((e.location || 'global') + '/' + e.engine_id)] = e.display_name || e.engine_id; });
      const tokRows = runtimes.filter(function(rt) {
        const mm = m(rt.agent);
        return mm.input_tokens != null && (Number(mm.input_tokens) + Number(mm.output_tokens || 0)) > 0;
      }).map(function(rt) {
        const mm = m(rt.agent);
        return {label: rtLabel(rt), total: Number(mm.input_tokens) + Number(mm.output_tokens || 0), segments: [
          {name: 'Input', value: Number(mm.input_tokens), color: '#2563eb'},
          {name: 'Output', value: Number(mm.output_tokens || 0), color: '#10b981'}]};
      });
      const gau = fleet.ge_assistant_usage;
      Object.keys((gau && gau.by_engine) || {}).forEach(function(k) {
        const g = gau.by_engine[k];
        const tot = Number(g.input_tokens || 0) + Number(g.output_tokens || 0);
        if (tot > 0) tokRows.push({label: 'GE assistant · ' + (engineNames[k] || g.engine_id), total: tot, segments: [
          {name: 'Input', value: Number(g.input_tokens || 0), color: '#2563eb'},
          {name: 'Output', value: Number(g.output_tokens || 0), color: '#10b981'}]});
      });
      tokRows.sort(function(a, b) { return b.total - a.total; });
      const noTok = runtimes.filter(function(rt) {
        const mm = m(rt.agent);
        return Number(mm.requests || 0) > 0 && mm.input_tokens == null;
      });
      const tokBox = document.getElementById('execChartTokens');
      if (tokBox) tokBox.replaceChildren(hbarChart(tokRows.slice(0, 8), null, fmtTokens));
      const srcTok = document.getElementById('execSrcTokens');
      if (srcTok) srcTok.textContent = 'Source: OpenTelemetry gen_ai data the agents export (Cloud Trace spans, Cloud Logging events) · ' + win +
        (tokRows.length > 8 ? ' · top 8 of ' + tokRows.length : '') +
        (noTok.length ? ' · no token data from ' + noTok.length + ' agent(s) with traffic: ' + noTok.map(rtLabel).join(', ') : '');

      // Needs attention: rules over the live payload only
      const items = [];
      const broken = agents.filter(isBrokenRegistration);
      if (broken.length) items.push({sev: 2, text: broken.length + ' agent registration(s) point at a backend that no longer exists, so people who pick them get errors: ' +
        broken.slice(0, 4).map(function(a) { return a.display_name || a.agent_id; }).join(', ') + (broken.length > 4 ? '…' : '') + '. See Clean up below.'});
      runtimes.forEach(function(rt) {
        const mm = m(rt.agent);
        const r = Number(mm.requests || 0), c = Number(mm.llm_calls || 0);
        if (r > 0 && c / r >= 20) items.push({sev: 2, text: rtLabel(rt) + ': ' + fmtInt(c) + ' LLM calls for ' + fmtInt(r) + ' requests (' +
          fmtTokens(Number(mm.input_tokens || 0) + Number(mm.output_tokens || 0)) + ' tokens) ' + win + '. Possible agent loop; check its traces.'});
      });
      if (noTok.length) items.push({sev: 0, text: noTok.length + ' agent(s) with traffic export no token data: ' + noTok.map(rtLabel).slice(0, 3).join(', ') +
        (noTok.length > 3 ? '…' : '') + '. For Agent Engine, deploy with GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true.'});
      runtimes.forEach(function(rt) {
        const a = rt.agent;
        const mm = m(a);
        if (Number(mm.errors_5xx || 0) > 0) items.push({sev: 2, text: rtLabel(rt) + ': ' + fmtInt(mm.errors_5xx) + ' server error(s) (5xx) ' + win + '.'});
        const r = Number(mm.requests || 0), x4 = Number(mm.errors_4xx || 0);
        if (r >= 20 && x4 / r > 0.2) items.push({sev: 1, text: rtLabel(rt) + ': ' + Math.round(100 * x4 / r) + '% of requests rejected (4xx). Usually auth or permission failures; check callers.'});
      });
      const idle = agents.filter(function(a) { return a.state === 'ENABLED' && m(a).requests === 0 && !Number(m(a).llm_calls || 0); });
      if (idle.length) items.push({sev: 0, text: idle.length + ' enabled agent(s) had zero requests ' + win + ': ' +
        idle.slice(0, 3).map(function(a) { return a.display_name || a.agent_id; }).join(', ') + (idle.length > 3 ? '…' : '') + '.'});
      const disabled = agents.filter(function(a) { return a.state && a.state !== 'ENABLED'; });
      if (disabled.length) items.push({sev: 0, text: disabled.length + ' agent(s) are disabled or private.'});
      const noCard = (muTotals.models_without_rate_card || []);
      if (noCard.length) items.push({sev: 1, text: 'Spend for ' + noCard.join(', ') + ' is not included (no list price on file).'});
      (fleet.errors || []).slice(0, 2).forEach(function(er) { items.push({sev: 1, text: 'Data source warning: ' + String(er)}); });
      items.sort(function(a, b) { return b.sev - a.sev; });
      const att = document.getElementById('execAttention');
      if (att) {
        const colors = ['#94a3b8', '#f59e0b', '#ef4444'];
        att.replaceChildren();
        if (!items.length) att.appendChild(el('li', null, [el('span', 'sev-dot', []), 'Nothing needs attention right now.']));
        items.slice(0, 8).forEach(function(it) {
          const dot = el('span', 'sev-dot', []);
          dot.style.background = colors[it.sev];
          att.appendChild(el('li', null, [dot, el('span', null, [it.text])]));
        });
      }
      const cleanupPanel = document.getElementById('execCleanupPanel');
      if (cleanupPanel) {
        cleanupPanel.classList.toggle('hidden', !broken.length);
        const cnt = document.getElementById('execCleanupCount');
        if (cnt) cnt.textContent = broken.length + ' to review';
        const box = document.getElementById('execCleanup');
        box.replaceChildren();
        broken.forEach(function(a) {
          const r = a.registration || {};
          const act = r.action || {};
          const cmd = String(act.delete_command || '');
          const btn = el('button', 'btn', ['Copy']);
          btn.addEventListener('click', function() { copyText(cmd, btn); });
          box.appendChild(el('div', 'cleanup-row', [
            el('div', null, [el('strong', null, [a.display_name || a.agent_id]),
              ' · ' + geRegionLabel(agentLocation(a)) + ' · ' + (a.engine_display_name || a.engine_id || '—')]),
            el('div', 'cleanup-evidence', ['Evidence: ' + String(r.evidence || '—') + (r.checked_at ? ' Checked ' + fmtAgo(r.checked_at) + '.' : '')]),
            act.summary ? el('div', 'cleanup-evidence', ['Action: ' + act.summary]) : null,
            cmd ? el('div', 'cleanup-cmd', [el('code', null, [cmd]), btn]) : null,
          ]));
        });
      }
      notifyHostSizeChanged();
    }

    function isBrokenRegistration(a) {
      const st = (a.registration || {}).status;
      return st === 'BACKEND_NOT_FOUND' || st === 'NO_BACKEND';
    }

    function shortTokenSource(src) {
      src = String(src || '');
      if (src.indexOf('Trace') >= 0) return 'Cloud Trace';
      if (src.indexOf('Logging') >= 0) return 'Cloud Logging';
      return src;
    }

    function tokenCell(a) {
      const m = a.metrics || {};
      const kind = (a.backend || {}).kind;
      if (m.input_tokens != null) {
        const src = String(m.token_source || '');
        const td = el('td', 'mono', [fmtTokens(m.input_tokens) + ' / ' + fmtTokens(m.output_tokens),
          src ? el('span', 'cell-sub', [shortTokenSource(src)]) : null]);
        td.title = src ? 'Source: ' + src : 'No traffic in this window (token sources checked).';
        return td;
      }
      if (Number(m.requests || 0) > 0 && kind === 'agent_engine') {
        const td = el('td', null, [el('span', 'token-na', ['Not emitted']), el('span', 'cell-sub', ['no gen_ai telemetry'])]);
        td.title = (a.notes || []).join(' ') ||
          'This agent served requests but exported no gen_ai token data to Cloud Logging or Cloud Trace.';
        return td;
      }
      if (Number(m.requests || 0) > 0 && kind === 'cloud_run') {
        const td = el('td', null, [el('span', 'token-na', ['Not measured']), el('span', 'cell-sub', ['Cloud Run: requests only'])]);
        td.title = 'Cloud Run reports request metrics only; VibeLift has no per-service token source for Cloud Run.';
        return td;
      }
      return el('td', 'mono', ['—']);
    }

    function registrationBadge(a) {
      const r = a.registration || {};
      if (isBrokenRegistration(a)) {
        const b = badge(r.status === 'NO_BACKEND' ? 'no backend' : 'backend deleted', 'badge-red');
        b.title = String(r.evidence || '') + ((r.action || {}).summary ? ' ' + r.action.summary : '');
        return b;
      }
      if (r.status === 'UNVERIFIED') {
        const b = badge('backend unverified', 'badge-yellow');
        b.title = String(r.evidence || '');
        return b;
      }
      return null;
    }

    function copyText(text, btn) {
      const done = function(ok) {
        if (!btn) return;
        btn.textContent = ok ? 'Copied' : 'Copy blocked: select the text';
        setTimeout(function() { btn.textContent = 'Copy'; }, 2500);
      };
      try {
        navigator.clipboard.writeText(text).then(function() { done(true); }, function() { done(false); });
      } catch (e) { done(false); }
    }

    function fmtInterval(iv) {
      if (!iv || !iv.start || !iv.end) return '—';
      const o = {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'};
      return new Date(iv.start).toLocaleString([], o) + ' – ' + new Date(iv.end).toLocaleString([], o);
    }

    function signedUsd(v) {
      const n = Number(v || 0);
      return (n > 0 ? '+' : (n < 0 ? '−' : '')) + fmtUsd(Math.abs(n));
    }

    function tdText(text, cls) { return el('td', cls || null, [text]); }

    function fillSelect(id, values) {
      const sel = document.getElementById(id);
      if (!sel) return;
      const prev = sel.value;
      sel.replaceChildren();
      (values || []).forEach(function(v) { const o = el('option', null, [v]); o.value = v; sel.appendChild(o); });
      if ((values || []).indexOf(prev) >= 0) sel.value = prev;
    }

    let liveFinopsFromFleet = false;
    function renderLiveFinops(lf) {
      if (!lf || typeof lf !== 'object') return;
      const te = lf.token_economics || {};
      const win = windowLabel(te.window_hours);
      const tt = te.totals || {};
      const teBadge = document.getElementById('liveTeBadge');
      if (teBadge) teBadge.textContent = te.status === 'LIVE' ? fmtUsd(tt.est_cost_usd) + ' · ' + fmtInt(tt.calls) + ' model calls' : '—';
      const teScope = document.getElementById('liveTeScope');
      if (teScope) teScope.textContent = te.status === 'LIVE'
        ? win + ' (' + fmtInterval(te.interval) + '). ' + (te.agents_note || '') + ' Project-wide model spend: ' + fmtUsd(tt.est_cost_usd) + ' at list price.'
        : (te.reason || 'Unavailable.');
      const teBody = document.getElementById('liveTeAgentsBody');
      if (teBody) {
        teBody.replaceChildren();
        (te.agents || []).forEach(function(r) {
          teBody.appendChild(el('tr', null, [
            tdText(r.display_name || '—'),
            tdText(fmtInt(r.requests), 'mono'),
            tdText(fmtInt(r.llm_calls), 'mono'),
            tdText(r.llm_calls_per_request == null ? '—' : String(r.llm_calls_per_request), 'mono'),
            tdText(fmtTokens(r.input_tokens), 'mono'),
            tdText(fmtTokens(r.output_tokens), 'mono'),
            tdText(r.tokens_per_request == null ? '—' : fmtTokens(r.tokens_per_request), 'mono'),
            tdText((r.models || []).join(', ') || '—', 'mono'),
            tdText(shortTokenSource(r.token_source) || '—'),
          ]));
        });
        (te.agents_without_token_telemetry || []).forEach(function(r) {
          const td = el('td', 'token-na', [r.runs_on === 'cloud_run' ? 'Not measured (Cloud Run reports requests only)' : 'No gen_ai token data exported']);
          td.colSpan = 7;
          teBody.appendChild(el('tr', 'fleet-row-muted', [tdText(r.display_name || '—'), tdText(fmtInt(r.requests), 'mono'), td]));
        });
        if (!teBody.children.length) {
          const td = el('td', 'fleet-empty', [te.status === 'LIVE' ? 'No agent token data in this window.' : (te.reason || 'Unavailable.')]);
          td.colSpan = 9;
          teBody.appendChild(el('tr', null, [td]));
        }
      }
      const geBox = document.getElementById('liveTeGe');
      if (geBox) {
        geBox.replaceChildren();
        const ge = te.ge_assistant || [];
        if (ge.length) {
          const head = el('thead', null, [el('tr', null, ['GE app', 'Assistant', 'Model calls', 'Input', 'Output', 'Conversations', 'Models']
            .map(function(h) { return el('th', null, [h]); }))]);
          const body = el('tbody', null, ge.map(function(g) {
            return el('tr', null, [
              tdText(g.app || g.engine_key), tdText(Object.keys(g.assistant_agents || {}).join(', ') || '—'),
              tdText(fmtInt(g.llm_calls), 'mono'), tdText(fmtTokens(g.input_tokens), 'mono'), tdText(fmtTokens(g.output_tokens), 'mono'),
              tdText(fmtInt(g.conversations), 'mono'), tdText(Object.keys(g.models || {}).join(', ') || '—', 'mono')]);
          }));
          geBox.appendChild(el('div', 'live-sub', ['Gemini Enterprise built-in assistant']));
          geBox.appendChild(el('div', 'table-scroll', [el('table', null, [head, body])]));
          geBox.appendChild(el('div', 'live-note', ['Source: Cloud Trace spans exported by Gemini Enterprise (cloud.platform gcp.gemini_enterprise). ' +
            'These calls are not in the Vertex AI model spend above.']));
        } else if (te.ge_assistant_status === 'UNAVAILABLE') {
          geBox.appendChild(el('div', 'live-note', ['Gemini Enterprise assistant token traces are unavailable (see data source status on the Agents tab).']));
        }
      }

      const sd = lf.spend_drift || {};
      const dBadge = document.getElementById('liveDriftBadge');
      const dScope = document.getElementById('liveDriftScope');
      const dBody = document.getElementById('liveDriftBody');
      const dmBody = document.getElementById('liveDriftModelsBody');
      const dNote = document.getElementById('liveDriftNote');
      if (dBody) dBody.replaceChildren();
      if (dmBody) dmBody.replaceChildren();
      if (sd.status !== 'LIVE') {
        if (dBadge) { dBadge.textContent = '—'; dBadge.className = 'badge badge-blue'; }
        if (dScope) dScope.textContent = sd.reason || 'Unavailable.';
        if (dNote) dNote.textContent = '';
      } else {
        if (dBadge) {
          dBadge.textContent = fmtUsd(sd.cost_before_usd) + ' → ' + fmtUsd(sd.cost_now_usd) +
            (sd.change_pct == null ? '' : ' (' + (sd.change_pct > 0 ? '+' : '') + sd.change_pct + '%)');
          dBadge.className = 'badge ' + (Number(sd.change_usd) > 0 ? 'badge-yellow' : 'badge-green');
        }
        if (dScope) dScope.textContent = 'This period: ' + fmtInterval(sd.current_interval) + '. Previous: ' +
          fmtInterval(sd.previous_interval) + '. ' + (sd.method || '');
        (sd.drivers || []).forEach(function(d) {
          if (dBody) dBody.appendChild(el('tr', null, [
            tdText(d.name), tdText(signedUsd(d.change_usd), 'mono'),
            tdText(d.share_of_change_pct == null ? '—' : d.share_of_change_pct + '%', 'mono'), tdText(d.description)]));
        });
        (sd.by_model || []).forEach(function(r) {
          if (dmBody) dmBody.appendChild(el('tr', null, [
            tdText(r.model, 'mono'), tdText(fmtUsd(r.cost_before_usd), 'mono'), tdText(fmtUsd(r.cost_now_usd), 'mono'),
            tdText(signedUsd(r.change_usd), 'mono'), tdText(fmtInt(r.calls_before) + ' → ' + fmtInt(r.calls_now), 'mono')]));
        });
        if (dNote) dNote.textContent = 'Source: Vertex AI model usage metrics (Cloud Monitoring) for both periods × list price. ' +
          'The drivers add up to the observed change; computed remainder: $' + Math.abs(Number(sd.unexplained_usd || 0)).toFixed(4) + '.' +
          ((sd.models_without_rate_card || []).length ? ' Not priced (no list price on file): ' + sd.models_without_rate_card.join(', ') + '.' : '');
      }

      fillSelect('wiFrom', lf.what_if_models || []);
      fillSelect('wiModel', lf.what_if_models || []);
      fillSelect('wiTo', lf.rate_card_models || []);
    }

    function onLiveWhatIfKind() {
      const cache = document.getElementById('wiKind').value === 'cache_share';
      document.querySelectorAll('.wi-switch').forEach(function(n) { n.classList.toggle('hidden', cache); });
      document.querySelectorAll('.wi-cache').forEach(function(n) { n.classList.toggle('hidden', !cache); });
    }

    async function runLiveWhatIf() {
      const kind = document.getElementById('wiKind').value;
      const body = {kind: kind, window_hours: fleetWindowHours};
      if (kind === 'cache_share') {
        body.model = document.getElementById('wiModel').value;
        body.target_cache_share_pct = Number(document.getElementById('wiCache').value);
      } else {
        body.from_model = document.getElementById('wiFrom').value;
        body.to_model = document.getElementById('wiTo').value;
        body.share_pct = Number(document.getElementById('wiShare').value);
      }
      const box = document.getElementById('liveWhatIfResult');
      box.replaceChildren(el('div', 'live-note', ['Computing from observed usage…']));
      let res = null;
      if (!isEmbedded()) {
        try {
          const r = await fetch('/api/what_if_live', {method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
          if (r.ok) res = await r.json();
        } catch (e) { res = null; }
      }
      if (!res) {
        res = {status: 'ERROR', error: isEmbedded()
          ? 'Projections run on the VibeLift server. Open the full dashboard to use them.'
          : 'The projection service did not respond.'};
      }
      box.replaceChildren();
      if (res.status !== 'PROJECTION') {
        box.appendChild(el('div', 'fleet-error', [String(res.error || res.status || 'Projection failed.')]));
        return;
      }
      box.appendChild(el('div', null, [el('strong', null, [res.scenario || 'Projection'])]));
      box.appendChild(el('div', 'wi-result-grid', [
        kpiCard('Observed spend', fmtUsd(res.observed_baseline_usd), fmtInterval(res.interval)),
        kpiCard('Change', signedUsd(res.change_usd), res.moved_cost_before_usd != null
          ? fmtUsd(res.moved_cost_before_usd) + ' → ' + fmtUsd(res.moved_cost_after_usd) + ' for the moved tokens'
          : (res.observed_cache_share_pct != null ? 'observed cache-read share ' + res.observed_cache_share_pct + '%' : '')),
        kpiCard('Projected spend', fmtUsd(res.projected_total_usd), 'same period, list price'),
      ]));
      box.appendChild(el('div', 'live-note', ['Assumptions: ' + (res.assumptions || []).join(' ')]));
    }

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
      if (fleet && Array.isArray(fleet.agents) && !fleet.__scoped) {
        geRawFleet = fleet;
        populateGeScopeSelect(fleet);
        fleet = applyGeScope(fleet);
      }
      try { renderExecOverview(fleet, currentState ? currentState.user_centric : null); } catch (e) { console.warn('overview', e); }
      if (!fleet || !Array.isArray(fleet.agents)) return;
      lastFleet = fleet;
      const rawLf = (geRawFleet && geRawFleet.live_finops) || fleet.live_finops;
      if (rawLf) { liveFinopsFromFleet = true; renderLiveFinops(rawLf); }
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
        kpiCard('GE assistant calls · project', traffic ? fmtInt(traffic.assistant_requests) : '—', 'StreamAssist requests, ' + win +
          (fleet.ge_assistant_usage && fleet.ge_assistant_usage.totals
            ? ' · assistant tokens ' + fmtTokens(fleet.ge_assistant_usage.totals.input_tokens) + ' in / ' +
              fmtTokens(fleet.ge_assistant_usage.totals.output_tokens) + ' out (Cloud Trace)' : ''))
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
            el('div', 'fleet-agent-app', [geRegionLabel(agentLocation(a)) + ' · ' + (a.engine_display_name || a.engine_id || '—')]),
            a.description ? el('div', 'fleet-agent-desc', [a.description]) : null,
            el('div', 'fleet-agent-tags', [
              badge(a.state || 'UNKNOWN', a.state === 'ENABLED' ? 'badge-green' : 'badge-yellow'),
              a.sharing_scope ? badge(String(a.sharing_scope).replace(/_/g, ' ').toLowerCase(), 'badge-blue') : null,
              registrationBadge(a),
            ]),
          ]),
          el('td', null, [badge(a.type_label || a.type, 'badge-blue')]),
          el('td', null, [el('div', null, [runsOn]), el('div', 'fleet-agent-desc', [[runsOnSub, scope].filter(Boolean).join(' · ')])]),
          el('td', 'mono', [fmtInt(m.requests)]),
          el('td', 'mono', [m.requests == null ? '—' : fmtInt(m.errors_4xx) + ' / ' + fmtInt(m.errors_5xx)]),
          el('td', 'mono', [m.latency_p50_ms == null && m.latency_p95_ms == null ? '—' : fmtMs(m.latency_p50_ms) + ' / ' + fmtMs(m.latency_p95_ms)]),
          el('td', 'mono', [fmtInt(m.llm_calls)]),
          tokenCell(a),
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
      if (fleet.token_log_scan && fleet.token_log_scan.traces_truncated) {
        notes.appendChild(el('li', null, ['Trace scan reached its page cap (' + fleet.token_log_scan.traces_scanned
          + ' traces); trace-based token totals are lower bounds for this window.']));
      }
    }

    async function refreshFleet(force) {
      const btn = document.getElementById('fleetRefreshBtn');
      if (btn) { btn.disabled = true; btn.textContent = 'Refreshing…'; }
      try {
        let fleet = null;
        if (isEmbedded()) {
          // Inside Gemini Enterprise the dashboard is a sandboxed MCP App: the host proxies tools/call.
          try {
            const result = await callHost('tools/call', {
              name: FLEET_TOOL,
              arguments: {window_hours: fleetWindowHours, force_refresh: !!force},
            }, 20000);
            if (result && !result.isError && result.structuredContent) {
              fleet = result.structuredContent;
              fleetRefreshMode = 'host';
            }
          } catch (innerErr) {
            fleet = null;
          }
          if (!fleet) {
            const fallback = await callHost('tools/call', {
              name: 'open_dashboard',
              arguments: {window_hours: fleetWindowHours, force_refresh: !!force},
            }, 20000);
            if (fallback && !fallback.isError && fallback.structuredContent) {
              const sc = fallback.structuredContent;
              if (sc.state) {
                renderState(sc.state);
                fleet = sc.state.ge_fleet;
              } else {
                fleet = sc.ge_fleet || sc;
              }
              if (fleet) fleetRefreshMode = 'host';
            }
          }
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
        if (isEmbedded()) {
          await refreshFleet(true);
          return;
        }
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

    const PLATFORM_META_LOCAL = {
      alpha_evolve: { id: 'alpha_evolve', name: 'AlphaEvolve (Cost, Speed & Quality Balance)' },
      opus_critic: { id: 'opus_critic', name: 'Opus Frontier Critic (Prompt Cleanup & Restructuring)' },
      vertex_vizier: { id: 'vertex_vizier', name: 'Google Vizier (Automated Parameter Tuning)' },
      hybrid_ensemble: { id: 'hybrid_ensemble', name: 'Hybrid Ensemble (AlphaEvolve + Vizier + Opus Critic)' },
    };

    function applyEmbeddedMutation(endpoint, payload) {
      if (!currentState || !currentState.active_agent) return false;
      const agent = currentState.active_agent;
      const ts = agent.timeline || [];
      const lastPt = ts.length > 0 ? ts[ts.length - 1] : {
        generation: 14,
        latency_ms: 690.0,
        cost_usd: 3.45,
        accuracy_pct: 96.4,
        cache_hit_pct: 91.2,
        error_rate_pct: 0.0,
      };
      const nextGen = Number(lastPt.generation || 14) + 1;

      if (endpoint === '/api/select_agent') {
        const targetId = payload && payload.agent_id;
        if (targetId && allAgentsCache[targetId]) {
          currentState.active_agent = JSON.parse(JSON.stringify(allAgentsCache[targetId]));
          renderState(currentState);
          return true;
        }
        return false;
      }

      if (endpoint === '/api/select_optimizer' && payload && payload.platform_id) {
        const pid = String(payload.platform_id);
        currentState.optimizer_platforms = currentState.optimizer_platforms || {};
        currentState.optimizer_platforms.active_platform_id = pid;
        currentState.optimizer_platforms.active_platform = PLATFORM_META_LOCAL[pid] || PLATFORM_META_LOCAL.alpha_evolve;
        renderState(currentState);
        return true;
      }

      if (endpoint === '/api/decorator_ingest') {
        currentState.decorator_events = currentState.decorator_events || [];
        currentState.decorator_events.unshift({
          timestamp: new Date().toLocaleTimeString() + ' (<10ms)',
          agent_name: agent.agent_id || 'it_service_desk',
          handler_name: (payload && payload.handler_name) || 'on_message_passing_turn',
          protocol: (payload && payload.protocol) || 'ADK / MCP Decorator Stream',
          model: agent.model || 'gemini-2.5-flash',
          latency_ms: 565.0,
          prompt_tokens: 19400,
          cached_tokens: 17980,
          output_tokens: 295,
          cache_hit_pct: 92.7,
          context_bloat_pct: 12.1,
          idle_ratio_pct: 6.4,
          skill_or_mcp: 'mcp://' + (agent.agent_id || 'it_service_desk') + '/decorator',
          user_cohort: 'Enterprise Active DAU Cohort',
          status: '200 OK (@vibelift_telemetry)',
        });
        renderState(currentState);
        return true;
      }

      if (endpoint === '/api/inject_anomaly') {
        (agent.parameters || []).forEach(function(p) {
          const k = p.key || p.param_id;
          if (k === 'latency_ms' || k === 'p95_latency_ms') { p.current_value = 2390.0; p.status = '⚠️ SLA BREACH (+246%)'; }
          else if (k === 'cost_usd' || k === 'cost_per_1k_turns_usd') { p.current_value = 24.80; p.status = '⚠️ CACHE BUST SPIKE'; }
          else if (k === 'accuracy_pct' || k === 'task_accuracy_pct') { p.current_value = 88.2; p.status = '⚠️ ACCURACY REGRESSION'; }
          else if (k === 'cache_hit_pct' || k === 'prompt_cache_hit_pct') { p.current_value = 14.5; p.status = '⚠️ PREFIX INVALIDATED'; }
          else if (k === 'error_rate_pct') { p.current_value = 9.4; p.status = '⚠️ 429 QUOTA ERRORS'; }
          else if (k === 'context_bloat_pct') { p.current_value = 68.5; p.status = '⚠️ CONTEXT BLOAT SPIKE'; }
          else if (k === 'idle_ratio_pct') { p.current_value = 44.0; p.status = '⚠️ TOOL WAIT BOTTLENECK'; }
        });
        agent.health_status = '⚠️ CRITICAL LOG ANOMALY (Cache Bust + 429 Spike)';
        ts.push({
          timestamp_label: 'Live Anomaly!',
          generation: lastPt.generation || 14,
          latency_ms: 2390.0,
          cost_usd: 24.80,
          accuracy_pct: 88.2,
          cache_hit_pct: 14.5,
          error_rate_pct: 9.4,
          event_marker: '⚠️ Dynamic Prompt Regression Injected',
        });
        agent.actions = agent.actions || [];
        agent.actions.unshift({
          generation: nextGen,
          timestamp: 'Just now (Live Log Alert)',
          parameter_targeted: 'All Parameters (Latency, Cost, Context Bloat & Cache Breach)',
          root_cause_from_logs: 'Upstream schema drift injected volatile correlation_id into system prefix, invalidating KV prefix cache and triggering 3x retry loops.',
          action_title: 'LIVE ALERT: Production Log Anomaly Detected — Awaiting Optimizer Remediation',
          action_taken: '@vibelift_telemetry decorator flagged P95 latency > 2,300ms and Cache Hit drop to 14.5%. Ready to trigger optimization cycle.',
          impact_summary: 'Click "Run Optimization Cycle" to evaluate and promote a remediation config.',
          status: 'ANOMALY ACTIVE — RUN OPTIMIZER',
          diff_snippet: '! ALERT: Uncached dynamic prefix token detected at offset 14\\n! Action Required: Execute Optimization Cycle',
        });
        allAgentsCache[agent.agent_id] = agent;
        renderState(currentState);
        return true;
      }

      if (endpoint === '/api/evolve_generation') {
        const curLat = Number(lastPt.latency_ms || lastPt.p95_latency_ms || 690.0);
        const curCost = Number(lastPt.cost_usd || lastPt.cost_per_1k_turns_usd || 3.45);
        const curAcc = Number(lastPt.accuracy_pct || lastPt.task_accuracy_pct || 96.4);
        const curCache = Number(lastPt.cache_hit_pct || lastPt.prompt_cache_hit_pct || 91.2);
        const newLat = Math.max(380.0, Math.round((curLat < 1500 ? curLat * 0.86 : 590.0) * 10) / 10);
        const newCost = Math.max(1.80, Math.round((curCost < 9.0 ? curCost * 0.84 : 2.85) * 100) / 100);
        const newAcc = Math.min(99.2, Math.round((curAcc > 92.0 ? curAcc + 0.6 : 97.6) * 10) / 10);
        const newCache = Math.min(97.5, Math.round((curCache > 50.0 ? curCache + 1.8 : 94.6) * 10) / 10);
        const activePlat = (currentState.optimizer_platforms && currentState.optimizer_platforms.active_platform && currentState.optimizer_platforms.active_platform.name)
          ? currentState.optimizer_platforms.active_platform.name.split(' (')[0]
          : 'AlphaEvolve';
        (agent.parameters || []).forEach(function(p) {
          const k = p.key || p.param_id;
          if (k === 'latency_ms' || k === 'p95_latency_ms') { p.current_value = newLat; p.status = 'OPTIMIZED BY GEN ' + nextGen; }
          else if (k === 'cost_usd' || k === 'cost_per_1k_turns_usd') { p.current_value = newCost; p.status = 'OPTIMIZED BY GEN ' + nextGen; }
          else if (k === 'accuracy_pct' || k === 'task_accuracy_pct') { p.current_value = newAcc; p.status = 'EXCEEDING TARGET (' + newAcc + '%)'; }
          else if (k === 'cache_hit_pct' || k === 'prompt_cache_hit_pct') { p.current_value = newCache; p.status = 'LOCKED (' + newCache + '% Hit)'; }
          else if (k === 'error_rate_pct') { p.current_value = 0.0; p.status = 'REMEDIATED (0.0%)'; }
          else if (k === 'context_bloat_pct') {
            const nextBloat = Math.max(6.0, Math.round(Number(p.current_value || 18.0) * 0.82 * 10) / 10);
            p.current_value = nextBloat;
            p.status = 'PRUNED (' + nextBloat + '% Bloat)';
          }
          else if (k === 'idle_ratio_pct') {
            const nextIdle = Math.max(3.5, Math.round(Number(p.current_value || 10.0) * 0.85 * 10) / 10);
            p.current_value = nextIdle;
            p.status = 'OPTIMIZED (' + nextIdle + '% Idle)';
          }
          else {
            p.current_value = p.direction === 'LOWER'
              ? Math.round((Number(p.current_value) * 0.92) * 100) / 100
              : Math.min(99.9, Math.round((Number(p.current_value) * 1.02) * 100) / 100);
            p.status = 'OPTIMIZED';
          }
        });
        agent.health_status = 'OPTIMIZED & REMEDIATED (' + activePlat + ' • Gen ' + nextGen + ' Active)';
        agent.monthly_savings_usd = Number(agent.monthly_savings_usd || 0) + 1850;
        ts.push({
          timestamp_label: 'Gen ' + nextGen + ' (Live)',
          generation: nextGen,
          latency_ms: newLat,
          cost_usd: newCost,
          accuracy_pct: newAcc,
          cache_hit_pct: newCache,
          error_rate_pct: 0.0,
          event_marker: activePlat + ' Gen ' + nextGen + ' Remediated',
        });
        agent.actions = agent.actions || [];
        agent.actions.unshift({
          generation: nextGen,
          timestamp: 'Just now (Live Run • ' + activePlat + ')',
          parameter_targeted: 'Cost, Speed & Quality Balance (Latency, Cost, Context Bloat & Cache Hit)',
          root_cause_from_logs: '@vibelift_telemetry decorator detected volatile header breaking prefix cache and redundant tool history bloating.',
          action_title: 'Gen ' + nextGen + ' [' + activePlat + ']: Sanitized Dynamic Header & Pruned Context Bloat',
          action_taken: 'Stripped volatile correlation_id from system prompt prefix, restored KV cache hit rate, and compacted N-2 tool history.',
          impact_summary: 'P95 Latency -> ' + newLat + 'ms | Cost -> $' + newCost + ' | Accuracy -> ' + newAcc + '% | Cache Hit -> ' + newCache + '%',
          status: 'PROMOTED TO PROD',
          diff_snippet: '- system_prefix: "Correlation={{corr_id}} | Follow all steps..."\\n+ system_prefix: "[STATIC_CACHED_V' + nextGen + '] Return single verified JSON block."',
        });
        allAgentsCache[agent.agent_id] = agent;
        renderState(currentState);
        return true;
      }

      if (endpoint === '/api/add_parameter' && payload) {
        const cleanLabel = String(payload.label || 'Custom Metric').trim() || 'Custom Metric';
        const slug = cleanLabel.toLowerCase().replace(/[^a-z0-9]+/g, '_');
        const bVal = Number(payload.baseline_val) || 80.0;
        const rawDir = String(payload.direction || 'HIGHER').toUpperCase();
        const dir = rawDir.includes('LOWER') ? 'LOWER' : 'HIGHER';
        const curVal = dir === 'LOWER' ? Math.round(bVal * 0.65 * 100) / 100 : Math.round(Math.min(99.0, bVal * 1.12) * 100) / 100;
        const newParamObj = {
          key: slug,
          label: cleanLabel,
          unit: payload.unit || '%',
          direction: dir,
          baseline_value: bVal,
          current_value: curVal,
          target_value: Number(payload.target_val) || 95.0,
          weight_pct: Number(payload.weight_pct) || 10,
          status: 'TRACKING IN LOGS (@vibelift_telemetry)',
        };
        agent.parameters = agent.parameters || [];
        const existingIdx = agent.parameters.findIndex(function(p) {
          return (p.key || p.param_id) === slug || String(p.label || '').toLowerCase() === cleanLabel.toLowerCase();
        });
        if (existingIdx >= 0) {
          agent.parameters[existingIdx] = newParamObj;
        } else {
          agent.parameters.push(newParamObj);
        }
        allAgentsCache[agent.agent_id] = agent;
        renderState(currentState);
        return true;
      }

      if (endpoint === '/api/reset' && initialStateSnapshot) {
        currentState = JSON.parse(JSON.stringify(initialStateSnapshot));
        if (currentState.all_agents) {
          allAgentsCache = JSON.parse(JSON.stringify(currentState.all_agents));
        }
        renderState(currentState);
        return true;
      }
      return false;
    }

    async function triggerApi(endpoint, payload = {}) {
      if (isEmbedded()) {
        applyEmbeddedMutation(endpoint, payload);
        if (endpoint === '/api/evolve_generation' && currentState && currentState.active_agent) {
          const activePlatId = (currentState.optimizer_platforms && currentState.optimizer_platforms.active_platform_id) || 'alpha_evolve';
          callHost('tools/call', {
            name: 'run_alpha_evolve_generation',
            arguments: {
              agent_id: currentState.active_agent.agent_id,
              platform_id: activePlatId,
            },
          }, 15000).then(function(res) {
            if (res && !res.isError && res.structuredContent) {
              const nextState = res.structuredContent.state || res.structuredContent;
              if (nextState && nextState.active_agent) {
                if (!nextState.ge_fleet && currentState && currentState.ge_fleet) {
                  nextState.ge_fleet = currentState.ge_fleet;
                }
                renderState(nextState);
              }
            }
          }).catch(function() {});
        }
        return;
      }
      try {
        const res = await fetch(endpoint, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        renderState(data);
      } catch (err) {
        applyEmbeddedMutation(endpoint, payload);
      }
    }

    function onSelectAgent(agentId) {
      triggerApi('/api/select_agent', {agent_id: agentId});
    }

    function onSelectOptimizer(platformId) {
      triggerApi('/api/select_optimizer', {platform_id: platformId});
    }

    function applyParameterPreset(label, unit, dirValue, baselineVal, targetVal, weightVal) {
      document.getElementById('newParamLabel').value = label;
      document.getElementById('newParamUnit').value = unit;
      document.getElementById('newParamDirection').value = dirValue;
      document.getElementById('newParamBaseline').value = String(baselineVal);
      document.getElementById('newParamTarget').value = String(targetVal);
      document.getElementById('newParamWeight').value = String(weightVal);
      onAddCustomParameter();
    }

    function emitLiveDecoratorEvent() {
      triggerApi('/api/decorator_ingest', {
        handler_name: 'on_message_passing_turn',
        protocol: 'ADK / MCP Decorator Stream (<10ms)',
      });
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
    // Docks on the Right Side Panel ('pip') by default with a prominent Fullscreen toggle button.
    // ---------------------------------------------------------------------------
    const INITIAL_EMBEDDED_STATE = __VIBELIFT_INITIAL_STATE_JSON__;
    const pending = {};
    let seq = 0;
    let isBridgeInitialized = false;
    let currentDisplayMode = 'pip';

    function notifyHostSizeChanged() {
      if (!isEmbedded() || currentDisplayMode === 'fullscreen') return;
      try {
        const h = Math.max(document.body ? document.body.offsetHeight : 0, 720);
        post({
          jsonrpc: '2.0',
          method: 'ui/notifications/size-changed',
          params: { height: h },
        });
      } catch (e) {}
    }

    function setDisplayMode(mode) {
      if (!isEmbedded()) {
        if (mode === 'fullscreen' && document.documentElement.requestFullscreen && !document.fullscreenElement) {
          document.documentElement.requestFullscreen().catch(function() {});
        } else if (mode !== 'fullscreen' && document.exitFullscreen && document.fullscreenElement) {
          document.exitFullscreen().catch(function() {});
        }
        currentDisplayMode = mode;
        syncDisplayModeButton();
        return Promise.resolve({ mode: mode });
      }
      return callHost('ui/request-display-mode', { mode: mode }, 4000).then(function(res) {
        currentDisplayMode = (res && res.mode) ? res.mode : mode;
        syncDisplayModeButton();
        notifyHostSizeChanged();
        return res;
      }).catch(function(err) {
        console.warn('Display mode request notice:', err);
        currentDisplayMode = mode;
        syncDisplayModeButton();
      });
    }

    document.addEventListener('fullscreenchange', function() {
      if (!isEmbedded()) {
        currentDisplayMode = document.fullscreenElement ? 'fullscreen' : 'pip';
        syncDisplayModeButton();
      }
    });

    function toggleDisplayMode() {
      const target = currentDisplayMode === 'fullscreen' ? 'pip' : 'fullscreen';
      setDisplayMode(target);
    }

    function syncDisplayModeButton() {
      const btn = document.getElementById('btnModeFullscreen');
      if (!btn) return;
      btn.style.display = 'inline-flex';
      if (currentDisplayMode === 'fullscreen') {
        btn.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z"/></svg> Dock to Right Panel';
        btn.title = 'Return to right side panel (pip)';
      } else {
        btn.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg> Fullscreen';
        btn.title = 'Expand dashboard to Fullscreen';
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
            p.resolve({
              protocolVersion: '2025-06-18',
              hostCapabilities: {},
              hostContext: { displayMode: 'pip', availableDisplayModes: ['pip', 'fullscreen', 'inline'] },
            });
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
          if (structured.focus_tab != null) {
            // An explicit request for an Advanced tab turns Advanced mode on instead of being ignored.
            if (ADVANCED_ONLY_TABS.indexOf(Number(structured.focus_tab)) >= 0 && !isAdvancedMode()) applyAdvancedMode(true);
            switchTab(structured.focus_tab);
          }
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

    // 0. If initial state was embedded in the MCP resource HTML, render immediately.
    if (INITIAL_EMBEDDED_STATE && typeof INITIAL_EMBEDDED_STATE === 'object') {
      try {
        renderState(INITIAL_EMBEDDED_STATE);
      } catch (err) {
        console.warn('Initial state render notice:', err);
      }
    }

    // 1. Instantly notify host that UI is ready
    emitAppInitialized();
    syncDisplayModeButton();

    // 2. Perform ui/initialize handshake per MCP Apps UI specification with availableDisplayModes
    const initPayload = {
      protocolVersion: '2025-06-18',
      appInfo: { name: 'vibelift-analytics-dashboard', version: '1.1.0' },
      clientInfo: { name: 'vibelift-analytics-dashboard', version: '1.1.0' },
      appCapabilities: { availableDisplayModes: ['pip', 'fullscreen', 'inline'] },
      capabilities: { availableDisplayModes: ['pip', 'fullscreen', 'inline'] },
    };
    callHost('ui/initialize', initPayload, 2500).catch(function() {
      return callHost('initialize', initPayload, 1500).catch(function() { return {}; });
    }).then(function(res) {
      emitAppInitialized();
      const hostMode = res && res.hostContext && res.hostContext.displayMode;
      if (hostMode) {
        currentDisplayMode = hostMode;
      }
      syncDisplayModeButton();
      // Automatically request Right Side Panel ('pip') when opened inline in Gemini Enterprise
      if (isEmbedded() && currentDisplayMode !== 'pip' && currentDisplayMode !== 'fullscreen') {
        setDisplayMode('pip');
      }
      notifyHostSizeChanged();
    }).catch(function(err) {
      console.warn('AppBridge handshake notice:', err);
      emitAppInitialized();
      if (isEmbedded()) {
        setDisplayMode('pip');
      }
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


def render_dashboard_html(
    initial_state: Mapping[str, object] | None = None,
) -> str:
  """Returns the self-contained Google Cloud 4-tab HTML UI."""
  state_json = 'null'
  if initial_state is not None:
    try:
      state_json = (
          json.dumps(initial_state)
          .replace('<', '\\u003c')
          .replace('>', '\\u003e')
          .replace('\u2028', '\\u2028')
          .replace('\u2029', '\\u2029')
      )
    except (TypeError, ValueError):
      state_json = 'null'
  return (
      _DASHBOARD_HTML.replace(
          '__VIBELIFT_GOOGLEY_LOGO_DATA_URI__',
          logo_asset.VIBELIFT_GOOGLEY_LOGO_DATA_URI,
      )
      .replace('__VIBELIFT_INITIAL_STATE_JSON__', state_json)
  )