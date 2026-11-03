'use strict';

/* Deux vues : #/nouvelles (offres fraîches, grille + actions) et
   #/candidatures (pipeline en colonnes, glisser-déposer). */

const LABELS = {
  new: 'Nouveau', shortlist: 'À postuler', applied: 'Postulé',
  interview: 'Entretien', rejected: 'Refusé', offer: 'Offre'
};
const PIPELINE = ['shortlist', 'applied', 'interview', 'rejected', 'offer'];

const state = {
  offers: [], stats: null, sites: [], q: '',
  view: 'new', openId: null, logTimer: null, boardTimer: null,
  offerId: null, offerTimer: null
};

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function toast(msg, ms = 2600) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove('show'), ms);
}

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleDateString('fr-FR', { day: '2-digit', month: 'short' });
}

function ago(iso) {
  if (!iso) return '';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (isNaN(s)) return '';
  if (s < 90) return 'à l’instant';
  if (s < 3600) return 'il y a ' + Math.round(s / 60) + ' min';
  if (s < 86400) return 'il y a ' + Math.round(s / 3600) + ' h';
  if (s < 604800) return 'il y a ' + Math.round(s / 86400) + ' j';
  return fmtDate(iso);
}

function scoreHtml(s) {
  if (s === null || s === undefined) return '<span class="score none">—</span>';
  const k = s >= 75 ? 'hi' : s >= 60 ? 'mid' : 'lo';
  return `<span class="score ${k}">${s}</span>`;
}

function runChip(o) {
  const r = o.run;
  if (r && (r.state === 'running' || r.state === 'queued')) {
    return `<span class="running"><span class="dot"></span>${r.state === 'queued' ? 'en file' : 'génération'}</span>`;
  }
  if (r && r.state === 'failed') return '<span class="running" style="color:var(--bad)">échec</span>';
  if (r && r.files && r.files.cv_pdf) return '<span class="docs">CV + LM</span>';
  return '';
}

function shortList(str, n = 4) {
  return (str || []).slice(0, n);
}

/* ---------------- lien de candidature ---------------- */

const APPLY_KIND = { site: "site de l’entreprise", ats: 'ATS', board: 'job board', manual: 'saisi à la main' };
const APPLY_CONF = { high: 'confiance élevée', medium: 'confiance moyenne', low: 'confiance faible' };

function applyBusy(o) {
  return !!(o.apply && (o.apply.state === 'queued' || o.apply.state === 'running'));
}

function applyButton(o, cls) {
  if (!o.apply || !o.apply.url) return '';
  return `<a class="btn ${cls || ''} apply" href="${esc(o.apply.url)}" target="_blank"
             rel="noopener" data-act="link" draggable="false"
             title="${esc(o.apply.note || 'Lien de candidature')}">Postuler ↗</a>`;
}

function applyHint(o) {
  if (applyBusy(o)) return '<span class="running"><span class="dot"></span>recherche du lien…</span>';
  if (!o.apply || !o.apply.url) return '';
  return `<span class="apply-meta">${esc(APPLY_KIND[o.apply.kind] || '')}${
    APPLY_CONF[o.apply.confidence] ? ' · ' + esc(APPLY_CONF[o.apply.confidence]) : ''}</span>`;
}

/* ---------------- routing ---------------- */

