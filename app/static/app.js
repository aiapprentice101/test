"use strict";

const $ = (id) => document.getElementById(id);
const form = $("search-form");
const statusEl = $("status");
const summaryEl = $("summary");
const resultsEl = $("results");

/* ---------------------------------------------------------------- helpers */

const pad = (n) => String(n).padStart(2, "0");

function timeOf(iso) {
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function dayOffset(startIso, endIso) {
  const a = new Date(startIso);
  const b = new Date(endIso);
  const days = Math.round(
    (new Date(b.getFullYear(), b.getMonth(), b.getDate()) -
      new Date(a.getFullYear(), a.getMonth(), a.getDate())) / 86400000
  );
  return days > 0 ? `+${days}` : "";
}

function duration(minutes) {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h ? `${h}h ${m}m` : `${m}m`;
}

function stopsLabel(stops) {
  if (stops === 0) return "Non-stop";
  return stops === 1 ? "1 stop" : `${stops} stops`;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function showStatus(message, { error = false, hint = null } = {}) {
  statusEl.hidden = false;
  statusEl.className = error ? "status error" : "status";
  statusEl.replaceChildren(el("div", null, message));
  if (hint) statusEl.appendChild(el("p", "hint", hint));
}

function clearStatus() {
  statusEl.hidden = true;
  statusEl.replaceChildren();
}

/* -------------------------------------------------------- airport picker */

function attachAutocomplete(inputId, listId) {
  const input = $(inputId);
  const list = $(listId);
  let items = [];
  let active = -1;
  let timer = null;
  // Monotonic request id: a response from an older query (or one that lands
  // after the user dismissed the list) must not reopen the dropdown.
  let seq = 0;

  const close = () => {
    clearTimeout(timer);
    seq += 1;
    list.hidden = true;
    list.replaceChildren();
    input.setAttribute("aria-expanded", "false");
    items = [];
    active = -1;
  };

  const choose = (match) => {
    input.value = match.code;
    input.dataset.code = match.code;
    input.title = match.name;
    close();
  };

  const render = (matches) => {
    items = matches;
    active = -1;
    list.replaceChildren(
      ...matches.map((m, i) => {
        const li = el("li");
        li.setAttribute("role", "option");
        li.dataset.index = String(i);
        li.appendChild(el("span", "code", m.code));
        li.appendChild(el("span", "name", m.name));
        li.addEventListener("mousedown", (e) => {
          e.preventDefault();
          choose(m);
        });
        return li;
      })
    );
    list.hidden = matches.length === 0;
    input.setAttribute("aria-expanded", String(matches.length > 0));
  };

  const highlight = () => {
    [...list.children].forEach((li, i) =>
      li.setAttribute("aria-selected", String(i === active))
    );
  };

  input.addEventListener("input", () => {
    delete input.dataset.code;
    const q = input.value.trim();
    clearTimeout(timer);
    if (q.length < 2) return close();
    const mine = ++seq;
    timer = setTimeout(async () => {
      try {
        const res = await fetch(`/api/airports?q=${encodeURIComponent(q)}&limit=8`);
        if (mine !== seq) return;
        if (!res.ok) return close();
        render(await res.json());
      } catch {
        if (mine === seq) close();
      }
    }, 150);
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") return close();
    if (list.hidden || items.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      active = (active + 1) % items.length;
      highlight();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      active = (active - 1 + items.length) % items.length;
      highlight();
    } else if (e.key === "Enter" && active >= 0) {
      e.preventDefault();
      choose(items[active]);
    }
  });

  input.addEventListener("blur", () => setTimeout(close, 120));
}

attachAutocomplete("origin", "origin-suggestions");
attachAutocomplete("destination", "destination-suggestions");

/* ------------------------------------------------------------ form wiring */

const returnField = $("return-field");
document.querySelectorAll('input[name="trip"]').forEach((radio) =>
  radio.addEventListener("change", () => {
    const roundTrip = radio.value === "round_trip" && radio.checked;
    returnField.hidden = !roundTrip;
    if (!roundTrip) $("return_date").value = "";
  })
);

$("swap").addEventListener("click", () => {
  const a = $("origin");
  const b = $("destination");
  [a.value, b.value] = [b.value, a.value];
});

(function seedDates() {
  const depart = new Date();
  depart.setDate(depart.getDate() + 30);
  const back = new Date(depart);
  back.setDate(back.getDate() + 7);
  const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  $("departure_date").value = iso(depart);
  $("departure_date").min = iso(new Date());
  $("return_date").value = iso(back);
})();

function codeList(value) {
  return value
    .split(/[,\s]+/)
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
}

function buildPayload() {
  const roundTrip = document.querySelector('input[name="trip"]:checked').value === "round_trip";
  const payload = {
    origin: ($("origin").dataset.code || $("origin").value).trim().toUpperCase(),
    destination: ($("destination").dataset.code || $("destination").value).trim().toUpperCase(),
    departure_date: $("departure_date").value,
    adults: Number($("adults").value),
    children: Number($("children").value),
    cabin_class: $("cabin_class").value,
    max_stops: $("max_stops").value,
    sort_by: $("sort_by").value,
    currency: $("currency").value.trim().toUpperCase() || "USD",
    airlines: codeList($("airlines").value),
    exclude_airlines: codeList($("exclude_airlines").value),
  };
  if (roundTrip && $("return_date").value) payload.return_date = $("return_date").value;
  const window_ = $("departure_window").value.trim();
  if (window_) payload.departure_window = window_;
  return payload;
}

/* --------------------------------------------------------------- results */

function renderSlice(slice) {
  const wrap = el("div", "slice");
  wrap.appendChild(el("span", "slice-dir", slice.direction === "return" ? "Return" : "Outbound"));

  const times = el("div");
  times.appendChild(
    el("div", "times", `${timeOf(slice.departure)} → ${timeOf(slice.arrival)}${dayOffset(slice.departure, slice.arrival)}`)
  );
  times.appendChild(el("div", "route", `${slice.origin} → ${slice.destination}`));
  wrap.appendChild(times);

  wrap.appendChild(el("span", "meta", duration(slice.duration_minutes)));
  wrap.appendChild(
    el("span", `stops-badge${slice.stops === 0 ? " nonstop" : ""}`, stopsLabel(slice.stops))
  );
  wrap.appendChild(
    el("span", "meta", slice.legs.map((l) => `${l.airline_code} ${l.flight_number}`).join(" · "))
  );
  return wrap;
}

function renderDetails(itinerary) {
  const details = el("details", "details");
  details.appendChild(el("summary", null, "Flight details"));

  itinerary.slices.forEach((slice) => {
    slice.legs.forEach((leg, i) => {
      const box = el("div", "leg");
      box.appendChild(
        el("div", "leg-head", `${leg.airline_name} ${leg.airline_code} ${leg.flight_number}`)
      );
      box.appendChild(
        el(
          "div",
          "leg-meta",
          `${timeOf(leg.departure)} ${leg.origin}${leg.origin_name ? ` (${leg.origin_name})` : ""} → ` +
            `${timeOf(leg.arrival)} ${leg.destination}${leg.destination_name ? ` (${leg.destination_name})` : ""}`
        )
      );
      const extras = [duration(leg.duration_minutes), leg.aircraft, leg.legroom].filter(Boolean);
      box.appendChild(el("div", "leg-meta", extras.join(" · ")));

      const layover = slice.layovers[i];
      if (layover) {
        box.appendChild(
          el(
            "div",
            "layover",
            `${duration(layover.duration_minutes)} layover in ${layover.city || layover.airport}` +
              (layover.change_of_airport ? " — airport change" : "") +
              (layover.overnight ? " — overnight" : "")
          )
        );
      }
      details.appendChild(box);
    });
  });
  return details;
}

function renderItinerary(itinerary, isCheapest) {
  const card = el("article", `itinerary${isCheapest ? " cheapest" : ""}`);

  const left = el("div");
  itinerary.slices.forEach((slice) => left.appendChild(renderSlice(slice)));

  const badges = el("div", "badges");
  if (isCheapest) badges.appendChild(el("span", "badge eco", "Cheapest"));
  if (itinerary.co2_emissions_kg) {
    const delta = itinerary.emissions_vs_typical_pct;
    const cls = delta != null && delta < 0 ? "badge eco" : "badge";
    const suffix = delta != null ? ` (${delta > 0 ? "+" : ""}${delta}% vs typical)` : "";
    badges.appendChild(el("span", cls, `${itinerary.co2_emissions_kg} kg CO₂${suffix}`));
  }
  if (itinerary.self_transfer) badges.appendChild(el("span", "badge warn", "Self-transfer"));
  if (itinerary.mixed_cabin) badges.appendChild(el("span", "badge warn", "Mixed cabin"));
  if (badges.childElementCount) left.appendChild(badges);
  card.appendChild(left);

  const priceCol = el("div", "price-col");
  priceCol.appendChild(
    el("div", itinerary.price == null ? "price unknown" : "price", itinerary.price_display)
  );
  const link = el("a", "book", "View on Google Flights →");
  link.href = itinerary.booking_url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  priceCol.appendChild(link);
  card.appendChild(priceCol);

  card.appendChild(renderDetails(itinerary));
  return card;
}

function renderResults(data) {
  resultsEl.replaceChildren();
  if (data.count === 0) {
    showStatus("No flights found for that search. Try different dates or fewer filters.");
    summaryEl.hidden = true;
    return;
  }
  clearStatus();

  summaryEl.hidden = false;
  summaryEl.replaceChildren();
  summaryEl.appendChild(el("span", null, `${data.count} itineraries`));
  if (data.cheapest_price != null) {
    const cheapest = data.itineraries.find((i) => i.price === data.cheapest_price);
    const span = el("span");
    span.append("Cheapest ", el("strong", null, cheapest.price_display));
    summaryEl.appendChild(span);
  }
  summaryEl.appendChild(el("span", null, `Found in ${data.elapsed_seconds}s`));

  data.itineraries.forEach((itinerary) =>
    resultsEl.appendChild(
      renderItinerary(itinerary, itinerary.price != null && itinerary.price === data.cheapest_price)
    )
  );
}

/* ---------------------------------------------------------------- submit */

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("submit");
  button.disabled = true;
  button.textContent = "Searching…";
  summaryEl.hidden = true;
  resultsEl.replaceChildren();
  showStatus("Searching Google Flights…");

  try {
    const res = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildPayload()),
    });
    const data = await res.json();

    if (!res.ok) {
      const detail =
        data.detail && typeof data.detail !== "string"
          ? data.detail.map((d) => d.msg).join("; ")
          : data.detail || "Search failed.";
      showStatus(detail, { error: true, hint: data.hint });
      return;
    }
    renderResults(data);
  } catch (err) {
    showStatus(`Could not reach the search API: ${err.message}`, { error: true });
  } finally {
    button.disabled = false;
    button.textContent = "Search flights";
  }
});
