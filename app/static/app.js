"use strict";

const $ = (id) => document.getElementById(id);
const form = $("ask-form");
const questionEl = $("question");
const askButton = $("ask-button");
const resetButton = $("reset");
const transcriptEl = $("transcript");

/** Text-only conversation history sent back with each question. */
const history = [];
/** Every chart on the page, so a resize can redraw them all. */
const charts = [];

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

/* ------------------------------------------------------------ agent status */

(async function checkAgent() {
  try {
    const data = await (await fetch("/api/agent/status")).json();
    const status = $("agent-status");
    if (data.available) {
      status.textContent = `agent ready — ${data.backend} · ${data.model}`;
    } else {
      status.className = "agent-status bad";
      status.textContent = (data.backend ? `${data.backend}: ` : "") +
        (data.reason || "agent unavailable");
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

resetButton.addEventListener("click", () => {
  history.length = 0;
  charts.length = 0;
  transcriptEl.replaceChildren();
  resetButton.hidden = true;
  $("examples").hidden = false;
  questionEl.focus();
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

/* ------------------------------------------------------------------ chart */

const SVG_NS = "http://www.w3.org/2000/svg";

function svg(tag, attrs = {}, text) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

/* A line, not bars: fares occupy a narrow band well above zero, and a bar
   chart truncated to that band overstates the differences. A line carries no
   zero-baseline promise, so it can show the real spread honestly. */
function drawChart(chart, points, tip) {
  if (points.length < 2) return;

  const width = chart.parentElement.clientWidth || 640;
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
    svg("text", {
      class: "chart-min-label",
      x: x(minIndex) + (labelRight ? 10 : -10),
      y: y(min) - 9,
      "text-anchor": labelRight ? "start" : "end",
    }, `cheapest ${money(min, points[0].currency)}`)
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

  const cursor = svg("g", {});
  cursor.setAttribute("visibility", "hidden");
  const cursorLine = svg("line", { class: "chart-cursor", y1: pad.top, y2: pad.top + plotH });
  const cursorDot = svg("circle", { class: "chart-cursor-dot", r: 4 });
  cursor.append(cursorLine, cursorDot);
  chart.appendChild(cursor);

  const hit = svg("rect", { x: pad.left, y: pad.top, width: plotW, height: plotH, fill: "transparent" });
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
    tip.style.left = `${x(i) / scale}px`;
    tip.style.top = `${y(point.price) / scale - 10}px`;
  });
  hit.addEventListener("mouseleave", () => {
    cursor.setAttribute("visibility", "hidden");
    tip.hidden = true;
  });
  chart.appendChild(hit);
}

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => charts.forEach((c) => drawChart(c.svg, c.points, c.tip)), 150);
});

/* ---------------------------------------------------------------- one turn */

function newTurn(question) {
  const turn = el("div", "turn");
  turn.appendChild(el("div", "bubble-user", question));

  const activity = el("section", "activity");
  const head = el("div", "activity-head");
  head.appendChild(el("h2", null, "What the agent is doing"));
  const toggle = el("button", "linkish", "hide");
  toggle.type = "button";
  head.appendChild(toggle);
  activity.appendChild(head);

  const log = el("ol", "activity-log");
  activity.appendChild(log);
  toggle.addEventListener("click", () => {
    const hidden = log.hasAttribute("hidden");
    log.toggleAttribute("hidden", !hidden);
    toggle.textContent = hidden ? "hide" : "show";
  });

  const progressWrap = el("div", "progress-wrap");
  progressWrap.hidden = true;
  const bar = el("div", "progress-bar");
  const fill = el("div", "progress-fill");
  bar.appendChild(fill);
  const progressLabel = el("span", "progress-label");
  progressWrap.append(bar, progressLabel);
  activity.appendChild(progressWrap);

  const answer = el("section", "answer");
  answer.hidden = true;
  const results = el("section", "results");
  const calendar = el("section", "calendar-section");
  calendar.hidden = true;

  turn.append(activity, answer, results, calendar);
  transcriptEl.appendChild(turn);
  turn.scrollIntoView({ behavior: "smooth", block: "nearest" });

  return { turn, log, progressWrap, fill, progressLabel, answer, results, calendar,
           answerText: [] };
}

function logLine(ui, text, kind) {
  ui.log.appendChild(el("li", kind, text));
}

/* --------------------------------------------------------------- rendering */

function renderDeals(ui, result) {
  ui.results.replaceChildren();
  (result.deals || []).forEach((deal) => {
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
          deal.trip_days ? ` · ${deal.trip_days} days` : ""}`
      : shortDate(deal.departure_date);
    middle.appendChild(el("div", "deal-dates", dates));

    if (deal.itinerary) {
      middle.appendChild(el("div", "deal-flights", deal.itinerary.slices
        .map((s) => s.legs.map((l) => `${l.airline_code}${l.flight_number}`).join(" "))
        .join(" / ")));
    }
    card.appendChild(middle);

    const price = el("div", "deal-price", money(deal.grid_price, deal.currency));
    if (deal.savings_vs_median > 0) {
      price.appendChild(el("span", "deal-save",
        `${money(deal.savings_vs_median, deal.currency)} under median`));
    }
    card.appendChild(price);
    ui.results.appendChild(card);
  });
}

function renderCalendar(ui, result) {
  const cells = result.calendar || [];
  if (cells.length < 2) return;

  const byDate = new Map();
  cells.forEach((cell) => {
    const existing = byDate.get(cell.departure_date);
    if (!existing || cell.price < existing.price) byDate.set(cell.departure_date, cell);
  });
  const points = [...byDate.values()].sort((a, b) =>
    a.departure_date < b.departure_date ? -1 : 1);

  const prices = points.map((p) => p.price);
  const currency = points[0].currency;

  ui.calendar.replaceChildren();
  ui.calendar.appendChild(el("h2", null, "Price calendar"));
  ui.calendar.appendChild(el("p", "muted",
    `Cheapest fare per departure date across ${points.length} dates. ` +
    `${money(Math.min(...prices), currency)} to ${money(Math.max(...prices), currency)}.`));

  const wrap = el("div", "chart-wrap");
  const chart = document.createElementNS(SVG_NS, "svg");
  chart.setAttribute("class", "chart");
  chart.setAttribute("role", "img");
  chart.setAttribute("aria-label", "Cheapest fare by departure date");
  const tip = el("div", "chart-tip");
  tip.hidden = true;
  wrap.append(chart, tip);
  ui.calendar.appendChild(wrap);
  ui.calendar.hidden = false;

  charts.push({ svg: chart, points, tip });
  drawChart(chart, points, tip);
}

function renderResults(ui, kind, data) {
  if (kind === "deal_search") {
    renderDeals(ui, data);
    renderCalendar(ui, data);
    const stats = data.stats || {};
    if (stats.cache_hits) {
      logLine(ui, `${stats.cache_hits} of ${stats.cache_hits + stats.requests_made} ` +
        `scans served from cache`, "cache");
    }
  } else if (kind === "point_search" && !ui.results.childElementCount) {
    (data.itineraries || []).slice(0, 8).forEach((itinerary, index) => {
      const card = el("article", `deal${index === 0 ? " best" : ""}`);
      card.appendChild(el("div", "deal-rank", `#${index + 1}`));
      const middle = el("div");
      const slice = itinerary.slices[0];
      middle.appendChild(el("div", "deal-route", `${slice.origin} → ${slice.destination}`));
      middle.appendChild(el("div", "deal-dates",
        `${shortDate(slice.departure.slice(0, 10))} · ${itinerary.max_stops} stop(s)`));
      middle.appendChild(el("div", "deal-flights", itinerary.slices
        .map((s) => s.legs.map((l) => `${l.airline_code}${l.flight_number}`).join(" "))
        .join(" / ")));
      card.appendChild(middle);
      card.appendChild(el("div", "deal-price", itinerary.price_display));
      ui.results.appendChild(card);
    });
  }
}

