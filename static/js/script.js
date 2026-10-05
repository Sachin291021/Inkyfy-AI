/* Inkify AI front-end: upload, live preview, conversion. */
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const MAX = 10 * 1024 * 1024;
const opts = {style:'student', ink:'#1c3ebe', paper:'ruled', page_size:'A4', font_size:18, line_spacing:1.5, margin:15, left_margin:10, irregularity:3,
  // pen settings: ink = body pen; pens = pens for the other roles ('body' = headings follow the body pen)
  pens:{heading:'body', important:'#c01e2a', definition:'#16803e', highlight:'#7c3abe'}, thickness:'medium', ink_intensity:100, ink_variation:3, highlight:'off'};
let docId = null, page = 1, pages = 1, zoom = 1, timer = null, seq = 0;

const customStyle = $('[data-opt=style]');
const customWrap = $('#customProfileWrap');
const customSelect = $('#customProfileSelect');
function syncCustomStyle() {
  const active = customStyle.value === 'custom';
  customWrap.hidden = !active;
  opts.custom_profile_id = active ? (customSelect.value || '') : '';
}
customSelect.addEventListener('change', () => { opts.custom_profile_id = customSelect.value; schedule(); });
customStyle.addEventListener('change', syncCustomStyle);
window.addEventListener('inkify:profiles-updated', e => {
  const selected = customSelect.value;
  customSelect.innerHTML = '<option value="">Select a saved profile</option>';
  (e.detail || []).filter(p => p.processing && p.processing.state === 'ready').forEach(p => {
    const option = document.createElement('option'); option.value = p.id; option.textContent = p.name; customSelect.append(option);
  });
  if ([...customSelect.options].some(o => o.value === selected)) customSelect.value = selected;
  opts.custom_profile_id = customSelect.value;
});
syncCustomStyle();


function toast(msg, type = '') {
  const t = document.createElement('div'); t.className = 'toast ' + type; t.textContent = msg;
  $('#toasts').append(t); setTimeout(() => t.remove(), 4500);
}
const size = b => b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB';
const icons = () => window.lucide && lucide.createIcons();

// ---- nav ----
$('#burger').onclick = () => { const o = $('#menu').classList.toggle('open'); $('#burger').setAttribute('aria-expanded', o); };
$$('#menu a').forEach(a => a.addEventListener('click', () => $('#menu').classList.remove('open')));

