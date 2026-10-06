"""JavaScript run inside each frame to describe the form a person is looking at.

Every control gets a stable `data-ja-id` attribute so later fill/click calls can
find it again. Radio buttons and same-named checkboxes are reported as one
group field whose members are tagged `<group id>.<index>`.
"""

EXTRACT_JS = r"""
(prefix) => {
  const W = window;
  W.__jaCounter = W.__jaCounter || 0;
  const newId = () => prefix + (++W.__jaCounter);
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const txt = (el) => clean(el ? (el.innerText || el.textContent || '') : '');
  const byId = (id) => (id ? document.getElementById(id) : null);
  const visible = (el) => {
    if (!el || !el.getClientRects().length) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const labelEl = (el) => {
    if (el.labels && el.labels.length) return el.labels[0];
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) return l;
    }
    return el.closest('label');
  };
  const preceding = (el) => {
    let node = el;
    for (let depth = 0; depth < 4 && node; depth++) {
      let sib = node.previousElementSibling;
      while (sib) {
        const t = txt(sib);
        if (t && t.length < 300) return t;
        sib = sib.previousElementSibling;
      }
      node = node.parentElement;
    }
    return '';
  };
  const labelledBy = (el) => {
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter((i) => i && i !== el.id);
    return clean(ids.map((i) => txt(byId(i))).join(' '));
  };
  const labelFor = (el) => {
    const ff = el.closest('[data-automation-id^="formField"]');
    if (ff) {
      const l = ff.querySelector('label, legend');
      if (l && txt(l)) return txt(l);
    }
    const l = labelEl(el);
    if (l && txt(l)) return txt(l);
    const lb = labelledBy(el);
    if (lb) return lb;
    const al = el.getAttribute('aria-label');
    if (al) return clean(al);
    if (el.placeholder) return clean(el.placeholder);
    if (el.title) return clean(el.title);
    return preceding(el) || el.name || '';
  };
  const groupQuestion = (container, first) => {
    if (container) {
      const ff = container.closest('[data-automation-id^="formField"]');
      if (ff) {
        const l = ff.querySelector('label, legend');
        if (l && txt(l) && !l.contains(first)) return txt(l);
      }
      const legend = container.querySelector(':scope > legend');
      if (legend && txt(legend)) return txt(legend);
      const lb = labelledBy(container);
      if (lb) return lb;
      const al = container.getAttribute('aria-label');
      if (al) return clean(al);
      return preceding(container);
    }
    return preceding(first);
  };
  const isRequired = (el, label) => {
    if (el.required || el.getAttribute('aria-required') === 'true') return true;
    if (/\*\s*$|\(required\)/i.test(label || '')) return true;
    const ff = el.closest('[data-automation-id^="formField"]');
    return !!(ff && ff.querySelector('abbr[title*="equired"], [class*="required" i]'));
  };
  const optionLabel = (el) => {
    const l = labelEl(el);
    if (l && txt(l)) return txt(l);
    return clean(el.getAttribute('aria-label') || labelledBy(el) || el.value || txt(el));
  };
  // The repeated block a field sits in, e.g. "Work Experience 2" or "Education 1".
  const HEADING = ':scope > h2, :scope > h3, :scope > h4, :scope > h5, :scope > legend, :scope > [role="heading"], :scope > div:first-child > h3, :scope > div:first-child > h4';
  const sectionOf = (el) => {
    let node = el.parentElement;
    for (let d = 0; node && d < 12; d++, node = node.parentElement) {
      const role = (node.getAttribute('role') || '').toLowerCase();
      const container = role === 'group' || role === 'region' || node.tagName === 'FIELDSET' || node.tagName === 'SECTION';
      let t = container ? labelledBy(node) : '';
      if (!t) { const h = node.querySelector(HEADING); t = h ? txt(h) : ''; }
      if (!t || t.length > 80) continue;
      if (container || /\b\d+\s*$/.test(t)) return t;
    }
    return '';
  };
  const tag = (el, id) => { el.setAttribute('data-ja-id', id); return id; };
  const idOf = (el) => el.getAttribute('data-ja-id') || tag(el, newId());

  const fields = [];
  const seen = new Set();
  const groups = new Map();
  const sel = 'input, textarea, select, button[aria-haspopup="listbox"], [role="combobox"], [role="radio"], [role="checkbox"], [role="switch"]';
  for (const el of document.querySelectorAll(sel)) {
    if (seen.has(el)) continue;
    seen.add(el);
    const tagName = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (tagName === 'input' && ['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
    // ARIA 1.1 combobox wrappers contain the real input; skip the wrapper.
    if (role === 'combobox' && tagName !== 'input' && el.querySelector('input')) continue;
    const isChoice = type === 'radio' || type === 'checkbox' || role === 'radio' || role === 'checkbox' || role === 'switch';
    const shown = visible(el) || (isChoice && visible(labelEl(el))) || type === 'file';
    if (!shown) continue;
    if (el.closest('[aria-hidden="true"]') && type !== 'file') continue;

    if (isChoice) {
      const isRadio = type === 'radio' || role === 'radio';
      const container = el.closest('fieldset, [role="radiogroup"], [role="group"]');
      const key = (isRadio ? 'r:' : 'c:') + (el.name ? 'n:' + el.name : container ? 'g:' + (container.getAttribute('data-ja-gid') || (container.setAttribute('data-ja-gid', newId()), container.getAttribute('data-ja-gid'))) : 'e:' + idOf(el));
      if (!groups.has(key)) groups.set(key, { isRadio, container, members: [] });
      groups.get(key).members.push(el);
      continue;
    }

    let kind = 'text';
    let options = null;
    let value = el.value;
    if (tagName === 'textarea') kind = 'textarea';
    else if (tagName === 'select') {
      kind = 'select';
      options = Array.from(el.options).map((o) => clean(o.text)).filter(Boolean);
      value = Array.from(el.selectedOptions).filter((o) => o.value !== '').map((o) => clean(o.text)).join(', ');
    } else if (type === 'file') { kind = 'file'; value = Array.from(el.files || []).map((f) => f.name).join(', '); }
    else if (type === 'password') { kind = 'password'; value = el.value ? '(set)' : ''; }
    else if (tagName === 'button' || (role === 'combobox' && tagName !== 'input')) { kind = 'listbox'; value = txt(el); }
    else if (role === 'combobox' || el.getAttribute('aria-autocomplete') === 'list') {
      kind = 'combobox';
      const container = el.closest('[data-automation-id="multiselectInputContainer"]') || el.parentElement;
      const pills = container ? Array.from(container.querySelectorAll('[data-automation-id="selectedItem"], [class*="selected" i] [class*="label" i]')).map(txt).filter(Boolean) : [];
      if (pills.length) value = pills.join(', ');
    }
    const label = labelFor(el);
    const f = {
      id: idOf(el), kind, label, required: isRequired(el, label), value: kind === 'password' ? value : clean(String(value || '')),
    };
    if (tagName === 'input' && type && type !== 'text') f.input_type = type;
    const section = sectionOf(el);
    if (section && section !== label) f.section = section;
    if (kind === 'text') {
      const dai = el.getAttribute('data-automation-id') || '';
      const al = clean(el.getAttribute('aria-label') || '');
      let sub = '';
      if (/month/i.test(dai)) sub = 'Month';
      else if (/year/i.test(dai)) sub = 'Year';
      else if (/day/i.test(dai) && /date/i.test(dai)) sub = 'Day';
      else if (al && al !== label && al.length < 40 && !label.includes(al)) sub = al;
      if (sub) f.sublabel = sub;
      if (role === 'spinbutton') f.role = 'spinbutton';
    }
    if (options) f.options = options;
    if (el.multiple) f.multiple = true;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') f.disabled = true;
    if (el.readOnly) f.readonly = true;
    if (el.getAttribute('aria-invalid') === 'true') f.invalid = true;
    if (el.maxLength > 0 && el.maxLength < 100000) f.max_length = el.maxLength;
    fields.push(f);
  }

  for (const g of groups.values()) {
    const first = g.members[0];
    const checkedOf = (m) => m.checked === true || m.getAttribute('aria-checked') === 'true';
    if (!g.isRadio && g.members.length === 1) {
      const label = optionLabel(first);
      const single = { id: idOf(first), kind: 'checkbox', label, required: isRequired(first, label), value: checkedOf(first) };
      const section = sectionOf(first);
      if (section && section !== label) single.section = section;
      fields.push(single);
      continue;
    }
    const gid = first.getAttribute('data-ja-gid-member') || newId();
    const options = [];
    let value = g.isRadio ? '' : [];
    g.members.forEach((m, i) => {
      m.setAttribute('data-ja-id', gid + '.' + i);
      m.setAttribute('data-ja-gid-member', gid);
      const ol = optionLabel(m);
      options.push(ol);
      if (checkedOf(m)) { if (g.isRadio) value = ol; else value.push(ol); }
    });
    const label = groupQuestion(g.container, first);
    const group = {
      id: gid, kind: g.isRadio ? 'radio_group' : 'checkbox_group', label, options, value,
      required: g.members.some((m) => isRequired(m, '')) || isRequired(g.container || first, label),
    };
    const section = sectionOf(g.container || first);
    if (section && section !== label) group.section = section;
    fields.push(group);
  }

  const SUBMIT = /\bsubmit\b|send (my )?application|finish (my )?application|complete (my )?application/i;
  const ACTION = /apply|next|continue|review|submit|save|add|upload|sign ?in|log ?in|create account|start|back|previous|edit|done|ok\b|accept|agree|use my last|autofill|manually|verify|confirm|remove|delete/i;
  const actions = [];
  for (const el of document.querySelectorAll('button, [role="button"], input[type="submit"], input[type="button"], a[href]')) {
    if (actions.length >= 60) break;
    if (el.getAttribute('aria-haspopup') === 'listbox' || !visible(el)) continue;
    const t = clean(txt(el) || el.value || el.getAttribute('aria-label') || '');
    if (!t || t.length > 60) continue;
    if (el.tagName === 'A' && !ACTION.test(t)) continue;
    const full = t + ' ' + (el.getAttribute('aria-label') || '');
    const a = { id: idOf(el), text: t };
    if (SUBMIT.test(full)) a.is_submit = true;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') a.disabled = true;
    actions.push(a);
  }

  const errors = [];
  for (const el of document.querySelectorAll('[role="alert"], [aria-live="assertive"], [data-automation-id="errorMessage"], [class*="error" i]:not(input):not(select):not(textarea)')) {
    const t = txt(el);
    if (t && t.length < 300 && visible(el) && !errors.includes(t)) errors.push(t);
    if (errors.length >= 10) break;
  }
  const headings = [];
  for (const el of document.querySelectorAll('h1, h2, h3, [data-automation-id="progressBarActiveStep"], [role="heading"]')) {
    const t = txt(el);
    if (t && t.length < 120 && visible(el) && !headings.includes(t)) headings.push(t);
    if (headings.length >= 8) break;
  }
  return { fields, actions, errors, headings };
}
"""