function route() {
  const h = location.hash.replace(/^#\/?/, '');
  const deep = h.match(/^offre\/(\d+)/);
  if (deep) {
    state.view = 'offer';
    state.offerId = Number(deep[1]);
  } else {
    state.view = h.startsWith('candidatures') ? 'pipeline' : 'new';
    state.offerId = null;
  }
  if (!location.hash) history.replaceState(null, '', '#/nouvelles');
  document.querySelectorAll('#nav a').forEach((a) =>
    a.classList.toggle('active', a.dataset.view === state.view));
  $('#viewNew').hidden = state.view !== 'new';
  $('#viewPipeline').hidden = state.view !== 'pipeline';
  $('#viewOffer').hidden = state.view !== 'offer';
  if (state.view === 'offer') {
    openOffer(state.offerId);
  } else {
    stopOffer();
    render();
  }
}

window.addEventListener('hashchange', route);

/* ---------------- data ---------------- */

async function load(quiet) {
  try {
    const r = await fetch('/api/board');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    state.offers = d.offers;
    state.stats = d.stats;
    render();
    schedule(activeRun());
  } catch (e) {
    if (!quiet) toast('Hub injoignable : ' + e.message, 4000);
  }
}

async function loadSites() {
  try {
    const r = await fetch('/api/sites');
    state.sites = await r.json();
  } catch (_) { state.sites = []; }
  renderSources();
}

function activeRun() {
  return state.offers.find((o) => (o.run && (o.run.state === 'running' || o.run.state === 'queued'))
    || applyBusy(o));
}

function schedule(active) {
  clearTimeout(state.boardTimer);
  state.boardTimer = setTimeout(() => load(true), active ? 4000 : 20000);
}

function matches(o) {
  if (!state.q) return true;
  const q = state.q.toLowerCase();
  return [o.title, o.company, o.location, o.contract, (o.matched || []).join(' ')]
    .join(' ').toLowerCase().includes(q);
}

/* ---------------- render ---------------- */

function renderSources() {
  const el = $('#sources');
  if (!el) return;
  el.innerHTML = state.sites.map((s) => `
    <a class="source" href="${esc(s.url)}" target="_blank" rel="noopener" title="Chercher sur ${esc(s.name)}">
      <span class="sq" style="background:${esc(s.color || '#b98cf0')}">${esc(s.short || s.name[0])}</span>
      ${esc(s.name)}<span class="ext">↗</span>
    </a>`).join('');
}

function renderStats() {
  const s = state.stats || { by_status: {} };
  const n = s.by_status.new || 0;
  const badge = $('#badgeNew');
  badge.textContent = n;
  badge.classList.toggle('zero', n === 0);

  $('#stats').innerHTML = [
    ['à postuler', s.by_status.shortlist || 0, 'hot'],
    ['postulé', s.by_status.applied || 0, ''],
    ['entretien', s.by_status.interview || 0, ''],
    ['refus', s.by_status.rejected || 0, ''],
  ].map(([l, v, k]) => `<div class="stat ${k}">${l} <b>${v}</b></div>`).join('');
}

function render() {
  renderStats();
  if (state.view === 'new') renderNew(); else renderPipeline();
}

function cardNew(o) {
  const gaps = shortList(o.gaps, 3).map((g) => `<span class="chip gap">${esc(g)}</span>`).join('');
  const chips = shortList(o.matched, 4).map((m) => `<span class="chip">${esc(m)}</span>`).join('');
  const busy = o.run && (o.run.state === 'running' || o.run.state === 'queued');
  return `
  <article class="card" data-id="${o.id}">
    <div class="card-top">
      ${scoreHtml(o.score)}
      ${runChip(o)}
      <span class="src">${esc(o.source || '')}</span>
    </div>
    <h3 title="${esc(o.title)}">${esc(o.title)}</h3>
    <div class="company">${esc(o.company || '—')}${o.location ? ' · ' + esc(o.location) : ''}</div>
    ${o.contract ? `<div class="meta">${esc(o.contract)}</div>` : ''}
    ${chips || gaps ? `<div class="chips">${chips}${gaps}</div>` : ''}
    <div class="card-actions">
      <button class="btn small adapt" data-act="adapt" ${busy ? 'disabled' : ''}>
        ${busy ? 'en cours…' : '⚡ Adapter CV + LM'}
      </button>
      <button class="btn small keep" data-act="keep" title="Garder pour postuler">✓</button>
      <button class="btn small drop" data-act="drop" title="Supprimer l’offre">✕</button>
      <span class="spacer"></span>
      ${o.url ? `<a class="btn small ghost" href="${esc(o.url)}" target="_blank" rel="noopener"
                  data-act="link">offre ↗</a>` : ''}
    </div>
  </article>`;
}

function cardPipe(o) {
  const r = o.run;
  const doc = r && r.files && r.files.cv_pdf
    ? '<span class="docs">CV + LM</span>' : runChip(o);
  return `
  <article class="card" draggable="true" data-id="${o.id}">
    <div class="card-top">
      ${scoreHtml(o.score)}
      ${doc}
      <span class="src">${esc(o.source || '')}</span>
    </div>
    <h3 title="${esc(o.title)}">${esc(o.title)}</h3>
    <div class="company">${esc(o.company || '—')}${o.location ? ' · ' + esc(o.location) : ''}</div>
    ${o.contract ? `<div class="meta">${esc(o.contract)}</div>` : ''}
    <div class="card-foot">
      <span class="when">${o.status === 'applied' && o.applied_at
        ? 'postulé ' + fmtDate(o.applied_at) : 'trouvée ' + fmtDate(o.found_at)}</span>
      <span class="spacer"></span>
      ${applyBusy(o) ? '<span class="running"><span class="dot"></span>lien…</span>' : applyButton(o, 'small')}
    </div>
  </article>`;
}

function renderNew() {
  const items = state.offers.filter((o) => o.status === 'new' && matches(o));
  const el = $('#gridNew');
  $('#newHint').textContent = items.length
    ? `${items.length} offre${items.length > 1 ? 's' : ''} · triées par score`
    : 'rien de neuf pour l’instant';
  el.innerHTML = items.length ? items.map(cardNew).join('')
    : `<div class="empty big" style="grid-column:1/-1">
         <b>Aucune nouvelle offre.</b><br>
         Le cron de 9h remplit cette page tout seul, ou cherche à la main
         sur les job boards ci-dessus puis « + Offre ».
       </div>`;

  el.querySelectorAll('.card').forEach((c) => {
    const id = Number(c.dataset.id);
    c.addEventListener('click', (e) => {
      const act = e.target.closest('[data-act]');
      if (act) {
        e.stopPropagation();
        const a = act.dataset.act;
        if (a === 'adapt') return tailor(id);
        if (a === 'keep') return move(id, 'shortlist', 'Ajoutée au suivi → À postuler');
        if (a === 'drop') return deleteOffer(id);
        return; // link : laisser le navigateur suivre
      }
      openDrawer(id);
    });
  });
}

function renderPipeline() {
  const board = $('#board');
  board.innerHTML = PIPELINE.map((st) => {
    const list = state.offers.filter((o) => o.status === st && matches(o));
    return `
    <section class="col" data-status="${st}">
      <div class="col-head"><h2>${LABELS[st]}</h2><span class="count">${list.length}</span></div>
      <div class="col-body" data-status="${st}">
        ${list.length ? list.map(cardPipe).join('') : '<div class="empty">—</div>'}
      </div>
    </section>`;
  }).join('');

  board.querySelectorAll('.card').forEach((el) => {
    el.addEventListener('click', (e) => {
      if (e.target.closest('[data-act]')) return; // lien « Postuler » : ne pas ouvrir le tiroir
      openDrawer(Number(el.dataset.id));
    });
    el.addEventListener('dragstart', (e) => {
      e.dataTransfer.setData('text/plain', el.dataset.id);
      el.classList.add('dragging');
    });
    el.addEventListener('dragend', () => el.classList.remove('dragging'));
  });

  board.querySelectorAll('.col').forEach((col) => {
    const st = col.dataset.status;
    col.addEventListener('dragover', (e) => { e.preventDefault(); col.classList.add('drag-over'); });
    col.addEventListener('dragleave', () => col.classList.remove('drag-over'));
    col.addEventListener('drop', async (e) => {
      e.preventDefault();
      col.classList.remove('drag-over');
      const id = Number(e.dataTransfer.getData('text/plain'));
      if (id) await move(id, st);
    });
  });
}

async function move(id, status, msg) {
  const o = state.offers.find((x) => x.id === id);
  if (!o || o.status === status) return;
  o.status = status;
  render();
  const r = await fetch('/api/offers/' + id, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status })
  });
  if (!r.ok) toast('Changement de statut échoué');
  else if (msg) toast(msg);
  load(true);
}

