// FinOps Dashboard dashboard enhancements.
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
      ' requests) or \u2265 ' + R.loopCallsPerRequest + ' LLM calls per request (or the agent\u2019s own cap, ' +
      '1.5 \u00d7 the p99 of its normal runs, when it has 30 days of session history). Degraded: 5xx \u2265 ' +
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
    // Agents with enough session history carry their own cap (server: finops.apply_loop_thresholds).
    var ownCap = num(m.loop_threshold_calls_per_request);
    var loopCap = ownCap !== null && ownCap > 0 ? ownCap : R.loopCallsPerRequest;
    var reasons = [];
    var id = 'healthy';
    if (cpr !== null && cpr >= loopCap) {
      id = 'failing';
      reasons.push(cpr.toFixed(1) + ' LLM calls per request (\u2265 ' + loopCap +
        (loopCap === ownCap ? ', this agent\u2019s cap from its last 30 days of sessions' : '') + '): possible agent loop.');
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

  // ---- Tokenomics & FinOps pure helpers ------------------------------------------------------------
  // Mirror of finops_dashboard/reference/vertex_ai_reference.json (list prices per 1M tokens) for the models the
  // what-if math needs before live rate cards arrive. tests/test_reference_data.py fails if it drifts.
  var DEFAULT_RATE_CARDS = {
    'gemini-2.5-pro': {input: 1.25, output: 10.0, cached_read: 0.125, cache_write: 1.25},
    'gemini-3.8-flash': {input: 0.75, output: 3.75, cached_read: 0.075, cache_write: 0.75},
    'gemini-2.5-flash': {input: 0.30, output: 2.50, cached_read: 0.03, cache_write: 0.30},
    'gemini-3.1-flash-lite': {input: 0.25, output: 1.50, cached_read: 0.025, cache_write: 0.25},
    'gemini-2.0-flash': {input: 0.15, output: 0.60, cached_read: 0.0375, cache_write: 0.15},
    'gemini-1.5-pro': {input: 1.25, output: 5.0, cached_read: 0.3125, cache_write: 1.25},
    'gemini-1.5-flash': {input: 0.075, output: 0.30, cached_read: 0.01875, cache_write: 0.075},
  };
  // gemini-2.5-flash retires on 2026-10-20; Google lists gemini-3.8-flash as its replacement.
  var FLASH_REPRICE_TARGET = 'gemini-3.8-flash';
  var REPRICE_TARGETS = [FLASH_REPRICE_TARGET, 'gemini-3.1-flash-lite'];

  function resolveRateCards(rawCards) {
    var out = {};
    Object.keys(DEFAULT_RATE_CARDS).forEach(function(k) { out[k] = DEFAULT_RATE_CARDS[k]; });
    Object.keys(rawCards || {}).forEach(function(k) {
      var c = rawCards[k];
      if (c && num(c.input) !== null && num(c.output) !== null) {
        out[k] = {
          input: num(c.input),
          output: num(c.output),
          cached_read: num(c.cached_read) !== null ? num(c.cached_read) : num(c.input),
          cache_write: num(c.cache_write) !== null ? num(c.cache_write) : num(c.input),
        };
      }
    });
    return out;
  }

  function matchRateCard(modelName, cards) {
    var m = String(modelName || '').trim().toLowerCase();
    if (!m) return null;
    if (cards[m]) return {model: m, card: cards[m]};
    var keys = Object.keys(cards).sort(function(a, b) { return b.length - a.length; });
    for (var i = 0; i < keys.length; i++) {
      if (m.indexOf(keys[i].toLowerCase()) >= 0) return {model: keys[i], card: cards[keys[i]]};
    }
    return null;
  }

  // 1. Budget runway gauge math from vlCostSummary(days, budgetUsd)
  function vlBudgetRunway(cs) {
    if (!cs || cs.status !== 'OK') return {status: 'NO_BILLING', budget: cs ? cs.budget : null};
    if (!cs.budget || cs.budget <= 0) {
      return {status: 'NO_BUDGET', mtd: cs.mtd, avg7: cs.avg7, projection: cs.projection, month: cs.month, lastDay: cs.lastDay};
    }
    if (cs.avg7 === null || cs.avg7 <= 0) {
      return {status: 'NO_PACE', budget: cs.budget, mtd: cs.mtd, month: cs.month, lastDay: cs.lastDay};
    }
    var remainingUsd = cs.budget - cs.mtd;
    var daysUntilExhausted = remainingUsd > 0 ? remainingUsd / cs.avg7 : 0;
    var lastDayNum = Number(String(cs.lastDay || '').slice(8, 10)) || 1;
    var exhaustDayFloat = lastDayNum + daysUntilExhausted;
    var exhaustsInMonth = remainingUsd <= 0 || exhaustDayFloat <= cs.daysInMonth;
    var exhaustDayOfMonth = exhaustsInMonth ? Math.max(1, Math.min(cs.daysInMonth, Math.ceil(exhaustDayFloat))) : null;
    var exhaustDate = exhaustDayOfMonth !== null
      ? cs.month + '-' + (exhaustDayOfMonth < 10 ? '0' + exhaustDayOfMonth : String(exhaustDayOfMonth))
      : null;
    var dailyPace = cs.dailyPace || (cs.budget / cs.daysInMonth);
    var paceRatio = dailyPace > 0 ? cs.avg7 / dailyPace : null;
    var safeDailyAllowance = cs.daysRemaining > 0 ? Math.max(0, remainingUsd) / cs.daysRemaining : 0;
    var varianceUsd = cs.projection !== null ? cs.budget - cs.projection : null;
    return {
      status: remainingUsd <= 0 ? 'EXHAUSTED' : (exhaustsInMonth ? 'AT_RISK' : 'RUNWAY_CLEAR'),
      budget: cs.budget,
      mtd: cs.mtd,
      remainingUsd: remainingUsd,
      avg7: cs.avg7,
      dailyPace: dailyPace,
      paceRatio: paceRatio,
      daysInMonth: cs.daysInMonth,
      daysRemaining: cs.daysRemaining,
      lastDay: cs.lastDay,
      lastDayOfMonth: lastDayNum,
      month: cs.month,
      daysUntilExhausted: daysUntilExhausted,
      exhaustsInMonth: exhaustsInMonth,
      exhaustDayOfMonth: exhaustDayOfMonth,
      exhaustDate: exhaustDate,
      safeDailyAllowance: safeDailyAllowance,
      projection: cs.projection,
      varianceUsd: varianceUsd,
      usedPct: cs.budgetUsedPct,
      projectedPct: cs.projectedPct,
    };
  }

  // 2. Spend drift waterfall steps + per-driver model breakdown
  function vlDriftWaterfall(drift) {
    if (!drift || drift.status !== 'LIVE') return null;
    var before = num(drift.cost_before_usd);
    var now = num(drift.cost_now_usd);
    if (before === null || now === null) return null;
    var running = before;
    var steps = [{
      id: 'before',
      kind: 'anchor',
      label: 'Previous period',
      sub: drift.before_window || '',
      value: before,
      start: 0,
      end: before,
    }];
    var byModel = Array.isArray(drift.by_model) ? drift.by_model : [];
    (drift.drivers || []).forEach(function(d) {
      var delta = num(d.change_usd) || 0;
      var s = running;
      var e = running + delta;
      running = e;
      var id = String(d.driver_id || '');
      var models = byModel.map(function(m) {
        var drv = (m && m.drivers_usd) || {};
        var md = num(drv[id]);
        return {
          model: m.model,
          deltaUsd: md,
          costBefore: num(m.cost_before_usd),
          costNow: num(m.cost_now_usd),
          callsBefore: num(m.invocations_before),
          callsNow: num(m.invocations_now),
        };
      }).filter(function(m) { return m.deltaUsd !== null && Math.abs(m.deltaUsd) >= 0.005; })
        .sort(function(a, b) { return Math.abs(b.deltaUsd) - Math.abs(a.deltaUsd); });
      steps.push({
        id: id,
        kind: 'delta',
        label: String(d.name || id),
        description: String(d.description || ''),
        delta: delta,
        sharePct: num(d.share_of_change_pct),
        pctOfBefore: before > 0 ? Math.round((delta / before) * 1000) / 10 : null,
        start: s,
        end: e,
        direction: Math.abs(delta) < 0.005 ? 'flat' : (delta > 0 ? 'up' : 'down'),
        models: models,
      });
    });
    steps.push({
      id: 'now',
      kind: 'anchor',
      label: 'This period',
      sub: drift.now_window || '',
      value: now,
      start: 0,
      end: now,
    });
    var peak = 0;
    steps.forEach(function(st) { peak = Math.max(peak, st.start, st.end, st.value || 0); });
    return {
      before: before,
      now: now,
      change: now - before,
      changePct: num(drift.change_pct),
      maxUsd: peak || 1,
      steps: steps,
    };
  }

  // 3. Thinking-token tax breakdown across Cloud Monitoring model_usage
  // In Cloud Monitoring (model_usage), output_tokens and reasoning_tokens are separate and both billed at card.output.
  function vlThinkingTax(mu, windowHours) {
    if (!mu || !Array.isArray(mu.models) || !mu.models.length) return null;
    var cards = resolveRateCards(mu.rate_cards);
    var hours = num(windowHours) || num(mu.window_hours) || 24;
    var scale30d = hours > 0 ? 720 / hours : 30;
    var rows = [];
    var totOut = 0;
    var totReason = 0;
    var totReasonUsd = 0;
    var totOutUsd = 0;
    var totEstUsd = 0;
    mu.models.forEach(function(m) {
      var outTok = num(m.output_tokens) || 0;
      var reasonTok = num(m.reasoning_tokens) || 0;
      var calls = num(m.invocations) || 0;
      var combinedOut = outTok + reasonTok;
      var matched = matchRateCard(m.model, cards);
      var outRate = matched ? matched.card.output : null;
      var reasonUsd = outRate !== null ? reasonTok * outRate / 1e6 : null;
      var visOutUsd = outRate !== null ? outTok * outRate / 1e6 : null;
      var combinedOutUsd = outRate !== null ? combinedOut * outRate / 1e6 : null;
      var estCost = num(m.est_cost_usd);
      totOut += outTok;
      totReason += reasonTok;
      if (reasonUsd !== null) totReasonUsd += reasonUsd;
      if (combinedOutUsd !== null) totOutUsd += combinedOutUsd;
      if (estCost !== null) totEstUsd += estCost;
      var reasonPerCall = calls > 0 ? Math.round(reasonTok / calls) : 0;
      var suggestedBudget = reasonPerCall > 0 ? Math.max(256, Math.min(8192, Math.round((reasonPerCall * 0.5) / 128) * 128)) : 1024;
      rows.push({
        model: m.model,
        invocations: calls,
        visibleOutputTokens: outTok,
        reasoningTokens: reasonTok,
        combinedOutputTokens: combinedOut,
        thinkingShareOfOutputPct: combinedOut > 0 ? 100 * reasonTok / combinedOut : 0,
        outputRatePer1M: outRate,
        reasoningCostUsd: reasonUsd,
        visibleOutputCostUsd: visOutUsd,
        estCostUsd: estCost,
        thinkingShareOfModelSpendPct: estCost && reasonUsd !== null ? 100 * reasonUsd / estCost : null,
        reasoningTokensPerCall: reasonPerCall,
        suggestedBudgetTokens: suggestedBudget,
        cap50SavingsWindowUsd: reasonUsd !== null ? reasonUsd * 0.5 : null,
        cap50Savings30dUsd: reasonUsd !== null ? reasonUsd * 0.5 * scale30d : null,
      });
    });
    rows.sort(function(a, b) { return (b.reasoningCostUsd || 0) - (a.reasoningCostUsd || 0); });
    var combinedAll = totOut + totReason;
    return {
      windowHours: hours,
      visibleOutputTokens: totOut,
      reasoningTokens: totReason,
      combinedOutputTokens: combinedAll,
      thinkingShareOfOutputPct: combinedAll > 0 ? 100 * totReason / combinedAll : 0,
      reasoningCostUsd: totReasonUsd,
      combinedOutputCostUsd: totOutUsd,
      totalEstCostUsd: totEstUsd,
      thinkingShareOfSpendPct: totEstUsd > 0 ? 100 * totReasonUsd / totEstUsd : null,
      cap50SavingsWindowUsd: totReasonUsd * 0.5,
      cap50Savings30dUsd: totReasonUsd * 0.5 * scale30d,
      models: rows,
    };
  }

  // 4. Model re-pricing what-if (both project-wide models and per-agent OTel metrics)
  function priceCloudMonitoringRow(m, card) {
    if (!m || !card) return null;
    var inp = num(m.input_tokens) || 0;
    var out = num(m.output_tokens) || 0;
    var reason = num(m.reasoning_tokens) || 0;
    var cr = num(m.cache_read_tokens) || 0;
    var cw = num(m.cache_write_tokens) || 0;
    return (inp * card.input + (out + reason) * card.output + cr * card.cached_read + cw * card.cache_write) / 1e6;
  }

  // OTel / BigQuery convention: input_tokens includes cached_tokens, output_tokens includes reasoning_tokens
  function priceOtelUsage(inpTotal, outTotal, cachedRead, card) {
    if (!card || inpTotal === null || outTotal === null) return null;
    var cr = Math.min(Math.max(0, cachedRead || 0), Math.max(0, inpTotal));
    var uncached = Math.max(0, inpTotal - cr);
    return (uncached * card.input + cr * card.cached_read + Math.max(0, outTotal) * card.output) / 1e6;
  }

  function vlModelRepricing(fleet) {
    var fl = fleet || {};
    var mu = fl.model_usage || {};
    var cards = resolveRateCards(mu.rate_cards);
    var hours = num(fl.window_hours) || num(mu.window_hours) || 24;
    var scale30d = hours > 0 ? 720 / hours : 30;
    var targets = REPRICE_TARGETS.filter(function(t) { return !!cards[t]; });

    var byModel = [];
    (mu.models || []).forEach(function(m) {
      var curMatch = matchRateCard(m.model, cards);
      if (!curMatch) return;
      var curUsd = num(m.est_cost_usd) !== null ? num(m.est_cost_usd) : priceCloudMonitoringRow(m, curMatch.card);
      if (curUsd === null || curUsd <= 0) return;
      var alts = [];
      targets.forEach(function(tModel) {
        if (tModel === curMatch.model) return;
        var altUsd = priceCloudMonitoringRow(m, cards[tModel]);
        if (altUsd === null || altUsd >= curUsd - 0.005) return;
        var saved = curUsd - altUsd;
        alts.push({
          targetModel: tModel,
          altCostUsd: altUsd,
          savingsWindowUsd: saved,
          savings30dUsd: saved * scale30d,
          savingsPct: 100 * saved / curUsd,
        });
      });
      if (alts.length) {
        byModel.push({
          model: m.model,
          invocations: num(m.invocations) || 0,
          currentCostUsd: curUsd,
          alternatives: alts,
        });
      }
    });

    var byAgent = [];
    (fl.agents || []).forEach(function(a) {
      var m = (a && a.metrics) || {};
      var b = (a && a.backend) || {};
      var inTok = num(m.input_tokens);
      var outTok = num(m.output_tokens);
      if (inTok === null || outTok === null || (inTok + outTok) <= 0) return;
      var cached = num(m.cached_tokens) || 0;
      var models = Array.isArray(b.models) && b.models.length ? b.models : [];
      var curMatch = models.length ? matchRateCard(models[0], cards) : null;
      if (!curMatch) return;
      var curUsd = priceOtelUsage(inTok, outTok, cached, curMatch.card);
      if (curUsd === null || curUsd <= 0) return;
      var alts = [];
      targets.forEach(function(tModel) {
        if (tModel === curMatch.model) return;
        var altUsd = priceOtelUsage(inTok, outTok, cached, cards[tModel]);
        if (altUsd === null || altUsd >= curUsd - 0.005) return;
        var saved = curUsd - altUsd;
        alts.push({
          targetModel: tModel,
          altCostUsd: altUsd,
          savingsWindowUsd: saved,
          savings30dUsd: saved * scale30d,
          savingsPct: 100 * saved / curUsd,
        });
      });
      if (alts.length) {
        byAgent.push({
          agentId: a.agent_id || a.resource_name || '',
          displayName: a.display_name || a.agent_id || 'Agent',
          engineName: a.engine_display_name || a.engine_id || '',
          currentModel: curMatch.model,
          requests: num(m.requests),
          llmCalls: num(m.llm_calls),
          inputTokens: inTok,
          outputTokens: outTok,
          cachedTokens: cached,
          currentCostUsd: curUsd,
          alternatives: alts,
        });
      }
    });
    byAgent.sort(function(a, b) { return b.currentCostUsd - a.currentCostUsd; });

    var flashTotalSaved = 0;
    var liteTotalSaved = 0;
    byModel.forEach(function(r) {
      r.alternatives.forEach(function(al) {
        if (al.targetModel === FLASH_REPRICE_TARGET) flashTotalSaved += al.savingsWindowUsd;
        if (al.targetModel === 'gemini-3.1-flash-lite') liteTotalSaved += al.savingsWindowUsd;
      });
    });

    return {
      windowHours: hours,
      rateCards: cards,
      byModel: byModel,
      byAgent: byAgent,
      flashSavingsWindowUsd: flashTotalSaved,
      flashSavings30dUsd: flashTotalSaved * scale30d,
      liteSavingsWindowUsd: liteTotalSaved,
      liteSavings30dUsd: liteTotalSaved * scale30d,
    };
  }

  // 5. Session receipt & turn-by-turn FinOps timeline (finops_mart.fct_sessions / fct_turns)
  // In fct_sessions and fct_turns: input_tokens includes cached_input_tokens; output_tokens includes reasoning_tokens.
  function vlSessionReceipt(session, turns, rawRateCards) {
    if (!session) return null;
    var cards = resolveRateCards(rawRateCards);
    var sessMatch = matchRateCard(session.model_name, cards) || {model: 'gemini-2.5-flash', card: cards['gemini-2.5-flash']};
    var turnList = Array.isArray(turns) ? turns : [];
    var turnItems = [];
    var sumUncached = 0;
    var sumCached = 0;
    var sumVisOut = 0;
    var sumReason = 0;
    var sumOut = 0;
    var sumUncachedUsd = 0;
    var sumCachedUsd = 0;
    var sumCacheSavedUsd = 0;
    var sumVisOutUsd = 0;
    var sumReasonUsd = 0;
    var sumCostUsd = 0;
    var hasTurnTokens = false;

    turnList.forEach(function(t, idx) {
      var tm = matchRateCard(t && t.model_name, cards) || sessMatch;
      var card = tm.card;
      var inp = num(t && t.input_tokens);
      var out = num(t && t.output_tokens);
      var crRaw = num(t && t.cached_input_tokens);
      var reasRaw = num(t && t.reasoning_tokens);
      if (inp !== null || out !== null) hasTurnTokens = true;
      var inpVal = inp || 0;
      var outVal = out || 0;
      var cr = Math.min(Math.max(0, crRaw || 0), inpVal);
      var uncached = Math.max(0, inpVal - cr);
      var reas = Math.min(Math.max(0, reasRaw || 0), outVal);
      var visOut = Math.max(0, outVal - reas);

      var uUsd = uncached * card.input / 1e6;
      var cUsd = cr * card.cached_read / 1e6;
      var savedUsd = cr * Math.max(0, card.input - card.cached_read) / 1e6;
      var voUsd = visOut * card.output / 1e6;
      var rUsd = reas * card.output / 1e6;
      var turnUsd = (inp !== null || out !== null) ? (uUsd + cUsd + voUsd + rUsd) : null;

      sumUncached += uncached;
      sumCached += cr;
      sumVisOut += visOut;
      sumReason += reas;
      sumOut += outVal;
      sumUncachedUsd += uUsd;
      sumCachedUsd += cUsd;
      sumCacheSavedUsd += savedUsd;
      sumVisOutUsd += voUsd;
      sumReasonUsd += rUsd;
      if (turnUsd !== null) sumCostUsd += turnUsd;

      turnItems.push({
        index: idx + 1,
        ts: (t && t.ts) || null,
        kind: (t && t.turn_kind) || 'TURN',
        status: (t && t.turn_status) || 'OK',
        model: (t && t.model_name) || tm.model,
        uncachedInputTokens: inp !== null ? uncached : null,
        cachedInputTokens: inp !== null ? cr : null,
        visibleOutputTokens: out !== null ? visOut : null,
        reasoningTokens: out !== null ? reas : null,
        totalTokens: num(t && t.total_tokens) !== null ? num(t.total_tokens) : ((inp !== null || out !== null) ? inpVal + outVal : null),
        llmCalls: num(t && t.llm_calls),
        toolCalls: num(t && t.tool_calls),
        toolNames: (t && t.tool_names) || null,
        costUsd: turnUsd,
        cacheSavedUsd: inp !== null ? savedUsd : null,
      });
    });

    // Fall back to session-level totals when turn-level tokens aren't populated
    if (!hasTurnTokens) {
      var sInp = num(session.input_tokens);
      var sOut = num(session.output_tokens);
      var sCrRaw = num(session.cached_input_tokens);
      var sReasRaw = num(session.reasoning_tokens);
      if (sInp !== null || sOut !== null) {
        var card = sessMatch.card;
        var inpV = sInp || 0;
        var outV = sOut || 0;
        sumCached = Math.min(Math.max(0, sCrRaw || 0), inpV);
        sumUncached = Math.max(0, inpV - sumCached);
        sumReason = Math.min(Math.max(0, sReasRaw || 0), outV);
        sumVisOut = Math.max(0, outV - sumReason);
        sumOut = outV;
        sumUncachedUsd = sumUncached * card.input / 1e6;
        sumCachedUsd = sumCached * card.cached_read / 1e6;
        sumCacheSavedUsd = sumCached * Math.max(0, card.input - card.cached_read) / 1e6;
        sumVisOutUsd = sumVisOut * card.output / 1e6;
        sumReasonUsd = sumReason * card.output / 1e6;
        sumCostUsd = sumUncachedUsd + sumCachedUsd + sumVisOutUsd + sumReasonUsd;
        hasTurnTokens = true;
      }
    }

    var totalInput = sumUncached + sumCached;
    var maxTurnTokens = Math.max.apply(null, [1].concat(turnItems.map(function(ti) { return ti.totalTokens || 0; })));
    return {
      sessionId: session.session_id || '',
      userLdap: vlUserLabel(session.user_email),
      agentName: session.agent_name || session.engine_key || DASH,
      model: session.model_name || sessMatch.model,
      rateCard: sessMatch.card,
      hasTokens: hasTurnTokens,
      uncachedInputTokens: sumUncached,
      cachedInputTokens: sumCached,
      totalInputTokens: totalInput,
      cacheHitPct: totalInput > 0 ? 100 * sumCached / totalInput : null,
      visibleOutputTokens: sumVisOut,
      reasoningTokens: sumReason,
      totalOutputTokens: sumOut,
      thinkingSharePct: sumOut > 0 ? 100 * sumReason / sumOut : null,
      uncachedInputUsd: sumUncachedUsd,
      cachedInputUsd: sumCachedUsd,
      cacheSavedUsd: sumCacheSavedUsd,
      visibleOutputUsd: sumVisOutUsd,
      reasoningUsd: sumReasonUsd,
      totalOutputUsd: sumVisOutUsd + sumReasonUsd,
      totalCostUsd: hasTurnTokens ? sumCostUsd : null,
      listPriceWithoutCacheUsd: hasTurnTokens ? (sumCostUsd + sumCacheSavedUsd) : null,
      maxTurnTokens: maxTurnTokens,
      turns: turnItems,
    };
  }

  function vlUserSummary(user, allSessions, turnsBySession, rawRateCards) {
    if (!user) return null;
    var ldap = user.user_ldap || vlUserLabel(user.user_email);
    var email = String(user.user_email || ldap || '').toLowerCase();
    var matched = (allSessions || []).filter(function(s) {
      var se = String((s && s.user_email) || '').toLowerCase();
      return se === email || vlUserLabel(se).toLowerCase() === String(ldap).toLowerCase();
    });
    var byAgent = {};
    var byModel = {};
    var receipts = [];
    var totalSaved = 0;
    var totalReasonUsd = 0;
    var totalEstUsd = 0;
    var sumCachedInput = 0;
    var sumTotalInput = 0;
    matched.forEach(function(s) {
      var key = (s.session_key || ((s.engine_key || '') + '|' + (s.session_id || '')));
      var tList = (turnsBySession && (turnsBySession[key] || turnsBySession[s.session_id])) || [];
      var r = vlSessionReceipt(s, tList, rawRateCards);
      if (r) {
        receipts.push({session: s, receipt: r});
        totalSaved += r.cacheSavedUsd || 0;
        totalReasonUsd += r.reasoningUsd || 0;
        totalEstUsd += r.totalCostUsd || 0;
        sumCachedInput += r.cachedInputTokens || 0;
        sumTotalInput += r.totalInputTokens || 0;
      }
      var ag = s.agent_name || s.engine_key || 'Unknown';
      byAgent[ag] = (byAgent[ag] || 0) + 1;
      var md = s.model_name || (r && r.model) || 'Unknown';
      byModel[md] = (byModel[md] || 0) + 1;
    });
    var derivedCacheHit = sumTotalInput > 0 ? Math.round((sumCachedInput / sumTotalInput) * 1000) / 10 : null;
    var tokExact = pick(num(user.tokens_30d), num(user.observed_tokens_exact));
    if (tokExact === null && num(user.total_tokens_m) !== null) tokExact = Math.round(num(user.total_tokens_m) * 1e6);
    var rawEst = pick(num(user.est_cost_30d), pick(num(user.est_cost_usd), pick(num(user.cost_30d_usd), num(user.monthly_cost_usd))));
    var estCost = rawEst !== null && totalEstUsd > rawEst ? Math.round(totalEstUsd * 10000) / 10000 : rawEst;
    return {
      ldap: ldap,
      department: user.department || DASH,
      primaryAgent: user.primary_agent || DASH,
      sessions30d: pick(num(user.sessions_30d), pick(num(user.sessions_7d), matched.length || null)),
      turns30d: pick(num(user.turns_30d), pick(num(user.interactions_7d), num(user.api_calls_observed))),
      tokens30d: tokExact,
      cacheHitPct: pick(num(user.cache_hit_pct), derivedCacheHit),
      avgCsat: pick(num(user.avg_csat), num(user.csat_rating)),
      estCost30d: estCost,
      recentSessionsCount: matched.length,
      recentEstCostUsd: totalEstUsd,
      recentCacheSavedUsd: totalSaved,
      recentReasoningUsd: totalReasonUsd,
      byAgent: byAgent,
      byModel: byModel,
      sessionReceipts: receipts,
    };
  }

  // ---- Adoption (finops_mart daily totals) --------------------------------------------------------
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
    parts.push('FinOps Dashboard rates it ' + health.label + ': ' + health.reasons.join(' '));
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
  // Builds alert policies for one agent from the same Cloud Monitoring metrics FinOps Dashboard reads, with the
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
      var p = {displayName: 'FinOps Dashboard: ' + name + ' \u00b7 ' + title, documentation: {content: doc, mimeType: 'text/markdown'},
               userLabels: {source: 'finops_dashboard', finops_agent: slug}, conditions: conditions, combiner: combiner, enabled: true,
               severity: severity};
      if (channel) p.notificationChannels = [channel];
      policies.push({key: key, file: 'finops-' + slug + '-' + key + '.json', policy: p});
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
         ' window with at least ' + R.minRequestsForRates + ' requests. That is the rule FinOps Dashboard uses to mark an agent Failing. ' +
         'Created from the FinOps Dashboard dashboard.');
    }
    if (include.latency) {
      add('p95', 'slow responses', 'WARNING', 'OR', [
        {displayName: 'p95 latency above ' + latencyS + ' s', conditionThreshold: {
          filter: 'metric.type="' + target.latencies + '" AND ' + base, aggregations: agg('REDUCE_PERCENTILE_95', windowS),
          comparison: 'COMPARISON_GT', thresholdValue: latencyS * 1000, duration: '0s', trigger: {count: 1},
          evaluationMissingData: 'EVALUATION_MISSING_DATA_INACTIVE'}},
      ], 'The 95th percentile latency of **' + name + '** (' + where + ') was above ' + latencyS + ' s in a ' + winText +
         ' window. FinOps Dashboard marks an agent Degraded at ' + (R.degradedP95Ms / 1000) + ' s. Created from the FinOps Dashboard dashboard.');
    }
    if (include.absence) {
      add('no-traffic', 'no traffic', 'WARNING', 'OR', [
        {displayName: 'No requests for ' + absenceMin + ' minutes', conditionAbsent: {
          filter: reqFilter, aggregations: agg('REDUCE_SUM', 300), duration: (absenceMin * 60) + 's', trigger: {count: 1}}},
      ], '**' + name + '** (' + where + ') received no requests for ' + absenceMin + ' minutes. Created from the FinOps Dashboard dashboard.');
    }
    var lines = ['# FinOps Dashboard alert policies for "' + name + '": ' + where + '.',
                 '# Review, then run with gcloud (needs roles/monitoring.alertPolicyEditor). Nothing is created until you run it.'];
    if (!channel) lines.push('# No notification channel: incidents only show in Cloud Monitoring until you add one.');
    if (target.kind === 'cloud_run') lines.push('# Cloud Run metrics are per service, so these alerts cover all traffic to ' + target.id + '.');
    policies.forEach(function(p) {
      lines.push('', 'cat > ' + p.file + " <<'FINOPS_POLICY'", JSON.stringify(p.policy, null, 2), 'FINOPS_POLICY',
                 "gcloud monitoring policies create --project='" + target.project + "' --policy-from-file=" + p.file);
    });
    return {ok: true, target: target, windowS: windowS, autoWindow: auto, policies: policies, script: lines.join('\n') + '\n'};
  }

  // ---- Pure PDF 1.4 Executive Report Builder (no DOM, 7-bit ASCII byte-exact) -----------------------
  function pdfCleanText(val) {
    if (val === null || val === undefined) return '--';
    return String(val)
      .replace(/[\u2014\u2013]/g, '--')
      .replace(/[\u00b7\u2022]/g, '|')
      .replace(/\u2192/g, '->')
      .replace(/\u2190/g, '<-')
      .replace(/\u2265/g, '>=')
      .replace(/\u2264/g, '<=')
      .replace(/\u00d7/g, 'x')
      .replace(/\u2026/g, '...')
      .replace(/\u2713/g, 'OK')
      .replace(/[\u26a0\u2605]/g, '*')
      .replace(/\s+/g, ' ')
      .replace(/[^\x20-\x7e]/g, '')
      .trim();
  }

  function pdfEsc(str) {
    return pdfCleanText(str).replace(/\\/g, '\\\\').replace(/\(/g, '\\(').replace(/\)/g, '\\)');
  }

  function pdfClip(str, maxChars) {
    var s = pdfCleanText(str);
    if (!maxChars || maxChars < 4 || s.length <= maxChars) return s;
    return s.slice(0, maxChars - 3) + '...';
  }

  function fmtCompactTok(v) {
    var n = num(v);
    if (n === null) return '--';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return Math.round(n).toLocaleString('en-US');
  }

  function vlGovernanceScore(input) {
    var inp = input && typeof input === 'object' ? input : {};
    var st = inp.state && typeof inp.state === 'object' ? inp.state : inp;
    var fl = inp.fleet && typeof inp.fleet === 'object' ? inp.fleet : (st.ge_fleet || inp);
    var agents = Array.isArray(inp.agents) ? inp.agents : (Array.isArray(fl.agents) ? fl.agents : []);
    var lf = (fl && fl.live_finops) || (st && st.live_finops) || {};
    var uc = (st && st.user_centric) || inp.user_centric || {};
    var clr = inp.costGovernance || (st && st.cost_governance) || inp.cost_governance || {};
    var agy = (st && st.antigravity_finops) || lf.antigravity_usage || inp.antigravity || {};
    var backendThr = (clr && clr.governance_score && clr.governance_score.accountability && clr.governance_score.accountability.thresholds) || inp.thresholds || {};
    var rawCacheTarget = num(pick(inp.cacheHitTargetPct, backendThr.cache_hit_target_pct));
    var cacheHitTargetPct = Math.max(1, rawCacheTarget !== null && rawCacheTarget > 0 ? rawCacheTarget : 50);
    var rawThinkLow = num(pick(inp.thinkingTaxLowPct, backendThr.thinking_tax_low_pct));
    var thinkingTaxLowPct = Math.max(0, Math.min(99, rawThinkLow !== null ? rawThinkLow : 25));
    var rawThinkHigh = num(pick(inp.thinkingTaxHighPct, backendThr.thinking_tax_high_pct));
    var thinkingTaxHighPct = Math.max(thinkingTaxLowPct + 1, Math.min(100, rawThinkHigh !== null ? rawThinkHigh : 65));
    var rawSeatTarget = num(pick(inp.seatUtilizationTargetPct, backendThr.seat_utilization_target_pct));
    var seatUtilTargetPct = Math.max(1, Math.min(100, rawSeatTarget !== null && rawSeatTarget > 0 ? rawSeatTarget : 85));
    var budget = num(pick(inp.budgetUsd, inp.budget, backendThr.monthly_budget_usd));

    // 1. Observability & Trace Readiness (0-20 pts)
    var totalAgents = agents.length;
    var traceEnabled = 0;
    var tokenAttributed = 0;
    agents.forEach(function(a) {
      if (!a || typeof a !== 'object') return;
      // The fleet sets observability_config.observability_enabled (the Trace badge's field);
      // trace_logging.enabled and a top-level observability_enabled are older payload shapes.
      var oc = a.observability_config || {};
      var tl = a.trace_logging || {};
      if (oc.observability_enabled === true || tl.enabled === true || a.observability_enabled === true) traceEnabled += 1;
      var m = a.metrics || {};
      if (num(m.input_tokens) !== null || m.token_source || (num(m.llm_calls) || 0) > 0) tokenAttributed += 1;
    });
    var tracePts, tokenPts, p1Grounding, p1Score, p1Summary, p1Rec;
    if (totalAgents > 0) {
      tracePts = Math.round(12 * traceEnabled / totalAgents);
      tokenPts = Math.round(8 * tokenAttributed / totalAgents);
      p1Grounding = 'MEASURED';
      p1Score = Math.max(0, Math.min(20, tracePts + tokenPts));
      p1Summary = traceEnabled + '/' + totalAgents + ' agents with trace logging; ' + tokenAttributed + '/' + totalAgents + ' with token telemetry.';
      p1Rec = p1Score >= 20
        ? 'All registered agents have trace logging & token attribution enabled.'
        : 'Enable Discovery Engine trace logging (observabilityConfig) on ' + (totalAgents - traceEnabled) + ' agent(s).';
    } else {
      tracePts = 6;
      tokenPts = 4;
      p1Grounding = 'UNCONFIGURED_NEUTRAL';
      p1Score = 10;
      p1Summary = 'No registered Gemini Enterprise agents in current scope.';
      p1Rec = 'Register agents in Gemini Enterprise and enable observabilityConfig trace logging.';
    }
    var p1Components = [
      {
        id: 'trace_logging',
        label: 'Discovery Engine Trace Logging',
        points: tracePts,
        max_points: 12,
        measured_value: totalAgents > 0 ? traceEnabled : null,
        target_value: totalAgents > 0 ? totalAgents : null,
        unit: 'agents',
        source: 'discoveryengine.googleapis.com/v1alpha (observabilityConfig)',
        grounding_status: p1Grounding,
      },
      {
        id: 'token_attribution',
        label: 'OTel / Cloud Logging Token Attribution',
        points: tokenPts,
        max_points: 8,
        measured_value: totalAgents > 0 ? tokenAttributed : null,
        target_value: totalAgents > 0 ? totalAgents : null,
        unit: 'agents',
        source: 'Cloud Monitoring v3 + Cloud Logging + Cloud Trace',
        grounding_status: p1Grounding,
      },
    ];

    // 2. Prompt Cache & Token Efficiency (0-20 pts)
    var mu = (fl && fl.model_usage) || {};
    var cache = vlCacheSavings(mu);
    var cacheSharePct = cache && cache.sharePct !== null ? cache.sharePct : null;
    if (cacheSharePct === null && agents.length) {
      var aIn = 0, aCr = 0;
      agents.forEach(function(a) {
        var m = (a && a.metrics) || {};
        var iTok = num(m.input_tokens) || 0;
        var cTok = Math.min(iTok, Math.max(0, num(m.cached_tokens) || 0));
        aIn += iTok;
        aCr += cTok;
      });
      if (aIn > 0) cacheSharePct = 100 * aCr / aIn;
    }
    var cachePts = cacheSharePct !== null ? Math.round(12 * Math.min(1, Math.max(0, cacheSharePct / cacheHitTargetPct))) : 6;
    var cacheGrounding = cacheSharePct !== null ? 'MEASURED' : 'UNCONFIGURED_NEUTRAL';
    var tax = vlThinkingTax(mu, fl && fl.window_hours);
    var thinkSharePct = tax ? tax.thinkingShareOfOutputPct : null;
    var thinkPts = 8;
    var thinkGrounding = thinkSharePct !== null ? 'MEASURED' : 'UNCONFIGURED_NEUTRAL';
    if (thinkSharePct !== null && (tax.reasoningTokens || 0) > 0) {
      if (thinkSharePct <= thinkingTaxLowPct) thinkPts = 8;
      else if (thinkSharePct >= thinkingTaxHighPct) thinkPts = 0;
      else thinkPts = Math.round(8 * (thinkingTaxHighPct - thinkSharePct) / (thinkingTaxHighPct - thinkingTaxLowPct));
    }
    var p2Score = Math.max(0, Math.min(20, cachePts + thinkPts));
    var p2Summary = (cacheSharePct !== null ? cacheSharePct.toFixed(1) + '% cache hit (target >=' + Math.round(cacheHitTargetPct) + '%)' : 'No prompt cache volume yet') +
      '; ' + (thinkSharePct !== null ? thinkSharePct.toFixed(1) + '% thinking share.' : '0% thinking overhead.');
    var p2Rec = p2Score >= 18
      ? 'Prompt cache reuse and reasoning token budgets meet efficiency targets.'
      : 'Inspect Prompt Cache X-Ray and cap ThinkingConfig budget on high-reasoning models.';
    var p2Components = [
      {
        id: 'prompt_cache_hit',
        label: 'Prompt Cache Hit Share',
        points: cachePts,
        max_points: 12,
        measured_value: cacheSharePct !== null ? Math.round(cacheSharePct * 10) / 10 : null,
        target_value: cacheHitTargetPct,
        unit: 'pct',
        source: 'ge_fleet.model_usage (cache_read_tokens / prompt_tokens)',
        grounding_status: cacheGrounding,
      },
      {
        id: 'thinking_tax_control',
        label: 'Reasoning Token Share Control',
        points: thinkPts,
        max_points: 8,
        measured_value: thinkSharePct !== null ? Math.round(thinkSharePct * 10) / 10 : null,
        target_value: thinkingTaxLowPct,
        unit: 'pct',
        source: 'ge_fleet.model_usage (reasoning_tokens / combined_output_tokens)',
        grounding_status: thinkGrounding,
      },
    ];

    // 3. Runtime Hygiene & Zombie Control (0-20 pts)
    var qItems = Array.isArray(clr.queue) ? clr.queue : (Array.isArray(clr.items) ? clr.items : []);
    var qById = {};
    qItems.forEach(function(q) {
      if (q && q.runtime_id) qById[String(q.runtime_id)] = String(q.status || '');
    });
    var unreg = Array.isArray(fl && fl.unregistered_runtimes) ? fl.unregistered_runtimes : [];
    var unaddressedZombies = 0;
    var warnedZombies = 0;
    var seenRids = {};
    unreg.forEach(function(u) {
      if (!u || typeof u !== 'object') return;
      var rid = String(u.agent_id || u.runtime_id || '').trim();
      var fstat = String(u.finops_status || '').toUpperCase();
      if (fstat === 'ZOMBIE_CANDIDATE' || fstat === 'ZOMBIE_IDLE_ENGINE' || fstat === 'ZOMBIE_IDLE_SERVICE' || fstat === 'ALWAYS_ON_UNREGISTERED') {
        if (rid) seenRids[rid] = true;
        var qs = qById[rid] || '';
        if (qs === 'SNOOZED' || qs === 'RECOVERED_ACTIVE' || qs.indexOf('CLEARED_') === 0) return;
        if (qs === 'WARNED_GRACE_PERIOD') warnedZombies += 1;
        else unaddressedZombies += 1;
      }
    });
    qItems.forEach(function(q) {
      if (!q || typeof q !== 'object') return;
      var rid = String(q.runtime_id || '').trim();
      if (seenRids[rid]) return;
      var qs = String(q.status || '');
      if (qs === 'PENDING_WARNING') unaddressedZombies += 1;
      else if (qs === 'WARNED_GRACE_PERIOD') warnedZombies += 1;
    });
    var brokenRegs = agents.filter(function(a) {
      var stReg = (a && a.registration && a.registration.status) || (a && a.backend && a.backend.status);
      return stReg === 'BACKEND_NOT_FOUND' || stReg === 'NO_BACKEND';
    }).length;
    var unwarnedDed = Math.min(20, unaddressedZombies * 5);
    var remAfterUnwarned = Math.max(0, 20 - unwarnedDed);
    var warnedDed = Math.min(remAfterUnwarned, warnedZombies * 2);
    var remAfterWarned = Math.max(0, remAfterUnwarned - warnedDed);
    var brokenDed = Math.min(remAfterWarned, brokenRegs * 3);
    var p3Score = Math.max(0, Math.min(20, 20 - unwarnedDed - warnedDed - brokenDed));
    var p3Summary = unaddressedZombies + ' unwarned zombie runtime(s), ' + warnedZombies + ' in grace window, ' + brokenRegs + ' broken registration(s).';
    var p3Rec = p3Score >= 20
      ? 'Zero unaddressed zombie runtimes or broken registrations.'
      : 'Run Stage 1/2 Scheduled Zombie Clearance or scale idle Cloud Run services to min-instances=0.';
    var p3Components = [
      {
        id: 'base_hygiene_allowance',
        label: 'Runtime Hygiene Base Allowance',
        points: 20,
        max_points: 20,
        measured_value: agents.length + unreg.length,
        target_value: 0,
        unit: 'runtimes_audited',
        source: 'Cloud Run v2 + Vertex AI ReasoningEngine Inventory',
        grounding_status: 'MEASURED',
      },
      {
        id: 'unwarned_zombie_penalty',
        label: 'Unwarned Zombie Runtime Penalty (-5 pts/ea)',
        points: -unwarnedDed,
        max_points: 0,
        measured_value: unaddressedZombies,
        target_value: 0,
        unit: 'zombies',
        source: 'ge_fleet.unregistered_runtimes + zombie_clearance_queue',
        grounding_status: 'MEASURED',
      },
      {
        id: 'warned_grace_penalty',
        label: 'Grace-Period Runtime Penalty (-2 pts/ea)',
        points: -warnedDed,
        max_points: 0,
        measured_value: warnedZombies,
        target_value: 0,
        unit: 'runtimes',
        source: 'finops_analytics.zombie_clearance_queue',
        grounding_status: 'MEASURED',
      },
      {
        id: 'broken_registration_penalty',
        label: 'Broken Registration Penalty (-3 pts/ea)',
        points: -brokenDed,
        max_points: 0,
        measured_value: brokenRegs,
        target_value: 0,
        unit: 'registrations',
        source: 'ge_fleet.agents[].registration.status',
        grounding_status: 'MEASURED',
      },
    ];

    // 4. Budget Runway & Drift Stability (0-20 pts)
    var gdu = (st && st.ge_daily_usage) || inp.ge_daily_usage || {};
    var days = Array.isArray(gdu.days) ? gdu.days : [];
    var cs = vlCostSummary(days, budget);
    var rw = vlBudgetRunway(cs);
    var runwayPts = 8;
    var rwLabel = 'Monthly budget cap not set (8/12 baseline)';
    var rwGrounding = 'UNCONFIGURED_NEUTRAL';
    var rwMeasuredVal = null;
    if (inp.budgetStatus) {
      var bs = String(inp.budgetStatus).toLowerCase();
      if (bs === 'on_track' || bs === 'runway_clear') { runwayPts = 12; rwLabel = 'Budget on track'; rwGrounding = 'MEASURED'; rwMeasuredVal = 'on_track'; }
      else if (bs === 'at_risk') { runwayPts = 6; rwLabel = 'Budget runway at risk'; rwGrounding = 'MEASURED'; rwMeasuredVal = 'at_risk'; }
      else if (bs === 'over' || bs === 'exhausted') { runwayPts = 0; rwLabel = 'Projected over budget'; rwGrounding = 'MEASURED'; rwMeasuredVal = 'over'; }
    } else if (rw && rw.status === 'RUNWAY_CLEAR') {
      runwayPts = 12; rwLabel = 'Budget on track'; rwGrounding = 'MEASURED'; rwMeasuredVal = 'on_track';
    } else if (rw && rw.status === 'AT_RISK') {
      runwayPts = 6; rwLabel = 'Budget runway at risk'; rwGrounding = 'MEASURED'; rwMeasuredVal = 'at_risk';
    } else if (rw && rw.status === 'EXHAUSTED') {
      runwayPts = 0; rwLabel = 'Budget exhausted'; rwGrounding = 'MEASURED'; rwMeasuredVal = 'over';
    }
    var delta = vlSpendDelta(lf.spend_drift);
    var changePct = delta && delta.pct !== null ? delta.pct : null;
    if (changePct === null && fl && fl.model_usage_previous && fl.model_usage_previous.totals && mu && mu.totals) {
      var prevUsd = num(fl.model_usage_previous.totals.estimated_cost_usd || fl.model_usage_previous.totals.est_cost_usd);
      var currUsd = num(mu.totals.estimated_cost_usd || mu.totals.est_cost_usd);
      if (prevUsd !== null && prevUsd > 0 && currUsd !== null) {
        changePct = Math.round(((currUsd - prevUsd) / prevUsd) * 1000) / 10;
      }
    }
    var driftPts = 6;
    var driftLabel = 'no prior-window drift baseline';
    var driftGrounding = 'UNCONFIGURED_NEUTRAL';
    if (changePct !== null) {
      driftGrounding = 'MEASURED';
      var sign = changePct >= 0 ? '+' : '';
      if (changePct <= 10) { driftPts = 8; driftLabel = sign + changePct.toFixed(1) + '% spend drift (stable)'; }
      else if (changePct <= 25) { driftPts = 5; driftLabel = sign + changePct.toFixed(1) + '% spend drift (moderate)'; }
      else if (changePct <= 50) { driftPts = 2; driftLabel = sign + changePct.toFixed(1) + '% spend drift (elevated)'; }
      else { driftPts = 0; driftLabel = sign + changePct.toFixed(1) + '% spend spike'; }
    }
    var p4Score = Math.max(0, Math.min(20, runwayPts + driftPts));
    var p4Summary = rwLabel + '; ' + driftLabel + '.';
    var p4Rec = p4Score >= 18
      ? 'Spend pace and period-over-period drift are within target guardrails.'
      : 'Set a monthly AI budget cap on the Cost tab and inspect the Spend Drift Waterfall.';
    var p4Components = [
      {
        id: 'budget_runway',
        label: 'Monthly Budget Runway Pace',
        points: runwayPts,
        max_points: 12,
        measured_value: rwMeasuredVal,
        target_value: 'on_track',
        unit: 'status',
        source: 'ge_daily_usage + FINOPS_MONTHLY_BUDGET_USD',
        grounding_status: rwGrounding,
      },
      {
        id: 'spend_drift',
        label: 'Period-over-Period Spend Drift',
        points: driftPts,
        max_points: 8,
        measured_value: changePct,
        target_value: 10,
        unit: 'pct',
        source: 'live_finops.spend_drift / model_usage_previous',
        grounding_status: driftGrounding,
      },
    ];

    // 5. Seat & Identity Attribution (0-20 pts)
    var agyKpis = (agy && (agy.kpis || agy.seats)) || {};
    var purchasedSeats = num(agyKpis.purchased_seats) || 0;
    var assignedSeats = num(agyKpis.assigned_seats) || 0;
    var idleSeats = num(agyKpis.idle_seats) || 0;
    var activeSeatsExplicit = num(pick(agyKpis.active_seats_7d, agyKpis.active_seats));
    var seatDenom = purchasedSeats || assignedSeats;
    var seatPts = 7;
    var seatTxt = 'Seat inventory not connected (7/10 baseline)';
    var seatGrounding = 'UNCONFIGURED_NEUTRAL';
    var seatRatioPct = null;
    if (seatDenom > 0) {
      var activeSeats = activeSeatsExplicit !== null ? Math.max(0, activeSeatsExplicit) : Math.max(0, (assignedSeats || seatDenom) - idleSeats);
      var seatRatio = activeSeats / seatDenom;
      seatRatioPct = Math.round(seatRatio * 1000) / 10;
      seatPts = Math.round(10 * Math.min(1, Math.max(0, (seatRatio * 100) / seatUtilTargetPct)));
      seatTxt = activeSeats + '/' + seatDenom + ' active seats (' + Math.round(seatRatio * 100) + '%)';
      seatGrounding = 'MEASURED';
    }
    var sessions = Array.isArray(uc.ge_sessions) ? uc.ge_sessions : [];
    var pUsers = Array.isArray(uc.power_users_ldap) ? uc.power_users_ldap : [];
    var attrPts = 7;
    var attrTxt = 'No session identity sample in current window';
    var attrGrounding = 'UNCONFIGURED_NEUTRAL';
    var attrRatioPct = null;
    var isVerifiedUser = function(raw) {
      var v = String(raw || '').trim().toLowerCase();
      return v !== '' && v !== 'unattributed' && v !== 'anonymous' && v !== 'unknown' && v !== DASH.toLowerCase();
    };
    if (sessions.length) {
      var verSess = sessions.filter(function(s) { return s && isVerifiedUser(s.user_email); }).length;
      attrRatioPct = Math.round((verSess / sessions.length) * 1000) / 10;
      attrPts = Math.round(10 * verSess / sessions.length);
      attrTxt = verSess + '/' + sessions.length + ' sessions LDAP-attributed';
      attrGrounding = 'MEASURED';
    } else if (pUsers.length) {
      var verUsers = pUsers.filter(function(u) { return u && isVerifiedUser(u.user_ldap || u.ldap || u.user_email); }).length;
      attrRatioPct = Math.round((verUsers / pUsers.length) * 1000) / 10;
      attrPts = Math.round(10 * verUsers / pUsers.length);
      attrTxt = verUsers + '/' + pUsers.length + ' top users LDAP-attributed';
      attrGrounding = 'MEASURED';
    }
    var p5Score = Math.max(0, Math.min(20, seatPts + attrPts));
    var p5Summary = seatTxt + '; ' + attrTxt + '.';
    var p5Rec = p5Score >= 18
      ? 'High seat utilization and verified LDAP session attribution.'
      : 'Reclaim idle Antigravity/GE seats and enforce authenticated user identity on agent sessions.';
    var p5Components = [
      {
        id: 'seat_utilization',
        label: 'Assigned Seat Utilization (7d)',
        points: seatPts,
        max_points: 10,
        measured_value: seatRatioPct,
        target_value: seatUtilTargetPct,
        unit: 'pct',
        source: 'antigravity_finops.kpis (active_seats_7d / purchased_seats)',
        grounding_status: seatGrounding,
      },
      {
        id: 'identity_attribution',
        label: 'Verified LDAP Session Attribution',
        points: attrPts,
        max_points: 10,
        measured_value: attrRatioPct,
        target_value: 100,
        unit: 'pct',
        source: 'user_centric.ge_sessions / power_users_ldap',
        grounding_status: attrGrounding,
      },
    ];

    var mkPillar = function(id, label, shortLabel, sc, summary, rec, actionLabel, tabIdx, targetId, comps) {
      var stLbl = sc >= 16 ? 'healthy' : (sc >= 10 ? 'warning' : 'critical');
      var stCode = sc >= 16 ? 'OPTIMAL' : (sc >= 10 ? 'MODERATE' : 'ACTION_NEEDED');
      return {
        id: id,
        label: label,
        shortLabel: shortLabel,
        score: sc,
        max: 20,
        maxScore: 20,
        max_score: 20,
        ptsLost: Math.max(0, 20 - sc),
        pct: Math.round(sc / 20 * 100),
        status: stLbl,
        statusCode: stCode,
        summary: summary,
        recommendation: rec,
        actionLabel: actionLabel,
        actionTab: tabIdx,
        actionTarget: targetId,
        tabIndex: tabIdx,
        targetId: targetId,
        components: comps || [],
      };
    };

    var pillars = [
      mkPillar('observability', 'Observability & Trace Readiness', 'Observability', p1Score, p1Summary, p1Rec, 'Manage Trace Logging', 0, 'enableAllObsBtn', p1Components),
      mkPillar('token_efficiency', 'Prompt Cache & Token Efficiency', 'Token Efficiency', p2Score, p2Summary, p2Rec, 'Inspect Tokenomics', 3, 'vlTokenomicsPanel', p2Components),
      mkPillar('runtime_hygiene', 'Runtime Hygiene & Zombie Control', 'Runtime Hygiene', p3Score, p3Summary, p3Rec, 'Review Clearance Queue', 3, 'scheduledClearancePanel', p3Components),
      mkPillar('budget_stability', 'Budget Runway & Drift Stability', 'Budget & Drift', p4Score, p4Summary, p4Rec, 'Configure Budget Cap', 3, 'vlSpendPanel', p4Components),
      mkPillar('seat_attribution', 'Seat & Identity Attribution', 'Seats & Identity', p5Score, p5Summary, p5Rec, 'Audit Seats & Users', 7, 'antigravityFinopsPanel', p5Components),
    ];

    var totalScore = pillars.reduce(function(acc, p) { return acc + p.score; }, 0);
    var measuredEarned = 0, measuredPossible = 0, unconfEarned = 0, unconfPossible = 0;
    var unconfIds = [];
    var sumCheckOk = (totalScore === pillars.reduce(function(acc, p) { return acc + p.score; }, 0)) &&
      (pillars.reduce(function(acc, p) { return acc + p.max; }, 0) === 100);
    pillars.forEach(function(p) {
      var cPts = 0, cMax = 0;
      (p.components || []).forEach(function(c) {
        cPts += c.points;
        cMax += c.max_points;
        if (c.grounding_status === 'MEASURED') {
          measuredEarned += c.points;
          measuredPossible += c.max_points;
        } else {
          unconfEarned += c.points;
          unconfPossible += c.max_points;
          unconfIds.push(c.id);
        }
      });
      if (cPts !== p.score || cMax !== p.max) sumCheckOk = false;
    });

    var grade = totalScore >= 85 ? 'A' : (totalScore >= 70 ? 'B' : (totalScore >= 55 ? 'C' : (totalScore >= 40 ? 'D' : 'F')));
    var status = totalScore >= 85 ? 'OPTIMAL' : (totalScore >= 70 ? 'GOVERNED' : (totalScore >= 50 ? 'NEEDS_TUNING' : 'CRITICAL'));
    var statusLabel = totalScore >= 85 ? 'Exemplary Governance' : (totalScore >= 70 ? 'Governed' : (totalScore >= 50 ? 'Needs Tuning' : 'Action Required'));
    var tier = totalScore >= 85 ? 'EXEMPLARY' : (totalScore >= 70 ? 'GOVERNED' : (totalScore >= 50 ? 'NEEDS_TUNING' : 'AT_RISK'));
    var badgeClass = totalScore >= 85 ? 'badge-green' : (totalScore >= 70 ? 'badge-blue' : (totalScore >= 50 ? 'badge-yellow' : 'badge-red'));
    var recs = pillars.filter(function(p) { return p.score < 20; }).slice().sort(function(a, b) { return a.score - b.score; });
    var topAction = recs.length ? recs[0] : null;
    var headline = topAction
      ? ('Top governance opportunity (+' + topAction.ptsLost + ' pts): ' + topAction.recommendation)
      : 'All 5 Enterprise AI Governance & Tokenomics pillars are operating at full score (100/100).';

    return {
      score: totalScore,
      maxScore: 100,
      max_score: 100,
      grade: grade,
      status: status,
      statusLabel: statusLabel,
      tier: tier,
      badgeClass: badgeClass,
      headline: headline,
      pillars: pillars,
      accountability: {
        total_points_earned: totalScore,
        total_points_possible: 100,
        measured_points_earned: measuredEarned,
        measured_points_possible: measuredPossible,
        unconfigured_neutral_points: unconfEarned,
        unconfigured_points_possible: unconfPossible,
        unconfigured_components: unconfIds,
        sum_check_ok: sumCheckOk,
        thresholds: {
          cache_hit_target_pct: cacheHitTargetPct,
          thinking_tax_low_pct: thinkingTaxLowPct,
          thinking_tax_high_pct: thinkingTaxHighPct,
          seat_utilization_target_pct: seatUtilTargetPct,
          monthly_budget_usd: budget,
        },
      },
      topAction: topAction,
      recommendations: recs,
    };
  }

  function vlBuildPdfReport(report) {
    var r = report || {};
    var fl = r.fleet || (r.state && r.state.ge_fleet) || {};
    var st = r.state || {};
    var agents = Array.isArray(r.agents) ? r.agents : (Array.isArray(fl.agents) ? fl.agents : []);
    var title = pdfCleanText(r.title || 'FinOps Dashboard | FinOps & Agent Fleet Executive Report');
    var activeTab = pdfCleanText(r.activeTab || 'Overview');
    var scope = pdfCleanText(r.scope || 'All apps | all regions');
    var winLbl = pdfCleanText(r.windowLabel || 'Last 24 hours');
    var proj = pdfCleanText(r.project || fl.project_id || st.gcp_project || 'Current');
    var exportedAt = pdfCleanText(r.exportedAt || new Date().toISOString().replace('T', ' ').slice(0, 19) + ' UTC');
    var dateSlug = exportedAt.slice(0, 10).replace(/[^0-9-]/g, '') || 'report';
    var filename = 'finops-finops-report-' + dateSlug + '.pdf';

    var hc = vlHealthCounts(agents);
    var totals = fl.totals || {};
    var mu = fl.model_usage || {};
    var muTotals = mu.totals || {};
    var uc = st.user_centric || {};
    var pUsers = Array.isArray(uc.power_users_ldap) ? uc.power_users_ldap : [];
    var gov = vlGovernanceScore({fleet: fl, state: st, agents: agents, budgetUsd: pick(r.budgetUsd, r.budget)});

    var kpis = Array.isArray(r.kpis) && r.kpis.length ? r.kpis.slice(0, 7) : [];
    if (!kpis.length) {
      var attnCount = (hc.failing || 0) + (hc.broken || 0) + (hc.degraded || 0);
      kpis = [
        {label: 'AI Governance Score', value: gov.score + ' / 100 (' + gov.grade + ')',
         sub: gov.tier + ' | 5-pillar posture'},
        {label: 'Agents Enabled', value: String(pick(totals.enabled, agents.length)) + ' / ' + String(pick(totals.agents, agents.length)),
         sub: (hc.healthy || 0) + ' healthy | ' + attnCount + ' need attention'},
        {label: 'Est. Model Spend', value: num(muTotals.est_cost_usd) !== null ? usdText(num(muTotals.est_cost_usd), 2) : '--',
         sub: (num(muTotals.invocations) !== null ? Math.round(num(muTotals.invocations)).toLocaleString('en-US') + ' calls' : 'List price estimate')},
        {label: 'Agent Tokens (In / Out)', value: fmtCompactTok(totals.input_tokens) + ' / ' + fmtCompactTok(totals.output_tokens),
         sub: (num(totals.llm_calls) !== null ? Math.round(num(totals.llm_calls)).toLocaleString('en-US') + ' LLM calls' : 'OTel gen_ai telemetry')},
      ];
    } else if (!kpis.some(function(k) { return k && /governance score/i.test(String(k.label || '')); })) {
      kpis.unshift({
        label: 'AI Governance Score',
        value: gov.score + ' / 100 (' + gov.grade + ')',
        sub: gov.tier + ' | 5-pillar posture',
      });
    }

    var sections = [];
    // Governance & Tokenomics 0-100 Scorecard Section
    sections.push({
      title: 'Enterprise AI Governance & Tokenomics Scorecard (' + gov.score + '/100 -- Grade ' + gov.grade + ' ' + gov.tier + ')',
      subtitle: gov.headline,
      headers: ['Governance Pillar', 'Score', 'Status', 'Measured Telemetry Evidence', 'Recommended Action'],
      widths: [148, 56, 74, 212, 230],
      rows: gov.pillars.map(function(p) {
        return [
          p.label,
          p.score + ' / ' + p.maxScore,
          p.status,
          p.summary,
          p.recommendation,
        ];
      }),
    });

    // Section 1: Fleet Health & Attention Summary + Agent Inventory
    if (agents.length) {
      var agentRows = agents.slice(0, 40).map(function(a) {
        var m = a.metrics || {};
        var hl = vlHealth(a);
        return [
          a.display_name || a.agent_id || '--',
          a.engine_display_name || a.engine_id || '--',
          a.type || '--',
          hl.label,
          num(m.requests) !== null ? Math.round(num(m.requests)).toLocaleString('en-US') : '--',
          num(m.requests) !== null ? ((num(m.errors_4xx) || 0) + ' / ' + (num(m.errors_5xx) || 0)) : '--',
          msText(num(m.latency_p95_ms)),
          fmtCompactTok(m.input_tokens) + ' / ' + fmtCompactTok(m.output_tokens),
        ];
      });
      sections.push({
        title: '1. Agent Fleet Inventory & Runtime Health (' + agents.length + ' agents)',
        subtitle: 'Health summary: ' + (hc.failing || 0) + ' Failing, ' + (hc.broken || 0) + ' Broken, ' +
          (hc.degraded || 0) + ' Degraded, ' + (hc.healthy || 0) + ' Healthy, ' + (hc.idle || 0) + ' Idle, ' +
          (hc.unknown || 0) + ' No telemetry.',
        headers: ['Agent', 'App / Engine', 'Type', 'Health', 'Requests', '4xx / 5xx', 'p95 Latency', 'Tokens In/Out'],
        widths: [150, 120, 62, 68, 66, 66, 74, 114],
        rows: agentRows,
      });
    }

    // Section 2: Model Usage & Tokenomics
    var models = Array.isArray(mu.models) ? mu.models : [];
    if (models.length) {
      sections.push({
        title: '2. Vertex AI Model Usage & Token Economics',
        subtitle: 'Project-wide model invocations, token volume, prompt cache reuse, and list-price spend (' + winLbl + ').',
        headers: ['Model', 'Invocations', 'Input Tok', 'Output Tok', 'Cache Read', 'Cache Share', 'Est. Spend'],
        widths: [180, 86, 90, 90, 96, 84, 94],
        rows: models.slice(0, 25).map(function(m) {
          return [
            m.model || '--',
            num(m.invocations) !== null ? Math.round(num(m.invocations)).toLocaleString('en-US') : '--',
            fmtCompactTok(m.input_tokens),
            fmtCompactTok(m.output_tokens),
            fmtCompactTok(m.cache_read_tokens),
            num(m.cache_read_share_pct) !== null ? num(m.cache_read_share_pct) + '%' : '--',
            num(m.est_cost_usd) !== null ? usdText(num(m.est_cost_usd), 2) : 'no rate card',
          ];
        }),
      });
    }

    // Section 3: Top Users
    if (pUsers.length) {
      sections.push({
        title: '3. Top Users & Session Activity',
        subtitle: 'Observed user sessions, turns, token consumption, cache hit rate, and list-price cost estimate.',
        headers: ['User (LDAP)', 'Department', 'Primary Agent', 'Sessions', 'Total Tokens', 'Cache Hit', 'Est. Cost'],
        widths: [120, 105, 145, 85, 95, 80, 90],
        rows: pUsers.slice(0, 25).map(function(u) {
          var costVal = num(u.est_cost_usd) !== null ? usdText(num(u.est_cost_usd), 2)
            : (num(u.monthly_cost_usd) !== null ? usdText(num(u.monthly_cost_usd), 2) : '--');
          return [
            vlUserLabel(u.user_ldap || u.user_email),
            u.department || '--',
            u.primary_agent || '--',
            num(u.sessions_7d) !== null ? Math.round(num(u.sessions_7d)).toLocaleString('en-US') : '--',
            u.total_tokens_m !== null && u.total_tokens_m !== undefined ? u.total_tokens_m + 'M' : '--',
            num(u.cache_hit_pct) !== null ? num(u.cache_hit_pct) + '%' : '--',
            costVal,
          ];
        }),
      });
    }

    // Additional custom sections from active DOM tables
    if (Array.isArray(r.sections)) {
      r.sections.forEach(function(sec) {
        if (sec && Array.isArray(sec.headers) && sec.headers.length && Array.isArray(sec.rows) && sec.rows.length) {
          sections.push(sec);
        }
      });
    }

    // Telemetry Grounding & Data Sources section
    var tv = st.telemetry_validation || {};
    var checks = Array.isArray(tv.checks) ? tv.checks : [];
    if (checks.length) {
      sections.push({
        title: (sections.length + 1) + '. Telemetry Grounding & Data Source Verification',
        subtitle: 'Automated grounding checks across BigQuery, Cloud Monitoring v3, and Discovery Engine APIs.',
        headers: ['Check ID', 'Tab', 'Metric', 'Expected', 'Actual', 'Source', 'Status'],
        widths: [74, 80, 146, 105, 105, 140, 70],
        rows: checks.slice(0, 20).map(function(c) {
          return [c.check_id || '--', c.tab || '--', c.metric || '--', c.expected || '--', c.actual || '--', c.source || '--', c.status || '--'];
        }),
      });
    }

    // Layout geometry: Landscape Letter (792 x 612 pt), margin 36 pt
    var W = 792, H = 612, M = 36, CW = W - 2 * M, Y_MIN = 48;
    var pages = [];
    var cur = null;
    var y = 0;

    function newPage(isFirst) {
      cur = [];
      pages.push(cur);
      if (isFirst) {
        cur.push('0.06 0.09 0.16 rg', M + ' ' + (H - M - 52) + ' ' + CW + ' 52 re f');
        cur.push('0.15 0.39 0.92 rg', M + ' ' + (H - M - 52) + ' 6 52 re f');
        cur.push('1 1 1 rg', 'BT /F2 14 Tf ' + (M + 16) + ' ' + (H - M - 22) + ' Td (' + pdfEsc(title) + ') Tj ET');
        var metaLine = 'View: ' + activeTab + '  |  Scope: ' + scope + '  |  Window: ' + winLbl + '  |  Project: ' + proj + '  |  Exported: ' + exportedAt;
        cur.push('0.82 0.87 0.94 rg', 'BT /F1 8.5 Tf ' + (M + 16) + ' ' + (H - M - 39) + ' Td (' + pdfEsc(pdfClip(metaLine, 128)) + ') Tj ET');
        y = H - M - 66;
      } else {
        cur.push('0.06 0.09 0.16 rg', M + ' ' + (H - M - 24) + ' ' + CW + ' 24 re f');
        cur.push('1 1 1 rg', 'BT /F2 9.5 Tf ' + (M + 10) + ' ' + (H - M - 16) + ' Td (' + pdfEsc(title + ' -- ' + activeTab + ' (continued)') + ') Tj ET');
        y = H - M - 38;
      }
    }

    newPage(true);

    // Draw KPI Grid on Page 1
    if (kpis.length) {
      var cols = Math.min(4, kpis.length);
      var gap = 8;
      var cardW = Math.floor((CW - gap * (cols - 1)) / cols);
      var cardH = 44;
      for (var i = 0; i < kpis.length; i++) {
        var col = i % cols;
        if (col === 0 && i > 0) y -= (cardH + gap);
        var cx = M + col * (cardW + gap);
        var cy = y - cardH;
        var k = kpis[i];
        cur.push('0.97 0.98 0.99 rg 0.80 0.84 0.88 RG 0.7 w', cx + ' ' + cy + ' ' + cardW + ' ' + cardH + ' re B');
        cur.push('0.28 0.33 0.41 rg', 'BT /F1 7.5 Tf ' + (cx + 8) + ' ' + (cy + 32) + ' Td (' + pdfEsc(pdfClip(k.label, 32)) + ') Tj ET');
        cur.push('0.06 0.09 0.16 rg', 'BT /F2 11.5 Tf ' + (cx + 8) + ' ' + (cy + 17) + ' Td (' + pdfEsc(pdfClip(k.value, 24)) + ') Tj ET');
        if (k.sub) {
          cur.push('0.39 0.45 0.55 rg', 'BT /F1 7 Tf ' + (cx + 8) + ' ' + (cy + 6) + ' Td (' + pdfEsc(pdfClip(k.sub, 36)) + ') Tj ET');
        }
      }
      y -= (cardH + 16);
    }

    function drawTableHeader(headers, widths) {
      var rh = 17;
      var ry = y - rh;
      cur.push('0.91 0.94 0.97 rg 0.78 0.82 0.88 RG 0.6 w', M + ' ' + ry + ' ' + CW + ' ' + rh + ' re B');
      var tx = M;
      for (var c = 0; c < headers.length; c++) {
        var cw = widths[c];
        var maxCh = Math.max(4, Math.floor((cw - 8) / 4.3));
        cur.push('0.06 0.09 0.16 rg', 'BT /F2 7.5 Tf ' + (tx + 4) + ' ' + (ry + 5) + ' Td (' + pdfEsc(pdfClip(headers[c], maxCh)) + ') Tj ET');
        tx += cw;
      }
      y = ry;
    }

    sections.forEach(function(sec) {
      var headers = sec.headers || [];
      var rows = sec.rows || [];
      if (!headers.length) return;
      var widths = Array.isArray(sec.widths) && sec.widths.length === headers.length
        ? sec.widths.slice()
        : headers.map(function() { return Math.floor(CW / headers.length); });
      var wSum = widths.reduce(function(a, b) { return a + b; }, 0) || CW;
      if (wSum !== CW) {
        widths = widths.map(function(w) { return Math.floor(w * CW / wSum); });
        widths[widths.length - 1] += CW - widths.reduce(function(a, b) { return a + b; }, 0);
      }

      var headerNeed = (sec.subtitle ? 30 : 18) + 17 + 16;
      if (y - headerNeed < Y_MIN) newPage(false);

      cur.push('0.06 0.09 0.16 rg', 'BT /F2 10.5 Tf ' + M + ' ' + (y - 12) + ' Td (' + pdfEsc(pdfClip(sec.title, 105)) + ') Tj ET');
      y -= 15;
      if (sec.subtitle) {
        cur.push('0.28 0.33 0.41 rg', 'BT /F1 8 Tf ' + M + ' ' + (y - 10) + ' Td (' + pdfEsc(pdfClip(sec.subtitle, 125)) + ') Tj ET');
        y -= 14;
      } else {
        y -= 3;
      }

      drawTableHeader(headers, widths);

      rows.forEach(function(row, rIdx) {
        var rh = 15.5;
        if (y - rh < Y_MIN) {
          newPage(false);
          cur.push('0.06 0.09 0.16 rg', 'BT /F2 9.5 Tf ' + M + ' ' + (y - 11) + ' Td (' + pdfEsc(pdfClip(sec.title + ' (continued)', 105)) + ') Tj ET');
          y -= 15;
          drawTableHeader(headers, widths);
        }
        var ry = y - rh;
        if (rIdx % 2 === 1) {
          cur.push('0.98 0.985 0.995 rg 0.86 0.89 0.93 RG 0.4 w', M + ' ' + ry + ' ' + CW + ' ' + rh + ' re B');
        } else {
          cur.push('1 1 1 rg 0.86 0.89 0.93 RG 0.4 w', M + ' ' + ry + ' ' + CW + ' ' + rh + ' re B');
        }
        var tx = M;
        for (var c = 0; c < headers.length; c++) {
          var cw = widths[c];
          var maxCh = Math.max(4, Math.floor((cw - 8) / 4.2));
          var cellVal = row && row[c] !== undefined && row[c] !== null ? row[c] : '--';
          cur.push('0.09 0.13 0.20 rg', 'BT /F1 7.5 Tf ' + (tx + 4) + ' ' + (ry + 4.5) + ' Td (' + pdfEsc(pdfClip(cellVal, maxCh)) + ') Tj ET');
          tx += cw;
        }
        y = ry;
      });
      y -= 12;
    });

    // Add footers to all pages
    var totalPages = pages.length;
    pages.forEach(function(cmds, idx) {
      cmds.push('0.80 0.84 0.88 RG 0.5 w', M + ' 36 m ' + (W - M) + ' 36 l S');
      cmds.push('0.39 0.45 0.55 rg', 'BT /F1 7.5 Tf ' + M + ' 24 Td (FinOps Analytics Platform for Agent Optimization | Grounded Telemetry Report) Tj ET');
      var pgTxt = 'Page ' + (idx + 1) + ' of ' + totalPages;
      cmds.push('0.39 0.45 0.55 rg', 'BT /F2 7.5 Tf ' + (W - M - 58) + ' 24 Td (' + pdfEsc(pgTxt) + ') Tj ET');
    });

    // Assemble PDF 1.4 objects
    var objs = [];
    objs.push('1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj');
    var pageObjIds = [];
    for (var p = 0; p < totalPages; p++) {
      pageObjIds.push(6 + p * 2);
    }
    objs.push('2 0 obj\n<< /Type /Pages /Kids [' + pageObjIds.map(function(id) { return id + ' 0 R'; }).join(' ') + '] /Count ' + totalPages + ' >>\nendobj');
    objs.push('3 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>\nendobj');
    objs.push('4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>\nendobj');
    objs.push('5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>\nendobj');

    for (var pIdx = 0; pIdx < totalPages; pIdx++) {
      var pageId = 6 + pIdx * 2;
      var streamId = pageId + 1;
      var streamBody = pages[pIdx].join('\n');
      objs.push(pageId + ' 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ' + W + ' ' + H + '] ' +
                '/Resources << /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >> /Contents ' + streamId + ' 0 R >>\nendobj');
      objs.push(streamId + ' 0 obj\n<< /Length ' + streamBody.length + ' >>\nstream\n' + streamBody + '\nendstream\nendobj');
    }

    var parts = ['%PDF-1.4\n'];
    var offsets = [0];
    var cursor = parts[0].length;
    for (var oIdx = 0; oIdx < objs.length; oIdx++) {
      offsets.push(cursor);
      var chunk = objs[oIdx] + '\n';
      parts.push(chunk);
      cursor += chunk.length;
    }
    var xrefOffset = cursor;
    var totalObjs = objs.length + 1;
    var xrefLines = ['xref', '0 ' + totalObjs, '0000000000 65535 f '];
    for (var x = 1; x < totalObjs; x++) {
      var pad = ('0000000000' + offsets[x]).slice(-10);
      xrefLines.push(pad + ' 00000 n ');
    }
    parts.push(xrefLines.join('\n') + '\n');
    parts.push('trailer\n<< /Size ' + totalObjs + ' /Root 1 0 R >>\nstartxref\n' + xrefOffset + '\n%%EOF\n');
    var pdfStr = parts.join('');
    var bytes = new Uint8Array(pdfStr.length);
    for (var bi = 0; bi < pdfStr.length; bi++) {
      bytes[bi] = pdfStr.charCodeAt(bi) & 0x7f;
    }
    return {pdf: pdfStr, bytes: bytes, pageCount: totalPages, filename: filename};
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
    var svg = btn.querySelector ? btn.querySelector('svg') : null;
    if (svg && btn.lastChild && btn.lastChild.nodeType === 3) {
      var tn = btn.lastChild;
      if (!btn.dataset.vlLabel) btn.dataset.vlLabel = tn.nodeValue;
      tn.nodeValue = ' ' + text;
      setTimeout(function() { tn.nodeValue = btn.dataset.vlLabel; }, 2600);
      return;
    }
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

  function triggerBlobDownload(filename, mime, dataPart) {
    try {
      var blob = new Blob([dataPart], {type: mime});
      var href = URL.createObjectURL(blob);
      var a = h('a', {href: href, download: filename, style: 'display:none'});
      document.body.appendChild(a);
      a.click();
      setTimeout(function() { try { URL.revokeObjectURL(href); a.remove(); } catch (e) {} }, 1800);
      return true;
    } catch (e) {
      return false;
    }
  }

  function downloadPdf(filename, pdfStr, pdfBytes, btn) {
    var b64 = '';
    try { b64 = typeof btoa === 'function' ? btoa(pdfStr) : ''; } catch (e) { b64 = ''; }
    if (embedded() && hostCan('downloadFile')) {
      callHost('ui/download-file', {contents: [{type: 'resource', resource: {
        uri: 'file:///' + filename, mimeType: 'application/pdf', blob: b64, text: pdfStr}}]}, 8000)
        .then(function() { flash(btn, 'Downloaded PDF'); })
        .catch(function() {
          if (triggerBlobDownload(filename, 'application/pdf', pdfBytes || pdfStr)) {
            flash(btn, 'Downloaded PDF');
          } else if (typeof window.print === 'function') {
            try { window.print(); } catch (e) {}
            flash(btn, 'Print opened');
          }
        });
      return;
    }
    var ok = triggerBlobDownload(filename, 'application/pdf', pdfBytes || pdfStr);
    if (ok) {
      flash(btn, 'Downloaded PDF');
    } else if (typeof window.print === 'function') {
      try { window.print(); } catch (e) {}
      flash(btn, 'Print opened');
    }
  }

  function exportPdf(opts) {
    var o = opts || {};
    var btn = o.button || byId('exportPdfBtn');
    var geSel = byId('geScopeSelect');
    var scopeTxt = geSel ? (geSel.options[geSel.selectedIndex] ? geSel.options[geSel.selectedIndex].text : geSel.value) : 'All apps';
    var winSel = byId('fleetWindow');
    var winTxt = winSel ? (winSel.options[winSel.selectedIndex] ? winSel.options[winSel.selectedIndex].text : winSel.value) : windowText();
    var projNode = byId('gcpProjectText');
    var projTxt = projNode && projNode.textContent ? projNode.textContent.trim() : 'Current';
    var activeTabBtn = document.querySelector ? document.querySelector('.tab-btn.active') : null;
    var activeTabName = activeTabBtn ? activeTabBtn.textContent.trim() : 'Overview';

    var fl = null;
    try { fl = lastFleet || geRawFleet || null; } catch (e) { fl = rawFleet(); }
    var st = state();

    // Capture visible KPI cards from the active view if available
    var domKpis = [];
    try {
      var activeSection = document.querySelector ? document.querySelector('main > section:not(.hidden)') : null;
      var kpiNodes = activeSection ? activeSection.querySelectorAll('.kpi-card') : [];
      Array.prototype.slice.call(kpiNodes, 0, 8).forEach(function(card) {
        var lbl = card.querySelector('.kpi-label');
        var val = card.querySelector('.kpi-value');
        var sub = card.querySelector('.kpi-sub');
        if (lbl && val) {
          domKpis.push({
            label: lbl.textContent.trim(),
            value: val.textContent.trim(),
            sub: sub ? sub.textContent.trim() : '',
          });
        }
      });
    } catch (e) {}

    var built = vlBuildPdfReport({
      activeTab: activeTabName,
      scope: scopeTxt,
      windowLabel: winTxt,
      project: projTxt,
      fleet: fl,
      state: st,
      kpis: domKpis,
    });

    downloadPdf(built.filename, built.pdf, built.bytes, btn);
    if (o.event && o.event.shiftKey && !embedded() && typeof window.print === 'function') {
      try { window.print(); } catch (e) {}
    }
    return built;
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
    downloadText((st.cfg.exportName || 'finops-' + st.id) + '-' + stamp + '.csv', 'text/csv', vlToCsv(headers, rows), st.csvBtn);
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
    safeRun('VL.renderHealthStrip.governanceScore', function() { renderGovernanceScore(state(), rawFleet() || {agents: list}); });
  }

  var govScoreExpanded = false;

  function runGovernanceAction(p) {
    if (!p) return;
    var tabIdx = typeof p.actionTab === 'number' ? p.actionTab : 6;
    try { switchTab(tabIdx); } catch (e) {}
    if (p.actionTarget) {
      var target = byId(p.actionTarget);
      if (target) {
        var panel = target.closest('.panel') || target;
        if (panel && panel.scrollIntoView) panel.scrollIntoView({block: 'start'});
      }
    }
  }

  function toggleGovernanceScorecard(forceOpen) {
    govScoreExpanded = typeof forceOpen === 'boolean' ? forceOpen : !govScoreExpanded;
    try { switchTab(6); } catch (e) {}
    renderGovernanceScore(state(), rawFleet());
    var panel = byId('vlGovScorePanel');
    if (govScoreExpanded && panel && panel.scrollIntoView) {
      panel.scrollIntoView({block: 'nearest'});
    }
  }

  function renderGovernanceScore(st, fl) {
    var stObj = st || state() || {};
    var flObj = fl || rawFleet() || stObj.ge_fleet || {};
    var gov = vlGovernanceScore({
      fleet: flObj,
      state: stObj,
      costGovernance: stObj.cost_governance || null,
      budgetUsd: readBudget(),
    });
    if (!gov) return;

    var chip = byId('vlGovScoreChip');
    if (chip) {
      var tierCls = gov.score >= 85 ? 'vl-gov-chip-ok' : (gov.score >= 70 ? 'vl-gov-chip-good' : (gov.score >= 50 ? 'vl-gov-chip-warn' : 'vl-gov-chip-bad'));
      chip.className = 'btn vl-gov-chip ' + tierCls;
      chip.setAttribute('aria-expanded', govScoreExpanded ? 'true' : 'false');
      chip.title = 'Enterprise AI Governance & Tokenomics Score: ' + gov.score + '/100 (' + gov.grade + ' \u00b7 ' + gov.statusLabel + '). Click to inspect 5-pillar breakdown.';
      setKids(chip, [
        h('span', {class: 'vl-gov-dot'}),
        h('span', {}, ['Gov Score: ']),
        h('strong', {class: 'mono'}, [gov.score + '/100']),
        h('span', {class: 'vl-gov-grade mono'}, [gov.grade]),
      ]);
      chip.classList.remove('hidden');
    }

    var box = byId('vlGovScorePanel');
    if (!box) return;
    var badgeTone = gov.score >= 85 ? 'badge-green' : (gov.score >= 70 ? 'badge-blue' : (gov.score >= 50 ? 'badge-yellow' : 'badge-red'));
    var toggleBtn = h('button', {
      type: 'button',
      class: 'btn btn-xs',
      'aria-expanded': govScoreExpanded ? 'true' : 'false',
      onclick: function() {
        govScoreExpanded = !govScoreExpanded;
        renderGovernanceScore(state(), rawFleet());
      },
    }, [govScoreExpanded ? 'Hide 5-pillar breakdown \u25b4' : 'Inspect 5 pillars & remediate \u25be']);

    var topAct = gov.topAction;
    var quickFixBtn = topAct && topAct.ptsLost > 0 ? h('button', {
      type: 'button',
      class: 'btn btn-xs btn-primary',
      onclick: function() { runGovernanceAction(topAct); },
    }, [topAct.actionLabel + ' (+' + topAct.ptsLost + ' pts)']) : null;

    var acct = gov.accountability || {};
    var acctBadge = acct.total_points_possible === 100 ? h('span', {
      class: 'badge badge-gray mono',
      title: 'Measured telemetry points: ' + acct.measured_points_earned + '/' + acct.measured_points_possible +
             ' | Unconfigured neutral baseline: ' + acct.unconfigured_neutral_points + '/' + acct.unconfigured_points_possible,
    }, [
      'Measured ' + acct.measured_points_earned + '/' + acct.measured_points_possible + ' pts' +
      (acct.unconfigured_points_possible > 0 ? ' \u00b7 Baseline ' + acct.unconfigured_neutral_points + '/' + acct.unconfigured_points_possible + ' pts' : ' \u00b7 100% Grounded'),
    ]) : null;

    var cards = govScoreExpanded ? h('div', {class: 'vl-gov-grid'}, (gov.pillars || []).map(function(p) {
      var pct = p.max > 0 ? Math.min(100, Math.max(0, Math.round(100 * p.score / p.max))) : 0;
      var barCls = p.status === 'healthy' ? 'vl-gov-bar-ok' : (p.status === 'warning' ? 'vl-gov-bar-warn' : 'vl-gov-bar-bad');
      var cardBadge = p.status === 'healthy' ? 'badge-green' : (p.status === 'warning' ? 'badge-yellow' : 'badge-red');
      var compRows = Array.isArray(p.components) && p.components.length ? h('div', {class: 'vl-gov-comp-list'}, p.components.map(function(c) {
        if (c.max_points === 0 && c.points === 0) return null;
        var ptStr = c.max_points > 0 ? (c.points + '/' + c.max_points + ' pts') : ((c.points >= 0 ? '+' : '') + c.points + ' pts');
        var isUnconf = c.grounding_status && c.grounding_status !== 'MEASURED';
        return h('div', {class: 'vl-gov-comp-row mono', title: 'Source: ' + (c.source || '') + ' (' + (c.grounding_status || 'MEASURED') + ')'}, [
          h('span', {class: 'vl-gov-comp-lbl'}, [c.label + (isUnconf ? ' (baseline)' : '')]),
          h('span', {class: 'vl-gov-comp-pts'}, [ptStr]),
        ]);
      })) : null;
      return h('div', {class: 'vl-gov-card'}, [
        h('div', {class: 'vl-gov-card-top'}, [
          h('strong', {class: 'vl-gov-card-title'}, [p.label]),
          h('span', {class: 'badge ' + cardBadge + ' mono'}, [p.score + '/' + p.max + ' pts']),
        ]),
        h('div', {class: 'vl-gov-bar', role: 'img', 'aria-label': p.label + ' score ' + p.score + ' of ' + p.max}, [
          h('div', {class: 'vl-gov-bar-fill ' + barCls, style: 'width:' + pct + '%'}),
        ]),
        compRows,
        h('div', {class: 'vl-gov-card-sum mono'}, [p.summary]),
        h('div', {class: 'vl-gov-card-rec'}, [p.recommendation]),
        p.actionLabel ? h('div', {class: 'vl-gov-card-foot'}, [
          h('button', {
            type: 'button',
            class: 'btn btn-xs',
            onclick: function() { runGovernanceAction(p); },
          }, [p.actionLabel + (p.ptsLost > 0 ? ' (+' + p.ptsLost + ' pts)' : ' \u2192')]),
        ]) : null,
      ]);
    })) : null;

    setKids(box, [
      h('div', {class: 'vl-gov-head'}, [
        h('div', {class: 'vl-gov-head-left'}, [
          h('span', {class: 'vl-strip-title'}, ['Enterprise AI Governance Score']),
          h('span', {class: 'vl-gov-big mono'}, [gov.score + '/100']),
          h('span', {class: 'badge ' + badgeTone}, ['Grade ' + gov.grade + ' \u00b7 ' + gov.statusLabel]),
          acctBadge,
          topAct && topAct.ptsLost > 0
            ? h('span', {class: 'vl-gov-sub'}, ['Top opportunity: ' + topAct.label + ' (' + topAct.summary + ')'])
            : h('span', {class: 'vl-gov-sub'}, ['All 5 FinOps & Governance pillars are operating at target thresholds.']),
        ]),
        h('div', {class: 'vl-links'}, [quickFixBtn, toggleBtn]),
      ]),
      cards,
    ]);
    box.classList.remove('hidden');
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
      ['Runs on', ({agent_engine: 'Vertex AI Agent Engine', cloud_run: 'Cloud Run', gke: 'GKE', gke_workload: 'GKE', external_endpoint: 'External endpoint',
                    gemini_enterprise_hosted: 'Gemini Enterprise hosted', dialogflow: 'Dialogflow'})[b.kind] || (b.kind ? b.kind : 'Google-managed')],
      ['Resource', b.resource || b.service || b.url || null, true],
      ['Region', b.location || b.region || null, true],
      ['Framework', b.framework || null],
      ['Models', models || null, true],
      ['Token source', m.token_source || null],
      ['Telemetry scope', {agent: 'Per agent', service: 'Whole Cloud Run service (all traffic)', container: 'GKE container (all replicas)', none: 'Inventory only'}[a.telemetry_scope] || null],
      ['Registration', a.registration ? [a.registration.status, a.registration.evidence].filter(Boolean).join(' \u2014 ') : null],
    ])]));

    var fl = rawFleet() || fleet || {};
    var repricingSec = agentRepricingSection(a, fl, win);
    if (repricingSec) wrap.appendChild(repricingSec);

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
        h('div', {class: 'vl-d-note'}, [reg.action.summary || 'Review, then run this command yourself. FinOps Dashboard never deletes anything.']),
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
        h('div', {class: 'vl-d-row'}, [h('span', {class: 'vl-d-note'}, ['From the most recent sessions in finops_mart.fct_sessions (agent name match).']), allBtn]),
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

  function agentRepricingSection(a, fl, win) {
    var rep = vlModelRepricing({agents: [a], model_usage: fl && fl.model_usage, window_hours: fl && fl.window_hours});
    if (!rep || !rep.byAgent || !rep.byAgent.length) return null;
    var row = rep.byAgent[0];
    return section('Model re-pricing what-if \u00b7 ' + win, [
      h('div', {class: 'vl-d-note'}, [
        'Observed agent tokens (', fmtTokens(row.inputTokens), ' input, ', fmtTokens(row.outputTokens), ' output, ',
        fmtTokens(row.cachedTokens), ' cached read) on ', h('code', {}, [row.currentModel]),
        ' cost an estimated ', h('strong', {class: 'mono'}, [usdText(row.currentCostUsd)]),
        ' in this window. List-price arithmetic if the exact same token mix ran on a lower-cost tier (quality and latency not evaluated):',
      ]),
      h('table', {class: 'vl-mini-table', style: 'margin-top:8px'}, [
        h('thead', {}, [h('tr', {}, [
          h('th', {}, ['Alternative tier']),
          h('th', {}, ['Est. cost (' + win + ')']),
          h('th', {}, ['Window savings']),
          h('th', {}, ['30-day pace savings']),
        ])]),
        h('tbody', {}, row.alternatives.map(function(al) {
          return h('tr', {}, [
            h('td', {class: 'mono'}, [al.targetModel]),
            h('td', {class: 'mono'}, [usdText(al.altCostUsd)]),
            h('td', {class: 'mono'}, [h('span', {class: 'badge badge-green'}, ['-' + usdText(al.savingsWindowUsd) + ' (' + pctText(al.savingsPct) + ')'])]),
            h('td', {class: 'mono'}, [usdText(al.savings30dUsd, 0) + '/mo']),
          ]);
        })),
      ]),
    ]);
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
    syncContextSoon('FinOps Dashboard dashboard: the user is viewing agent "' + (fresh.display_name || id) + '" (' +
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
    var sp = sparkline(vals, {width: 160, height: 26, color: '#10b981', fill: 'rgba(16,185,129,0.12)', label: 'Daily active users, last ' + vals.length + ' days (finops_mart)'});
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
      if (!panel) return;
      // A target on another tab (e.g. the Cost tab's loop audit) needs that tab shown before it can scroll.
      var tabPanel = typeof panel.closest === 'function' ? panel.closest('[id^="tabPanel"]') : null;
      var tabIdx = tabPanel ? Number(String(tabPanel.id).slice('tabPanel'.length)) : NaN;
      if (tabPanel && tabPanel.classList.contains('hidden') && isFinite(tabIdx) &&
          typeof window !== 'undefined' && typeof window.switchTab === 'function') {
        window.switchTab(tabIdx);
      }
      if (panel.scrollIntoView) panel.scrollIntoView({behavior: 'smooth', block: 'start'});
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
  // Part 7: Cost tab spend overview, runway gauge, waterfall chart, and tokenomics
  // ===================================================================================================
  function budgetKey() {
    var st = state();
    return 'finops.budget.' + ((st && st.gcp_project) || 'default');
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
    var beforeUsd = num(drift.cost_before_usd);
    var shareNote = '';
    if (share !== null && Math.abs(share) <= 200) {
      shareNote = ' (' + share.toFixed(0) + '% of the change)';
    } else if (beforeUsd && beforeUsd > 0) {
      var vsPrev = (ch / beforeUsd) * 100;
      shareNote = ' (' + (vsPrev >= 0 ? '+' : '') + vsPrev.toFixed(0) + '% vs previous period)';
    }
    return kpi('Biggest driver of change', String(d.name || d.driver_id),
      (ch > 0 ? '+' : '-') + usdText(Math.abs(ch)) + shareNote +
      ' vs the previous period of the same length. ' + String(d.description || ''),
      null, ch > 0 ? 'vl-kpi-warn' : 'vl-kpi-ok');
  }

  function renderBudgetRunway(cs) {
    var box = byId('vlBudgetRunway');
    if (!box) return;
    var rw = vlBudgetRunway(cs);
    if (!rw || rw.status === 'NO_BILLING' || rw.status === 'NO_PACE') {
      box.classList.add('hidden');
      box.replaceChildren();
      return;
    }
    if (rw.status === 'NO_BUDGET') {
      var presets = [1500, 2000, 3000].map(function(val) {
        return h('button', {type: 'button', class: 'btn btn-xs', onclick: function() {
          writeBudget(val);
          renderSpend(state());
        }}, ['Try ' + usdText(val, 0) + '/mo']);
      });
      setKids(box, [
        h('div', {class: 'vl-runway-card vl-runway-empty'}, [
          h('div', {class: 'vl-runway-head'}, [
            h('span', {class: 'vl-runway-title'}, ['Budget runway gauge']),
            h('span', {class: 'vl-d-note', style: 'margin:0'}, [
              'Month-to-date billed spend is ', h('strong', {class: 'mono'}, [usdText(rw.mtd)]),
              (rw.projection !== null ? ' (projected month-end ' + usdText(rw.projection) + '). ' : '. '),
              'Pick a monthly budget to see your exhaustion date, pace ratio, and safe daily cap:',
            ]),
            h('div', {class: 'vl-links'}, presets.concat([
              h('button', {type: 'button', class: 'btn btn-xs btn-primary', onclick: function() { toggleBudgetForm(true); }}, ['Custom budget\u2026']),
            ])),
          ]),
        ]),
      ]);
      box.classList.remove('hidden');
      return;
    }

    var isRisk = rw.status === 'AT_RISK' || rw.status === 'EXHAUSTED';
    var statusBadge = rw.status === 'EXHAUSTED'
      ? h('span', {class: 'badge badge-red'}, ['Budget exhausted'])
      : (rw.status === 'AT_RISK'
          ? h('span', {class: 'badge badge-yellow'}, ['Exhausts ' + shortDay(rw.exhaustDate) + ' (' + Math.ceil(rw.daysUntilExhausted) + 'd left)'])
          : h('span', {class: 'badge badge-green'}, ['Clears ' + monthName(rw.month) + ' (+' + usdText(rw.varianceUsd, 0) + ' headroom)']));

    var usedW = Math.min(100, Math.max(0, rw.usedPct || 0));
    var projW = rw.projectedPct !== null ? Math.min(100, Math.max(usedW, rw.projectedPct)) : usedW;
    var dayPct = rw.daysInMonth > 0 ? Math.min(100, Math.max(0, 100 * rw.lastDayOfMonth / rw.daysInMonth)) : 0;

    var track = h('div', {class: 'vl-runway-track', role: 'img', 'aria-label': 'Budget runway progress bar'}, [
      h('div', {class: 'vl-runway-fill-used' + (isRisk ? ' vl-runway-warn' : ''), style: 'width:' + usedW.toFixed(1) + '%',
                title: 'MTD billed: ' + usdText(rw.mtd) + ' (' + pctText(rw.usedPct) + ')'}),
      projW > usedW ? h('div', {class: 'vl-runway-fill-proj' + (isRisk ? ' vl-runway-warn' : ''),
                                style: 'left:' + usedW.toFixed(1) + '%;width:' + (projW - usedW).toFixed(1) + '%',
                                title: 'Projected remaining burn: ' + usdText(rw.projection - rw.mtd)}) : null,
      h('div', {class: 'vl-runway-marker', style: 'left:' + dayPct.toFixed(1) + '%',
                title: 'Calendar progress: day ' + rw.lastDayOfMonth + ' of ' + rw.daysInMonth + ' (' + pctText(dayPct) + ' of month)'}),
    ]);

    var metrics = h('div', {class: 'vl-runway-metrics'}, [
      h('div', {class: 'vl-runway-stat'}, [
        h('span', {class: 'vl-runway-stat-lbl'}, ['7-day burn vs budget pace']),
        h('span', {class: 'vl-runway-stat-val mono'}, [
          usdText(rw.avg7) + '/d vs ' + usdText(rw.dailyPace) + '/d' +
          (rw.paceRatio !== null ? ' (' + rw.paceRatio.toFixed(2) + '\u00d7)' : ''),
        ]),
      ]),
      h('div', {class: 'vl-runway-stat'}, [
        h('span', {class: 'vl-runway-stat-lbl'}, ['Safe daily cap (' + rw.daysRemaining + 'd left)']),
        h('span', {class: 'vl-runway-stat-val mono'}, [usdText(rw.safeDailyAllowance) + '/day']),
      ]),
      h('div', {class: 'vl-runway-stat'}, [
        h('span', {class: 'vl-runway-stat-lbl'}, [rw.exhaustsInMonth ? 'Projected exhaustion date' : 'Projected month-end']),
        h('span', {class: 'vl-runway-stat-val mono'}, [
          rw.exhaustsInMonth
            ? shortDay(rw.exhaustDate) + ' (day ' + rw.exhaustDayOfMonth + ' of ' + rw.daysInMonth + ')'
            : usdText(rw.projection) + ' of ' + usdText(rw.budget, 0) + ' (' + pctText(rw.projectedPct) + ')',
        ]),
      ]),
    ]);

    setKids(box, [
      h('div', {class: 'vl-runway-card' + (isRisk ? ' vl-runway-card-warn' : ' vl-runway-card-ok')}, [
        h('div', {class: 'vl-runway-head'}, [
          h('div', {}, [
            h('span', {class: 'vl-runway-title'}, ['Budget runway \u00b7 ' + monthName(rw.month)]),
            ' ',
            statusBadge,
          ]),
          h('div', {class: 'vl-links'}, [
            h('button', {type: 'button', class: 'btn btn-xs', onclick: function() { toggleBudgetForm(true); }}, ['Adjust budget (' + usdText(rw.budget, 0) + ')']),
          ]),
        ]),
        track,
        h('div', {class: 'vl-runway-legend'}, [
          h('span', {}, [h('i', {class: 'vl-rw-swatch vl-rw-s-used'}), 'MTD billed (' + usdText(rw.mtd) + ')']),
          h('span', {}, [h('i', {class: 'vl-rw-swatch vl-rw-s-proj'}), 'Projected 7d run-rate (' + usdText(rw.projection) + ')']),
          h('span', {}, [h('i', {class: 'vl-rw-swatch vl-rw-s-cal'}), 'Calendar day ' + rw.lastDayOfMonth + '/' + rw.daysInMonth]),
        ]),
        metrics,
      ]),
    ]);
    box.classList.remove('hidden');
  }

  var selectedWaterfallStepId = null;

  function renderDriftWaterfall() {
    var box = byId('vlDriftWaterfall');
    var detailBox = byId('vlDriftDriverDetail');
    if (!box) return;
    var lf = liveFinops();
    var wf = vlDriftWaterfall(lf && lf.spend_drift);
    if (!wf || !wf.steps || wf.steps.length < 2) {
      box.replaceChildren();
      if (detailBox) { detailBox.classList.add('hidden'); detailBox.replaceChildren(); }
      return;
    }

    var W = 960;
    var H = 205;
    var L = 62;
    var R = 20;
    var T = 26;
    var B = 44;
    var plotW = W - L - R;
    var plotH = H - T - B;
    var maxV = wf.maxUsd * 1.15 || 1;
    var yOf = function(val) { return T + plotH * (1 - Math.max(0, val) / maxV); };

    var svg = svgEl('svg', {
      viewBox: '0 0 ' + W + ' ' + H,
      class: 'vl-wf-svg',
      role: 'img',
      'aria-label': 'Spend change waterfall chart from ' + usdText(wf.before) + ' to ' + usdText(wf.now),
    });

    [0, 0.5, 1].forEach(function(f) {
      var val = maxV * f;
      var y = yOf(val);
      svg.appendChild(svgEl('line', {x1: L, x2: W - R, y1: y.toFixed(1), y2: y.toFixed(1), stroke: '#e2e8f0', 'stroke-width': 1}));
      svg.appendChild(svgEl('text', {x: L - 8, y: (y + 3).toFixed(1), 'text-anchor': 'end', class: 'vl-wf-axis'}, usdText(val, val >= 10 ? 0 : 2)));
    });

    var n = wf.steps.length;
    var slotW = plotW / n;
    var barW = Math.min(88, slotW * 0.62);

    var showDriverDetail = function(step) {
      if (!detailBox) return;
      if (!step || step.kind !== 'delta' || selectedWaterfallStepId === step.id) {
        selectedWaterfallStepId = null;
        detailBox.classList.add('hidden');
        detailBox.replaceChildren();
        renderDriftWaterfall();
        resized();
        return;
      }
      selectedWaterfallStepId = step.id;
      var closeBtn = h('button', {type: 'button', class: 'btn btn-xs', onclick: function() {
        selectedWaterfallStepId = null;
        detailBox.classList.add('hidden');
        detailBox.replaceChildren();
        renderDriftWaterfall();
        resized();
      }}, ['Close \u2715']);

      var rows = (step.models || []).map(function(m) {
        var d = m.deltaUsd || 0;
        return h('tr', {}, [
          h('td', {class: 'mono'}, [m.model || DASH]),
          h('td', {class: 'mono'}, [
            h('span', {class: 'badge ' + (d > 0 ? 'badge-yellow' : 'badge-green')}, [(d >= 0 ? '+' : '-') + usdText(Math.abs(d))]),
          ]),
          h('td', {class: 'mono'}, [usdText(m.costBefore) + ' \u2192 ' + usdText(m.costNow)]),
          h('td', {class: 'mono'}, [fmtInt(m.callsBefore) + ' \u2192 ' + fmtInt(m.callsNow)]),
        ]);
      });

      var detailPctNote = null;
      if (step.sharePct !== null && Math.abs(step.sharePct) <= 200) {
        detailPctNote = h('span', {class: 'vl-d-note', style: 'margin-left:8px'}, ['(' + step.sharePct.toFixed(1) + '% of net change)']);
      } else if (step.pctOfBefore !== null) {
        detailPctNote = h('span', {class: 'vl-d-note', style: 'margin-left:8px'}, ['(' + (step.pctOfBefore >= 0 ? '+' : '') + step.pctOfBefore.toFixed(1) + '% vs previous period)']);
      }

      setKids(detailBox, [
        h('div', {class: 'vl-drift-detail-head'}, [
          h('div', {}, [
            h('strong', {}, [step.label + ': ' + (step.delta >= 0 ? '+' : '-') + usdText(Math.abs(step.delta))]),
            detailPctNote,
            h('div', {class: 'vl-d-note'}, [step.description]),
          ]),
          closeBtn,
        ]),
        rows.length ? h('table', {class: 'vl-mini-table', style: 'margin-top:8px'}, [
          h('thead', {}, [h('tr', {}, [
            h('th', {}, ['Model']),
            h('th', {}, [step.label + ' impact']),
            h('th', {}, ['Model spend (before \u2192 now)']),
            h('th', {}, ['Calls (before \u2192 now)']),
          ])]),
          h('tbody', {}, rows),
        ]) : h('div', {class: 'vl-d-note'}, ['No single model moved by at least $0.01 on this driver.']),
      ]);
      detailBox.classList.remove('hidden');
      renderDriftWaterfall();
      resized();
    };

    wf.steps.forEach(function(st, i) {
      var cx = L + (i + 0.5) * slotW;
      var bx = cx - barW / 2;
      var topVal = st.kind === 'anchor' ? st.value : Math.max(st.start, st.end);
      var botVal = st.kind === 'anchor' ? 0 : Math.min(st.start, st.end);
      var yTop = yOf(topVal);
      var yBot = yOf(botVal);
      var bh = Math.max(3, yBot - yTop);
      var fill = st.kind === 'anchor'
        ? '#2563eb'
        : (st.direction === 'up' ? '#f59e0b' : (st.direction === 'down' ? '#10b981' : '#94a3b8'));
      var isSelected = selectedWaterfallStepId === st.id;

      if (i > 0) {
        var prevEnd = wf.steps[i - 1].end;
        var yConn = yOf(prevEnd);
        var prevRight = L + (i - 0.5) * slotW + barW / 2;
        svg.appendChild(svgEl('line', {
          x1: prevRight.toFixed(1),
          x2: bx.toFixed(1),
          y1: yConn.toFixed(1),
          y2: yConn.toFixed(1),
          stroke: '#94a3b8',
          'stroke-width': 1.2,
          'stroke-dasharray': '3,3',
        }));
      }

      var g = svgEl('g', {
        class: 'vl-wf-bar' + (st.kind === 'delta' ? ' vl-wf-clickable' : '') + (isSelected ? ' vl-wf-selected' : ''),
        tabindex: st.kind === 'delta' ? '0' : null,
        role: st.kind === 'delta' ? 'button' : null,
        'aria-label': st.label + ': ' + (st.kind === 'anchor' ? usdText(st.value) : (st.delta >= 0 ? '+' : '-') + usdText(Math.abs(st.delta))),
      });
      var rect = svgEl('rect', {
        x: bx.toFixed(1),
        y: yTop.toFixed(1),
        width: barW.toFixed(1),
        height: bh.toFixed(1),
        rx: 4,
        fill: fill,
        stroke: isSelected ? '#0f172a' : 'none',
        'stroke-width': isSelected ? '2' : '0',
      });
      g.appendChild(rect);

      var valTxt = st.kind === 'anchor'
        ? usdText(st.value)
        : (Math.abs(st.delta) < 0.005 ? '$0.00' : (st.delta > 0 ? '+' : '-') + usdText(Math.abs(st.delta)));
      g.appendChild(svgEl('text', {
        x: cx.toFixed(1),
        y: Math.max(12, yTop - 6).toFixed(1),
        'text-anchor': 'middle',
        class: 'vl-wf-val',
      }, valTxt));

      g.appendChild(svgEl('text', {
        x: cx.toFixed(1),
        y: (H - B + 16).toFixed(1),
        'text-anchor': 'middle',
        class: 'vl-wf-lbl',
      }, st.label));

      if (st.kind === 'delta' && Math.abs(st.delta) >= 0.005) {
        var subPctTxt = null;
        if (st.sharePct !== null && Math.abs(st.sharePct) <= 200) {
          subPctTxt = (st.sharePct >= 0 ? '+' : '') + st.sharePct.toFixed(0) + '%';
        } else if (st.pctOfBefore !== null) {
          subPctTxt = (st.pctOfBefore >= 0 ? '+' : '') + st.pctOfBefore.toFixed(0) + '% vs prev';
        }
        if (subPctTxt) {
          g.appendChild(svgEl('text', {
            x: cx.toFixed(1),
            y: (H - B + 30).toFixed(1),
            'text-anchor': 'middle',
            class: 'vl-wf-sub',
          }, subPctTxt));
        }
      } else if (st.kind === 'anchor') {
        g.appendChild(svgEl('text', {
          x: cx.toFixed(1),
          y: (H - B + 30).toFixed(1),
          'text-anchor': 'middle',
          class: 'vl-wf-sub',
        }, st.id === 'before' ? 'baseline' : (wf.change >= 0 ? '+' : '-') + usdText(Math.abs(wf.change))));
      }

      g.appendChild(svgEl('title', {}, st.label + ': ' + valTxt + (st.description ? ' \u2014 ' + st.description + ' (Click to inspect models)' : '')));

      if (st.kind === 'delta') {
        g.addEventListener('click', function() { showDriverDetail(st); });
        g.addEventListener('keydown', function(ev) {
          if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); showDriverDetail(st); }
        });
      }
      svg.appendChild(g);
    });

    setKids(box, [
      h('div', {class: 'vl-wf-wrap'}, [
        svg,
        h('div', {class: 'vl-wf-hint'}, [
          'Click any driver bar (Call volume, Prompt size, Output length, Caching, Models started/stopped) to inspect per-model contributions.',
        ]),
      ]),
    ]);
  }

  function renderTokenomics(fl) {
    var panel = byId('vlTokenomicsPanel');
    if (!panel) return;
    var fleet = fl || rawFleet() || {};
    var mu = fleet.model_usage || null;
    var win = windowText();
    var badgeEl = byId('vlTokenomicsBadge');
    if (badgeEl) badgeEl.textContent = 'List-price math \u00b7 ' + win;

    var tax = vlThinkingTax(mu, fleet.window_hours);
    var rep = vlModelRepricing(fleet);
    var kpisBox = byId('vlTokenomicsKpis');
    var taxBox = byId('vlThinkingTaxBox');
    var repBox = byId('vlRepricingBox');

    if (!tax && (!rep || (!rep.byModel.length && !rep.byAgent.length))) {
      setKids(kpisBox, [kpi('Tokenomics', DASH, 'Needs Vertex AI model usage or per-agent token telemetry.')]);
      if (taxBox) taxBox.replaceChildren();
      if (repBox) repBox.replaceChildren();
      return;
    }

    var stObj = state() || {};
    var lf = (fleet && fleet.live_finops) || stObj.live_finops || {};
    var vr = lf.value_realization || null;
    var lcb = lf.loop_circuit_breaker || null;
    var gsu = lf.gsu_and_quota_forecast || null;
    var mmr = lf.model_modernization_radar || null;

    var vrBox = byId('vlValueRealizationBox');
    var lcbBox = byId('vlLoopCircuitBreakerBox');
    var gsuBox = byId('vlGsuForecastBox');
    var mmrBox = byId('vlModernizationRadarBox');

    var cards = [];
    if (tax) {
      cards.push(kpi('Thinking-token spend \u00b7 ' + win, usdText(tax.reasoningCostUsd),
        fmtTokens(tax.reasoningTokens) + ' reasoning tokens (' + pctText(tax.thinkingShareOfOutputPct) + ' of all output tokens; ' +
        (tax.thinkingShareOfSpendPct !== null ? pctText(tax.thinkingShareOfSpendPct) + ' of total model spend' : 'billed at output rate') + ').',
        null, tax.thinkingShareOfOutputPct >= 45 ? 'vl-kpi-warn' : null));
      cards.push(kpi('50% thinking-budget cap', 'Save ' + usdText(tax.cap50SavingsWindowUsd) + ' (' + win + ')',
        'Projected ' + usdText(tax.cap50Savings30dUsd, 0) + '/30d if reasoning tokens per call were capped by 50% via ThinkingConfig.',
        null, 'vl-kpi-whatif'));
    }
    if (vr && vr.status === 'LIVE') {
      if (num(vr.realized_cache_savings_window_usd) !== null) {
        cards.push(kpi('Cache-read savings \u00b7 ' + win, usdText(vr.realized_cache_savings_window_usd),
          'Run-rate ' + usdText(vr.realized_cache_savings_30d_usd, 0) + '/30d at list price from a ' +
          pctText(num(vr.fleet_cache_hit_share_pct)) + ' cache-hit share (' + fmtTokens(vr.total_cache_read_tokens) +
          ' cached tokens). Gross: cache storage charges are not deducted.',
          null, 'vl-kpi-ok'));
      }
      if (num(vr.failure_token_tax_window_usd) !== null) {
        cards.push(kpi('Failure token tax (est.) \u00b7 ' + win, usdText(vr.failure_token_tax_window_usd),
          fmtInt(vr.agent_failed_requests) + ' server errors (5xx, ' + pctText(num(vr.agent_failure_rate_pct)) +
          ' of requests that ran); ' + fmtInt(vr.agent_rejected_requests) + ' rejected (4xx) assumed token-free. ' +
          'Cost per successful request: ' +
          (num(vr.cost_per_successful_request_usd) !== null ? usdText(vr.cost_per_successful_request_usd, 4) : DASH) + '.',
          null, num(vr.agent_failure_rate_pct) >= 5 ? 'vl-kpi-warn' : null));
      }
    }
    if (lcb && lcb.status === 'LIVE' && num(lcb.offender_count) > 0) {
      cards.push(kpi('Spend above loop threshold (est.) \u00b7 ' + win,
        num(lcb.excess_loop_cost_window_usd) !== null ? usdText(lcb.excess_loop_cost_window_usd) : fmtInt(lcb.total_excess_llm_calls) + ' calls',
        lcb.offender_count + ' runtime(s) at ' + (lcb.threshold_basis === 'PER_AGENT'
          ? 'their loop threshold (own cap from session history, else ' + lcb.threshold_calls_per_request + ' LLM calls/request; '
          : '\u2265 ' + lcb.threshold_calls_per_request + ' LLM calls/request (') +
        fmtInt(lcb.total_excess_llm_calls) + ' calls above the threshold' +
        (num(lcb.excess_loop_cost_30d_usd) !== null ? ' \u00b7 ' + usdText(lcb.excess_loop_cost_30d_usd, 0) + '/30d' : '') + ').',
        null, 'vl-kpi-bad'));
    }
    if (rep) {
      cards.push(kpi('Re-price Pro \u2192 3.8 Flash',
        rep.flashSavingsWindowUsd > 0 ? 'Save ' + usdText(rep.flashSavingsWindowUsd) + ' (' + win + ')' : '$0.00',
        rep.flashSavingsWindowUsd > 0
          ? 'Projected ' + usdText(rep.flashSavings30dUsd, 0) + '/30d at list price if higher-tier traffic ran on ' + FLASH_REPRICE_TARGET + '.'
          : 'All observed traffic is already on Flash or Flash-Lite tiers.',
        null, 'vl-kpi-whatif'));
      cards.push(kpi('Re-price to 3.1 Flash-Lite',
        rep.liteSavingsWindowUsd > 0 ? 'Save ' + usdText(rep.liteSavingsWindowUsd) + ' (' + win + ')' : '$0.00',
        rep.liteSavingsWindowUsd > 0
          ? 'Projected ' + usdText(rep.liteSavings30dUsd, 0) + '/30d at list price if eligible traffic ran on gemini-3.1-flash-lite.'
          : 'No lower-priced tier available for observed models.',
        null, 'vl-kpi-whatif'));
    }
    setKids(kpisBox, cards);

    if (taxBox) {
      if (!tax || !tax.models.length) {
        taxBox.replaceChildren();
      } else if (!tax.reasoningTokens || tax.reasoningTokens <= 0) {
        setKids(taxBox, [
          h('div', {class: 'vl-tok-card'}, [
            h('h3', {class: 'vl-tok-title'}, ['Thinking-token tax breakdown']),
            h('div', {class: 'vl-d-note', style: 'margin-top:4px'}, [
              'No reasoning ("thinking") tokens were billed across the ' + tax.models.length + ' active model(s) in this window \u2014 100% of the ' +
              fmtTokens(tax.visibleOutputTokens) + ' output tokens were visible response tokens.',
            ]),
          ]),
        ]);
      } else {
        var activeTaxModels = tax.models.filter(function(m) { return (m.reasoningTokens || 0) > 0 || (m.visibleOutputTokens || 0) > 0; });
        var topReasonModel = activeTaxModels.filter(function(m) { return (m.reasoningTokens || 0) > 0; })[0] || activeTaxModels[0];
        var snippet = 'from google.genai import types\n\n' +
          '# Cap reasoning tokens per call (measured avg: ' + intText(topReasonModel.reasoningTokensPerCall) +
          ' reasoning tok/call on ' + topReasonModel.model + ')\n' +
          'generate_content_config = types.GenerateContentConfig(\n' +
          '    thinking_config=types.ThinkingConfig(thinking_budget=' + topReasonModel.suggestedBudgetTokens + ')\n' +
          ')';
        var copySnippetBtn = h('button', {type: 'button', class: 'btn btn-xs', onclick: function() { copyText(snippet, copySnippetBtn); }}, ['Copy ADK config']);
        setKids(taxBox, [
          h('div', {class: 'vl-tok-card'}, [
            h('div', {class: 'vl-tok-head'}, [
              h('div', {}, [
                h('h3', {class: 'vl-tok-title'}, ['Thinking-token tax breakdown']),
                h('div', {class: 'vl-d-note'}, [
                  'Vertex AI bills reasoning ("thinking") tokens at the model\u2019s output token rate. Separate visible output from internal reasoning:',
                ]),
              ]),
              copySnippetBtn,
            ]),
            h('div', {class: 'table-scroll', style: 'margin-top:8px'}, [
              h('table', {class: 'vl-mini-table'}, [
                h('thead', {}, [h('tr', {}, [
                  h('th', {}, ['Model']),
                  h('th', {}, ['Visible output']),
                  h('th', {}, ['Thinking tokens']),
                  h('th', {}, ['Thinking share of output']),
                  h('th', {}, ['Thinking tok / call']),
                  h('th', {}, ['Thinking cost (' + win + ')']),
                  h('th', {}, ['50% cap savings (30d)']),
                ])]),
                h('tbody', {}, activeTaxModels.map(function(m) {
                  var pct = m.thinkingShareOfOutputPct || 0;
                  return h('tr', {}, [
                    h('td', {class: 'mono'}, [m.model]),
                    h('td', {class: 'mono'}, [fmtTokens(m.visibleOutputTokens) + ' (' + usdText(m.visibleOutputCostUsd) + ')']),
                    h('td', {class: 'mono'}, [fmtTokens(m.reasoningTokens)]),
                    h('td', {}, [
                      h('div', {class: 'vl-tax-bar-wrap'}, [
                        h('div', {class: 'vl-tax-bar'}, [
                          h('div', {class: 'vl-tax-bar-fill', style: 'width:' + Math.min(100, pct).toFixed(1) + '%'}),
                        ]),
                        h('span', {class: 'mono'}, [pctText(pct)]),
                      ]),
                    ]),
                    h('td', {class: 'mono'}, [intText(m.reasoningTokensPerCall)]),
                    h('td', {class: 'mono'}, [usdText(m.reasoningCostUsd)]),
                    h('td', {class: 'mono'}, [
                      m.cap50Savings30dUsd > 0
                        ? h('span', {class: 'badge badge-green'}, ['-' + usdText(m.cap50Savings30dUsd, 0) + '/mo'])
                        : DASH,
                    ]),
                  ]);
                })),
              ]),
            ]),
            h('pre', {class: 'vl-cmd', style: 'margin-top:10px'}, [snippet]),
          ]),
        ]);
      }
    }

    if (repBox) {
      if (!rep || (!rep.byModel.length && !rep.byAgent.length)) {
        repBox.replaceChildren();
      } else {
        var agentRows = rep.byAgent.map(function(ag) {
          var flashAlt = ag.alternatives.filter(function(al) { return al.targetModel === FLASH_REPRICE_TARGET; })[0] || null;
          var liteAlt = ag.alternatives.filter(function(al) { return al.targetModel === 'gemini-3.1-flash-lite'; })[0] || null;
          var openBtn = h('button', {type: 'button', class: 'btn btn-xs', onclick: function() {
            var target = findAgent(ag.agentId);
            if (target) openAgent(target, openBtn);
          }}, [ag.displayName]);
          return h('tr', {}, [
            h('td', {}, [openBtn, ag.engineName ? h('div', {class: 'vl-d-note', style: 'margin:2px 0 0'}, [ag.engineName]) : null]),
            h('td', {class: 'mono'}, [ag.currentModel]),
            h('td', {class: 'mono'}, [fmtTokens(ag.inputTokens) + ' / ' + fmtTokens(ag.outputTokens)]),
            h('td', {class: 'mono'}, [usdText(ag.currentCostUsd)]),
            h('td', {class: 'mono'}, [
              flashAlt
                ? h('span', {class: 'badge badge-green'}, ['-' + usdText(flashAlt.savingsWindowUsd) + ' (' + usdText(flashAlt.savings30dUsd, 0) + '/mo)'])
                : DASH,
            ]),
            h('td', {class: 'mono'}, [
              liteAlt
                ? h('span', {class: 'badge badge-blue'}, ['-' + usdText(liteAlt.savingsWindowUsd) + ' (' + usdText(liteAlt.savings30dUsd, 0) + '/mo)'])
                : DASH,
            ]),
          ]);
        });

        var modelRows = rep.byModel.map(function(m) {
          var flashAlt = m.alternatives.filter(function(al) { return al.targetModel === FLASH_REPRICE_TARGET; })[0] || null;
          var liteAlt = m.alternatives.filter(function(al) { return al.targetModel === 'gemini-3.1-flash-lite'; })[0] || null;
          return h('tr', {}, [
            h('td', {class: 'mono'}, [m.model]),
            h('td', {class: 'mono'}, [fmtInt(m.invocations)]),
            h('td', {class: 'mono'}, [usdText(m.currentCostUsd)]),
            h('td', {class: 'mono'}, [
              flashAlt
                ? h('span', {class: 'badge badge-green'}, ['-' + usdText(flashAlt.savingsWindowUsd) + ' (' + usdText(flashAlt.savings30dUsd, 0) + '/mo)'])
                : DASH,
            ]),
            h('td', {class: 'mono'}, [
              liteAlt
                ? h('span', {class: 'badge badge-blue'}, ['-' + usdText(liteAlt.savingsWindowUsd) + ' (' + usdText(liteAlt.savings30dUsd, 0) + '/mo)'])
                : DASH,
            ]),
          ]);
        });

        setKids(repBox, [
          h('div', {class: 'vl-tok-card'}, [
            h('h3', {class: 'vl-tok-title'}, ['Model re-pricing what-if (list-price arithmetic)']),
            h('div', {class: 'vl-d-note'}, [
              'Re-prices the exact observed input, cached-read, and output (including thinking) token counts against lower-cost Gemini rate cards. ' +
              'Pure price arithmetic: test answer quality and tool-calling accuracy before changing a production model.',
            ]),
            modelRows.length ? h('div', {class: 'table-scroll', style: 'margin-top:8px'}, [
              h('table', {class: 'vl-mini-table'}, [
                h('thead', {}, [h('tr', {}, [
                  h('th', {}, ['Project model (Cloud Monitoring)']),
                  h('th', {}, ['Calls']),
                  h('th', {}, ['Current est. (' + win + ')']),
                  h('th', {}, ['If ' + FLASH_REPRICE_TARGET]),
                  h('th', {}, ['If gemini-3.1-flash-lite']),
                ])]),
                h('tbody', {}, modelRows),
              ]),
            ]) : null,
            agentRows.length ? h('div', {class: 'table-scroll', style: 'margin-top:10px'}, [
              h('table', {class: 'vl-mini-table'}, [
                h('thead', {}, [h('tr', {}, [
                  h('th', {}, ['Registered agent (OTel tokens)']),
                  h('th', {}, ['Current model']),
                  h('th', {}, ['Input / Output tok']),
                  h('th', {}, ['Current est. (' + win + ')']),
                  h('th', {}, ['Savings on 3.8 Flash']),
                  h('th', {}, ['Savings on 3.1 Flash-Lite']),
                ])]),
                h('tbody', {}, agentRows),
              ]),
            ]) : null,
          ]),
        ]);
      }
    }

    // Feature 1 UI: cache-read savings & failure token tax ledger
    if (vrBox) {
      if (!vr || vr.status !== 'LIVE') {
        vrBox.replaceChildren();
      } else {
        var taxCell = function(ag) {
          var t = num(ag.failure_token_tax_usd);
          if (t === null) return DASH;  // no rate card: unknown, never a fake $0.00
          if (t <= 0) return usdText(0);
          return h('span', {class: 'badge badge-red'}, [usdText(t, 3) + ' (' + usdText(ag.failure_token_tax_30d_usd, 0) + '/30d)']);
        };
        var agentTaxRows = (vr.by_agent || []).slice(0, 8).map(function(ag) {
          return h('tr', {}, [
            h('td', {}, [ag.display_name || ag.agent_id || DASH]),
            h('td', {class: 'mono'}, [fmtInt(ag.successful_requests) + ' / ' + fmtInt(ag.failed_requests) + ' / ' +
              fmtInt(ag.rejected_requests) + (num(ag.failure_rate_pct) !== null ? ' (' + pctText(num(ag.failure_rate_pct)) + ' 5xx)' : '')]),
            h('td', {class: 'mono'}, [num(ag.productive_spend_usd) !== null ? usdText(ag.productive_spend_usd, 3) : DASH]),
            h('td', {class: 'mono'}, [taxCell(ag)]),
            h('td', {class: 'mono'}, [num(ag.cost_per_successful_request_usd) !== null ? usdText(ag.cost_per_successful_request_usd, 4) : DASH]),
          ]);
        });
        var martNote = num(vr.mart_failure_rate_pct) !== null
          ? 'Gemini Enterprise turn failure rate (' + String(vr.mart_source || 'finops_mart') +
            (num(vr.mart_window_days) !== null ? ', ' + vr.mart_window_days + ' days' : '') + '): ' +
            pctText(num(vr.mart_failure_rate_pct)) + ' (' + fmtInt(vr.mart_failed_turns) + ' of ' + fmtInt(vr.mart_turns_total) + ' turns).'
          : null;
        setKids(vrBox, [
          h('div', {class: 'vl-tok-card'}, [
            h('div', {class: 'vl-tok-head'}, [
              h('div', {}, [
                h('h3', {class: 'vl-tok-title'}, ['Cache Savings & Failure Token Tax']),
                h('div', {class: 'vl-d-note'}, [
                  'Estimated split of agent token spend into productive spend and spend on server errors (5xx); ' +
                  'rejected (4xx) requests are assumed token-free. Cache-read savings (gross, before cache storage): ' +
                  (num(vr.realized_cache_savings_window_usd) !== null ? usdText(vr.realized_cache_savings_window_usd) + ' in ' + win + ', ' + usdText(vr.realized_cache_savings_30d_usd, 0) + '/30d' : DASH) + '.',
                ]),
                martNote ? h('div', {class: 'vl-d-note'}, [martNote]) : null,
              ]),
              h('span', {class: 'badge badge-green'}, [
                'Productive: ' + (num(vr.productive_agent_spend_usd) !== null ? usdText(vr.productive_agent_spend_usd) : DASH),
              ]),
            ]),
            agentTaxRows.length ? h('div', {class: 'table-scroll', style: 'margin-top:8px'}, [
              h('table', {class: 'vl-mini-table'}, [
                h('thead', {}, [h('tr', {}, [
                  h('th', {}, ['Agent runtime']),
                  h('th', {}, ['OK / 5xx / 4xx']),
                  h('th', {}, ['Productive spend (' + win + ')']),
                  h('th', {}, ['Failure token tax (est.)']),
                  h('th', {}, ['Cost / successful req']),
                ])]),
                h('tbody', {}, agentTaxRows),
              ]),
            ]) : h('div', {class: 'vl-d-note', style: 'margin-top:6px'}, ['No per-agent request + token telemetry in this window.']),
          ]),
        ]);
      }
    }

    // Feature 2 UI: loop / fan-out audit + ADK circuit-breaker snippet
    if (lcbBox) {
      var kindLabels = {SKILL_OR_TOOL: 'Skill / tool', SUB_AGENT: 'Sub-agent', MCP_SERVER: 'MCP server'};
      var subLoops = (lcb && Array.isArray(lcb.looping_sub_agents)) ? lcb.looping_sub_agents : [];
      var subLoopNote = subLoops.length
        ? h('div', {class: 'vl-d-note', style: 'margin-top:8px'}, [
            'Sub-agents / tools at \u2265 ' + lcb.threshold_calls_per_request + ' calls per sampled trace: ' +
            subLoops.slice(0, 5).map(function(s) {
              var kName = kindLabels[s.kind] || String(s.kind || '');
              return String(s.name || DASH) + ' (' + kName + ', ' + String(s.calls_per_trace) +
                ' calls/trace over ' + fmtInt(s.traces_count) + ' traces)';
            }).join('; ') + '.',
          ])
        : null;
      // F2: per-agent caps and runaway sessions from the last 30 days of Gemini Enterprise sessions.
      var lb = lf.loop_baselines || null;
      var lbAgents = (lb && lb.status === 'LIVE' && Array.isArray(lb.agents)) ? lb.agents : [];
      var runaways = [];
      lbAgents.forEach(function(b) {
        (b.runaways || []).forEach(function(rw) { runaways.push({agent: b, rw: rw}); });
      });
      runaways.sort(function(x, y) { return (num(y.rw.calls_per_run) || 0) - (num(x.rw.calls_per_run) || 0); });
      var baseNode = lbAgents.length ? h('div', {style: 'margin-top:12px'}, [
        h('div', {class: 'vl-d-note'}, ['Per-agent loop caps from the last 30 days of sessions: cap = max(10, 1.5 \u00d7 p99 of ' +
          'normal runs, largest normal run). A runaway session has a run above the agent\u2019s outlier fence.']),
        h('div', {class: 'table-scroll', style: 'margin-top:6px'}, [
          h('table', {class: 'vl-mini-table'}, [
            h('thead', {}, [h('tr', {}, [
              h('th', {}, ['Agent']), h('th', {}, ['Runs']), h('th', {}, ['LLM calls per run (p50 / p99)']),
              h('th', {}, ['Recommended cap']), h('th', {}, ['Runaway sessions']),
            ])]),
            h('tbody', {}, lbAgents.map(function(b) {
              return h('tr', {}, [
                h('td', {}, [String(b.agent_name || DASH)]),
                h('td', {class: 'mono', title: b.basis === 'TURN' ? 'Turns' : 'Sessions (average calls per turn; no turn detail)'},
                  [fmtInt(b.samples) + (b.basis === 'TURN' ? ' turns' : ' sessions')]),
                h('td', {class: 'mono'}, [String(b.p50) + ' / ' + String(b.p99_within_fence)]),
                h('td', {class: 'mono'}, [fmtInt(b.recommended_cap)]),
                h('td', {class: 'mono'}, [num(b.runaway_sessions) > 0
                  ? h('span', {class: 'badge badge-yellow'}, [fmtInt(b.runaway_sessions) + ' (' + pctText(num(b.runaway_session_share_pct)) + ')'])
                  : '0']),
              ]);
            })),
          ]),
        ]),
        runaways.length ? h('div', {class: 'table-scroll', style: 'margin-top:8px'}, [
          h('table', {class: 'vl-mini-table'}, [
            h('thead', {}, [h('tr', {}, [
              h('th', {}, ['Runaway session']), h('th', {}, ['Agent']), h('th', {}, ['User']),
              h('th', {}, ['LLM calls in one turn']), h('th', {}, ['Cap']),
            ])]),
            h('tbody', {}, runaways.slice(0, 8).map(function(x) {
              return h('tr', {}, [
                h('td', {class: 'mono'}, [String(x.rw.session_id || DASH)]),
                h('td', {}, [String(x.agent.agent_name || DASH)]),
                h('td', {}, [String(x.rw.user_email || DASH)]),
                h('td', {class: 'mono'}, [h('span', {class: 'badge badge-red'}, [String(x.rw.calls_per_run) +
                  (x.rw.run_basis === 'SESSION_AVERAGE' ? ' avg' : '')])]),
                h('td', {class: 'mono'}, [fmtInt(x.agent.recommended_cap)]),
              ]);
            })),
          ]),
        ]) : null,
        (lb.insufficient || []).length ? h('div', {class: 'vl-d-note', style: 'margin-top:6px'}, [
          'Not enough history for a cap (fewer than ' + fmtInt(lb.min_samples) + ' runs; the default ' +
          (lcb ? lcb.threshold_calls_per_request : 20) + ' calls/request rule applies): ' +
          lb.insufficient.slice(0, 6).map(function(b) { return String(b.agent_name); }).join(', ') + '.']) : null,
      ]) : null;
      if (!lcb || lcb.status !== 'LIVE') {
        lcbBox.replaceChildren();
      } else if (!lcb.offenders || !lcb.offenders.length) {
        setKids(lcbBox, [
          h('div', {class: 'vl-tok-card'}, [
            h('div', {class: 'vl-tok-head'}, [
              h('h3', {class: 'vl-tok-title'}, ['Agent Loop & Fan-Out Audit']),
              h('span', {class: 'badge badge-green'}, ['No runtime at its loop threshold']),
            ]),
            h('div', {class: 'vl-d-note', style: 'margin-top:4px'}, [
              lcb.threshold_basis === 'PER_AGENT'
                ? 'No agent runtime averages its own cap (or ' + lcb.threshold_calls_per_request +
                  ' LLM calls per request without session history) in this window.'
                : 'No agent runtime averages \u2265 ' + lcb.threshold_calls_per_request +
                  ' LLM calls per request in this window (same rule as the health card).',
            ]),
            baseNode,
            subLoopNote,
          ]),
        ]);
      } else {
        var topLoop = lcb.offenders[0];
        var loopSnippet = String(topLoop.adk_circuit_breaker_snippet || '');
        var copyLoopBtn = h('button', {type: 'button', class: 'btn btn-xs', onclick: function() { copyText(loopSnippet, copyLoopBtn); }}, ['Copy ADK RunConfig']);
        setKids(lcbBox, [
          h('div', {class: 'vl-tok-card'}, [
            h('div', {class: 'vl-tok-head'}, [
              h('div', {}, [
                h('h3', {class: 'vl-tok-title'}, ['Agent Loop & Fan-Out Audit']),
                h('div', {class: 'vl-d-note'}, [
                  'Runtimes whose average LLM calls per request reach their threshold (their own cap from 30 days of sessions, ' +
                  'else ' + lcb.threshold_calls_per_request + '), and the estimated token spend above it. A cap makes runs that ' +
                  'exceed it fail, so prefer a cap derived from the agent\u2019s own history.',
                ]),
              ]),
              copyLoopBtn,
            ]),
            h('div', {class: 'table-scroll', style: 'margin-top:8px'}, [
              h('table', {class: 'vl-mini-table'}, [
                h('thead', {}, [h('tr', {}, [
                  h('th', {}, ['Agent']),
                  h('th', {}, ['Calls / req']),
                  h('th', {}, ['Threshold']),
                  h('th', {}, ['Calls above threshold']),
                  h('th', {}, ['Tokens above threshold']),
                  h('th', {}, ['Spend above threshold (' + win + ' / 30d, est.)']),
                ])]),
                h('tbody', {}, lcb.offenders.map(function(o) {
                  return h('tr', {}, [
                    h('td', {}, [o.display_name]),
                    h('td', {class: 'mono'}, [h('span', {class: 'badge badge-red'}, [String(o.llm_calls_per_request) + 'x'])]),
                    h('td', {class: 'mono', title: o.threshold_basis === 'SESSION_P99'
                      ? 'This agent\u2019s cap from its last 30 days of sessions' : 'Default rule (not enough session history)'},
                      [String(o.threshold_calls_per_request) + (o.threshold_basis === 'SESSION_P99' ? ' (own cap)' : ' (default)')]),
                    h('td', {class: 'mono'}, [fmtInt(o.excess_llm_calls)]),
                    h('td', {class: 'mono'}, [fmtTokens(o.excess_tokens)]),
                    h('td', {class: 'mono'}, [
                      num(o.excess_loop_cost_window_usd) !== null
                        ? usdText(o.excess_loop_cost_window_usd) + ' (' + usdText(o.excess_loop_cost_30d_usd, 0) + '/30d)'
                        : DASH,
                    ]),
                  ]);
                })),
              ]),
            ]),
            loopSnippet ? h('details', {style: 'margin-top:10px'}, [
              h('summary', {}, ['Show ADK RunConfig for ' + String(topLoop.display_name || 'the top offender')]),
              h('pre', {class: 'vl-cmd', style: 'margin-top:6px'}, [loopSnippet]),
            ]) : null,
            baseNode,
            subLoopNote,
          ]),
        ]);
      }
    }

    // Feature 4 UI: pay-as-you-go vs Provisioned Throughput (GSU) break-even & 30/60/90-day projection
    if (gsuBox) {
      if (!gsu || gsu.status !== 'PROJECTION' || !Array.isArray(gsu.by_model) || !gsu.by_model.length) {
        gsuBox.replaceChildren();
      } else {
        var gsuRef = gsu.gsu_reference || {};
        var recBadge = {COMMIT_GSU_1YR: 'badge-yellow', STAY_ON_PAYG: 'badge-green'};
        var recLabel = {
          COMMIT_GSU_1YR: 'Commit 1-yr GSU',
          STAY_ON_PAYG: 'Stay on pay-as-you-go',
          NO_PT_REFERENCE: 'Pay-as-you-go only',
          UNPRICED_MODEL: 'No rate card',
        };
        var hasProfileSizing = gsu.sizing_basis === 'PER_MINUTE_PROFILE';
        setKids(gsuBox, [
          h('div', {class: 'vl-tok-card'}, [
            h('div', {class: 'vl-tok-head'}, [
              h('div', {}, [
                h('h3', {class: 'vl-tok-title'}, ['Pay-as-you-go vs. Provisioned Throughput & 30/60/90-Day Projection']),
                h('div', {class: 'vl-d-note'}, [
                  'Linear projection, not a forecast: 30d ' + usdText(num(gsu.forecast_30d_usd), 0) +
                  ' \u00b7 60d ' + usdText(num(gsu.forecast_60d_usd), 0) +
                  ' \u00b7 90d ' + usdText(num(gsu.forecast_90d_usd), 0) +
                  ' (' + String(gsu.burn_rate_basis || '') + ').',
                ]),
                h('div', {class: 'vl-d-note'}, [String(gsu.sizing_caveat || '')]),
              ]),
              h('span', {class: 'badge ' + (num(gsu.gsu_candidates_count) > 0 ? 'badge-yellow' : 'badge-blue')}, [
                num(gsu.gsu_candidates_count) > 0 ? gsu.gsu_candidates_count + ' model(s) past 1-yr break-even' : 'Pay-as-you-go is cheaper',
              ]),
            ]),
            h('div', {class: 'table-scroll', style: 'margin-top:8px'}, [
              h('table', {class: 'vl-mini-table'}, [
                h('thead', {}, [h('tr', {}, [
                  h('th', {}, ['Model']),
                  h('th', {}, [hasProfileSizing ? 'GSU load (avg / p99 / peak)' : 'Avg GSU load']),
                  h('th', {}, ['Pay-as-you-go 30d']),
                  h('th', {}, ['1-yr GSU (' + usdText(num(gsuRef.gsu_1yr_usd), 0) + '/GSU-month)']),
                  h('th', {}, ['Break-even']),
                  h('th', {}, ['Recommendation']),
                ])]),
                h('tbody', {}, gsu.by_model.map(function(m) {
                  var loadCell = DASH;
                  if (num(m.est_single_gsu_utilization_pct) !== null) {
                    loadCell = m.sizing_basis === 'PER_MINUTE_PROFILE' && num(m.p99_gsu_units) !== null
                      ? pctText(num(m.est_single_gsu_utilization_pct)) + ' avg \u00b7 ' + m.p99_gsu_units + ' GSU p99 (' + m.peak_gsu_units + ' peak)'
                      : pctText(num(m.est_single_gsu_utilization_pct)) + ' of 1 GSU';
                  }
                  var gsuCostCell = DASH;
                  if (num(m.gsu_1yr_commitment_usd) !== null) {
                    var spillUsd = num(m.spillover_payg_30d_usd);
                    gsuCostCell = usdText(num(m.gsu_1yr_commitment_usd), 0) + ' (' + m.recommended_gsu_units + ' GSU' +
                      (spillUsd !== null && spillUsd >= 1 ? ' + ' + usdText(spillUsd, 0) + ' spillover' : '') + ')';
                  }
                  return h('tr', {}, [
                    h('td', {class: 'mono'}, [m.model]),
                    h('td', {class: 'mono'}, [loadCell]),
                    h('td', {class: 'mono'}, [usdText(num(m.payg_30d_run_rate_usd), 0)]),
                    h('td', {class: 'mono'}, [gsuCostCell]),
                    h('td', {class: 'mono'}, [pctText(num(m.break_even_progress_pct))]),
                    h('td', {title: m.recommendation_note || ''}, [
                      h('span', {class: 'badge vl-nowrap ' + (recBadge[m.recommendation] || 'badge-blue')}, [
                        recLabel[m.recommendation] || m.recommendation,
                      ]),
                    ]),
                  ]);
                })),
              ]),
            ]),
            h('div', {class: 'vl-d-note', style: 'margin-top:6px'}, [
              'Sources (checked ' + String(gsuRef.checked || DASH) + '): ' + String(gsuRef.price_source || DASH) + '; ' +
              String(gsuRef.throughput_source || DASH) + '.',
            ]),
          ]),
        ]);
      }
    }

    // Feature 5 UI: model retirement & right-sizing radar
    if (mmrBox) {
      if (!mmr || mmr.status !== 'LIVE' || !Array.isArray(mmr.findings) || !mmr.findings.length) {
        mmrBox.replaceChildren();
      } else {
        var migSnippet = String(mmr.findings[0].migration_snippet || '');
        var copyMigBtn = migSnippet
          ? h('button', {type: 'button', class: 'btn btn-xs', onclick: function() { copyText(migSnippet, copyMigBtn); }}, ['Copy top migration'])
          : null;
        var headParts = [];
        var retChange = num(mmr.retirement_cost_change_30d_usd);
        if (num(mmr.retiring_model_count) > 0) {
          headParts.push(mmr.retiring_model_count + ' retired or retiring model(s)' + (retChange !== null
            ? ' (same tokens on the replacements: ' + (retChange > 0 ? '+' : '') + usdText(retChange, 0) + '/30d at list price)' : ''));
        }
        if (num(mmr.project_model_savings_30d_usd) > 0) {
          headParts.push('right-sizing candidates worth ' + usdText(num(mmr.project_model_savings_30d_usd), 0) + '/30d at list price (eval first)');
        }
        setKids(mmrBox, [
          h('div', {class: 'vl-tok-card'}, [
            h('div', {class: 'vl-tok-head'}, [
              h('div', {}, [
                h('h3', {class: 'vl-tok-title'}, ['Model Retirement & Right-Sizing Radar']),
                h('div', {class: 'vl-d-note'}, [
                  (headParts.length ? headParts.join('; ') : mmr.finding_count + ' finding(s)') +
                  '. Lifecycle as of ' + String(mmr.as_of || DASH) + ' (' + String(mmr.lifecycle_source || DASH) + ').',
                ]),
              ]),
              copyMigBtn,
            ]),
            h('div', {class: 'table-scroll', style: 'margin-top:8px'}, [
              h('table', {class: 'vl-mini-table'}, [
                h('thead', {}, [h('tr', {}, [
                  h('th', {}, ['Target']),
                  h('th', {}, ['Current \u2192 Recommended']),
                  h('th', {}, ['Deadline / gate']),
                  h('th', {}, ['List-price change (30d)']),
                  h('th', {}, ['Why']),
                ])]),
                h('tbody', {}, mmr.findings.slice(0, 8).map(function(f) {
                  var chg = num(f.cost_change_30d_usd);
                  var retired = num(f.days_until_retirement) !== null && f.days_until_retirement <= 0;
                  var chgNode = DASH;
                  if (chg !== null) {
                    if (chg > 0) {
                      chgNode = h('span', {class: 'badge badge-yellow vl-nowrap', title: 'Replacement list price is higher at the same token volume'}, ['+' + usdText(chg, 0)]);
                    } else if (chg < 0) {
                      chgNode = h('span', {class: 'badge badge-green vl-nowrap', title: 'Lower list price at the same token volume'}, ['-' + usdText(Math.abs(chg), 0)]);
                    } else {
                      chgNode = '$0';
                    }
                  }
                  return h('tr', {}, [
                    h('td', {}, [f.subject]),
                    h('td', {class: 'mono'}, [f.current_model + ' \u2192 ' + (f.recommended_model || DASH)]),
                    h('td', {class: 'mono'}, [f.retirement_date
                      ? h('span', {class: 'badge badge-red vl-nowrap'}, [(retired ? 'Retired ' : 'Retires ') + f.retirement_date])
                      : h('span', {class: 'badge badge-yellow vl-nowrap'}, ['Eval first'])]),
                    h('td', {class: 'mono'}, [chgNode]),
                    h('td', {style: 'font-size:11.5px;color:var(--text-secondary)'}, [f.rationale]),
                  ]);
                })),
              ]),
            ]),
          ]),
        ]);
      }
    }
  }

  function renderSpend(st) {
    var panel = byId('vlSpendPanel');
    if (!panel || !st) return;
    var gdu = st.ge_daily_usage || null;
    var days = gdu && Array.isArray(gdu.days) ? gdu.days : [];
    var billingStatus = gdu ? String(gdu.billing_status || 'NOT_CONFIGURED') : null;
    var br = st.billing_reconciliation || null;
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
      if (br && br.status === 'LIVE' && num(br.effective_savings_rate_pct) !== null) {
        // Credits are negative in the export; savings read better as positive amounts.
        var saved = function(v) { var n = num(v); return n === null ? DASH : usdText(Math.abs(n)); };
        var credSub;
        if (br.esr_basis === 'LIST_PRICE') {
          credSub = 'Vs list price ' + usdText(num(br.total_list_usd)) + ': discounts built into prices ' +
            usdText(num(br.price_discount_usd)) + ' + commitments/SUD ' + saved(br.total_cud_credits_usd) +
            ' + contract ' + saved(br.total_contract_discount_credits_usd) + '. Promotions excluded.' +
            (num(br.credit_esr_pct) !== null ? ' Credits alone: ' + pctText(num(br.credit_esr_pct)) + '.' : '');
        } else {
          credSub = 'Credits only: commitments/SUD ' + saved(br.total_cud_credits_usd) + ' + contract ' +
            saved(br.total_contract_discount_credits_usd) + ' on gross ' + usdText(num(br.total_gross_usd)) +
            '. Promotions excluded. Lower bound: ' + (br.list_price_column === 'absent'
              ? 'this export has no list prices (cost_at_list).'
              : 'discounts built into prices are not counted.');
        }
        billedCards.push(kpi('Effective Savings Rate (ESR)', pctText(num(br.effective_savings_rate_pct)), credSub,
          null, num(br.effective_savings_rate_pct) > 0 ? 'vl-kpi-ok' : null));
      }
      billedCards.push(budgetCard(cs));
      billedCards.push(kpi('Cost per 1k turns \u00b7 7 days', cs.per1kTurns7d !== null ? usdText(cs.per1kTurns7d) : DASH,
        'Billed AI spend \u00f7 Gemini Enterprise turns (finops_mart), last ' + cs.avg7Days + ' billed days.'));
      if (badgeEl) { badgeEl.textContent = 'Billing export: connected'; badgeEl.className = 'badge badge-green'; }
    } else {
      var words = {LIVE: 'no billed days yet', LOADING: 'loading\u2026', ERROR: 'error', NOT_CONFIGURED: 'not connected', NOT_CONNECTED: 'not connected'};
      var why = gdu ? 'Billing export ' + (words[billingStatus] || billingStatus.toLowerCase()) + '. See "Cloud Billing export" below to connect it.'
                    : 'Billed spend, month to date and run-rate need the Cloud Billing export and the finops_mart on a live project.';
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

    renderBudgetRunway(cs);
    renderDriftWaterfall();
    renderTokenomics(fl);
    renderGovernanceScore(st, fl);

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
        setKids(chartBox, [h('div', {class: 'chart-empty'}, [gdu ? 'No daily rows in finops_mart yet.'
          : 'Daily spend and usage appear here on a live project (Cloud Billing export + finops_mart).'])]);
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
        'finops_mart daily turns \u00b7 Vertex AI model usage \u00d7 list price for the selected time range \u00b7 ' +
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
    var input = h('input', {type: 'number', min: '0', step: '100', value: cur ? String(cur) : '', placeholder: 'e.g. 2000', 'aria-label': 'Monthly AI budget in USD'});
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
  // Part 8: Users adoption, conversation receipt, turn timeline, and user detail drawer
  // ===================================================================================================
  function currentRateCards() {
    var fl = rawFleet();
    return fl && fl.model_usage ? fl.model_usage.rate_cards : null;
  }

  function fmtReceiptUsd(v) {
    if (v === null || v === undefined || !isFinite(v)) return DASH;
    var abs = Math.abs(v);
    var d = abs > 0 && abs < 0.01 ? 4 : 3;
    return (v < 0 ? '-$' : '$') + abs.toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
  }

  function sessionReceiptNode(s, turns) {
    var r = vlSessionReceipt(s, turns, currentRateCards());
    if (!r || !r.hasTokens) return null;

    var lineItems = [
      h('div', {class: 'vl-receipt-item'}, [
        h('span', {class: 'vl-receipt-lbl'}, ['Uncached input (' + fmtTokens(r.uncachedInputTokens) + ' @ $' + r.rateCard.input + '/1M)']),
        h('span', {class: 'vl-receipt-val mono'}, [fmtReceiptUsd(r.uncachedInputUsd)]),
      ]),
      h('div', {class: 'vl-receipt-item'}, [
        h('span', {class: 'vl-receipt-lbl'}, [
          'Cached input (' + fmtTokens(r.cachedInputTokens) +
          (r.cacheHitPct !== null ? ', ' + pctText(r.cacheHitPct) + ' hit' : '') +
          ' @ $' + r.rateCard.cached_read + '/1M)',
        ]),
        h('span', {class: 'vl-receipt-val mono'}, [fmtReceiptUsd(r.cachedInputUsd)]),
      ]),
      r.cacheSavedUsd > 0 ? h('div', {class: 'vl-receipt-item vl-receipt-discount'}, [
        h('span', {class: 'vl-receipt-lbl'}, ['Context cache discount vs uncached list price']),
        h('span', {class: 'vl-receipt-val mono'}, ['-' + fmtReceiptUsd(r.cacheSavedUsd) + ' saved']),
      ]) : null,
      h('div', {class: 'vl-receipt-item'}, [
        h('span', {class: 'vl-receipt-lbl'}, ['Visible output (' + fmtTokens(r.visibleOutputTokens) + ' @ $' + r.rateCard.output + '/1M)']),
        h('span', {class: 'vl-receipt-val mono'}, [fmtReceiptUsd(r.visibleOutputUsd)]),
      ]),
      h('div', {class: 'vl-receipt-item'}, [
        h('span', {class: 'vl-receipt-lbl'}, [
          'Thinking / reasoning (' + fmtTokens(r.reasoningTokens) +
          (r.thinkingSharePct !== null ? ', ' + pctText(r.thinkingSharePct) + ' of output' : '') +
          ' @ $' + r.rateCard.output + '/1M)',
        ]),
        h('span', {class: 'vl-receipt-val mono'}, [fmtReceiptUsd(r.reasoningUsd)]),
      ]),
      h('div', {class: 'vl-receipt-item vl-receipt-total'}, [
        h('span', {class: 'vl-receipt-lbl'}, ['Session estimated total (' + r.model + ' list price)']),
        h('span', {class: 'vl-receipt-val mono'}, [fmtReceiptUsd(r.totalCostUsd)]),
      ]),
    ];

    var timelineRows = r.turns.map(function(ti) {
      var tot = ti.totalTokens || 1;
      var wPct = Math.max(6, Math.min(100, 100 * tot / (r.maxTurnTokens || 1)));
      var uP = tot > 0 ? 100 * (ti.uncachedInputTokens || 0) / tot : 0;
      var cP = tot > 0 ? 100 * (ti.cachedInputTokens || 0) / tot : 0;
      var vP = tot > 0 ? 100 * (ti.visibleOutputTokens || 0) / tot : 0;
      var rP = Math.max(0, 100 - uP - cP - vP);
      return h('div', {class: 'vl-tl-row'}, [
        h('span', {class: 'vl-tl-idx mono'}, ['#' + ti.index]),
        h('span', {class: 'vl-tl-meta mono'}, [
          (ti.ts ? String(ti.ts).slice(11, 19) : DASH) + ' \u00b7 ' + ti.model,
        ]),
        h('div', {class: 'vl-tl-bar-track', title: 'Uncached in: ' + fmtInt(ti.uncachedInputTokens) +
          ' \u00b7 Cached in: ' + fmtInt(ti.cachedInputTokens) +
          ' \u00b7 Visible out: ' + fmtInt(ti.visibleOutputTokens) +
          ' \u00b7 Thinking: ' + fmtInt(ti.reasoningTokens)}, [
          h('div', {class: 'vl-tl-bar', style: 'width:' + wPct.toFixed(1) + '%'}, [
            h('span', {class: 'vl-tl-seg vl-tl-u', style: 'width:' + uP.toFixed(1) + '%'}),
            h('span', {class: 'vl-tl-seg vl-tl-c', style: 'width:' + cP.toFixed(1) + '%'}),
            h('span', {class: 'vl-tl-seg vl-tl-v', style: 'width:' + vP.toFixed(1) + '%'}),
            h('span', {class: 'vl-tl-seg vl-tl-r', style: 'width:' + rP.toFixed(1) + '%'}),
          ]),
        ]),
        h('span', {class: 'vl-tl-tools mono'}, [
          ti.toolNames ? (ti.toolCalls || 1) + '\u00d7 ' + ti.toolNames : (ti.toolCalls ? ti.toolCalls + ' tool(s)' : 'no tools'),
        ]),
        h('span', {class: 'vl-tl-cost mono'}, [fmtReceiptUsd(ti.costUsd)]),
      ]);
    });

    return h('div', {class: 'vl-receipt-box'}, [
      h('div', {class: 'vl-receipt-col'}, [
        h('div', {class: 'vl-receipt-title'}, ['Conversation FinOps receipt \u00b7 ' + (r.userLdap || 'user') + ' \u00b7 ' + r.agentName]),
        h('div', {class: 'vl-receipt-list'}, lineItems),
      ]),
      timelineRows.length ? h('div', {class: 'vl-timeline-col'}, [
        h('div', {class: 'vl-receipt-title'}, ['Turn-by-turn token & tool timeline (' + timelineRows.length + ' turns)']),
        h('div', {class: 'vl-tl-legend'}, [
          h('span', {}, [h('i', {class: 'vl-tl-dot vl-tl-u'}), 'Uncached in']),
          h('span', {}, [h('i', {class: 'vl-tl-dot vl-tl-c'}), 'Cached in']),
          h('span', {}, [h('i', {class: 'vl-tl-dot vl-tl-v'}), 'Visible out']),
          h('span', {}, [h('i', {class: 'vl-tl-dot vl-tl-r'}), 'Thinking']),
        ]),
        h('div', {class: 'vl-tl-list'}, timelineRows),
      ]) : null,
    ]);
  }

  function userRowDecorate(tr, u) {
    if (!tr || !u || !tr.cells || !tr.cells.length) return;
    var firstTd = tr.cells[0];
    var btn = h('button', {
      type: 'button',
      class: 'btn btn-xs vl-user-inspect-btn',
      title: 'Open itemized FinOps profile and session receipts for ' + (u.user_ldap || vlUserLabel(u.user_email)),
      onclick: function(ev) {
        ev.stopPropagation();
        openUser(u, btn);
      },
    }, ['Profile \u203a']);
    firstTd.appendChild(document.createTextNode(' '));
    firstTd.appendChild(btn);
  }

  function openUser(u, opener) {
    if (!u) return;
    var st = state() || {};
    var uc = st.user_centric || {};
    var summary = vlUserSummary(u, uc.ge_sessions || [], uc.ge_session_turns || {}, currentRateCards());
    if (!summary) return;
    closeAgent(true);
    var drawer = byId('vlDrawer');
    var back = byId('vlDrawerBackdrop');
    if (!drawer) return;

    var closeBtn = h('button', {type: 'button', class: 'btn btn-xs vl-d-close', 'aria-label': 'Close user profile', onclick: function() {
      closeAgent();
    }}, ['Close \u2715']);

    var filterBtn = h('button', {type: 'button', class: 'btn btn-xs btn-primary', onclick: function() {
      closeAgent();
      try {
        switchTab(4);
        if (typeof setGeSessionUserFilter === 'function') setGeSessionUserFilter(u.user_email || summary.ldap);
      } catch (e) { /* ignore */ }
    }}, ['Filter sessions table to ' + summary.ldap]);

    var agentList = Object.keys(summary.byAgent).map(function(k) { return k + ' (' + summary.byAgent[k] + ')'; }).join(', ') || DASH;
    var modelList = Object.keys(summary.byModel).map(function(k) { return k + ' (' + summary.byModel[k] + ')'; }).join(', ') || DASH;

    var wrap = h('div', {class: 'vl-agent-detail vl-mode-drawer'}, [
      h('div', {class: 'vl-d-head'}, [
        h('div', {class: 'vl-d-titlewrap'}, [
          h('h2', {class: 'vl-d-title', id: 'vlDrawerTitle'}, ['User FinOps profile \u00b7 ' + summary.ldap]),
          h('div', {class: 'vl-d-sub'}, [[summary.department, 'Primary agent: ' + summary.primaryAgent].filter(Boolean).join(' \u00b7 ')]),
        ]),
        h('div', {class: 'vl-d-actions'}, [filterBtn, closeBtn]),
      ]),
      section('30-day usage & FinOps summary (finops_mart)', [
        h('div', {class: 'vl-minis'}, [
          miniKpi('Sessions (30d)', fmtInt(summary.sessions30d), 'finops_mart.fct_sessions'),
          miniKpi('Turns (30d)', fmtInt(summary.turns30d), summary.sessions30d ? (summary.turns30d / summary.sessions30d).toFixed(1) + ' turns/session' : null),
          miniKpi('Tokens (30d)', fmtTokens(summary.tokens30d), summary.cacheHitPct !== null ? pctText(summary.cacheHitPct) + ' cache hit' : null),
          miniKpi('Est. 30d spend', summary.estCost30d !== null ? usdText(summary.estCost30d) : DASH, 'List-price estimate'),
          miniKpi('Recent cache savings', fmtReceiptUsd(summary.recentCacheSavedUsd), 'Across ' + summary.recentSessionsCount + ' recent session(s)'),
          miniKpi('Recent thinking tax', fmtReceiptUsd(summary.recentReasoningUsd), 'Reasoning tokens @ output rate'),
        ]),
      ]),
      section('Agent & model mix in recent sessions', [
        kv([
          ['Agents used', agentList],
          ['Models used', modelList, true],
          ['Avg CSAT', summary.avgCsat !== null ? summary.avgCsat + ' \u2605' : 'No ratings'],
        ]),
      ]),
      section('Recent session receipts (' + summary.sessionReceipts.length + ')',
        summary.sessionReceipts.length
          ? summary.sessionReceipts.slice(0, 5).map(function(sr) {
              return h('div', {style: 'margin-bottom:12px'}, [
                h('div', {class: 'vl-d-note', style: 'margin-bottom:4px;font-weight:600'}, [
                  'Session ' + (sr.session.session_id || DASH) + ' \u00b7 ' + (sr.session.agent_name || DASH) +
                  ' \u00b7 ' + fmtInt(sr.session.turns) + ' turns',
                ]),
                sessionReceiptNode(sr.session, (uc.ge_session_turns || {})[sr.session.session_key || ((sr.session.engine_key || '') + '|' + (sr.session.session_id || ''))] || []),
              ]);
            })
          : [h('div', {class: 'vl-d-note'}, ['No recent sessions in the current scope for this user.'])]
      ),
    ]);

    openState = {id: 'user:' + summary.ldap, mode: 'drawer', opener: opener || document.activeElement};
    drawer.replaceChildren(wrap);
    drawer.classList.remove('hidden');
    if (back) back.classList.remove('hidden');
    document.body.classList.add('vl-drawer-open');
    closeBtn.focus();
    resized();
  }

  function renderAdoption(st) {
    var panel = byId('vlAdoptionPanel');
    if (!panel || !st) return;
    var days = st.ge_daily_usage && Array.isArray(st.ge_daily_usage.days) ? st.ge_daily_usage.days : [];
    var ad = vlAdoption(days, new Date().toISOString());
    var grid = byId('vlAdoptionKpis');
    var chartBox = byId('vlAdoptionChart');
    if (!ad) {
      if (grid) grid.replaceChildren();
      if (chartBox) chartBox.replaceChildren(h('div', {class: 'chart-empty'}, ['No daily rows in finops_mart yet.']));
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
      src.textContent = 'Source: ' + ((st.ge_daily_usage && st.ge_daily_usage.mart_dataset) || 'finops_mart') +
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
    if (openState && String(openState.id || '').indexOf('user:') !== 0) p.set('agent', openState.id);
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
      if (ev.key === 'Escape') {
        var inlineDrawers = [
          ['nl2sqlCopilotDrawer', 'toggleNl2SqlBtn'],
          ['telemetryValidatorDrawer', 'toggleTelemetryValidatorBtn'],
          ['agentObsDrawer', 'toggleObsScriptBtn']
        ];
        for (var dIdx = 0; dIdx < inlineDrawers.length; dIdx++) {
          var drw = byId(inlineDrawers[dIdx][0]);
          if (drw && !drw.classList.contains('hidden')) {
            drw.classList.add('hidden');
            var tBtn = byId(inlineDrawers[dIdx][1]);
            if (tBtn) tBtn.setAttribute('aria-expanded', 'false');
            return;
          }
        }
      }
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
  var THEME_KEY = 'finops.theme';
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
      // The button title (e.g. "... seats, WTU allowance & PAYG") makes a tab findable by what it covers.
      items.push({kind: 'Go to', group: 'Go to', title: label, keywords: ['tab ' + label, 'go ' + label].concat(btn.title ? [btn.title] : []), boost: 40,
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
                  keywords: [u.department, u.primary_agent, 'receipt', 'profile'], hideWhenEmpty: true,
                  run: function() { openUser(u); }});
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
      byId('vlSpendPanel') ? {title: 'Set the monthly AI budget', keywords: ['budget', 'cost', 'spend', 'runway'],
                              run: function() { try { switchTab(3); } catch (e) { return; } toggleBudgetForm(true); revealTable('vlSpendKpis'); }} : null,
      byId('vlTokenomicsPanel') ? {title: 'Inspect Tokenomics & thinking-token tax', keywords: ['tokenomics', 'thinking', 'repricing', 'flash', 'cost'],
                                   run: function() { try { switchTab(3); } catch (e) { return; } var p = byId('vlTokenomicsPanel'); if (p && p.scrollIntoView) p.scrollIntoView({block: 'start'}); }} : null,
      byId('vlGovScorePanel') ? {title: 'Inspect Enterprise AI Governance Score (0\u2013100)', keywords: ['governance', 'score', 'scorecard', 'compliance', 'pillars', 'tokenomics', 'zombie'],
                                 run: function() { toggleGovernanceScorecard(true); }} : null,
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
  function renderLogScanCapNotice(fleet) {
    var meta = byId('fleetMeta');
    if (!meta || !meta.parentNode) return;
    var existing = byId('vlLogCapBanner');
    var scan = fleet && fleet.token_log_scan;
    if (!scan || !scan.truncated) {
      if (existing) existing.remove();
      return;
    }
    var n = num(scan.entries_scanned);
    var capLabel = n !== null ? fmtInt(n) + '-entry' : 'configured';
    var node = h('div', {id: 'vlLogCapBanner', class: 'vl-log-cap-banner', role: 'status'}, [
      h('span', {class: 'badge badge-yellow'}, ['Sampled logs']),
      h('span', {}, [
        'Live Cloud Logging token scan reached the ' + capLabel + ' safety cap (FINOPS_TOKEN_LOG_MAX_ENTRIES). ' +
        'Per-agent token totals for this window are lower bounds; deploy deploy/setup_bigquery_sink.sh (finops_mart) for unsampled token & session rollups.',
      ]),
    ]);
    if (existing) existing.replaceWith(node);
    else meta.parentNode.insertBefore(node, meta.nextSibling);
  }

  // ---- Structured Error Classification (Code Bug vs Validation vs Live Guardrail vs Network) --------
  function vlClassifyError(where, httpStatus, payload, jsError) {
    var loc = String(where || '/api');
    var status = typeof httpStatus === 'number' && isFinite(httpStatus) ? httpStatus : 0;
    var p = payload && typeof payload === 'object' ? payload : {};
    var pStatus = String(p.status || '');
    var pKind = String(p.error_kind || '');

    // 1. Live-mode safety guardrail (HTTP 200 with status: DISABLED_IN_LIVE_MODE)
    if (pStatus === 'DISABLED_IN_LIVE_MODE' || pKind === 'DISABLED_IN_LIVE_MODE') {
      var liveMsg = String(p.reason || p.user_message || 'Synthetic simulator actions are disabled when connected to a live Google Cloud project so real telemetry is never overwritten.');
      var liveTodo = 'Use Live What-If or Tokenomics & Cockpit FinOps to model changes against live telemetry.';
      return {
        kind: 'DISABLED_IN_LIVE_MODE',
        code: 'VL-LIVE-GUARD',
        isCodeBug: false,
        statePreserved: true,
        where: loc,
        requestId: null,
        exceptionType: null,
        headline: 'Simulator Action Disabled in Live Mode (VL-LIVE-GUARD)',
        userMessage: liveMsg,
        whatToDo: liveTodo,
        diagnosticCmd: null,
        bannerText: '[VL-LIVE-GUARD] ' + liveMsg + ' — ' + liveTodo,
      };
    }

    // A rejected fetch() (offline, DNS, CORS, aborted) arrives here as a bare JS error with no HTTP status.
    // That is a network failure, not a FinOps Dashboard code bug: route it to the network branch below.
    var jsText = jsError ? String((jsError.name || '') + ' ' + (jsError.message || '')) : '';
    var looksLikeFetchFailure = /failed to fetch|networkerror|network request failed|load failed|fetch failed|econnrefused|enotfound|timed? ?out|aborterror|the operation was aborted/i.test(jsText);
    var thrownWhileFetching = loc.charAt(0) === '/' && jsError && /^(TypeError|AbortError|DOMException)$/.test(String(jsError.name || ''));
    var isNetworkError = Boolean(jsError && !status && !pStatus && (looksLikeFetchFailure || thrownWhileFetching));
    // A bare 502/503/504 with no FinOps Dashboard error body comes from the Cloud Run front end (cold start,
    // overload, upstream timeout): transient infrastructure, not a code bug. HTTP 500 stays a code bug.
    var bareGatewayError = status >= 502 && status <= 504 && !pKind && p.is_code_bug !== true && !p.error_code;

    // 2. Internal server code bug (HTTP 500+ or error_kind === INTERNAL_CODE_ERROR) or client JS runtime bug
    if (pKind === 'INTERNAL_CODE_ERROR' || p.is_code_bug === true || (status >= 500 && !bareGatewayError) || (jsError && !status && !pStatus && !isNetworkError)) {
      var isClientJs = Boolean(jsError && !status && pKind !== 'INTERNAL_CODE_ERROR');
      var code = String(p.error_code || (isClientJs ? 'VL-UI-RUNTIME' : 'VL-CODE-500'));
      var reqId = p.request_id ? String(p.request_id) : null;
      var excType = String(p.exception_type || (jsError && jsError.name) || 'RuntimeError');
      var msg = String(
        p.user_message ||
        ('FinOps Dashboard hit an internal code error (' + excType + ') in ' + loc + '. Your current dashboard state was preserved.')
      );
      var todo = String(
        p.what_to_do ||
        ('This is a FinOps Dashboard code bug, not an issue with your input. Your current view is intact. ' +
         (reqId ? 'Share Request ID ' + reqId + ' (' + code + ') with the platform engineering team.' : 'Retry or reload the page.'))
      );
      var diag = p.diagnostic_cmd ? String(p.diagnostic_cmd) : null;
      var refPart = reqId ? ' [Request ID: ' + reqId + ' | ' + excType + ']' : ' [' + excType + ']';
      return {
        kind: 'INTERNAL_CODE_ERROR',
        code: code,
        isCodeBug: true,
        statePreserved: true,
        where: loc,
        requestId: reqId,
        exceptionType: excType,
        headline: 'Internal Code Error (' + code + ')' + refPart,
        userMessage: msg,
        whatToDo: todo,
        diagnosticCmd: diag,
        bannerText: '[' + code + ' · Code Bug' + refPart + '] ' + msg + ' What to do: ' + todo + (diag ? ' (' + diag + ')' : ''),
      };
    }

    // 3. Input / schema validation error (HTTP 400/422, VALIDATION_ERROR, INVALID_REQUEST, or rejected_ingest_payload)
    if (
      status === 400 || status === 422 ||
      pKind === 'VALIDATION_ERROR' ||
      pStatus === 'INVALID_REQUEST' ||
      p.error === 'rejected_ingest_payload' ||
      p.error === 'invalid_payload' ||
      Boolean(p.dead_letter) ||
      (pStatus === 'ERROR' && Array.isArray(p.errors))
    ) {
      var isIngestDlq = (p.error === 'rejected_ingest_payload' || p.error === 'invalid_payload' || Boolean(p.dead_letter));
      var vCode = String(p.error_code || (isIngestDlq ? 'VL-INGEST-422' : 'VL-INPUT-422'));
      var errs = Array.isArray(p.errors) && p.errors.length
        ? p.errors.map(String)
        : [String(p.user_message || p.message || p.reason || 'Invalid request parameters.')];
      var dlNote = p.dead_letter ? ' (Metadata recorded in Dead-Letter Queue without raw PII.)' : '';
      var vMsg = loc + ' rejected the input: ' + errs.join('; ') + dlNote;
      var vTodo = String(p.what_to_do || 'Check that numeric fields are finite numbers and required identifiers are valid, then retry.');
      return {
        kind: 'VALIDATION_ERROR',
        code: vCode,
        isCodeBug: false,
        statePreserved: true,
        where: loc,
        requestId: null,
        exceptionType: null,
        errors: errs,
        deadLettered: Boolean(p.dead_letter),
        headline: 'Input Validation Error (' + vCode + ')',
        userMessage: vMsg,
        whatToDo: vTodo,
        diagnosticCmd: null,
        bannerText: '[' + vCode + ' · Check Input] ' + vMsg + ' What to do: ' + vTodo,
      };
    }

    // 4. Upstream GCP / configuration status notices
    if (pStatus === 'PERMISSION_DENIED' || pStatus === 'NOT_CONFIGURED' || pStatus === 'NOT_CONNECTED' || pStatus === 'DISABLED' || pStatus === 'ERROR') {
      var gCode = 'VL-GCP-' + (pStatus || 'NOTICE');
      var gMsg = String(p.user_message || p.message || p.reason || ('Operation on ' + loc + ' could not complete (' + pStatus + ').'));
      var gTodo = String(p.what_to_do || p.hint || p.refresh_cli || 'Verify Google Cloud IAM permissions and service configuration.');
      return {
        kind: 'UPSTREAM_GCP_STATUS',
        code: gCode,
        isCodeBug: false,
        statePreserved: true,
        where: loc,
        requestId: null,
        exceptionType: null,
        headline: 'Service / Configuration Notice (' + gCode + ')',
        userMessage: gMsg,
        whatToDo: gTodo,
        diagnosticCmd: null,
        bannerText: '[' + gCode + '] ' + gMsg + ' — ' + gTodo,
      };
    }

    // 5. Network / unreachable / HTTP status fallback
    var netCode = status ? 'VL-HTTP-' + status : 'VL-NET-ERR';
    var netMsg = status
      ? (loc + ' returned HTTP ' + status + '.')
      : ('Could not reach ' + loc + (jsError && jsError.message ? ' (' + jsError.message + ')' : '') + '.');
    var netTodo = 'Your current dashboard state is preserved. Check your network connection or /api/health and retry.';
    return {
      kind: 'NETWORK_ERROR',
      code: netCode,
      isCodeBug: false,
      statePreserved: true,
      where: loc,
      requestId: null,
      exceptionType: null,
      headline: 'Request Failed (' + netCode + ')',
      userMessage: netMsg,
      whatToDo: netTodo,
      diagnosticCmd: null,
      bannerText: '[' + netCode + '] ' + netMsg + ' ' + netTodo,
    };
  }

  function reportUiRuntimeError(where, err) {
    var info = vlClassifyError(where, 0, null, err || new Error('UI render error'));
    try {
      if (typeof showApiNotice === 'function') showApiNotice(info.bannerText, info);
    } catch (_) {}
    if (typeof console !== 'undefined' && console.warn) {
      console.warn('FinOps Dashboard UI boundary caught error in ' + where + ':', err);
    }
    return info;
  }

  function safeRun(where, fn) {
    try {
      return fn();
    } catch (err) {
      reportUiRuntimeError(where, err);
      return null;
    }
  }

  function onFleet(fleet) {
    safeRun('VL.onFleet.openAgent', function() {
      if (pendingAgentId && fleet && Array.isArray(fleet.agents) && fleet.agents.length) {
        var target = findAgent(pendingAgentId);
        pendingAgentId = null;
        if (target) openAgent(target, null);
      } else if (openState && String(openState.id || '').indexOf('user:') !== 0) {
        var a = findAgent(openState.id);
        var typing = document.activeElement && document.activeElement.closest && document.activeElement.closest('.vl-alert-form');
        if (a && !typing) renderOpenAgent(a);
      }
    });
    safeRun('VL.onFleet.logCapNotice', function() { renderLogScanCapNotice(fleet); });
    safeRun('VL.onFleet.freshness', function() { renderFreshness(); });
    safeRun('VL.onFleet.spendOrTokenomics', function() {
      var st = state();
      if (st) renderSpend(st);
      else if (fleet) renderTokenomics(fleet);
    });
    safeRun('VL.onFleet.governanceScore', function() { renderGovernanceScore(state(), fleet); });
  }
  function onState(st) {
    if (!st) return;
    safeRun('VL.onState.spend', function() { renderSpend(st); });
    safeRun('VL.onState.adoption', function() { renderAdoption(st); });
    safeRun('VL.onState.freshness', function() { renderFreshness(); });
    safeRun('VL.onState.governanceScore', function() { renderGovernanceScore(st, rawFleet()); });
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
      placeholder: 'Search agents, apps, runtimes, models\u2026  ( / )', noun: 'agents', exportName: 'finops-agents',
      compactKey: 'finops.agentsCompact',
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
                                  noSort: [0], exportName: 'finops-sessions',
                                  isDetailRow: function(tr) { return tr.classList.contains('session-turns-row'); }});
    smartTable('powerUsersBody', {placeholder: 'Search users or departments\u2026', noun: 'users', pageSize: 25, hideEmptyColumns: true,
                                  exportName: 'finops-users'});
    smartTable('geMartDailyBody', {placeholder: 'Filter days (e.g. 2026-10)\u2026', noun: 'days', pageSize: 10, exportName: 'finops-daily-usage'});
    smartTable('geMartByAppBody', {placeholder: 'Filter by day, app, agent or model\u2026', noun: 'rows', pageSize: 15, exportName: 'finops-daily-by-app'});
    ['fleetUnregisteredBody', 'execUnregisteredBody'].forEach(function(id) {
      smartTable(id, {placeholder: 'Search standalone runtimes\u2026', noun: 'runtimes', noSort: [6], exportName: 'finops-standalone-runtimes'});
    });
    ['fleetSkillsMcpBody', 'liveSkillsMcpBody'].forEach(function(id) {
      smartTable(id, {placeholder: 'Search tools, sub-agents and MCP servers\u2026', noun: 'items', exportName: 'finops-skills-mcp'});
    });
    smartTable('aiveUsageBody', {placeholder: 'Search activity\u2026', noun: 'events', pageSize: 10, exportName: 'finops-activity'});
    smartTable('vocRatingsBody', {placeholder: 'Search feedback\u2026', noun: 'ratings', pageSize: 10, exportName: 'finops-feedback'});
    smartTable('decoratorEventsBody', {placeholder: 'Search events\u2026', noun: 'events', pageSize: 10, exportName: 'finops-sdk-events'});
    ['modelUsageBody', 'liveTeAgentsBody', 'liveDriftModelsBody', 'cloudRunServicesBody'].forEach(function(id) {
      smartTable(id, {toolbar: false});
    });
    var govChip = byId('vlGovScoreChip');
    if (govChip) {
      govChip.addEventListener('click', function() { toggleGovernanceScorecard(); });
    }
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
    budgetRunway: vlBudgetRunway, driftWaterfall: vlDriftWaterfall, thinkingTax: vlThinkingTax,
    modelRepricing: vlModelRepricing, sessionReceipt: vlSessionReceipt, userSummary: vlUserSummary,
    trendTotals: vlTrendTotals, runtimeKey: vlRuntimeKey, agentPrompt: vlAgentPrompt, safeExternalUrl: safeExternalUrl, userLabel: vlUserLabel,
    resolveTheme: vlResolveTheme,
    paletteScore: vlPaletteScore, paletteFilter: vlPaletteFilter,
    alertPolicies: vlAlertPolicies, alertTarget: vlAlertTarget, autoAlertWindow: vlAutoAlertWindow,
    classifyError: vlClassifyError, governanceScore: vlGovernanceScore, buildPdfReport: vlBuildPdfReport,
    // DOM (browser only)
    mount: mount, smartTable: smartTable, setTableQuery: setTableQuery, setTableFacet: setTableFacet,
    renderHealthStrip: renderHealthStrip, renderGovernanceScore: renderGovernanceScore, toggleGovernanceScorecard: toggleGovernanceScorecard,
    agentRowDecorate: agentRowDecorate, healthCell: healthCell, typeCell: typeCell, agentSpark: agentSpark,
    openAgent: openAgent, closeAgent: closeAgent, openUser: openUser, userRowDecorate: userRowDecorate, sessionReceiptNode: sessionReceiptNode,
    decorateAttention: decorateAttention, attentionSummary: attentionSummary,
    spendDeltaNode: spendDeltaNode, requestsSparkNode: requestsSparkNode, usersSparkNode: usersSparkNode,
    setHostCapabilities: setHostCapabilities, urlWindowHours: urlWindowHours, syncUrl: syncUrl, onTab: onTab,
    setTheme: setTheme, toggleTheme: toggleTheme, setHostTheme: setHostTheme, currentTheme: currentTheme,
    openPalette: openPalette, closePalette: closePalette,
    onFleet: onFleet, onState: onState, onTraceRow: onTraceRow, focusAgents: focusAgents, exportPdf: exportPdf,
  };
  return api;
})();

if (typeof document !== 'undefined' && document.body) {
  try { VL.mount(); } catch (err) { if (typeof console !== 'undefined') console.warn('FinOps Dashboard enhancements did not mount: ' + (err && err.message ? err.message : 'unknown error')); }
}
