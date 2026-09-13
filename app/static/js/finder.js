/* Service Finder: AI search-as-you-type, filters, sorting, result cards and map. */
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const zar = (n) => 'R' + Math.round(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  const svg = (path) => `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
  const ICON = {
    pin: svg('<path d="M12 21s-7-6.2-7-12a7 7 0 0 1 14 0c0 5.8-7 12-7 12z"/><circle cx="12" cy="9" r="2.5"/>'),
    star: svg('<path d="m12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z"/>'),
    clock: svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
    heart: svg('<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l1 1.1L12 21l7.8-7.5 1-1.1a5.5 5.5 0 0 0 0-7.8z"/>'),
    map: svg('<path d="m9 4-6 2v14l6-2 6 2 6-2V4l-6 2z"/><path d="M9 4v14M15 6v14"/>'),
    calendar: svg('<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/>'),
    plus: svg('<path d="M12 5v14M5 12h14"/>'),
    sparkles: svg('<path d="M12 3l1.8 4.9L19 9.7l-5.2 1.8L12 16.5l-1.8-5L5 9.7l5.2-1.8z"/>'),
    building: svg('<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M9 21v-4h6v4"/>'),
    refresh: svg('<path d="M21 12a9 9 0 1 1-2.6-6.4L21 8M21 3v5h-5"/>'),
    shield: svg('<path d="M12 3 4 6v6c0 5 3.5 8.5 8 9 4.5-.5 8-4 8-9V6z"/>'),
  };

  const els = {
    form: $('#finder-form'), q: $('#q'), results: $('#results'), count: $('#result-count'), summary: $('#ai-summary'),
    notice: $('#notice'), location: $('#f-location'), category: $('#f-category'), sort: $('#f-sort'),
    min: $('#price-min'), max: $('#price-max'), priceLabel: $('#price-label'), fill: $('#range-fill'),
    filters: $('#filters'), filtersBackdrop: $('.filters-backdrop'), mapCol: $('#map-col'),
  };
  if (!els.form) return;

  // Price slider uses a log scale so R250 GP visits and R200k surgeries are both usable.
  const LO = 100, HI = 250000, STEPS = 1000;
  const state = { minRating: null, priceTouched: false, sortTouched: false, coords: null, controller: null, timer: null };
  const sliderToPrice = (v) => {
    const p = LO * Math.pow(HI / LO, v / STEPS);
    return p < 1000 ? Math.round(p / 10) * 10 : p < 10000 ? Math.round(p / 100) * 100 : Math.round(p / 1000) * 1000;
  };
  const priceValues = () => {
    const a = +els.min.value, b = +els.max.value;
    return { min: a > 0 ? sliderToPrice(a) : null, max: b < STEPS ? sliderToPrice(b) : null };
  };

  function updatePriceUI() {
    const a = +els.min.value, b = +els.max.value;
    els.fill.style.left = `${(a / STEPS) * 100}%`;
    els.fill.style.right = `${100 - (b / STEPS) * 100}%`;
    const { min, max } = priceValues();
    els.priceLabel.textContent = !min && !max ? 'Any price'
      : min && max ? `${zar(min)} – ${zar(max)}` : max ? `Up to ${zar(max)}` : `From ${zar(min)}`;
  }

  function onRange(e) {
    const gap = 25;
    if (+els.min.value > +els.max.value - gap) {
      if (e.target === els.min) els.min.value = +els.max.value - gap; else els.max.value = +els.min.value + gap;
    }
    state.priceTouched = true;
    updatePriceUI();
    schedule(350);
  }

  function schedule(ms = 450) {
    clearTimeout(state.timer);
    state.timer = setTimeout(search, ms);
  }

  const skeleton = (n = 4) => Array.from({ length: n }, () =>
    '<div class="result-card skeleton-card" aria-hidden="true"><div class="sk sk-chip"></div><div class="sk sk-title"></div><div class="sk sk-line"></div><div class="sk sk-line short"></div><div class="sk sk-price"></div></div>'
  ).join('');

  async function search() {
    clearTimeout(state.timer);
    const params = new URLSearchParams();
    const q = els.q.value.trim();
    if (q) params.set('q', q);
    if (els.location.value) params.set('location', els.location.value);
    if (els.category.value) params.set('category', els.category.value);
    if (state.sortTouched) params.set('sort', els.sort.value);
    if (state.priceTouched) {
      const { min, max } = priceValues();
      if (min) params.set('min_price', min);
      if (max) params.set('max_price', max);
    }
    if (state.minRating) params.set('min_rating', state.minRating);
    if (state.coords) { params.set('lat', state.coords.lat); params.set('lng', state.coords.lng); }

    state.controller?.abort();
    const controller = (state.controller = new AbortController());
    const hasResults = els.results.querySelector('.result-card:not(.skeleton-card)');
    els.form.classList.add('is-loading');
    els.results.setAttribute('aria-busy', 'true');
    if (hasResults) els.results.classList.add('is-stale'); else els.results.innerHTML = skeleton();
    els.count.textContent = 'Searching…';

    try {
      const res = await fetch('/api/search?' + params.toString(), { signal: controller.signal, headers: { Accept: 'application/json' } });
      if (!res.ok) throw new Error(`Search failed (${res.status})`);
      render(await res.json());
      const url = new URL(window.location.href);
      if (q) url.searchParams.set('q', q); else url.searchParams.delete('q');
      history.replaceState(null, '', url);
    } catch (err) {
      if (err.name === 'AbortError') return;
      els.results.innerHTML = `<div class="empty-state"><h3>Something went wrong</h3><p class="muted">${esc(err.message)}. Please try again.</p><div class="btn-row"><button class="btn btn-outline btn-sm" type="button" data-retry>Retry</button></div></div>`;
      els.count.textContent = '';
    } finally {
      if (state.controller === controller) {
        els.form.classList.remove('is-loading');
        els.results.classList.remove('is-stale');
        els.results.removeAttribute('aria-busy');
      }
    }
  }

  function render(data) {
    const it = data.intent;
    els.count.innerHTML = data.count
      ? `<strong>${data.count}</strong> price${data.count === 1 ? '' : 's'} from <strong>${data.provider_count}</strong> provider${data.provider_count === 1 ? '' : 's'}`
      : 'No matching prices';

    const chips = [];
    (it.service_names || []).forEach((n) => chips.push(`<span class="chip chip-primary">${esc(n)}</span>`));
    (it.categories || []).forEach((n) => chips.push(`<span class="chip chip-primary">${esc(n)}</span>`));
    if (it.location) chips.push(`<span class="chip">${ICON.pin} ${esc(it.location)}</span>`);
    if (it.min_price || it.max_price) {
      const label = it.min_price && it.max_price ? `${zar(it.min_price)} – ${zar(it.max_price)}` : it.max_price ? `Under ${zar(it.max_price)}` : `From ${zar(it.min_price)}`;
      chips.push(`<span class="chip">${label}</span>`);
    }
    if (it.min_rating) chips.push(`<span class="chip">${it.min_rating}★ and up</span>`);
    els.summary.innerHTML = data.query
      ? `<span class="ai-badge ${it.parser === 'ai' ? 'is-ai' : ''}">${ICON.sparkles} ${it.parser === 'ai' ? 'AI search' : 'Smart match'}</span><span class="ai-text">${esc(it.summary)}</span>${chips.join('')}`
      : '';

    if (!state.sortTouched && it.sort) {
      const opt = [...els.sort.options].find((o) => o.value === it.sort && !o.disabled);
      els.sort.value = opt ? it.sort : 'relevance';
    }

    els.notice.hidden = !data.notice;
    els.notice.textContent = data.notice || '';

    if (!data.results.length) {
      els.results.innerHTML = emptyState();
      updateMap([]);
      return;
    }
    els.results.innerHTML = data.results.map(card).join('');
    updateMap(data.results);
  }

  function emptyState() {
    const suggestions = ['GP consultation', 'Dental check-up', 'MRI scan', 'Emergency room visit'];
    return `<div class="empty-state">
      <h3>No prices match yet</h3>
      <p class="muted">Try a broader search, remove a filter, or search another city.</p>
      <div class="btn-row">${suggestions.map((s) => `<button type="button" class="chip chip-link" data-suggest="${esc(s)}">${esc(s)}</button>`).join('')}</div>
    </div>`;
  }

  function card(item, i) {
    const p = item.provider;
    const rating = p.rating
      ? `<span class="rating">${ICON.star}<strong>${Number(p.rating).toFixed(1)}</strong><span class="muted">(${p.review_count}${p.rating_source === 'Google' ? ' on Google' : ''})</span></span>`
      : '<span class="muted">No ratings yet</span>';
    const badges = [
      p.is_partner ? `<span class="badge badge-partner">${ICON.shield} Partner</span>` : '',
      p.is_verified ? '<span class="badge badge-verified">Verified prices</span>' : '',
      p.is_demo ? '<span class="badge badge-demo">Sample data</span>' : '',
    ].join('');
    const isTel = p.book_link && p.book_link.startsWith('tel:');
    return `<article class="result-card" data-provider="${p.id}" style="animation-delay:${Math.min(i, 8) * 35}ms">
      <div class="rc-top">
        <div>
          <span class="chip chip-soft">${esc(item.service.category)}</span>
          <h3 class="rc-service">${esc(item.service.name)}</h3>
          <a class="rc-provider" href="${esc(p.url)}">${esc(p.name)}</a>
          <div class="rc-meta">
            <span>${ICON.building} ${esc(p.type)}</span>
            <span>${ICON.pin} ${esc(p.city || p.address)}${item.distance_km != null ? ` · ${item.distance_km} km` : ''}</span>
          </div>
        </div>
        <button type="button" class="icon-btn fav-btn ${p.is_favorite ? 'is-active' : ''}" data-fav="${p.id}" aria-pressed="${p.is_favorite}" aria-label="Save ${esc(p.name)}">${ICON.heart}</button>
      </div>
      <div class="rc-body">
        <div class="rc-price">
          <span class="price">${esc(item.price_label)}</span>
          <span class="updated">${ICON.refresh} Updated ${esc(item.updated_label)}</span>
        </div>
        <div class="rc-stats">
          ${rating}
          ${p.avg_wait_minutes != null ? `<span>${ICON.clock} ~${p.avg_wait_minutes} min wait</span>` : ''}
          ${badges}
        </div>
      </div>
      ${item.notes ? `<p class="rc-notes">${esc(item.notes)}</p>` : ''}
      <div class="rc-actions">
        ${p.book_link ? `<a class="btn btn-primary btn-sm" href="${esc(p.book_link)}" ${isTel ? '' : 'target="_blank" rel="noopener"'}>${ICON.calendar} ${isTel ? 'Call to book' : 'Book appointment'}</a>` : ''}
        <a class="btn btn-outline btn-sm" href="${esc(p.url)}">More details</a>
        <a class="btn btn-ghost btn-sm" href="${esc(p.directions_link)}" target="_blank" rel="noopener">${ICON.map} Directions</a>
        <button type="button" class="btn btn-ghost btn-sm" data-plan="${item.listing_id}">${ICON.plus} Cost plan</button>
      </div>
    </article>`;
  }

  // ---------- Map (Leaflet + OpenStreetMap; directions open in Google Maps)
  let map = null, layer = null, markers = {};
  function ensureMap() {
    if (map || !window.L || !$('#map')) return map;
    map = L.map('map', { scrollWheelZoom: false }).setView([-29, 25], 5);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }).addTo(map);
    layer = L.featureGroup().addTo(map);
    return map;
  }
  function updateMap(results) {
    if (!ensureMap()) return;
    layer.clearLayers();
    markers = {};
    const byProvider = new Map();
    results.forEach((r) => {
      const p = r.provider;
      if (p.latitude == null || p.longitude == null) return;
      if (!byProvider.has(p.id)) byProvider.set(p.id, { p, items: [] });
      byProvider.get(p.id).items.push(r);
    });
    byProvider.forEach(({ p, items }) => {
      const cheapest = Math.min(...items.map((i) => i.price_min));
      const icon = L.divIcon({ className: 'price-pin', html: `<span>${zar(cheapest)}</span>`, iconSize: [0, 0] });
      const popup = `<strong><a href="${esc(p.url)}">${esc(p.name)}</a></strong><br>` +
        items.slice(0, 4).map((i) => `${esc(i.service.name)}: <b>${esc(i.price_label)}</b>`).join('<br>') +
        `<br><a href="${esc(p.directions_link)}" target="_blank" rel="noopener">Directions ↗</a>`;
      const marker = L.marker([p.latitude, p.longitude], { icon, riseOnHover: true }).bindPopup(popup);
      marker.on('mouseover', () => $$(`.result-card[data-provider="${p.id}"]`).forEach((c) => c.classList.add('is-hovered')));
      marker.on('mouseout', () => $$(`.result-card[data-provider="${p.id}"]`).forEach((c) => c.classList.remove('is-hovered')));
      marker.addTo(layer);
      markers[p.id] = marker;
    });
    if (state.coords) {
      L.circleMarker([state.coords.lat, state.coords.lng], { radius: 7, color: '#0369a1', fillColor: '#0ea5e9', fillOpacity: 0.9 })
        .bindPopup('You are here').addTo(layer);
    }
    if (layer.getLayers().length) map.fitBounds(layer.getBounds(), { padding: [40, 40], maxZoom: 13 });
  }

  els.results.addEventListener('mouseover', (e) => {
    const cardEl = e.target.closest('.result-card');
    Object.values(markers).forEach((m) => m.getElement()?.classList.remove('is-active'));
    if (cardEl) markers[cardEl.dataset.provider]?.getElement()?.classList.add('is-active');
  });

  // ---------- Events
  els.form.addEventListener('submit', (e) => { e.preventDefault(); search(); els.q.blur(); });
  els.q.addEventListener('input', () => {
    const len = els.q.value.trim().length;
    if (len === 0 || len >= 3) schedule(500);
  });
  [els.location, els.category].forEach((el) => el.addEventListener('change', () => search()));
  els.sort.addEventListener('change', () => { state.sortTouched = true; search(); });
  els.min.addEventListener('input', onRange);
  els.max.addEventListener('input', onRange);

  $$('[data-rating]').forEach((btn) => btn.addEventListener('click', () => {
    $$('[data-rating]').forEach((b) => { b.classList.toggle('is-active', b === btn); b.setAttribute('aria-pressed', String(b === btn)); });
    state.minRating = btn.dataset.rating ? Number(btn.dataset.rating) : null;
    search();
  }));

  $('#reset-filters').addEventListener('click', () => {
    els.location.value = ''; els.category.value = '';
    els.min.value = 0; els.max.value = STEPS; state.priceTouched = false; updatePriceUI();
    state.minRating = null;
    $$('[data-rating]').forEach((b, i) => { b.classList.toggle('is-active', i === 0); b.setAttribute('aria-pressed', String(i === 0)); });
    els.sort.value = 'relevance'; state.sortTouched = false;
    search();
  });

  $('#use-location').addEventListener('click', (e) => {
    const btn = e.currentTarget;
    if (!navigator.geolocation) { toast('Location is not available in this browser', 'error'); return; }
    btn.classList.add('is-loading');
    navigator.geolocation.getCurrentPosition((pos) => {
      btn.classList.remove('is-loading');
      state.coords = { lat: pos.coords.latitude.toFixed(5), lng: pos.coords.longitude.toFixed(5) };
      const opt = [...els.sort.options].find((o) => o.value === 'distance');
      opt.disabled = false; opt.textContent = 'Nearest to me';
      els.sort.value = 'distance'; state.sortTouched = true;
      btn.innerHTML = `${ICON.pin} Using your location`;
      search();
    }, () => { btn.classList.remove('is-loading'); toast('Could not get your location', 'error'); }, { timeout: 10000 });
  });

  els.results.addEventListener('click', (e) => {
    if (e.target.closest('[data-retry]')) search();
    const suggest = e.target.closest('[data-suggest]');
    if (suggest) { els.q.value = suggest.dataset.suggest; search(); }
  });

  // Mobile filter drawer & map toggle
  const setFilters = (open) => {
    els.filters.classList.toggle('open', open);
    els.filtersBackdrop.hidden = !open;
    document.body.classList.toggle('no-scroll', open);
  };
  $('#open-filters').addEventListener('click', () => setFilters(true));
  $('#close-filters').addEventListener('click', () => setFilters(false));
  els.filtersBackdrop.addEventListener('click', () => setFilters(false));
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setFilters(false); });
  $('#toggle-map').addEventListener('click', (e) => {
    const show = !els.mapCol.classList.contains('show');
    els.mapCol.classList.toggle('show', show);
    e.currentTarget.setAttribute('aria-pressed', String(show));
    if (show && ensureMap()) setTimeout(() => { map.invalidateSize(); if (layer.getLayers().length) map.fitBounds(layer.getBounds(), { padding: [40, 40], maxZoom: 13 }); }, 50);
  });

  updatePriceUI();
  search();
})();