// ---- upload ----
const drop = $('#uploadCard'), input = $('#file');
drop.onclick = e => { if (!e.target.closest('button') && !docId) input.click(); };
drop.onkeydown = e => { if ((e.key === 'Enter' || e.key === ' ') && !docId) { e.preventDefault(); input.click(); } };
['dragenter', 'dragover'].forEach(v => drop.addEventListener(v, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(v => drop.addEventListener(v, e => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => e.dataTransfer.files[0] && upload(e.dataTransfer.files[0]));
input.onchange = () => { if (input.files[0]) upload(input.files[0]); input.value = ''; };
$('#replace').onclick = e => { e.stopPropagation(); input.click(); };
$('#remove').onclick = e => { e.stopPropagation(); reset(); };

function setStatus(t, err) { const b = $('#fstatus'); b.textContent = t; b.classList.toggle('err', !!err); }

const FILE_TYPES = {docx: 'Word', pdf: 'PDF', png: 'Image', jpg: 'Image', jpeg: 'Image'};   // accepted uploads -> label in the file info area
async function upload(file) {
  const ext = (file.name.toLowerCase().match(/\.([a-z0-9]+)$/) || [])[1], type = FILE_TYPES[ext];
  if (!type) return toast('Unsupported file. Please choose a .docx, .pdf, .png, .jpg or .jpeg file.', 'err');
  if (file.size > MAX) return toast('File is too large (max 10 MB).', 'err');
  if (!file.size) return toast('This file is empty.', 'err');
  $('#dropEmpty').hidden = true; $('#dropFile').hidden = false;
  $('#fname').textContent = file.name; $('#fmeta').textContent = `${size(file.size)} · ${type}`; setStatus(type === 'Word' ? 'Uploading…' : 'Reading text…');
  const fd = new FormData(); fd.append('file', file);
  try {
    const r = await fetch('/api/upload', {method: 'POST', body: fd}), j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || (r.status >= 500 ? 'The server could not process this file. Please try again.' : 'Upload failed.'));
    docId = j.id; loadedFor = null; $('#editor').hidden = true; $('#vbody').classList.remove('edit'); setStatus('Uploaded');
    $('#fmeta').textContent = `${size(file.size)} · ${j.type || type} · ${j.words} words`;
    $('#studio').hidden = false; $('#result').hidden = true;
    render(); toast(j.ocr ? 'Text recognised – check it with “Edit text”' : 'Document uploaded', 'ok');
    if (j.notice) toast(j.notice, 'err');
    $('#studio').scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (e) { toast(e instanceof TypeError ? 'Network error. Check your connection and try again.' : e.message, 'err'); reset(); }
}
function reset() {
  docId = null; loadedFor = null; $('#editor').hidden = true; $('#vbody').classList.remove('edit'); page = 1; $('#studio').hidden = true; $('#dropFile').hidden = true; $('#dropEmpty').hidden = false; $('#result').hidden = true;
  $('#fname').textContent = ''; $('#fmeta').textContent = ''; setStatus('Ready');   // back to the initial look (no stale name / "Uploading…" after an error)
}
$('#another').onclick = () => { reset(); $('#uploadCard').scrollIntoView({behavior: 'smooth'}); };

// ---- controls ----
$$('[data-opt]').forEach(el => {
  const out = el.parentElement.querySelector('output');
  const sync = () => { opts[el.dataset.opt] = el.type === 'range' ? +el.value : el.value; if (out) out.textContent = el.value + (el.dataset.unit || ''); };
  sync(); el.addEventListener('input', () => { sync(); schedule(); });
});

// ---- pens: palette, custom colour, roles ----
const PRESET = {}; $$('#palette .pen[data-pen]').forEach(b => PRESET[b.dataset.pen] = b.dataset.hex.toLowerCase());
const HEXNAME = Object.fromEntries(Object.entries(PRESET).map(([n, h]) => [h, n]));
const ROLES = ['body', 'heading', 'important', 'definition', 'highlight'];
const cap = t => t.charAt(0).toUpperCase() + t.slice(1);
const penLabel = h => HEXNAME[(h || '').toLowerCase()] ? cap(HEXNAME[h.toLowerCase()]) : (h || '').toUpperCase();
const normHex = v => {                                   // '#1c3ebe' | '1C3EBE' | '#abc'  ->  '#rrggbb' (or null)
  v = (v || '').trim(); if (v && v[0] !== '#') v = '#' + v;
  if (/^#[0-9a-f]{3}$/i.test(v)) v = '#' + [...v.slice(1)].map(c => c + c).join('');
  return /^#[0-9a-f]{6}$/i.test(v) ? v.toLowerCase() : null;
};
const hexOf = (r, g, b) => '#' + [r, g, b].map(n => n.toString(16).padStart(2, '0')).join('');
let active = '#1c3ebe';                                   // the pen that "apply" buttons use
const penErr = msg => { const e = $('#penErr'); e.hidden = !msg; e.textContent = msg || ''; };

function setActive(hex, from) {
  active = hex;
  $$('#palette .pen[data-pen]').forEach(b => { const on = b.dataset.hex.toLowerCase() === hex; b.classList.toggle('on', on); b.setAttribute('aria-checked', on); });
  const cs = $('#customSwatch'); cs.classList.toggle('on', !HEXNAME[hex]); cs.style.setProperty('--c', hex);
  $('#activePen').style.setProperty('--c', hex); $('#apName').textContent = penLabel(hex); $('#apHex').textContent = hex.toUpperCase();
  if (from !== 'hex') $('#penHex').value = hex.toUpperCase();
  if (from !== 'rgb') [1, 3, 5].forEach((i, k) => $('#pen' + 'RGB'[k]).value = parseInt(hex.slice(i, i + 2), 16));
  if (from !== 'picker') $('#penPicker').value = hex;
  $('#applyDot').style.setProperty('--c', hex); $('#applyName').textContent = 'Apply ' + penLabel(hex);
  ['#penHex', '#penR', '#penG', '#penB'].forEach(q => $(q).classList.remove('bad')); penErr('');
}
$$('#palette .pen[data-pen]').forEach(b => b.onclick = () => setActive(b.dataset.hex.toLowerCase()));
$('#penPicker').addEventListener('input', e => setActive(e.target.value.toLowerCase(), 'picker'));
$('#penHex').addEventListener('input', e => {
  const h = normHex(e.target.value);
  if (h) setActive(h, 'hex');
  else { e.target.classList.toggle('bad', e.target.value.trim().length > 0); penErr(e.target.value.trim() ? 'Enter a HEX colour such as #1C3EBE.' : ''); }
});
['R', 'G', 'B'].forEach(k => $('#pen' + k).addEventListener('input', () => {
  const v = ['R', 'G', 'B'].map(q => $('#pen' + q).value.trim());
  const ok = v.every(x => /^\d{1,3}$/.test(x) && +x <= 255);
  ['R', 'G', 'B'].forEach((q, i) => $('#pen' + q).classList.toggle('bad', v[i] !== '' && !(/^\d{1,3}$/.test(v[i]) && +v[i] <= 255)));
  if (ok) setActive(hexOf(...v.map(Number)), 'rgb'); else penErr(v.some(x => x === '') ? '' : 'RGB values must be whole numbers from 0 to 255.');
}));
['#penHex', '#penR', '#penG', '#penB'].forEach(q => $(q).addEventListener('blur', () => setActive(active)));   // snap fields back to the active pen

const roleHex = r => r === 'body' ? opts.ink : (r === 'heading' && opts.pens.heading === 'body' ? opts.ink : opts.pens[r]);
function refreshRoles() {
  $$('.role').forEach(row => {
    const r = row.dataset.role, h = roleHex(r);
    row.querySelector('.dot').style.setProperty('--c', h);
    row.querySelector('small').textContent = penLabel(h) + (r === 'heading' && opts.pens.heading === 'body' ? ' · same as body' : '');
  });
  $$('[data-dot]').forEach(d => d.style.setProperty('--c', roleHex(d.dataset.dot)));
}
$$('[data-set]').forEach(b => b.onclick = () => {
  const r = b.dataset.set; if (r === 'body') opts.ink = active; else opts.pens[r] = active;
  refreshRoles(); schedule();
});
$('[data-same]').onclick = () => { opts.pens.heading = 'body'; refreshRoles(); schedule(); };
$$('#thickness button').forEach(b => b.onclick = () => {
  $$('#thickness button').forEach(x => { x.classList.toggle('on', x === b); x.setAttribute('aria-checked', x === b); });
  opts.thickness = b.dataset.th; schedule();
});
function legend(list) {                                  // "Pens in use" chips under the preview toolbar
  const box = $('#legend'), chips = $('#legendChips'); chips.textContent = '';
  (list || []).forEach(h => { const c = document.createElement('span'), d = document.createElement('i'); c.className = 'chip'; d.className = 'dot'; d.style.setProperty('--c', h); c.append(d, penLabel(h)); chips.append(c); });
  box.hidden = !(list && list.length);
}
setActive(active); refreshRoles();
const schedule = () => { clearTimeout(timer); if (docId) timer = setTimeout(() => render(), 350); };  // debounce

// ---- rendering / preview ----
async function api(final) {
  const r = await fetch('/api/render', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: docId, options: opts, final})});
  let j = {}; try { j = await r.json(); } catch (_) {}
  if (!r.ok) {
    const e = new Error(j.error || (r.status >= 500 ? 'The server had a problem. Please try again.' : 'Rendering failed.'));
    e.status = r.status; throw e;
  }
  return j;
}
async function render() {
  if (opts.style === 'custom' && !opts.custom_profile_id) { toast('Select a processed handwriting profile first.', 'err'); return; }
  const my = ++seq; $('#loading').classList.add('on');
  try {
    const j = await api(false); if (my !== seq) return;
    pages = j.pages; page = Math.min(page, pages); legend(j.pens); show();
  } catch (e) {
    if (my === seq) { toast(e instanceof TypeError ? 'Network error. Check your connection and try again.' : e.message, 'err'); $('#loading').classList.remove('on'); if (e.status === 404) reset(); }
  }
}
function show() {
  const img = $('#img');
  img.onload = () => $('#loading').classList.remove('on');
  img.onerror = () => { $('#loading').classList.remove('on'); toast('The preview could not be loaded. If this keeps happening, please upload your document again.', 'err'); };
  img.src = `/api/page/${docId}/${page}?t=${Date.now()}`;
  $('#pg').textContent = `${page} / ${pages}`; $('#prev').disabled = page <= 1; $('#next').disabled = page >= pages;
  const sel = k => $(`[data-opt=${k}]`).selectedOptions[0].textContent;
  $('#tag').textContent = `${sel('style')} · ${sel('paper')} · ${penLabel(opts.ink)} pen · ${cap(opts.thickness)}`;
  img.style.width = (zoom * 100) + '%'; $('#zl').textContent = Math.round(zoom * 100) + '%';
}
$('#prev').onclick = () => { if (page > 1) { page--; show(); } };
$('#next').onclick = () => { if (page < pages) { page++; show(); } };
$('#zin').onclick = () => { zoom = Math.min(2.5, zoom + .25); show(); };
$('#zout').onclick = () => { zoom = Math.max(.5, zoom - .25); show(); };

// ---- text editor (edit the converted text, preview updates live) ----
let etimer = null, loadedFor = null;
const wordCount = t => (t.trim().match(/\S+/g) || []).length;
async function openEditor(open) {
  $('#editor').hidden = !open; $('#vbody').classList.toggle('edit', open); $('#editBtn').setAttribute('aria-expanded', open);
  if (!open) return;
  if (loadedFor !== docId) {
    const j = await (await fetch('/api/doc/' + docId)).json(); $('#txt').value = j.text || ''; loadedFor = docId;
  }
  $('#wc').textContent = wordCount($('#txt').value) + ' words'; $('#txt').focus();
}
async function saveText() {
  const text = $('#txt').value; $('#wc').textContent = wordCount(text) + ' words';
  try {
    const r = await fetch('/api/doc', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: docId, text})});
    const j = await r.json(); if (!r.ok) throw new Error(j.error || 'Could not save.');
    $('#result').hidden = true; pdfUrl = null; render();   // old PDF is stale until converted again
  } catch (e) { toast(e.message, 'err'); }
}
$('#editBtn').onclick = () => openEditor($('#editor').hidden);
$('#editAfter').onclick = () => { openEditor(true); $('#editor').scrollIntoView({behavior: 'smooth', block: 'center'}); };
$('#doneEdit').onclick = () => openEditor(false);
$('#txt').addEventListener('input', () => { clearTimeout(etimer); etimer = setTimeout(saveText, 700); $('#wc').textContent = wordCount($('#txt').value) + ' words'; });
$('#resetTxt').onclick = async () => {
  const j = await (await fetch('/api/doc/' + docId + '?original=1')).json(); $('#txt').value = j.text || ''; saveText(); toast('Text reset to the original');
};


