// VibeLift dashboard enhancements.
//
// Inlined into the same self-contained HTML as the main dashboard script (so the Gemini Enterprise MCP
// App stays a single file) and loaded BEFORE it. Functions here read the main script's globals
// (currentState, geRawFleet, el(), fmtInt(), callHost(), ...) only when they are called, which is always
// after the main script has run.
//
// What it adds: smart tables (search, sort, facets, paging, CSV), a rule-based fleet health model, an
// agent detail view, KPI context (spend change, sparklines), the Cost tab spend overview, Users adoption,
// shareable URL state, MCP host integration (ui/open-link, ui/download-file, ui/message,
// ui/update-model-context) and keyboard / ARIA support.
//
// Same honesty rules as the rest of the dashboard: every number comes from a payload the dashboard
// already received, missing values render as a dash, and projections and derived labels say how they
// were computed. The pure helpers in part 1 have no DOM access so tests can run them in Node.
var VL = (function() {
  'use strict';

  // ===================================================================================================
  // Part 1: pure helpers (no DOM)
  // ===================================================================================================
  var DASH = '\u2014';
  var WINDOW_OPTIONS = [1, 6, 24, 168, 720, 2160, 4320, 8760];

  function num(v) {
    if (v === null || v === undefined || v === '' || typeof v === 'boolean') return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }
  function pick(v, dflt) { return v === null || v === undefined ? dflt : v; }
  function vlSum(values) {
    var seen = false;
    var total = 0;
    (values || []).forEach(function(v) {
      var n = num(v);
      if (n !== null) { seen = true; total += n; }
    });
    return seen ? total : null;
  }
  function byDay(a, b) { return String(a.day).localeCompare(String(b.day)); }
  function pctText(p) {
    if (p === null || p === undefined || !isFinite(p)) return DASH;
    if (p > 0 && p < 0.1) return '<0.1%';
    return p.toFixed(p < 10 ? 1 : 0) + '%';
  }
  function msText(v) {
    if (v === null || v === undefined) return DASH;
    return v >= 1000 ? (v / 1000).toFixed(1) + ' s' : Math.round(v) + ' ms';
  }
  function usdText(v, digits) {
    if (v === null || v === undefined || !isFinite(v)) return DASH;
    var d = pick(digits, 2);
    return (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
  }

  // ---- Fleet health: transparent, deterministic rules over measured metrics -------------------------
  var HEALTH_RULES = {
    minRequestsForRates: 20,    // error-rate rules need at least this many requests in the window
    failing5xxPct: 5,           // Failing: server errors at or above this share of requests
    loopCallsPerRequest: 20,    // Failing: this many LLM calls per request suggests an agent loop
    degraded5xxPct: 1,          // Degraded: server errors at or above this share of requests
    degraded4xxPct: 20,         // Degraded: rejected (4xx) requests at or above this share
    degradedP95Ms: 15000,       // Degraded: p95 latency at or above this value
  };
  var HEALTH_LEVELS = [
    {id: 'failing', label: 'Failing', rank: 0},
    {id: 'broken', label: 'Broken', rank: 1},
    {id: 'degraded', label: 'Degraded', rank: 2},
    {id: 'healthy', label: 'Healthy', rank: 3},
    {id: 'idle', label: 'Idle', rank: 4},
    {id: 'disabled', label: 'Disabled', rank: 5},
    {id: 'unknown', label: 'No telemetry', rank: 6},
  ];
  var HEALTH_BY_ID = {};
  HEALTH_LEVELS.forEach(function(l) { HEALTH_BY_ID[l.id] = l; });

  function healthRulesText() {
    var R = HEALTH_RULES;
    return 'Failing: 5xx \u2265 ' + R.failing5xxPct + '% of requests (with \u2265 ' + R.minRequestsForRates +
      ' requests) or \u2265 ' + R.loopCallsPerRequest + ' LLM calls per request. Degraded: 5xx \u2265 ' +
      R.degraded5xxPct + '%, 4xx \u2265 ' + R.degraded4xxPct + '%, p95 \u2265 ' + (R.degradedP95Ms / 1000) +
      ' s, or any 5xx on low traffic. Idle: enabled with 0 requests. Broken: the backend is missing. ' +
      'No telemetry: inventory only. Computed in your browser from the metrics shown.';
  }

  function healthResult(id, reasons) {
    var lvl = HEALTH_BY_ID[id];
    return {id: id, label: lvl.label, rank: lvl.rank, reasons: reasons};
  }

  function vlHealth(a) {
    var m = (a && a.metrics) || {};
    var reg = (a && a.registration) || {};
    if (reg.status === 'BACKEND_NOT_FOUND' || reg.status === 'NO_BACKEND') {
      return healthResult('broken', [reg.status === 'NO_BACKEND'
        ? 'No backend is configured for this registration.'
        : 'The backend this registration points at returned HTTP 404.']);
    }
    if (a && a.state && a.state !== 'ENABLED') return healthResult('disabled', ['State is ' + a.state + '.']);
    var r = num(m.requests);
    var llm = num(m.llm_calls);
    if (r === null) {
      return healthResult('unknown', ['Inventory only: this agent exposes no per-project runtime telemetry.']);
    }
    if (r === 0 && !(llm > 0)) return healthResult('idle', ['Enabled, but no requests in the selected time range.']);
    var R = HEALTH_RULES;
    var e4 = num(m.errors_4xx) || 0;
    var e5 = num(m.errors_5xx) || 0;
    var p95 = num(m.latency_p95_ms);
    var rated = r >= R.minRequestsForRates;
    var p5 = r > 0 ? 100 * e5 / r : 0;
    var p4 = r > 0 ? 100 * e4 / r : 0;
    var cpr = llm !== null && r > 0 ? llm / r : null;
    var reasons = [];
    var id = 'healthy';
    if (cpr !== null && cpr >= R.loopCallsPerRequest) {
      id = 'failing';
      reasons.push(cpr.toFixed(1) + ' LLM calls per request (\u2265 ' + R.loopCallsPerRequest + '): possible agent loop.');
    }
    if (rated && p5 >= R.failing5xxPct) {
      id = 'failing';
      reasons.push('Server errors (5xx) on ' + pctText(p5) + ' of requests (\u2265 ' + R.failing5xxPct + '%).');
    }
    if (id !== 'failing') {
      if (rated && p5 >= R.degraded5xxPct) {
        id = 'degraded';
        reasons.push('Server errors (5xx) on ' + pctText(p5) + ' of requests (\u2265 ' + R.degraded5xxPct + '%).');
      } else if (!rated && e5 > 0) {
        id = 'degraded';
        reasons.push(e5 + ' server error(s) on low traffic (' + r + ' requests).');
      }
      if (rated && p4 >= R.degraded4xxPct) {
        id = 'degraded';
        reasons.push('Rejected (4xx) ' + pctText(p4) + ' of requests (\u2265 ' + R.degraded4xxPct + '%): usually auth or permission failures.');
      }
      if (p95 !== null && p95 >= R.degradedP95Ms) {
        id = 'degraded';
        reasons.push('p95 latency ' + msText(p95) + ' (\u2265 ' + (R.degradedP95Ms / 1000) + ' s).');
      }
    }
    if (id === 'healthy') {
      reasons.push('Within thresholds: 5xx ' + pctText(p5) + ', 4xx ' + pctText(p4) +
        (p95 !== null ? ', p95 ' + msText(p95) : '') + (cpr !== null ? ', ' + cpr.toFixed(1) + ' LLM calls per request' : '') + '.');
    }
    return healthResult(id, reasons);
  }

  function vlHealthCounts(agents) {
    var counts = {};
    HEALTH_LEVELS.forEach(function(l) { counts[l.id] = 0; });
    (agents || []).forEach(function(a) { counts[vlHealth(a).id] += 1; });
    return counts;
  }

  // 2 = critical, 1 = warning, 0 = info: the server-error (5xx) rules alone, for "Needs attention".
  function vlServerErrorSeverity(a) {
    var m = (a && a.metrics) || {};
    var r = num(m.requests) || 0;
    var e5 = num(m.errors_5xx) || 0;
    if (!e5) return 0;
    var R = HEALTH_RULES;
    if (r < R.minRequestsForRates) return 1;
    var p5 = 100 * e5 / r;
    return p5 >= R.failing5xxPct ? 2 : (p5 >= R.degraded5xxPct ? 1 : 0);
  }

  // ---- Table sort values ---------------------------------------------------------------------------
  var REL_UNITS = {s: 1, min: 60, h: 3600, d: 86400};
  function vlParseSortValue(text) {
    var s = String(text === null || text === undefined ? '' : text).replace(/\u2212/g, '-').trim();
    if (!s || s === DASH || s === '-' || s === '\u2013' || /^n\/?a$/i.test(s) ||
        /^no (rate card|ratings|data|gen_ai|token|traffic)/i.test(s) ||
        /^not (measured|emitted|checked)/i.test(s) || /^unpriced/i.test(s)) {
      return null;
    }
    var rel = /^(\d+(?:\.\d+)?)\s*(s|min|h|d) ago$/.exec(s);
    if (rel) return -(Number(rel[1]) * REL_UNITS[rel[2]]);
    if (/^\d{4}-\d{2}-\d{2}/.test(s)) {
      var t = Date.parse(s.length === 10 ? s + 'T00:00:00Z' : s.replace(' ', 'T'));
      if (!isNaN(t)) return t;
    }
    var m = /^[<>~]?\s*(-)?\s*\$?\s*(\d[\d,]*(?:\.\d+)?|\.\d+)\s*(k|K|M|B)?(?![A-Za-z])\s*(ms|s|%)?/.exec(s);
    if (m) {
      var v = Number(m[2].replace(/,/g, ''));
      if (m[3] === 'k' || m[3] === 'K') v *= 1e3;
      else if (m[3] === 'M') v *= 1e6;
      else if (m[3] === 'B') v *= 1e9;
      if (m[4] === 's') v *= 1000;
      return m[1] ? -v : v;
    }
    return s.toLowerCase();
  }
  function vlCompare(a, b, dir) {
    var an = a === null || a === undefined;
    var bn = b === null || b === undefined;
    if (an || bn) return an && bn ? 0 : (an ? 1 : -1);  // missing values always sort last
    var at = typeof a;
    var bt = typeof b;
    var r;
    if (at !== bt) r = at === 'number' ? -1 : 1;
    else r = at === 'number' ? a - b : String(a).localeCompare(String(b));
    return dir === 'asc' ? r : -r;
  }

  // ---- CSV -----------------------------------------------------------------------------------------
  function csvCell(v) {
    var s = v === null || v === undefined ? '' : String(v);
    // Spreadsheet formula injection guard (OWASP): neutralize cells that a spreadsheet would evaluate.
    if (/^[=+@\t\r]/.test(s) || /^-[^\d.$\s]/.test(s)) s = "'" + s;
    if (/[",\r\n]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"';
    return s;
  }
  function vlToCsv(headers, rows) {
    return [headers].concat(rows || []).map(function(r) { return r.map(csvCell).join(','); }).join('\r\n') + '\r\n';
  }

  // ---- Spend math ----------------------------------------------------------------------------------
  // days: ge_daily_usage.days. ai_net_usd is null for days the billing export has not covered yet.
  function vlCostSummary(days, budgetUsd) {
    var billed = (days || []).filter(function(d) {
      return d && d.day && num(d.ai_net_usd) !== null;
    }).slice().sort(byDay);
    var budget = num(budgetUsd) > 0 ? num(budgetUsd) : null;
    if (!billed.length) return {status: 'NO_BILLING', billedDays: 0, budget: budget};
    var last = String(billed[billed.length - 1].day).slice(0, 10);
    var month = last.slice(0, 7);
    var inMonth = billed.filter(function(d) { return String(d.day).slice(0, 7) === month; });
    var mtd = vlSum(inMonth.map(function(d) { return d.ai_net_usd; }));
    var total = vlSum(billed.map(function(d) { return d.ai_net_usd; }));
    var recent = billed.slice(-7);
    var recentSpend = vlSum(recent.map(function(d) { return d.ai_net_usd; }));
    var avg7 = recent.length >= 3 ? recentSpend / recent.length : null;
    var year = Number(month.slice(0, 4));
    var mon = Number(month.slice(5, 7));
    var daysInMonth = new Date(Date.UTC(year, mon, 0)).getUTCDate();
    var remaining = Math.max(0, daysInMonth - Number(last.slice(8, 10)));
    var projection = avg7 !== null ? mtd + avg7 * remaining : null;
    var turns7 = vlSum(recent.map(function(d) { return d.interactions; }));
    var out = {
      status: 'OK', billedDays: billed.length, firstDay: String(billed[0].day).slice(0, 10), lastDay: last,
      month: month, total: total, mtd: mtd, mtdDays: inMonth.length, avg7: avg7, avg7Days: recent.length,
      daysInMonth: daysInMonth, daysRemaining: remaining, projection: projection,
      per1kTurns7d: turns7 ? recentSpend / turns7 * 1000 : null, budget: budget,
    };
    if (budget) {
      out.budgetUsedPct = 100 * mtd / budget;
      out.projectedPct = projection !== null ? 100 * projection / budget : null;
      out.dailyPace = budget / daysInMonth;
      var basis = out.projectedPct !== null ? out.projectedPct : out.budgetUsedPct;
      out.budgetStatus = basis > 100 ? 'over' : (basis >= 90 ? 'at_risk' : 'on_track');
    }
    return out;
  }

  function vlCacheSavings(mu) {
    if (!mu || !Array.isArray(mu.models)) return null;
    var cards = mu.rate_cards || {};
    var saved = 0;
    var priced = false;
    var cacheRead = 0;
    var prompt = 0;
    mu.models.forEach(function(m) {
      var cr = num(m.cache_read_tokens) || 0;
      var inp = num(m.input_tokens) || 0;
      cacheRead += cr;
      prompt += cr + inp;
      var c = cards[m.model];
      if (c && num(c.input) !== null && num(c.cached_read) !== null) {
        priced = true;
        saved += cr * (Number(c.input) - Number(c.cached_read)) / 1e6;
      }
    });
    return {savedUsd: priced ? saved : null, cacheReadTokens: cacheRead, promptTokens: prompt,
            sharePct: prompt ? 100 * cacheRead / prompt : null};
  }

  function vlSpendDelta(drift) {
    if (!drift || drift.status !== 'LIVE') return null;
    var before = num(drift.cost_before_usd);
    var now = num(drift.cost_now_usd);
    if (before === null || now === null) return null;
    return {before: before, now: now, change: now - before, pct: num(drift.change_pct)};
  }

  // ---- Adoption (vibelift_mart daily totals) --------------------------------------------------------
  function vlAdoption(days, todayIso) {
    var rows = (days || []).filter(function(d) { return d && d.day; }).slice().sort(byDay);
    if (!rows.length) return null;
    var today = String(todayIso || '').slice(0, 10);
    var complete = today ? rows.filter(function(d) { return String(d.day).slice(0, 10) < today; }) : rows;
    if (!complete.length) complete = rows;
    var last7 = complete.slice(-7);
    var latest = complete[complete.length - 1];
    var sessions = vlSum(last7.map(function(d) { return d.sessions; }));
    var turns = vlSum(last7.map(function(d) { return d.interactions; }));
    var failed = vlSum(last7.map(function(d) { return d.failed_turns; }));
    var users = vlSum(last7.map(function(d) { return d.active_users; }));
    return {
      latestDay: String(latest.day).slice(0, 10),
      activeUsersLatest: num(latest.active_users),
      activeUsersAvg7: users !== null ? users / last7.length : null,
      sessionsPerDay7: sessions !== null ? sessions / last7.length : null,
      turnsPerSession7: sessions ? turns / sessions : null,
      failedRatePct7: turns ? 100 * (failed || 0) / turns : null,
      daysCounted: last7.length,
      series: rows.map(function(d) {
        return {day: String(d.day).slice(0, 10), users: num(d.active_users), sessions: num(d.sessions),
                turns: num(d.interactions), partial: today ? String(d.day).slice(0, 10) >= today : false};
      }),
    };
  }

  // ---- Request trend (fleet.trend.by_runtime) -------------------------------------------------------
  function vlRuntimeKey(a) {
    var b = (a && a.backend) || {};
    if (b.kind === 'cloud_run' && b.service) return 'cloud_run:' + b.service;
    if (b.resource || b.url) return (b.kind || 'unknown') + ':' + (b.resource || b.url);  // same as fleet.runtime_backend_key
    return 'agent:' + ((a && (a.resource_name || a.agent_id)) || '');
  }
  function vlTrendTotals(trend, keys) {
    if (!trend || !Array.isArray(trend.bucket_ends) || !trend.bucket_ends.length) return null;
    var n = trend.bucket_ends.length;
    var total = new Array(n).fill(0);
    var e4 = new Array(n).fill(0);
    var e5 = new Array(n).fill(0);
    var found = false;
    (keys || []).forEach(function(k) {
      var row = (trend.by_runtime || {})[k];
      if (!row) return;
      found = true;
      for (var i = 0; i < n; i++) {
        total[i] += Number((row.requests || [])[i] || 0);
        e4[i] += Number((row.errors_4xx || [])[i] || 0);
        e5[i] += Number((row.errors_5xx || [])[i] || 0);
      }
    });
    if (!found) return null;
    return {total: total, errors_4xx: e4, errors_5xx: e5, bucket_ends: trend.bucket_ends,
            bucket_seconds: Number(trend.bucket_seconds || 0)};
  }

  function vlAgentPrompt(a, health, windowText) {
    var m = (a && a.metrics) || {};
    var name = (a && (a.display_name || a.agent_id)) || 'this agent';
    var app = (a && (a.engine_display_name || a.engine_id)) || 'its Gemini Enterprise app';
    var parts = ['Investigate the Gemini Enterprise agent "' + name + '" (' + app + ').'];
    parts.push('VibeLift rates it ' + health.label + ': ' + health.reasons.join(' '));
    var facts = [];
    if (num(m.requests) !== null) facts.push(num(m.requests) + ' requests');
    if (num(m.errors_5xx) !== null) facts.push(num(m.errors_5xx) + ' server errors (5xx)');
    if (num(m.errors_4xx) !== null) facts.push(num(m.errors_4xx) + ' rejected requests (4xx)');
    if (num(m.latency_p95_ms) !== null) facts.push('p95 latency ' + msText(num(m.latency_p95_ms)));
    if (num(m.llm_calls) !== null) facts.push(num(m.llm_calls) + ' LLM calls');
    if (facts.length) parts.push('Measured over the ' + windowText + ': ' + facts.join(', ') + '.');
    parts.push('What are the likely causes, and what should I check first?');
    return parts.join(' ');
  }

  // ---- Cloud Monitoring alert policies (pure) ---------------------------------------------------------
  // Builds alert policies for one agent from the same Cloud Monitoring metrics VibeLift reads, with the
  // dashboard's health thresholds as defaults. Nothing is created here: the user copies the commands,
  // reviews them and runs `gcloud monitoring policies create --policy-from-file` (GA) themselves.
  var ALERT_WINDOWS = [300, 900, 3600];
  var PROJECT_ID_RE = /^(?:\d{6,20}|[a-z][a-z0-9-]{4,28}[a-z0-9]|[a-z][a-z0-9.-]{0,62}:[a-z][a-z0-9-]{4,28}[a-z0-9])$/;
  var ENGINE_ID_RE = /^\d{1,30}$/;
  var SERVICE_NAME_RE = /^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
  var CHANNEL_ID_RE = /^\d{1,30}$/;
  var CHANNEL_NAME_RE = /^projects\/([^\/\s]+)\/notificationChannels\/(\d{1,30})$/;

  function oneLine(v) { return String(v === null || v === undefined ? '' : v).replace(/[\u0000-\u001f\u007f\u2028\u2029]+/g, ' ').trim(); }
  function alertSlug(name) {
    var slug = String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40).replace(/-+$/g, '');
    return slug || 'agent';
  }
  // The monitored resource behind an agent, or null when it has no per-project runtime metrics.
  function vlAlertTarget(a, fleetProject) {
    var b = (a && a.backend) || {};
    var project = String(b.project || fleetProject || '');
    if (b.kind === 'agent_engine') {
      var m = /\/reasoningEngines\/(\d+)$/.exec(String(b.resource || ''));
      var id = String(b.reasoning_engine_id || (m ? m[1] : ''));
      return {kind: 'agent_engine', project: project, id: id, label: 'resource.label.reasoning_engine_id',
              resourceType: 'aiplatform.googleapis.com/ReasoningEngine',
              requests: 'aiplatform.googleapis.com/reasoning_engine/request_count',
              latencies: 'aiplatform.googleapis.com/reasoning_engine/request_latencies',
              describe: 'Agent Engine ' + id + (b.location ? ' (' + oneLine(b.location) + ')' : '')};
    }
    if (b.kind === 'cloud_run' && b.service) {
      return {kind: 'cloud_run', project: project, id: String(b.service), label: 'resource.label.service_name',
              resourceType: 'cloud_run_revision', requests: 'run.googleapis.com/request_count',
              latencies: 'run.googleapis.com/request_latencies',
              describe: 'Cloud Run service ' + oneLine(b.service) + (b.region ? ' (' + oneLine(b.region) + ')' : '')};
    }
    return null;
  }
  // Smallest evaluation window in which the agent's observed traffic gives at least minRequests requests.
  function vlAutoAlertWindow(requests, windowHours, minRequests) {
    var r = num(requests);
    var hours = num(windowHours);
    if (r === null || hours === null || hours <= 0) return {seconds: 3600, perWindow: null};
    var perMinute = r / (hours * 60);
    for (var i = 0; i < ALERT_WINDOWS.length; i++) {
      var per = perMinute * ALERT_WINDOWS[i] / 60;
      if (per >= minRequests) return {seconds: ALERT_WINDOWS[i], perWindow: per};
    }
    return {seconds: 3600, perWindow: perMinute * 60};
  }
  function vlAlertPolicies(a, opts) {
    var o = opts || {};
    var R = HEALTH_RULES;
    var target = vlAlertTarget(a, o.project);
    if (!target) {
      return {ok: false, reason: 'This agent type has no per-project runtime metrics (no-code, Google-managed or Dialogflow agents), so there is nothing to alert on.'};
    }
    var include = o.include || {errors: true, latency: true, absence: false};
    var errorPct = num(o.errorPct);
    if (errorPct === null) errorPct = R.failing5xxPct;
    var latencyS = num(o.latencyS);
    if (latencyS === null) latencyS = R.degradedP95Ms / 1000;
    var absenceMin = num(o.absenceMin);
    if (absenceMin === null) absenceMin = 60;
    var problems = [];
    if (!PROJECT_ID_RE.test(target.project)) problems.push('The Google Cloud project is unknown or not a valid project ID.');
    if (target.kind === 'agent_engine' && !ENGINE_ID_RE.test(target.id)) problems.push('The Agent Engine ID is missing or invalid.');
    if (target.kind === 'cloud_run' && !SERVICE_NAME_RE.test(target.id)) problems.push('The Cloud Run service name is invalid.');
    if (!include.errors && !include.latency && !include.absence) problems.push('Pick at least one condition.');
    if (include.errors && !(errorPct > 0 && errorPct <= 100)) problems.push('The server error threshold must be above 0% and at most 100%.');
    if (include.latency && !(latencyS > 0 && latencyS <= 3600)) problems.push('The latency threshold must be above 0 s and at most 3600 s.');
    if (include.absence && !(absenceMin >= 5 && absenceMin <= 1440 && Math.round(absenceMin) === absenceMin)) {
      problems.push('The no-traffic time must be a whole number of minutes from 5 to 1440.');
    }
    var channel = null;
    var ch = String(o.channel === null || o.channel === undefined ? '' : o.channel).trim();
    if (ch) {
      var named = CHANNEL_NAME_RE.exec(ch);
      if (CHANNEL_ID_RE.test(ch)) channel = 'projects/' + target.project + '/notificationChannels/' + ch;
      else if (named && PROJECT_ID_RE.test(named[1])) channel = ch;
      else problems.push('The notification channel must be a channel ID or projects/PROJECT/notificationChannels/ID.');
    }
    var auto = vlAutoAlertWindow(((a && a.metrics) || {}).requests, o.windowHours, R.minRequestsForRates);
    var windowS = ALERT_WINDOWS.indexOf(Number(o.windowS)) >= 0 ? Number(o.windowS) : auto.seconds;
    if (problems.length) return {ok: false, reason: problems.join(' '), problems: problems, target: target, autoWindow: auto};

    var name = oneLine((a && (a.display_name || a.agent_id)) || 'agent');
    var slug = alertSlug(name);
    var where = target.describe + ' in project ' + target.project;
    var winText = windowS >= 3600 ? '1-hour' : (windowS / 60) + '-minute';
    var base = 'resource.type="' + target.resourceType + '" AND ' + target.label + '="' + target.id + '"';
    var reqFilter = 'metric.type="' + target.requests + '" AND ' + base;
    var agg = function(reducer, seconds) {
      return [{alignmentPeriod: seconds + 's', perSeriesAligner: 'ALIGN_DELTA', crossSeriesReducer: reducer, groupByFields: [target.label]}];
    };
    var policies = [];
    var add = function(key, title, severity, combiner, conditions, doc) {
      var p = {displayName: 'VibeLift: ' + name + ' \u00b7 ' + title, documentation: {content: doc, mimeType: 'text/markdown'},
               userLabels: {source: 'vibelift', vibelift_agent: slug}, conditions: conditions, combiner: combiner, enabled: true,
               severity: severity};
      if (channel) p.notificationChannels = [channel];
      policies.push({key: key, file: 'vibelift-' + slug + '-' + key + '.json', policy: p});
    };
    if (include.errors) {
      add('5xx', 'server errors', 'CRITICAL', 'AND', [
        {displayName: 'Server errors (5xx) above ' + errorPct + '% of requests', conditionThreshold: {
          filter: reqFilter + ' AND metric.label.response_code_class=starts_with("5")', aggregations: agg('REDUCE_SUM', windowS),
          denominatorFilter: reqFilter, denominatorAggregations: agg('REDUCE_SUM', windowS),
          comparison: 'COMPARISON_GT', thresholdValue: errorPct / 100, duration: '0s', trigger: {count: 1},
          evaluationMissingData: 'EVALUATION_MISSING_DATA_INACTIVE'}},
        {displayName: 'At least ' + R.minRequestsForRates + ' requests in the window', conditionThreshold: {
          filter: reqFilter, aggregations: agg('REDUCE_SUM', windowS), comparison: 'COMPARISON_GE',
          thresholdValue: R.minRequestsForRates, duration: '0s', trigger: {count: 1},
          evaluationMissingData: 'EVALUATION_MISSING_DATA_INACTIVE'}},
      ], 'Server errors (5xx) on **' + name + '** (' + where + ') were above ' + errorPct + '% of requests in a ' + winText +
         ' window with at least ' + R.minRequestsForRates + ' requests. That is the rule VibeLift uses to mark an agent Failing. ' +
         'Created from the VibeLift dashboard.');
    }
    if (include.latency) {
      add('p95', 'slow responses', 'WARNING', 'OR', [
        {displayName: 'p95 latency above ' + latencyS + ' s', conditionThreshold: {
          filter: 'metric.type="' + target.latencies + '" AND ' + base, aggregations: agg('REDUCE_PERCENTILE_95', windowS),
          comparison: 'COMPARISON_GT', thresholdValue: latencyS * 1000, duration: '0s', trigger: {count: 1},
          evaluationMissingData: 'EVALUATION_MISSING_DATA_INACTIVE'}},
      ], 'The 95th percentile latency of **' + name + '** (' + where + ') was above ' + latencyS + ' s in a ' + winText +
         ' window. VibeLift marks an agent Degraded at ' + (R.degradedP95Ms / 1000) + ' s. Created from the VibeLift dashboard.');
    }
    if (include.absence) {
      add('no-traffic', 'no traffic', 'WARNING', 'OR', [
        {displayName: 'No requests for ' + absenceMin + ' minutes', conditionAbsent: {
          filter: reqFilter, aggregations: agg('REDUCE_SUM', 300), duration: (absenceMin * 60) + 's', trigger: {count: 1}}},
      ], '**' + name + '** (' + where + ') received no requests for ' + absenceMin + ' minutes. Created from the VibeLift dashboard.');
    }
    var lines = ['# VibeLift alert policies for "' + name + '": ' + where + '.',
                 '# Review, then run with gcloud (needs roles/monitoring.alertPolicyEditor). Nothing is created until you run it.'];
    if (!channel) lines.push('# No notification channel: incidents only show in Cloud Monitoring until you add one.');
    if (target.kind === 'cloud_run') lines.push('# Cloud Run metrics are per service, so these alerts cover all traffic to ' + target.id + '.');
    policies.forEach(function(p) {
      lines.push('', 'cat > ' + p.file + " <<'VIBELIFT_POLICY'", JSON.stringify(p.policy, null, 2), 'VIBELIFT_POLICY',
                 "gcloud monitoring policies create --project='" + target.project + "' --policy-from-file=" + p.file);
    });
    return {ok: true, target: target, windowS: windowS, autoWindow: auto, policies: policies, script: lines.join('\n') + '\n'};
  }

  // ===================================================================================================
  // Part 2: DOM helpers
  // ===================================================================================================
  var SVGNS = 'http://www.w3.org/2000/svg';

  function h(tag, props, kids) {
    var n = document.createElement(tag);
    Object.keys(props || {}).forEach(function(k) {
      var v = props[k];
      if (v === null || v === undefined || v === false) return;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k.slice(0, 2) === 'on' && typeof v === 'function') n.addEventListener(k.slice(2), v);
      else if (k === 'dataset') Object.keys(v).forEach(function(d) { n.dataset[d] = v[d]; });
      else n.setAttribute(k, v === true ? '' : String(v));
    });
    (kids || []).forEach(function(c) {
      if (c === null || c === undefined || c === false) return;
      n.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    });
    return n;
  }
  function svgEl(tag, attrs, text) {
    var e = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function(k) { e.setAttribute(k, attrs[k]); });
    if (text !== null && text !== undefined) e.textContent = text;
    return e;
  }
  function byId(id) { return document.getElementById(id); }
  function debounce(fn, ms) {
    var t = null;
    return function() {
      var args = arguments;
      if (t) clearTimeout(t);
      t = setTimeout(function() { t = null; fn.apply(null, args); }, ms);
    };
  }
  function resized() { try { notifyHostSizeChanged(); } catch (e) { /* main script not ready */ } }
  function embedded() { try { return isEmbedded(); } catch (e) { return false; } }
  function windowText() {
    try { return windowLabel(userSelectedWindow ? fleetWindowHours : ((geRawFleet || {}).window_hours)); } catch (e) { return 'selected time range'; }
  }
  function rawFleet() {
    try { return geRawFleet || lastFleet || null; } catch (e) { return null; }
  }
  function state() {
    try { return currentState; } catch (e) { return null; }
  }
  function flash(btn, text) {
    if (!btn) return;
    if (!btn.dataset.vlLabel) btn.dataset.vlLabel = btn.textContent;
    btn.textContent = text;
    setTimeout(function() { btn.textContent = btn.dataset.vlLabel; }, 2600);
  }
  function healthBadge(hl) {
    var b = h('span', {class: 'vl-health vl-h-' + hl.id, title: hl.reasons.join(' ')}, [hl.label]);
    return b;
  }

  // ---- Host bridge (MCP Apps) ----------------------------------------------------------------------
  var hostCaps = {};
  function setHostCapabilities(caps) { hostCaps = caps && typeof caps === 'object' ? caps : {}; }
  function hostCan(cap) { return embedded() && !!hostCaps[cap]; }

  // Only https Google Cloud console links are opened. Every link is already built from a fixed
  // https://console.cloud.google.com/ prefix plus encoded identifiers; this is defense in depth.
  var EXTERNAL_HOSTS = ['console.cloud.google.com'];
  function safeExternalUrl(url) {
    try {
      var u = new URL(String(url));
      return (u.protocol === 'https:' && EXTERNAL_HOSTS.indexOf(u.hostname) >= 0) ? String(url) : null;
    } catch (e) { return null; }
  }

  function openExternal(rawUrl, btn) {
    var url = safeExternalUrl(rawUrl);
    if (!url) { flash(btn, 'Link blocked'); return; }
    if (embedded()) {
      if (hostCan('openLinks')) {
        callHost('ui/open-link', {url: url}, 6000).catch(function() {
          copyText(url, btn);
        });
        return;
      }
      copyText(url, btn);  // the sandbox may block popups; give the user the URL instead
      return;
    }
    var w = window.open(url, '_blank', 'noopener,noreferrer');
    if (w) w.opener = null;
  }

  function downloadText(filename, mime, text, btn) {
    if (embedded()) {
      if (hostCan('downloadFile')) {
        callHost('ui/download-file', {contents: [{type: 'resource', resource: {
          uri: 'file:///' + filename, mimeType: mime, text: text}}]}, 8000)
          .then(function() { flash(btn, 'Downloaded'); })
          .catch(function() { copyText(text, btn); });
        return;
      }
      copyText(text, btn);
      flash(btn, 'CSV copied to clipboard');
      return;
    }
    var blob = new Blob(['\ufeff' + text], {type: mime + ';charset=utf-8'});
    var href = URL.createObjectURL(blob);
    var a = h('a', {href: href, download: filename, style: 'display:none'});
    document.body.appendChild(a);
    a.click();
    setTimeout(function() { URL.revokeObjectURL(href); a.remove(); }, 1500);
    flash(btn, 'Downloaded');
  }

  function askAssistant(prompt, btn) {
    if (hostCan('message')) {
      callHost('ui/message', {role: 'user', content: {type: 'text', text: prompt}}, 8000)
        .then(function() { flash(btn, 'Sent to Gemini'); })
        .catch(function() { copyText(prompt, btn); });
      return;
    }
    copyText(prompt, btn);
  }

  var lastContext = '';
  var syncContextSoon = debounce(function(text) {
    if (!hostCan('updateModelContext') || !text || text === lastContext) return;
    lastContext = text;
    callHost('ui/update-model-context', {content: [{type: 'text', text: text}]}, 6000).catch(function() {});
  }, 600);

  // ===================================================================================================
  // Part 3: small charts
  // ===================================================================================================
  function sparkline(values, opts) {
    var o = opts || {};
    var vals = (values || []).map(num);
    var known = vals.filter(function(v) { return v !== null; });
    if (known.length < 2) return null;
    var W = pick(o.width, 120);
    var H = pick(o.height, 28);
    var max = Math.max.apply(null, known);
    var min = o.zeroBased === false ? Math.min.apply(null, known) : 0;
    var span = max - min || 1;
    var step = W / (vals.length - 1);
    var pts = [];
    vals.forEach(function(v, i) {
      if (v === null) return;
      pts.push((i * step).toFixed(1) + ',' + (H - 2 - (H - 4) * (v - min) / span).toFixed(1));
    });
    var svg = svgEl('svg', {viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, class: 'vl-spark', role: 'img',
                            'aria-label': o.label || 'trend', preserveAspectRatio: 'none'});
    if (o.area !== false) {
      svg.appendChild(svgEl('polygon', {points: '0,' + H + ' ' + pts.join(' ') + ' ' + W + ',' + H,
                                        fill: o.fill || 'rgba(37,99,235,0.12)'}));
    }
    svg.appendChild(svgEl('polyline', {points: pts.join(' '), fill: 'none', stroke: o.color || '#2563eb',
                                       'stroke-width': '1.6', 'vector-effect': 'non-scaling-stroke'}));
    if (o.label) svg.appendChild(svgEl('title', {}, o.label));
    return svg;
  }

  // Daily bars plus an optional line on its own axis. series: [{day, bar, line, partial}]
  function barLineChart(series, opts) {
    var o = opts || {};
    var rows = (series || []).filter(function(r) { return r && r.day; });
    var barMax = Math.max.apply(null, [0].concat(rows.map(function(r) { return num(r.bar) || 0; }), [num(o.refLine) || 0]));
    var lineMax = Math.max.apply(null, [0].concat(rows.map(function(r) { return num(r.line) || 0; })));
    if (!rows.length || (!barMax && !lineMax)) return h('div', {class: 'chart-empty'}, [o.empty || 'No data yet.']);
    var W = 1000;
    var H = 210;
    var L = 56;
    var Rm = o.lineLabel ? 56 : 12;
    var T = 10;
    var B = 24;
    var plotH = H - T - B;
    var plotW = W - L - Rm;
    var bw = plotW / rows.length;
    var svg = svgEl('svg', {viewBox: '0 0 ' + W + ' ' + H, preserveAspectRatio: 'none', class: 'vl-chart', role: 'img',
                            'aria-label': o.ariaLabel || 'Daily chart'});
    [0, 0.5, 1].forEach(function(f) {
      var y = T + plotH * (1 - f);
      svg.appendChild(svgEl('line', {x1: L, x2: W - Rm, y1: y, y2: y, stroke: '#e2e8f0', 'stroke-width': 1}));
      if (barMax) svg.appendChild(svgEl('text', {x: L - 6, y: y + 3, 'text-anchor': 'end'}, o.barFmt ? o.barFmt(barMax * f) : Math.round(barMax * f).toLocaleString()));
      if (lineMax && o.lineLabel) svg.appendChild(svgEl('text', {x: W - Rm + 6, y: y + 3, 'text-anchor': 'start', class: 'vl-axis-r'}, o.lineFmt ? o.lineFmt(lineMax * f) : Math.round(lineMax * f).toLocaleString()));
    });
    var every = Math.max(1, Math.ceil(rows.length / 10));
    var linePts = [];
    rows.forEach(function(r, i) {
      var x = L + i * bw;
      var bv = num(r.bar);
      if (bv !== null && barMax) {
        var bh = plotH * bv / barMax;
        svg.appendChild(svgEl('rect', {x: (x + bw * 0.14).toFixed(1), y: (T + plotH - bh).toFixed(1), width: (bw * 0.72).toFixed(1),
                                       height: bh.toFixed(1), fill: r.partial ? (o.partialColor || '#bfdbfe') : (o.barColor || '#2563eb'), rx: 1.5}));
      }
      var lv = num(r.line);
      if (lv !== null && lineMax) linePts.push((x + bw / 2).toFixed(1) + ',' + (T + plotH - plotH * lv / lineMax).toFixed(1));
      var hit = svgEl('rect', {x: x.toFixed(1), y: T, width: bw.toFixed(1), height: plotH, fill: 'transparent'});
      hit.appendChild(svgEl('title', {}, (o.tip ? o.tip(r) : r.day)));
      svg.appendChild(hit);
      if (i % every === 0) {
        var d = new Date(r.day + 'T00:00:00Z');
        var label = isNaN(d.getTime()) ? r.day : d.toLocaleDateString(undefined, {month: 'short', day: 'numeric', timeZone: 'UTC'});
        svg.appendChild(svgEl('text', {x: (x + bw / 2).toFixed(1), y: H - 6, 'text-anchor': 'middle'}, label));
      }
    });
    if (num(o.refLine) && barMax) {
      var ry = T + plotH - plotH * num(o.refLine) / barMax;
      svg.appendChild(svgEl('line', {x1: L, x2: W - Rm, y1: ry, y2: ry, stroke: '#b45309', 'stroke-width': 1.4, 'stroke-dasharray': '6,4'}));
      if (o.refLabel) svg.appendChild(svgEl('text', {x: W - Rm - 4, y: ry - 4, 'text-anchor': 'end', class: 'vl-ref'}, o.refLabel));
    }
    if (linePts.length > 1) {
      svg.appendChild(svgEl('polyline', {points: linePts.join(' '), fill: 'none', stroke: o.lineColor || '#10b981', 'stroke-width': 2.2,
                                         'vector-effect': 'non-scaling-stroke'}));
    }
    return svg;
  }

  // ===================================================================================================
  // Part 4: smart tables
  // ===================================================================================================
  var tables = {};
  var EMPTY_CELL = /^(\u2014|-|\u2013|no ratings)?$/i;

  function cellText(td) {
    if (!td) return '';
    if (td.dataset && td.dataset.csv !== undefined) return td.dataset.csv;
    var out = [];
    (function walk(node) {
      node.childNodes.forEach(function(c) {
        if (c.nodeType === 3) { var t = c.nodeValue.trim(); if (t) out.push(t); return; }
        if (c.nodeType !== 1) return;
        var tag = c.tagName;
        if (tag === 'BUTTON' || tag === 'SELECT' || tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'svg' || tag === 'SVG') return;
        walk(c);
      });
    })(td);
    return out.join(' ').replace(/\s+/g, ' ').trim();
  }
  function sortKeyOf(td) {
    if (!td) return null;
    if (td.dataset && td.dataset.sort !== undefined) {
      var n = num(td.dataset.sort);
      return n !== null ? n : (td.dataset.sort === '' ? null : String(td.dataset.sort).toLowerCase());
    }
    return vlParseSortValue(cellText(td));
  }
  function isMessageRow(tr) {
    return tr && tr.cells && tr.cells.length === 1 && tr.cells[0].colSpan > 1;
  }
  function headerRow(st) {
    var thead = st.table.tHead;
    return thead && thead.rows.length ? thead.rows[0] : null;
  }
  function sortableCol(st, i, th) {
    if ((st.cfg.noSort || []).indexOf(i) >= 0) return false;
    return !!(th && th.textContent.trim());
  }

  function smartTable(tbodyId, cfg) {
    var tbody = byId(tbodyId);
    if (!tbody || tables[tbodyId]) return tables[tbodyId] || null;
    var table = tbody.closest('table');
    if (!table) return null;
    var c = cfg || {};
    var st = {id: tbodyId, cfg: c, tbody: tbody, table: table, query: '', facets: {}, sortCol: null, sortDir: 'desc',
              page: 0, pageSize: pick(c.pageSize, 0), showAllCols: false, applying: false, groups: []};
    var wrap = table.parentElement;
    if (c.toolbar !== false) {
      st.search = h('input', {type: 'search', class: 'vl-tt-search', placeholder: c.placeholder || 'Search',
                              'aria-label': c.placeholder || 'Search table', autocomplete: 'off', spellcheck: 'false'});
      st.search.addEventListener('input', debounce(function() {
        st.query = st.search.value;
        st.page = 0;
        apply(st);
        if (c.onQuery) c.onQuery(st.query);
      }, 140));
      st.facetBox = h('div', {class: 'vl-tt-facets'});
      st.count = h('span', {class: 'vl-tt-count', 'aria-live': 'polite'});
      st.colsBtn = h('button', {type: 'button', class: 'btn btn-xs hidden', onclick: function() {
        st.showAllCols = !st.showAllCols;
        apply(st);
      }}, ['Show all columns']);
      st.csvBtn = h('button', {type: 'button', class: 'btn btn-xs', title: 'Download the rows that match the current search and filters',
                               onclick: function() { exportCsv(st); }}, ['Export CSV']);
      var compactBtn = null;
      if (c.compactKey) {
        var stored = null;
        try { stored = localStorage.getItem(c.compactKey); } catch (e) { stored = null; }
        var compactOn = stored === null ? true : stored === '1';
        table.classList.toggle('vl-compact', compactOn);
        compactBtn = h('button', {type: 'button', class: 'btn btn-xs', 'aria-pressed': compactOn ? 'true' : 'false',
                                  title: 'Compact rows hide descriptions and per-row trace buttons; open an agent for everything'}, ['Compact rows']);
        compactBtn.addEventListener('click', function() {
          var on = !table.classList.contains('vl-compact');
          table.classList.toggle('vl-compact', on);
          compactBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
          try { localStorage.setItem(c.compactKey, on ? '1' : '0'); } catch (e) { /* storage blocked */ }
          resized();
        });
      }
      st.compactBtn = compactBtn;
      st.bar = h('div', {class: 'vl-tt'}, [
        h('div', {class: 'vl-tt-row'}, [st.search, h('span', {class: 'vl-tt-spacer'}), st.count, compactBtn, st.colsBtn, st.csvBtn]),
        st.facetBox,
      ]);
      wrap.parentNode.insertBefore(st.bar, wrap);
    }
    if (st.pageSize) {
      st.prev = h('button', {type: 'button', class: 'btn btn-xs', onclick: function() { st.page -= 1; apply(st); }}, ['\u2039 Prev']);
      st.next = h('button', {type: 'button', class: 'btn btn-xs', onclick: function() { st.page += 1; apply(st); }}, ['Next \u203a']);
      st.pageText = h('span', {class: 'vl-pager-text'});
      st.sizeSel = h('select', {class: 'vl-pager-size', 'aria-label': 'Rows per page'});
      [10, 25, 50, 100, 0].forEach(function(n) {
        var opt = h('option', {value: String(n)}, [n ? n + ' per page' : 'All rows']);
        if (n === st.pageSize) opt.selected = true;
        st.sizeSel.appendChild(opt);
      });
      st.sizeSel.addEventListener('change', function() { st.pageSize = Number(st.sizeSel.value); st.page = 0; apply(st); });
      st.pager = h('div', {class: 'vl-pager hidden'}, [st.prev, st.pageText, st.next, st.sizeSel]);
      wrap.parentNode.insertBefore(st.pager, wrap.nextSibling);
    }
    var thead = table.tHead;
    if (thead) {
      var onSort = function(ev) {
        var th = ev.target.closest('th');
        var row = headerRow(st);
        if (!th || !row || th.parentElement !== row) return;
        var i = Array.prototype.indexOf.call(row.cells, th);
        if (!sortableCol(st, i, th)) return;
        if (st.sortCol !== i) {
          st.sortCol = i;
          st.sortDir = columnLooksNumeric(st, i) ? 'desc' : 'asc';
        } else if ((st.sortDir === 'desc') === columnLooksNumeric(st, i)) {
          st.sortDir = st.sortDir === 'desc' ? 'asc' : 'desc';
        } else {
          st.sortCol = null;  // third click: back to the server's order
        }
        st.page = 0;
        apply(st);
      };
      thead.addEventListener('click', onSort);
      thead.addEventListener('keydown', function(ev) {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onSort(ev); }
      });
    }
    if (c.defaultSort) { st.sortCol = c.defaultSort.col; st.sortDir = c.defaultSort.dir; }
    st.observer = new MutationObserver(function() { if (!st.applying) apply(st); });
    st.observer.observe(tbody, {childList: true});
    tables[tbodyId] = st;
    apply(st);
    return st;
  }

  function columnLooksNumeric(st, i) {
    var rows = st.groups.length ? st.groups : collectGroups(st);
    for (var k = 0; k < rows.length; k++) {
      var key = sortKeyOf(rows[k].main.cells[i]);
      if (key !== null) return typeof key === 'number';
    }
    return false;
  }

  function collectGroups(st) {
    var groups = [];
    var cur = null;
    Array.prototype.forEach.call(st.tbody.rows, function(tr) {
      if (cur && st.cfg.isDetailRow && st.cfg.isDetailRow(tr)) { cur.extra.push(tr); return; }
      cur = {main: tr, extra: []};
      groups.push(cur);
    });
    return groups;
  }

  function apply(st) {
    st.applying = true;
    try {
      applyInner(st);
    } finally {
      st.observer.takeRecords();
      st.applying = false;
    }
    resized();
  }

  function applyInner(st) {
    var c = st.cfg;
    var groups = collectGroups(st);
    st.groups = groups;
    var hdr = headerRow(st);
    if (hdr) {
      Array.prototype.forEach.call(hdr.cells, function(th, i) {
        if (sortableCol(st, i, th)) {
          th.classList.add('vl-sortable');
          th.tabIndex = 0;
          th.setAttribute('aria-sort', st.sortCol === i ? (st.sortDir === 'asc' ? 'ascending' : 'descending') : 'none');
        } else {
          th.classList.remove('vl-sortable');
          th.removeAttribute('aria-sort');
        }
      });
    }
    var message = groups.length === 1 && isMessageRow(groups[0].main);
    if (!groups.length || message) {
      if (st.count) st.count.textContent = '';
      if (st.facetBox) st.facetBox.replaceChildren();
      if (st.pager) st.pager.classList.add('hidden');
      if (st.colsBtn) st.colsBtn.classList.add('hidden');
      setColumnVisibility(st, []);
      return;
    }
    var missingIdx = groups.some(function(g) { return g.main._vlIdx === undefined; });
    if (missingIdx) groups.forEach(function(g, i) { g.main._vlIdx = i; });
    // Filter.
    var terms = String(st.query || '').toLowerCase().split(/\s+/).filter(Boolean);
    var facetDefs = c.facets || [];
    groups.forEach(function(g) {
      var ok = true;
      if (terms.length) {
        if (g.main._vlText === undefined) g.main._vlText = (g.main.textContent || '').toLowerCase();
        ok = terms.every(function(t) { return g.main._vlText.indexOf(t) >= 0; });
      }
      facetDefs.forEach(function(f) {
        var sel = st.facets[f.key];
        if (ok && sel && sel !== 'all' && g.main.dataset[f.key] !== sel) ok = false;
      });
      g.match = ok;
    });
    // Sort (or restore the server's order).
    var ordered = groups.slice();
    if (st.sortCol !== null && st.sortCol !== undefined) {
      ordered.forEach(function(g) { g.key = sortKeyOf(g.main.cells[st.sortCol]); });
      ordered.sort(function(a, b) { return vlCompare(a.key, b.key, st.sortDir) || (a.main._vlIdx - b.main._vlIdx); });
    } else {
      ordered.sort(function(a, b) { return a.main._vlIdx - b.main._vlIdx; });
    }
    var changed = ordered.some(function(g, i) { return g !== groups[i]; });
    if (changed) {
      var frag = document.createDocumentFragment();
      ordered.forEach(function(g) { frag.appendChild(g.main); g.extra.forEach(function(x) { frag.appendChild(x); }); });
      st.tbody.appendChild(frag);
    }
    // Paginate.
    var matched = ordered.filter(function(g) { return g.match; });
    var pages = st.pageSize ? Math.max(1, Math.ceil(matched.length / st.pageSize)) : 1;
    st.page = Math.max(0, Math.min(st.page, pages - 1));
    var start = st.pageSize ? st.page * st.pageSize : 0;
    var end = st.pageSize ? start + st.pageSize : matched.length;
    var visibleSet = new Set(matched.slice(start, end));
    ordered.forEach(function(g) {
      var filtered = !g.match;
      var paged = g.match && !visibleSet.has(g);
      [g.main].concat(g.extra).forEach(function(tr) {
        tr.classList.toggle('vl-row-filtered', filtered);
        tr.classList.toggle('vl-row-paged', paged);
      });
    });
    // Toolbar text, facets, pager.
    var noun = c.noun || 'rows';
    if (st.count) {
      var total = groups.length;
      if (st.pageSize && matched.length > st.pageSize) {
        st.count.textContent = (start + 1) + '\u2013' + Math.min(end, matched.length) + ' of ' + matched.length +
          (matched.length !== total ? ' matching (' + total + ' ' + noun + ')' : ' ' + noun);
      } else {
        st.count.textContent = matched.length === total ? total + ' ' + noun : matched.length + ' of ' + total + ' ' + noun;
      }
    }
    if (st.pager) {
      st.pager.classList.toggle('hidden', !(st.pageSize && matched.length > st.pageSize) && !(st.pageSize === 0 && st.cfg.pageSize));
      st.pageText.textContent = st.pageSize ? 'Page ' + (st.page + 1) + ' of ' + pages : 'All ' + matched.length + ' rows';
      st.prev.disabled = st.page <= 0;
      st.next.disabled = st.page >= pages - 1;
    }
    if (st.facetBox) renderFacets(st, groups);
    // Columns that are empty for every visible row.
    var hide = [];
    if (c.hideEmptyColumns && hdr) {
      var vis = matched.slice(start, end);
      Array.prototype.forEach.call(hdr.cells, function(th, i) {
        if (!vis.length || !th.textContent.trim()) return;
        var allEmpty = vis.every(function(g) { return EMPTY_CELL.test(cellText(g.main.cells[i]).trim()); });
        if (allEmpty) hide.push(i);
      });
    }
    if (st.colsBtn) {
      st.colsBtn.classList.toggle('hidden', !hide.length && !st.showAllCols);
      st.colsBtn.textContent = st.showAllCols ? 'Hide empty columns' : 'Show ' + hide.length + ' empty column' + (hide.length === 1 ? '' : 's');
    }
    setColumnVisibility(st, st.showAllCols ? [] : hide);
  }

  function setColumnVisibility(st, hideIdx) {
    var hidden = new Set(hideIdx);
    var hdr = headerRow(st);
    if (hdr) Array.prototype.forEach.call(hdr.cells, function(th, i) { th.classList.toggle('vl-col-hidden', hidden.has(i)); });
    Array.prototype.forEach.call(st.tbody.rows, function(tr) {
      if (isMessageRow(tr) || (st.cfg.isDetailRow && st.cfg.isDetailRow(tr))) return;
      Array.prototype.forEach.call(tr.cells, function(td, i) { td.classList.toggle('vl-col-hidden', hidden.has(i)); });
    });
  }

  function renderFacets(st, groups) {
    var box = st.facetBox;
    box.replaceChildren();
    (st.cfg.facets || []).forEach(function(f) {
      var counts = {};
      groups.forEach(function(g) {
        var v = g.main.dataset[f.key];
        if (v) counts[v] = (counts[v] || 0) + 1;
      });
      var values = (f.order || Object.keys(counts).sort()).filter(function(v) { return counts[v]; });
      if (values.length < 2 && !st.facets[f.key]) return;
      var sel = st.facets[f.key] || 'all';
      var grp = h('div', {class: 'vl-facet', role: 'group', 'aria-label': f.label});
      grp.appendChild(h('span', {class: 'vl-facet-label'}, [f.label]));
      var mk = function(value, label, count) {
        var b = h('button', {type: 'button', class: 'vl-chip' + (f.dot ? ' vl-chip-' + value : ''),
                             'aria-pressed': sel === value ? 'true' : 'false',
                             onclick: function() { st.facets[f.key] = sel === value && value !== 'all' ? 'all' : value; st.page = 0; apply(st); }},
                  [f.dot && value !== 'all' ? h('i', {class: 'vl-dot vl-dot-' + value}) : null, label,
                   count !== null ? h('span', {class: 'vl-chip-n'}, [String(count)]) : null]);
        return b;
      };
      grp.appendChild(mk('all', 'All', groups.length));
      values.forEach(function(v) { grp.appendChild(mk(v, (f.labels && f.labels[v]) || v, counts[v])); });
      box.appendChild(grp);
    });
  }

  function exportCsv(st) {
    var hdr = headerRow(st);
    var cols = [];
    if (hdr) Array.prototype.forEach.call(hdr.cells, function(th, i) { if (th.textContent.trim()) cols.push(i); });
    var headers = cols.map(function(i) { return hdr.cells[i].textContent.trim(); });
    var rows = (st.groups || []).filter(function(g) { return g.match && !isMessageRow(g.main); }).map(function(g) {
      return cols.map(function(i) { return cellText(g.main.cells[i]); });
    });
    var stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-');
    downloadText((st.cfg.exportName || 'vibelift-' + st.id) + '-' + stamp + '.csv', 'text/csv', vlToCsv(headers, rows), st.csvBtn);
  }

  function setTableQuery(tbodyId, q) {
    var st = tables[tbodyId];
    if (!st || !st.search) return;
    st.search.value = q || '';
    st.query = st.search.value;
    st.page = 0;
    apply(st);
  }
  function setTableFacet(tbodyId, key, value) {
    var st = tables[tbodyId];
    if (!st) return;
    st.facets[key] = value || 'all';
    st.page = 0;
    apply(st);
  }

  // ===================================================================================================
  // Part 5: fleet health strip, agents table helpers and the agent detail view
  // ===================================================================================================
  var TYPE_LABELS = {ADK: 'ADK', A2A: 'A2A', LOW_CODE: 'No-code', MANAGED: 'Google-managed'};

  function renderHealthStrip(agents) {
    var box = byId('vlHealthStrip');
    if (!box) return;
    var list = agents || [];
    box.replaceChildren();
    if (!list.length) { box.classList.add('hidden'); return; }
    box.classList.remove('hidden');
    var counts = vlHealthCounts(list);
    box.appendChild(h('span', {class: 'vl-strip-title'}, ['Fleet health']));
    HEALTH_LEVELS.forEach(function(l) {
      if (!counts[l.id]) return;
      box.appendChild(h('button', {type: 'button', class: 'vl-chip vl-chip-' + l.id, title: 'Show ' + l.label.toLowerCase() + ' agents on the Agents tab',
                                   onclick: function() { focusAgents({health: l.id}); }},
        [h('i', {class: 'vl-dot vl-dot-' + l.id}), l.label, h('span', {class: 'vl-chip-n'}, [String(counts[l.id])])]));
    });
    box.appendChild(h('span', {class: 'vl-strip-rules', title: healthRulesText()}, ['How is this computed?']));
  }

  function focusAgents(opts) {
    try { switchTab(0); } catch (e) { return; }
    if (opts && opts.health) setTableFacet('fleetAgentsBody', 'health', opts.health);
    if (opts && opts.query !== undefined) setTableQuery('fleetAgentsBody', opts.query);
    var panel = byId('fleetAgentsBody');
    if (panel && panel.closest('.panel') && panel.closest('.panel').scrollIntoView) panel.closest('.panel').scrollIntoView({block: 'start'});
  }

  function agentRowDecorate(tr, a) {
    var hl = vlHealth(a);
    tr.dataset.health = hl.id;
    tr.dataset.type = a.type || 'UNKNOWN';
    tr.dataset.agent = a.agent_id || a.resource_name || '';
    tr.classList.add('vl-row-click');
    tr.tabIndex = 0;
    tr.title = 'Open details for ' + (a.display_name || a.agent_id);
    var open = function() { openAgent(a, tr); };
    tr.addEventListener('click', function(ev) {
      if (ev.target.closest('button, a, input, select, textarea, .vl-agent-detail')) return;
      open();
    });
    tr.addEventListener('keydown', function(ev) {
      if ((ev.key === 'Enter' || ev.key === ' ') && ev.target === tr) { ev.preventDefault(); open(); }
    });
    return hl;
  }
  function healthCell(hl) {
    var td = h('td', {dataset: {sort: String(hl.rank), csv: hl.label}}, [healthBadge(hl)]);
    return td;
  }
  function typeCell(a) {
    var full = a.type_label || a.type || DASH;
    var short = TYPE_LABELS[a.type] || full;
    return h('td', {title: full, dataset: {csv: full, sort: short.toLowerCase()}}, [h('span', {class: 'badge badge-blue vl-type'}, [short])]);
  }
  function agentSpark(fleet, a) {
    var tt = vlTrendTotals(fleet && fleet.trend, [vlRuntimeKey(a)]);
    if (!tt) return null;
    var peak = Math.max.apply(null, tt.total);
    if (!peak) return null;
    var label = 'Requests per ' + bucketLabel(tt.bucket_seconds) + ', ' + windowText() + ' (peak ' + peak.toLocaleString() + ')';
    return sparkline(tt.total, {width: 72, height: 18, label: label});
  }
  function bucketLabel(sec) {
    var s = Number(sec || 0);
    if (s >= 86400) return (s / 86400 === 1 ? 'day' : (s / 86400) + ' days');
    if (s >= 3600) return (s / 3600 === 1 ? 'hour' : (s / 3600) + ' hours');
    return Math.round(s / 60) + ' min';
  }

  function consoleLinks(a, fleet) {
    var b = a.backend || {};
    var proj = (fleet && fleet.project_id) || '';
    var hours = (fleet && fleet.window_hours) || '';
    var dur = hours ? ';duration=PT' + hours + 'H' : '';
    var logs = function(filter, p) {
      return 'https://console.cloud.google.com/logs/query;query=' + encodeURIComponent(filter) + dur + '?project=' + encodeURIComponent(p);
    };
    var links = [];
    if (b.kind === 'agent_engine') {
      var p = b.project || proj;
      links.push({label: 'Agent Engine in Console', url: 'https://console.cloud.google.com/vertex-ai/agents/agent-engines?project=' + encodeURIComponent(p)});
      if (b.reasoning_engine_id) {
        links.push({label: 'Logs for this Agent Engine', url: logs('resource.type="aiplatform.googleapis.com/ReasoningEngine"\nresource.labels.reasoning_engine_id="' + b.reasoning_engine_id + '"', p)});
      }
      links.push({label: 'Trace Explorer', url: 'https://console.cloud.google.com/traces/list?project=' + encodeURIComponent(p)});
    } else if (b.kind === 'cloud_run' && b.service) {
      links.push({label: 'Cloud Run service', url: b.region
        ? 'https://console.cloud.google.com/run/detail/' + encodeURIComponent(b.region) + '/' + encodeURIComponent(b.service) + '/metrics?project=' + encodeURIComponent(proj)
        : 'https://console.cloud.google.com/run?project=' + encodeURIComponent(proj)});
      links.push({label: 'Logs for this service', url: logs('resource.type="cloud_run_revision"\nresource.labels.service_name="' + b.service + '"', proj)});
    }
    if (proj) links.push({label: 'Gemini Enterprise apps', url: 'https://console.cloud.google.com/gen-app-builder/engines?project=' + encodeURIComponent(proj)});
    return links;
  }

  // Users are shown by LDAP (the part before '@'), like the Top users table, never by full email address.
  function vlUserLabel(email) {
    var s = String(email === null || email === undefined ? '' : email).trim();
    if (!s) return DASH;
    var at = s.indexOf('@');
    return at > 0 ? s.slice(0, at) : s;
  }

  function relatedSessions(a) {
    var st = state();
    var sessions = (st && st.user_centric && Array.isArray(st.user_centric.ge_sessions)) ? st.user_centric.ge_sessions : [];
    var names = [a.display_name, a.agent_id].filter(Boolean).map(function(s) { return String(s).toLowerCase(); });
    return sessions.filter(function(s) {
      var n = String(s.agent_name || s.agent_id || '').toLowerCase();
      return n && names.indexOf(n) >= 0;
    });
  }

  function miniKpi(label, value, sub) {
    return h('div', {class: 'vl-mini'}, [h('div', {class: 'vl-mini-label'}, [label]), h('div', {class: 'vl-mini-value mono', title: typeof value === 'string' ? value : null}, [value]),
                                         sub ? h('div', {class: 'vl-mini-sub'}, [sub]) : null]);
  }
  function kv(rows) {
    var dl = h('dl', {class: 'vl-kv'});
    rows.forEach(function(r) {
      if (r[1] === null || r[1] === undefined || r[1] === '') return;
      dl.appendChild(h('dt', {}, [r[0]]));
      dl.appendChild(h('dd', {class: r[2] ? 'mono' : null}, [r[1]]));
    });
    return dl;
  }
  function section(title, kids, extraClass) {
    return h('section', {class: 'vl-d-sec' + (extraClass ? ' ' + extraClass : '')}, [h('h3', {}, [title])].concat(kids));
  }

  var traceWraps = {};
  function onTraceRow(res) {
    var ref = traceWraps[res];
    if (ref && ref.wrap.isConnected) { try { fillTraceRow(ref.wrap, ref.agent); } catch (e) { /* ignore */ } }
  }

  function agentDetail(a, fleet, mode) {
    var m = a.metrics || {};
    var b = a.backend || {};
    var hl = vlHealth(a);
    var win = windowText();
    var r = num(m.requests);
    var e4 = num(m.errors_4xx);
    var e5 = num(m.errors_5xx);
    var llm = num(m.llm_calls);
    var inTok = num(m.input_tokens);
    var outTok = num(m.output_tokens);
    var cached = num(m.cached_tokens);
    var wrap = h('div', {class: 'vl-agent-detail vl-mode-' + mode});

    var closeBtn = h('button', {type: 'button', class: 'btn btn-xs vl-d-close', 'aria-label': 'Close details', onclick: closeAgent}, ['Close \u2715']);
    var prompt = vlAgentPrompt(a, hl, win);
    var askBtn = h('button', {type: 'button', class: 'btn btn-xs btn-primary',
                              title: hostCan('message') ? 'Send an investigation request about this agent to the Gemini Enterprise chat'
                                                        : 'Copy an investigation prompt with these measured values',
                              onclick: function() { askAssistant(prompt, askBtn); }},
                   [hostCan('message') ? 'Ask Gemini to investigate' : 'Copy investigation prompt']);
    var linkBtn = embedded() ? null : h('button', {type: 'button', class: 'btn btn-xs', title: 'Copy a link that opens this view',
                                                   onclick: function() { copyText(shareUrl(), linkBtn); }}, ['Copy link']);
    var engineLabel = (function() { try { return geRegionLabel(agentLocation(a)); } catch (e) { return a.location || ''; } })();
    wrap.appendChild(h('div', {class: 'vl-d-head'}, [
      h('div', {class: 'vl-d-titlewrap'}, [
        h('h2', {class: 'vl-d-title', id: mode === 'drawer' ? 'vlDrawerTitle' : null}, [a.display_name || a.agent_id || 'Agent']),
        h('div', {class: 'vl-d-sub'}, [[engineLabel, a.engine_display_name || a.engine_id, a.type_label || a.type].filter(Boolean).join(' \u00b7 ')]),
        h('div', {class: 'vl-d-badges'}, [healthBadge(hl), badge(a.state || 'UNKNOWN', a.state === 'ENABLED' ? 'badge-green' : 'badge-yellow'),
                                          a.sharing_scope ? badge(String(a.sharing_scope).replace(/_/g, ' ').toLowerCase(), 'badge-blue') : null,
                                          (function() { try { return registrationBadge(a); } catch (e) { return null; } })()]),
      ]),
      h('div', {class: 'vl-d-actions'}, [askBtn, linkBtn, closeBtn]),
    ]));
    if (a.description) wrap.appendChild(h('p', {class: 'vl-d-desc'}, [a.description]));

    wrap.appendChild(section('Why "' + hl.label + '"', [
      h('ul', {class: 'vl-reasons'}, hl.reasons.map(function(t) { return h('li', {}, [t]); })),
      h('div', {class: 'vl-d-note'}, [healthRulesText()]),
    ], 'vl-sec-health vl-sec-' + hl.id));

    var cpr = llm !== null && r ? llm / r : null;
    var minis = h('div', {class: 'vl-minis'}, [
      miniKpi('Requests', fmtInt(r), win),
      miniKpi('Server errors (5xx)', fmtInt(e5), r ? pctText(100 * (e5 || 0) / r) + ' of requests' : null),
      miniKpi('Rejected (4xx)', fmtInt(e4), r ? pctText(100 * (e4 || 0) / r) + ' of requests' : null),
      miniKpi('Latency p50 / p95', m.latency_p50_ms === null && m.latency_p95_ms === null ? DASH : fmtMs(m.latency_p50_ms) + ' / ' + fmtMs(m.latency_p95_ms), null),
      miniKpi('LLM calls', fmtInt(llm), cpr !== null ? cpr.toFixed(1) + ' per request' : null),
      miniKpi('Tokens in / out', inTok === null ? DASH : fmtTokens(inTok) + ' / ' + fmtTokens(outTok),
              inTok && cached !== null ? pctText(100 * cached / inTok) + ' of input from cache' : null),
      miniKpi('Conversations', fmtInt(m.conversations), null),
      miniKpi('Last activity', m.last_activity ? fmtAgo(m.last_activity) : DASH, m.last_activity ? String(m.last_activity).replace('T', ' ').slice(0, 16) + ' UTC' : null),
    ]);
    if (num(m.vcpu_hours) !== null || num(m.billable_instance_hours) !== null) {
      minis.appendChild(miniKpi('Compute allocation', num(m.vcpu_hours) !== null
        ? num(m.vcpu_hours) + ' vCPU-h' : num(m.billable_instance_hours) + ' instance-h',
        num(m.memory_gib_hours) !== null ? num(m.memory_gib_hours) + ' GiB-h memory' : 'measured usage, not dollars'));
    }
    wrap.appendChild(section('Key numbers \u00b7 ' + win, [minis]));

    var key = vlRuntimeKey(a);
    var hasSeries = !!(fleet && fleet.trend && fleet.trend.by_runtime && fleet.trend.by_runtime[key]);
    var chart = null;
    try { chart = hasSeries ? trendChart(fleet.trend, [key]) : null; } catch (e) { chart = null; }
    wrap.appendChild(section('Requests over time', [
      chart || h('div', {class: 'chart-empty'}, [r === null ? 'No runtime request history: this agent exposes inventory only.'
                                                           : 'No per-runtime request history in this window.']),
      chart ? h('div', {class: 'chart-legend'}, [
        h('span', {}, [h('i', {style: 'background:#2563eb'}), 'Successful']),
        h('span', {}, [h('i', {style: 'background:#f59e0b'}), 'Rejected (4xx)']),
        h('span', {}, [h('i', {style: 'background:#ef4444'}), 'Server error (5xx)'])]) : null,
      h('div', {class: 'chart-source'}, ['Source: ' + ((fleet && fleet.trend && fleet.trend.source) || 'Cloud Monitoring') + ' \u00b7 ' + win]),
    ]));

    var models = (b.models || []).join(', ');
    wrap.appendChild(section('Runtime & tokens', [kv([
      ['Runs on', ({agent_engine: 'Vertex AI Agent Engine', cloud_run: 'Cloud Run', gke: 'GKE', external_endpoint: 'External endpoint',
                    gemini_enterprise_hosted: 'Gemini Enterprise hosted', dialogflow: 'Dialogflow'})[b.kind] || (b.kind ? b.kind : 'Google-managed')],
      ['Resource', b.resource || b.service || b.url || null, true],
      ['Region', b.location || b.region || null, true],
      ['Framework', b.framework || null],
      ['Models', models || null, true],
      ['Token source', m.token_source || null],
      ['Telemetry scope', {agent: 'Per agent', service: 'Whole Cloud Run service (all traffic)', none: 'Inventory only'}[a.telemetry_scope] || null],
      ['Registration', a.registration ? [a.registration.status, a.registration.evidence].filter(Boolean).join(' \u2014 ') : null],
    ])]));

    var fl = rawFleet() || fleet || {};
    var shared = (fl.agents || []).filter(function(o) { return o !== a && o.resource_name !== a.resource_name && vlRuntimeKey(o) === key && key.indexOf('agent:') !== 0; });
    if (shared.length) {
      wrap.appendChild(section('Shared runtime', [h('div', {class: 'vl-d-note'}, ['The same runtime is also registered as: ' +
        shared.map(function(o) { return (o.display_name || o.agent_id) + ' (' + (o.engine_display_name || o.engine_id) + ')'; }).join(', ') +
        '. Its traffic cannot be split between these registrations.'])]));
    }

    if (a.resource_name && String(a.resource_name).indexOf('/agents/') >= 0) {
      var tw = h('div', {class: 'fleet-agent-tags trace-row'});
      traceWraps[String(a.resource_name)] = {wrap: tw, agent: a};
      try { fillTraceRow(tw, a); } catch (e) { /* trace UI unavailable */ }
      wrap.appendChild(section('Trace logging', [tw, h('div', {class: 'vl-d-note'}, ['Changes live agent settings (observabilityConfig); you are asked to confirm first.'])]));
    }

    var reg = a.registration || {};
    if ((reg.status === 'BACKEND_NOT_FOUND' || reg.status === 'NO_BACKEND') && reg.action && reg.action.delete_command) {
      var cbtn = h('button', {type: 'button', class: 'btn btn-xs', onclick: function() { copyText(reg.action.delete_command, cbtn); }}, ['Copy']);
      wrap.appendChild(section('Clean up', [
        h('div', {class: 'vl-d-note'}, [reg.action.summary || 'Review, then run this command yourself. VibeLift never deletes anything.']),
        h('div', {class: 'cleanup-cmd'}, [h('code', {}, [reg.action.delete_command]), cbtn])]));
    }

    var skills = (fl.skills_and_mcp || []).filter(function(sk) {
      return sk && (sk.parent_agent_name === a.display_name || (sk.parent_engines || []).indexOf(a.engine_id) >= 0 && sk.parent_agent_name === a.display_name);
    });
    if (skills.length) {
      wrap.appendChild(section('Tools, sub-agents & MCP servers', [h('table', {class: 'vl-mini-table'}, [
        h('thead', {}, [h('tr', {}, [h('th', {}, ['Name']), h('th', {}, ['Kind']), h('th', {}, ['Calls']), h('th', {}, ['Avg latency'])])]),
        h('tbody', {}, skills.map(function(sk) {
          return h('tr', {}, [h('td', {class: 'mono'}, [sk.name || DASH]), h('td', {}, [String(sk.kind || '').replace(/_/g, ' ').toLowerCase()]),
                              h('td', {class: 'mono'}, [fmtInt(sk.calls)]), h('td', {class: 'mono'}, [sk.avg_latency_ms !== null && sk.avg_latency_ms !== undefined ? fmtMs(sk.avg_latency_ms) : DASH])]);
        })),
      ])]));
    }

    wrap.appendChild(alertSection(a, fl));

    var sess = relatedSessions(a);
    if (sess.length) {
      var byUser = {};
      sess.forEach(function(s) {
        var u = s.user_email ? vlUserLabel(s.user_email) : 'unknown';
        var e = byUser[u] || (byUser[u] = {user: u, sessions: 0, tokens: null});
        e.sessions += 1;
        if (num(s.total_tokens) !== null) e.tokens = (e.tokens || 0) + num(s.total_tokens);
      });
      var users = Object.keys(byUser).map(function(k) { return byUser[k]; }).sort(function(x, y) { return y.sessions - x.sessions; }).slice(0, 5);
      var allBtn = h('button', {type: 'button', class: 'btn btn-xs', onclick: function() {
        closeAgent();
        try { switchTab(4); } catch (e) { return; }
        setTableQuery('geSessionsBody', a.display_name || a.agent_id || '');
      }}, ['See all ' + sess.length + ' in Users']);
      wrap.appendChild(section('Recent sessions & top users', [
        h('table', {class: 'vl-mini-table'}, [
          h('thead', {}, [h('tr', {}, [h('th', {}, ['Session']), h('th', {}, ['User']), h('th', {}, ['Turns']), h('th', {}, ['Tokens']), h('th', {}, ['Ended'])])]),
          h('tbody', {}, sess.slice(0, 6).map(function(s) {
            return h('tr', {}, [h('td', {class: 'mono'}, [String(s.session_id || DASH).slice(-10)]), h('td', {class: 'mono'}, [vlUserLabel(s.user_email)]),
                                h('td', {class: 'mono'}, [fmtInt(s.turns)]), h('td', {class: 'mono'}, [fmtTokens(s.total_tokens)]),
                                h('td', {class: 'mono'}, [s.session_end ? fmtAgo(s.session_end) : DASH])]);
          })),
        ]),
        h('div', {class: 'vl-topusers'}, [h('strong', {}, ['Top users: '])].concat(users.map(function(u, i) {
          return (i ? ', ' : '') + u.user + ' (' + u.sessions + (u.sessions === 1 ? ' session' : ' sessions') + ')';
        }))),
        h('div', {class: 'vl-d-row'}, [h('span', {class: 'vl-d-note'}, ['From the most recent sessions in vibelift_mart.fct_sessions (agent name match).']), allBtn]),
      ]));
    }

    var links = consoleLinks(a, fl);
    if (links.length) {
      wrap.appendChild(section('Open in Google Cloud', [h('div', {class: 'vl-links'}, links.map(function(lk) {
        var lb = h('button', {type: 'button', class: 'btn btn-xs', title: lk.url, onclick: function() { openExternal(lk.url, lb); }}, [lk.label + ' \u2197']);
        return lb;
      }))]));
    }
    if ((a.data_sources || []).length || (a.notes || []).length) {
      wrap.appendChild(section('Data sources', [h('ul', {class: 'vl-reasons'},
        (a.data_sources || []).concat(a.notes || []).map(function(t) { return h('li', {}, [t]); }))]));
    }
    return wrap;
  }

  // ---- Alert policy section (agent detail) -----------------------------------------------------------
  // The form state lives here so a background refresh of the detail never wipes what the user typed.
  var alertForm = {agentId: null};
  function resetAlertForm(id) {
    alertForm = {agentId: id, open: false, errors: true, latency: true, absence: false,
                 errorPct: HEALTH_RULES.failing5xxPct, latencyS: HEALTH_RULES.degradedP95Ms / 1000, absenceMin: 60,
                 windowS: 'auto', channel: ''};
  }
  function windowName(seconds) { return seconds >= 3600 ? '1 hour' : (seconds / 60) + ' min'; }
  function alertSection(a, fl) {
    var id = a.agent_id || a.resource_name;
    if (alertForm.agentId !== id) resetAlertForm(id);
    if (openState && openState.alert) { alertForm.open = true; openState.alert = false; }
    var hours = num(fl.window_hours);
    var probe = vlAlertPolicies(a, {project: fl.project_id, windowHours: hours});
    if (!probe.target) {
      return section('Alert policy', [h('div', {class: 'vl-d-note'}, [probe.reason])]);
    }
    var out = h('pre', {class: 'vl-cmd', tabindex: '0', 'aria-label': 'gcloud commands'});
    var err = h('div', {class: 'vl-alert-err', role: 'alert'});
    var copyBtn = h('button', {type: 'button', class: 'btn btn-xs btn-primary'}, ['Copy commands']);
    var askBtn = hostCan('message') ? h('button', {type: 'button', class: 'btn btn-xs'}, ['Ask Gemini to review']) : null;
    var current = null;
    var refresh = function() {
      current = vlAlertPolicies(a, {project: fl.project_id, windowHours: hours, include: {errors: alertForm.errors, latency: alertForm.latency,
                                    absence: alertForm.absence}, errorPct: alertForm.errorPct, latencyS: alertForm.latencyS,
                                    absenceMin: alertForm.absenceMin, windowS: alertForm.windowS, channel: alertForm.channel});
      out.textContent = current.ok ? current.script : '';
      out.classList.toggle('hidden', !current.ok);
      err.textContent = current.ok ? '' : current.reason;
      err.classList.toggle('hidden', current.ok);
      copyBtn.disabled = !current.ok;
      if (askBtn) askBtn.disabled = !current.ok;
      resized();
    };
    var numInput = function(key, step, min, max, label) {
      var inp = h('input', {type: 'number', class: 'vl-alert-num', step: step, min: min, max: max, value: String(alertForm[key]),
                            'aria-label': label});
      inp.addEventListener('input', function() { alertForm[key] = inp.value === '' ? null : Number(inp.value); refresh(); });
      return inp;
    };
    var check = function(key, kids) {
      var cb = h('input', {type: 'checkbox', checked: alertForm[key] ? true : null});
      cb.addEventListener('change', function() { alertForm[key] = cb.checked; refresh(); });
      return h('label', {class: 'vl-alert-row'}, [cb].concat(kids));
    };
    var auto = probe.autoWindow || vlAutoAlertWindow((a.metrics || {}).requests, hours, HEALTH_RULES.minRequestsForRates);
    var winSel = h('select', {class: 'vl-alert-sel', 'aria-label': 'Evaluation window'}, [
      h('option', {value: 'auto'}, ['Auto: ' + windowName(auto.seconds) + (auto.perWindow !== null ? ' (about ' + intText(auto.perWindow) +
                                     ' requests per window at the observed rate)' : ' (no traffic measured)')]),
    ].concat(ALERT_WINDOWS.map(function(sec) { return h('option', {value: String(sec)}, [windowName(sec)]); })));
    winSel.value = String(alertForm.windowS);
    winSel.addEventListener('change', function() { alertForm.windowS = winSel.value === 'auto' ? 'auto' : Number(winSel.value); refresh(); });
    var chan = h('input', {type: 'text', class: 'vl-alert-chan', value: alertForm.channel, spellcheck: 'false', autocomplete: 'off',
                           placeholder: 'Channel ID or projects/PROJECT/notificationChannels/ID', 'aria-label': 'Notification channel'});
    chan.addEventListener('input', function() { alertForm.channel = chan.value; refresh(); });
    copyBtn.addEventListener('click', function() { if (current && current.ok) copyText(current.script, copyBtn); });
    if (askBtn) {
      askBtn.addEventListener('click', function() {
        if (current && current.ok) askAssistant('Review these Cloud Monitoring alert policies for the Gemini Enterprise agent "' +
          oneLine(a.display_name || id) + '" before I run them. Are the thresholds and windows sensible?\n\n' + current.script, askBtn);
      });
    }
    var form = h('div', {class: 'vl-alert-form' + (alertForm.open ? '' : ' hidden')}, [
      check('errors', [' Server errors (5xx) above ', numInput('errorPct', '0.5', '0.1', '100', 'Server error threshold, percent'),
                       ' % of requests, with at least ' + HEALTH_RULES.minRequestsForRates + ' requests in the window']),
      check('latency', [' p95 latency above ', numInput('latencyS', '1', '1', '3600', 'Latency threshold, seconds'), ' s']),
      check('absence', [' No requests for ', numInput('absenceMin', '5', '5', '1440', 'No-traffic time, minutes'), ' minutes']),
      h('div', {class: 'vl-alert-row'}, [h('span', {class: 'vl-alert-lbl'}, ['Window']), winSel]),
      h('div', {class: 'vl-alert-row'}, [h('span', {class: 'vl-alert-lbl'}, ['Notify']), chan]),
      err, out,
      h('div', {class: 'vl-d-row'}, [h('span', {class: 'vl-d-note'}, [
        'Defaults are the dashboard\u2019s health rules. Nothing is created until you run the commands with gcloud ' +
        '(roles/monitoring.alertPolicyEditor).']), h('span', {class: 'vl-links'}, [askBtn, copyBtn])]),
    ]);
    var toggle = h('button', {type: 'button', class: 'btn btn-xs', 'aria-expanded': alertForm.open ? 'true' : 'false'},
                   [alertForm.open ? 'Hide' : 'Create alert policy\u2026']);
    toggle.addEventListener('click', function() {
      alertForm.open = !alertForm.open;
      form.classList.toggle('hidden', !alertForm.open);
      toggle.textContent = alertForm.open ? 'Hide' : 'Create alert policy\u2026';
      toggle.setAttribute('aria-expanded', alertForm.open ? 'true' : 'false');
      resized();
    });
    refresh();
    var sec = section('Alert policy', [
      h('div', {class: 'vl-d-row'}, [h('span', {class: 'vl-d-note'}, ['Cloud Monitoring alerts on ' + probe.target.describe +
        ', built from the metrics this dashboard reads, as copy-and-run gcloud commands.']), toggle]),
      form,
    ], 'vl-alert-sec');
    return sec;
  }

  // ---- Drawer / inline detail ------------------------------------------------------------------------
  var openState = null;  // {id, mode, opener}
  var pendingAgentId = null;

  function useDrawer() {
    if (!embedded()) return true;
    try { return currentDisplayMode === 'fullscreen'; } catch (e) { return false; }
  }
  function findAgent(id) {
    var fl = rawFleet();
    var list = (fl && fl.agents) || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].agent_id === id || list[i].resource_name === id) return list[i];
    }
    return null;
  }
  function scopedFleet() { try { return lastFleet || geRawFleet; } catch (e) { return null; } }

  function openAgent(a, opener, opts) {
    if (!a) return;
    var id = a.agent_id || a.resource_name;
    var fresh = findAgent(id) || a;
    var mode = useDrawer() ? 'drawer' : 'inline';
    var wantAlert = !!(opts && opts.alert);
    if (!wantAlert && openState && openState.id === id && openState.mode === 'inline' && mode === 'inline') { closeAgent(); return; }
    closeAgent(true);
    openState = {id: id, mode: mode, opener: opener || document.activeElement, alert: wantAlert};
    renderOpenAgent(fresh);
    if (wantAlert) {
      var sec = document.querySelector('.vl-alert-sec');
      if (sec && sec.scrollIntoView) sec.scrollIntoView({block: 'start'});
    }
    syncUrl();
    var m = fresh.metrics || {};
    syncContextSoon('VibeLift dashboard: the user is viewing agent "' + (fresh.display_name || id) + '" (' +
      (fresh.engine_display_name || fresh.engine_id || '') + '), status ' + vlHealth(fresh).label + '. Over the ' + windowText() + ': ' +
      [num(m.requests) !== null ? num(m.requests) + ' requests' : null, num(m.errors_5xx) !== null ? num(m.errors_5xx) + ' 5xx' : null,
       num(m.errors_4xx) !== null ? num(m.errors_4xx) + ' 4xx' : null, num(m.latency_p95_ms) !== null ? 'p95 ' + msText(num(m.latency_p95_ms)) : null,
       num(m.llm_calls) !== null ? num(m.llm_calls) + ' LLM calls' : null].filter(Boolean).join(', ') + '.');
  }

  function renderOpenAgent(a) {
    if (!openState) return;
    var fleet = scopedFleet();
    if (openState.mode === 'drawer') {
      var drawer = byId('vlDrawer');
      var back = byId('vlDrawerBackdrop');
      if (!drawer) return;
      var scroll = drawer.scrollTop;
      drawer.replaceChildren(agentDetail(a, fleet, 'drawer'));
      drawer.classList.remove('hidden');
      if (back) back.classList.remove('hidden');
      document.body.classList.add('vl-drawer-open');
      drawer.scrollTop = scroll;
      if (!drawer.contains(document.activeElement)) {
        var close = drawer.querySelector('.vl-d-close');
        if (close) close.focus();
      }
    } else {
      var row = null;
      var body = byId('fleetAgentsBody');
      if (body) {
        Array.prototype.forEach.call(body.rows, function(tr) { if (tr.dataset.agent === openState.id) row = tr; });
      }
      if (!row) {
        try { switchTab(0); } catch (e) { return; }
        if (body) Array.prototype.forEach.call(body.rows, function(tr) { if (tr.dataset.agent === openState.id) row = tr; });
      }
      if (!row) return;
      var st = tables.fleetAgentsBody;
      if (st && (row.classList.contains('vl-row-filtered') || row.classList.contains('vl-row-paged'))) {
        st.query = '';
        if (st.search) st.search.value = '';
        st.facets = {};
        apply(st);
      }
      var old = body.querySelector('tr.vl-agent-detail-row');
      if (old) old.remove();
      var td = h('td', {colspan: String(row.cells.length)}, [agentDetail(a, fleet, 'inline')]);
      var detailRow = h('tr', {class: 'vl-agent-detail-row'}, [td]);
      row.classList.add('vl-row-open');
      row.parentNode.insertBefore(detailRow, row.nextSibling);
      if (st) apply(st);
      if (detailRow.scrollIntoView) detailRow.scrollIntoView({block: 'nearest'});
    }
    resized();
  }

  function closeAgent(silent) {
    if (!openState) return;
    var prev = openState;
    openState = null;
    var drawer = byId('vlDrawer');
    var back = byId('vlDrawerBackdrop');
    if (drawer) { drawer.classList.add('hidden'); drawer.replaceChildren(); }
    if (back) back.classList.add('hidden');
    document.body.classList.remove('vl-drawer-open');
    var body = byId('fleetAgentsBody');
    if (body) {
      var old = body.querySelector('tr.vl-agent-detail-row');
      if (old) old.remove();
      Array.prototype.forEach.call(body.querySelectorAll('tr.vl-row-open'), function(tr) { tr.classList.remove('vl-row-open'); });
    }
    if (!silent) {
      syncUrl();
      if (prev.opener && prev.opener.isConnected && prev.opener.focus) prev.opener.focus();
      resized();
    }
  }

  // ===================================================================================================
  // Part 6: Overview KPI context and "Needs attention"
  // ===================================================================================================
  function liveFinops() {
    var fl = rawFleet();
    var st = state();
    return (fl && fl.live_finops) || (st && st.live_finops) || null;
  }
  function spendDeltaNode(fleetWindowText) {
    var lf = liveFinops();
    var d = vlSpendDelta(lf && lf.spend_drift);
    if (!d) return null;
    var up = d.change > 0;
    var flat = Math.abs(d.change) < 0.005;
    var pct = d.pct !== null ? Math.abs(d.pct).toFixed(1) + '%' : 'new spend';
    return h('div', {class: 'vl-delta ' + (flat ? 'vl-delta-flat' : (up ? 'vl-delta-up' : 'vl-delta-down')),
                     title: 'Previous period (' + fleetWindowText + ' before this one): ' + usdText(d.before) +
                            '. Both periods are priced at current list prices; see "Why model spend changed" on the Cost tab.'},
      [(flat ? '\u2192 ' : (up ? '\u25b2 ' : '\u25bc ')) + (flat ? 'flat' : pct) + ' vs previous period (' + usdText(d.before) + ')']);
  }
  function requestsSparkNode(fleet, keys) {
    var tt = vlTrendTotals(fleet && fleet.trend, keys);
    if (!tt) return null;
    var peak = Math.max.apply(null, tt.total);
    if (!peak) return null;
    var idx = tt.total.indexOf(peak);
    var end = new Date(tt.bucket_ends[idx]);
    var when = isNaN(end.getTime()) ? '' : ' at ' + end.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric'});
    return h('div', {class: 'vl-kpi-spark'}, [sparkline(tt.total, {width: 160, height: 26, label: 'Requests per ' + bucketLabel(tt.bucket_seconds)}),
                                             h('span', {class: 'vl-kpi-spark-note'}, ['peak ' + peak.toLocaleString() + '/' + bucketLabel(tt.bucket_seconds) + when])]);
  }
  function usersSparkNode() {
    var st = state();
    var days = st && st.ge_daily_usage && Array.isArray(st.ge_daily_usage.days) ? st.ge_daily_usage.days : [];
    var ad = vlAdoption(days, new Date().toISOString());
    if (!ad) return null;
    var vals = ad.series.filter(function(s) { return !s.partial; }).map(function(s) { return s.users; });
    var sp = sparkline(vals, {width: 160, height: 26, color: '#10b981', fill: 'rgba(16,185,129,0.12)', label: 'Daily active users, last ' + vals.length + ' days (vibelift_mart)'});
    if (!sp) return null;
    return h('div', {class: 'vl-kpi-spark'}, [sp, h('span', {class: 'vl-kpi-spark-note'}, ['daily active users \u00b7 ' + vals.length + ' days'])]);
  }

  function decorateAttention(li, item) {
    if (!item || (!item.agent && !item.target)) return;
    li.classList.add('vl-att-click');
    li.tabIndex = 0;
    li.setAttribute('role', 'button');
    var go = function() {
      if (item.agent) { openAgent(item.agent, li); return; }
      var panel = byId(item.target);
      if (panel && panel.scrollIntoView) panel.scrollIntoView({behavior: 'smooth', block: 'start'});
    };
    li.addEventListener('click', go);
    li.addEventListener('keydown', function(ev) { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); go(); } });
    li.appendChild(h('span', {class: 'vl-att-go', 'aria-hidden': 'true'}, [item.agent ? 'Details \u203a' : 'View \u203a']));
  }
  function attentionSummary(items) {
    var badgeEl = byId('vlAttentionBadge');
    if (!badgeEl) return;
    var c = [0, 0, 0];
    (items || []).forEach(function(it) { c[it.sev] = (c[it.sev] || 0) + 1; });
    var parts = [];
    if (c[2]) parts.push(c[2] + ' critical');
    if (c[1]) parts.push(c[1] + ' warning' + (c[1] === 1 ? '' : 's'));
    if (c[0]) parts.push(c[0] + ' info');
    badgeEl.textContent = parts.join(' \u00b7 ') || 'all clear';
    badgeEl.className = 'badge ' + (c[2] ? 'badge-red' : (c[1] ? 'badge-yellow' : 'badge-green'));
  }

  // ===================================================================================================
  // Part 7: Cost tab spend overview
  // ===================================================================================================
  function budgetKey() {
    var st = state();
    return 'vibelift.budget.' + ((st && st.gcp_project) || 'default');
  }
  function readBudget() {
    try { return num(localStorage.getItem(budgetKey())); } catch (e) { return null; }
  }
  function writeBudget(v) {
    try { if (v) localStorage.setItem(budgetKey(), String(v)); else localStorage.removeItem(budgetKey()); } catch (e) { /* storage blocked */ }
  }
  function monthName(month) {
    var d = new Date(month + '-01T00:00:00Z');
    return isNaN(d.getTime()) ? month : d.toLocaleDateString(undefined, {month: 'long', year: 'numeric', timeZone: 'UTC'});
  }
  function shortDay(day) {
    var d = new Date(day + 'T00:00:00Z');
    return isNaN(d.getTime()) ? day : d.toLocaleDateString(undefined, {month: 'short', day: 'numeric', timeZone: 'UTC'});
  }
  function kpi(label, value, sub, extra, cls) {
    return h('div', {class: 'kpi-card' + (cls ? ' ' + cls : '')}, [h('div', {class: 'kpi-label'}, [label]),
                                                                   h('div', {class: 'kpi-value mono'}, [value]),
                                                                   sub ? h('div', {class: 'kpi-sub'}, [sub]) : null, extra || null]);
  }

  function setKids(node, kids) {
    if (!node) return;
    node.replaceChildren.apply(node, (kids || []).filter(function(k) { return k !== null && k !== undefined && k !== false; }));
  }
  function intText(v) { return v === null || v === undefined || !isFinite(v) ? DASH : Math.round(v).toLocaleString(); }

  function topDriverCard(win) {
    var lf = liveFinops();
    var drift = lf && lf.spend_drift;
    if (!drift || drift.status !== 'LIVE' || !Array.isArray(drift.drivers)) return null;
    var drivers = drift.drivers.filter(function(d) { return num(d.change_usd) !== null && Math.abs(num(d.change_usd)) >= 0.005; });
    if (!drivers.length) return kpi('Biggest driver of change', 'No change', 'Spend matched the previous period of the same length.');
    drivers.sort(function(a, b) { return Math.abs(num(b.change_usd)) - Math.abs(num(a.change_usd)); });
    var d = drivers[0];
    var ch = num(d.change_usd);
    var share = num(d.share_of_change_pct);
    return kpi('Biggest driver of change', String(d.name || d.driver_id),
      (ch > 0 ? '+' : '-') + usdText(Math.abs(ch)) + (share !== null ? ' (' + share.toFixed(0) + '% of the change)' : '') +
      ' vs the previous period of the same length. ' + String(d.description || ''),
      null, ch > 0 ? 'vl-kpi-warn' : 'vl-kpi-ok');
  }

  function renderSpend(st) {
    var panel = byId('vlSpendPanel');
    if (!panel || !st) return;
    var gdu = st.ge_daily_usage || null;
    var days = gdu && Array.isArray(gdu.days) ? gdu.days : [];
    var billingStatus = gdu ? String(gdu.billing_status || 'NOT_CONFIGURED') : null;
    var fl = rawFleet() || st.ge_fleet || {};
    var mu = fl.model_usage || null;
    var budget = readBudget();
    var cs = vlCostSummary(days, budget);
    var win = windowText();
    var billedCards = [];
    var estCards = [];
    var badgeEl = byId('vlSpendBadge');
    if (cs.status === 'OK') {
      billedCards.push(kpi('Billed AI spend \u00b7 ' + cs.billedDays + ' days', usdText(cs.total),
        shortDay(cs.firstDay) + ' \u2013 ' + shortDay(cs.lastDay) + ' (the export lags about a day)'));
      billedCards.push(kpi('Month to date \u00b7 ' + monthName(cs.month), usdText(cs.mtd),
        cs.mtdDays + ' billed day' + (cs.mtdDays === 1 ? '' : 's') + ' through ' + shortDay(cs.lastDay)));
      billedCards.push(kpi('Projected month-end', cs.projection !== null ? usdText(cs.projection) : DASH,
        cs.projection !== null ? 'Run-rate: last ' + cs.avg7Days + ' billed days avg ' + usdText(cs.avg7) + '/day \u00d7 ' + cs.daysRemaining +
          ' days left. A projection, not an invoice forecast.' : 'Needs at least 3 billed days.'));
      billedCards.push(budgetCard(cs));
      billedCards.push(kpi('Cost per 1k turns \u00b7 7 days', cs.per1kTurns7d !== null ? usdText(cs.per1kTurns7d) : DASH,
        'Billed AI spend \u00f7 Gemini Enterprise turns (vibelift_mart), last ' + cs.avg7Days + ' billed days.'));
      if (badgeEl) { badgeEl.textContent = 'Billing export: connected'; badgeEl.className = 'badge badge-green'; }
    } else {
      var words = {LIVE: 'no billed days yet', LOADING: 'loading\u2026', ERROR: 'error', NOT_CONFIGURED: 'not connected', NOT_CONNECTED: 'not connected'};
      var why = gdu ? 'Billing export ' + (words[billingStatus] || billingStatus.toLowerCase()) + '. See "Cloud Billing export" below to connect it.'
                    : 'Billed spend, month to date and run-rate need the Cloud Billing export and the vibelift_mart on a live project.';
      billedCards.push(kpi('Billed AI spend', DASH, why));
      billedCards.push(budgetCard(cs));
      if (badgeEl) {
        badgeEl.textContent = gdu ? 'Billing export: ' + (words[billingStatus] || billingStatus.toLowerCase()) : 'Estimates only';
        badgeEl.className = 'badge ' + (billingStatus === 'ERROR' ? 'badge-red' : 'badge-yellow');
      }
    }
    if (mu && mu.totals) {
      estCards.push(kpi('Est. model spend \u00b7 ' + win, fmtUsd(mu.totals.est_cost_usd),
        fmtInt(mu.totals.invocations) + ' model calls, project-wide', spendDeltaNode(win)));
      estCards.push(topDriverCard(win));
      var cache = vlCacheSavings(mu);
      if (cache) {
        estCards.push(kpi('Cache savings \u00b7 ' + win, cache.savedUsd !== null ? usdText(cache.savedUsd) : DASH,
          (cache.sharePct !== null ? pctText(cache.sharePct) + ' of prompt tokens read from cache' : 'No prompt tokens') +
          ', billed at the cached-read price instead of the input price. Storage fees not included.'));
      }
    }
    setKids(byId('vlSpendKpis'), billedCards);
    setKids(byId('vlSpendKpisEst'), estCards);
    var estTitle = byId('vlSpendEstTitle');
    if (estTitle) {
      estTitle.textContent = 'Estimated \u00b7 observed tokens \u00d7 list price \u00b7 ' + win;
      estTitle.classList.toggle('hidden', !estCards.filter(Boolean).length);
    }

    var chartBox = byId('vlSpendChart');
    var legend = byId('vlSpendLegend');
    var src = byId('vlSpendSource');
    if (chartBox) {
      var todayIso = new Date().toISOString().slice(0, 10);
      var billed = cs.status === 'OK';
      var series = days.slice().sort(byDay).map(function(d) {
        var day = String(d.day).slice(0, 10);
        return {day: day, bar: billed ? num(d.ai_net_usd) : num(d.total_tokens), line: num(d.interactions), partial: day >= todayIso, raw: d};
      });
      if (!series.length) {
        setKids(chartBox, [h('div', {class: 'chart-empty'}, [gdu ? 'No daily rows in vibelift_mart yet.'
          : 'Daily spend and usage appear here on a live project (Cloud Billing export + vibelift_mart).'])]);
        setKids(legend, []);
      } else {
        setKids(chartBox, [barLineChart(series, {
          ariaLabel: billed ? 'Daily billed AI spend and turns' : 'Daily tokens and turns',
          barFmt: billed ? function(v) { return usdText(v, 0); } : function(v) { return fmtTokens(v); },
          lineFmt: intText,
          lineLabel: 'turns',
          refLine: billed && cs.dailyPace ? cs.dailyPace : null,
          refLabel: billed && cs.dailyPace ? 'budget pace ' + usdText(cs.dailyPace, 0) + '/day' : null,
          tip: function(r) {
            var d = r.raw;
            return r.day + (r.partial ? ' (today, partial)' : '') + ': ' +
              (billed ? (num(d.ai_net_usd) !== null ? usdText(num(d.ai_net_usd)) + ' billed AI' : 'not billed yet') : fmtTokens(d.total_tokens) + ' tokens') +
              ' \u00b7 ' + fmtInt(d.interactions) + ' turns \u00b7 ' + fmtInt(d.sessions) + ' sessions' +
              (num(d.usd_per_1k_turns) !== null ? ' \u00b7 ' + usdText(num(d.usd_per_1k_turns)) + ' per 1k turns' : '');
          },
        })]);
        setKids(legend, [
          h('span', {}, [h('i', {style: 'background:#2563eb'}), billed ? 'Billed AI spend per day' : 'Total tokens per day']),
          h('span', {}, [h('i', {style: 'background:#10b981'}), 'Turns (right axis)']),
          billed && cs.dailyPace ? h('span', {}, [h('i', {style: 'background:#b45309'}), 'Daily budget pace']) : null,
          series.some(function(r) { return r.partial && r.bar !== null; })
            ? h('span', {}, [h('i', {style: 'background:#bfdbfe'}), 'Today (partial)']) : null]);
      }
    }
    if (src) {
      src.textContent = 'Sources: Cloud Billing export (AI services only, project-level, never split per agent or user) \u00b7 ' +
        'vibelift_mart daily turns \u00b7 Vertex AI model usage \u00d7 list price for the selected time range \u00b7 ' +
        'the budget is stored only in this browser.';
    }
  }

  function budgetCard(cs) {
    var btn = h('button', {type: 'button', class: 'btn btn-xs', onclick: function() { toggleBudgetForm(true); }}, [cs.budget ? 'Change budget' : 'Set monthly budget']);
    if (!cs.budget) {
      return kpi('Monthly AI budget', 'Not set', 'Track month-to-date burn and the run-rate against a budget. Stored in this browser only.', btn);
    }
    if (cs.status !== 'OK') {
      return kpi('Monthly AI budget', usdText(cs.budget, 0), 'Needs the Cloud Billing export to measure burn.', btn);
    }
    var cls = {on_track: 'vl-kpi-ok', at_risk: 'vl-kpi-warn', over: 'vl-kpi-bad'}[cs.budgetStatus];
    var words = {on_track: 'On track', at_risk: 'At risk', over: 'Projected over budget'}[cs.budgetStatus];
    var bar = h('div', {class: 'vl-progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100',
                        'aria-valuenow': String(Math.round(Math.min(100, cs.budgetUsedPct)))}, [
      h('div', {class: 'vl-progress-used', style: 'width:' + Math.min(100, cs.budgetUsedPct).toFixed(1) + '%'}),
      cs.projectedPct !== null ? h('div', {class: 'vl-progress-proj', style: 'left:' + Math.min(100, cs.projectedPct).toFixed(1) + '%',
                                          title: 'Projected month-end: ' + pctText(cs.projectedPct) + ' of budget'}) : null,
    ]);
    return kpi('Monthly AI budget \u00b7 ' + words, pctText(cs.budgetUsedPct) + ' used',
      usdText(cs.mtd) + ' of ' + usdText(cs.budget, 0) + (cs.projectedPct !== null ? ' \u00b7 projected ' + pctText(cs.projectedPct) : ''),
      h('div', {}, [bar, btn]), cls);
  }

  function toggleBudgetForm(show) {
    var box = byId('vlBudgetForm');
    if (!box) return;
    if (!show) { box.classList.add('hidden'); box.replaceChildren(); resized(); return; }
    var cur = readBudget();
    var input = h('input', {type: 'number', min: '0', step: '100', value: cur ? String(cur) : '', placeholder: 'e.g. 5000', 'aria-label': 'Monthly AI budget in USD'});
    var save = h('button', {type: 'button', class: 'btn btn-primary btn-xs', onclick: function() {
      var v = num(input.value);
      writeBudget(v && v > 0 ? v : null);
      toggleBudgetForm(false);
      renderSpend(state());
    }}, ['Save']);
    var clear = h('button', {type: 'button', class: 'btn btn-xs', onclick: function() { writeBudget(null); toggleBudgetForm(false); renderSpend(state()); }}, ['Remove budget']);
    var cancel = h('button', {type: 'button', class: 'btn btn-xs', onclick: function() { toggleBudgetForm(false); }}, ['Cancel']);
    setKids(box, [h('label', {}, ['Monthly AI budget (USD) ', input]), save, cur ? clear : null, cancel,
                  h('span', {class: 'vl-d-note'}, ['Compared with billed AI spend from the Cloud Billing export. Saved in this browser for this project.'])]);
    box.classList.remove('hidden');
    input.focus();
    input.addEventListener('keydown', function(ev) { if (ev.key === 'Enter') save.click(); if (ev.key === 'Escape') cancel.click(); });
    resized();
  }

  // ===================================================================================================
  // Part 8: Users adoption
  // ===================================================================================================
  function renderAdoption(st) {
    var panel = byId('vlAdoptionPanel');
    if (!panel || !st) return;
    var days = st.ge_daily_usage && Array.isArray(st.ge_daily_usage.days) ? st.ge_daily_usage.days : [];
    var ad = vlAdoption(days, new Date().toISOString());
    var grid = byId('vlAdoptionKpis');
    var chartBox = byId('vlAdoptionChart');
    if (!ad) {
      if (grid) grid.replaceChildren();
      if (chartBox) chartBox.replaceChildren(h('div', {class: 'chart-empty'}, ['No daily rows in vibelift_mart yet.']));
      return;
    }
    var complete = ad.series.filter(function(s) { return !s.partial; });
    var spark = sparkline(complete.map(function(s) { return s.users; }), {width: 160, height: 26, color: '#10b981', fill: 'rgba(16,185,129,0.12)',
                                                                          label: 'Daily active users, last ' + complete.length + ' days'});
    if (grid) {
      grid.replaceChildren(
        kpi('Active users \u00b7 ' + shortDay(ad.latestDay), fmtInt(ad.activeUsersLatest), 'Latest complete day (UTC)', spark ? h('div', {class: 'vl-kpi-spark'}, [spark]) : null),
        kpi('Daily active users \u00b7 ' + ad.daysCounted + '-day avg', intText(ad.activeUsersAvg7), 'Average of daily distinct users'),
        kpi('Sessions per day', intText(ad.sessionsPerDay7), ad.daysCounted + '-day average'),
        kpi('Turns per session', ad.turnsPerSession7 !== null ? ad.turnsPerSession7.toFixed(1) : DASH, 'Turns \u00f7 sessions, ' + ad.daysCounted + ' days'),
        kpi('Failed turns', ad.failedRatePct7 !== null ? pctText(ad.failedRatePct7) : DASH, 'Failed \u00f7 all turns, ' + ad.daysCounted + ' days',
            null, ad.failedRatePct7 !== null && ad.failedRatePct7 >= 2 ? 'vl-kpi-warn' : null));
    }
    if (chartBox) {
      chartBox.replaceChildren(barLineChart(ad.series.map(function(s) { return {day: s.day, bar: s.users, line: s.sessions, partial: s.partial, raw: s}; }), {
        ariaLabel: 'Daily active users and sessions', barColor: '#10b981', partialColor: '#bbf7d0', lineColor: '#2563eb', lineLabel: 'sessions',
        tip: function(r) {
          return r.day + (r.partial ? ' (today, partial)' : '') + ': ' + fmtInt(r.raw.users) + ' active users \u00b7 ' + fmtInt(r.raw.sessions) +
            ' sessions \u00b7 ' + fmtInt(r.raw.turns) + ' turns';
        },
      }));
    }
    var partialKey = byId('vlAdoptionPartialKey');
    if (partialKey) partialKey.classList.toggle('hidden', !ad.series.some(function(s) { return s.partial && s.users !== null; }));
    var src = byId('vlAdoptionSource');
    if (src) {
      src.textContent = 'Source: ' + ((st.ge_daily_usage && st.ge_daily_usage.mart_dataset) || 'vibelift_mart') +
        ' daily totals (all Gemini Enterprise apps; not filtered by the app selector) \u00b7 averages use complete days only.';
    }
  }

  // ===================================================================================================
  // Part 9: freshness chip, URL state, keyboard and ARIA
  // ===================================================================================================
  function renderFreshness() {
    var chip = byId('vlFreshChip');
    if (!chip) return;
    var fl = rawFleet();
    var mode = 'pending';
    try { mode = fleetRefreshMode; } catch (e) { mode = 'pending'; }
    var gen = fl && fl.generated_at ? Date.parse(fl.generated_at) : NaN;
    var cls = 'vl-fresh-wait';
    var text = 'Loading\u2026';
    if (!isNaN(gen)) {
      var age = Math.max(0, (Date.now() - gen) / 1000);
      var ago = age < 90 ? Math.round(age) + ' s ago' : (age < 5400 ? Math.round(age / 60) + ' min ago' : Math.round(age / 3600) + ' h ago');
      if (mode === 'snapshot') { cls = 'vl-fresh-warn'; text = 'Snapshot \u00b7 ' + ago; }
      else if (age < 180) { cls = 'vl-fresh-ok'; text = 'Live \u00b7 updated ' + ago; }
      else if (age < 900) { cls = 'vl-fresh-warn'; text = 'Updated ' + ago; }
      else { cls = 'vl-fresh-bad'; text = 'Stale \u00b7 updated ' + ago; }
      chip.title = 'Fleet data collected ' + new Date(gen).toLocaleString() + '. Cloud Monitoring is usually 3\u201310 min behind. Click to refresh.';
    }
    chip.className = 'btn vl-fresh ' + cls;
    chip.replaceChildren(h('i', {class: 'vl-fresh-dot'}), text);
  }

  function urlParams() {
    try { return new URLSearchParams(location.search); } catch (e) { return null; }
  }
  function urlWindowHours() {
    var p = urlParams();
    var w = p ? Number(p.get('w')) : 0;
    return WINDOW_OPTIONS.indexOf(w) >= 0 ? w : 0;
  }
  function shareUrl() {
    var p = new URLSearchParams();
    try {
      if (currentTabIndex !== 6) p.set('tab', String(currentTabIndex));
      if (geScope && geScope !== 'all') p.set('scope', geScope);
      if (fleetWindowHours && fleetWindowHours !== 24) p.set('w', String(fleetWindowHours));
      if (!document.body.classList.contains('simple-mode')) p.set('advanced', '1');
    } catch (e) { /* main script not ready */ }
    if (openState) p.set('agent', openState.id);
    var st = tables.fleetAgentsBody;
    if (st && st.query) p.set('q', st.query);
    var qs = p.toString();
    return location.origin + location.pathname + (qs ? '?' + qs : '');
  }
  function syncUrl() {
    if (embedded()) return;
    try {
      var u = shareUrl();
      if (u !== location.href) history.replaceState(null, '', u);
    } catch (e) { /* about:srcdoc or blocked */ }
  }

  function onTab(tab) {
    document.querySelectorAll('.tabs-bar .tab-btn').forEach(function(b) {
      var on = b.classList.contains('active');
      b.setAttribute('aria-selected', on ? 'true' : 'false');
      b.tabIndex = on ? 0 : -1;
    });
    if (openState && openState.mode === 'inline' && tab !== 0) closeAgent(true);
    syncUrl();
  }

  function setupAria() {
    var nav = document.querySelector('.tabs-bar');
    if (!nav) return;
    nav.setAttribute('role', 'tablist');
    nav.querySelectorAll('.tab-btn').forEach(function(b) {
      var idx = String(b.id || '').replace('tabBtn', '');
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-controls', 'tabPanel' + idx);
      var panel = byId('tabPanel' + idx);
      if (panel) { panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', b.id); }
    });
    nav.addEventListener('keydown', function(ev) {
      var keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
      if (keys.indexOf(ev.key) < 0) return;
      var tabsList = Array.prototype.filter.call(nav.querySelectorAll('.tab-btn'), function(b) { return b.offsetParent !== null; });
      var i = tabsList.indexOf(document.activeElement);
      if (i < 0) return;
      ev.preventDefault();
      var next = ev.key === 'Home' ? 0 : (ev.key === 'End' ? tabsList.length - 1 : (i + (ev.key === 'ArrowRight' ? 1 : -1) + tabsList.length) % tabsList.length);
      var target = tabsList[next];
      try { switchTab(Number(String(target.id).replace('tabBtn', ''))); } catch (e) { return; }
      target.focus();
    });
  }

  function setupKeys() {
    document.addEventListener('keydown', function(ev) {
      if (ev.key === 'Escape' && paletteOpen()) { closePalette(); return; }
      if (ev.key === 'Escape' && openState) { closeAgent(); return; }
      var tag = ev.target && ev.target.tagName;
      var typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (ev.target && ev.target.isContentEditable);
      if (ev.key === '/' && !typing && !ev.metaKey && !ev.ctrlKey) {
        var tab = 6;
        try { tab = currentTabIndex; } catch (e) { tab = 6; }
        var target = {0: 'fleetAgentsBody', 4: 'powerUsersBody', 3: 'geMartByAppBody'}[tab];
        var st = target ? tables[target] : null;
        if (st && st.search && st.search.offsetParent !== null) { ev.preventDefault(); st.search.focus(); }
      }
    });
    var back = byId('vlDrawerBackdrop');
    if (back) back.addEventListener('click', function() { closeAgent(); });
  }

  // ---- Theme (light / dark) --------------------------------------------------------------------------
  // The saved choice wins; otherwise the MCP host's theme (when embedded); otherwise the OS setting.
  // The <head> script applies the same rule before first paint, so dark mode never flashes light.
  var THEME_KEY = 'vibelift.theme';
  var themeChoice = null;  // 'light' | 'dark' | null (follow the host or the OS)
  var hostTheme = null;
  function vlResolveTheme(choice, host, system) {
    if (choice === 'light' || choice === 'dark') return choice;
    if (host === 'light' || host === 'dark') return host;
    return system === 'dark' ? 'dark' : 'light';
  }
  function systemTheme() {
    try { return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'; } catch (e) { return 'light'; }
  }
  function currentTheme() { return vlResolveTheme(themeChoice, embedded() ? hostTheme : null, systemTheme()); }
  function applyTheme() {
    var theme = currentTheme();
    document.documentElement.setAttribute('data-theme', theme);
    var btn = byId('vlThemeBtn');
    if (btn) {
      var dark = theme === 'dark';
      btn.setAttribute('aria-pressed', dark ? 'true' : 'false');
      btn.textContent = dark ? '\u2600' : '\u263E';
      btn.title = dark ? 'Switch to the light theme' : 'Switch to the dark theme';
    }
  }
  function setTheme(choice) {
    themeChoice = choice === 'light' || choice === 'dark' ? choice : null;
    try {
      if (themeChoice) localStorage.setItem(THEME_KEY, themeChoice); else localStorage.removeItem(THEME_KEY);
    } catch (e) { /* storage blocked: the choice lasts for this page only */ }
    applyTheme();
  }
  function toggleTheme() { setTheme(currentTheme() === 'dark' ? 'light' : 'dark'); }
  function setHostTheme(theme) {
    hostTheme = theme === 'light' || theme === 'dark' ? theme : null;
    applyTheme();
  }
  function setupTheme() {
    try {
      var saved = localStorage.getItem(THEME_KEY);
      themeChoice = saved === 'light' || saved === 'dark' ? saved : null;
    } catch (e) { themeChoice = null; }
    applyTheme();
    var btn = byId('vlThemeBtn');
    if (btn) btn.addEventListener('click', toggleTheme);
    try {
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      var follow = function() { if (!themeChoice) applyTheme(); };
      if (mq.addEventListener) mq.addEventListener('change', follow); else if (mq.addListener) mq.addListener(follow);
    } catch (e) { /* no matchMedia */ }
  }

  // ===================================================================================================
  // Part 11: command palette (Cmd/Ctrl+K): jump to agents, users, sessions, apps, time ranges, actions
  // ===================================================================================================
  // Ranks text against a query: exact > prefix > word start > substring > all words > letters in order.
  // Returns null when the text does not match.
  function vlPaletteScore(query, text) {
    var q = String(query === null || query === undefined ? '' : query).trim().toLowerCase();
    var t = String(text === null || text === undefined ? '' : text).toLowerCase();
    if (!q) return 0;
    if (!t) return null;
    if (t === q) return 1000;
    if (t.indexOf(q) === 0) return 800 - Math.min(t.length, 100) / 10;
    var spaced = ' ' + t.replace(/[^a-z0-9@]+/g, ' ');
    if (spaced.indexOf(' ' + q) >= 0) return 600 - spaced.indexOf(' ' + q) / 10;
    var at = t.indexOf(q);
    if (at >= 0) return 400 - Math.min(at, 100) / 10;
    var words = q.split(/\s+/).filter(Boolean);
    if (words.length > 1 && words.every(function(w) { return t.indexOf(w) >= 0; })) return 300;
    var pos = -1;
    var gaps = 0;
    for (var k = 0; k < q.length; k++) {
      if (q[k] === ' ') continue;
      var next = t.indexOf(q[k], pos + 1);
      if (next < 0) return null;
      if (pos >= 0) gaps += next - pos - 1;
      pos = next;
    }
    return Math.max(1, 200 - gaps);
  }
  // items: [{title, keywords?, boost?, hideWhenEmpty?}]; returns the matching items, best first.
  function vlPaletteFilter(items, query, limit) {
    var q = String(query === null || query === undefined ? '' : query).trim();
    var scored = [];
    (items || []).forEach(function(it, idx) {
      if (!q && it.hideWhenEmpty) return;
      var best = null;
      [it.title].concat(it.keywords || []).forEach(function(text) {
        var sc = vlPaletteScore(q, text);
        if (sc !== null && (best === null || sc > best)) best = sc;
      });
      if (best === null) return;
      scored.push({item: it, base: best, score: best + (q ? (it.boost || 0) : 0), idx: idx});
    });
    // Letters-in-order matches only help with typos: drop them when something matches directly.
    var direct = scored.some(function(x) { return x.base >= 300; });
    if (direct) scored = scored.filter(function(x) { return x.base >= 300; });
    scored.sort(function(a, b) { return (b.score - a.score) || (a.idx - b.idx); });
    return scored.slice(0, pick(limit, 60)).map(function(x) { return x.item; });
  }

  var palette = null;  // {root, input, list, items, shown, active, opener}
  function isMac() { try { return /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || ''); } catch (e) { return false; } }
  function clickById(id) { var b = byId(id); if (b) b.click(); }
  function visible(node) { return !!(node && node.offsetParent !== null); }
  function selectOption(id, value) {
    var sel = byId(id);
    if (!sel) return;
    sel.value = value;
    sel.dispatchEvent(new Event('change', {bubbles: true}));
  }
  function revealTable(tbodyId) {
    var body = byId(tbodyId);
    var panel = body && body.closest('.panel');
    if (panel && panel.scrollIntoView) panel.scrollIntoView({block: 'start'});
  }

  function paletteItems() {
    var items = [];
    var st = state() || {};
    var fl = rawFleet() || {};
    // Tabs that are visible right now (advanced and simulator tabs only when shown).
    Array.prototype.forEach.call(document.querySelectorAll('.tabs-bar .tab-btn'), function(btn) {
      if (!visible(btn) || btn.classList.contains('active')) return;
      var idx = Number(String(btn.id || '').replace('tabBtn', ''));
      var label = (btn.textContent || '').replace(/\s+/g, ' ').trim();
      items.push({kind: 'Go to', group: 'Go to', title: label, keywords: ['tab ' + label, 'go ' + label], boost: 40,
                  run: function() { try { switchTab(idx); } catch (e) { /* not ready */ } }});
    });
    var agents = Array.isArray(fl.agents) ? fl.agents.slice() : [];
    var counts = vlHealthCounts(agents);
    HEALTH_LEVELS.forEach(function(l) {
      if (!counts[l.id]) return;
      items.push({kind: 'Show', group: 'Show', title: 'Show ' + l.label.toLowerCase() + ' agents',
                  sub: counts[l.id] + (counts[l.id] === 1 ? ' agent' : ' agents'), keywords: ['health ' + l.label, 'filter ' + l.label],
                  dot: l.id, boost: l.rank <= 2 ? 20 : 0, hideWhenEmpty: l.rank > 2,  // problems first; the rest on search
                  run: function() { focusAgents({health: l.id}); }});
    });
    agents.sort(function(a, b) { return vlHealth(a).rank - vlHealth(b).rank; });
    agents.forEach(function(a) {
      var hl = vlHealth(a);
      var b = a.backend || {};
      items.push({kind: 'Agent', group: 'Agents', title: String(a.display_name || a.agent_id || DASH), dot: hl.id,
                  sub: [hl.label, a.engine_display_name || a.engine_id, TYPE_LABELS[a.type] || a.type].filter(Boolean).join(' \u00b7 '),
                  keywords: [a.agent_id, a.engine_display_name, a.type_label, b.service, b.reasoning_engine_id].concat(b.models || []),
                  boost: 30, run: function() { openAgent(a); }});
    });
    agents.forEach(function(a) {
      if (!vlAlertTarget(a, fl.project_id)) return;
      items.push({kind: 'Alert', group: 'Alerts', title: 'Create alert policy: ' + String(a.display_name || a.agent_id || DASH),
                  sub: 'Cloud Monitoring · gcloud commands', keywords: ['alert', 'alerting', 'monitoring', 'policy', 'notify'],
                  hideWhenEmpty: true, run: function() { openAgent(a, null, {alert: true}); }});
    });
    var scope = byId('geScopeSelect');
    if (scope) {
      Array.prototype.forEach.call(scope.options, function(opt) {
        if (opt.value === scope.value) return;
        items.push({kind: 'App', group: 'Apps & regions', title: opt.textContent, sub: 'Show only this scope',
                    keywords: ['scope', 'app', 'region'], run: function() { selectOption('geScopeSelect', opt.value); }});
      });
    }
    var win = byId('fleetWindow');
    if (win) {
      Array.prototype.forEach.call(win.options, function(opt) {
        if (opt.value === win.value) return;
        items.push({kind: 'Time range', group: 'Time range', title: opt.textContent, keywords: ['time range', 'window', 'period'],
                    run: function() { selectOption('fleetWindow', opt.value); }});
      });
    }
    var uc = st.user_centric || {};
    (Array.isArray(uc.power_users_ldap) ? uc.power_users_ldap : []).forEach(function(u) {
      var ldap = u.user_ldap || vlUserLabel(u.user_email);
      if (!ldap || ldap === DASH) return;
      items.push({kind: 'User', group: 'Users', title: ldap, sub: u.primary_agent ? 'Mostly ' + u.primary_agent : 'Top user',
                  keywords: [u.department, u.primary_agent], hideWhenEmpty: true,
                  run: function() { try { switchTab(4); } catch (e) { return; } setTableQuery('powerUsersBody', ldap); revealTable('powerUsersBody'); }});
    });
    (Array.isArray(uc.ge_sessions) ? uc.ge_sessions : []).slice(0, 80).forEach(function(se) {
      var sid = String(se.session_id || '');
      if (!sid) return;
      items.push({kind: 'Session', group: 'Sessions', title: 'Session \u2026' + sid.slice(-8),
                  sub: [vlUserLabel(se.user_email), se.agent_name, se.session_end ? fmtAgo(se.session_end) : null].filter(function(x) { return x && x !== DASH; }).join(' \u00b7 '),
                  keywords: [sid, vlUserLabel(se.user_email), se.agent_name], hideWhenEmpty: true,
                  run: function() { try { switchTab(4); } catch (e) { return; } setTableQuery('geSessionsBody', sid); revealTable('geSessionsBody'); }});
    });
    var dark = currentTheme() === 'dark';
    var actions = [
      {title: 'Refresh data', keywords: ['reload', 'sync'], run: function() { clickById('syncGcpBtn'); }},
      embedded() ? null : {title: 'Copy link to this view', keywords: ['share', 'url'], run: function() { clickById('vlCopyLinkBtn'); }},
      {title: 'Export PDF', keywords: ['print', 'report', 'download'], run: function() { clickById('exportPdfBtn'); }},
      {title: dark ? 'Use the light theme' : 'Use the dark theme', keywords: ['theme', 'dark mode', 'light mode', 'appearance'],
       run: function() { setTheme(dark ? 'light' : 'dark'); }},
      themeChoice ? {title: embedded() ? 'Match the Gemini Enterprise theme' : 'Match the system theme', keywords: ['theme', 'auto'],
                     run: function() { setTheme(null); }} : null,
      tables.fleetAgentsBody && tables.fleetAgentsBody.compactBtn ? {
        title: tables.fleetAgentsBody.compactBtn.getAttribute('aria-pressed') === 'true' ? 'Show full agent rows' : 'Use compact agent rows',
        keywords: ['compact', 'density', 'rows'], run: function() { tables.fleetAgentsBody.compactBtn.click(); }} : null,
      byId('advancedToggleBtn') ? {title: byId('advancedToggleBtn').getAttribute('aria-pressed') === 'true' ? 'Hide advanced tabs' : 'Show advanced tabs',
                                   keywords: ['advanced', 'optimizer', 'sdk'], run: function() { clickById('advancedToggleBtn'); }} : null,
      embedded() && byId('btnModeFullscreen') ? {title: 'Toggle fullscreen', keywords: ['side panel', 'expand'], run: function() { clickById('btnModeFullscreen'); }} : null,
      byId('vlSpendPanel') ? {title: 'Set the monthly AI budget', keywords: ['budget', 'cost', 'spend'],
                              run: function() { try { switchTab(3); } catch (e) { return; } toggleBudgetForm(true); revealTable('vlSpendKpis'); }} : null,
    ];
    actions.forEach(function(a) {
      if (!a) return;
      items.push({kind: 'Action', group: 'Actions', title: a.title, keywords: a.keywords, boost: 10, run: a.run});
    });
    return items;
  }

  var PALETTE_GROUPS = ['Go to', 'Show', 'Actions', 'Agents', 'Apps & regions', 'Time range'];
  function paletteVisibleItems(query) {
    var items = palette.items;
    if (String(query || '').trim()) return vlPaletteFilter(items, query, 60);
    // Empty query: a few items per group, in a fixed group order.
    var out = [];
    PALETTE_GROUPS.forEach(function(g) {
      var groupItems = items.filter(function(it) { return it.group === g && !it.hideWhenEmpty; });
      out = out.concat(groupItems.slice(0, g === 'Agents' ? 8 : 6));
    });
    return out;
  }
  function renderPaletteList() {
    var q = palette.input.value;
    var shown = paletteVisibleItems(q);
    palette.shown = shown;
    palette.active = shown.length ? Math.min(palette.active, shown.length - 1) : -1;
    var kids = [];
    var lastGroup = null;
    var grouped = !String(q).trim();
    shown.forEach(function(it, i) {
      if (grouped && it.group !== lastGroup) {
        kids.push(h('li', {class: 'vl-pal-group', role: 'presentation'}, [it.group]));
        lastGroup = it.group;
      }
      var li = h('li', {id: 'vlPalOpt' + i, class: 'vl-pal-item' + (i === palette.active ? ' vl-pal-active' : ''), role: 'option',
                        'aria-selected': i === palette.active ? 'true' : 'false'}, [
        it.dot ? h('span', {class: 'vl-dot vl-dot-' + it.dot, 'aria-hidden': 'true'}) : h('span', {class: 'vl-pal-bullet', 'aria-hidden': 'true'}),
        h('span', {class: 'vl-pal-title'}, [it.title]),
        it.sub ? h('span', {class: 'vl-pal-sub'}, [it.sub]) : null,
        h('span', {class: 'vl-pal-kind'}, [it.kind]),
      ]);
      li.addEventListener('mousemove', function() { if (palette.active !== i) { palette.active = i; markActive(); } });
      li.addEventListener('click', function() { runPaletteItem(i); });
      kids.push(li);
    });
    if (!shown.length) kids.push(h('li', {class: 'vl-pal-empty', role: 'presentation'}, ['No matches for \u201c' + String(q).trim() + '\u201d.']));
    setKids(palette.list, kids);
    markActive();
  }
  function markActive() {
    Array.prototype.forEach.call(palette.list.querySelectorAll('.vl-pal-item'), function(li) {
      var on = li.id === 'vlPalOpt' + palette.active;
      li.classList.toggle('vl-pal-active', on);
      li.setAttribute('aria-selected', on ? 'true' : 'false');
      if (on && li.scrollIntoView) li.scrollIntoView({block: 'nearest'});
    });
    if (palette.active >= 0) palette.input.setAttribute('aria-activedescendant', 'vlPalOpt' + palette.active);
    else palette.input.removeAttribute('aria-activedescendant');
  }
  function runPaletteItem(i) {
    var it = palette && palette.shown[i];
    if (!it) return;
    closePalette(true);
    try { it.run(); } catch (e) { /* the target view is not ready */ }
  }
  function buildPalette() {
    var root = byId('vlPalette');
    if (!root) return null;
    var input = h('input', {id: 'vlPaletteInput', class: 'vl-pal-input', type: 'text', role: 'combobox', autocomplete: 'off',
                            spellcheck: 'false', 'aria-expanded': 'true', 'aria-controls': 'vlPaletteList', 'aria-autocomplete': 'list',
                            'aria-label': 'Search agents, users, sessions, apps and commands',
                            placeholder: 'Search agents, users, sessions, apps or commands\u2026'});
    var list = h('ul', {id: 'vlPaletteList', class: 'vl-pal-list', role: 'listbox', 'aria-label': 'Results'});
    setKids(root, [
      h('div', {class: 'vl-pal-head'}, [h('span', {class: 'vl-pal-icon', 'aria-hidden': 'true'}, ['\u2315']), input,
                                        h('kbd', {class: 'vl-kbd'}, ['Esc'])]),
      list,
      h('div', {class: 'vl-pal-foot', 'aria-hidden': 'true'}, ['\u2191\u2193 move \u00b7 \u21b5 open \u00b7 Esc close \u00b7 ' +
                                                                 (isMac() ? '\u2318K' : 'Ctrl+K') + ' anywhere']),
    ]);
    input.addEventListener('input', function() { palette.active = 0; renderPaletteList(); });
    input.addEventListener('keydown', function(ev) {
      var n = palette.shown.length;
      if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
        ev.preventDefault();
        if (!n) return;
        palette.active = (palette.active + (ev.key === 'ArrowDown' ? 1 : n - 1)) % n;
        markActive();
      } else if (ev.key === 'Home' || ev.key === 'End') {
        if (!n) return;
        ev.preventDefault();
        palette.active = ev.key === 'Home' ? 0 : n - 1;
        markActive();
      } else if (ev.key === 'Enter') {
        ev.preventDefault();
        runPaletteItem(palette.active);
      } else if (ev.key === 'Escape') {
        ev.preventDefault();
        ev.stopPropagation();
        closePalette();
      } else if (ev.key === 'Tab') {
        ev.preventDefault();  // keep focus in the dialog; the list is driven from the input
      }
    });
    var back = byId('vlPaletteBackdrop');
    if (back) back.addEventListener('click', function() { closePalette(); });
    return {root: root, input: input, list: list, items: [], shown: [], active: 0, opener: null};
  }
  function openPalette(initialQuery) {
    if (!palette) palette = buildPalette();
    if (!palette) return;
    palette.opener = document.activeElement;
    palette.items = paletteItems();
    palette.input.value = initialQuery || '';
    palette.active = 0;
    var inline = embedded() && !useDrawer();  // side panel: the iframe is as tall as the page, so no fixed overlay
    palette.root.classList.toggle('vl-pal-inline', inline);
    palette.root.classList.remove('hidden');
    var back = byId('vlPaletteBackdrop');
    if (back) back.classList.toggle('hidden', inline);
    var btn = byId('vlPaletteBtn');
    if (btn) btn.setAttribute('aria-expanded', 'true');
    renderPaletteList();
    if (inline && palette.root.scrollIntoView) palette.root.scrollIntoView({block: 'start'});
    palette.input.focus();
    resized();
  }
  function closePalette(keepFocus) {
    if (!palette || palette.root.classList.contains('hidden')) return;
    palette.root.classList.add('hidden');
    var back = byId('vlPaletteBackdrop');
    if (back) back.classList.add('hidden');
    var btn = byId('vlPaletteBtn');
    if (btn) btn.setAttribute('aria-expanded', 'false');
    if (!keepFocus && palette.opener && palette.opener.focus) palette.opener.focus();
    resized();
  }
  function paletteOpen() { return !!(palette && !palette.root.classList.contains('hidden')); }
  function setupPalette() {
    var btn = byId('vlPaletteBtn');
    if (btn) {
      var kbd = btn.querySelector('kbd');
      if (kbd) kbd.textContent = isMac() ? '\u2318K' : 'Ctrl K';
      btn.addEventListener('click', function() { if (paletteOpen()) closePalette(); else openPalette(); });
    }
    document.addEventListener('keydown', function(ev) {
      if ((ev.metaKey || ev.ctrlKey) && !ev.altKey && !ev.shiftKey && String(ev.key).toLowerCase() === 'k') {
        ev.preventDefault();
        if (paletteOpen()) closePalette(); else openPalette();
      }
    });
  }

  // ===================================================================================================
  // Part 10: hooks called by the main script, and mount
  // ===================================================================================================
  function onFleet(fleet) {
    if (pendingAgentId && fleet && Array.isArray(fleet.agents) && fleet.agents.length) {
      var target = findAgent(pendingAgentId);
      pendingAgentId = null;
      if (target) openAgent(target, null);
    } else if (openState) {
      var a = findAgent(openState.id);
      var typing = document.activeElement && document.activeElement.closest && document.activeElement.closest('.vl-alert-form');
      if (a && !typing) renderOpenAgent(a);
    }
    renderFreshness();
    var st = state();
    if (st) renderSpend(st);
  }
  function onState(st) {
    if (!st) return;
    renderSpend(st);
    renderAdoption(st);
    renderFreshness();
  }

  function mount() {
    setupTheme();
    setupPalette();
    setupAria();
    setupKeys();
    var p = urlParams();
    if (p && p.get('agent')) pendingAgentId = p.get('agent');
    var w = urlWindowHours();
    var sel = byId('fleetWindow');
    if (w && sel) sel.value = String(w);
    smartTable('fleetAgentsBody', {
      placeholder: 'Search agents, apps, runtimes, models\u2026  ( / )', noun: 'agents', exportName: 'vibelift-agents',
      compactKey: 'vibelift.agentsCompact',
      isDetailRow: function(tr) { return tr.classList.contains('vl-agent-detail-row'); },
      facets: [
        {key: 'health', label: 'Health', dot: true, order: HEALTH_LEVELS.map(function(l) { return l.id; }),
         labels: HEALTH_LEVELS.reduce(function(acc, l) { acc[l.id] = l.label; return acc; }, {})},
        {key: 'type', label: 'Type', order: ['ADK', 'A2A', 'LOW_CODE', 'MANAGED'], labels: TYPE_LABELS},
      ],
      onQuery: debounce(syncUrl, 400),
    });
    var q = p ? p.get('q') : null;
    if (q) setTableQuery('fleetAgentsBody', q);
    smartTable('geSessionsBody', {placeholder: 'Search sessions by user, app, agent or id\u2026', noun: 'sessions', pageSize: 25,
                                  noSort: [0], exportName: 'vibelift-sessions',
                                  isDetailRow: function(tr) { return tr.classList.contains('session-turns-row'); }});
    smartTable('powerUsersBody', {placeholder: 'Search users or departments\u2026', noun: 'users', pageSize: 25, hideEmptyColumns: true,
                                  exportName: 'vibelift-users'});
    smartTable('geMartDailyBody', {placeholder: 'Filter days (e.g. 2026-10)\u2026', noun: 'days', pageSize: 10, exportName: 'vibelift-daily-usage'});
    smartTable('geMartByAppBody', {placeholder: 'Filter by day, app, agent or model\u2026', noun: 'rows', pageSize: 15, exportName: 'vibelift-daily-by-app'});
    ['fleetUnregisteredBody', 'execUnregisteredBody'].forEach(function(id) {
      smartTable(id, {placeholder: 'Search standalone runtimes\u2026', noun: 'runtimes', noSort: [6], exportName: 'vibelift-standalone-runtimes'});
    });
    ['fleetSkillsMcpBody', 'liveSkillsMcpBody'].forEach(function(id) {
      smartTable(id, {placeholder: 'Search tools, sub-agents and MCP servers\u2026', noun: 'items', exportName: 'vibelift-skills-mcp'});
    });
    smartTable('aiveUsageBody', {placeholder: 'Search activity\u2026', noun: 'events', pageSize: 10, exportName: 'vibelift-activity'});
    smartTable('vocRatingsBody', {placeholder: 'Search feedback\u2026', noun: 'ratings', pageSize: 10, exportName: 'vibelift-feedback'});
    smartTable('decoratorEventsBody', {placeholder: 'Search events\u2026', noun: 'events', pageSize: 10, exportName: 'vibelift-sdk-events'});
    ['modelUsageBody', 'liveTeAgentsBody', 'liveDriftModelsBody', 'cloudRunServicesBody'].forEach(function(id) {
      smartTable(id, {toolbar: false});
    });
    var chip = byId('vlFreshChip');
    if (chip) {
      chip.addEventListener('click', function() { try { refreshFleet(true); } catch (e) { /* not ready */ } });
      setInterval(renderFreshness, 5000);
    }
    var copyBtn = byId('vlCopyLinkBtn');
    if (copyBtn) {
      if (embedded()) copyBtn.classList.add('hidden');
      copyBtn.addEventListener('click', function() { copyText(shareUrl(), copyBtn); });
    }
  }

  var api = {
    // pure (tested in Node)
    health: vlHealth, healthCounts: vlHealthCounts, serverErrorSeverity: vlServerErrorSeverity, healthRules: HEALTH_RULES, healthLevels: HEALTH_LEVELS,
    healthRulesText: healthRulesText, parseSortValue: vlParseSortValue, compare: vlCompare, toCsv: vlToCsv,
    costSummary: vlCostSummary, cacheSavings: vlCacheSavings, spendDelta: vlSpendDelta, adoption: vlAdoption,
    trendTotals: vlTrendTotals, runtimeKey: vlRuntimeKey, agentPrompt: vlAgentPrompt, safeExternalUrl: safeExternalUrl, userLabel: vlUserLabel,
    resolveTheme: vlResolveTheme,
    paletteScore: vlPaletteScore, paletteFilter: vlPaletteFilter,
    alertPolicies: vlAlertPolicies, alertTarget: vlAlertTarget, autoAlertWindow: vlAutoAlertWindow,
    // DOM (browser only)
    mount: mount, smartTable: smartTable, setTableQuery: setTableQuery, setTableFacet: setTableFacet,
    renderHealthStrip: renderHealthStrip, agentRowDecorate: agentRowDecorate, healthCell: healthCell, typeCell: typeCell, agentSpark: agentSpark,
    openAgent: openAgent, closeAgent: closeAgent, decorateAttention: decorateAttention, attentionSummary: attentionSummary,
    spendDeltaNode: spendDeltaNode, requestsSparkNode: requestsSparkNode, usersSparkNode: usersSparkNode,
    setHostCapabilities: setHostCapabilities, urlWindowHours: urlWindowHours, syncUrl: syncUrl, onTab: onTab,
    setTheme: setTheme, toggleTheme: toggleTheme, setHostTheme: setHostTheme, currentTheme: currentTheme,
    openPalette: openPalette, closePalette: closePalette,
    onFleet: onFleet, onState: onState, onTraceRow: onTraceRow, focusAgents: focusAgents,
  };
  return api;
})();

if (typeof document !== 'undefined' && document.body) {
  try { VL.mount(); } catch (err) { if (typeof console !== 'undefined') console.warn('VibeLift enhancements did not mount: ' + (err && err.message ? err.message : 'unknown error')); }
}
