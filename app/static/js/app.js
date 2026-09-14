/* Site-wide behaviour: mobile nav, sticky header, toasts, favorites, cost plan, confirmations. */
(() => {
  const header = document.querySelector('.site-header');
  const toggle = document.querySelector('.nav-toggle');
  const drawer = document.getElementById('mobile-nav');
  const backdrop = document.querySelector('.mobile-nav-backdrop');

  function setNav(open) {
    if (!drawer) return;
    drawer.classList.toggle('open', open);
    drawer.setAttribute('aria-hidden', String(!open));
    toggle?.setAttribute('aria-expanded', String(open));
    backdrop.hidden = !open;
    document.body.classList.toggle('no-scroll', open);
    if (open) drawer.querySelector('a, button')?.focus();
  }
  toggle?.addEventListener('click', () => setNav(!drawer.classList.contains('open')));
  backdrop?.addEventListener('click', () => setNav(false));
  drawer?.querySelector('.mobile-nav-close')?.addEventListener('click', () => setNav(false));
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setNav(false); });
  window.matchMedia('(min-width: 901px)').addEventListener('change', (e) => { if (e.matches) setNav(false); });

  const onScroll = () => header?.classList.toggle('scrolled', window.scrollY > 4);
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  // Toasts
  const toastRoot = document.getElementById('toast-root');
  window.toast = (message, kind = 'success', html = false) => {
    if (!toastRoot) return;
    const el = document.createElement('div');
    el.className = `toast ${kind === 'error' ? 'error' : ''}`;
    el[html ? 'innerHTML' : 'textContent'] = message;
    toastRoot.appendChild(el);
    setTimeout(() => { el.classList.add('leaving'); setTimeout(() => el.remove(), 260); }, 3200);
  };

  // Flash messages
  document.querySelectorAll('.flash').forEach((flash) => {
    flash.querySelector('.alert-close')?.addEventListener('click', () => flash.remove());
    if (flash.classList.contains('alert-success')) setTimeout(() => flash.remove(), 7000);
  });

  // Confirm dangerous forms
  document.addEventListener('submit', (e) => {
    const form = e.target.closest('form[data-confirm]');
    if (form && !window.confirm(form.dataset.confirm)) e.preventDefault();
  });

  // CSRF: every POST form and fetch call sends this session's token
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
  document.querySelectorAll('form').forEach((form) => {
    if (form.method.toLowerCase() !== 'post' || form.querySelector('input[name="csrf_token"]')) return;
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = 'csrf_token';
    input.value = csrfToken;
    form.appendChild(input);
  });

  const loginRedirect = () => {
    window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname + window.location.search);
  };

  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-CSRF-Token': csrfToken },
      body: body ? JSON.stringify(body) : null,
    });
    if (res.status === 401) { loginRedirect(); throw new Error('login'); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Something went wrong');
    return data;
  }

  // Favorites & cost plan (delegated: works for server-rendered and JS-rendered cards)
  document.addEventListener('click', async (e) => {
    const fav = e.target.closest('[data-fav]');
    if (fav) {
      e.preventDefault();
      try {
        const { favorite } = await postJSON(`/api/favorites/${fav.dataset.fav}`);
        document.querySelectorAll(`[data-fav="${fav.dataset.fav}"]`).forEach((btn) => {
          btn.classList.toggle('is-active', favorite);
          btn.setAttribute('aria-pressed', String(favorite));
          btn.classList.remove('pop'); void btn.offsetWidth; btn.classList.add('pop');
        });
        toast(favorite ? 'Saved — we\'ll notify you if prices change' : 'Removed from saved providers');
      } catch (err) { if (err.message !== 'login') toast(err.message, 'error'); }
      return;
    }
    const plan = e.target.closest('[data-plan]');
    if (plan) {
      e.preventDefault();
      plan.classList.add('is-loading');
      try {
        const data = await postJSON('/api/planned', { listing_id: Number(plan.dataset.plan) });
        toast(data.created ? 'Added to your cost tracker · <a href="/dashboard#costs">View</a>' : 'Already in your cost tracker · <a href="/dashboard#costs">View</a>', 'success', true);
      } catch (err) { if (err.message !== 'login') toast(err.message, 'error'); }
      finally { plan.classList.remove('is-loading'); }
    }
  });

  window.CostCare = { postJSON, csrfToken };
})();