// ---- colour tools in the text editor (pen tags, same grammar as utils/pens.py) ----
const KNOWN = new Set([...Object.keys(PRESET), 'darkblue', ...ROLES]);
const TAGRE = /\{(\/|[A-Za-z]{3,10}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3}|rgb\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*\))\}/g;
const validTok = t => t === '/' || KNOWN.has(t) || /^#([0-9a-f]{3}|[0-9a-f]{6})$/.test(t) || (/^rgb\(\d+,\d+,\d+\)$/.test(t) && t.match(/\d+/g).every(n => +n <= 255));
const normTok = t => t === '/' ? t : t.toLowerCase().replace(/\s+/g, '');
const stripTags = s => s.replace(TAGRE, (m, t) => validTok(normTok(t)) ? '' : m);
const tokenFor = hex => HEXNAME[hex] || hex;
function parseRuns(text) {                                // line -> [{t: text, tok: pen|null}]
  const tags = []; let m; TAGRE.lastIndex = 0;
  while ((m = TAGRE.exec(text))) { const t = normTok(m[1]); if (validTok(t)) tags.push([m.index, m.index + m[0].length, t]); }
  let line = null, pos = 0; const lead = text.length - text.trimStart().length;
  if (tags.length && tags[0][0] <= lead && tags[0][2] !== '/' && tags.every(t => t[2] !== '/')) { line = tags[0][2]; pos = tags[0][1]; tags.shift(); }
  const runs = []; let cur = line;
  for (const [s0, e0, t] of tags) { if (s0 > pos) runs.push({t: text.slice(pos, s0), tok: cur}); cur = t === '/' ? line : t; pos = e0; }
  if (pos < text.length) runs.push({t: text.slice(pos), tok: cur});
  return runs;
}
function paint(runs, a, b, tok) {                         // give plain-text range [a,b) a pen (null = default pen)
  const out = []; let pos = 0;
  for (const r of runs) {
    const s0 = pos, e0 = pos + r.t.length; pos = e0;
    if (e0 <= a || s0 >= b) { out.push(r); continue; }
    const i = Math.max(a, s0) - s0, j = Math.min(b, e0) - s0;
    if (i > 0) out.push({t: r.t.slice(0, i), tok: r.tok});
    out.push({t: r.t.slice(i, j), tok});
    if (j < r.t.length) out.push({t: r.t.slice(j), tok: r.tok});
  }
  return out;
}
function serialize(runs) {
  const m = [];
  for (const r of runs) { if (!r.t) continue; if (m.length && m[m.length - 1].tok === r.tok) m[m.length - 1].t += r.t; else m.push({...r}); }
  if (!m.length) return '';
  if (m.length === 1) return m[0].tok ? `{${m[0].tok}} ${m[0].t.trimStart()}` : m[0].t;       // whole line in one pen
  return m.map(r => r.tok ? `{${r.tok}}${r.t}{/}` : r.t).join('');                              // coloured spans
}
function replaceRange(ta, a, b, text) {                    // keeps the browser's undo history when possible
  ta.focus(); ta.setSelectionRange(a, b);
  let ok = false; try { ok = document.execCommand('insertText', false, text); } catch (_) {}
  if (!ok || ta.value.slice(a, a + text.length) !== text) { ta.setRangeText(text, a, b, 'end'); ta.dispatchEvent(new Event('input', {bubbles: true})); }
}
function applyColour(tok) {                                // tok: pen name / role / #hex, or null = remove colour
  const ta = $('#txt'), v = ta.value; if (!v.trim()) return toast('Type or upload some text first.', 'err');
  let a = ta.selectionStart, b = ta.selectionEnd; if (b > a && v[b - 1] === '\n') b--;      // triple-click selects the line break too
  const ls = v.lastIndexOf('\n', a - 1) + 1; let le = v.indexOf('\n', b); if (le < 0) le = v.length;
  const lines = v.slice(ls, le).split('\n'); let off = ls, done = 0;
  const inline = b > a && lines.length === 1;
  const out = lines.map(line => {
    const head = (line.match(/^#{1,3}\s+/) || [''])[0], body = line.slice(head.length), plain = stripTags(body);
    if (!plain.trim()) return line;
    const runs = parseRuns(body); let pa = 0, pb = plain.length;
    if (inline) {
      pa = stripTags(body.slice(0, Math.max(0, a - off - head.length))).length; pb = stripTags(body.slice(0, Math.max(0, b - off - head.length))).length;
      const t0 = plain.length - plain.trimStart().length, t1 = plain.trimEnd().length;
      if (pa <= t0 && pb >= t1) { pa = 0; pb = plain.length; }                                // selection covers the whole line
    }
    off += line.length + 1; done++;
    return head + serialize(paint(runs, pa, pb, tok));
  });
  if (!done) return toast('Select some text, or put the cursor in a line with text.', 'err');
  const text = out.join('\n'); if (text !== v.slice(ls, le)) replaceRange(ta, ls, le, text);
  ta.setSelectionRange(inline ? ls + text.length : ls, ls + text.length);                    // keep whole lines selected for the next pen
  clearTimeout(etimer); saveText();
}
$$('#ctools button').forEach(b => b.addEventListener('mousedown', e => e.preventDefault()));  // keep the text selection while clicking
$('#applyPen').onclick = () => applyColour(tokenFor(active));
$$('[data-apply]').forEach(b => b.onclick = () => applyColour(b.dataset.apply));
$('#clearPen').onclick = () => applyColour(null);
$('#colourText').onclick = async () => { await openEditor(true); $('#editor').scrollIntoView({behavior: 'smooth', block: 'center'}); };

// ---- download the converted PDF ----
// The file is fetched first and checked (HTTP status, content type, "%PDF" header) so an error page can never be
// saved as a broken .pdf. It is then saved through a temporary link, which works on desktop and on mobile
// browsers. If that fails, the browser falls back to navigating to the attachment URL directly.
let pdfUrl = null, pdfName = 'document-handwritten.pdf', dlBusy = false;
function dlState(state, text) {
  const b = $('#dl'); b.classList.toggle('loading', state === 'loading'); b.classList.toggle('done', state === 'done');
  b.setAttribute('aria-busy', state === 'loading'); b.setAttribute('aria-disabled', state === 'loading'); $('#dlLabel').textContent = text;
}
async function downloadPdf() {
  if (dlBusy) return;                                   // ignore repeated clicks while a download is running
  if (!pdfUrl) return toast('Please convert your document first.', 'err');
  dlBusy = true; dlState('loading', 'Preparing PDF…');
  const ctl = new AbortController(), killer = setTimeout(() => ctl.abort(), 180000);
  let expired = false;
  try {
    const r = await fetch(pdfUrl, {cache: 'no-store', signal: ctl.signal});
    if (!r.ok) {                                        // error responses are JSON - never save them as a .pdf
      let msg = 'Download failed. Please convert the document again.';
      try { msg = (await r.json()).error || msg; } catch (_) {}
      expired = r.status === 404; throw new Error(msg);
    }
    if (!(r.headers.get('Content-Type') || '').toLowerCase().includes('application/pdf'))
      throw new Error('The server did not return a PDF. Please convert the document again.');
    const blob = await r.blob();
    const want = +r.headers.get('Content-Length') || 0;  // (skipped if a proxy re-compressed the body)
    if (!blob.size || (want && !r.headers.get('Content-Encoding') && blob.size !== want))
      throw new Error('The download was interrupted before it finished. Please try again.');
    const head = new TextDecoder().decode(await blob.slice(0, 5).arrayBuffer());
    const tail = new TextDecoder().decode(await blob.slice(Math.max(0, blob.size - 1024)).arrayBuffer());
    if (head !== '%PDF-' || !tail.includes('%%EOF')) throw new Error('The file received is not a complete PDF. Please convert again.');
    const file = new Blob([blob], {type: 'application/pdf'}), url = URL.createObjectURL(file);
    const a = document.createElement('a'); a.href = url; a.download = pdfName.replace(/(\.pdf)?$/i, '.pdf'); a.rel = 'noopener'; a.style.display = 'none';
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 120000);   // never revoke while the browser may still be saving
    dlState('done', 'Downloaded ✓'); toast('PDF downloaded – check your Downloads folder', 'ok');
  } catch (e) {
    if (e.name === 'AbortError') { toast('The download timed out. Please try again.', 'err'); dlState('', 'Download PDF'); }
    else if (e instanceof TypeError) {                  // network / blob problem: let the browser download straight from the server
      toast('Starting direct download…'); window.location.href = pdfUrl; dlState('done', 'Download started');
    } else {
      toast(e.message, 'err'); dlState('', 'Download PDF');
      if (expired) { pdfUrl = null; $('#result').hidden = true; }   // session/file gone: the user must convert again
    }
  } finally { clearTimeout(killer); }
  setTimeout(() => { if (pdfUrl) dlState('', 'Download PDF again'); dlBusy = false; }, 2500);
}
$('#dl').addEventListener('click', e => { e.preventDefault(); downloadPdf(); });

// ---- convert ----
$('#convertBtn').onclick = async () => {
  const b = $('#convertBtn'); b.disabled = true; b.textContent = 'Converting…'; $('#loading').classList.add('on'); $('#result').hidden = true;
  try {
    const j = await api(true); pages = j.pages; page = 1; legend(j.pens); show();
    if (!j.download) throw new Error('The PDF could not be created. Please try again.');
    pdfUrl = j.download;
    pdfName = j.filename || (($('#fname').textContent.replace(/\.docx$/i, '') || 'document') + '-handwritten.pdf');
    $('#resInfo').textContent = `${j.pages} page${j.pages > 1 ? 's' : ''} generated – your PDF is ready`;
    $('#dl').href = pdfUrl; dlBusy = false; dlState('', 'Download PDF'); $('#result').hidden = false; icons();
    $('#result').scrollIntoView({behavior: 'smooth', block: 'center'}); toast('Conversion successful! Your PDF is ready to download.', 'ok');
  } catch (e) {
    toast(e instanceof TypeError ? 'Network error. Check your connection and try again.' : (e.message || 'Conversion failed. Please try again.'), 'err');
    $('#loading').classList.remove('on'); if (e.status === 404) reset();
  }
  b.disabled = false; b.innerHTML = '<i data-lucide="wand-2"></i> Convert to Handwriting'; icons();
};
icons();
