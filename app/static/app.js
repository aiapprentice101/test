"use strict";

const $ = (id) => document.getElementById(id);
const form = $("ask-form");
const questionEl = $("question");
const askButton = $("ask-button");
const activityEl = $("activity");
const logEl = $("activity-log");
const answerEl = $("answer");
const resultsEl = $("results");
const calendarSection = $("calendar-section");

/* ------------------------------------------------------------------ utils */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function money(amount, currency) {
  if (amount == null) return "—";
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: currency || "USD",
      maximumFractionDigits: 0,
    }).format(amount);
  } catch {
    return `${currency || ""} ${Math.round(amount).toLocaleString()}`;
  }
}

function shortDate(iso) {
  if (!iso) return "";
  const d = new Date(`${iso}T00:00:00`);
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

function logLine(text, kind) {
  const li = el("li", kind, text);
  logEl.appendChild(li);
  return li;
}

/* ------------------------------------------------------------ agent status */

(async function checkAgent() {
  try {
    const res = await fetch("/api/agent/status");
    const data = await res.json();
    const status = $("agent-status");
    if (data.available) {
      status.textContent = `agent ready — ${data.backend} · ${data.model}`;
    } else {
      status.className = "agent-status bad";
      const where = data.backend ? `${data.backend}: ` : "";
      status.textContent = where + (data.reason || "agent unavailable");
    }
  } catch {
    /* leave the status line blank */
  }
})();

$("examples").addEventListener("click", (event) => {
  const q = event.target.dataset?.q;
  if (q) {
    questionEl.value = q;
    questionEl.focus();
  }
});

$("toggle-activity").addEventListener("click", (event) => {
  const hidden = logEl.hasAttribute("hidden");
  logEl.toggleAttribute("hidden", !hidden);
  event.target.textContent = hidden ? "hide" : "show";
});

/* ------------------------------------------------------------- tool labels */

const TOOL_LABELS = {
  find_airports: (i) => `Looking up airports for "${i.query}"`,
  list_destination_regions: () => "Checking available regions",
  estimate_search_cost: (i) => `Costing the search${describeScope(i)}`,
  find_best_fares: (i) => `Searching fares${describeScope(i)}`,
  search_one_date: (i) => `Checking ${i.origin} → ${i.destination} on ${i.departure_date}`,
};

function describeScope(input) {
  const to = input.destination_region || input.destinations;
  const bits = [];
  if (input.origins && to) bits.push(`${input.origins} → ${to}`);
  if (input.depart_from && input.depart_to) {
    bits.push(`departing ${shortDate(input.depart_from)}–${shortDate(input.depart_to)}`);
  }
  return bits.length ? `: ${bits.join(", ")}` : "";
}

/* ---------------------------------------------------------------- results */

function renderDeals(result) {
  resultsEl.replaceChildren();
  const deals = result.deals || [];
  if (!deals.length) return;

  const heading = el("h2", "sr-only", "Results");
  resultsEl.appendChild(heading);

  deals.forEach((deal) => {
    const isBest = deal.rank === 1;
    const card = el("article", `deal${isBest ? " best" : ""}`);
    card.appendChild(el("div", "deal-rank", `#${deal.rank}`));

    const middle = el("div");
    const route = el("div", "deal-route");
    route.append(`${deal.origin} → ${deal.destination}`);
    if (isBest) route.appendChild(el("span", "badge", "Best"));
    if (deal.refine_note) route.appendChild(el("span", "badge warn", "estimate"));
    middle.appendChild(route);

    const dates = deal.return_date
      ? `${shortDate(deal.departure_date)} → ${shortDate(deal.return_date)}${
          deal.trip_days ? ` · ${deal.trip_days} days` : ""
        }`
      : shortDate(deal.departure_date);
    middle.appendChild(el("div", "deal-dates", dates));

    if (deal.itinerary) {
      const flights = deal.itinerary.slices
        .map((s) => s.legs.map((l) => `${l.airline_code}${l.flight_number}`).join(" "))
        .join(" / ");
      middle.appendChild(el("div", "deal-flights", flights));
    }
    card.appendChild(middle);

    const price = el("div", "deal-price", money(deal.grid_price, deal.currency));
    if (deal.savings_vs_median > 0) {
      price.appendChild(
        el("span", "deal-save", `${money(deal.savings_vs_median, deal.currency)} under median`)
      );
    }
    card.appendChild(price);
    resultsEl.appendChild(card);
  });
}

const SVG_NS = "http://www.w3.org/2000/svg";
let chartPoints = [];

function svg(tag, attrs = {}, text) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

/* A line, not bars: fares occupy a narrow band well above zero, and a bar
   chart truncated to that band overstates the differences. A line carries no
   zero-baseline promise, so it can show the real spread honestly. */
function drawChart() {
  const chart = $("chart");
  const points = chartPoints;
  if (points.length < 2) return;

  const width = $("chart-wrap").clientWidth || 640;
  const height = 200;
  const pad = { top: 18, right: 18, bottom: 24, left: 56 };
  const plotW = Math.max(60, width - pad.left - pad.right);
  const plotH = height - pad.top - pad.bottom;

  const prices = points.map((p) => p.price);
  const min = Math.min(...prices);
  const max = Math.max(...prices);
  const headroom = (max - min) * 0.15 || max * 0.02;
  const lo = min - headroom;
  const hi = max + headroom;

  const x = (i) => pad.left + (points.length === 1 ? plotW / 2 : (i * plotW) / (points.length - 1));
  const y = (price) => pad.top + plotH - ((price - lo) / (hi - lo)) * plotH;

  chart.setAttribute("viewBox", `0 0 ${width} ${height}`);
  chart.replaceChildren();

  // Recessive gridlines carrying the price scale.
  const grid = svg("g", { class: "chart-grid" });
  [0, 0.5, 1].forEach((t) => {
    const price = lo + (hi - lo) * (1 - t);
    const gy = pad.top + plotH * t;
    grid.appendChild(svg("line", { x1: pad.left, x2: width - pad.right, y1: gy, y2: gy }));
    grid.appendChild(
      svg("text", { x: pad.left - 8, y: gy + 3, "text-anchor": "end" },
          money(price, points[0].currency))
    );
  });
  chart.appendChild(grid);

  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(p.price)}`).join(" ");
  chart.appendChild(
    svg("path", {
      class: "chart-area",
      d: `${line} L${x(points.length - 1)},${pad.top + plotH} L${x(0)},${pad.top + plotH} Z`,
    })
  );
  chart.appendChild(svg("path", { class: "chart-line", d: line }));

  // Emphasis: the cheapest date is the answer, so mark and label it directly
  // rather than leaving the reader to find it by color alone.
  const minIndex = prices.indexOf(min);
  chart.appendChild(svg("circle", { class: "chart-min-dot", cx: x(minIndex), cy: y(min), r: 5 }));
  const labelRight = x(minIndex) < width - pad.right - 90;
  chart.appendChild(
    svg(
      "text",
      {
        class: "chart-min-label",
        x: x(minIndex) + (labelRight ? 10 : -10),
        y: y(min) - 9,
        "text-anchor": labelRight ? "start" : "end",
      },
      `cheapest ${money(min, points[0].currency)}`
    )
  );

  const axis = svg("g", { class: "chart-axis" });
  const step = Math.ceil(points.length / 6);
  points.forEach((point, i) => {
    if (i % step !== 0 && i !== points.length - 1) return;
    axis.appendChild(
      svg("text", { x: x(i), y: height - 6, "text-anchor": "middle" },
          shortDate(point.departure_date))
    );
  });
  chart.appendChild(axis);

  const cursor = svg("g", { class: "chart-cursor-group" });
  cursor.setAttribute("visibility", "hidden");
  const cursorLine = svg("line", { class: "chart-cursor", y1: pad.top, y2: pad.top + plotH });
  const cursorDot = svg("circle", { class: "chart-cursor-dot", r: 4 });
  cursor.append(cursorLine, cursorDot);
  chart.appendChild(cursor);

  const tip = $("chart-tip");
  const hit = svg("rect", {
    x: pad.left, y: pad.top, width: plotW, height: plotH, fill: "transparent",
  });
  hit.addEventListener("mousemove", (event) => {
    const box = chart.getBoundingClientRect();
    const scale = width / box.width;
    const px = (event.clientX - box.left) * scale;
    const i = Math.max(0, Math.min(points.length - 1,
      Math.round(((px - pad.left) / plotW) * (points.length - 1))));
    const point = points[i];
    cursor.setAttribute("visibility", "visible");
    cursorLine.setAttribute("x1", x(i));
    cursorLine.setAttribute("x2", x(i));
    cursorDot.setAttribute("cx", x(i));
    cursorDot.setAttribute("cy", y(point.price));
    tip.hidden = false;
    tip.textContent =
      `${point.departure_date} · ${point.destination} · ${money(point.price, point.currency)}`;
    tip.style.left = `${(x(i) / scale)}px`;
    tip.style.top = `${(y(point.price) / scale) - 10}px`;
  });
  hit.addEventListener("mouseleave", () => {
    cursor.setAttribute("visibility", "hidden");
    tip.hidden = true;
  });
  chart.appendChild(hit);
}

function renderCalendar(result) {
  const cells = result.calendar || [];
  if (cells.length < 2) {
    calendarSection.hidden = true;
    chartPoints = [];
    return;
  }
  const byDate = new Map();
  cells.forEach((cell) => {
    const existing = byDate.get(cell.departure_date);
    if (!existing || cell.price < existing.price) byDate.set(cell.departure_date, cell);
  });
  chartPoints = [...byDate.values()].sort((a, b) =>
    a.departure_date < b.departure_date ? -1 : 1
  );

  const prices = chartPoints.map((p) => p.price);
  const currency = chartPoints[0].currency;
  $("calendar-caption").textContent =
    `Cheapest fare per departure date across ${chartPoints.length} dates. ` +
    `${money(Math.min(...prices), currency)} to ${money(Math.max(...prices), currency)}.`;
  calendarSection.hidden = false;
  drawChart();
}

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(drawChart, 150);
});

function renderResults(kind, data) {
  if (kind === "deal_search") {
    renderDeals(data);
    renderCalendar(data);
  } else if (kind === "point_search" && !resultsEl.childElementCount) {
    resultsEl.replaceChildren();
    (data.itineraries || []).slice(0, 8).forEach((itinerary, index) => {
      const card = el("article", `deal${index === 0 ? " best" : ""}`);
      card.appendChild(el("div", "deal-rank", `#${index + 1}`));
      const middle = el("div");
      const slice = itinerary.slices[0];
      middle.appendChild(el("div", "deal-route", `${slice.origin} → ${slice.destination}`));
      middle.appendChild(
        el("div", "deal-dates", `${shortDate(slice.departure.slice(0, 10))} · ${itinerary.max_stops} stop(s)`)
      );
      middle.appendChild(
        el("div", "deal-flights", itinerary.slices
          .map((s) => s.legs.map((l) => `${l.airline_code}${l.flight_number}`).join(" "))
          .join(" / "))
      );
      card.appendChild(middle);
      card.appendChild(el("div", "deal-price", itinerary.price_display));
      resultsEl.appendChild(card);
    });
  }
}