/* La croix des « Nouvelles offres » supprime : une offre qu'on ne veut pas
   n'est pas un refus (la colonne Refusé = candidature qui n'a pas abouti). */
async function deleteOffer(id) {
  const o = state.offers.find((x) => x.id === id);
  if (!o) return;
  const ok = confirm('Supprimer « ' + o.title + ' » du hub ?\n\n'
    + 'Elle disparaît et ne sera plus réimportée par le cron.\n'
    + 'Pour noter un refus, passez-la dans le suivi puis glissez-la en Refusé.');
  if (!ok) return;
  const r = await fetch('/api/offers/' + id, { method: 'DELETE' });
  if (!r.ok) { toast('Suppression impossible'); return; }
  state.offers = state.offers.filter((x) => x.id !== id);
  if (state.openId === id) closeDrawer();
  render();
  toast('Offre supprimée');
  load(true);
}

/* ---------------- drawer ---------------- */

async function openDrawer(id) {
  state.openId = id;
  $('#drawerWrap').classList.add('open');
  $('#dBody').innerHTML = '<div class="empty">chargement…</div>';
  await refreshDrawer();
}

function closeDrawer() {
  state.openId = null;
  $('#drawerWrap').classList.remove('open');
  clearTimeout(state.logTimer);
}

async function refreshDrawer() {
  if (!state.openId) return;
  const id = state.openId;
  const r = await fetch('/api/offers/' + id);
  if (!r.ok) return;
  const o = await r.json();
  if (state.openId !== id) return;
  renderDrawer(o);
  const run = o.run;
  clearTimeout(state.logTimer);
  if (run && (run.state === 'running' || run.state === 'queued')) {
    state.logTimer = setTimeout(() => { pollLog(run.id); refreshDrawer(); }, 4000);
  }
}

