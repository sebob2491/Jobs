"""JavaScript run inside each frame to describe the form a person is looking at.

Every control gets a stable `data-ja-id` attribute so later fill/click calls can
find it again. Radio buttons and same-named checkboxes are reported as one
group field whose members are tagged `<group id>.<index>`.
"""

EXTRACT_JS = r"""
(prefix) => {
  const W = window;
  W.__jaCounter = W.__jaCounter || 0;
  // Letters of this page load's own: an id read on one page (or tab) never names a box on
  // the next, where the count starts again. Letters only, so it can't read as a frame's "f2-".
  W.__jaDoc = W.__jaDoc || Array.from({ length: 3 }, () => String.fromCharCode(97 + Math.floor(Math.random() * 26))).join('');
  const newId = () => prefix + W.__jaDoc + (++W.__jaCounter);
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
  // Text before a control with no label of its own. Text before another control is that
  // control's label: an unlabelled phone extension box isn't "Phone Number *". A long text
  // is passed over, unless only a row's number comes before it: Phoenix Children's
  // qualifications are a table of rows "5. | <the requirement> | Yes / No", and a long
  // requirement's question was "5.".
  const CONTROL = 'input:not([type="hidden"]), select, textarea';
  const ROW_NUMBER = /^\(?\d{1,3}[.):]?$/;
  const preceding = (el) => {
    let node = el;
    for (let depth = 0; depth < 4 && node; depth++) {
      let sib = node.previousElementSibling, long = '';
      while (sib) {
        if (sib.matches(CONTROL) || sib.querySelector(CONTROL)) return '';
        const t = txt(sib);
        if (t && long && ROW_NUMBER.test(t)) return long;
        if (t && t.length < 300) return t;
        if (t && !long) long = t.slice(0, 297).replace(/\s+\S*$/, '') + '\u2026';
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
    // "First Name *", "* First Name", "Email * (work)"; not "* indicates a required field"
    if ((/\*/.test(label || '') && !/indicates?|denotes?|required fields?/i.test(label)) || /\(required\)/i.test(label || '')) return true;
    // Oracle: a required row's label says so only by a class (its star is drawn by CSS)
    const row = el.closest('.input-row');
    if (row && row.querySelector('.input-row__label--required')) return true;
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
      if (!t) {
        // the last heading before the field: "Work Experience 2" sits beside block 2, after block 1
        const hs = Array.from(node.querySelectorAll(HEADING)).filter((h) => h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING);
        t = hs.length ? txt(hs[hs.length - 1]) : '';
      }
      if (!t || t.length > 80) continue;
      if (container || /\b\d+\s*$/.test(t)) return t;
    }
    return '';
  };
  // Text a widget displays beside its input (react-select's chosen value). Only looks inside
  // the widget itself: stops at the first wrapper that holds other form controls.
  const shownNear = (el) => {
    for (let n = el.parentElement, d = 0; n && d < 3; n = n.parentElement, d++) {
      const others = Array.from(n.querySelectorAll('input, select, textarea, button')).filter((x) => x !== el && x.type !== 'hidden');
      if (others.length) break;
      const copy = n.cloneNode(true);
      copy.querySelectorAll('label, legend, input, [role="listbox"], [role="option"], [aria-live]').forEach((x) => x.remove());
      const t = clean(copy.textContent);
      if (t && t.length < 120) return t;
    }
    return '';
  };
  // Choices shown as pills (Eightfold's location picker shows "Singapore" with a Remove
  // button beside a read-only input): look in the smallest wrapper that holds no other
  // input, leaving out any open menu.
  const pillsNear = (el) => {
    for (let n = el.parentElement, d = 0; n && d < 6; n = n.parentElement, d++) {
      if (Array.from(n.querySelectorAll('input, select, textarea')).some((x) => x !== el && x.type !== 'hidden')) break;
      const pills = Array.from(n.querySelectorAll('[data-automation-id="selectedItem"], [class*="selected" i] [class*="label" i], [class*="pill" i] [class*="label" i]'))
        .filter((p) => !p.closest('[role="listbox"], [role="option"]')).map(txt).filter(Boolean);
      if (pills.length) return Array.from(new Set(pills));
    }
    return [];
  };
  const tag = (el, id) => { el.setAttribute('data-ja-id', id); return id; };
  // A site that adds a block by copying one (jQuery's clone) copies our ids too: the copy
  // gets new ones, or filling block 2 would write into block 1.
  const assigned = new Map();
  const usedIds = new Set();
  const idOf = (el) => {
    if (assigned.has(el)) return assigned.get(el);
    let id = el.getAttribute('data-ja-id');
    if (!id || usedIds.has(id)) id = tag(el, newId());
    usedIds.add(id);
    assigned.set(el, id);
    return id;
  };
  const gidOwners = new Map();
  const ownGid = (el, attr) => {
    let gid = el.getAttribute(attr);
    if (!gid || (gidOwners.has(gid) && gidOwners.get(gid) !== el)) { gid = newId(); el.setAttribute(attr, gid); }
    gidOwners.set(gid, el);
    return gid;
  };

  // "search", "job-search", "jobSearchForm"; not "research" ("toyotaresearchinstitute")
  const isSearchName = (s) => !!s && (/(^|[^a-zA-Z])search/i.test(s) || /[a-z]Search/.test(s));
  const GENERIC_FILE = /^(attach|upload|choose (a )?file|browse|select files?|add (a )?file|drop (your )?files? here|or|enter manually)$/i;
  const HONEYPOT = /for robots|robots only|if you('| a)?re (a )?human|not (be )?(filled|entered) by humans|honey ?pot|leave this field (blank|empty)/i;
  const fields = [];
  const passwordBoxes = [];
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
    const hiddenBy = el.closest('[aria-hidden="true"]');
    if (hiddenBy && type !== 'file') {
      // What an open dialog hides behind it isn't the form, but Paycom's Quick Apply wraps
      // its own fields in aria-hidden inside its dialog: those are the form.
      const dialog = el.closest('[aria-modal="true"], [role="dialog"]');
      if (!dialog || !dialog.contains(hiddenBy)) continue;
    }

    if (isChoice) {
      const isRadio = type === 'radio' || role === 'radio';
      let container = el.closest('fieldset, [role="radiogroup"], [role="group"]');
      if (!container && isRadio && !el.name) {
        // ARIA radios with no name and no group: one question's options share a wrapper
        for (let n = el.parentElement, d = 0; n && d < 3 && !container; n = n.parentElement, d++) {
          if (n.querySelectorAll('[role="radio"], input[type="radio"]').length > 1) container = n;
        }
      }
      const key = (isRadio ? 'r:' : 'c:') + (el.name ? 'n:' + el.name : container ? 'g:' + ownGid(container, 'data-ja-gid') : 'e:' + idOf(el));
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
    else if (role === 'combobox' || el.getAttribute('aria-autocomplete') === 'list'
             || el.getAttribute('data-uxi-widget-type') === 'selectinput') {  // Workday's search prompts (2026)
      kind = 'combobox';
      // its chosen items sit beside the input's own box, in the prompt's outer container
      const workday = el.closest('[data-automation-id="multiSelectContainer"]')
        || el.closest('[data-automation-id="multiselectInputContainer"]');
      const pills = workday ? Array.from(workday.querySelectorAll('[data-automation-id="selectedItem"], [class*="selected" i] [class*="label" i]')).map(txt).filter(Boolean)
        : pillsNear(el);
      if (pills.length) value = pills.join(', ');
      else if (workday) value = '';  // text left in its search box isn't a choice; nor is "0 items selected"
      else if (!el.value) value = shownNear(el);  // react-select shows the choice beside an empty input
    }
    // A site's own search box (header, nav, search form) is not part of the application.
    if (el.closest('[role="search"], header, nav') || type === 'search') continue;
    const form = el.closest('form');
    if (form && [form.getAttribute('action'), form.id, form.getAttribute('class')].some(isSearchName)) continue;
    let label = labelFor(el);
    // Upload widgets often label the input with its button ("Attach"); use the field's heading.
    if (kind === 'file' && GENERIC_FILE.test(label)) {
      for (let node = el.parentElement, d = 0; node && d < 5; node = node.parentElement, d++) {
        // the nearest heading *before* the input, so a big container doesn't hand back an earlier field's label
        const cands = Array.from(node.querySelectorAll('label, legend, [class*="label" i], h3, h4, [id$="-label"]'))
          .filter((c) => c.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)
          .map(txt).filter((t) => t && t.length < 80 && !GENERIC_FILE.test(t));
        if (cands.length) { label = cands[cands.length - 1]; break; }
      }
    }
    // Bot traps ("for robots only, do not enter if you're human") must never be filled.
    if (HONEYPOT.test(label) || HONEYPOT.test(el.name || '')) continue;
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
    // a Workday search prompt: what it lists on opening is only its top level
    if (kind === 'combobox' && (el.getAttribute('data-uxi-widget-type') === 'selectinput'
        || el.closest('[data-automation-id="multiSelectContainer"], [data-automation-id="multiselectInputContainer"]'))) f.search = true;
    // Oracle's lookups (ZIP, City): what they list on opening is a default page ("00000, …"),
    // not the place searched for
    if (kind === 'combobox' && el.getAttribute('aria-haspopup') === 'grid') f.search = true;
    // SuccessFactors' paginated select: lists 100 entries at a time, the rest by search
    if (kind === 'combobox' && el.classList.contains('rcmpaginatedselectinput')) f.paged = true;
    if (el.multiple) f.multiple = true;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') f.disabled = true;
    if (el.readOnly) f.readonly = true;
    if (el.getAttribute('aria-invalid') === 'true') f.invalid = true;
    if (el.maxLength > 0 && el.maxLength < 100000) f.max_length = el.maxLength;
    fields.push(f);
    if (kind === 'password') passwordBoxes.push(el);
  }

  for (const g of groups.values()) {
    const first = g.members[0];
    const checkedOf = (m) => m.checked === true || m.getAttribute('aria-checked') === 'true';
    if (!g.isRadio && g.members.length === 1) {
      let label = optionLabel(first);
      // "I have a preferred name" [x] Yes: the question is beside the box, not its label
      if (!label || /^(yes|no|y|true|i agree|agree|i accept|accept)$/i.test(label)) {
        let q = '';
        for (let n = first.parentElement, d = 0; n && d < 3 && !q; n = n.parentElement, d++) {
          q = labelledBy(n) || clean(n.getAttribute('aria-label') || '');
        }
        const legend = !q && first.closest('fieldset') && first.closest('fieldset').querySelector(':scope > legend');
        label = q || (legend && txt(legend)) || label;
      }
      const single = { id: idOf(first), kind: 'checkbox', label, required: isRequired(first, label), value: checkedOf(first) };
      const section = sectionOf(first);
      if (section && section !== label) single.section = section;
      fields.push(single);
      continue;
    }
    const gid = ownGid(first, 'data-ja-gid-member');
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
  // browser.FINALISH_RE and final_text: a form's own "Apply for this job", "Apply Now ›" sends it
  const FINALISH = /^(apply( now| online)?( for (this|the) (job|position|role|opening))?|apply to (this |the )?(job|position|role|opening)|send( now| (my )?application)?|finish|complete( (my )?application)?|confirm( and send)?)$/i;
  const finalText = (t) => t.replace(/\s+(arrow_forward|arrow_right_alt|chevron_right|navigate_next|east)$/i, '').replace(/[\s\u203a\u00bb\u2192>!.]+$/, '').trim();
  const POSTING_PAGE = /career(?:_|%5f)ns=job(?:_|%5f)listing(?:&|#|$)/i;  // browser.POSTING_PAGE_RE
  // A box of the site's own beside the application with a Submit of its own: the footer's
  // job alerts or newsletter sign-up. Its Submit is never the application's.
  const SIDE_BOX = /job alerts?|alerts? by e-?mail|e-?mail alerts?|newsletter|\bsubscribe\b|talent (?:community|network|pool)|notify me|similar (?:jobs|openings|roles)|stay (?:connected|in touch)/i;
  const boxesIn = (form) => [...form.elements].filter((e) => /^(INPUT|SELECT|TEXTAREA)$/.test(e.tagName)
    && !/^(hidden|submit|button|image|reset)$/i.test(e.type || '') && e.getClientRects().length > 0);
  const sideBox = (el) => {
    const boxes = el.form ? boxesIn(el.form) : null;
    // one or two boxes, one of them for typing (an email address), and words about alerts
    const small = boxes && boxes.length >= 1 && boxes.length <= 2
      && boxes.some((e) => e.tagName === 'INPUT' && /^(text|email|search|)$/i.test(e.getAttribute('type') || ''));
    if (small && SIDE_BOX.test(el.form.innerText || '')) return true;
    const foot = el.closest('footer, [role="contentinfo"]');
    return !!foot && !(boxes && boxes.length > 2) && SIDE_BOX.test(foot.innerText || '');
  };
  const ACTION = /apply|next|continue|review|submit|save|add|upload|sign ?in|log ?in|create (an |your |a new )?account|sign ?up|register|start|back|previous|edit|done|ok\b|accept|agree|use my last|autofill|manually|verify|confirm|remove|delete/i;
  // Up to 60 of the page's buttons. A dropdown's entries are choices in a field, not
  // things to do on the page: Eightfold draws them as buttons, and an open list of
  // referral sources or countries used to fill all 60 places before "Submit
  // application", so it was never seen. Submit and next-step buttons always make it in.
  const actions = [];
  const STEP = /^(next|continue|save and continue|review|submit)/i;
  const COOKIE_BOX = '[id*="cookie" i], [class*="cookie" i], [aria-label*="cookie" i], [id*="consent" i], '
    + '[class*="consent" i], [id*="onetrust" i], [class*="onetrust" i], [id*="cybot" i], [id*="gdpr" i], [id*="truste" i]';
  // Buttons drawn as web components (UKG's <ukg-button>, whose real button is in its shadow root) count too.
  const BUTTONS = 'button, [role="button"], input[type="submit"], input[type="button"], a[href], [data-tag-name="button"], '
    + 'ukg-button, sl-button, mwc-button, ion-button, vaadin-button, fluent-button, md-filled-button, md-outlined-button, '
    + 'md-text-button, md-filled-tonal-button, md-elevated-button';
  for (const el of document.querySelectorAll(BUTTONS)) {
    if (el.getAttribute('aria-haspopup') === 'listbox' || !visible(el)) continue;
    const role = el.getAttribute('role');
    const t = clean(txt(el) || el.value || el.getAttribute('aria-label') || '');
    // A menu's entries are mostly site navigation, left out, but an open menu of ways to
    // apply (Qorvo's "Apply now ▾": Apply Now, Start apply with LinkedIn) holds the way in.
    const inMenu = role === 'menuitem' || !!el.closest('[role="menu"]');
    const applyItem = inMenu && !el.closest('[role="menubar"]') && /\bapply\b/i.test(t);  // not the site's top bar
    if (role === 'option' || el.closest('[role="listbox"]') || (inMenu && !applyItem)) continue;
    if (!t || t.length > 60) continue;
    if (el.tagName === 'A' && !ACTION.test(t)) continue;
    const full = t + ' ' + (el.getAttribute('aria-label') || '');
    const formSubmit = el.type === 'submit' && !!el.form;
    // SuccessFactors' older sites show a posting inside a form whose submit is "Apply": with
    // nothing in the form to fill, it opens the application and sends nothing
    const opensApplication = formSubmit && POSTING_PAGE.test(location.href) && /^apply( now)?$/i.test(t)
      && ![...el.form.elements].some((e) => /^(INPUT|SELECT|TEXTAREA)$/.test(e.tagName)
        && !/^(hidden|submit|button|image|reset)$/i.test(e.type || '') && e.getClientRects().length > 0);
    const isSubmit = SUBMIT.test(full) || (formSubmit && FINALISH.test(finalText(t)) && !opensApplication);
    if (actions.length >= 60 && !isSubmit && !formSubmit && !STEP.test(t)) continue;
    const a = { id: idOf(el), text: t };
    if (applyItem) a.menu = true;
    // A cookie banner's own buttons (OneTrust's sits at the very end of a long page, past
    // the page text the desk reads)
    const box = el.closest(COOKIE_BOX);
    if (box && box !== document.body && box !== document.documentElement) a.cookie = true;
    if (formSubmit) a.form_submit = true;
    if (isSubmit) a.is_submit = true;
    // how many boxes the form around a plain button shows: a page's script can send a
    // name-and-email box from its "Apply" too
    const around = !formSubmit && (el.form || el.closest('form'));
    if (around && boxesIn(around).length) a.form_fields = boxesIn(around).length;
    if (isSubmit && sideBox(el)) a.aside = true;
    // A button after a password box: a sign-in form's own "Sign In", not the one in the
    // site's header (Workday's opens a sign-in pop-up, and sends nothing)
    if (passwordBoxes.some((p) => p.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) a.after_password = true;
    // A "Create Account" in a form with two password boxes is that form's own button, which
    // creates the account (Workday draws it as a div, not a form's submit): never the way to
    // the form. Boxes a pop-up hides count too.
    if (/account|sign ?up|register/i.test(t)) {
      for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
        const boxes = n.querySelectorAll('input[type="password"]').length;
        if (boxes) { if (boxes >= 2) a.account_form = true; break; }
      }
    }
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') a.disabled = true;
    // A link to something already on show on this page (Phoenix Children's "Apply!" to its form,
    // #apply) only scrolls there; one to something hidden may be what shows it
    const anchor = el.tagName === 'A' && /^#([A-Za-z][\w:-]*)$/.exec(el.getAttribute('href') || '');
    const target = anchor && document.getElementById(anchor[1]);
    if (target && target.getClientRects().length > 0) a.same_page = true;
    actions.push(a);
  }

  const errors = [];
  for (const el of document.querySelectorAll('[role="alert"], [aria-live="assertive"], [data-automation-id="errorMessage"], [class*="error" i]:not(input):not(select):not(textarea)')) {
    // without icon-font glyphs (Amkor's "\ue0b1 Invalid email address or password"; an alert
    // that's only an icon says nothing)
    const t = txt(el).replace(/[\uE000-\uF8FF]/g, '').trim();
    if (!/[\p{L}\p{N}]/u.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) continue;  // screen-reader announcements ("… page is loaded")
    if (/\bpage is loaded\b|^loading\b/i.test(t)) continue;
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

# Facts click() needs about an element before deciding whether it may press it.
ELEMENT_INFO_JS = r"""
(el) => {
  const BOX = '[id*="cookie" i], [class*="cookie" i], [aria-label*="cookie" i], [id*="consent" i], '
    + '[class*="consent" i], [id*="onetrust" i], [class*="onetrust" i], [id*="cybot" i], [id*="gdpr" i], [id*="truste" i]';
  const names = (n) => `${n.id || ''} ${typeof n.className === 'string' ? n.className : ''} ${n.getAttribute('aria-label') || ''}`;
  const named = (n) => /cookie|onetrust|cybot|truste|gdpr/i.test(names(n));
  const words = (n) => (n.innerText || n.textContent || n.value || n.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
  const page = (n) => n === document.body || n === document.documentElement;
  // the whole banner: the outermost cookie/consent box around it (OneTrust's accept button is
  // a box of its own by its id, inside the banner holding its "Reject All")
  const near = el.closest(BOX);
  let banner = null;
  for (let n = near; n && !page(n); n = n.parentElement && n.parentElement.closest(BOX)) banner = n;
  // a "consent" box only when it speaks of cookies, not an application's own consent step
  const cookie = !!banner && (named(near) || /cookie/i.test(near.innerText || '') || named(banner));
  const shown = (n) => n.getClientRects().length > 0 && getComputedStyle(n).visibility !== 'hidden';
  const top = cookie ? document.elementFromPoint(innerWidth / 2, innerHeight / 2) : null;
  const over = top && top.closest(BOX);
  return {
    label: [el.innerText || el.textContent || el.value || '', el.getAttribute('aria-label') || ''].join(' ').replace(/\s+/g, ' ').trim(),
    // its words as the page script reads them: an icon-only button's are its aria-label
    text: words(el),
    formSubmit: el.type === 'submit' && !!el.form,
    // in a cookie banner (OneTrust, Cookiebot, TrustArc and the like)
    cookie,
    // the banner's buttons (is there a way to decline?), and whether it covers the page's middle
    cookieButtons: cookie ? [...banner.querySelectorAll('button, [role="button"], input[type="submit"], input[type="button"], a[href]')]
      .filter(shown).map(words).filter((t) => t && t.length <= 60).slice(0, 20) : [],
    cookieBlocking: !!top && (banner.contains(top) || (!!over && !page(over) && named(over))),
    // what its form has to fill in, where it can be seen
    formFields: el.form ? [...el.form.elements].filter((e) => /^(INPUT|SELECT|TEXTAREA)$/.test(e.tagName)
      && !/^(hidden|submit|button|image|reset)$/i.test(e.type || '') && e.getClientRects().length > 0).length : 0,
  };
}
"""

# Is something else drawn on top of this element's centre?
COVERED_JS = r"""
(el) => {
  el.scrollIntoView({ block: 'center', inline: 'nearest' });
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) return true;
  const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  return !(top && (top === el || el.contains(top)));
}
"""

# Text boxes the page marks invalid while they show a value: shown, editable, not a menu's
# box, and not the one the person is typing in.
LOST_BOXES_JS = r"""
() => {
  const out = [];
  for (const el of document.querySelectorAll('input[data-ja-id], textarea[data-ja-id]')) {
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (!['text', 'email', 'tel', 'url', 'number', 'search'].includes(type) && el.tagName !== 'TEXTAREA') continue;
    if (el.getAttribute('aria-invalid') !== 'true' || el.readOnly || el.disabled) continue;
    if (el.getAttribute('role') === 'combobox' || el.getAttribute('role') === 'spinbutton') continue;
    if (el === document.activeElement || !el.value.trim()) continue;
    if (!el.getClientRects().length || getComputedStyle(el).visibility === 'hidden') continue;
    out.push({ id: el.getAttribute('data-ja-id'), value: el.value });
  }
  return out;
}
"""

# Before opening a dropdown: remember which options are already showing (other menus
# some sites leave open), so they're never mistaken for this field's choices.
MARK_OPTIONS_JS = r"""
() => {
  const visible = (o) => o.getClientRects().length > 0 && getComputedStyle(o).visibility !== 'hidden';
  for (const o of document.querySelectorAll('[role="option"]')) {
    o.removeAttribute('data-ja-before');
    if (visible(o)) o.setAttribute('data-ja-before', (o.innerText || o.textContent || '').replace(/\s+/g, ' ').trim());
  }
}
"""

# The options of *this* field's menu: its aria-controls/aria-owns listbox, else options that
# appeared or changed since MARK_OPTIONS_JS, else (when the field says its menu is open) the
# nearest visible menu. Each is tagged data-ja-opt=<text> so the click hits the right one.
FIELD_OPTIONS_JS = r"""
(el) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const text = (o) => clean(o.innerText || o.textContent);
  const visible = (o) => o.getClientRects().length > 0 && getComputedStyle(o).visibility !== 'hidden';
  const all = Array.from(document.querySelectorAll('[role="option"]')).filter(visible);
  let opts = [];
  for (let n = el, d = 0; n && n.getAttribute && d < 4 && !opts.length; n = n.parentElement, d++) {
    const ref = n.getAttribute('aria-controls') || n.getAttribute('aria-owns');
    const box = ref ? document.getElementById(ref.split(/\s+/)[0]) : null;
    if (box) opts = Array.from(box.querySelectorAll('[role="option"]')).filter(visible);
    // Oracle's lookups (ZIP, City, Address Line 1) list their entries as a grid's cells
    if (box && !opts.length) opts = Array.from(box.querySelectorAll('[role="gridcell"]')).filter(visible);
  }
  if (!opts.length && all.length) {
    // No ARIA link: a field's menu is the one attached to it, opening just below (or above)
    // it and overlapping it horizontally. Menus some sites leave open for earlier fields sit
    // further up the page; options that were already showing before opening rank last.
    const outer = (f) => f.closest('[role="combobox"]') || f;
    const box = (el.closest('[role="combobox"]') || el.parentElement || el).getBoundingClientRect();
    const overlaps = (b, r) => Math.min(b.right, r.right) - Math.max(b.left, r.left) > 0;
    const gapTo = (b, r) => Math.min(Math.abs(b.top - r.bottom), Math.abs(r.top - b.bottom));
    // the other dropdowns on the page: a menu that sits nearer one of them is that one's
    // (Micron's previous question kept its menu open over this field and drew it afresh)
    const others = Array.from(document.querySelectorAll('[role="combobox"], [aria-haspopup="listbox"], input[aria-autocomplete]'))
      .map(outer).filter((f) => f !== outer(el) && !f.contains(el) && !el.contains(f) && f.getClientRects().length > 0)
      .map((f) => f.getBoundingClientRect());
    let mine = outer(el).getBoundingClientRect();  // measured like the others, not by its wrapper
    if (!mine.width && !mine.height) mine = box;
    const groups = new Map();
    for (const o of all) {
      const c = o.closest('[role="listbox"]') || o.parentElement;
      groups.set(c, (groups.get(c) || []).concat([o]));
    }
    let best = [], bestGap = Infinity;
    for (const [c, os] of groups) {
      // a menu that was already showing before this field was opened is never its menu
      if (os.every((o) => o.getAttribute('data-ja-before') === text(o))) continue;
      const b = c.getBoundingClientRect();
      if (!overlaps(b, box)) continue;
      const gap = gapTo(b, box);
      if (others.some((r) => overlaps(b, r) && gapTo(b, r) < gapTo(b, mine))) continue;
      if (gap < 120 && gap < bestGap) { bestGap = gap; best = os; }
    }
    opts = best;
  }
  for (const o of document.querySelectorAll('[data-ja-opt]')) o.removeAttribute('data-ja-opt');
  const out = [];
  for (const o of opts) {
    const t = text(o);
    if (t && !out.includes(t)) { o.setAttribute('data-ja-opt', t); out.push(t); }
  }
  return out;
}
"""

# Is a CAPTCHA challenge (hCaptcha's or reCAPTCHA's pictures, Cloudflare's check) showing
# in this frame? Their frames sit hidden in many pages until they're needed, and the
# checkbox ones are small: only a big, visible challenge frame counts.
CHALLENGE_JS = r"""
() => Array.from(document.querySelectorAll('iframe')).some((f) => {
  const src = f.getAttribute('src') || '';
  const title = f.getAttribute('title') || '';
  if (!/hcaptcha\.com.*challenge|recaptcha\/(api2|enterprise)\/bframe|challenges\.cloudflare\.com/i.test(src)
      && !/(hcaptcha|recaptcha|captcha).*challenge|challenge.*(hcaptcha|recaptcha|captcha)/i.test(title)) return false;
  const r = f.getBoundingClientRect();
  const s = getComputedStyle(f);
  return r.width > 150 && r.height > 150 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth
    && s.visibility !== 'hidden' && s.display !== 'none' && Number(s.opacity || 1) > 0.1;
})
"""


# Is this input one of Workday's search prompts? Those search when Enter is pressed.
WORKDAY_PROMPT_JS = r"""
(el) => el.getAttribute('data-uxi-widget-type') === 'selectinput'
  || !!el.closest('[data-automation-id="multiSelectContainer"], [data-automation-id="multiselectInputContainer"]')
