/* Inkify AI - "My Own Handwriting" (Part 1: template, sample upload, profile management).
   Self-contained: only uses the global toast() from script.js when present. API: /api/hw/* (see own_handwriting.py). */
(() => {
  'use strict';
  const $ = s => document.querySelector(s);
  const API = '/api/hw';
  const L = {max_sample_bytes: 8 * 1024 * 1024, max_samples: 20, name_max: 40, extensions: ['png', 'jpg', 'jpeg', 'pdf']};   // overwritten by /limits
  const fmtSize = b => b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB';
  const note = (m, t = '') => (typeof window.toast === 'function' ? window.toast(m, t) : console.log(m));
  const icons = () => window.lucide && window.lucide.createIcons();
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;

  // ------------------------------------------------------------------------------------------ state
  const cur = {id: null, saved: false, name: ''};           // the profile being created / edited
  let items = [];                                           // sample cards: {key, id, name, size, file, state, pct, msg, kind, meta, thumb, local, node}
  let creating = null, running = false, uid = 0, ready = false;

  async function api(method, url, body) {
    let r, j = null;
    try {
      r = await fetch(API + url, {method, headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined, credentials: 'same-origin'});
    } catch (_) { throw Object.assign(new Error('Network error. Check your connection and try again.'), {status: 0}); }
    try { j = await r.json(); } catch (_) { /* non-JSON */ }
    if (!r.ok) throw Object.assign(new Error((j && j.error) || `Request failed (${r.status}).`), {status: r.status, code: j && j.code});
    return j;
  }

  // ------------------------------------------------------------------------------------------ routing (dedicated section)
  function route() {
    const own = location.hash === '#my-handwriting';
    document.body.classList.toggle('view-own', own);
    if (own) { init(); window.scrollTo({top: 0}); }
    else if (location.hash.length > 1) {                     // hidden sections can't be scrolled to by the browser: do it once they're visible again
      let t = null; try { t = document.getElementById(decodeURIComponent(location.hash.slice(1))); } catch (_) { /* bad hash */ }
      if (t) requestAnimationFrame(() => t.scrollIntoView());
    } else window.scrollTo({top: 0});
  }
  window.addEventListener('hashchange', route);

  async function init() {
    if (ready) return; ready = true;
    icons();
    api('GET', '/limits').then(j => {
      Object.assign(L, j);
      $('#hwLimits').textContent = `Up to ${L.max_samples} files · max ${L.max_sample_bytes / 1048576} MB each · images at least ${j.min_side} px on the shorter side · PDFs up to ${j.max_pdf_pages} pages.`;
    }).catch(() => {});
    loadProfiles();
  }

  // ------------------------------------------------------------------------------------------ name field
  const nameIn = $('#hwName');
  function nameError(msg) { const e = $('#hwNameErr'); e.hidden = !msg; e.textContent = msg || ''; nameIn.classList.toggle('bad', !!msg); }
  nameIn.addEventListener('input', () => { $('#hwNameCount').textContent = nameIn.value.length; nameError(''); });
  document.querySelectorAll('[data-name]').forEach(b => b.addEventListener('click', () => { nameIn.value = b.dataset.name; nameIn.dispatchEvent(new Event('input')); nameIn.focus(); }));
  nameIn.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); $('#hwSave').click(); } });

  // ------------------------------------------------------------------------------------------ choosing files
  const drop = $('#hwDrop'), input = $('#hwFile');
  const openPicker = () => { if (items.length < L.max_samples) input.click(); else note(`You can add up to ${L.max_samples} samples per profile.`, 'err'); };
  drop.addEventListener('click', e => { if (!e.target.closest('button')) openPicker(); });
  drop.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openPicker(); } });
  $('#hwBrowse').addEventListener('click', openPicker);
  $('#hwAdd').addEventListener('click', openPicker);
  ['dragenter', 'dragover'].forEach(v => drop.addEventListener(v, e => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(v => drop.addEventListener(v, e => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', e => addFiles(e.dataTransfer.files));
  input.addEventListener('change', () => { addFiles(input.files); input.value = ''; });
  // a file dropped outside the zone must not make the browser navigate away from the page
  ['dragover', 'drop'].forEach(v => window.addEventListener(v, e => { if (document.body.classList.contains('view-own') && !e.target.closest('#hwDrop')) e.preventDefault(); }));

  const extOf = n => (n.includes('.') ? n.split('.').pop() : '').toLowerCase();

  function check(file) {                                   // same rules as the server (which re-checks everything)
    if (!L.extensions.includes(extOf(file.name))) return 'Unsupported file type. Please use PNG, JPG, JPEG or PDF.';
    if (file.size === 0) return 'This file is empty (0 bytes).';
    if (file.size > L.max_sample_bytes) return `Too large (${fmtSize(file.size)}). Max ${L.max_sample_bytes / 1048576} MB per sample.`;
    if (items.some(i => i.file && i.state !== 'error' && i.file.name === file.name && i.file.size === file.size && i.file.lastModified === file.lastModified)) return 'This file was already added.';
    return null;
  }

  function addFiles(list) {
    const files = [...list]; if (!files.length) return;
    let rejected = 0;
    for (const f of files) {
      let msg = check(f);
      if (!msg && items.filter(i => i.state !== 'error').length >= L.max_samples) msg = `Limit reached (max ${L.max_samples} samples per profile).`;
      const it = {key: ++uid, name: f.name, size: f.size, file: f, state: msg ? 'error' : 'queued', pct: 0, msg: msg || 'Waiting…', kind: extOf(f.name) === 'pdf' ? 'PDF' : (extOf(f.name) || '').toUpperCase().replace('JPEG', 'JPG'), local: null};
      if (msg) rejected++;
      else if (/^image\//.test(f.type) || ['png', 'jpg', 'jpeg'].includes(extOf(f.name))) it.local = URL.createObjectURL(f);
      items.push(it); mount(it);
    }
    if (rejected) note(rejected === files.length ? (rejected === 1 ? 'That file could not be added – see the message below.' : `${rejected} files could not be added – see the messages below.`)
      : `${rejected} of ${files.length} files could not be added – see the messages below.`, 'err');
    refresh(); pump();
  }

  // ------------------------------------------------------------------------------------------ upload queue (one file at a time)
  async function ensureProfile() {
    if (cur.id) return cur.id;
    creating = creating || api('POST', '/profiles', {name: nameIn.value.trim()}).then(p => { cur.id = p.id; cur.saved = false; return p.id; }).finally(() => { creating = null; });
    return creating;
  }

  function sendFile(pid, it) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest(); xhr.open('POST', `${API}/profiles/${pid}/samples`); xhr.timeout = 180000; xhr.withCredentials = true;
      xhr.upload.onprogress = e => { if (e.lengthComputable) { it.pct = Math.round(e.loaded / e.total * 100); paint(it); } };
      xhr.onload = () => {
        let j = null; try { j = JSON.parse(xhr.responseText); } catch (_) { /* not JSON */ }
        if (xhr.status >= 200 && xhr.status < 300 && j) resolve(j);
        else reject(Object.assign(new Error((j && j.error) || (xhr.status === 413 ? `Too large. Max ${L.max_sample_bytes / 1048576} MB per sample.` : `Upload failed (error ${xhr.status}).`)), {status: xhr.status}));
      };
      xhr.onerror = () => reject(new Error('Network error. Check your connection and try again.'));
      xhr.ontimeout = () => reject(new Error('The upload timed out. Please try again.'));
      const fd = new FormData(); fd.append('file', it.file); xhr.send(fd);
    });
  }

  async function pump() {
    if (running) return; running = true;
    try {
      let it;
      while ((it = items.find(i => i.state === 'queued'))) {
        it.state = 'uploading'; it.pct = 0; paint(it); refresh();
        try {
          const pid = await ensureProfile();
          const j = await sendFile(pid, it);
          Object.assign(it, {id: j.id, thumb: j.thumb, url: j.url, kind: j.kind === 'pdf' ? 'PDF' : j.kind.toUpperCase(), meta: j, state: 'done', file: null});
        } catch (e) {
          it.state = 'error'; it.msg = e.message || 'Upload failed.'; it.file = null;
          if (e.status === 404) cur.id = null;             // the draft expired on the server: the next upload starts a fresh one
        }
        paint(it); refresh();
      }
    } finally { running = false; }
  }

  // ------------------------------------------------------------------------------------------ sample cards
  const grid = $('#hwGrid');
  function card(it, tag = 'article') {
    const n = el(tag, 'hw-sample in');
    n.innerHTML = '<div class="hw-thumb"><i data-lucide="file-text" class="ph"></i><span class="hw-kind"></span></div><div class="hw-meta"><b></b><span></span></div><div class="hw-prog"><i></i></div>';
    n.querySelector('b').textContent = it.name;           // textContent: file names are never parsed as HTML
    n.querySelector('b').title = it.name;
    return n;
  }
  function mount(it) {
    const n = it.node = card(it);
    const rm = el('button', 'icon hw-rm', '<i data-lucide="x"></i>'); rm.type = 'button';
    rm.addEventListener('click', () => removeItem(it)); n.append(rm); it.rm = rm;
    grid.append(n); paint(it); icons();
  }
  function paint(it) {
    const n = it.node; if (!n) return;
    n.dataset.state = it.state;
    n.querySelector('.hw-kind').textContent = it.kind || '';
    n.querySelector('.hw-prog i').style.width = it.pct + '%';
    const th = n.querySelector('.hw-thumb'), src = it.state === 'done' ? it.thumb : it.local;
    let img = th.querySelector('img');
    if (src && it.state !== 'error') {
      if (!img) { img = el('img'); img.alt = `Preview of ${it.name}`; img.loading = 'lazy'; img.addEventListener('error', () => img.remove()); th.prepend(img); }
      if (img.getAttribute('src') !== src) img.src = src;
    } else if (img) img.remove();
    if (it.state === 'done' && it.local) { URL.revokeObjectURL(it.local); it.local = null; }
    let sub = it.msg;
    if (it.state === 'queued') sub = 'Waiting…';
    else if (it.state === 'uploading') sub = it.pct >= 100 ? 'Checking file…' : `Uploading ${it.pct}%`;
    else if (it.state === 'removing') sub = 'Removing…';
    else if (it.state === 'done') sub = [fmtSize(it.size), it.meta && it.meta.width ? `${it.meta.width}×${it.meta.height}` : (it.meta && it.meta.pages ? plural(it.meta.pages, 'page') : '')].filter(Boolean).join(' · ');
    n.querySelector('.hw-meta span').textContent = sub;
    if (it.rm) it.rm.setAttribute('aria-label', it.state === 'error' ? `Dismiss ${it.name}` : `Remove ${it.name}`);
  }

  async function removeItem(it) {
    if (it.state === 'uploading') return;
    if (it.state === 'done' && it.id && cur.id) {
      it.state = 'removing'; paint(it);
      try { await api('DELETE', `/profiles/${cur.id}/samples/${it.id}`); }
      catch (e) { if (e.status !== 404) { it.state = 'done'; paint(it); return note(e.message, 'err'); } }
    }
    if (it.local) URL.revokeObjectURL(it.local);
    items = items.filter(i => i !== it); it.node.remove(); refresh();
  }

  // ------------------------------------------------------------------------------------------ summary + button states
  function refresh() {
    const done = items.filter(i => i.state === 'done'), busy = items.some(i => i.state === 'queued' || i.state === 'uploading');
    const bytes = done.reduce((a, i) => a + i.size, 0), errs = items.filter(i => i.state === 'error').length;
    $('#hwSummary').textContent = items.length ? `${plural(done.length, 'sample')} ready · ${fmtSize(bytes)}${busy ? ' · uploading…' : ''}${errs ? ` · ${plural(errs, 'problem')}` : ''}` : 'No samples yet';
    $('#hwEmpty').hidden = items.length > 0; $('#hwAdd').hidden = items.length === 0;
    drop.classList.toggle('full', items.length >= L.max_samples);
    $('#hwSave').disabled = !done.length || busy;
    $('#hwProcess').disabled = !done.length || busy;
    $('#hwFinalHint').textContent = !done.length ? (busy ? 'Uploading your samples…' : 'Upload at least one sample to continue.')
      : busy ? 'Finishing the upload…' : `${plural(done.length, 'sample')} ready. Name your profile and save it – then process it in the next step of the project.`;
  }
  window.addEventListener('beforeunload', e => { if (items.some(i => i.state === 'uploading')) { e.preventDefault(); e.returnValue = ''; } });

  // ------------------------------------------------------------------------------------------ save / process
  $('#hwSave').addEventListener('click', async () => {
    const name = nameIn.value.trim().replace(/\s+/g, ' ');
    if (!name) { nameError('Please enter a profile name.'); nameIn.focus(); return nameIn.scrollIntoView({block: 'center', behavior: 'smooth'}); }
    if (name.length > L.name_max) return nameError(`Profile name is too long (max ${L.name_max} characters).`);
    const b = $('#hwSave'); b.disabled = true;
    try {
      const p = await api('POST', `/profiles/${cur.id}/save`, {name});
      note(`Profile “${p.name}” saved.`, 'ok');
      resetForm(false); await loadProfiles(p.id);
      $('#hwProfilesSec').scrollIntoView({behavior: 'smooth', block: 'start'});
    } catch (e) {
      if (e.code && e.code.startsWith('name')) nameError(e.message); else note(e.message, 'err');
      refresh();
    }
  });

  $('#hwProcess').addEventListener('click', async () => {
    if (!cur.id) return note('Create a profile and upload samples first.', 'err');
    const button = $('#hwProcess');
    button.disabled = true;
    const original = button.innerHTML;
    button.textContent = 'Processing handwriting…';
    try {
      const result = await api('POST', `/profiles/${cur.id}/process`);
      note(result.message || 'Handwriting samples processed and saved.', 'ok');
      await loadProfiles(cur.id);
    } catch (e) {
      note(e.message || 'Handwriting processing failed. Please try again.', 'err');
    } finally {
      button.innerHTML = original;
      refresh();
    }
  });

  // ------------------------------------------------------------------------------------------ form reset / edit mode
  function resetForm(discardDraft = true) {
    if (discardDraft && cur.id && !cur.saved) api('DELETE', `/profiles/${cur.id}`).catch(() => {});   // unsaved draft: remove its files
    items.forEach(i => i.local && URL.revokeObjectURL(i.local));
    items = []; grid.innerHTML = ''; Object.assign(cur, {id: null, saved: false, name: ''});
    nameIn.value = ''; nameIn.dispatchEvent(new Event('input')); $('#hwEditBanner').hidden = true; refresh();
  }
  $('#hwCancelEdit').addEventListener('click', () => { resetForm(false); window.scrollTo({top: 0, behavior: 'smooth'}); });

  async function startEdit(id) {
    if (items.some(i => i.state === 'uploading')) return note('Please wait for the current upload to finish.', 'err');
    if (items.some(i => i.state === 'done') && !(cur.id === id)) {
      if (!(await confirmDlg('Discard current samples?', 'You have samples that are not saved yet. Editing another profile will discard them.', 'Discard'))) return;
    }
    let p; try { p = await api('GET', `/profiles/${id}`); } catch (e) { return note(e.message, 'err'); }
    resetForm(true);
    Object.assign(cur, {id: p.id, saved: true, name: p.name}); nameIn.value = p.name; nameIn.dispatchEvent(new Event('input'));
    for (const s of p.samples) {
      const it = {key: ++uid, id: s.id, name: s.name, size: s.bytes, state: 'done', pct: 100, kind: s.kind === 'pdf' ? 'PDF' : s.kind.toUpperCase(), meta: s, thumb: s.thumb, url: s.url};
      items.push(it); mount(it);
    }
    $('#hwEditName').textContent = `“${p.name}”`; $('#hwEditBanner').hidden = false; refresh(); icons();
    $('#hwEditBanner').scrollIntoView({behavior: 'smooth', block: 'start'});
  }

  // ------------------------------------------------------------------------------------------ dialogs
  document.querySelectorAll('.hw-dlg').forEach(d => {
    d.addEventListener('click', e => { if (e.target === d || e.target.closest('[data-close]')) d.close(); });   // backdrop / ✕ / Cancel
  });
  function confirmDlg(title, text, okLabel = 'Delete') {
    const d = $('#hwConfirmDlg'); $('#hwCfTitle').textContent = title; $('#hwCfText').textContent = text; $('#hwCfOk').textContent = okLabel;
    return new Promise(res => {
      const ok = () => { done(true); }, done = v => { d.removeEventListener('close', onClose); $('#hwCfOk').removeEventListener('click', ok); d.close(); res(v); }, onClose = () => done(false);
      $('#hwCfOk').addEventListener('click', ok); d.addEventListener('close', onClose); d.showModal();
    });
  }

  async function previewProfile(id) {
    let p; try { p = await api('GET', `/profiles/${id}`); } catch (e) { return note(e.message, 'err'); }
    $('#hwPvTitle').textContent = p.name; $('#hwPvMeta').textContent = `${plural(p.sample_count, 'sample')} · created ${fmtDate(p.created)}`;
    const g = $('#hwPvGrid'); g.innerHTML = '';
    if (!p.samples.length) g.append(el('p', 'hint', 'This profile has no samples.'));
    for (const s of p.samples) {
      const a = card({name: s.name}, 'a'); a.href = s.url; a.target = '_blank'; a.rel = 'noopener'; a.title = `Open ${s.name}`; a.classList.remove('in');
      a.querySelector('.hw-kind').textContent = s.kind === 'pdf' ? 'PDF' : s.kind.toUpperCase();
      const img = el('img'); img.src = s.thumb; img.alt = `Preview of ${s.name}`; img.addEventListener('error', () => img.remove()); a.querySelector('.hw-thumb').prepend(img);
      a.querySelector('.hw-meta span').textContent = [fmtSize(s.bytes), s.width ? `${s.width}×${s.height}` : (s.pages ? plural(s.pages, 'page') : '')].filter(Boolean).join(' · ');
      g.append(a);
    }
    icons(); $('#hwPreviewDlg').showModal();
  }

  // ------------------------------------------------------------------------------------------ saved profiles
  const fmtDate = t => new Date(t * 1000).toLocaleDateString(undefined, {day: 'numeric', month: 'short', year: 'numeric'});
  const STATES = {not_started: 'Not processed yet', queued: 'Queued', processing: 'Processing…', ready: 'Ready to use', failed: 'Processing failed'};   // Part 2 drives these

  async function loadProfiles(highlight) {
    let list = [];
    try { list = (await api('GET', '/profiles')).profiles; } catch (e) { note(e.message, 'err'); }
    window.dispatchEvent(new CustomEvent('inkify:profiles-updated', {detail: list}));
    const g = $('#hwProfiles'); g.innerHTML = '';
    $('#hwPCount').textContent = list.length; $('#hwNoProfiles').hidden = list.length > 0;
    for (const p of list) {
      const c = el('article', 'card hw-pcard'); c.dataset.id = p.id;
      c.innerHTML = '<h4></h4><div class="hw-pmeta"><span><i data-lucide="calendar"></i><i class="d"></i></span><span><i data-lucide="images"></i><i class="n"></i></span></div><div class="hw-strip"></div><span class="hw-state"></span>' +
        '<div class="hw-pbtns"><button type="button" class="btn ghost" data-a="preview"><i data-lucide="eye"></i> Preview</button><button type="button" class="btn ghost" data-a="edit"><i data-lucide="pencil"></i> Edit</button><button type="button" class="btn ghost del" data-a="delete" aria-label="Delete profile"><i data-lucide="trash-2"></i><span class="dl">Delete</span></button></div>';
      c.querySelector('h4').textContent = p.name; c.querySelector('h4').title = p.name;
      c.querySelector('.d').textContent = fmtDate(p.created); c.querySelector('.n').textContent = plural(p.sample_count, 'sample');
      c.querySelector('.hw-state').textContent = STATES[(p.processing || {}).state] || STATES.not_started;
      for (const s of p.samples) { const im = el('img'); im.src = s.thumb; im.alt = ''; im.loading = 'lazy'; im.addEventListener('error', () => im.remove()); c.querySelector('.hw-strip').append(im); }
      if (!p.samples.length) c.querySelector('.hw-strip').remove();
      c.querySelector('[data-a=preview]').addEventListener('click', () => previewProfile(p.id));
      c.querySelector('[data-a=edit]').addEventListener('click', () => startEdit(p.id));
      c.querySelector('[data-a=delete]').addEventListener('click', async () => {
        if (!(await confirmDlg(`Delete “${p.name}”?`, `This permanently deletes the profile and its ${plural(p.sample_count, 'sample')}. This cannot be undone.`))) return;
        try { await api('DELETE', `/profiles/${p.id}`); } catch (e) { if (e.status !== 404) return note(e.message, 'err'); }
        if (cur.id === p.id) resetForm(false);
        note(`Profile “${p.name}” deleted.`, 'ok'); loadProfiles();
      });
      g.append(c);
      if (p.id === highlight) { c.classList.add('flash'); }
    }
    icons();
  }

  // ------------------------------------------------------------------------------------------ boot
  refresh(); route();
})();