function renderDrawer(o) {
  const busy = o.run && (o.run.state === 'running' || o.run.state === 'queued');
  $('#dTitle').textContent = o.title;
  $('#dExpand').href = '#/offre/' + o.id;
  $('#dSub').innerHTML = `${esc(o.company || '—')}${o.location ? ' · ' + esc(o.location) : ''}
     ${o.score != null ? ' · match ' + o.score + '/100' : ''}`;

  const opts = Object.keys(LABELS).map((s) =>
    `<option value="${s}" ${o.status === s ? 'selected' : ''}>${LABELS[s]}</option>`).join('');

  const run = o.run;
  let runHtml = '';
  if (run) {
    const st = run.state;
    const label = st === 'queued' ? 'en file d’attente' :
      st === 'running' ? `en cours depuis ${ago(run.started_at).replace('il y a ', '')}` :
      st === 'done' ? `terminé ${ago(run.finished_at)}` : `échec ${ago(run.finished_at)}`;
    const files = ['cv_pdf', 'lm_pdf', 'cv_docx', 'lm_docx']
      .filter((k) => run.files && run.files[k])
      .map((k) => {
        const name = { cv_pdf: 'CV (PDF)', lm_pdf: 'Lettre (PDF)', cv_docx: 'CV (DOCX)', lm_docx: 'Lettre (DOCX)' }[k];
        return `<a class="btn small" href="/api/offers/${o.id}/doc/${k}">${name}</a>`;
      }).join('');
    const m = run.manifest || {};
    const summary = m.company_summary
      ? `<p style="font-size:12.5px;color:var(--muted);margin:12px 0 0;line-height:1.6">${esc(m.company_summary)}</p>` : '';
    const adapted = (m.adapted || []).length
      ? `<div class="chips" style="margin-top:10px">${m.adapted.map((a) => `<span class="chip">${esc(a)}</span>`).join('')}</div>` : '';
    runHtml = `
      <div class="runbox" style="margin-top:11px">
        <div class="state">
          ${busy ? '<span class="dot" style="color:var(--warn)"></span>' : ''}
          <span>Run #${run.id} — ${esc(label)}</span>
        </div>
        ${run.error ? `<div style="color:var(--bad);font-size:12px;margin-top:7px">${esc(run.error)}</div>` : ''}
        ${files ? `<div class="files" style="margin-top:11px">${files}</div>` : ''}
        ${m.email_sent ? '<div style="font-size:11.5px;color:var(--good);margin-top:9px">mail d’archive envoyé</div>' : ''}
        ${summary}${adapted}
        ${busy ? '<pre class="logs" id="logs">chargement du log…</pre>'
               : `<div style="margin-top:11px"><button class="btn small ghost" onclick="toggleLog(${run.id})">voir le log</button></div>
                  <pre class="logs" id="logs" style="display:none"></pre>`}
      </div>`;
  }

  const tl = (o.events || []).map((e) => `
    <div class="tl"><span class="b ${e.kind === 'documents_ready' || e.kind === 'status' ? 'on' : ''}"></span>
      <div>${esc(e.message || e.kind)}<div class="d">${esc(e.kind)} · ${new Date(e.at).toLocaleString('fr-FR')}</div></div>
    </div>`).join('');

  $('#dBody').innerHTML = `
    <div>
      <p class="sect-title">Statut</p>
      <select id="dStatus">${opts}</select>
    </div>
    <div>
      <p class="sect-title">Adaptation</p>
      <div class="files">
        <button class="btn primary" id="dTailor" ${busy ? 'disabled' : ''}>
          ${run && run.state === 'done' ? '↻ Regénérer CV + LM' : '⚡ Adapter CV + LM'}
        </button>
        ${applyButton(o)}
        ${o.url ? `<a class="btn" href="${esc(o.url)}" target="_blank" rel="noopener">offre ↗</a>` : ''}
      </div>
      <div class="apply-line">${applyHint(o)}</div>
      ${(!o.apply || !o.apply.url) && !applyBusy(o)
        ? '<div style="margin-top:8px"><button class="btn small ghost" id="dHunt">chercher le lien de candidature</button></div>'
        : ''}
      ${runHtml}
    </div>
    <div>
      <p class="sect-title">Détails</p>
      <dl class="kv">
        <dt>Entreprise</dt><dd>${esc(o.company || '—')}</dd>
        <dt>Lieu</dt><dd>${esc(o.location || '—')}</dd>
        <dt>Contrat</dt><dd>${esc(o.contract || '—')}</dd>
        <dt>Trouvée le</dt><dd>${esc(fmtDate(o.found_at))} · ${esc(o.source || '')}</dd>
        <dt>Postulé le</dt><dd>${o.applied_at ? esc(fmtDate(o.applied_at)) : '—'}</dd>
      </dl>
    </div>
    <div>
      <p class="sect-title">Notes</p>
      <textarea id="dNotes" placeholder="contact, salaire, relance prévue…">${esc(o.notes || '')}</textarea>
    </div>
    <div>
      <p class="sect-title">Description de l’offre</p>
      <div class="desc">${esc(o.description || 'aucune description enregistrée')}</div>
    </div>
    <div>
      <p class="sect-title">Historique</p>
      <div class="timeline">${tl || '<div class="empty">—</div>'}</div>
    </div>
    <div>
      <button class="btn small ghost" id="dDelete" style="color:var(--bad)">supprimer cette offre</button>
    </div>`;

  $('#dStatus').onchange = async (e) => {
    await fetch('/api/offers/' + o.id, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: e.target.value })
    });
    load(true); refreshDrawer();
  };
  $('#dNotes').onblur = async (e) => {
    await fetch('/api/offers/' + o.id, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notes: e.target.value })
    });
    toast('Note enregistrée');
  };
  $('#dTailor').onclick = () => tailor(o.id);
  if ($('#dHunt')) $('#dHunt').onclick = () => hunt(o.id);
  $('#dDelete').onclick = async () => {
    if (!confirm('Supprimer « ' + o.title + ' » du hub ?\n\nElle ne sera plus réimportée par le cron. Pour la récupérer, il faudra la rajouter à la main.')) return;
    await fetch('/api/offers/' + o.id, { method: 'DELETE' });
    closeDrawer(); load(true); toast('Offre supprimée');
  };
}