function handleEvent(ui, event) {
  switch (event.type) {
    case "thinking":
      logLine(ui, event.text.split("\n")[0].slice(0, 160), "think");
      break;
    case "tool_call": {
      const label = TOOL_LABELS[event.name];
      logLine(ui, label ? label(event.input || {}) : `Calling ${event.name}`, "tool");
      break;
    }
    case "plan":
      logLine(ui, `Plan: ~${event.estimated_requests} requests across ` +
        `${event.destinations.length} destination(s)`);
      break;
    case "progress": {
      ui.progressWrap.hidden = false;
      const pct = event.total ? Math.round((100 * event.done) / event.total) : 0;
      ui.fill.style.width = `${pct}%`;
      ui.progressLabel.textContent =
        `${event.stage === "scan" ? "scanning dates" : "pricing best options"} ` +
        `${event.done}/${event.total}`;
      break;
    }
    case "text":
      ui.answerText.push(event.text);
      ui.answer.hidden = false;
      ui.answer.replaceChildren(
        ...ui.answerText.join("\n\n").split(/\n\n+/).map((p) => el("p", null, p))
      );
      break;
    case "results":
      renderResults(ui, event.kind, event.data);
      break;
    case "error":
      logLine(ui, event.message, "err");
      ui.error = event.message;
      break;
    default:
      break;
  }
}

/* ------------------------------------------------------------------ submit */

form.addEventListener("submit", async (submitEvent) => {
  submitEvent.preventDefault();
  const question = questionEl.value.trim();
  if (!question) return;

  askButton.disabled = true;
  askButton.textContent = "Searching…";
  questionEl.value = "";
  $("examples").hidden = true;
  resetButton.hidden = false;

  const ui = newTurn(question);

  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // Text-only history: the assistant's summary carries the facts, and
      // replaying tool calls would tie the payload to one provider's shape.
      body: JSON.stringify({ question, history: [...history] }),
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
          handleEvent(ui, JSON.parse(line.slice(6)));
        } catch {
          /* ignore a malformed frame rather than killing the stream */
        }
      }
    }
    ui.progressWrap.hidden = true;

    const answer = ui.answerText.join("\n\n").trim();
    if (answer) {
      history.push({ role: "user", content: question });
      history.push({ role: "assistant", content: answer });
    } else if (ui.error) {
      ui.answer.hidden = false;
      ui.answer.replaceChildren(el("p", null, ui.error));
    }
  } catch (err) {
    ui.answer.hidden = false;
    ui.answer.replaceChildren(el("p", null, `Could not reach the agent: ${err.message}`));
  } finally {
    askButton.disabled = false;
    askButton.textContent = "Send";
    questionEl.focus();
  }
});
