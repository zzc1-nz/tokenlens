/* ======================================================================================
   TokenLens — dashboard application logic
   --------------------------------------------------------------------------------------
   Vanilla JS. No framework, no build step, no CDN. Every renderer is defensive:
   a missing/renamed field degrades to a placeholder instead of throwing, so a partial
   backend can never blank the console.

   Contract source of truth: docs/API_CONTRACT.md. Every endpoint this file talks to is
   listed once in API_PATHS below; tests/test_dashboard_api.py cross-checks those literals
   against the live OpenAPI schema (contract-drift guard).
   ====================================================================================== */
(function () {
  "use strict";

  /* ------------------------------------------------------------------ API endpoints */

  var API_PATHS = {
    overview: "/api/v1/dashboard/overview",
    breakdown: "/api/v1/usage/breakdown",
    trends: "/api/v1/trends",
    exportCsv: "/api/v1/export/csv",
    budgets: "/api/v1/budgets",
    budgetEvaluate: "/api/v1/budgets/evaluate",
    budgetOne: "/api/v1/budgets/{id}",
    alerts: "/api/v1/alerts",
    alertsSummary: "/api/v1/alerts/summary",
    anomalies: "/api/v1/anomalies",
    anomaliesSummary: "/api/v1/anomalies/summary",
    anomaliesDetect: "/api/v1/anomalies/detect",
    anomalyOne: "/api/v1/anomalies/{id}",
  };

  var POLL_MS = 3000;
  var STORAGE_KEY = "tokenlens.apiKey";

  var ANOMALY_REASON_LABELS = {
    retry_storm: "重试风暴",
    context_bloat: "上下文膨胀",
    uncompressed_tool_output: "工具返回数据未压缩",
    cache_miss_storm: "缓存命中崩塌",
    traffic_spike: "流量尖峰",
    unknown: "未知",
  };

  var STATUS_LABELS = {
    ok: "正常",
    warning: "警告",
    critical: "严重",
    exceeded: "超支",
  };

  var ANOMALY_STATUS_LABELS = {
    open: "待处理",
    acknowledged: "已确认",
    resolved: "已解决",
  };

  var SEVERITY_LABELS = {
    low: "低",
    medium: "中",
    high: "高",
    critical: "致命",
    warning: "警告",
    exceeded: "超支",
  };

  /* ------------------------------------------------------------------------ state */

  var state = {
    tab: "overview",
    overview: null,
    overviewError: null,
    lastPollAt: 0,
    pollTimer: null,
    inFlight: false,
    dimension: "project",
    dimensionWindowHours: 168,
    dimensionRows: [],
    dimensionSort: { key: "cost_usd", dir: "desc" },
    trendPeriod: "day",
    trendAgent: "",
    trendModel: "",
    trendMetric: "cost_usd",
    trendPoints: [],
    trendComparison: null,
    trendError: null,
    budgets: [],
    alerts: [],
    alertsSummary: null,
    anomalies: [],
    anomaliesSummary: null,
    anomalyStatusFilter: "",
    filtersLoaded: false,
    apiKey: readApiKey(),
  };

  /* ------------------------------------------------------------------ number formats */

  var nfInt = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 });
  var nfCompact = new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 });
  var nf2 = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  var nf4 = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 4, maximumFractionDigits: 4 });
  var nf1 = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });

  function money(value) {
    var v = num(value, 0);
    if (Math.abs(v) >= 1000) return "$" + nfInt.format(v);
    if (Math.abs(v) >= 1) return "$" + nf2.format(v);
    return "$" + nf4.format(v);
  }

  function intText(value) {
    return nfInt.format(Math.round(num(value, 0)));
  }

  function compactText(value) {
    return nfCompact.format(num(value, 0));
  }

  function pctText(ratio, digits) {
    var r = num(ratio, 0);
    if (!isFinite(r)) return "—";
    return (digits === 1 ? nf1.format(r * 100) : nf1.format(r * 100)) + "%";
  }

  function pctFromShare(value) {
    return nf1.format(num(value, 0)) + "%";
  }

  /* ------------------------------------------------------------------- small helpers */

  function num(value, fallback) {
    var n = typeof value === "number" ? value : parseFloat(value);
    return isFinite(n) ? n : (fallback === undefined ? 0 : fallback);
  }

  function isObj(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function obj(value) {
    return isObj(value) ? value : {};
  }

  function arr(value) {
    return Array.isArray(value) ? value : [];
  }

  function str(value, fallback) {
    if (value === null || value === undefined) return fallback === undefined ? "" : fallback;
    var text = String(value);
    return text === "" ? (fallback === undefined ? "" : fallback) : text;
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function setText(id, text) {
    var node = byId(id);
    if (node) node.textContent = text;
  }

  function setHtml(id, html) {
    var node = byId(id);
    if (node) node.innerHTML = html;
  }

  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, num(value, 0)));
  }

  /** ISO-8601 UTC string -> compact local clock label (defensive on garbage input). */
  function timeLabel(iso, withDate) {
    if (!iso) return "—";
    var date = new Date(iso);
    if (isNaN(date.getTime())) return str(iso, "—");
    var hh = String(date.getHours()).padStart(2, "0");
    var mm = String(date.getMinutes()).padStart(2, "0");
    if (!withDate) return hh + ":" + mm;
    var mo = String(date.getMonth() + 1).padStart(2, "0");
    var dd = String(date.getDate()).padStart(2, "0");
    return mo + "-" + dd + " " + hh + ":" + mm;
  }

  function dateTimeLabel(iso) {
    if (!iso) return "—";
    var date = new Date(iso);
    if (isNaN(date.getTime())) return str(iso, "—");
    var mo = String(date.getMonth() + 1).padStart(2, "0");
    var dd = String(date.getDate()).padStart(2, "0");
    var hh = String(date.getHours()).padStart(2, "0");
    var mm = String(date.getMinutes()).padStart(2, "0");
    return mo + "-" + dd + " " + hh + ":" + mm;
  }

  function readApiKey() {
    try {
      return window.localStorage.getItem(STORAGE_KEY) || "";
    } catch (err) {
      return "";
    }
  }

  var toastTimer = null;
  function toast(message, tone) {
    var node = byId("toast");
    if (!node) return;
    node.textContent = String(message || "");
    node.setAttribute("data-tone", tone || "info");
    node.hidden = false;
    if (toastTimer) window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      node.hidden = true;
    }, 4200);
  }

  /* -------------------------------------------------------------------- HTTP layer */

  function fillPath(template, params) {
    var path = str(template, "");
    var values = params || {};
    Object.keys(values).forEach(function (key) {
      path = path.replace("{" + key + "}", encodeURIComponent(String(values[key])));
    });
    return path;
  }

  function withQuery(path, query) {
    var pairs = [];
    var data = query || {};
    Object.keys(data).forEach(function (key) {
      var value = data[key];
      if (value === null || value === undefined || value === "") return;
      pairs.push(encodeURIComponent(key) + "=" + encodeURIComponent(String(value)));
    });
    if (!pairs.length) return path;
    return path + (path.indexOf("?") >= 0 ? "&" : "?") + pairs.join("&");
  }

  /**
   * One fetch wrapper: JSON in, JSON out, never throws.
   * Resolves `{ ok, status, data, error }` so callers decide how loud to be.
   */
  function request(path, options) {
    var opts = options || {};
    var headers = { Accept: "application/json" };
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    if (state.apiKey) headers["X-API-Key"] = state.apiKey;
    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      cache: "no-store",
    })
      .then(function (response) {
        return response
          .text()
          .then(function (text) {
            var parsed = null;
            if (text) {
              try {
                parsed = JSON.parse(text);
              } catch (err) {
                parsed = null;
              }
            }
            return { ok: response.ok, status: response.status, data: parsed, text: text };
          })
          .catch(function () {
            return { ok: response.ok, status: response.status, data: null, text: "" };
          });
      })
      .then(function (result) {
        if (!result.ok) {
          var detail = "";
          if (isObj(result.data) && result.data.detail) detail = String(result.data.detail);
          result.error = detail || "HTTP " + result.status;
        }
        return result;
      })
      .catch(function (err) {
        return { ok: false, status: 0, data: null, error: (err && err.message) || "网络错误" };
      });
  }

  function getJSON(path, query) {
    return request(withQuery(path, query));
  }

  /* ---------------------------------------------------------------------- chrome */

  function setConnection(stateName, label) {
    var dot = byId("conn-dot");
    if (dot) dot.setAttribute("data-state", stateName);
    setText("conn-label", label);
  }

  function tickClock() {
    var now = new Date();
    setText(
      "clock",
      String(now.getHours()).padStart(2, "0") + ":" +
        String(now.getMinutes()).padStart(2, "0") + ":" +
        String(now.getSeconds()).padStart(2, "0")
    );
  }

  function switchTab(name) {
    if (!name) return;
    state.tab = name;
    var tabs = document.querySelectorAll(".tab");
    Array.prototype.forEach.call(tabs, function (tab) {
      var isActive = tab.getAttribute("data-tab") === name;
      tab.classList.toggle("is-active", isActive);
      tab.setAttribute("aria-selected", isActive ? "true" : "false");
    });
    var panels = document.querySelectorAll(".panel");
    Array.prototype.forEach.call(panels, function (panel) {
      var isActive = panel.id === "panel-" + name;
      panel.classList.toggle("is-active", isActive);
      if (isActive) panel.removeAttribute("hidden");
      else panel.setAttribute("hidden", "hidden");
    });
    if (name === "dimensions") loadDimensions();
    if (name === "trends") loadTrends();
    if (name === "budgets") loadBudgets();
    if (name === "anomalies") loadAnomalies();
    if (name === "overview") renderOverview();
  }

  /* ================================================================== 1. OVERVIEW */

  /**
   * Read a breakdown list out of the overview payload.
   *
   * §9 specifies ``by_agent``/``by_project``/``by_model``/``by_runtime``; we also accept
   * the bare dimension name so a backend that keys them differently degrades to a
   * working panel instead of an empty one.
   */
  function breakdownRows(breakdowns, dimension) {
    var data = obj(breakdowns);
    if (Array.isArray(data["by_" + dimension])) return data["by_" + dimension];
    if (Array.isArray(data[dimension])) return data[dimension];
    return [];
  }

  function renderOverview() {
    var data = obj(state.overview);
    var kpis = obj(data.kpis);
    var breakdowns = obj(data.breakdowns);
    var timeseries = obj(data.timeseries);

    /* KPI cards */
    var todayCost = num(kpis.today_cost_usd, 0);
    var todayTokens = num(kpis.today_tokens, 0);
    var weekCost = num(kpis.week_cost_usd, 0);
    var monthCost = num(kpis.month_cost_usd, 0);
    var burnTpm = num(kpis.burn_rate_tokens_per_min, 0);
    var burnUph = num(kpis.burn_rate_usd_per_hour, 0);
    var cacheHit = clamp(kpis.cache_hit_rate, 0, 1);

    setText("kpi-today-cost", state.overview ? money(todayCost) : "—");
    setText("kpi-today-tokens", state.overview ? intText(todayTokens) + " tokens" : "—");
    setText("kpi-week-cost", state.overview ? money(weekCost) : "—");
    setText("kpi-month-cost", state.overview ? money(monthCost) : "—");
    setText("kpi-burn-rate", state.overview ? intText(burnTpm) + " tok/min" : "—");
    setText("kpi-burn-cost", state.overview ? money(burnUph) + " / 小时" : "—");
    setText("kpi-cache-hit", state.overview ? pctText(cacheHit) : "—");

    var cacheBar = byId("kpi-cache-bar");
    if (cacheBar) cacheBar.style.width = clamp(cacheHit * 100, 0, 100).toFixed(1) + "%";

    var activeAgents = num(kpis.active_agents, 0);
    var activeProjects = num(kpis.active_projects, 0);
    var totalSessions = num(kpis.total_sessions, 0);
    setText("kpi-active-agents", state.overview ? intText(activeAgents) : "—");
    setText(
      "kpi-active-projects",
      state.overview ? intText(activeProjects) + " 个项目 · " + intText(totalSessions) + " 会话" : "—"
    );

    var todayNode = byId("kpi-today-cost");
    if (todayNode) {
      todayNode.classList.toggle("is-warn", todayCost > 0 && weekCost > 0 && todayCost > weekCost * 0.5);
      todayNode.classList.toggle("is-bad", todayCost > 0 && monthCost > 0 && todayCost > monthCost * 0.4);
    }

    setText("overview-generated", data.generated_at ? "更新于 " + timeLabel(data.generated_at, true) : "等待数据…");

    /* hourly sparkline */
    var hourly = arr(timeseries.hourly);
    var sparkValues = hourly.map(function (point) {
      return num(obj(obj(point).totals).cost_usd, 0);
    });
    var sparkSvg = byId("sparkline");
    var sparkOk = false;
    if (window.TLCharts && sparkSvg) {
      sparkOk = window.TLCharts.sparkline(sparkSvg, sparkValues, { color: "#4de3c1", height: 180 });
    }
    var sparkWrap = byId("sparkline-wrap");
    if (sparkWrap) sparkWrap.classList.toggle("is-empty", !sparkOk);
    setText("spark-sub", hourly.length ? "最近 " + hourly.length + " 个小时" : "最近 24 小时");
    setText(
      "sparkline-foot",
      sparkOk
        ? "合计 " + money(sparkValues.reduce(function (a, b) { return a + b; }, 0)) +
          " · 峰值 " + money(Math.max.apply(null, sparkValues.concat([0])))
        : "暂无数据"
    );

    /* daily bars */
    var daily = arr(timeseries.daily);
    var dailySeries = daily.map(function (point) {
      var p = obj(point);
      return { label: str(p.label, timeLabel(p.bucket_start, true)), value: num(obj(p.totals).cost_usd, 0) };
    });
    var dailySvg = byId("daily-chart");
    var dailyOk = false;
    if (window.TLCharts && dailySvg) {
      dailyOk = window.TLCharts.barChart(dailySvg, dailySeries, {
        height: 180,
        color: "#45a7ff",
        formatValue: function (v) { return money(v); },
        formatLabel: function (label) { return String(label || "").slice(5); },
      });
    }
    var dailyWrap = dailySvg ? dailySvg.parentNode : null;
    if (dailyWrap) dailyWrap.classList.toggle("is-empty", !dailyOk);
    setText("daily-sub", daily.length ? "最近 " + daily.length + " 天" : "最近 7 天");
    setText(
      "daily-chart-foot",
      dailyOk
        ? "合计 " + money(dailySeries.reduce(function (a, row) { return a + row.value; }, 0)) +
          " · 日均 " + money(dailySeries.length ? dailySeries.reduce(function (a, row) { return a + row.value; }, 0) / dailySeries.length : 0)
        : "暂无数据"
    );

    /* budgets */
    var budgets = arr(data.budgets);
    state.budgets = budgets.length ? budgets : state.budgets;
    renderBudgetBars("overview-budgets", budgets);
    setText("budget-summary-sub", budgets.length ? budgets.length + " 个预算" : "—");

    /* alerts mini list */
    var alerts = obj(data.alerts);
    var recentAlerts = arr(alerts.recent);
    var alertSummary = obj(alerts.summary);
    renderAlertMiniList("overview-alerts", recentAlerts);
    state.alertsSummary = Object.keys(alertSummary).length ? alertSummary : state.alertsSummary;

    /* anomalies mini list */
    var anomalies = obj(data.anomalies);
    var recentAnomalies = arr(anomalies.recent);
    renderAnomalyMiniList("overview-anomalies", recentAnomalies);
    if (Object.keys(obj(anomalies.summary)).length) state.anomaliesSummary = obj(anomalies.summary);

    /* top projects / agents */
    renderMiniBars("overview-top-projects", breakdownRows(breakdowns, "project"));
    renderMiniBars("overview-top-agents", breakdownRows(breakdowns, "agent"));

    /* keep dropdowns in sync with reality */
    syncFilterOptions(breakdownRows(breakdowns, "agent"), breakdownRows(breakdowns, "model"));
  }

  function syncFilterOptions(agents, models) {
    if (state.filtersLoaded) return;
    var agentSelect = byId("trend-agent");
    var modelSelect = byId("trend-model");
    var filled = false;
    if (agentSelect && agents.length) {
      while (agentSelect.options.length > 1) agentSelect.remove(1);
      agents.forEach(function (row) {
        var item = obj(row);
        var option = document.createElement("option");
        option.value = str(item.key, "");
        option.textContent = str(item.label, str(item.key, "—"));
        agentSelect.appendChild(option);
      });
      filled = true;
    }
    if (modelSelect && models.length) {
      while (modelSelect.options.length > 1) modelSelect.remove(1);
      models.forEach(function (row) {
        var item = obj(row);
        var option = document.createElement("option");
        option.value = str(item.key, "");
        option.textContent = str(item.label, str(item.key, "—"));
        modelSelect.appendChild(option);
      });
      filled = true;
    }
    if (filled) state.filtersLoaded = true;
  }

  function statusOf(value) {
    var key = str(value, "ok").toLowerCase();
    return STATUS_LABELS[key] ? key : "ok";
  }

  function renderBudgetBars(targetId, budgets) {
    var host = byId(targetId);
    if (!host) return;
    if (!budgets.length) {
      host.innerHTML = '<p class="empty">暂无预算，请到「预算与告警」创建。</p>';
      return;
    }
    host.innerHTML = budgets.map(function (raw) {
      var item = obj(raw);
      var budget = obj(item.budget);
      var status = statusOf(item.status);
      var pct = clamp(num(item.pct, 0) * 100, 0, 100);
      var thresholds = arr(budget.thresholds);
      var ticks = thresholds
        .map(function (threshold) {
          var position = clamp(num(threshold, 0) * 100, 0, 100);
          return '<i class="bbar-tick" style="left:' + position.toFixed(1) + '%"></i>';
        })
        .join("");
      return (
        '<div class="bbar">' +
        '<div class="bbar-top">' +
        '<span class="bbar-name">' + esc(str(budget.name, "未命名预算")) +
        '<span class="bbar-scope">' + esc(str(budget.scope, "global")) + ":" + esc(str(budget.scope_key, "*")) + "</span></span>" +
        '<span class="bbar-nums">' + money(item.spent_usd) + " / " + money(item.limit_usd) + "</span>" +
        "</div>" +
        '<div class="bbar-track"><div class="bbar-fill" data-status="' + status + '" style="width:' + pct.toFixed(1) + '%"></div>' +
        '<div class="bbar-ticks">' + ticks + "</div></div>" +
        '<div class="bbar-foot"><span>' + esc(str(STATUS_LABELS[status], status)) +
        " · " + pctFromShare(pct) + "</span>" +
        "<span>预测 " + money(item.projected_usd) + " · 触发 " + intText(item.trigger_count) + " 次</span></div>" +
        "</div>"
      );
    }).join("");
  }

  function renderAlertMiniList(targetId, alerts) {
    var host = byId(targetId);
    if (!host) return;
    if (!alerts.length) {
      host.innerHTML = '<p class="empty">暂无告警。</p>';
      return;
    }
    host.innerHTML = alerts.slice(0, 6).map(function (raw) {
      var alert = obj(raw);
      var severity = str(alert.severity, "warning");
      return (
        '<div class="mini-item">' +
        '<div class="mi-main"><span class="mi-title">' + esc(str(alert.budget_name, "预算告警")) + "</span>" +
        '<span class="mi-sub">' + esc(timeLabel(alert.created_at, true)) + " · 阈值 " +
        esc(intText(num(alert.threshold, 0) * 100) + "%") + " · 触发 " + intText(alert.trigger_count) + " 次</span></div>" +
        '<span class="status-pill" data-status="' + esc(severity) + '">' + esc(str(SEVERITY_LABELS[severity], severity)) + "</span>" +
        "</div>"
      );
    }).join("");
  }

  function renderAnomalyMiniList(targetId, anomalies) {
    var host = byId(targetId);
    if (!host) return;
    if (!anomalies.length) {
      host.innerHTML = '<p class="empty">暂无异常。</p>';
      return;
    }
    host.innerHTML = anomalies.slice(0, 6).map(function (raw) {
      var item = obj(raw);
      var reason = str(item.reason, "unknown");
      var label = str(item.reason_label, ANOMALY_REASON_LABELS[reason] || ANOMALY_REASON_LABELS.unknown);
      var severity = str(item.severity, "low");
      return (
        '<div class="mini-item">' +
        '<div class="mi-main"><span class="mi-title">' + esc(label) + " · " + esc(str(item.agent, "—")) + "</span>" +
        '<span class="mi-sub">' + esc(timeLabel(item.detected_at, true)) + " · " + esc(str(item.project, "—")) +
        " · 比率 " + nf1.format(num(item.ratio, 0)) + "x</span></div>" +
        '<span class="badge" data-tone="' + esc(severity) + '">' + esc(str(SEVERITY_LABELS[severity], severity)) + "</span>" +
        "</div>"
      );
    }).join("");
  }

  function renderMiniBars(targetId, rows) {
    var host = byId(targetId);
    if (!host) return;
    if (!rows.length) {
      host.innerHTML = '<p class="empty">暂无数据</p>';
      return;
    }
    var top = rows.slice(0, 6);
    var maxCost = top.reduce(function (acc, row) { return Math.max(acc, num(obj(row).totals && obj(row).totals.cost_usd, 0)); }, 0) || 1;
    host.innerHTML = top.map(function (raw) {
      var row = obj(raw);
      var totals = obj(row.totals);
      var cost = num(totals.cost_usd, 0);
      var width = clamp((cost / maxCost) * 100, 0, 100);
      return (
        '<div class="mini-bar">' +
        '<div class="mini-bar-top"><span class="mini-bar-name">' + esc(str(row.label, str(row.key, "—"))) + "</span>" +
        '<span class="mini-bar-num">' + money(cost) + " · " + pctFromShare(num(row.share_pct, 0)) + "</span></div>" +
        '<div class="mini-bar-track"><div class="mini-bar-fill" style="width:' + width.toFixed(1) + '%"></div></div>' +
        "</div>"
      );
    }).join("");
  }

  /* ================================================================ 2. DIMENSIONS */

  var DIMENSION_ENDPOINTS = {
    project: "project",
    agent: "agent",
    model: "model",
    runtime: "runtime",
  };

  function loadDimensions() {
    var dimension = DIMENSION_ENDPOINTS[state.dimension] || "project";
    var note = byId("dimension-note");
    if (note) note.textContent = "加载中…";
    getJSON(API_PATHS.breakdown, { dimension: dimension, limit: 100 }).then(function (result) {
      if (result.ok && isObj(result.data)) {
        state.dimensionRows = arr(result.data.rows);
        if (note) {
          note.textContent = state.dimensionRows.length
            ? dimension + " · " + state.dimensionRows.length + " 行"
            : "暂无数据";
        }
      } else {
        /* graceful fallback to the overview payload when the query API is unavailable */
        state.dimensionRows = fallbackBreakdown(dimension);
        if (note) note.textContent = state.dimensionRows.length ? "离线兜底数据（overview）" : "接口不可用";
      }
      renderDimensionTable();
    });
  }

  function fallbackBreakdown(dimension) {
    var breakdowns = obj(obj(state.overview).breakdowns);
    var map = {
      project: breakdowns.by_project,
      agent: breakdowns.by_agent,
      model: breakdowns.by_model,
      runtime: breakdowns.by_runtime,
    };
    return arr(map[dimension]);
  }

  function sortValue(row, key) {
    var item = obj(row);
    var totals = obj(item.totals);
    if (key === "label") return str(item.label, str(item.key, ""));
    if (key === "share_pct") return num(item.share_pct, 0);
    return num(totals[key], 0);
  }

  function renderDimensionTable() {
    var tbody = byId("dimension-tbody");
    if (!tbody) return;
    var rows = state.dimensionRows.slice();
    var sort = state.dimensionSort;

    rows.sort(function (a, b) {
      var left = sortValue(a, sort.key);
      var right = sortValue(b, sort.key);
      var cmp;
      if (typeof left === "string" || typeof right === "string") {
        cmp = String(left).localeCompare(String(right), "zh-CN");
      } else {
        cmp = left - right;
      }
      return sort.dir === "asc" ? cmp : -cmp;
    });

    Array.prototype.forEach.call(document.querySelectorAll("#dimension-table thead th.sortable"), function (th) {
      th.classList.remove("sort-asc", "sort-desc");
      if (th.getAttribute("data-sort") === sort.key) {
        th.classList.add(sort.dir === "asc" ? "sort-asc" : "sort-desc");
      }
    });

    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="10" class="empty">暂无数据</td></tr>';
      setText("dimension-foot", "—");
      return;
    }

    var totalCost = rows.reduce(function (acc, row) { return acc + num(obj(obj(row).totals).cost_usd, 0); }, 0);
    tbody.innerHTML = rows.map(function (raw) {
      var row = obj(raw);
      var totals = obj(row.totals);
      var share = num(row.share_pct, totalCost > 0 ? (num(totals.cost_usd, 0) / totalCost) * 100 : 0);
      return (
        "<tr>" +
        '<td class="key-cell" title="' + esc(str(row.key, "")) + '">' + esc(str(row.label, str(row.key, "—"))) + "</td>" +
        '<td class="num">' + money(totals.cost_usd) + "</td>" +
        '<td class="num">' + pctFromShare(share) + "</td>" +
        '<td class="num">' + intText(totals.total_tokens) + "</td>" +
        '<td class="num muted">' + intText(totals.input_tokens) + "</td>" +
        '<td class="num muted">' + intText(totals.output_tokens) + "</td>" +
        '<td class="num muted">' + intText(totals.cache_read_tokens) + "</td>" +
        '<td class="num muted">' + intText(totals.requests) + "</td>" +
        '<td class="num muted">' + pctText(totals.cache_hit_rate) + "</td>" +
        '<td class="share-col"><div class="share-track"><div class="share-fill" style="width:' +
        clamp(share, 0, 100).toFixed(1) + '%"></div></div></td>' +
        "</tr>"
      );
    }).join("");

    setText(
      "dimension-foot",
      rows.length + " 行 · 合计成本 " + money(totalCost) +
        " · 合计 Tokens " + intText(rows.reduce(function (acc, row) { return acc + num(obj(obj(row).totals).total_tokens, 0); }, 0))
    );
  }

  /* ==================================================================== 3. TRENDS */

  var TREND_METRIC_LABELS = {
    cost_usd: "成本 (USD)",
    total_tokens: "总 Tokens",
    requests: "请求数",
  };

  function loadTrends() {
    var note = byId("trend-note");
    if (note) note.textContent = "加载中…";
    var query = {
      period: state.trendPeriod,
      limit: 30,
      agent: state.trendAgent,
      model: state.trendModel,
    };
    getJSON(API_PATHS.trends, query).then(function (result) {
      if (result.ok && isObj(result.data)) {
        state.trendPoints = arr(result.data.points);
        state.trendComparison = obj(result.data.comparison);
        state.trendError = null;
        if (note) note.textContent = state.trendPoints.length ? state.trendPoints.length + " 个数据点" : "暂无数据";
      } else {
        state.trendPoints = fallbackTrendPoints();
        state.trendComparison = null;
        state.trendError = result.error || "接口不可用";
        if (note) note.textContent = state.trendPoints.length ? "离线兜底数据（overview）" : (result.error || "接口不可用");
      }
      renderTrends();
    });
  }

  function fallbackTrendPoints() {
    var timeseries = obj(obj(state.overview).timeseries);
    var hourly = arr(timeseries.hourly);
    var daily = arr(timeseries.daily);
    if (state.trendPeriod === "day") return hourly.slice(-24);
    return daily.slice(-30);
  }

  function metricOf(point) {
    var totals = obj(obj(point).totals);
    return num(totals[state.trendMetric], 0);
  }

  function renderTrends() {
    var points = state.trendPoints;
    var series = points.map(function (raw) {
      var point = obj(raw);
      return {
        label: str(point.label, timeLabel(point.bucket_start, true)),
        value: metricOf(point),
        hint: "成本 " + money(obj(point.totals).cost_usd),
      };
    });

    var metricLabel = TREND_METRIC_LABELS[state.trendMetric] || state.trendMetric;
    setText("trend-chart-title", metricLabel + " 趋势");
    setText(
      "trend-chart-sub",
      series.length ? series.length + " 点 · " + (state.trendPeriod === "day" ? "按小时" : state.trendPeriod === "week" ? "按天" : "按天/月") : "—"
    );

    var svg = byId("trend-chart");
    var wrap = svg ? svg.parentNode : null;
    var ok = false;
    if (window.TLCharts && svg) {
      ok = window.TLCharts.lineArea(svg, series, {
        height: 260,
        color: "#4de3c1",
        wrapEl: wrap,
        tooltipEl: byId("trend-tooltip"),
        formatValue: state.trendMetric === "cost_usd"
          ? function (v) { return money(v); }
          : function (v) { return compactText(v); },
        formatLabel: function (label) { return String(label || "").slice(5); },
      });
    }
    if (wrap) wrap.classList.toggle("is-empty", !ok);

    /* comparison panel */
    var host = byId("trend-comparison");
    if (host) {
      var comparison = state.trendComparison;
      if (!isObj(comparison) || !Object.keys(comparison).length) {
        host.innerHTML = '<p class="empty">暂无对比数据' + (state.trendError ? "（" + esc(state.trendError) + "）" : "") + "</p>";
      } else {
        var currentCost = num(comparison.current_cost_usd, 0);
        var previousCost = num(comparison.previous_cost_usd, 0);
        var deltaPct = num(comparison.delta_pct, 0);
        var deltaUsd = num(comparison.delta_usd, 0);
        var tokenDelta = num(comparison.token_delta_pct, 0);
        var tone = deltaPct > 0 ? "trend-up" : deltaPct < 0 ? "trend-down" : "";
        host.innerHTML =
          '<div class="compare-cell"><span class="cc-label">当前周期</span>' +
          '<span class="cc-value">' + money(currentCost) + "</span>" +
          '<span class="cc-delta ' + tone + '">' + (deltaUsd >= 0 ? "+" : "") + money(deltaUsd) +
          " (" + (deltaPct >= 0 ? "+" : "") + nf1.format(deltaPct) + "%)</span></div>" +
          '<div class="compare-cell"><span class="cc-label">上一周期</span>' +
          '<span class="cc-value">' + money(previousCost) + "</span>" +
          '<span class="cc-delta">Tokens ' + (tokenDelta >= 0 ? "+" : "") + nf1.format(tokenDelta) + "%</span></div>";
      }
    }

    /* data table */
    var tbody = byId("trend-tbody");
    if (!tbody) return;
    if (!points.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">暂无数据</td></tr>';
      return;
    }
    var recent = points.slice(-40).reverse();
    tbody.innerHTML = recent.map(function (raw) {
      var point = obj(raw);
      var totals = obj(point.totals);
      return (
        "<tr>" +
        "<td>" + esc(str(point.label, timeLabel(point.bucket_start, true))) + "</td>" +
        '<td class="num">' + money(totals.cost_usd) + "</td>" +
        '<td class="num">' + intText(totals.total_tokens) + "</td>" +
        '<td class="num">' + intText(totals.requests) + "</td>" +
        '<td class="num">' + pctText(totals.cache_hit_rate) + "</td>" +
        '<td class="num muted">' + money(totals.avg_cost_per_request) + "</td>" +
        "</tr>"
      );
    }).join("");
  }

  function exportCsv() {
    var query = {
      dataset: "timeseries",
      bucket: state.trendPeriod === "day" ? "hour" : "day",
      agent: state.trendAgent,
      model: state.trendModel,
    };
    var url = withQuery(API_PATHS.exportCsv, query);
    var link = document.createElement("a");
    link.href = url;
    link.setAttribute("download", "");
    link.style.display = "none";
    document.body.appendChild(link);
    link.click();
    window.setTimeout(function () {
      if (link.parentNode) link.parentNode.removeChild(link);
    }, 0);
    toast("已开始下载 " + query.dataset + " CSV（" + query.bucket + " 粒度）");
  }

  /* ============================================================ 4. BUDGETS/ALERTS */

  function loadBudgets() {
    var tasks = [
      getJSON(API_PATHS.budgets),
      getJSON(API_PATHS.alerts, { limit: 50 }),
      getJSON(API_PATHS.alertsSummary),
    ];
    Promise.all(tasks).then(function (results) {
      var budgetResult = results[0] || {};
      var alertResult = results[1] || {};
      var summaryResult = results[2] || {};

      if (budgetResult.ok && isObj(budgetResult.data)) {
        state.budgets = arr(budgetResult.data.items);
      } else {
        state.budgets = arr(obj(state.overview).budgets);
      }
      if (alertResult.ok && isObj(alertResult.data)) {
        state.alerts = arr(alertResult.data.items);
      } else {
        state.alerts = arr(obj(obj(state.overview).alerts).recent);
      }
      if (summaryResult.ok && isObj(summaryResult.data)) {
        state.alertsSummary = summaryResult.data;
      } else if (!state.alertsSummary) {
        state.alertsSummary = obj(obj(state.overview).alerts).summary;
      }
      renderBudgets();
      renderAlerts();
    });
  }

  function renderBudgets() {
    var host = byId("budget-cards");
    if (!host) return;
    var budgets = arr(state.budgets);
    setText("budget-list-sub", budgets.length ? budgets.length + " 个预算" : "—");
    if (!budgets.length) {
      host.innerHTML = '<p class="empty">暂无预算。用左侧表单创建第一个预算。</p>';
      return;
    }
    host.innerHTML = budgets.map(function (raw) {
      var item = obj(raw);
      var budget = obj(item.budget);
      var status = statusOf(item.status);
      var id = num(budget.id, 0);
      var pct = clamp(num(item.pct, 0) * 100, 0, 100);
      var thresholds = arr(budget.thresholds);
      var ticks = thresholds.map(function (threshold) {
        return '<i class="bbar-tick" style="left:' + clamp(num(threshold, 0) * 100, 0, 100).toFixed(1) + '%"></i>';
      }).join("");
      return (
        '<div class="budget-card" data-status="' + status + '">' +
        '<div class="bc-head"><span class="bc-name">' + esc(str(budget.name, "未命名预算")) + "</span>" +
        '<span class="status-pill" data-status="' + status + '">' + esc(str(STATUS_LABELS[status], status)) + "</span></div>" +
        '<div class="bc-meta">' + esc(str(budget.scope, "global")) + ":" + esc(str(budget.scope_key, "*")) +
        " · " + esc(str(budget.period, "daily")) + " · 阈值 " + esc(thresholds.join(" / ")) + "</div>" +
        '<div class="bc-nums"><span class="bc-spent">' + money(item.spent_usd) + "</span>" +
        '<span class="bc-limit">/ ' + money(item.limit_usd) + "</span></div>" +
        '<div class="bbar-track"><div class="bbar-fill" data-status="' + status + '" style="width:' + pct.toFixed(1) + '%"></div>' +
        '<div class="bbar-ticks">' + ticks + "</div></div>" +
        '<div class="bc-extra"><span>预测 ' + money(item.projected_usd) + " · " + pctFromShare(num(item.projected_pct, 0) * 100) + "</span>" +
        "<span>触发 " + intText(item.trigger_count) + " 次 · 超支 " + money(item.overspend_usd) + "</span></div>" +
        '<div class="bc-actions">' +
        '<button class="btn btn-sm btn-ghost" data-budget-toggle="' + id + '" data-enabled="' +
        (budget.enabled === false ? "0" : "1") + '">' + (budget.enabled === false ? "启用" : "停用") + "</button>" +
        '<button class="btn btn-sm btn-danger" data-budget-delete="' + id + '">删除</button>' +
        "</div>" +
        "</div>"
      );
    }).join("");
  }

  function renderAlerts() {
    var host = byId("alert-summary");
    var summary = obj(state.alertsSummary);
    if (host) {
      var bySeverity = obj(summary.by_severity);
      var totalAlerts = num(summary.total_alerts, 0);
      var totalTriggers = num(summary.total_triggers, 0);
      var totalOverspend = num(summary.total_overspend_usd, 0);
      if (!Object.keys(summary).length) {
        host.innerHTML = '<p class="empty">暂无告警。</p>';
      } else {
        host.innerHTML =
          '<div class="summary-cell"><span class="sc-label">告警条数</span><span class="sc-value">' + intText(totalAlerts) + "</span></div>" +
          '<div class="summary-cell is-warn"><span class="sc-label">累计触发</span><span class="sc-value">' + intText(totalTriggers) + "</span></div>" +
          '<div class="summary-cell is-bad"><span class="sc-label">累计超支</span><span class="sc-value">' + money(totalOverspend) + "</span></div>" +
          '<div class="summary-cell"><span class="sc-label">警告</span><span class="sc-value">' + intText(bySeverity.warning) + "</span></div>" +
          '<div class="summary-cell is-warn"><span class="sc-label">严重</span><span class="sc-value">' + intText(bySeverity.critical) + "</span></div>" +
          '<div class="summary-cell is-bad"><span class="sc-label">超支</span><span class="sc-value">' + intText(bySeverity.exceeded) + "</span></div>";
      }
      setText("alert-summary-sub", summary.last_triggered_at ? "最近触发 " + timeLabel(summary.last_triggered_at, true) : "—");
    }

    var tbody = byId("alert-tbody");
    if (!tbody) return;
    var alerts = arr(state.alerts);
    if (!alerts.length) {
      tbody.innerHTML = '<tr><td colspan="10" class="empty">暂无告警记录</td></tr>';
      return;
    }
    tbody.innerHTML = alerts.map(function (raw) {
      var alert = obj(raw);
      var severity = str(alert.severity, "warning");
      var channels = arr(alert.channels);
      return (
        "<tr>" +
        "<td>" + esc(timeLabel(alert.created_at, true)) + "</td>" +
        '<td class="key-cell">' + esc(str(alert.budget_name, "—")) + "</td>" +
        "<td>" + esc(str(alert.scope, "global")) + ":" + esc(str(alert.scope_key, "*")) + "</td>" +
        '<td class="num">' + intText(num(alert.threshold, 0) * 100) + "%</td>" +
        '<td><span class="status-pill" data-status="' + esc(severity) + '">' +
        esc(str(SEVERITY_LABELS[severity], severity)) + "</span></td>" +
        '<td class="num">' + money(alert.spent_usd) + "</td>" +
        '<td class="num">' + money(alert.limit_usd) + "</td>" +
        '<td class="num">' + money(alert.overspend_usd) + "</td>" +
        '<td class="num">' + intText(alert.trigger_count) + "</td>" +
        '<td class="muted">' + esc(channels.length ? channels.join(", ") : "—") + "</td>" +
        "</tr>"
      );
    }).join("");
  }

  function submitBudget(event) {
    if (event) event.preventDefault();
    var name = str((byId("budget-name") || {}).value, "").trim();
    var limit = num((byId("budget-limit") || {}).value, 0);
    var message = byId("budget-form-msg");
    if (!name) {
      if (message) { message.textContent = "请填写预算名称"; message.className = "form-msg is-err"; }
      return;
    }
    if (!(limit > 0)) {
      if (message) { message.textContent = "限额必须大于 0"; message.className = "form-msg is-err"; }
      return;
    }
    var scope = str((byId("budget-scope") || {}).value, "global");
    var scopeKey = str((byId("budget-scope-key") || {}).value, "*").trim() || "*";
    if (scope === "global") scopeKey = "*";
    var thresholds = str((byId("budget-thresholds") || {}).value, "0.5,0.8,1.0")
      .split(",")
      .map(function (part) { return parseFloat(part); })
      .filter(function (value) { return isFinite(value) && value > 0; });
    if (!thresholds.length) thresholds = [0.5, 0.8, 1.0];

    var payload = {
      name: name,
      scope: scope,
      scope_key: scopeKey,
      period: str((byId("budget-period") || {}).value, "daily"),
      limit_usd: limit,
      thresholds: thresholds,
      desktop: !!(byId("budget-desktop") || {}).checked,
      webhook_url: str((byId("budget-webhook") || {}).value, "").trim() || null,
    };

    var submit = byId("budget-submit");
    if (submit) submit.disabled = true;
    if (message) { message.textContent = "提交中…"; message.className = "form-msg"; }

    request(API_PATHS.budgets, { method: "POST", body: payload }).then(function (result) {
      if (submit) submit.disabled = false;
      if (result.ok) {
        if (message) { message.textContent = "已创建：" + name; message.className = "form-msg is-ok"; }
        toast("预算「" + name + "」创建成功");
        var form = byId("budget-form");
        if (form) form.reset();
        var scopeKeyInput = byId("budget-scope-key");
        if (scopeKeyInput) scopeKeyInput.value = "*";
        loadBudgets();
      } else {
        if (message) { message.textContent = "创建失败：" + str(result.error, "未知错误"); message.className = "form-msg is-err"; }
        toast("创建预算失败：" + str(result.error, "未知错误"), "error");
      }
    });
  }

  function evaluateBudgets() {
    var button = byId("budget-evaluate");
    if (button) button.disabled = true;
    var message = byId("budget-form-msg");
    if (message) { message.textContent = "评估中…"; message.className = "form-msg"; }
    request(API_PATHS.budgetEvaluate, { method: "POST" }).then(function (result) {
      if (button) button.disabled = false;
      if (result.ok && isObj(result.data)) {
        var triggered = arr(result.data.triggered).length;
        var evaluated = num(result.data.evaluated, 0);
        if (message) {
          message.textContent = "已评估 " + evaluated + " 个预算，触发 " + triggered + " 条告警";
          message.className = triggered ? "form-msg is-err" : "form-msg is-ok";
        }
        toast("预算评估完成：触发 " + triggered + " 条告警", triggered ? "warn" : "info");
        loadBudgets();
      } else {
        if (message) { message.textContent = "评估失败：" + str(result.error, "未知错误"); message.className = "form-msg is-err"; }
        toast("预算评估失败：" + str(result.error, "未知错误"), "error");
      }
    });
  }

  function toggleBudget(id, enabled) {
    request(fillPath(API_PATHS.budgetOne, { id: id }), { method: "PATCH", body: { enabled: enabled } }).then(function (result) {
      if (result.ok) {
        toast("预算已" + (enabled ? "启用" : "停用"));
        loadBudgets();
      } else {
        toast("操作失败：" + str(result.error, "未知错误"), "error");
      }
    });
  }

  function deleteBudget(id) {
    request(fillPath(API_PATHS.budgetOne, { id: id }), { method: "DELETE" }).then(function (result) {
      if (result.ok) {
        toast("预算已删除");
        loadBudgets();
      } else {
        toast("删除失败：" + str(result.error, "未知错误"), "error");
      }
    });
  }

  /* ================================================================ 5. ANOMALIES */

  function loadAnomalies() {
    var query = { limit: 50, status: state.anomalyStatusFilter };
    Promise.all([getJSON(API_PATHS.anomalies, query), getJSON(API_PATHS.anomaliesSummary)]).then(function (results) {
      var listResult = results[0] || {};
      var summaryResult = results[1] || {};
      if (listResult.ok && isObj(listResult.data)) {
        state.anomalies = arr(listResult.data.items);
      } else {
        state.anomalies = arr(obj(obj(state.overview).anomalies).recent);
      }
      if (summaryResult.ok && isObj(summaryResult.data)) {
        state.anomaliesSummary = summaryResult.data;
      } else if (!state.anomaliesSummary) {
        state.anomaliesSummary = obj(obj(state.overview).anomalies).summary;
      }
      renderAnomalies();
    });
  }

  function renderAnomalies() {
    var summary = obj(state.anomaliesSummary);
    var host = byId("anomaly-summary");
    if (host) {
      if (!Object.keys(summary).length) {
        host.innerHTML = '<p class="empty">暂无异常。</p>';
      } else {
        var byReason = obj(summary.by_reason);
        var bySeverity = obj(summary.by_severity);
        var reasonCells = Object.keys(ANOMALY_REASON_LABELS)
          .filter(function (key) { return num(byReason[key], 0) > 0; })
          .map(function (key) {
            return (
              '<div class="summary-cell is-warn"><span class="sc-label">' + esc(ANOMALY_REASON_LABELS[key]) +
              '</span><span class="sc-value">' + intText(byReason[key]) + "</span></div>"
            );
          })
          .join("");
        host.innerHTML =
          '<div class="summary-cell"><span class="sc-label">异常总数</span><span class="sc-value">' +
          intText(summary.total_events) + "</span></div>" +
          '<div class="summary-cell is-warn"><span class="sc-label">待处理</span><span class="sc-value">' +
          intText(summary.open_events) + "</span></div>" +
          '<div class="summary-cell is-bad"><span class="sc-label">预估浪费</span><span class="sc-value">' +
          money(summary.estimated_waste_usd) + "</span></div>" +
          '<div class="summary-cell"><span class="sc-label">致命</span><span class="sc-value">' +
          intText(bySeverity.critical) + "</span></div>" +
          reasonCells;
      }
    }
    setText(
      "anomaly-note",
      summary.last_detected_at
        ? "最近检测 " + timeLabel(summary.last_detected_at, true)
        : "尚未运行检测"
    );

    var cards = byId("anomaly-cards");
    if (!cards) return;
    var items = arr(state.anomalies);
    if (!items.length) {
      cards.innerHTML = '<p class="empty">暂无异常事件。点击「立即检测」运行一次检测。</p>';
      return;
    }
    cards.innerHTML = items.map(renderAnomalyCard).join("");
  }

  function renderAnomalyCard(raw) {
    var item = obj(raw);
    var reason = str(item.reason, "unknown");
    var label = str(item.reason_label, ANOMALY_REASON_LABELS[reason] || ANOMALY_REASON_LABELS.unknown);
    var severity = str(item.severity, "low");
    var status = str(item.status, "open");
    var id = num(item.id, 0);
    var evidence = obj(item.evidence);
    var evidenceRows = Object.keys(evidence)
      .slice(0, 10)
      .map(function (key) {
        var value = evidence[key];
        var text;
        if (typeof value === "number") {
          text = Math.abs(value) >= 1000 ? intText(value) : nf1.format(value);
        } else if (typeof value === "boolean") {
          text = value ? "是" : "否";
        } else if (value === null || value === undefined) {
          text = "—";
        } else if (typeof value === "object") {
          try {
            text = JSON.stringify(value);
          } catch (err) {
            text = "—";
          }
        } else {
          text = String(value);
        }
        return '<div class="evidence-row"><dt>' + esc(key) + "</dt><dd>" + esc(text) + "</dd></div>";
      })
      .join("");

    var statusButtons = ["open", "acknowledged", "resolved"]
      .map(function (candidate) {
        var active = candidate === status;
        return (
          '<button class="btn btn-sm ' + (active ? "btn-primary" : "btn-ghost") + '" ' +
          'data-anomaly-status="' + candidate + '" data-anomaly-id="' + id + '"' +
          (active ? " disabled" : "") + ">" + esc(ANOMALY_STATUS_LABELS[candidate]) + "</button>"
        );
      })
      .join("");

    return (
      '<article class="anomaly-card" data-severity="' + esc(severity) + '">' +
      '<div class="ac-head"><div class="ac-title">' +
      '<span class="ac-reason">' + esc(label) +
      '<span class="badge" data-tone="' + esc(severity) + '">' + esc(str(SEVERITY_LABELS[severity], severity)) + "</span></span>" +
      '<span class="ac-sub">' + esc(str(item.agent, "—")) + " · " + esc(str(item.project, "—")) +
      " · " + esc(str(item.model, "—")) + " · " + esc(timeLabel(item.detected_at, true)) + "</span>" +
      "</div>" +
      '<div class="ac-badges"><span class="badge" data-tone="' + esc(status) + '">' +
      esc(str(ANOMALY_STATUS_LABELS[status], status)) + "</span></div></div>" +
      (item.reason_detail ? '<p class="ac-detail">' + esc(item.reason_detail) + "</p>" : "") +
      '<div class="ac-metrics">' +
      '<div class="ac-metric"><span class="am-label">观测值</span><span class="am-value">' +
      intText(item.observed) + "</span></div>" +
      '<div class="ac-metric"><span class="am-label">基线</span><span class="am-value">' +
      intText(item.baseline) + "</span></div>" +
      '<div class="ac-metric"><span class="am-label">z-score</span><span class="am-value">' +
      nf1.format(num(item.score, 0)) + "</span></div>" +
      '<div class="ac-metric"><span class="am-label">倍率</span><span class="am-value">' +
      nf1.format(num(item.ratio, 0)) + "x</span></div>" +
      '<div class="ac-metric"><span class="am-label">浪费成本</span><span class="am-value">' +
      money(item.cost_usd) + "</span></div>" +
      "</div>" +
      (evidenceRows ? '<div class="evidence"><div class="evidence-title">证据</div><dl class="evidence-list">' + evidenceRows + "</dl></div>" : "") +
      '<div class="btn-row">' + statusButtons + "</div>" +
      "</article>"
    );
  }

  function detectAnomalies() {
    var button = byId("anomaly-detect");
    if (button) button.disabled = true;
    var lookback = num((byId("anomaly-lookback") || {}).value, 60);
    request(withQuery(API_PATHS.anomaliesDetect, { lookback_minutes: lookback }), { method: "POST" }).then(function (result) {
      if (button) button.disabled = false;
      if (result.ok && isObj(result.data)) {
        var found = arr(result.data.detected).length;
        toast("检测完成：扫描 " + intText(result.data.scanned_agents) + " 个 agent，发现 " + found + " 个异常", found ? "warn" : "info");
        loadAnomalies();
      } else {
        toast("检测失败：" + str(result.error, "未知错误"), "error");
      }
    });
  }

  function patchAnomaly(id, status) {
    request(fillPath(API_PATHS.anomalyOne, { id: id }), { method: "PATCH", body: { status: status } }).then(function (result) {
      if (result.ok) {
        toast("异常状态已更新为「" + str(ANOMALY_STATUS_LABELS[status], status) + "」");
        loadAnomalies();
      } else {
        toast("更新失败：" + str(result.error, "未知错误"), "error");
      }
    });
  }

  /* ================================================================ POLL LOOP */

  function refreshOverview() {
    if (state.inFlight) return;
    state.inFlight = true;
    setConnection("loading", "刷新中…");
    getJSON(API_PATHS.overview, { days: 7 }).then(function (result) {
      state.inFlight = false;
      state.lastPollAt = Date.now();
      if (result.ok && isObj(result.data)) {
        state.overview = result.data;
        state.overviewError = null;
        setConnection("ok", "已连接 · " + POLL_MS / 1000 + "s 轮询");
        if (state.tab === "overview") renderOverview();
        /* keep the other tabs' fallbacks fresh without extra requests */
        if (state.tab === "dimensions" && !state.dimensionRows.length) renderDimensionTable();
      } else {
        state.overviewError = result.error || "接口不可用";
        setConnection("error", "离线 · " + state.overviewError);
        if (!state.overview) {
          renderOffline();
        }
      }
    });
  }

  function renderOffline() {
    var ids = [
      "kpi-today-cost", "kpi-week-cost", "kpi-month-cost", "kpi-burn-rate", "kpi-cache-hit", "kpi-active-agents",
    ];
    ids.forEach(function (id) { setText(id, "—"); });
    setText("overview-generated", "后端不可用，等待重连…");
    var sparkWrap = byId("sparkline-wrap");
    if (sparkWrap) sparkWrap.classList.add("is-empty");
    var dailySvg = byId("daily-chart");
    if (dailySvg && dailySvg.parentNode) dailySvg.parentNode.classList.add("is-empty");
  }

  function refreshActiveTab() {
    refreshOverview();
    if (state.tab === "dimensions") loadDimensions();
    else if (state.tab === "trends") loadTrends();
    else if (state.tab === "budgets") loadBudgets();
    else if (state.tab === "anomalies") loadAnomalies();
  }

  /* ==================================================================== WIRING */

  function wireTabs() {
    var nav = byId("tabs");
    if (nav) {
      nav.addEventListener("click", function (event) {
        var target = event.target;
        while (target && target !== nav && !target.getAttribute("data-tab")) target = target.parentNode;
        if (!target || target === nav) return;
        switchTab(target.getAttribute("data-tab"));
      });
    }
    document.addEventListener("click", function (event) {
      var link = event.target;
      while (link && link !== document.body && !link.getAttribute("data-goto")) link = link.parentNode;
      if (!link || link === document.body) return;
      event.preventDefault();
      switchTab(link.getAttribute("data-goto"));
    });
  }

  function wireDimension() {
    var switcher = byId("dimension-switch");
    if (switcher) {
      switcher.addEventListener("click", function (event) {
        var target = event.target;
        while (target && target !== switcher && !target.getAttribute("data-dimension")) target = target.parentNode;
        if (!target || target === switcher) return;
        state.dimension = target.getAttribute("data-dimension");
        Array.prototype.forEach.call(switcher.querySelectorAll(".seg"), function (seg) {
          seg.classList.toggle("is-active", seg === target);
        });
        loadDimensions();
      });
    }
    var windowSelect = byId("dimension-window");
    if (windowSelect) {
      windowSelect.addEventListener("change", function () {
        state.dimensionWindowHours = num(windowSelect.value, 168);
        loadDimensions();
      });
    }
    var table = byId("dimension-table");
    if (table) {
      table.addEventListener("click", function (event) {
        var th = event.target;
        while (th && th !== table && !th.getAttribute("data-sort")) th = th.parentNode;
        if (!th || th === table) return;
        var key = th.getAttribute("data-sort");
        if (state.dimensionSort.key === key) {
          state.dimensionSort.dir = state.dimensionSort.dir === "desc" ? "asc" : "desc";
        } else {
          state.dimensionSort.key = key;
          state.dimensionSort.dir = key === "label" ? "asc" : "desc";
        }
        renderDimensionTable();
      });
    }
  }

  function wireTrends() {
    var periodSwitcher = byId("trend-period");
    if (periodSwitcher) {
      periodSwitcher.addEventListener("click", function (event) {
        var target = event.target;
        while (target && target !== periodSwitcher && !target.getAttribute("data-period")) target = target.parentNode;
        if (!target || target === periodSwitcher) return;
        state.trendPeriod = target.getAttribute("data-period");
        Array.prototype.forEach.call(periodSwitcher.querySelectorAll(".seg"), function (seg) {
          seg.classList.toggle("is-active", seg === target);
        });
        loadTrends();
      });
    }
    var agentSelect = byId("trend-agent");
    if (agentSelect) {
      agentSelect.addEventListener("change", function () {
        state.trendAgent = agentSelect.value;
        loadTrends();
      });
    }
    var modelSelect = byId("trend-model");
    if (modelSelect) {
      modelSelect.addEventListener("change", function () {
        state.trendModel = modelSelect.value;
        loadTrends();
      });
    }
    var metricSelect = byId("trend-metric");
    if (metricSelect) {
      metricSelect.addEventListener("change", function () {
        state.trendMetric = metricSelect.value;
        renderTrends();
      });
    }
    var exportButton = byId("trend-export");
    if (exportButton) exportButton.addEventListener("click", exportCsv);
  }

  function wireBudgets() {
    var form = byId("budget-form");
    if (form) form.addEventListener("submit", submitBudget);
    var scopeSelect = byId("budget-scope");
    if (scopeSelect) {
      scopeSelect.addEventListener("change", function () {
        var scopeKeyInput = byId("budget-scope-key");
        if (!scopeKeyInput) return;
        if (scopeSelect.value === "global") {
          scopeKeyInput.value = "*";
          scopeKeyInput.disabled = true;
        } else {
          if (scopeKeyInput.value === "*") scopeKeyInput.value = "";
          scopeKeyInput.disabled = false;
        }
      });
    }
    var evaluateButton = byId("budget-evaluate");
    if (evaluateButton) evaluateButton.addEventListener("click", evaluateBudgets);

    var cards = byId("budget-cards");
    if (cards) {
      cards.addEventListener("click", function (event) {
        var node = event.target;
        while (node && node !== cards && !node.getAttribute("data-budget-toggle") && !node.getAttribute("data-budget-delete")) {
          node = node.parentNode;
        }
        if (!node || node === cards) return;
        var toggleId = node.getAttribute("data-budget-toggle");
        if (toggleId) {
          toggleBudget(num(toggleId, 0), node.getAttribute("data-enabled") !== "1");
          return;
        }
        var deleteId = node.getAttribute("data-budget-delete");
        if (deleteId) deleteBudget(num(deleteId, 0));
      });
    }
  }

  function wireAnomalies() {
    var detectButton = byId("anomaly-detect");
    if (detectButton) detectButton.addEventListener("click", detectAnomalies);
    var statusFilter = byId("anomaly-status-filter");
    if (statusFilter) {
      statusFilter.addEventListener("change", function () {
        state.anomalyStatusFilter = statusFilter.value;
        loadAnomalies();
      });
    }
    var cards = byId("anomaly-cards");
    if (cards) {
      cards.addEventListener("click", function (event) {
        var node = event.target;
        while (node && node !== cards && !node.getAttribute("data-anomaly-status")) node = node.parentNode;
        if (!node || node === cards) return;
        patchAnomaly(num(node.getAttribute("data-anomaly-id"), 0), node.getAttribute("data-anomaly-status"));
      });
    }
  }

  function wireGlobal() {
    var refreshButton = byId("refresh-all");
    if (refreshButton) refreshButton.addEventListener("click", refreshActiveTab);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) refreshOverview();
    });
  }

  function startPolling() {
    if (state.pollTimer) window.clearInterval(state.pollTimer);
    state.pollTimer = window.setInterval(function () {
      if (document.hidden) return;
      refreshOverview();
    }, POLL_MS);
  }

  function init() {
    tickClock();
    window.setInterval(tickClock, 1000);
    wireTabs();
    wireDimension();
    wireTrends();
    wireBudgets();
    wireAnomalies();
    wireGlobal();
    switchTab("overview");
    refreshOverview();
    startPolling();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  /* Expose a tiny surface for manual poking in the devtools console. */
  window.TokenLensApp = {
    state: state,
    API_PATHS: API_PATHS,
    refresh: refreshActiveTab,
    switchTab: switchTab,
    formatters: { money: money, int: intText, pct: pctText },
  };
})();