async function hunt(id) {
  const r = await fetch(`/api/offers/${id}/apply-link`, { method: 'POST' });
  if (!r.ok) { toast('Recherche du lien impossible'); return; }
  const d = await r.json();
  toast(d.apply_state === 'queued' ? 'Recherche du lien de candidature lancée' : 'Recherche déjà en cours');
  load(true);
  if (state.openId === id) refreshDrawer();
  if (state.view === 'offer' && state.offerId === id) fetchOffer(id);
}

async function tailor(id) {
  if (!confirm('Lancer l’adaptation CV + LM ?\n\nCela lance un agent complet (recherche entreprise, CV, lettre, PDF, QA, mail à toi). Compte 5 à 15 minutes.')) return;
  const r = await fetch(`/api/offers/${id}/tailor`, { method: 'POST' });
  if (!r.ok) { toast('Lancement impossible'); return; }
  toast('Run lancé — tu peux fermer cette fenêtre, le hub continue.');
  refreshDrawer(); load(true);
}

async function pollLog(runId, elId) {
  const el = document.getElementById(elId || 'logs');
  if (!el || el.offsetParent === null) return;
  try {
    const r = await fetch(`/api/runs/${runId}/log?lines=40`);
    const d = await r.json();
    el.textContent = (d.lines || []).join('\n') || '(en attente de sortie)';
    el.scrollTop = el.scrollHeight;
  } catch (_) { /* silencieux */ }
}

async function toggleLog(runId, elId) {
  const el = document.getElementById(elId || 'logs');
  if (!el) return;
  el.style.display = '';
  const r = await fetch(`/api/runs/${runId}/log?lines=200`);
  const d = await r.json();
  el.textContent = (d.lines || []).join('\n') || '(log vide)';
}

/* ---------------- fiche d'une offre (onglet dédié) ---------------- */

/* Markdown minimal et sûr : on échappe d'abord, on ne reconnaît ensuite que
   les formes utilisées par le dossier entreprise (#, ##, listes, gras, liens).
   Aucune balise brute venue du fichier ne peut donc s'exécuter. */
