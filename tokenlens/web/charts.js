/* ======================================================================================
   TokenLens — hand-written SVG chart helpers
   --------------------------------------------------------------------------------------
   No chart library, no CDN, no build step. Every function is defensive: bad input renders
   an empty state instead of throwing, so a partially populated API can never blank the
   whole dashboard.

   Public surface (window.TLCharts):
     sparkline(svg, values, opts)          area + line, no axes  (KPI / hourly cost)
     lineArea(svg, series, opts)           axes + grid + area + line + hover tooltip
     barChart(svg, series, opts)           axes + grid + bars + hover tooltip
     clear(svg)                            remove rendered children
     ensureViewBox(svg, fallbackW, h)      viewBox that matches the rendered pixel size
   ====================================================================================== */
(function (global) {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";

  /* ------------------------------------------------------------------ tiny DOM helpers */

  function el(name, attrs) {
    var node = document.createElementNS(NS, name);
    if (attrs) {
      for (var key in attrs) {
        if (Object.prototype.hasOwnProperty.call(attrs, key) && attrs[key] !== null && attrs[key] !== undefined) {
          node.setAttribute(key, String(attrs[key]));
        }
      }
    }
    return node;
  }

  function clear(svg) {
    if (!svg) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
  }

  function num(value, fallback) {
    var n = typeof value === "number" ? value : parseFloat(value);
    return isFinite(n) ? n : (fallback === undefined ? 0 : fallback);
  }

  /** Keep the drawing coordinate system equal to the rendered pixel box (no stretching). */
  function ensureViewBox(svg, fallbackW, height) {
    var w = Math.round(num(svg.clientWidth, 0)) || fallbackW || 640;
    var h = Math.round(num(svg.clientHeight, 0)) || height || 180;
    if (w < 40) w = fallbackW || 640;
    svg.setAttribute("viewBox", "0 0 " + w + " " + h);
    svg.setAttribute("preserveAspectRatio", "none");
    return { w: w, h: h };
  }

  /** Unique gradient ids per <svg> so several charts can coexist on one page. */
  function defs(svg, id, stops) {
    var group = el("defs");
    var gradient = el("linearGradient", { id: id, x1: "0", y1: "0", x2: "0", y2: "1" });
    for (var i = 0; i < stops.length; i += 1) {
      gradient.appendChild(
        el("stop", {
          offset: stops[i][0],
          "stop-color": stops[i][1],
          "stop-opacity": stops[i][2] === undefined ? 1 : stops[i][2],
        })
      );
    }
    group.appendChild(gradient);
    svg.appendChild(group);
    return group;
  }

  /** Catmull-Rom-ish smoothing kept simple: straight segments with rounded joins look
      crisper for observability data than overshooting splines. */
  function pathFromPoints(points) {
    if (!points.length) return "";
    var d = "M" + points[0][0].toFixed(2) + " " + points[0][1].toFixed(2);
    for (var i = 1; i < points.length; i += 1) {
      d += "L" + points[i][0].toFixed(2) + " " + points[i][1].toFixed(2);
    }
    return d;
  }

  /** "Nice" upper bound + step for the y axis (1/2/5 * 10^n). */
  function niceScale(maxValue, targetTicks) {
    var max = num(maxValue, 0);
    if (!(max > 0)) max = 1;
    var raw = max / Math.max(1, targetTicks || 4);
    var magnitude = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var residual = raw / magnitude;
    var step;
    if (residual <= 1) step = 1;
    else if (residual <= 2) step = 2;
    else if (residual <= 5) step = 5;
    else step = 10;
    step *= magnitude;
    var top = Math.ceil(max / step) * step;
    if (!(top > 0)) top = step;
    return { max: top, step: step, ticks: Math.round(top / step) };
  }

  /* ------------------------------------------------------------------------ sparkline */

  /**
   * Compact area+line sparkline. `values` is an array of numbers (may contain nulls).
   * opts: { color, height, pad }
   */
  function sparkline(svg, values, opts) {
    opts = opts || {};
    clear(svg);
    if (!svg) return false;
    var list = (values || []).map(function (v) { return num(v, null); }).filter(function (v) { return v !== null; });
    var box = ensureViewBox(svg, 640, opts.height || 180);
    var padX = num(opts.pad, 10);
    var padTop = 12;
    var padBottom = 10;
    if (list.length === 0) return false;
    if (list.length === 1) list = [list[0], list[0]];

    defs(svg, "spark-grad-" + Math.random().toString(36).slice(2, 8), [
      [0, opts.color || "#4de3c1", 0.42],
      [1, opts.color || "#4de3c1", 0.02],
    ]);

    var gradientId = svg.querySelector("defs linearGradient").getAttribute("id");
    var min = Math.min.apply(null, list);
    var max = Math.max.apply(null, list);
    if (max === min) { max = min + 1; }
    var innerW = Math.max(1, box.w - padX * 2);
    var innerH = Math.max(1, box.h - padTop - padBottom);

    var coords = list.map(function (value, index) {
      var x = padX + (innerW * index) / (list.length - 1);
      var y = padTop + innerH - ((value - min) / (max - min)) * innerH;
      return [x, y];
    });

    var areaPath =
      pathFromPoints(coords) +
      "L" + coords[coords.length - 1][0].toFixed(2) + " " + (box.h - padBottom).toFixed(2) +
      "L" + coords[0][0].toFixed(2) + " " + (box.h - padBottom).toFixed(2) + "Z";

    svg.appendChild(el("path", { d: areaPath, fill: "url(#" + gradientId + ")", stroke: "none" }));
    svg.appendChild(el("path", { d: pathFromPoints(coords), fill: "none", stroke: opts.color || "#4de3c1", "stroke-width": 1.8, "stroke-linejoin": "round", "stroke-linecap": "round" }));

    var last = coords[coords.length - 1];
    svg.appendChild(el("circle", { cx: last[0], cy: last[1], r: 2.8, fill: opts.color || "#4de3c1" }));
    return true;
  }

  /* ------------------------------------------------------------------- line + area */

  /**
   * Full line+area chart with y grid, axis labels and an optional hover tooltip.
   *
   * series: [{ label, value }]  (already ordered oldest -> newest)
   * opts:   { formatValue(v), formatLabel(s), tooltipEl, wrapEl, color, height, emptyEl }
   */
  function lineArea(svg, series, opts) {
    opts = opts || {};
    clear(svg);
    if (!svg) return false;
    var rows = (series || []).filter(function (row) { return row && isFinite(num(row.value, NaN)); });
    var box = ensureViewBox(svg, 720, opts.height || 260);
    if (!rows.length) return false;

    var padLeft = 54;
    var padRight = 14;
    var padTop = 14;
    var padBottom = 26;
    var innerW = Math.max(1, box.w - padLeft - padRight);
    var innerH = Math.max(1, box.h - padTop - padBottom);
    var values = rows.map(function (row) { return num(row.value, 0); });
    var scale = niceScale(Math.max.apply(null, values.concat([0])), 4);

    var gradientId = "area-grad-" + Math.random().toString(36).slice(2, 8);
    defs(svg, gradientId, [
      [0, opts.color || "#4de3c1", 0.34],
      [1, opts.color || "#4de3c1", 0.015],
    ]);

    /* horizontal grid + y labels */
    for (var tick = 0; tick <= scale.ticks; tick += 1) {
      var value = scale.step * tick;
      var y = padTop + innerH - (value / scale.max) * innerH;
      svg.appendChild(el("line", { x1: padLeft, y1: y.toFixed(2), x2: padLeft + innerW, y2: y.toFixed(2), class: "grid-line" }));
      var label = el("text", { x: padLeft - 8, y: (y + 3.5).toFixed(2), "text-anchor": "end", class: "axis-label" });
      label.textContent = opts.formatValue ? opts.formatValue(value) : String(Math.round(value));
      svg.appendChild(label);
    }

    var stepX = rows.length > 1 ? innerW / (rows.length - 1) : 0;
    var coords = values.map(function (value, index) {
      return [padLeft + stepX * index, padTop + innerH - (value / scale.max) * innerH];
    });

    /* area */
    var areaPath =
      pathFromPoints(coords) +
      "L" + coords[coords.length - 1][0].toFixed(2) + " " + (padTop + innerH).toFixed(2) +
      "L" + coords[0][0].toFixed(2) + " " + (padTop + innerH).toFixed(2) + "Z";
    svg.appendChild(el("path", { d: areaPath, fill: "url(#" + gradientId + ")" }));
    svg.appendChild(el("path", { d: pathFromPoints(coords), class: "line" }));

    /* x labels: at most 7, always including first and last */
    var maxLabels = Math.max(2, Math.min(7, Math.floor(innerW / 84)));
    var stride = Math.max(1, Math.ceil(rows.length / maxLabels));
    for (var i = 0; i < rows.length; i += stride) {
      var text = el("text", {
        x: coords[i][0].toFixed(2),
        y: (box.h - 7).toFixed(2),
        "text-anchor": i === 0 ? "start" : (i === rows.length - 1 ? "end" : "middle"),
        class: "axis-label",
      });
      text.textContent = opts.formatLabel ? opts.formatLabel(rows[i].label) : String(rows[i].label || "");
      svg.appendChild(text);
    }

    /* single data point still deserves a dot */
    if (rows.length === 1) {
      svg.appendChild(el("circle", { cx: coords[0][0], cy: coords[0][1], r: 3, class: "dot" }));
    }

    /* hover layer */
    var hoverLine = el("line", { class: "hover-line", x1: padLeft, y1: padTop, x2: padLeft, y2: padTop + innerH });
    svg.appendChild(hoverLine);
    var hoverDot = el("circle", { r: 3.5, class: "dot", opacity: 0 });
    svg.appendChild(hoverDot);

    var wrap = opts.wrapEl;
    var tooltip = opts.tooltipEl;
    if (wrap && tooltip && rows.length > 0) {
      var overlay = el("rect", {
        x: padLeft, y: padTop, width: innerW, height: innerH, class: "hit",
      });
      svg.appendChild(overlay);
      overlay.addEventListener("mousemove", function (event) {
        var rect = svg.getBoundingClientRect();
        var relative = rect.width ? (event.clientX - rect.left) * (box.w / rect.width) : 0;
        var index = stepX > 0 ? Math.round((relative - padLeft) / stepX) : 0;
        index = Math.max(0, Math.min(rows.length - 1, index));
        var point = coords[index];
        hoverLine.setAttribute("x1", point[0]);
        hoverLine.setAttribute("x2", point[0]);
        hoverLine.style.opacity = "1";
        hoverDot.setAttribute("cx", point[0]);
        hoverDot.setAttribute("cy", point[1]);
        hoverDot.style.opacity = "1";
        tooltip.hidden = false;
        tooltip.innerHTML =
          "<div>" + escapeHtml(rows[index].label || "") + "</div>" +
          "<div><b>" + escapeHtml(opts.formatValue ? opts.formatValue(rows[index].value) : String(rows[index].value)) + "</b>" +
          (rows[index].hint ? " · " + escapeHtml(rows[index].hint) : "") + "</div>";
        var left = rect.width ? (point[0] / box.w) * rect.width : 0;
        var top = rect.width ? (point[1] / box.h) * rect.height : 0;
        tooltip.style.left = Math.max(58, Math.min(rect.width - 58, left)) + "px";
        tooltip.style.top = Math.max(30, top) + "px";
      });
      overlay.addEventListener("mouseleave", function () {
        hoverLine.style.opacity = "0";
        hoverDot.style.opacity = "0";
        tooltip.hidden = true;
      });
    }
    return true;
  }

  /* ------------------------------------------------------------------------ bar chart */

  /** Bar chart sharing the lineArea axes conventions. */
  function barChart(svg, series, opts) {
    opts = opts || {};
    clear(svg);
    if (!svg) return false;
    var rows = (series || []).filter(function (row) { return row && isFinite(num(row.value, NaN)); });
    var box = ensureViewBox(svg, 640, opts.height || 180);
    if (!rows.length) return false;

    var padLeft = 52;
    var padRight = 12;
    var padTop = 14;
    var padBottom = 24;
    var innerW = Math.max(1, box.w - padLeft - padRight);
    var innerH = Math.max(1, box.h - padTop - padBottom);
    var values = rows.map(function (row) { return num(row.value, 0); });
    var scale = niceScale(Math.max.apply(null, values.concat([0])), 3);
    var gradientId = "bar-grad-" + Math.random().toString(36).slice(2, 8);
    defs(svg, gradientId, [
      [0, opts.color || "#45a7ff", 0.95],
      [1, opts.color || "#45a7ff", 0.35],
    ]);

    for (var tick = 0; tick <= scale.ticks; tick += 1) {
      var value = scale.step * tick;
      var y = padTop + innerH - (value / scale.max) * innerH;
      svg.appendChild(el("line", { x1: padLeft, y1: y.toFixed(2), x2: padLeft + innerW, y2: y.toFixed(2), class: "grid-line" }));
      var label = el("text", { x: padLeft - 8, y: (y + 3.5).toFixed(2), "text-anchor": "end", class: "axis-label" });
      label.textContent = opts.formatValue ? opts.formatValue(value) : String(Math.round(value));
      svg.appendChild(label);
    }

    var slot = innerW / rows.length;
    var barWidth = Math.max(3, Math.min(34, slot * 0.62));
    var maxLabelCount = Math.max(2, Math.min(8, Math.floor(innerW / 70)));
    var stride = Math.max(1, Math.ceil(rows.length / maxLabelCount));

    rows.forEach(function (row, index) {
      var height = Math.max(0, (num(row.value, 0) / scale.max) * innerH);
      var x = padLeft + slot * index + (slot - barWidth) / 2;
      var y = padTop + innerH - height;
      var rect = el("rect", {
        x: x.toFixed(2), y: y.toFixed(2), width: barWidth.toFixed(2), height: height.toFixed(2),
        rx: Math.min(3, barWidth / 2), fill: "url(#" + gradientId + ")",
      });
      var title = el("title");
      title.textContent = (row.label || "") + " · " + (opts.formatValue ? opts.formatValue(row.value) : row.value);
      rect.appendChild(title);
      svg.appendChild(rect);

      if (index % stride === 0) {
        var text = el("text", {
          x: (padLeft + slot * index + slot / 2).toFixed(2),
          y: (box.h - 7).toFixed(2),
          "text-anchor": "middle",
          class: "axis-label",
        });
        text.textContent = opts.formatLabel ? opts.formatLabel(row.label) : String(row.label || "");
        svg.appendChild(text);
      }
    });
    return true;
  }

  function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  global.TLCharts = {
    sparkline: sparkline,
    lineArea: lineArea,
    barChart: barChart,
    clear: clear,
    ensureViewBox: ensureViewBox,
    escapeHtml: escapeHtml,
    niceScale: niceScale,
  };
})(window);