# Options of an open listbox/autocomplete popup.
OPTIONS_JS = r"""
() => {
  const out = [];
  for (const el of document.querySelectorAll('[role="option"]')) {
    if (!el.getClientRects().length) continue;
    const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}
"""

# Fallback for custom-styled radios/checkboxes whose input is display:none.
CLICK_CHOICE_JS = r"""
(el) => {
  const l = (el.labels && el.labels[0]) || el.closest('label');
  (l || el).click();
  return el.checked === true || el.getAttribute('aria-checked') === 'true';
}
"""

VISIBLE_TEXT_JS = r"""
() => (document.body ? document.body.innerText : '').replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim()
"""

# Numbered entry headings ("Work Experience 2") and the Add buttons that create more.
ENTRIES_JS = r"""
(kindPattern) => {
  const kind = new RegExp(kindPattern, 'i');
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const txt = (el) => clean(el ? (el.innerText || el.textContent || '') : '');
  const visible = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const headings = Array.from(document.querySelectorAll('h1, h2, h3, h4, h5, h6, legend, [role="heading"]'))
    .filter(visible).map(txt);
  const entries = headings.filter((t) => kind.test(t) && /\b\d+\s*$/.test(t)).length;
  const buttons = [];
  for (const b of document.querySelectorAll('button, [role="button"]')) {
    if (!visible(b)) continue;
    const label = clean(txt(b) + ' ' + (b.getAttribute('aria-label') || ''));
    if (!/^add\b/i.test(txt(b)) && !/^add\b/i.test(b.getAttribute('aria-label') || '')) continue;
    // What is this button adding? Its own label, else the nearest heading above it.
    let context = label;
    if (!kind.test(context)) {
      for (let node = b.parentElement, d = 0; node && d < 6; node = node.parentElement, d++) {
        const h = node.querySelector('h1, h2, h3, h4, h5, legend, [role="heading"]');
        if (h) { context += ' ' + txt(h); break; }  // nearest section only
      }
    }
    if (kind.test(context)) {
      if (!b.getAttribute('data-ja-id')) {
        window.__jaCounter = (window.__jaCounter || 0) + 1;
        b.setAttribute('data-ja-id', 'add' + window.__jaCounter);
      }
      buttons.push({ id: b.getAttribute('data-ja-id'), text: txt(b) });
    }
  }
  return { entries, buttons };
}
"""