function inlineMd(s) {
  return s
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

function md(text) {
  const lines = esc(text || '').split(/\r?\n/);
  const out = [];
  let para = [], list = null, items = [], gap = false;
  const closeP = () => {
    if (para.length) { out.push('<p>' + inlineMd(para.join(' ')) + '</p>'); para = []; }
  };
  const closeL = () => {
    if (list) {
      out.push(`<${list}>` + items.map((t) => '<li>' + inlineMd(t) + '</li>').join('') + `</${list}>`);
      list = null; items = [];
    }
  };
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) { closeP(); gap = true; continue; }
    const head = line.match(/^(#{1,6})\s+(.*)$/);
    if (head) {
      closeP(); closeL(); gap = false;
      const tag = head[1].length <= 2 ? 'h2' : 'h3';
      out.push(`<${tag}>${inlineMd(head[2])}</${tag}>`);
      continue;
    }
    if (/^(-{3,}|\*{3,})$/.test(line)) { closeP(); closeL(); gap = false; out.push('<hr>'); continue; }
    const ul = line.match(/^[-*]\s+(.*)$/);
    const ol = line.match(/^\d+\.\s+(.*)$/);
    if (ul || ol) {
      closeP();
      const kind = ul ? 'ul' : 'ol';
      if (list && list !== kind) closeL();
      list = kind;
      items.push((ul || ol)[1]);
      gap = false;
      continue;
    }
    if (line.startsWith('&gt;')) {
      closeP(); closeL(); gap = false;
      out.push('<blockquote>' + inlineMd(line.slice(4).trim()) + '</blockquote>');
      continue;
    }
    // retour à la ligne dans une puce : markdown le rend comme un espace
    if (list && !gap) { items[items.length - 1] += ' ' + line; continue; }
    closeL();
    gap = false;
    para.push(line);
  }
  closeP(); closeL();
  return out.join('');
}

function fmtSize(n) {
  if (!n) return '';
  return n >= 1048576 ? (n / 1048576).toFixed(1).replace('.', ',') + ' Mo'
    : Math.max(1, Math.round(n / 1024)) + ' Ko';
}

function stepsHtml(o) {
  const seq = ['new', 'shortlist', 'applied', 'interview', 'offer'];
  const i = seq.indexOf(o.status);
  return seq.map((s, k) => {
    const cls = o.status === 'rejected' ? 'step'
      : k === i ? 'step now' : i > k ? 'step done' : 'step';
    return `<span class="${cls}">${LABELS[s]}</span>`;
  }).join('') + (o.status === 'rejected' ? '<span class="step now bad">Refusé</span>' : '');
}

function applyPanel(o) {
  const a = o.apply || { state: 'idle' };
  let body = '';
  if (a.url) {
    body += `<a class="btn apply wide" href="${esc(a.url)}" target="_blank" rel="noopener">Postuler ↗</a>
      <div class="apply-line">${applyHint(o)}</div>
      ${a.note ? `<p class="hint-line" style="margin-top:8px">${esc(a.note)}</p>` : ''}`;
    if ((a.alternatives || []).length) {
      body += `<details class="alts"><summary>${a.alternatives.length} autre${a.alternatives.length > 1 ? 's' : ''} piste${a.alternatives.length > 1 ? 's' : ''}</summary>
        <ul class="md tight">${a.alternatives.map((x) => `<li><a href="${esc(x.url)}" target="_blank"
          rel="noopener">${esc(APPLY_KIND[x.kind] || 'lien')}</a>${x.note ? ' — ' + esc(x.note) : ''}</li>`).join('')}</ul></details>`;
    }
  } else if (applyBusy(o)) {
    body = `<div class="state"><span class="dot" style="color:var(--warn)"></span>
      <span>recherche du lien de candidature…</span></div>`;
  } else if (a.state === 'not_found') {
    body = `<p class="hint-line">${esc(a.note || 'Aucun lien de candidature vérifié trouvé.')} Colle le lien ci-dessous.</p>`;
  } else if (a.state === 'failed') {
    body = `<p class="hint-line">Recherche impossible : ${esc(a.note || 'erreur inconnue')}. Colle le lien ci-dessous.</p>`;
  } else {
    body = '<p class="hint-line">Pas encore de lien : la recherche démarre quand l’offre passe en « À postuler ».</p>';
  }
  return `${body}
    <details class="edit-link"><summary>corriger le lien</summary>
      <label class="f">URL de candidature
        <input type="text" id="oApplyUrl" value="${esc(a.url || '')}" placeholder="https://…">
      </label>
      <p class="hint-line" style="margin-top:6px">Enregistré à la sortie du champ. Vider le champ efface le lien.</p>
    </details>
    <div class="files" style="margin-top:10px">
      <button class="btn small ghost" id="oHunt">${a.url ? '↻ relancer la recherche' : 'chercher le lien'}</button>
    </div>`;
}

function docRow(o, d) {
  return `
    <div class="doc">
      <span class="ext">${esc(d.ext)}</span>
      <span class="name" title="${esc(d.filename)}">${esc(d.label)}</span>
      <span class="size">${fmtSize(d.size)}</span>
      <a class="btn small" href="/api/offers/${o.id}/doc/${d.kind}"
         title="télécharger ${esc(d.filename)}">↓ télécharger</a>
      ${d.ext === 'pdf' ? `<a class="btn small ghost"
          href="/api/offers/${o.id}/doc/${d.kind}?inline=true" target="_blank"
          rel="noopener" title="aperçu dans le navigateur">aperçu ↗</a>` : ''}
    </div>`;
}

function openOffer(id) {
  const el = $('#detail');
  if (!el || !id) return;
  if (!el.dataset.id || Number(el.dataset.id) !== id) {
    el.dataset.id = String(id);
    el.innerHTML = '<div class="empty">chargement…</div>';
  }
  fetchOffer(id);
}

async function fetchOffer(id) {
  let r;
  try {
    r = await fetch('/api/offers/' + id);
  } catch (e) {
    $('#detail').innerHTML = '<div class="empty">Hub injoignable.</div>';
    return;
  }
  if (!r.ok) {
    $('#detail').innerHTML = `<div class="empty big"><b>Offre introuvable.</b><br>
      Elle a peut-être été supprimée. <a href="#/nouvelles">Retour aux nouvelles offres</a></div>`;
    document.title = 'jobhunter';
    return;
  }
  const o = await r.json();
  if (state.view !== 'offer' || state.offerId !== id) return;
  renderOffer(o);
  clearTimeout(state.offerTimer);
  const busy = o.run && (o.run.state === 'running' || o.run.state === 'queued');
  state.offerTimer = setTimeout(() => fetchOffer(id), busy || applyBusy(o) ? 4000 : 30000);
  if (busy) pollLog(o.run.id, 'oLogs');
}

function renderOffer(o) {
  document.title = o.title + ' — jobhunter';
  const run = o.run;
  const busy = run && (run.state === 'running' || run.state === 'queued');
  const m = (run && run.manifest) || {};
  const research = o.research;

  const opts = Object.keys(LABELS).map((s) =>
    `<option value="${s}" ${o.status === s ? 'selected' : ''}>${LABELS[s]}</option>`).join('');

  const researchHtml = research
    ? `${research.kind === 'summary'
        ? '<p class="hint-line">Résumé du manifest — le dossier de recherche complet n\'a pas été conservé pour ce run.</p>'
        : ''}
       <div class="md">${md(research.markdown)}</div>
       ${research.truncated ? '<p class="hint-line">Dossier tronqué à l\'affichage.</p>' : ''}`
    : `<div class="empty">Aucune recherche entreprise : lance « ⚡ Adapter CV + LM »
         pour que l'agent produise le dossier.</div>`;

  const adapted = (m.adapted || []).map((a) => `<li>${esc(a)}</li>`).join('');
  const gaps = (m.gaps || []).map((g) => `<li>${esc(g)}</li>`).join('');

  let runHtml = '<div class="empty">Aucun run d\'adaptation.</div>';
  if (run) {
    const label = run.state === 'queued' ? 'en file d’attente'
      : run.state === 'running' ? `en cours depuis ${ago(run.started_at).replace('il y a ', '')}`
      : run.state === 'done' ? `terminé ${ago(run.finished_at)}`
      : `échec ${ago(run.finished_at)}`;
    runHtml = `
      <div class="runbox">
        <div class="state">
          ${busy ? '<span class="dot" style="color:var(--warn)"></span>' : ''}
          <span>Run #${run.id} — ${esc(label)}</span>
        </div>
        ${run.error ? `<div class="err">${esc(run.error)}</div>` : ''}
        ${busy ? '<pre class="logs" id="oLogs">chargement du log…</pre>'
               : `<div style="margin-top:11px"><button class="btn small ghost" onclick="toggleLog(${run.id}, 'oLogs')">voir le log</button></div>
                  <pre class="logs" id="oLogs" style="display:none"></pre>`}
      </div>`;
  }

  const docs = (o.documents || []).length
    ? o.documents.map((d) => docRow(o, d)).join('')
    : '<div class="empty">Aucun document. Lance une adaptation.</div>';

  const tl = (o.events || []).map((e) => `
    <div class="tl"><span class="b ${e.kind === 'documents_ready' || e.kind === 'status' ? 'on' : ''}"></span>
      <div>${esc(e.message || e.kind)}<div class="d">${esc(e.kind)} · ${new Date(e.at).toLocaleString('fr-FR')}</div></div>
    </div>`).join('');

  const backHref = o.status === 'new' ? '#/nouvelles' : '#/candidatures';
  const backLabel = o.status === 'new' ? '← retour aux nouvelles offres' : '← retour au suivi';

  $('#detail').innerHTML = `
    <div class="detail-top">
      <a class="back" href="${backHref}">${backLabel}</a>
      <div class="detail-head">
        <div class="detail-title">
          <div class="card-top">
            ${scoreHtml(o.score)}
            ${runChip(o)}
          </div>
          <h1>${esc(o.title)}</h1>
          <div class="company">${esc(o.company || '—')}${o.location ? ' · ' + esc(o.location) : ''}${o.contract ? ' · ' + esc(o.contract) : ''}</div>
        </div>
        <div class="detail-actions">
          <label class="f">Statut<select id="oStatus">${opts}</select></label>
          <button class="btn primary" id="oTailor" ${busy ? 'disabled' : ''}>
            ${busy ? 'en cours…' : run && run.state === 'done' ? '↻ Regénérer CV + LM' : '⚡ Adapter CV + LM'}
          </button>
          ${applyButton(o)}
          ${o.url ? `<a class="btn" href="${esc(o.url)}" target="_blank" rel="noopener">offre ↗</a>` : ''}
        </div>
      </div>
      <div class="steps">${stepsHtml(o)}</div>
    </div>

    <div class="detail-body">
      <div class="detail-main">
        <section class="panel">
          <p class="sect-title">Entreprise</p>
          ${researchHtml}
        </section>

        <section class="panel">
          <p class="sect-title">Ce qui a été adapté</p>
          ${adapted ? `<ul class="md tight">${adapted}</ul>` : '<div class="empty">Rien à afficher tant qu\'aucun run n\'a abouti.</div>'}
          ${gaps ? `<p class="sub-title">Écarts assumés</p><ul class="md tight">${gaps}</ul>` : ''}
        </section>

        <section class="panel">
          <p class="sect-title">Description de l’offre</p>
          <div class="desc tall">${esc(o.description || 'aucune description enregistrée')}</div>
        </section>

        <section class="panel">
          <p class="sect-title">Notes</p>
          <textarea id="oNotes" placeholder="contact, salaire, relance prévue…">${esc(o.notes || '')}</textarea>
        </section>

        <section class="panel">
          <p class="sect-title">Historique</p>
          <div class="timeline">${tl || '<div class="empty">—</div>'}</div>
        </section>
      </div>

      <aside class="detail-rail">
        <section class="panel">
          <p class="sect-title">Candidature</p>
          ${applyPanel(o)}
        </section>

        <section class="panel">
          <p class="sect-title">Documents</p>
          <div class="doc-list">${docs}</div>
        </section>

        <section class="panel">
          <p class="sect-title">Génération</p>
          ${runHtml}
        </section>

        <section class="panel">
          <p class="sect-title">Détails</p>
          <dl class="kv">
            <dt>Entreprise</dt><dd>${esc(o.company || '—')}</dd>
            <dt>Lieu</dt><dd>${esc(o.location || '—')}</dd>
            <dt>Contrat</dt><dd>${esc(o.contract || '—')}</dd>
            <dt>Trouvée le</dt><dd>${esc(fmtDate(o.found_at))} · ${esc(o.source || '')}</dd>
            <dt>Postulé le</dt><dd>${o.applied_at ? esc(fmtDate(o.applied_at)) : '—'}</dd>
            ${o.url ? `<dt>Lien</dt><dd><a href="${esc(o.url)}" target="_blank" rel="noopener">${esc(o.url.length > 48 ? o.url.slice(0, 48) + '…' : o.url)}</a></dd>` : ''}
          </dl>
        </section>

        <section class="panel">
          <button class="btn small ghost danger" id="oDelete">supprimer cette offre</button>
        </section>
      </aside>
    </div>`;

  $('#oStatus').onchange = async (e) => {
    await fetch('/api/offers/' + o.id, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: e.target.value })
    });
    load(true); fetchOffer(o.id);
  };
  $('#oNotes').onblur = async (e) => {
    await fetch('/api/offers/' + o.id, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notes: e.target.value })
    });
    toast('Note enregistrée');
  };
  $('#oTailor').onclick = () => tailor(o.id);
  $('#oHunt').onclick = () => hunt(o.id);
  $('#oApplyUrl').onblur = async (e) => {
    const url = e.target.value.trim();
    if (url === (o.apply && o.apply.url ? o.apply.url : '')) return;
    await fetch('/api/offers/' + o.id, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ apply_url: url })
    });
    toast(url ? 'Lien de candidature enregistré' : 'Lien de candidature effacé');
    load(true); fetchOffer(o.id);
  };
  $('#oDelete').onclick = async () => {
    if (!confirm('Supprimer « ' + o.title + ' » du hub ?\n\nElle ne sera plus réimportée par le cron. Pour la récupérer, il faudra la rajouter à la main.')) return;
    await fetch('/api/offers/' + o.id, { method: 'DELETE' });
    load(true); toast('Offre supprimée');
    location.hash = '#/nouvelles';
  };
}

