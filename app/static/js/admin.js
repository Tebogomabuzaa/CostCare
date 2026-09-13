/* Admin: Google Business Profile linking, sync, discovery and Excel upload UX. */
(() => {
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  async function callApi(url, body, button, output, loadingText = 'Contacting Google…') {
    button && (button.disabled = true, button.classList.add('is-loading'));
    if (output) output.innerHTML = `<p class="muted small">${loadingText}</p>`;
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: body ? JSON.stringify(body) : null,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`);
      return data;
    } catch (err) {
      if (output) output.innerHTML = `<div class="alert alert-error small">${esc(err.message)}</div>`;
      throw err;
    } finally {
      button && (button.disabled = false, button.classList.remove('is-loading'));
    }
  }

  const reviewHtml = (r) => `<article class="review"><div class="review-head"><strong>${esc(r.author)}</strong>
    <span class="small muted">${'★'.repeat(Math.round(r.rating || 0))} · ${esc(r.when || '')}</span></div>
    <p class="review-text small">${esc((r.text || '').slice(0, 260))}</p></article>`;

  // ---- Provider edit page: link / preview / sync
  const gForm = document.getElementById('google-link-form');
  if (gForm) {
    const out = document.getElementById('google-result');
    const providerId = gForm.dataset.provider;

    gForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = gForm.querySelector('[type=submit]');
      try {
        const d = await callApi(`/api/admin/providers/${providerId}/google-profile`, { url: gForm.url.value.trim() }, btn, out);
        out.innerHTML = `<div class="alert alert-success small">Linked <strong>${esc(d.name)}</strong> — ${d.rating ?? '–'}★ from ${d.review_count ?? 0} ratings, ${d.reviews_stored} reviews captured, location ${d.latitude?.toFixed?.(4) ?? '?'}, ${d.longitude?.toFixed?.(4) ?? '?'}.</div>`;
        toast('Google profile linked');
        setTimeout(() => window.location.reload(), 1400);
      } catch { /* shown in output */ }
    });

    gForm.querySelector('[data-google-preview]')?.addEventListener('click', async (e) => {
      if (!gForm.url.value.trim()) { gForm.url.focus(); return; }
      try {
        const d = await callApi('/api/admin/google/preview', { url: gForm.url.value.trim() }, e.currentTarget, out);
        out.innerHTML = `<div class="google-preview">
          <h4>${esc(d.name)}</h4>
          <p class="small muted">${esc(d.address || '')}</p>
          <p class="small">${d.rating ?? '–'}★ · ${d.review_count ?? 0} ratings${d.phone ? ' · ' + esc(d.phone) : ''}${d.maps_link ? ` · <a href="${esc(d.maps_link)}" target="_blank" rel="noopener">Google Maps ↗</a>` : ''}</p>
          <div class="review-list compact">${(d.reviews || []).map(reviewHtml).join('') || '<p class="small muted">No reviews returned.</p>'}</div>
          <p class="small muted">Not saved yet — click “Link & capture reviews” if this is the right place.</p>
        </div>`;
      } catch { /* shown */ }
    });

    document.querySelector('[data-google-sync]')?.addEventListener('click', async (e) => {
      try {
        const d = await callApi(`/api/admin/providers/${providerId}/google-sync`, null, e.currentTarget, out, 'Refreshing from Google…');
        out.innerHTML = `<div class="alert alert-success small">Refreshed: ${d.rating ?? '–'}★, ${d.reviews_stored} reviews.</div>`;
        setTimeout(() => window.location.reload(), 1000);
      } catch { /* shown */ }
    });
  }

  // ---- Dashboard: sync all + discover
  document.querySelector('[data-sync-all]')?.addEventListener('click', async (e) => {
    try {
      const d = await callApi('/api/admin/google/sync-all', null, e.currentTarget, null);
      toast(`Refreshing ${d.queued} Google profiles in the background`);
    } catch (err) { toast(err.message, 'error'); }
  });

  const discover = document.getElementById('discover-form');
  if (discover) {
    const out = document.getElementById('discover-result');
    discover.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = discover.querySelector('button');
      try {
        const d = await callApi('/api/admin/google/discover',
          { query: discover.query.value.trim(), limit: Number(discover.limit.value) || 10 }, btn, out, 'Searching Google and importing places…');
        out.innerHTML = d.created
          ? `<div class="alert alert-success small">Imported ${d.created} new provider(s). Add their prices next.</div>
             <ul class="change-list">${d.providers.map((p) => `<li><a href="/admin/providers/${p.provider_id}"><strong>${esc(p.name)}</strong></a>
             <div class="small muted">${esc(p.address || '')} · ${p.rating ?? '–'}★ (${p.review_count ?? 0}) · ${p.reviews_stored} reviews</div></li>`).join('')}</ul>`
          : '<div class="alert alert-info small">No new places found (existing ones are skipped).</div>';
      } catch { /* shown */ }
    });
  }

  // ---- Excel dropzone
  const dz = document.querySelector('.dropzone');
  if (dz) {
    const input = dz.querySelector('input[type=file]');
    const label = dz.querySelector('[data-filename]');
    const show = () => {
      const f = input.files[0];
      dz.classList.toggle('has-file', !!f);
      label.textContent = f ? `${f.name} · ${(f.size / 1024).toFixed(0)} KB` : 'or drag it here';
    };
    input.addEventListener('change', show);
    ['dragenter', 'dragover'].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add('dragover'); }));
    ['dragleave', 'drop'].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove('dragover'); }));
    dz.addEventListener('drop', (e) => { if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; show(); } });
    document.getElementById('upload-form')?.addEventListener('submit', (e) => {
      e.submitter?.classList.add('is-loading');
    });
  }
})();