"""


# What a Workday search prompt has chosen (its pills), or null for any other field.
WORKDAY_CHOSEN_JS = r"""
(el) => {
  const box = el.closest('[data-automation-id="multiSelectContainer"]')
    || el.closest('[data-automation-id="multiselectInputContainer"]');
  if (!box) return null;
  return Array.from(box.querySelectorAll('[data-automation-id="selectedItem"]'))
    .map((p) => (p.innerText || p.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
}
"""


# What a dropdown field currently displays: input value, button text, or selected chips.
SHOWN_VALUE_JS = r"""
(el) => {
  // react-select clears its input and shows the choice in a sibling, Workday shows chips:
  // walk up to the nearest wrapper that displays something, ignoring labels and open menus.
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const parts = [];
  if (el.value) parts.push(el.value);
  if (el.tagName !== 'INPUT') parts.push(el.innerText || el.textContent || '');
  for (let n = el.parentElement, d = 0; n && d < 4; n = n.parentElement, d++) {
    const others = Array.from(n.querySelectorAll('input, select, textarea, button')).filter((x) => x !== el && x.type !== 'hidden');
    if (others.length) break;  // that's the surrounding form, not the widget
    const copy = n.cloneNode(true);
    copy.querySelectorAll('label, legend, input, [role="listbox"], [role="option"], [aria-live]').forEach((x) => x.remove());
    const t = clean(copy.textContent);
    if (t && t.length < 120) { parts.push(t); break; }
  }
  return clean(parts.join(' '));
}
"""


# Resolves once nothing has been added to or removed from the page for `quiet` ms
# (or after `most` ms): single-page apps draw the next step after the network is idle.
QUIET_JS = r"""
([quiet, most]) => new Promise((resolve) => {
  let timer;
  const done = () => { observer.disconnect(); clearTimeout(timer); clearTimeout(cap); resolve(true); };
  const observer = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(done, quiet); });
  const cap = setTimeout(done, most);
  observer.observe(document, { childList: true, subtree: true });
  timer = setTimeout(done, quiet);
})
"""


# A click on the page itself, on no control: closes menus that ignore Escape and focus
# moving away (Eightfold's), the way clicking beside a menu does.
OUTSIDE_CLICK_JS = r"""
() => { for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'])
  document.body.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window })); }
"""


# Is a dropdown menu open (one with options showing)?
OPEN_MENU_JS = r"""
() => [...document.querySelectorAll('[role="listbox"], [role="menu"], [role="grid"]')].some((m) =>
  m.tagName !== 'SELECT' && m.getClientRects().length > 0 && getComputedStyle(m).visibility !== 'hidden'
  && (m.getAttribute('role') === 'grid'
    // a grid is a menu only as a field's list (Oracle's lookups), not as a table on the page
    ? !!(m.id && document.querySelector(`[aria-controls~="${CSS.escape(m.id)}"]`)) && !!m.querySelector('[role="gridcell"]')
    : !!m.querySelector('[role="option"], [role="menuitem"]')))
"""