function stopOffer() {
  clearTimeout(state.offerTimer);
  state.offerTimer = null;
  document.title = 'jobhunter';
}

/* ---------------- ajout manuel ---------------- */

function openModal() { $('#modalWrap').classList.add('open'); $('#fTitle').focus(); }
function closeModal() {
  $('#modalWrap').classList.remove('open');
  ['#fTitle', '#fCompany', '#fLocation', '#fContract', '#fUrl', '#fDesc'].forEach((s) => $(s).value = '');
}

async function saveOffer() {
  const body = {
    title: $('#fTitle').value.trim(),
    company: $('#fCompany').value.trim() || null,
    location: $('#fLocation').value.trim() || null,
    contract: $('#fContract').value.trim() || null,
    url: $('#fUrl').value.trim() || null,
    description: $('#fDesc').value.trim() || null,
    source: 'manuel'
  };
  if (!body.title) { toast('Il faut au moins un intitulé'); return; }
  const r = await fetch('/api/offers', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  });
  const d = await r.json();
  if (!r.ok) { toast(d.detail || d.error || 'ajout impossible'); return; }
  if (d.dismissed) { toast('Cette offre avait été supprimée — rétablis-la en la recréant ici.'); return; }
  closeModal(); load(true);
  toast(d.created ? 'Offre ajoutée' : 'Offre déjà présente — complétée');
  openDrawer(d.id);
}

/* ---------------- wiring ---------------- */

$('#add').onclick = openModal;
$('#mCancel').onclick = closeModal;
$('#mSave').onclick = saveOffer;
$('#modalWrap').addEventListener('click', (e) => { if (e.target.id === 'modalWrap') closeModal(); });
$('#dClose').onclick = closeDrawer;
$('#drawerWrap').addEventListener('click', (e) => { if (e.target.id === 'drawerWrap') closeDrawer(); });
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { closeDrawer(); closeModal(); }
});
$('#q').addEventListener('input', (e) => { state.q = e.target.value.trim(); render(); });
$('#refresh').onclick = async () => {
  await fetch('/api/ingest', { method: 'POST' });
  await load();
  toast('À jour');
};

route();
loadSites();
load();