/* ----------------------------------------------------------------- stream */

function handleEvent(event, state) {
  switch (event.type) {
    case "thinking":
      logLine(event.text.split("\n")[0].slice(0, 160), "think");
      break;
    case "tool_call": {
      const label = TOOL_LABELS[event.name];
      logLine(label ? label(event.input || {}) : `Calling ${event.name}`, "tool");
      break;
    }
    case "plan":
      logLine(
        `Plan: ~${event.estimated_requests} requests across ${event.destinations.length} destination(s)`
      );
      break;
    case "progress": {
      const wrap = $("progress-wrap");
      wrap.hidden = false;
      const pct = event.total ? Math.round((100 * event.done) / event.total) : 0;
      $("progress-fill").style.width = `${pct}%`;
      $("progress-label").textContent =
        `${event.stage === "scan" ? "scanning dates" : "pricing best options"} ${event.done}/${event.total}`;
      break;
    }
    case "text":
      state.answer.push(event.text);
      answerEl.hidden = false;
      answerEl.replaceChildren(
        ...state.answer.join("\n\n").split(/\n\n+/).map((p) => el("p", null, p))
      );
      break;
    case "results":
      renderResults(event.kind, event.data);
      break;
    case "error":
      logLine(event.message, "err");
      state.error = event.message;
      break;
    default:
      break;
  }
}

form.addEventListener("submit", async (submitEvent) => {
  submitEvent.preventDefault();
  const question = questionEl.value.trim();
  if (!question) return;

  askButton.disabled = true;
  askButton.textContent = "Searching…";
  logEl.replaceChildren();
  answerEl.hidden = true;
  answerEl.replaceChildren();
  resultsEl.replaceChildren();
  calendarSection.hidden = true;
  $("progress-wrap").hidden = true;
  activityEl.hidden = false;
  document.querySelector(".notice")?.remove();

  const state = { answer: [], error: null };

  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    if (!res.ok || !res.body) throw new Error(`server returned ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    // SSE frames are separated by a blank line; a frame can straddle chunks.
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop();
      for (const frame of frames) {
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        try {
          handleEvent(JSON.parse(line.slice(6)), state);
        } catch {
          /* ignore a malformed frame rather than killing the stream */
        }
      }
    }
    $("progress-wrap").hidden = true;

    if (state.error && !state.answer.length) {
      const notice = el("div", "notice", state.error);
      activityEl.after(notice);
    }
  } catch (err) {
    activityEl.after(el("div", "notice", `Could not reach the agent: ${err.message}`));
  } finally {
    askButton.disabled = false;
    askButton.textContent = "Search";
  }
});
