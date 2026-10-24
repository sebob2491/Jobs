"""JavaScript run inside each frame to describe the form a person is looking at.

Every control gets a stable `data-ja-id` attribute so later fill/click calls can
find it again. Radio buttons and same-named checkboxes are reported as one
group field whose members are tagged `<group id>.<index>`.
"""

# A button in a sign-in form and nothing more: the nearest box around it that holds a shown password
# box holds just that one, at most one box to type in (the email or user name) and check boxes
# ("Remember me"), and the button comes after the password. What it sends is the sign-in, whatever
# it reads: SuccessFactors' sign-in button is "Submit". Put into EXTRACT_JS and ELEMENT_INFO_JS.
SIGN_IN_FORM_JS = r"""
  // an element's form, where it's a real one: a web component's own "form" property can be anything (UKG
  // Pro's "Create account" button's, live, Oct 2026: no elements to go through, and the click threw)
  const formOf = (el) => (el.form instanceof HTMLFormElement ? el.form : null);
  const signInForm = (el) => {
    const shown = (e) => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
    const passwords = (n) => [...n.querySelectorAll('input[type="password"]')].filter(shown);
    let box = null;
    for (let n = el.parentElement; n && n !== document.documentElement; n = n.parentElement) {
      if (passwords(n).length) { box = n; break; }
    }
    if (!box || passwords(box).length !== 1) return false;
    if (!(passwords(box)[0].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) return false;
    const boxes = [...box.querySelectorAll('input, select, textarea, [role="combobox"], [role="listbox"], '
      + '[role="radio"], [role="textbox"], [contenteditable="true"]')].filter((e) => shown(e)
      && !(e.tagName === 'INPUT' && /^(hidden|submit|button|image|reset|password|checkbox)$/i.test(e.type || '')));
    return boxes.length <= 1 && boxes.every((e) => e.tagName === 'INPUT' && /^(text|email)$/i.test(e.type || 'text')
      && !/combobox|listbox/i.test(e.getAttribute('role') || ''));
  };
"""

# Dialogs open over the page (Eightfold's notice about its AI screening, opened as the resume
# goes up): shown, drawn on top, and modal (aria-modal, an alert, or over the window's middle).
# Not one off the window (a side panel slid away), nor one holding boxes of its own (the form
# itself: Paycom's Quick Apply, a sign-in pop-up), nor a box's own pop-up (a date picker's
# calendar, a country list, what a box's aria-controls opens), nor one with nothing to press (a
# loading overlay). In a frame, only one over boxes there (not a chat widget's frame). Each with
# its heading, its words, and whether it's a cookie banner (by its names, or words about cookies
# up front). Put into EXTRACT_JS and NOTICE_OVER_JS.
OPEN_DIALOGS_JS = r"""
  const openDialogs = () => {
    const words = (n) => (n ? (n.innerText || n.textContent || '') : '').replace(/\s+/g, ' ').trim();
    const drawn = (n) => {
      for (; n && n.nodeType === 1; n = n.parentElement) {
        const s = getComputedStyle(n);
        if (s.visibility === 'hidden' || Number(s.opacity) < 0.1) return false;
      }
      return true;
    };
    const BOX = 'input:not([type="hidden"]):not([type="button"]):not([type="submit"]):not([type="reset"]):not([type="image"]), '
      + 'select, textarea, [role="combobox"], [role="textbox"], [role="radio"], [role="checkbox"], [role="switch"]';
    const PRESS = 'button, [role="button"], a[href], input[type="submit"], input[type="button"]';
    const COOKIE_NAMES = /cookie|onetrust|cybot|truste|gdpr/i;
    const named = (n) => COOKIE_NAMES.test(`${n.id || ''} ${typeof n.className === 'string' ? n.className : ''} ${n.getAttribute('aria-label') || ''}`);
    const out = [];
    const boxes = [...document.querySelectorAll(BOX)].filter((b) => b.getClientRects().length > 0);
    for (const d of document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog[open]')) {
      if (out.some((o) => o.el.contains(d)) || !d.getClientRects().length || !drawn(d)) continue;
      const r = d.getBoundingClientRect();
      const left = Math.max(r.left, 0), right = Math.min(r.right, innerWidth);
      const top = Math.max(r.top, 0), bottom = Math.min(r.bottom, innerHeight);
      if (right - left < 40 || bottom - top < 40) continue;
      const hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2);
      if (!hit || !d.contains(hit)) continue;  // under something else
      const middle = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
      let modal = d.getAttribute('aria-modal') === 'true' || d.getAttribute('role') === 'alertdialog' || (!!middle && d.contains(middle));
      try { modal = modal || d.matches(':modal'); } catch (e) { /* a browser without :modal */ }
      if (!modal || boxes.some((b) => d.contains(b)) || (window !== window.top && !boxes.length)) continue;
      if (d.querySelector('[role="grid"], [role="listbox"]') || (d.id && document.querySelector(
        `[aria-controls~="${CSS.escape(d.id)}"], [aria-owns~="${CSS.escape(d.id)}"]`))) continue;
      if (![...d.querySelectorAll(PRESS)].some((b) => b.getClientRects().length > 0)) continue;  // a loading overlay
      const labelled = (d.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
        .map((i) => words(document.getElementById(i))).join(' ').trim();
      const heading = (labelled || (d.getAttribute('aria-label') || '').trim()
        || words(d.querySelector('h1, h2, h3, h4, h5, h6, [role="heading"], legend'))).slice(0, 200);
      const text = words(d).slice(0, 3000);
      const cookie = named(d) || !!(d.parentElement && d.parentElement.closest('[id*="cookie" i], [class*="cookie" i], '
        + '[id*="onetrust" i], [id*="cybot" i], [id*="truste" i], [id*="gdpr" i]')) || /cookie/i.test(`${heading} ${text.slice(0, 300)}`);
      out.push({ el: d, heading, text, cookie });
    }
    return out;
  };
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
  // what a jQuery UI multiselect has ticked: its hidden select's choices, and those its widget lists under its button
  // ("<id>-button_selection-list"), each read as the choice it names (a widget may keep only its own list: the review)
  const multiTicked = (button, select) => {
    const names = Array.from(select.options).map((o) => clean(o.text)).filter(Boolean);
    const ticked = Array.from(select.selectedOptions).map((o) => clean(o.text)).filter(Boolean);
    const list = document.getElementById(button.id + '_selection-list');
    for (const li of list ? list.querySelectorAll('li') : []) {
      const said = clean(li.textContent);
      const named = names.filter((n) => said === n || said.includes(n)).sort((a, b) => b.length - a.length)[0];
      if (named && !ticked.includes(named)) ticked.push(named);
    }
    return ticked;
  };
  // BrassRing's search box for "check each applicable box" (Edward Jones' second race question, run 88): an input that
  // names the hidden select it stands for in its "select" attribute. What's ticked shows as pills in the ".selectionList"
  // after its box, each with a Remove button, and may never reach the select
  const standsFor = (el) => {
    const s = el.tagName === 'INPUT' && el.getAttribute('select') ? document.getElementById(el.getAttribute('select')) : null;
    return s && s.tagName === 'SELECT' ? s : null;
  };
  const searchTicked = (el, select) => {
    const ticked = Array.from(select.selectedOptions).filter((o) => o.value !== '').map((o) => clean(o.text)).filter(Boolean);
    let list = el.id ? document.getElementById(el.id + '_selection-list') : null;
    for (let s = el.parentElement && el.parentElement.nextElementSibling; s && !list; s = s.nextElementSibling) {
      if (s.classList.contains('selectionList')) list = s;
    }
    for (const li of list ? list.querySelectorAll('li') : []) {
      const copy = li.cloneNode(true);
      copy.querySelectorAll('button, .screenReaderText').forEach((x) => x.remove());
      const said = clean(copy.textContent);
      if (said && !ticked.includes(said)) ticked.push(said);
    }
    return ticked;
  };
  // Oracle's cx-multi-select (Recruiting Cloud's "select all that apply" question, MSTS' run 144): an input whose ticked
  // choices show as pills under its box, beside its container; the input holds only words typed to search. null for
  // any other box
  const cxTicked = (el) => {
    const box = el.tagName === 'INPUT' ? el.closest('.cx-multi-select-container') : null;
    if (!box || !box.parentElement) return null;
    return Array.from(box.parentElement.querySelectorAll(':scope > .cx-multi-select-pill__values '
      + '.cx-multi-select-pill__value-text')).map(txt).filter(Boolean);
  };
  const byId = (id) => (id ? document.getElementById(id) : null);
  // a wrapper folded shut (height or width 0, its overflow hidden: an accordion closed) hides what's in it, though
  // each box keeps its own size (the review of Oct 15); read once a wrapper per look
  const folded = new Map();
  const foldedShut = (n) => {
    if (!folded.has(n)) {
      const s = getComputedStyle(n);
      const r = n.getBoundingClientRect();
      folded.set(n, /hidden|clip/.test(`${s.overflowX} ${s.overflowY}`) && (r.height < 1 || r.width < 1));
    }
    return folded.get(n);
  };
  // (on top of the page, drawn wherever its wrappers are: a fixed box, a modal dialog, an open popover)
  const onTop = (n, st) => {
    if (st.position === 'fixed') return true;
    try { return n.matches(':modal, :popover-open'); } catch (e) { return false; }
  };
  const visible = (el) => {
    if (!el || !el.getClientRects().length) return false;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') return false;
    // a wrapper folded shut hides what's in its flow; not a box fixed or on the top layer (a Quick Apply dialog in an
    // overflow-hidden host of no height), nor one placed absolutely against a wrapper outside the folded one (the
    // review of 0.3.188)
    if (onTop(el, s)) return true;
    let abs = s.position === 'absolute';
    for (let n = el.parentElement, d = 0; n && d < 8 && n !== document.body; n = n.parentElement, d++) {
      const ns = getComputedStyle(n), positioned = ns.position !== 'static';
      if ((!abs || positioned) && foldedShut(n)) return false;
      if (onTop(n, ns)) return true;
      if (positioned) abs = ns.position === 'absolute';
    }
    return true;
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
  // A file box's words that say nothing of what it takes ("Attach"); and a drop zone's, "Drop files here or Select
  // files" (Workday's, live run 144)
  const GENERIC_FILE = new RegExp('^(attach|upload|choose (a )?file|browse|select files?|add (a )?file|'
    + '((drag (and|&) )?drop (your )?files? here)( or (select|browse|choose|upload) (your )?files?)?|or|enter manually)$', 'i');
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
    // a file box inside an upload widget labelled for it: Workday's questionnaire, its "...-FileUpload" wrapper
    // labelled by the question's own rich text ("Please upload your Letter of Application/Cover Letter."), not the drop
    // zone's words before the box (CSN's, NSU's and UNLV's, live run 144). Only a wrapper that holds this box and no
    // other, and only where the words before the box say nothing of their own: never a group's, a section's or a
    // tab's label over several boxes ("Resume and Cover Letter" over a resume's box and a cover letter's: the review)
    const before = preceding(el);
    if (el.type === 'file' && (!before || GENERIC_FILE.test(before))) {
      for (let up = el.parentElement, i = 0; up && i < 3; up = up.parentElement, i++) {
        if (up.matches('form, fieldset, section, [role="group"], [role="radiogroup"], [role="region"], '
                       + '[role="dialog"], [role="tabpanel"]')) break;
        if ([...up.querySelectorAll(CONTROL)].some((c) => c !== el)) break;
        if (up.hasAttribute('aria-labelledby')) {
          const t = labelledBy(up);
          if (t) return t;
          break;
        }
      }
    }
    return before || el.name || '';
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
      // a question written inside the group as a paragraph of its own, just before its choices, not a legend (the
      // review of Oct 15: it read as ""): walked back from the child holding the first choice, stopping at another
      // question's boxes (one fieldset holding several questions: each its own, the review of 0.3.188), past a choice's
      // own label and anything not drawn (a hidden "This field is required")
      let holder = first;
      while (holder.parentElement && holder.parentElement !== container) holder = holder.parentElement;
      let inner = '';
      for (let sib = holder.parentElement === container ? holder.previousElementSibling : null; sib;
           sib = sib.previousElementSibling) {
        if (sib.matches(CONTROL) || sib.querySelector(CONTROL)) break;
        if ((sib.matches('label') && sib.htmlFor) || !visible(sib)) continue;
        const t = txt(sib);
        if (t && t.length < 300) { inner = t; break; }
      }
      // (words before the group outside it are its question unless none: a hint inside it, "Select one", isn't, the
      // review of 0.3.188; a question inside it, ending "?" or ":", is)
      const before = preceding(container);
      if (inner && (!before || /[?:]\s*\*?$/.test(inner))) return inner;
      return before;
    }
    return preceding(first);
  };
  // A class that says its box is required ("required", "form-group required", "field--mandatory"); never "not-required",
  // "optional", a word it's only part of ("requiredFieldsNote"), nor a framework's record of its validators: AngularJS
  // puts "ng-valid-required" on every box with an ng-required, false or not, and on its ng-form (the review)
  const REQUIRED_CLASS = /(?:^|[_-])(?:required|mandatory)(?:$|[_-])/i;
  const NOT_REQUIRED_CLASS = /(?:not|non)[-_]?required|optional/i;
  const requiredClass = (n) => {
    const c = n && typeof n.className === 'string' ? n.className : '';
    return !NOT_REQUIRED_CLASS.test(c) && c.split(/\s+/).some((t) => REQUIRED_CLASS.test(t)
      && !/valid|^ng-|pristine|dirty|touched/i.test(t));
  };
  // The wrappers around a box (or a group's container) that hold no other question's box, nearest first, three at most:
  // what they say of being required is the box's own
  const ownWrappers = (el) => {
    const out = [];
    for (let n = el.parentElement, d = 0; n && d < 3 && n !== document.body; n = n.parentElement, d++) {
      if ([...n.querySelectorAll(CHOICE)].some((x) => x !== el && !el.contains(x))) break;
      out.push(n);
    }
    return out;
  };
  const isRequired = (el, label) => {
    // (and SAP's Gigya screen sets: data-required, their star a label of its own beside the question's, Edwards
    // Vacuum's "Make my profile visible to:", run 61)
    if (el.required || el.getAttribute('aria-required') === 'true' || el.getAttribute('data-required') === 'true') return true;
    // "First Name *", "* First Name", "Email * (work)", Lever's "Full name ✱"; not "* indicates a required field"
    if ((/[*✱＊∗]/.test(label || '') && !/indicates?|denotes?|required fields?/i.test(label))
        || /\(required\)/i.test(label || '')) return true;
    // the box's own label drawn required by its class (its star drawn by CSS: "<label class="required">")
    const own = labelEl(el);
    if (own && own !== el && requiredClass(own)) return true;
    // a wrapper of the box's own that says so (the owner's note 48, Oct 20: "Ready to submit" with required boxes still
    // empty): aria-required on the group or field around it, its class, or a marker in it beside the box ("<span
    // class="required">*</span>" outside the label, an <abbr title="required">)
    for (const n of ownWrappers(el)) {
      if (n.getAttribute('aria-required') === 'true' || n.getAttribute('data-required') === 'true' || requiredClass(n)) return true;
      if ([...n.querySelectorAll('abbr[title*="equired" i], [class*="required" i], [class*="mandatory" i]')]
        .some((m) => visible(m) && (!m.contains(el) && requiredClass(m) && clean(m.textContent || '').length <= 12
          && !/optional/i.test(m.textContent || '')
          || m.matches('abbr[title*="equired" i]')))) return true;
    }
    // Taleo's questionnaire: "1. Are you over the age of 18? . Required" (Kforce's, live, Oct 2026)
    if (/[?.:]\s*\.?\s*required\s*$/i.test(label || '')) return true;
    // Oracle: a required row's label says so only by a class (its star is drawn by CSS)
    const row = el.closest('.input-row');
    if (row && row.querySelector('.input-row__label--required')) return true;
    const ff = el.closest('[data-automation-id^="formField"]');
    if (ff && ff.querySelector('abbr[title*="equired"], [class*="required" i]')) return true;
    // a file box the group around it says is required ("Your resume/CV is required - upload your document here":
    // EMD's, live run 19, Oct 11), when what it names is the box's own (not an attachments box beside it)
    if (el.type === 'file') {
      let up = el.parentElement;
      for (let i = 0; up && i < 4; i++, up = up.parentElement) {
        const said = (up.innerText || up.textContent || '').slice(0, 400);
        const m = said.match(/\b(resume|cv|cover letter|transcript)s?\b[^.!?\n]{0,40}\b(?:is|are) required\b/i);
        if (m) return new RegExp('\\b' + m[1] + '\\b', 'i').test(label || '') && !/optional/i.test(label || '');
      }
    }
    return false;
  };
  const optionLabel = (el) => {
    const l = labelEl(el);
    if (l && txt(l)) return txt(l);
    return clean(el.getAttribute('aria-label') || labelledBy(el) || el.value || txt(el));
  };
  // The words beside a check box with no label of its own: the text of its own wrapper, up to the
  // first one that holds another box (UKG Pro's Create Account consent, live, Oct 2026: "By checking
  // this box, I have read and agree to the Consent and Privacy Policy*" in a span after the box).
  const CHOICE = CONTROL + ', [role="checkbox"], [role="radio"], [role="switch"]';
  // a file list's boxes (Taleo's), and the file a row of one is for (its "Select the file: <name>" box's title)
  const FILE_BOX = /^(select the file:\s*\S.*|(this )?document is (a|the|your) (resume|cv))$/i;
  const fileOfRow = (row) => {
    for (const b of row.querySelectorAll('input[type="checkbox"]')) {
      const m = clean(b.title || '').match(/^select the file:\s*(.+)$/i);
      if (m) return m[1];
    }
    return '';
  };
  const besideText = (el) => {
    for (let n = el.parentElement, d = 0; n && n !== document.body && d < 3; n = n.parentElement, d++) {
      if ([...n.querySelectorAll(CHOICE)].some((x) => x !== el && !el.contains(x))) return '';
      const copy = n.cloneNode(true);
      copy.querySelectorAll('button, [role="button"], input, select, textarea, script, style').forEach((x) => x.remove());
      const t = clean(copy.textContent);
      if (t) return t.length < 300 ? t : t.slice(0, 297).replace(/\s+\S*$/, '') + '…';
    }
    return '';
  };
  // The repeated block a field sits in, e.g. "Work Experience 2" or "Education 1". Not the field's
  // own group: Workday's date is a group labelled by the date's label ("From"), inside the block,
  // and (live, Oct 2026) a fieldset whose legend is that label.
  const HEADING = ':scope > h2, :scope > h3, :scope > h4, :scope > h5, :scope > legend, :scope > [role="heading"], :scope > div:first-child > h3, :scope > div:first-child > h4';
  const bare = (t) => clean(t).replace(/[\s*:]+$/, '').toLowerCase();
  const BLOCK_NAME = /\b(?:work|experience|employment|education|school|licen[cs]e|certification|reference)/i;
  const sectionOf = (el, own) => {
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
      // (UKG Pro's blocks: a form region its aria-label numbers, "Work Experience 2", with no heading: Desert
      // Financial's, run 55. Only a block's name: never a whole application's "Step 2 of 3" or its requisition number)
      const region = role === 'form' ? clean(node.getAttribute('aria-label') || '') : '';
      if (!t && BLOCK_NAME.test(region) && /\b\d+\s*$/.test(region)) t = region;
      if (!t || t.length > 80 || (own && bare(t) === bare(own))) continue;  // (the field's own group)
      if (container || /\b\d+\s*$/.test(t)) return t;
    }
    return '';
  };
  // The block Workday's ids name ("workExperience-16--location", data-fkit-id
  // "workExperience-16--startDate"): what ties a box to the block of its neighbours
  const BLOCK_ID = /^(.+?-\d+)--/;
  const blockId = (el) => {
    const own = BLOCK_ID.exec(el.id || '');
    if (own) return own[1];
    const kit = el.closest('[data-fkit-id]');
    const m = kit && BLOCK_ID.exec(kit.getAttribute('data-fkit-id') || '');
    return m ? m[1] : '';
  };
  // Files an upload box has already sent: Workday's box empties after each upload and lists the
  // file below it ("resume.pdf  Successfully Uploaded!"), and Eightfold's stays empty beside the
  // file's own buttons ("Replace", "Delete file resume.pdf", "Preview file: resume.pdf") under
  // "File upload completed successfully". So an empty box isn't one still to fill: its value is
  // the file's name, where the page gives it. Looks only in the box's own field: stops at the
  // first wrapper holding another form control, or another upload box, shown or not (Workday's
  // My Experience: the Cover Letter's box, emptied of a file kept from before, read the
  // Resume/CV list's file as its own, and the cover letter written for the job never went in).
  // A second file input within the box's own field (the wrapper around it and its own label), with
  // no label of its own or the same ones, and hidden or beside it in its own wrapper, is the same
  // upload's (a drop zone's hidden twin). A box's labels are all its aria-labelledby names:
  // "docs-legend rl" and "docs-legend cl" are a group's two boxes (its legend, then each one's own),
  // never one upload; and a shown box of its own row, unlabelled or named only by the section's
  // heading, is another upload (the review's third round: the Cover letter box read the resume's file).
  const FILE_BUTTON = /^(?:delete|remove|preview|download|view|open) file:?\s+(.+)$/i;
  const UPLOADED = /\bfile upload completed\b|\bupload(?:ed)? (?:completed? )?successfully\b|\bsuccessfully uploaded\b/i;
  const fileLabels = (el) => {
    const named = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map((id) => document.getElementById(id)).filter(Boolean);
    if (named.length) return named;
    const label = (el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null) || el.closest('label');
    return label ? [label] : [];
  };
  const sameUpload = (el, x) => {
    const mine = fileLabels(el);
    if (!mine.length) return false;
    const own = mine[mine.length - 1];  // (the field's own label: a group's legend comes first)
    let field = el.parentElement;
    while (field && !field.contains(own)) field = field.parentElement;
    const theirs = fileLabels(x);
    return !!field && field.contains(x)
      && (!theirs.length || theirs.length === mine.length && theirs.every((l, i) => l === mine[i]))
      && (x.getClientRects().length === 0 || x.parentElement === el.parentElement);
  };
  const uploadedNear = (el) => {
    // iCIMS keeps a file sent with its form in a hidden box named after its own ("<name>_FileName"), shown nowhere once
    // its page is back (Valley Health's, run 144: read as empty, the resume went up again, and the page with it)
    const kept = el.id && el.id.endsWith('_File') ? document.getElementById(el.id.slice(0, -'_File'.length) + '_FileName') : null;
    if (kept && kept.type === 'hidden' && clean(kept.value)) return clean(kept.value);
    for (let n = el.parentElement, d = 0; n && d < 6; n = n.parentElement, d++) {
      if (Array.from(n.querySelectorAll('input, select, textarea')).some((x) => x !== el && x.type !== 'hidden'
          && (x.type === 'file' ? !sameUpload(el, x) : x.getClientRects().length > 0))) break;
      const items = Array.from(n.querySelectorAll('[data-automation-id="file-upload-item-name"], '
        + '[data-automation-id*="fileName" i], [class*="file-name" i], [class*="filename" i]')).map(txt).filter(Boolean);
      const buttons = Array.from(n.querySelectorAll('button, [role="button"], a')).filter((b) => b.getClientRects().length > 0)
        .map((b) => clean(b.getAttribute('aria-label') || txt(b)));
      items.push(...buttons.map((t) => (FILE_BUTTON.exec(t) || [])[1]).filter(Boolean));
      if (items.length) return Array.from(new Set(items)).join(', ');
      if (buttons.some((t) => /^replace(?: file)?$/i.test(t)) || UPLOADED.test(txt(n))) return 'uploaded';
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
    // (only an id of our own shape is kept: one a page set itself, quotes and all, would steer the desk's selectors:
    // the review of Oct 15)
    if (!id || usedIds.has(id) || !/^[\w.:-]{1,80}$/.test(id)) id = tag(el, newId());
    usedIds.add(id);
    assigned.set(el, id);
    return id;
  };
  const gidOwners = new Map();
  const ownGid = (el, attr) => {
    let gid = el.getAttribute(attr);
    // (our own shape only, as idOf keeps: one a page set itself never becomes a selector, the review of 0.3.188)
    if (!gid || !/^[\w.:-]{1,80}$/.test(gid) || (gidOwners.has(gid) && gidOwners.get(gid) !== el)) {
      gid = newId();
      el.setAttribute(attr, gid);
    }
    gidOwners.set(gid, el);
    return gid;
  };

  // "search", "job-search", "jobSearchForm"; not "research" ("toyotaresearchinstitute")
  const isSearchName = (s) => !!s && (/(^|[^a-zA-Z])search/i.test(s) || /[a-z]Search/.test(s));
  const HONEYPOT = /for robots|robots only|if you('| a)?re (a )?human|not (be )?(filled|entered) by humans|honey ?pot|leave this field (blank|empty)/i;
  const fields = [];
  const fieldEls = new Map();  // each field's element (a group's first member), for the aside pass
  const blockIds = new Map();  // a field's blockId, where its box has one
  const passwordBoxes = [];
  const seen = new Set();
  const groups = new Map();
  // (and a jQuery UI selectmenu's button, a span standing in for its hidden select: BrassRing's security questions,
  // run 67, never read, so the desk could only leave them to the person)
  const sel = 'input, textarea, select, button[aria-haspopup="listbox"], [role="button"][aria-haspopup="listbox"], '
    + '[role="combobox"], [role="radio"], [role="checkbox"], [role="switch"]';
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
      // Same-named check boxes in different blocks are each block's own (Workday names every job's
      // "I currently work here" alike): one group per block, never one across them
      const block = !isRadio && el.name ? '@' + sectionOf(el, '') : '';
      const key = (isRadio ? 'r:' : 'c:') + (el.name ? 'n:' + el.name + block : container ? 'g:' + ownGid(container, 'data-ja-gid') : 'e:' + idOf(el));
      if (!groups.has(key)) groups.set(key, { isRadio, container, members: [] });
      groups.get(key).members.push(el);
      continue;
    }

    let kind = 'text';
    let options = null;
    let value = el.value;
    // a jQuery UI selectmenu's button ("<id>-button") and the hidden select it stands for: its choices and whether
    // it's required are the select's
    const twin = role === 'button' && el.id && el.id.endsWith('-button')
      ? document.getElementById(el.id.slice(0, -'-button'.length)) : null;
    const native = twin && twin.tagName === 'SELECT' ? twin : null;
    // (BrassRing's search box standing for a select of several choices: the question itself, not a site's search)
    const standIn = tagName === 'input' && role === 'combobox' && standsFor(el) && standsFor(el).multiple ? standsFor(el) : null;
    const cx = role === 'combobox' ? cxTicked(el) : null;  // (Oracle's multi-select: its pills)
    // (and no other element acting as a button: a site's own menus, a header's language picker, aren't its form)
    if (role === 'button' && !['button', 'input', 'select', 'textarea'].includes(tagName) && !native) continue;
    // (the select such a search box stands for is that box's question, never one of its own: BrassRing's hides it only
    // with its stylesheet, its style attribute garbled)
    if (tagName === 'select' && el.multiple && el.id
        && document.querySelector(`input[select="${CSS.escape(el.id)}"][role="combobox"]`)) continue;
    if (tagName === 'textarea') kind = 'textarea';
    else if (tagName === 'select') {
      kind = 'select';
      options = Array.from(el.options).map((o) => clean(o.text)).filter(Boolean);
      // (nor its own first choice that only asks for one, "Select your state" with a value of its own, or a greyed one
      // it opens on, "<option disabled selected>Gender</option>": nothing chosen, as for a jQuery UI menu's list)
      // (only a prompt: a first choice whose words start by asking, "Select Gender", "Select Your Highest Level of
      // Education", whatever its value; never an answer such as "Choose not to disclose" or "Select if you prefer not
      // to say": the review, round 2)
      const PROMPT = /^\s*(?:-+\s*)?(?:please )?(?:select|choose|pick)(?![\w-])/i;  // (never "Pick-up shifts")
      const ANSWERS = /\b(?:not|decline[sd]?|prefer|don'?t|do not|wish)\b/i;
      value = Array.from(el.selectedOptions).filter((o) => o.value !== '' && !o.disabled
        && !(o.index === 0 && PROMPT.test(o.text) && !ANSWERS.test(o.text)))
        .map((o) => clean(o.text)).join(', ');
    } else if (type === 'file') {
      kind = 'file';
      value = Array.from(el.files || []).map((f) => f.name).join(', ') || uploadedNear(el);
    }
    else if (type === 'password') { kind = 'password'; value = el.value ? '(set)' : ''; }
    else if (tagName === 'button' || native || (role === 'combobox' && tagName !== 'input')) {
      kind = 'listbox';
      value = txt(el);
      if (native) {
        options = Array.from(native.options).map((o) => clean(o.text)).filter(Boolean);
        // (its button shows the placeholder, "Select question", whatever its value: "", or "-1")
        const chosen = Array.from(native.selectedOptions).filter((o) => o.value !== '' && !o.disabled
          && !(o.index === 0 && /^\s*(?:-+\s*)?(?:select|choose|please select|pick)\b/i.test(o.text)));
        // (a multiselect's button reads "Choose..." whatever is ticked: what's ticked is its value, BrassRing's
        // "Please check each applicable box", run 87)
        if (native.multiple) value = multiTicked(el, native).join(', ');
        else if (!chosen.length) value = '';
      }
    }
    else if (role === 'combobox' || el.getAttribute('aria-autocomplete') === 'list'
             || el.getAttribute('data-uxi-widget-type') === 'selectinput') {  // Workday's search prompts (2026)
      kind = 'combobox';
      // its chosen items sit beside the input's own box, in the prompt's outer container
      const workday = el.closest('[data-automation-id="multiSelectContainer"]')
        || el.closest('[data-automation-id="multiselectInputContainer"]');
      const pills = cx ? cx : standIn ? searchTicked(el, standIn)
        : workday ? Array.from(workday.querySelectorAll('[data-automation-id="selectedItem"], [class*="selected" i] [class*="label" i]')).map(txt).filter(Boolean)
        : pillsNear(el);
      if (pills.length) value = pills.join(', ');
      else if (workday || standIn || cx) value = '';  // text left in its search box isn't a choice; nor is "0 items selected"
      else if (!el.value) value = shownNear(el);  // react-select shows the choice beside an empty input
      if (standIn) {  // (its choices, where the select holds them; BrassRing's lists them only once opened)
        const named = Array.from(standIn.options).filter((o) => o.value !== '').map((o) => clean(o.text)).filter(Boolean);
        if (named.length) options = named;
      }
    }
    // A site's own search box (header, nav, search form) is not part of the application.
    if (el.closest('[role="search"], header, nav') || (type === 'search' && !standIn)) continue;
    const form = el.closest('form');
    if (form && [form.getAttribute('action'), form.id, form.getAttribute('class')].some(isSearchName)) continue;
    let label = labelFor(el);
    // Upload widgets often label the input with its button ("Attach"); use the field's heading,
    // or for a hidden one the upload button right after it, which opens it and names what it takes
    // ("Upload Resume/CV": EMD's, Oct 2026)
    if (kind === 'file' && GENERIC_FILE.test(label) && el.getClientRects().length === 0) {
      const next = el.nextElementSibling;
      const t = next && next.matches('button, [role="button"]') ? txt(next) : '';
      if (t && t.length < 80 && /^(upload|attach|add|choose|select|browse)\b/i.test(t) && !GENERIC_FILE.test(t)) label = t;
    }
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
      id: idOf(el), kind, label, value: kind === 'password' ? value : clean(String(value || '')),
      required: isRequired(el, label) || [native, standIn].some((s) => !!s && (s.required
        || s.getAttribute('aria-required') === 'true' || s.getAttribute('ng-required') === 'true')),
    };
    // an account's security answer the desk put in for the person (browser.fill's `secret`): never read back. "(set)"
    // only for a real answer the desk saw go in; a list or menu still on its placeholder (whatever its value: "-1", or a
    // button's "Select question") has nothing; anything else in it, "(unconfirmed)"
    if (el.hasAttribute('data-ja-secret') && f.value) {
      const placeholder = /^\s*(?:-+\s*)?(?:select|choose|please select|pick)\b/i;
      const list = tagName === 'select' ? el : native;
      const unchosen = list
        ? !Array.from(list.selectedOptions).some((o) => o.value !== '' && !o.disabled
                                                      && !(o.index === 0 && placeholder.test(o.text)))
        : (kind === 'listbox' || kind === 'combobox') && placeholder.test(f.value);
      const kept = el.getAttribute('data-ja-secret-index');  // (the choice confirmed: another since is the person's)
      const moved = !!list && kept !== null && list.selectedIndex !== Number(kept);
      f.value = unchosen ? '' : el.getAttribute('data-ja-secret') === 'in' && !moved ? '(set)' : '(unconfirmed)';
    }
    if (tagName === 'input' && type && type !== 'text') f.input_type = type;
    // a box that takes a date: its own type, or a date picker's, by its hint or by the calendar it opens as a dialog
    // (Eightfold's "Preferred Start Date": a combobox hinted "Select date", issue #112, note 41); a hint that's a date's
    // form ("DD/MM/YYYY") as it is, for the order its parts go in
    const DATE_HINT = /^(?:(?:select|choose|pick|enter) (?:a )?date|[md]{1,2}([\/.\-])[md]{1,2}\1y{2,4}|y{4}([\/.\-])m{1,2}\2d{1,2})$/i;
    const dateHint = [el.getAttribute('placeholder'), el.getAttribute('aria-label')].map((h) => clean(h || ''))
      .find((h) => DATE_HINT.test(h));
    if (tagName === 'input' && (type === 'date' || dateHint
        || (el.getAttribute('aria-haspopup') === 'dialog' && /\bdate\b/i.test(label)))) {
      f.date = dateHint && !/date/i.test(dateHint) ? dateHint.toLowerCase() : true;
    }
    // a box the site marks as a phone's (iCIMS's "Number" beside its Phone Country Code: tel-national)
    const autocomplete = (el.getAttribute('autocomplete') || '').trim().toLowerCase().split(/\s+/).pop();
    if (tagName === 'input' && /^tel(-national|-local)?$/.test(autocomplete)) f.autocomplete = autocomplete;
    const section = sectionOf(el, label);
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
      // a text box that keeps only a number (UKG Pro's question box, autoNumeric's: run 147, anything else emptied)
      if (el.getAttribute('data-automation') === 'numeric-response'
          || /\bautoNumeric\s*:/.test(el.getAttribute('data-bind') || '')) f.numeric = true;
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
    if (el.multiple || (standIn && standIn.multiple) || (kind === 'combobox' && cx)) f.multiple = true;
    // a list whose first choice (or preset country) the desk judged the site's once: a pick of it since is the person's
    if ((kind === 'select' || kind === 'listbox' || kind === 'combobox') && el.hasAttribute('data-ja-default-judged')) f.judged = true;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') f.disabled = true;
    if (el.readOnly) f.readonly = true;
    if (el.getAttribute('aria-invalid') === 'true') f.invalid = true;
    // the browser's own reading of a required box, read silently (its validity: never checkValidity() or
    // reportValidity(), which tell the page): still empty ("missing"), or holding what its own rules refuse, in a form
    // whose sending the browser checks ("bad": not one that turns the checks off, nor a box in no form)
    const validity = (native || standIn || el).validity;
    if (f.required && validity && !f.disabled && !f.readonly) {
      // (only where the value read here agrees: an upload widget empties its box once the file is sent, its name shown
      // beside it, and a search box's choice shows as a pill, the box itself empty: the review of the ready check)
      // ("bad" only for a box that isn't empty but holds what its rules refuse: an emptied upload box is neither)
      if (validity.valueMissing) { if (!f.value) f.missing = true; }
      else if (!validity.valid && el.form instanceof HTMLFormElement && !el.form.noValidate) f.bad = true;
    }
    // a file box in a part of the page not drawn, one that holds its words too (a section folded shut or hidden, a step
    // not on show): not one the page asks for now. Not one an upload widget hides alone, beside its own button
    if (kind === 'file' && !visible(el)) {
      let hidden = null;
      for (let n = el.parentElement, d = 0; n && d < 6 && n !== document.body && !visible(n); n = n.parentElement, d++) {
        hidden = n;
      }
      if (hidden && clean(hidden.textContent || '')) f.unseen = true;
    }
    if (el.maxLength > 0 && el.maxLength < 100000) f.max_length = el.maxLength;
    fields.push(f);
    fieldEls.set(f, el);
    const ofBlock = blockId(el);
    if (ofBlock) blockIds.set(f, ofBlock);
    if (kind === 'password') passwordBoxes.push(el);
  }

  // An upload that's only a button opening the site's own dialog, with no file box on the page ("Upload a Resume
  // Opens a dialog": the older SuccessFactors site's, APS's, the live check's plus probe Oct 11): read as the file
  // box it stands for, and filled through its dialog
  for (const b of document.querySelectorAll('[role="button"], button')) {
    if (b.getClientRects().length === 0 || b.disabled) continue;
    const said = clean([labelledBy(b), b.getAttribute('aria-label') || '', txt(b)].join(' '));
    const m = said.match(/\bupload (?:a |an |your )?(resume|cv|cover letter|transcript)\b/i);
    if (!m || !/\bopens? a (?:dialog|window)\b/i.test(said)) continue;
    const field = b.closest('.RCMFormField, [class*="attachmentField" i]') || b.parentElement;
    if (field && field.querySelector('input[type="file"]')) continue;  // (its own file box is read as itself)
    const lab = field && field.querySelector('label');
    const label = clean((lab ? (lab.textContent || '') : m[1]).replace(/^[\s*\u00a0]+/, '')) || m[1];
    if (fields.some((f) => f.kind === 'file' && f.label === label)) continue;
    // a file there already: its name shown in place of the upload's words, or the upload's tick
    const shown = field ? [...field.querySelectorAll('[id$="_attachDownloadLabelLink"], [class*="attachSuccess" i]')]
      .filter((x) => x.getClientRects().length > 0) : [];
    const link = field && field.querySelector('[id$="_attachDownloadLabelLink"]');
    const name = link && link.getClientRects().length > 0 && !/^upload\b/i.test(txt(link)) ? txt(link) : '';
    const f = { id: idOf(b), kind: 'file', label, value: name || (shown.length ? 'uploaded' : ''), opens_dialog: true,
                required: !!(lab && /\*/.test(lab.textContent || '')) || /\brequired\b/i.test(said) };
    const section = sectionOf(b, label);
    if (section && section !== label) f.section = section;
    fields.push(f);
    fieldEls.set(f, b);
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
      // still nothing but the box's own value ("", or "on" for one with none): the words beside it
      if (/^(on|off|true|false|[01])?$/i.test(label)) label = besideText(first) || label;
      // nor beside it: a file list's box, by its title (Kforce's Taleo, the nightly of Oct 12: "Select the file:
      // resume.pdf" and "Document is a resume", one row a file), with its row's file and how many rows have that file.
      // Only those: another box's title ("I agree" beside a row's job alerts) isn't its question
      const titled = /^(on|off|true|false|[01])?$/i.test(label) && FILE_BOX.test(clean(first.title || ''))
        ? clean(first.title) : '';
      if (titled) label = titled;
      const single = { id: idOf(first), kind: 'checkbox', label, required: isRequired(first, label), value: checkedOf(first) };
      // one the page marks wrong while it's unticked, as a group so marked (BrassRing's "Employment At Will", Edward
      // Jones' run 89: "Required" beside it, naming nothing)
      if (first.getAttribute('aria-invalid') === 'true' && !single.value) single.invalid = true;
      // (and the browser's own word that a required one is still unticked: its validity, read silently)
      if (single.required && first.validity && first.validity.valueMissing && !first.disabled) single.missing = true;
      const row = titled && first.closest('tr');
      if (row) {
        const table = row.closest('table') || row.parentElement;
        single.row_file = fileOfRow(row);
        single.same_file_rows = [...table.querySelectorAll('tr')].filter((r) => fileOfRow(r) === single.row_file).length;
      }
      const section = sectionOf(first);
      if (section && section !== label) single.section = section;
      fields.push(single);
      fieldEls.set(single, first);
      const ofBlock = blockId(first);
      if (ofBlock) blockIds.set(single, ofBlock);
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
    // a group the page marks wrong (Edwards Vacuum's Create Account, run 61: "This field is required" over its radios)
    // (while nothing's chosen, or the choice is the one marked: a mark the page left on one radio after another was
    // picked isn't about the group as it is now)
    const marked = g.members.filter((m) => m.getAttribute('aria-invalid') === 'true');
    if (marked.length && (!g.members.some(checkedOf) || marked.some(checkedOf))) group.invalid = true;
    // (a required group with nothing chosen, by the browser's own validity, read silently)
    if (group.required && !g.members.some(checkedOf) && g.members.some((m) => m.validity && m.validity.valueMissing
        && !m.disabled)) group.missing = true;
    const section = sectionOf(g.container || first);
    if (section && section !== label) group.section = section;
    fields.push(group);
    fieldEls.set(group, first);
  }
  // A box whose block wasn't found above takes the one its neighbours with the same block id are in
  const blockOf = new Map();
  for (const [f, b] of blockIds) if (f.section && !blockOf.has(b)) blockOf.set(b, f.section);
  for (const [f, b] of blockIds) {
    if (!f.section && blockOf.has(b) && blockOf.get(b) !== f.label) f.section = blockOf.get(b);
  }

  const SUBMIT = /\bsubmit\b|send (my )?application|finish (my )?application|complete (my )?application/i;
  // browser.FINALISH_RE and final_text: a form's own "Apply for this job", "Apply Now ›" sends it
  const FINALISH = /^(apply( now| online)?( for (this|the) (job|position|role|opening))?|apply to (this |the )?(job|position|role|opening)|send( now| (my )?application)?|finish|complete( (my )?application)?|confirm( and send)?)$/i;
  const finalText = (t) => t.replace(/\s+(arrow_forward|arrow_right_alt|chevron_right|navigate_next|east)$/i, '').replace(/[\s\u203a\u00bb\u2192>!.]+$/, '').trim();
  const POSTING_PAGE = /career(?:_|%5f)ns=job(?:_|%5f)listing(?:&|#|$)/i;  // browser.POSTING_PAGE_RE
  // A box of the site's own beside the application with a Submit of its own: the footer's
  // job alerts or newsletter sign-up. Its Submit is never the application's.
  const SIDE_BOX = /job alerts?|alerts? by e-?mail|e-?mail alerts?|newsletter|\bsubscribe\b|talent (?:community|network|pool)|notify me|similar (?:jobs|openings|roles)|stay (?:connected|in touch)/i;
  /*SIGN_IN_FORM*/
  /*OPEN_DIALOGS*/
  const boxesIn = (form) => [...form.elements].filter((e) => /^(INPUT|SELECT|TEXTAREA)$/.test(e.tagName)
    && !/^(hidden|submit|button|image|reset)$/i.test(e.type || '') && e.getClientRects().length > 0);
  // a heading that says its form signs the person up ("Sign up for Job Alerts", "Get Job Alerts", "Newsletter")
  const SIGN_UP = /^(?:(?:sign ?up|subscribe|register) (?:for|to)|get|create|set up|receive)\b.{0,30}\b(?:job alerts?|e-?mail alerts?|newsletter)|^(?:job alerts?|newsletter)$/i;
  const sideBox = (el) => {
    const boxes = formOf(el) ? boxesIn(formOf(el)) : null;
    // one or two boxes, one of them for typing (an email address), and words about alerts
    const small = boxes && boxes.length >= 1 && boxes.length <= 2
      && boxes.some((e) => e.tagName === 'INPUT' && /^(text|email|search|)$/i.test(e.getAttribute('type') || ''));
    if (small && SIDE_BOX.test(formOf(el).innerText || '')) return true;
    // a few boxes in a form that says, by its own heading or its kind, it's a sign-up for job alerts or a newsletter
    // (CommonSpirit's "Sign up for Job Alerts", Dignity Health, run 82: an email, a job category, a location, Remote
    // and Job Type, and a spam trap hidden from the eye). Its heading is the nearest one shown before the boxes shown,
    // in a form that isn't the page's frame (one around its header, main or h1), and none of its own buttons goes on
    // with an application ("Submit Application" under "Job Alerts and Communication Preferences": the review's)
    const form = formOf(el);
    if (form && !form.querySelector('header, main, nav, aside, h1, [role="main"], [role="banner"]')) {
      const shown = boxes.filter((e) => visible(e) && !e.closest('[aria-hidden="true"]'));
      const heads = shown.length ? [...form.querySelectorAll('h2, h3, h4, h5, [role="heading"]')].filter((h) => visible(h)
        && (h.compareDocumentPosition(shown[0]) & Node.DOCUMENT_POSITION_FOLLOWING)) : [];
      const head = heads[heads.length - 1];
      // (all its ways on, by their words, values and names: "Submit Application" with the value "Submit", Taleo's
      // input "Save and Continue", a link's Next; and a heading that names the sign-up, close above its boxes, not a
      // talent pool's evergreen posting or a banner far above: the second review's)
      const own = [...form.querySelectorAll('button, input[type="submit"], input[type="button"], input[type="image"], '
        + 'a[href], [role="button"], [role="link"]')]
        .map((b) => [b.value, txt(b), b.getAttribute('aria-label'), b.getAttribute('alt')].join(' '));
      let gap = '';
      if (head) {
        const r = document.createRange();
        r.setStartAfter(head);
        r.setEndBefore(shown[0]);
        gap = clean(r.toString());
      }
      if (shown.length && shown.length <= 6 && !own.some((t) => /applica|apply|next|continue|review|save/i.test(t))
          && (head && gap.length < 200 && SIGN_UP.test(txt(head))
              || /job[\s_-]?alert|newsletter/i.test(form.getAttribute('data-form-type') || ''))) return true;
    }
    const foot = el.closest('footer, [role="contentinfo"]');
    return !!foot && !(boxes && boxes.length > 2) && SIDE_BOX.test(foot.innerText || '');
  };
  const ACTION = /apply|next|continue|review|submit|save|add|upload|sign ?in|log ?in|create (an |your |a new )?account|sign ?up|register|start|back|previous|edit|done|ok\b|accept|agree|use my last|autofill|manually|verify|confirm|remove|delete|forgot|reset|don['\u2019]?t have an account/i;
  // Up to 60 of the page's buttons. A dropdown's entries are choices in a field, not
  // things to do on the page: Eightfold draws them as buttons, and an open list of
  // referral sources or countries used to fill all 60 places before "Submit
  // application", so it was never seen. Submit and next-step buttons always make it in.
  const actions = [];
  const actionEls = new Map();  // each button's element, for the form pass
  const STEP = /^(next|continue|save and continue|review|submit)/i;
  const COOKIE_BOX = '[id*="cookie" i], [class*="cookie" i], [aria-label*="cookie" i], [id*="onetrust" i], '
    + '[class*="onetrust" i], [id*="cybot" i], [id*="gdpr" i], [id*="truste" i]';
  // a "consent" box only when it speaks of cookies: not an account's own consent step (Northrop's Eightfold
  // "consentContainer" and its "auth-v2-consent-submit-button", live, Oct 2026)
  const CONSENT_BOX = '[id*="consent" i], [class*="consent" i]';
  // or a banner known by its words alone (the review of Oct 15): a dialog, or a box fixed to the page, named nothing,
  // whose opening words speak of cookies ("We use cookies…", "Cookie Notice"), short, with no box to fill in: not a
  // sticky header with a cookie link, an app's fixed shell or a privacy notice that mentions cookies in passing (its
  // review)
  const spoken = (n) => {
    const t = (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim();
    return t.length < 800 && /cookie/i.test(t.slice(0, 40))
      && !n.querySelector('input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"]), select, textarea');
  };
  const spokenBox = (n) => {
    for (; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      const p = getComputedStyle(n).position;
      if ((p === 'fixed' || p === 'sticky' || n.matches('[role="dialog"], [role="alertdialog"], dialog[open]')) && spoken(n)) return n;
    }
    return null;
  };
  // Buttons drawn as web components (UKG's <ukg-button>, whose real button is in its shadow root) count too,
  // and links whose address is in data-href, followed by the site's script on a click (KPMG's Apply Now).
  const BUTTONS = 'button, [role="button"], input[type="submit"], input[type="button"], a[href], a[data-href], '
    + '[data-tag-name="button"], '
    + 'ukg-button, sl-button, mwc-button, ion-button, vaadin-button, fluent-button, md-filled-button, md-outlined-button, '
    + 'md-text-button, md-filled-tonal-button, md-elevated-button';
  for (const el of document.querySelectorAll(BUTTONS)) {
    if (el.getAttribute('aria-haspopup') === 'listbox' || !visible(el)) continue;
    // a Google map's own controls (Benchmark's posting, live, Oct 2026: drawn afresh all the while) are
    // never an application's buttons
    if (el.closest('.gm-style') && !el.closest('.gm-style-iw, .gm-style-iw-c')) continue;  // (not its info window's)
    const role = el.getAttribute('role');
    // Workday draws a form's own button hidden (aria-hidden) under a div of its own that takes the click
    // (data-automation-id="click_filter", role="button"), named for the button or not at all (KLA's Sign
    // In's is "Submit", live, Oct 2026). The cover reads as the button it covers, and the hidden button,
    // which does nothing when pressed, says it's covered.
    const under = role === 'button' && !txt(el) && el.nextElementSibling;
    const covers = under && under.matches('button[aria-hidden="true"]') && clean(txt(under)) ? under : null;
    const coverOf = el.getAttribute('aria-hidden') === 'true' && el.previousElementSibling;
    const covered = !!coverOf && coverOf.getAttribute('role') === 'button' && !txt(coverOf) && visible(coverOf);
    const t = covers ? clean(txt(covers)) : clean(txt(el) || el.value || el.getAttribute('aria-label') || '');
    // A menu's entries are mostly site navigation, left out, but an open menu of ways to
    // apply (Qorvo's "Apply now ▾": Apply Now, Start apply with LinkedIn) holds the way in.
    const inMenu = role === 'menuitem' || !!el.closest('[role="menu"]');
    const applyItem = inMenu && !el.closest('[role="menubar"]') && /\bapply\b/i.test(t);  // not the site's top bar
    if (role === 'option' || el.closest('[role="listbox"]') || (inMenu && !applyItem)) continue;
    // (a link to read a privacy statement or terms may say more: Amkor's SuccessFactors "Please read and accept
    // the data privacy statement to proceed.", nightly live check Oct 11)
    if (!t || t.length > 60 && !(t.length <= 120 && /^(please )?(read|view|open)\b.*\b(privacy|terms)\b/i.test(t))) continue;
    // (and a link to read the statement is one to press, whatever else it says: Edwards Vacuum's SAP screen set, run 57,
    // "Read and acknowledge the data privacy statement.", in the label of a box greyed out until it's read)
    if (el.tagName === 'A' && !ACTION.test(t) && !/^(please )?(read|view|open)\b.*\b(privacy|terms)\b/i.test(t)) continue;
    const full = covers ? t : t + ' ' + (el.getAttribute('aria-label') || '');
    const formSubmit = el.type === 'submit' && !!formOf(el);
    // SuccessFactors' older sites show a posting inside a form whose submit is "Apply": with
    // nothing in the form to fill, it opens the application and sends nothing
    const opensApplication = formSubmit && POSTING_PAGE.test(location.href) && /^apply( now)?$/i.test(t)
      && ![...formOf(el).elements].some((e) => /^(INPUT|SELECT|TEXTAREA)$/.test(e.tagName)
        && !/^(hidden|submit|button|image|reset)$/i.test(e.type || '') && e.getClientRects().length > 0);
    const isSubmit = SUBMIT.test(full) || (formSubmit && FINALISH.test(finalText(t)) && !opensApplication);
    if (actions.length >= 60 && !isSubmit && !formSubmit && !STEP.test(t)) continue;
    const a = { id: idOf(el), text: t };
    if (applyItem) a.menu = true;
    // a link in the site's own header or navigation, and where it goes: the site's "Apply Now" there (Station
    // Casinos' Oracle, live run 144: a link to its list of jobs) isn't a posting's own way in. Not a header in the
    // page's main part or its article: a posting's own, with its title and its Apply (the review)
    if (el.tagName === 'A' && el.closest('header, nav, [role="banner"], [role="navigation"]')
        && !el.closest('main, article, [role="main"]')) {
      a.site_nav = true;
      if (/^https?:/i.test(el.href || '')) a.goes_to = el.href;
    }
    // a saved draft's "Continue Application" link: where it goes, which says whose application it is (issue #112, note 38)
    if (el.tagName === 'A' && el.href && /^\s*continue\b.*\bapplication\s*$/i.test(t)) a.href = el.href;
    // A cookie banner's own buttons (OneTrust's sits at the very end of a long page, past
    // the page text the desk reads)
    const consent = el.closest(CONSENT_BOX);
    const box = el.closest(COOKIE_BOX) || (consent && /cookie/i.test(consent.innerText || '') ? consent : null) || spokenBox(el);
    if (box && box !== document.body && box !== document.documentElement) a.cookie = true;
    if (formSubmit) a.form_submit = true;
    if (isSubmit) a.is_submit = true;
    if (covered) a.covered = true;
    // how many boxes the form around a plain button shows: a page's script can send a
    // name-and-email box from its "Apply" too
    const around = !formSubmit && (formOf(el) || el.closest('form'));
    if (around && boxesIn(around).length) a.form_fields = boxesIn(around).length;
    if (isSubmit && sideBox(el)) a.aside = true;
    // A button after a password box: a sign-in form's own "Sign In", not the one in the
    // site's header (Workday's opens a sign-in pop-up, and sends nothing)
    if (passwordBoxes.some((p) => p.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) a.after_password = true;
    if (a.after_password && signInForm(el)) a.sign_in_form = true;
    // A "Create Account" in a form with two password boxes is that form's own button, which
    // creates the account (Workday draws it as a div, not a form's submit): never the way to
    // the form. Boxes a pop-up hides count too.
    if (/account|sign ?up|register/i.test(t)) {
      for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
        const boxes = n.querySelectorAll('input[type="password"]').length;
        if (boxes) { if (boxes >= 2) a.account_form = true; break; }
      }
    }
    // A link or plain button after the password boxes, in the very form that holds two of them,
    // whatever it reads: that form's own, which a script sends (ApplicantStack's "Submit", a link
    // whose script calls form.submit()). A form's submit button says so already (form_submit).
    const ownForm = !formSubmit && a.after_password && (formOf(el) || el.closest('form'));
    if (ownForm && ownForm.querySelectorAll('input[type="password"]').length >= 2) a.in_account_form = true;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') a.disabled = true;
    // A block's own Delete ("Education 2"'s): which block it deletes
    // (and whether it names that block, "Delete Education 2", not a file's or a pill's inside it)
    if (/^(delete|remove)\b/i.test(t)) {
      const sec = sectionOf(el, '');
      if (sec) a.section = sec;
      const named = clean(el.getAttribute('aria-label') || el.getAttribute('title') || t).toLowerCase();
      if (sec && named.endsWith(sec.toLowerCase())) a.names_block = true;
      else if (el.getAttribute('aria-label') || el.getAttribute('title')) a.names_other = true;
    }
    // A link to something already on show on this page (Phoenix Children's "Apply!" to its form,
    // #apply) only scrolls there; one to something hidden may be what shows it
    const anchor = el.tagName === 'A' && /^#([A-Za-z][\w:-]*)$/.exec(el.getAttribute('href') || '');
    const target = anchor && document.getElementById(anchor[1]);
    if (target && target.getClientRects().length > 0) a.same_page = true;
    actions.push(a);
    actionEls.set(a, el);
  }
  // A box in such a sign-up (a talent community's email and consent beside a posting) isn't the
  // application's either: not one in a form whose own button goes on with an application
  // ("Enter your email to start your application ... job alerts" and Apply)
  const APPLYISH = /^(apply|next|continue|start|begin|review|save and continue)/i;
  const formAside = new Map();
  for (const f of fields) {
    const el = fieldEls.get(f);
    if (!el) continue;
    const form = formOf(el) || null;
    if (form && !formAside.has(form)) {
      const own = [...form.querySelectorAll('button, input[type="submit"], [role="button"]')].map((b) => b.value || txt(b));
      formAside.set(form, !own.some((t) => APPLYISH.test(t.trim())) && sideBox(el));
    }
    if (form ? formAside.get(form) : sideBox(el)) f.aside = true;
  }
  // Which <form> each box and button is in: its index among this document's forms, left out for one in none. What
  // ties an email box to its own form's button, never to another form's beside it in the same frame (a job alerts
  // sign-up above a password reset)
  const forms = [...document.querySelectorAll('form')];
  const formIndex = (el) => {
    const form = formOf(el) || el.closest('form');
    return form ? forms.indexOf(form) : -1;
  };
  for (const [items, els] of [[fields, fieldEls], [actions, actionEls]]) {
    for (const item of items) {
      const el = els.get(item);
      const at = el ? formIndex(el) : -1;
      if (at >= 0) item.form = at;
    }
  }

  const errors = [];
  // A message that says a box is wanted or wrong ("This field is required", "Please select an option", "2 issues"),
  // never a form's own note on what's required ("Fields marked * are required")
  const SAYS_WANTED = /\b(?:required|must (?:be|have|enter|select|provide|choose|contain|match)|missing|invalid|is not valid|can(?:no|')?t be (?:blank|empty|left blank)|please (?:enter|select|fill|choose|provide|complete|correct|answer|upload|attach)|errors? found|\d+ (?:errors?|issues?|problems?))\b/i;
  const A_NOTE = /\b(?:fields?|questions?|items?) (?:marked|with)\b|\b(?:indicat|denot)(?:es?|ed)\b/i;
  const RULES = /\bpasswords?\b|\bmust contain\b|\bcharacters?\b/i;  // (a password's rules, on show from the start)
  const wanted = [];
  // (Workday's own alert under a box, data-automation-id="inputAlert": Banner Health's text area, run 99, "Error: The field
  // … is required and must have a value." in no alert role or "error" class)
  // (and, where they say a box is wanted, a polite live region's words and a box's message drawn by an "invalid" class,
  // Bootstrap's invalid-feedback: the owner's note 50, Oct 20, a job called ready with the page's own "required" shown)
  for (const el of document.querySelectorAll('[role="alert"], [aria-live="assertive"], [data-automation-id="errorMessage"], [data-automation-id="inputAlert"], [class*="error" i]:not(input):not(select):not(textarea), '
      + '[aria-live="polite"], [class*="invalid" i]:not(input):not(select):not(textarea):not(button):not(td)')) {
    const OWN = '[role="alert"], [aria-live="assertive"], [data-automation-id="errorMessage"], [data-automation-id="inputAlert"], [class*="error" i]';
    // (one around a message read on its own, or around a box, is never read whole: its label would be said as an error;
    // nor one inside a message read whole, nor a password's rules shown before anything is typed. Kept apart, as
    // "wanted": only the desk's check of a page before it's called ready reads them, the rest reads errors as before)
    const own = el.matches(OWN);
    if (!own && (!SAYS_WANTED.test(txt(el)) || A_NOTE.test(txt(el)) || RULES.test(txt(el)) || el.querySelector(OWN)
                 || el.querySelector('input, select, textarea')
                 || (el.parentElement && el.parentElement.closest(OWN)))) continue;
    // without icon-font glyphs (Amkor's "\ue0b1 Invalid email address or password"; an alert
    // that's only an icon says nothing)
    // (never a button or a link marked aria-live, as BrassRing's "Let's get started" is: probe 81; nor a checkbox's or a
    // radio's own label, as Oracle's are, "Disabled Veteran": Mayo Clinic, run 158, unless its words say a box is wanted;
    // a message drawn as a text box's label still is, and one that marks itself as an error, jQuery Validate's
    // <label class="error" for=…>)
    const boxLabel = el.matches('label') && el.control && /^(?:checkbox|radio)$/i.test(el.control.type || '');
    if ((el.matches('button, a[href], [role="button"], [role="link"]') || boxLabel && !SAYS_WANTED.test(txt(el)))
        && !el.matches('[role="alert"], [data-automation-id="errorMessage"], [data-automation-id="inputAlert"], '
                       + '[class*="error" i]')) continue;
    const t = txt(el).replace(/[\uE000-\uF8FF]/g, '').trim();
    if (!/[\p{L}\p{N}]/u.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) continue;  // screen-reader announcements ("… page is loaded")
    // (nor a site's word that it's on its way, EY's "Please wait..." in an alert, run 147: busy, never an error)
    if (/\bpage is loaded\b|^loading\b|^(?:please wait|redirecting(?: you)?)\b[\s,.!]*(?:\.\.\.|…)?$/i.test(t)) continue;
    if (!own) {
      if (t && t.length < 300 && visible(el) && !wanted.includes(t) && wanted.length < 10) wanted.push(t);
      continue;
    }
    if (t && t.length < 300 && visible(el) && !errors.includes(t)) errors.push(t);
    if (errors.length >= 10) break;
  }
  // A box marked invalid says what's wrong in what its aria-describedby points at (Workday's field alert, Banner
  // Health's text area, run 99: the page's own record of the answer shown in it was empty, and its "Error - <the
  // question>" button in Errors Found was longer than a button's words here, so nothing named the box): read as
  // the page's error where its words say something's wrong, never a plain hint
  // (never a box outside the application, a footer's job-alerts email box marked invalid at load, say: the review)
  alerts: for (const box of document.querySelectorAll('[aria-invalid="true"][aria-describedby]')) {
    if (sideBox(box) || box.closest(COOKIE_BOX)) continue;
    for (const id of box.getAttribute('aria-describedby').split(/\s+/)) {
      const el = id && document.getElementById(id);
      if (!el || el.matches('input, select, textarea, button, a[href]')) continue;
      const t = txt(el).replace(/[\uE000-\uF8FF]/g, '').trim();
      // (a box's own alert quotes its whole question, Workday's over 200 characters: room for one, where a page-wide
      // alert gets 300; the readers shorten what they show)
      if (!t || t.length >= 1000 || !/\b(error|required|invalid|must|missing|blank|problem|fix)\b/i.test(t)) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 1 || r.height <= 1) continue;  // (a screen reader's alone, as above)
      if (errors.length >= 10) break alerts;
      if (visible(el) && !errors.includes(t)) errors.push(t);
    }
  }
  // The page's own notices: its alerts, status and live regions, banners, messages and headings, never one inside a list of
  // postings (a "Similar jobs" card's "You have already applied" is that job's, not this one's: the review of note 49).
  // A card of its own, the page's main content in one (Bootstrap's), isn't a list: one beside others like it is
  const BOX = 'article, [class*="card" i], [class*="tile" i], [class*="result" i]';
  const listed = (el) => {
    if (el.closest('li, [role="listitem"]')) return true;
    for (let box = el.closest(BOX); box; box = box.parentElement && box.parentElement.closest(BOX)) {
      const up = box.parentElement;
      if (up && Array.from(up.children).some((o) => o !== box && o.tagName === box.tagName
                                                 && o.className === box.className)) return true;
    }
    return false;
  };
  // Nor one after a heading over other postings ("Similar jobs", "You may also like"), unless the page's own alert or
  // live region (a toast drawn at the page's end); and one in a card of its own goes with the jobs the card names (its
  // links and headings: `card_notices`, this job's only where they're its title: the review of fix-ready-check, 3)
  const HEAD = 'h1, h2, h3, h4, h5, h6, [role="heading"]';
  const SIMILAR = /\b(?:similar|recommended|related|suggested|you (?:may|might) also like|other (?:jobs|openings|positions|roles|opportunities)|more (?:jobs|openings|positions|roles|opportunities))\b/i;
  const heads = Array.from(document.querySelectorAll(HEAD));
  const afterSimilar = (el) => {
    let last = null;
    for (const h of heads) {
      if (h === el || h.contains(el) || !(h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) break;
      last = h;
    }
    return !!last && SIMILAR.test(txt(last));
  };
  const notices = [];
  const cardNotices = [];
  for (const el of document.querySelectorAll('[role="alert"], [role="status"], [aria-live], [class*="alert" i], '
      + '[class*="banner" i], [class*="notice" i], [class*="message" i], [class*="toast" i], [class*="flash" i], '
      + 'h1, h2, h3, h4')) {
    if (listed(el)) continue;
    const t = txt(el);
    if (!t || t.length >= 300 || !visible(el)) continue;
    const live = el.closest('[role="alert"], [role="status"], [aria-live]');
    if (!live && afterSimilar(el)) continue;
    const box = el.closest(BOX);
    if (box && !live) {
      const names = Array.from(box.querySelectorAll('a[href], ' + HEAD))
        .filter((n) => n !== el && !n.contains(el) && !el.contains(n)).map(txt).filter((n) => n && n.length < 120);
      if (cardNotices.length < 10) cardNotices.push({ text: t, names });
      continue;
    }
    if (!notices.includes(t)) notices.push(t);
    if (notices.length >= 20) break;
  }
  const headings = [];
  for (const el of document.querySelectorAll('h1, h2, h3, [data-automation-id="progressBarActiveStep"], [role="heading"]')) {
    const t = txt(el);
    if (t && t.length < 120 && visible(el) && !headings.includes(t)) headings.push(t);
    if (headings.length >= 8) break;
  }
  // Dialogs open over the page, each with all its buttons (an Eightfold form has more than the 60
  // above before its notice's "I Agree")
  const dialogs = openDialogs().map((d) => ({
    heading: d.heading, text: d.text, ...(d.cookie ? { cookie: true } : {}),
    buttons: [...d.el.querySelectorAll(BUTTONS)].filter(visible).map((b) => {
      const t = clean(txt(b) || b.value || b.getAttribute('aria-label') || '');
      const off = b.disabled || b.getAttribute('aria-disabled') === 'true';
      return t && t.length <= 60 ? { id: idOf(b), text: t, ...(off ? { disabled: true } : {}) } : null;
    }).filter(Boolean),
  }));
  // A loading indicator on show: Workday's dots where a step's questions go while it loads them
  // ([data-automation-id="loading"], KLA's, live, Oct 2026), its dots for a file going up, Workday's
  // Canvas dots elsewhere, or a part of the page marked busy (aria-busy, a progress bar with no value,
  // which a step's "2 of 6" bar has)
  const LOADING = '[data-automation-id="loading"], [data-automation-id*="loadingSpinner" i], '
    + '[data-automation-id*="loading-dots" i], [data-automation-id*="loadingDots" i], [data-part="loading-animation-dot"], '
    + '[aria-busy="true"], [role="progressbar"]:not([aria-valuenow]), '
    // (and Cornerstone's loading overlay over the whole page, its spinner in a box and the page greyed, while a
    // requisition's Apply Now stays disabled and without its words: Linde's, the live check's run 105; the site
    // removes it once loaded)
    + '[data-tag="Loading_spinner_tag"], .p-loading[data-tag], '
    // (and Workday's whole step still loading: its grey bars where a step goes, Baker Tilly's Create Account/Sign In, run 62)
    + '[data-automation-id="applyFlowLoadingPage"]';
  // and Workday's placeholder for a section still loading: a hidden "Loading" beside its grey bars (Wells Fargo's and
  // Hitachi's My Information after the account's sign-in, run 57: its Address, Email and Phone never came)
  const bones = () => [...document.querySelectorAll('div, span')].some((el) => el.childElementCount === 0
    && /^loading(?:\.\.\.|\u2026)?$/i.test((el.textContent || '').trim()) && el.nextElementSibling
    && el.nextElementSibling.getAttribute('aria-hidden') === 'true' && visible(el.nextElementSibling));
  // and a site's own word that it's on its way somewhere, on show: "Please wait...", "Loading...", "Redirecting..."
  // (EY's posting while it sent the applicant on to SuccessFactors, run 147: the desk took it for a page going
  // nowhere). Only those words alone, with their dots, in a box drawn on the page; never a screen reader's alone
  const waiting = () => [...document.querySelectorAll('div, span, p, [role="alert"], [role="status"]')].some((el) => {
    if (el.childElementCount > 2) return false;  // (the words' own box, a spinner beside them at most: cheap first)
    const t = (el.textContent || '').trim();
    if (t.length > 60 || !/^(?:(?:please wait|loading|redirecting(?: you)?)[\s,.!]*)+(?:\.\.\.|…)$/i.test(t)) {
      return false;
    }
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1 && visible(el) && !faded(el);  // (faded: below, read once busy is)
  });
  // and Workday's step still drawing: under its progress bar, only its loading dots, boxes with no words, no names and
  // nothing to press (Grand Canyon University's Create Account/Sign In, run 69: no automation id on them)
  const drawing = () => {
    const flow = document.querySelector('[data-automation-id="applyFlowPage"]');
    let bar = flow && flow.querySelector('[data-automation-id="progressBar"]');
    while (bar && bar !== flow && !bar.nextElementSibling) bar = bar.parentElement;
    const body = bar && bar !== flow ? bar.nextElementSibling : null;
    return !!body && visible(body) && !(body.innerText || '').trim()
      && !body.querySelector('input, select, textarea, button, a, [role="button"], [role="link"], iframe, img')
      && [...body.querySelectorAll('div, span')].filter((el) => {  // (drawn: the dots themselves, not empty wrappers)
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && !el.childElementCount && visible(el);
      }).length >= 3;
  };
  // and Workday's application frame mounted with nothing in it yet: no words and nothing to press, nor Workday's own
  // loading dots (those are its loading page, said as such): empty (Grand Canyon University's right after the
  // account's sign-in, run 91: only the site's header and footer), or holding only empty boxes (Baker Tilly's after
  // Apply Manually, the live check's run 117)
  const unmounted = () => [...document.querySelectorAll('[data-mfe-id="applyFlow"]')]
    .some((m) => !(m.textContent || '').trim() && !m.querySelector(LOADING)
      && !m.querySelector('[data-ja-id], input, select, textarea, button, a, [role="button"], [role="link"], iframe, img'));
  const empty = unmounted();
  // (one drawn fully transparent, itself or a wrapper, is no sign of loading: a page kept "busy" by an invisible
  // progress bar waited for nothing, the review of Oct 15)
  const faded = (el) => {
    for (let n = el, d = 0; n && d < 6 && n !== document.body; n = n.parentElement, d++) {
      if (getComputedStyle(n).opacity === '0') return true;
    }
    return false;
  };
  const busy = [...document.querySelectorAll(LOADING)].some((el) => visible(el) && !faded(el)) || bones() || drawing()
    || empty || waiting();
  return { fields, actions, errors, ...(wanted.length ? { wanted } : {}), ...(notices.length ? { notices } : {}),
           ...(cardNotices.length ? { card_notices: cardNotices } : {}),
           headings, dialogs, ...(busy ? { busy: true } : {}),
           ...(empty ? { empty_frame: true } : {}) };
}
""".replace("/*SIGN_IN_FORM*/", SIGN_IN_FORM_JS).replace("/*OPEN_DIALOGS*/", OPEN_DIALOGS_JS)

# Is a dialog open over the page that the box with this id isn't in (OPEN_DIALOGS_JS)? Its heading
# (or "a dialog"), else null. Nothing is filled behind one. A cookie banner has its own rule.
NOTICE_OVER_JS = r"""
(id) => {
  /*OPEN_DIALOGS*/
  const el = document.querySelector(`[data-ja-id="${CSS.escape(id)}"], [data-ja-gid-member="${CSS.escape(id)}"]`);
  const over = openDialogs().find((d) => !d.cookie && !(el && d.el.contains(el)));
  return over ? (over.heading || 'a dialog') : null;
}
""".replace("/*OPEN_DIALOGS*/", OPEN_DIALOGS_JS)

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

# What an attestation's gate reads of its page, failing closed (the fifth review of fix-accept-gates): `visible`, the
# page's drawn words (innerText); `hidden`, every other word the person can see or open, one line for each text node
# (those not drawn, in a closed <details>, a collapsed or content-visibility: hidden box; all of an open shadow root's;
# a <textarea>'s value and a read-only or disabled text box's; a title, alt or aria-label of four words or more);
# `refuse`, why the page can't be read whole (a closed shadow root, a <canvas>, a frame, ::before/::after words, more
# elements than it reads), else null. Iterative, with no recursion to run out of
GATE_READ_JS = r"""
() => {
  const SKIP = 'script, style, template, noscript, select, option, datalist';
  const out = { visible: document.body ? document.body.innerText : '', hidden: [], refuse: null };
  if (!document.body) return out;
  const said = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const shownEl = (el) => (el.checkVisibility ? el.checkVisibility({ checkVisibilityCSS: true })
    : el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden');
  const range = document.createRange();
  const roots = [document.body];
  let elements = 0;
  while (roots.length && !out.refuse) {
    const root = roots.pop();
    const inShadow = root instanceof ShadowRoot;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n && !out.refuse; n = walker.nextNode()) {
      if (n.nodeType === Node.TEXT_NODE) {
        const parent = n.parentElement;
        if (!parent || parent.closest(SKIP)) continue;
        const t = said(n.textContent);
        if (!t) continue;
        range.selectNodeContents(n);
        // (drawn: boxed, its element shown, not in a content-visibility: hidden box, and in its element's drawn words)
        const drawn = range.getClientRects().length > 0 && shownEl(parent)
          && getComputedStyle(parent).contentVisibility !== 'hidden' && said(parent.innerText).includes(t);
        if (inShadow || !drawn || parent.closest('svg')) out.hidden.push(t);
        continue;
      }
      const el = n;
      if (++elements > 20000) { out.refuse = 'too many elements'; break; }
      const tag = el.tagName.toLowerCase();
      if (tag === 'canvas') out.refuse = 'a canvas';
      else if (tag === 'iframe' || tag === 'frame' || tag === 'object' || tag === 'embed') out.refuse = 'a frame';
      if (out.refuse) break;
      if (el.shadowRoot) roots.push(el.shadowRoot);
      else if (tag.includes('-')) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) { out.refuse = 'a closed shadow root'; break; }
      }
      for (const pseudo of ['::before', '::after']) {
        const content = getComputedStyle(el, pseudo).content;
        if (content && !/^(none|normal)$/.test(content) && /[A-Za-z]{2,}/.test(content.replace(/^(?:counter|attr|url)\(.*\)$/, ''))) {
          out.refuse = 'words in ' + pseudo; break;
        }
      }
      if (out.refuse) break;
      if (tag === 'textarea' && said(el.value)) out.hidden.push(said(el.value));
      if (tag === 'input' && /^(|text|search|email|url|tel)$/i.test(el.getAttribute('type') || '')
          && (el.readOnly || el.disabled) && said(el.value)) out.hidden.push(said(el.value));
      for (const attr of ['title', 'alt', 'aria-label']) {
        const t = said(el.getAttribute(attr));
        if (t.split(' ').length >= 4) out.hidden.push(t);
      }
    }
  }
  out.hidden = out.hidden.join('\n');
  return out;
}
"""

# Numbered entry headings ("Work Experience 2"), how many of those have no boxes drawn yet, and
# the Add buttons that create more.
ENTRIES_JS = r"""
(arg) => {
  // [kind pattern, the frame's id prefix ("f2-" in a frame, "" on the page): an Add button's id says its frame, as every
  // other id the form reader gives does (the review of 0.3.186)]
  const [kindPattern, prefix] = Array.isArray(arg) ? arg : [arg, ''];
  const kind = new RegExp(kindPattern, 'i');
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const txt = (el) => clean(el ? (el.innerText || el.textContent || '') : '');
  const visible = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const numbered = Array.from(document.querySelectorAll('h1, h2, h3, h4, h5, h6, legend, [role="heading"]'))
    .filter((h) => visible(h) && kind.test(txt(h)) && /\b\d+\s*$/.test(txt(h)));
  // and blocks with no heading that a form region's aria-label numbers (UKG Pro's "Work Experience 2": Desert
  // Financial's, run 55, where a block was added on every pass), each once
  const key = (t) => t.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();  // ("WORK EXPERIENCE #2" is "Work Experience 2")
  const named = new Set(numbered.map((h) => key(txt(h))));
  const regions = Array.from(document.querySelectorAll('[role="form"][aria-label]')).filter((r) => {
    const t = clean(r.getAttribute('aria-label'));
    return visible(r) && kind.test(t) && /\b\d+\s*$/.test(t) && !named.has(key(t)) && !numbered.some((h) => r.contains(h));
  });
  // and UKG Pro's saved rows, each with its own pencil "Edit Experience Item 1" and no box (Desert Financial's, Nikon's
  // and UCT's, run 56: the resume's job not counted, one added and asked of the person, and the job in twice): each
  // number once, its row shown (an open row's own Edit is hidden while its editor is), and none a region counted above
  const items = new Set();
  for (const b of document.querySelectorAll('button, [role="button"]')) {
    const t = clean(b.getAttribute('aria-label') || txt(b));
    const m = /^edit\b(.*)\bitem\s*(\d+)$/i.exec(t);
    const row = b.closest('li') || b;
    if (!m || !kind.test(m[1]) || /volunteer|military/i.test(m[1]) || !(visible(b) || visible(row))
        || regions.some((r) => r.contains(b))) continue;  // (a volunteer or military row is no job's)
    items.add(m[2]);
  }
  const entries = numbered.length + regions.length + items.size;
  // Workday draws a new block's heading first and its boxes a moment later: a block's boxes are
  // the ones after its heading, before the next heading
  const follows = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
  const marks = Array.from(document.querySelectorAll('h1, h2, h3, h4, h5, h6, [role="heading"]')).filter(visible);
  const boxes = Array.from(document.querySelectorAll('input:not([type="hidden"]), select, textarea, [role="combobox"], '
    + 'button[aria-haspopup="listbox"]')).filter(visible);
  const empty = numbered.filter((h) => {
    const next = marks.find((m) => m !== h && !h.contains(m) && follows(h, m));
    return !boxes.some((b) => follows(h, b) && !(next && follows(next, b)));
  }).length + regions.filter((r) => !boxes.some((b) => r.contains(b))).length;
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
      if (!/^[\w.:-]{1,80}$/.test(b.getAttribute('data-ja-id') || '')) {
        window.__jaCounter = (window.__jaCounter || 0) + 1;
        b.setAttribute('data-ja-id', prefix + 'add' + window.__jaCounter);
      }
      buttons.push({ id: b.getAttribute('data-ja-id'), text: txt(b) });
    }
  }
  return { entries, empty, buttons, boxes: boxes.length };
}
"""

# Before an Add is pressed (add_entries, in browser_blocks.py): the numbered block headings of its kind on the page
# now are marked as had, and the first new one drawn after the press is marked as the desk's own (data-ja-added). The
# account's saved block drawn late beside it (Hitachi High-Tech America's Workday, run 92) is never taken for it: only
# a block so marked is ever deleted again (pipeline._drop_added_extra). A list drawn again from scratch loses the
# marks: then nothing is.
ADD_WATCH_JS = r"""
(kindPattern) => {
  const kind = new RegExp(kindPattern, 'i');
  const SEL = 'h1, h2, h3, h4, h5, h6, legend, [role="heading"]';
  const txt = (el) => (el.textContent || '').replace(/\s+/g, ' ').trim();
  const numbered = (el) => el.matches(SEL) && kind.test(txt(el)) && /\b\d+\s*$/.test(txt(el));
  document.querySelectorAll(SEL).forEach((h) => { if (numbered(h)) h.setAttribute('data-ja-had', '1'); });
  if (window.__jaAddWatch) window.__jaAddWatch.disconnect();
  const watch = new MutationObserver((records) => {
    for (const r of records) {
      for (const n of r.addedNodes) {
        if (n.nodeType !== 1) continue;
        const drawn = [n, ...n.querySelectorAll(SEL)].find((h) => numbered(h) && !h.hasAttribute('data-ja-had'));
        if (drawn) {
          drawn.setAttribute('data-ja-added', '1');
          watch.disconnect();
          window.__jaAddWatch = null;
          return;
        }
      }
    }
  });
  watch.observe(document.body, { childList: true, subtree: true });
  window.__jaAddWatch = watch;
}
"""

# The watch above stopped (add_entries done), and the headings of the blocks so marked as they read now.
ADD_UNWATCH_JS = r"""
() => { if (window.__jaAddWatch) { window.__jaAddWatch.disconnect(); window.__jaAddWatch = null; } }
"""
ADDED_BLOCKS_JS = r"""
() => [...document.querySelectorAll('[data-ja-added]')].filter((h) => h.getClientRects().length > 0)
  .map((h) => (h.textContent || '').replace(/\s+/g, ' ').trim())
"""

# Facts click() needs about an element before deciding whether it may press it.
ELEMENT_INFO_JS = r"""
(el) => {
  /*SIGN_IN_FORM*/
  const BOX = '[id*="cookie" i], [class*="cookie" i], [aria-label*="cookie" i], [id*="consent" i], '
    + '[class*="consent" i], [id*="onetrust" i], [class*="onetrust" i], [id*="cybot" i], [id*="gdpr" i], [id*="truste" i]';
  const names = (n) => `${n.id || ''} ${typeof n.className === 'string' ? n.className : ''} ${n.getAttribute('aria-label') || ''}`;
  const named = (n) => /cookie|onetrust|cybot|truste|gdpr/i.test(names(n));
  const words = (n) => (n.innerText || n.textContent || n.value || n.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
  const under = el.getAttribute('role') === 'button' && !(el.innerText || el.textContent || '').trim() && el.nextElementSibling;
  const covers = under && under.matches('button[aria-hidden="true"]') && (under.innerText || under.textContent || '').trim() ? under : null;
  const page = (n) => n === document.body || n === document.documentElement;
  // the whole banner: the outermost cookie/consent box around it (OneTrust's accept button is
  // a box of its own by its id, inside the banner holding its "Reject All")
  // (or a banner known by its words alone: a dialog, or a box fixed to the page, named nothing, whose opening words
  // speak of cookies, short, with no box to fill in: the review of Oct 15, narrowed by its review)
  const spoken = (n) => {
    const t = (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim();
    return t.length < 800 && /cookie/i.test(t.slice(0, 40))
      && !n.querySelector('input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"]), select, textarea');
  };
  const spokenBox = (n) => {
    for (; n && !page(n); n = n.parentElement) {
      const p = getComputedStyle(n).position;
      if ((p === 'fixed' || p === 'sticky' || n.matches('[role="dialog"], [role="alertdialog"], dialog[open]')) && spoken(n)) return n;
    }
    return null;
  };
  const near = el.closest(BOX) || spokenBox(el);
  let banner = null;
  for (let n = near; n && !page(n); n = n.parentElement && n.parentElement.closest(BOX)) banner = n;
  // a "consent" box only when it speaks of cookies, not an application's own consent step
  const cookie = !!banner && (named(near) || /cookie/i.test(near.innerText || '') || named(banner));
  const shown = (n) => n.getClientRects().length > 0 && getComputedStyle(n).visibility !== 'hidden';
  const top = cookie ? document.elementFromPoint(innerWidth / 2, innerHeight / 2) : null;
  const over = top && top.closest(BOX);
  return {
    // (Workday's cover over a hidden button reads as the button it covers, as EXTRACT_JS reads it)
    label: (covers ? words(covers)
      : [el.innerText || el.textContent || el.value || '', el.getAttribute('aria-label') || ''].join(' ')).replace(/\s+/g, ' ').trim(),
    // its words as the page script reads them: an icon-only button's are its aria-label
    text: covers ? words(covers) : words(el),
    formSubmit: el.type === 'submit' && !!formOf(el),
    // in a cookie banner (OneTrust, Cookiebot, TrustArc and the like)
    cookie,
    // the banner's buttons (is there a way to decline?), and whether it covers the page's middle
    cookieButtons: cookie ? [...banner.querySelectorAll('button, [role="button"], input[type="submit"], input[type="button"], a[href]')]
      .filter(shown).map(words).filter((t) => t && t.length <= 60).slice(0, 20) : [],
    cookieBlocking: !!top && (banner.contains(top) || (!!over && !page(over) && named(over))),
    // what its form has to fill in, where it can be seen
    formFields: formOf(el) ? [...formOf(el).elements].filter((e) => /^(INPUT|SELECT|TEXTAREA)$/.test(e.tagName)
      && !/^(hidden|submit|button|image|reset)$/i.test(e.type || '') && e.getClientRects().length > 0).length : 0,
    // a sign-in form's own button, whatever it reads ("Submit"): it sends the sign-in, nothing more
    signInForm: signInForm(el),
  };
}
""".replace("/*SIGN_IN_FORM*/", SIGN_IN_FORM_JS)

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

# The resume's file boxes, each with the file sent into it (by the desk, or one that reads a value) and the hidden
# fields beside it that the page keeps the upload in (EMD's Phenom: #isResume, #resumeBucketId, #resumeFileSize,
# #resumeName, #resumePath). The body of the scripts below: `uploads`, and `filled` for a hidden field with a value
_RESUME_UPLOADS = r"""
  const named = /resume|(^|[_-])cv([_-]|$)/i;
  // (only the fields an upload is kept in: a save-for-later's "resumeToken" isn't one, the review of 0.3.136's)
  const kept_in = /resume.?(?:path|name|file|size|bucket|relative|url|id)|^isresume$/i;
  const hidden = Array.from(document.querySelectorAll('input[type="hidden"]'))
    .filter((el) => kept_in.test(el.id || '') || kept_in.test(el.name || ''));
  // (the resume's own file box, and the hidden fields beside it: its form's, or within a few levels of it; not a
  // cover letter's box, nor a "resumeToken" of a save-for-later elsewhere on the page: the review of note 28)
  const near = (box, el) => {
    if (box.form) return box.form === el.form;
    for (let n = box.parentElement, d = 0; n && d < 6; n = n.parentElement, d++) if (n.contains(el)) return true;
    // (a box outside every form, on a page with one: that form's are its fields, EMD's as it is, run 49)
    return !!el.form && document.forms.length === 1;
  };
  // a box that names nothing itself: the words of its own block ("Your resume/CV is required", its "Upload
  // Resume/CV": EMD's, run 49), the nearest that says anything, and only a short one
  const block = (box) => {
    for (let n = box.parentElement, d = 0; n && d < 4; n = n.parentElement, d++) {
      const t = (n.innerText || n.textContent || '').trim();
      if (t.length > 400) break;
      if (t) return t;
    }
    return '';
  };
  const filled = (el) => !!(el.value || '').trim();
  const uploads = [];
  for (const box of document.querySelectorAll('input[type="file"]')) {
    const file = box.__jaUploaded ? String(box.__jaUploaded) : (box.files && box.files.length ? box.files[0].name : '');
    const said = [box.id, box.name, box.getAttribute('aria-label') || '',
                  ...Array.from(box.labels || []).map((l) => l.textContent || '')].join(' ').trim();
    const words = said || block(box);
    if (!file || !named.test(words) || !said && /cover letter|transcript|other documents?|additional/i.test(words)) continue;
    uploads.push({box, file, kept: hidden.filter((el) => near(box, el))});
  }
"""

# A resume the page shows as uploaded (the file's name, a Delete link) though the hidden fields it keeps the upload
# in stayed empty (EMD's Phenom, issue #112, note 28: #isResume, #resumeBucketId, #resumeFileSize, #resumeName and
# #resumePath all empty, so Next did nothing and said nothing). Two or more hidden fields named for the resume, every
# one empty, and a file sent into a file box of the page (by the desk, or one that reads a value): the file's name.
UPLOAD_NOT_KEPT_JS = "() => {" + _RESUME_UPLOADS + r"""
  const unkept = uploads.find((u) => u.kept.length >= 2 && !u.kept.some(filled));
  return unkept ? unkept.file : '';
}
"""

# Whether the file `name` is in a resume box and kept by the page (UPLOAD_NOT_KEPT_JS's fields beside it have a
# value): "kept", "not kept", or "" when no resume box holds it
UPLOAD_STATE_JS = "(name) => {" + _RESUME_UPLOADS + r"""
  const mine = uploads.filter((u) => u.file === name && u.kept.length >= 2);
  if (!mine.length) return '';
  return mine.some((u) => u.kept.some(filled)) ? 'kept' : 'not kept';
}
"""

# The Delete beside a file the page shows as uploaded but never kept (UPLOAD_NOT_KEPT_JS: EMD's Phenom, runs 53 to 64),
# for the file to go up again. Only a file the desk sent itself (a box it gave the file to: never one the person
# chose), shown once (one row with its name, within the form holding the empty fields the upload is kept in, or
# around the resume's box), and
# the one control in that row (its name, its Download, its Delete; never further up than a row with a file box or a
# form in it) that says it deletes or removes, and nothing else: shown, and only deleting (never a form's submit
# button, a box, or a link to another page; the review of 0.3.153). Marked data-ja-unkept; true when there was one
UNKEPT_DELETE_JS = "(name) => {" + _RESUME_UPLOADS + r"""
  document.querySelectorAll('[data-ja-unkept]').forEach((el) => el.removeAttribute('data-ja-unkept'));
  const mine = uploads.find((u) => u.file === name && u.box.__jaUploaded === name && u.kept.length >= 2
                                   && !u.kept.some(filled));
  if (!mine) return false;
  // (within the form holding those fields, or the resume box's own part of the page: the blocks around it up to
  // one with a form in it. EMD's row with the file's name and Delete is beside its box, above the form, run 64)
  const forms = [...new Set(mine.kept.map((el) => el.form).filter(Boolean))];
  let area = null;
  for (let n = mine.box.parentElement, d = 0; n && n !== document.body && d < 6 && !n.querySelector('form');
       n = n.parentElement, d++) area = n;
  const within = (el) => !forms.length || forms.some((f) => f.contains(el)) || !!area && area.contains(el);
  const shown = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const txt = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const says = (el) => [el.getAttribute('aria-label') || '', el.getAttribute('title') || '', txt(el)]
    .some((t) => /^(?:delete|remove)(?: file| (?:the )?(?:resume|cv|document))?$/i.test(t.trim()));
  const only = (el) => el.tagName === 'A' ? /^(?:#.*|javascript:.*)?$/i.test((el.getAttribute('href') || '').trim())
    : el.tagName === 'BUTTON' ? !el.form || el.type === 'button' : el.tagName !== 'INPUT';
  const rows = [...document.querySelectorAll('span, div, p, a, li, td')]
    .filter((el) => el.childElementCount === 0 && txt(el) === name && shown(el) && within(el));
  if (rows.length !== 1) return false;  // (the name twice, a cover letter's say: which is the resume's isn't sure)
  // (a row with another file's name in it is a list of files: a Delete there may be the other file's)
  const another = (row) => [...row.querySelectorAll('*')].some((el) => el.childElementCount === 0 && el !== rows[0]
    && /\.[a-z][a-z0-9]{1,4}$/i.test(txt(el)) && txt(el) !== name);
  for (let row = rows[0].parentElement, d = 0; row && d < 3; row = row.parentElement, d++) {
    if (row.matches('form') || row.querySelector('form, input[type="file"]') || txt(row).length > 300
        || another(row)) break;
    const controls = [...row.querySelectorAll('a, button, [role="button"]')].filter((el) => shown(el) && says(el));
    if (controls.length > 1 || controls.length && !only(controls[0])) return false;
    if (controls.length) {
      controls[0].setAttribute('data-ja-unkept', '1');
      return true;
    }
  }
  return false;
}
"""

# After that Delete: whether the page still shows the file's name anywhere (a site that asks first, or takes a
# moment); once it doesn't, the boxes the desk gave it to forget they were (UNKEPT_FORGET_JS), so it goes up again.
# UNKEPT_PRESSED_SHOWN_JS: whether the Delete pressed is still there (the name shown elsewhere too: a note of it)
UNKEPT_SHOWN_JS = "(name) => (document.body.innerText || '').includes(name)"
UNKEPT_PRESSED_SHOWN_JS = """() => {
  const el = document.querySelector('[data-ja-unkept="1"]');
  return !!el && el.isConnected && el.getClientRects().length > 0;
}"""
UNKEPT_FORGET_JS = r"""
(name) => {
  document.querySelectorAll('input[type="file"]').forEach((box) => {
    if (box.__jaUploaded === name) { delete box.__jaUploaded; box.value = ''; }
  });
}
"""

# What a resume (or cover letter) box shows, as Workday lists it: the box's own part of the page (as the form reader
# reads what it shows, uploadedNear: the wrappers around it up to one holding another form control or another file box,
# and the first holding Workday's file items), and in it the items Workday lists each file in (data-automation-id
# "file-upload-item", its name in "file-upload-item-name", "Successfully Uploaded!"). Only Workday's: that's where an
# account's last resume is kept (NXP's, the weekly probe of Oct 2026). Another site's list of stored files may be the
# person's own file library, where a Delete takes the file off their account: never touched (Applier._replace_kept).
# The body of the scripts below, run on the box (`el`)
_KEPT_ITEMS = r"""
  const shown = (x) => x.getClientRects().length > 0 && getComputedStyle(x).visibility !== 'hidden';
  const txt = (x) => (x.innerText || x.textContent || '').replace(/\s+/g, ' ').trim();
  const ITEM = '[data-automation-id="file-upload-item"]';
  const NAME = '[data-automation-id="file-upload-item-name"]';
  const UPLOADED = /\bsuccessfully uploaded\b|\bupload(?:ed)? (?:completed? )?successfully\b|\bfile upload completed\b/i;
  let area = null;
  for (let n = el.parentElement, d = 0; n && n !== document.body && d < 6; n = n.parentElement, d++) {
    if ([...n.querySelectorAll('input, select, textarea')].some((x) => x !== el && x.type !== 'hidden'
        && (x.type === 'file' || x.getClientRects().length > 0))) break;
    area = n;
    if (n.querySelector(ITEM)) break;
  }
  const items = area ? [...area.querySelectorAll(ITEM)].filter(shown) : [];
  // (a file's name: its item's own name, within three levels of it)
  const nameOf = (item) => {
    const names = [...item.querySelectorAll(NAME)].filter((n) => {
      let d = 0;
      for (let p = n.parentElement; p && p !== item; p = p.parentElement) d++;
      return d < 3;
    });
    return names.length === 1 ? txt(names[0]) : names.length ? null : '';
  };
"""

# An upload box showing a file, before the file tailored for this job goes in place of one kept from before
# (Applier._replace_kept): "mine" where the desk sent a file into this box itself (its own upload, never one kept from
# before, whatever its name), "other" where the files it shows aren't in Workday's list (_KEPT_ITEMS: never touched), ""
# otherwise. Never "there" by a name alone: every tailored resume is named <First>_<Last>_Resume.pdf, so a file kept
# from the last application at the same employer has the name of this job's tailored one
KEPT_STATE_JS = "(el) => {" + _KEPT_ITEMS + r"""
  if (el.__jaUploaded) return 'mine';
  return items.length ? '' : 'other';
}
"""

# The Delete (or Remove) of the file a box shows as kept from before, run on the box, for the file tailored for this job
# to go in its place: in Workday's list alone (_KEPT_ITEMS), its own item: the one with the file's name (`name`), or,
# where the box shows no name (''), the box's one item, which says it's uploaded. Only as 0.3.153's Delete is: the item
# its own row (no box, no form and no other file's name in it, at most 300 characters), and in it the one control that
# says it deletes or removes (never one naming another file, "Delete other.pdf"), shown, and only deleting (never a
# form's submit button, a reset, or a link to another page). Never a control outside it: a saved block's Delete, a
# section's Remove, another file's. Marks it data-ja-kept-delete; true when there was one
KEPT_DELETE_JS = "(el, name) => {" + _KEPT_ITEMS + r"""
  document.querySelectorAll('[data-ja-kept-delete]').forEach((x) => x.removeAttribute('data-ja-kept-delete'));
  const said = (x) => [x.getAttribute('aria-label') || '', x.getAttribute('title') || '', txt(x)]
    .map((t) => t.trim()).filter(Boolean);
  const DELETES = /^(?:delete|remove)(?: (?:the )?(?:file|resume|cv|document|cover letter|attachment))?$/i;
  const NAMING = /^(?:delete|remove)(?: file)?:?\s+(.+)$/i;
  const named = (t) => { const m = !DELETES.test(t) && NAMING.exec(t); return m ? m[1].trim() : ''; };
  const deletes = (x) => {
    const words = said(x);
    if (words.some((t) => named(t) && named(t) !== name)) return false;  // (another file's, or another thing's)
    return words.some((t) => DELETES.test(t) || !!name && named(t) === name);
  };
  const only = (x) => x.tagName === 'A' ? /^(?:#.*|javascript:.*)?$/i.test((x.getAttribute('href') || '').trim())
    : x.tagName === 'BUTTON' ? !x.form || x.type === 'button' : x.tagName !== 'INPUT';
  const rows = items.filter((item) => nameOf(item) === name);
  // (no name: the box's one item, saying it's uploaded; a name: the one item with it)
  if (rows.length !== 1 || !name && (items.length !== 1 || !UPLOADED.test(txt(rows[0])))) return false;
  const row = rows[0];
  if (txt(row).length > 300 || row.querySelector('input, select, textarea, form')) return false;
  const found = [...row.querySelectorAll('a, button, [role="button"]')].filter((x) => shown(x) && deletes(x));
  if (found.length !== 1 || !only(found[0])) return false;
  found[0].setAttribute('data-ja-kept-delete', '1');
  return true;
}
"""

# After that Delete, in the box's frame: how many of Workday's file items show that file (its name, or with none, no
# name and "uploaded"). The file is gone once there are fewer than before the press: never by its Delete alone going (a
# page that draws its list again), nor by its name gone from the page (a note may name it)
KEPT_ROWS_JS = r"""(name) => {
  const shown = (x) => x.getClientRects().length > 0 && getComputedStyle(x).visibility !== 'hidden';
  const txt = (x) => (x.innerText || x.textContent || '').replace(/\s+/g, ' ').trim();
  const UPLOADED = /\bsuccessfully uploaded\b|\bupload(?:ed)? (?:completed? )?successfully\b|\bfile upload completed\b/i;
  return [...document.querySelectorAll('[data-automation-id="file-upload-item"]')].filter(shown).filter((item) => {
    const names = [...item.querySelectorAll('[data-automation-id="file-upload-item-name"]')].map(txt);
    return name ? names.includes(name) : !names.some(Boolean) && UPLOADED.test(txt(item));
  }).length;
}"""

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
    // (drawn over it: more than a joined border's pixel or a rounding)
    const covers = (b, r) => overlaps(b, r) && Math.min(b.bottom, r.bottom) - Math.max(b.top, r.top) > 4;
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
      // (but never one it's drawn over, while it's clear of this one: Workday's prompt, once a group's own list opens
      // under its header, covers the Country and Prefix lists below it, nearer the Country list's edge than its own
      // box, live probe 47)
      if (others.some((r) => overlaps(b, r) && !(covers(b, r) && !covers(b, mine)) && gapTo(b, r) < gapTo(b, mine))) continue;
      if (gap < 120 && gap < bestGap) { bestGap = gap; best = os; }
    }
    opts = best;
  }
  for (const o of document.querySelectorAll('[data-ja-opt]')) o.removeAttribute('data-ja-opt');
  // (never a list's word that it's empty: Workday's "No Items.", which the desk offered HonorHealth's Skills as a
  // choice, probe 50)
  const empty = /^(?:no (?:items|results|matches|options|data)(?: found| available)?|nothing found)\.?$/i;
  const out = [];
  for (const o of opts) {
    const t = text(o);
    if (t && empty.test(t)) el.setAttribute('data-ja-empty', '1');  // (a list to pick from, that said it's empty)
    if (t && !empty.test(t) && !out.includes(t)) { o.setAttribute('data-ja-opt', t); out.push(t); }
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
  // (and DataDome's frame, its challenge or its block: Wynn's and Boyd's SmartRecruiters postings, live run 144)
  if (!/hcaptcha\.com.*challenge|recaptcha\/(api2|enterprise)\/bframe|challenges\.cloudflare\.com|captcha-delivery\.com/i.test(src)
      && !/(hcaptcha|recaptcha|captcha).*challenge|challenge.*(hcaptcha|recaptcha|captcha)|datadome/i.test(title)) return false;
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


# A jQuery UI multiselect's button ("<id>-button" over a hidden <select multiple>: BrassRing's "Please check each
# applicable box", Edward Jones' run 87), BrassRing's search box standing for one (run 88), or Oracle's cx-multi-select
# (run 144): the choices it has ticked (its select's, and those its widget lists beside it; Oracle's pills), or null for
# any other control
TWIN_TICKED_JS = r"""
(el) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  // what a jQuery UI multiselect has ticked: its hidden select's choices, and those its widget lists under its button
  // ("<id>-button_selection-list"), each read as the choice it names (a widget may keep only its own list: the review)
  const multiTicked = (button, select) => {
    const names = Array.from(select.options).map((o) => clean(o.text)).filter(Boolean);
    const ticked = Array.from(select.selectedOptions).map((o) => clean(o.text)).filter(Boolean);
    const list = document.getElementById(button.id + '_selection-list');
    for (const li of list ? list.querySelectorAll('li') : []) {
      const said = clean(li.textContent);
      const named = names.filter((n) => said === n || said.includes(n)).sort((a, b) => b.length - a.length)[0];
      if (named && !ticked.includes(named)) ticked.push(named);
    }
    return ticked;
  };
  // and BrassRing's search box for several choices (an input naming its select in its "select" attribute): the pills
  // after its box, each read without its Remove button
  const searchTicked = (input, select) => {
    const ticked = Array.from(select.selectedOptions).filter((o) => o.value !== '').map((o) => clean(o.text)).filter(Boolean);
    let list = input.id ? document.getElementById(input.id + '_selection-list') : null;
    for (let s = input.parentElement && input.parentElement.nextElementSibling; s && !list; s = s.nextElementSibling) {
      if (s.classList.contains('selectionList')) list = s;
    }
    for (const li of list ? list.querySelectorAll('li') : []) {
      const copy = li.cloneNode(true);
      copy.querySelectorAll('button, .screenReaderText').forEach((x) => x.remove());
      const said = clean(copy.textContent);
      if (said && !ticked.includes(said)) ticked.push(said);
    }
    return ticked;
  };
  // and Oracle's cx-multi-select: the pills under its box (MSTS' run 144)
  const cx = el.tagName === 'INPUT' ? el.closest('.cx-multi-select-container') : null;
  if (cx && cx.parentElement) {
    return Array.from(cx.parentElement.querySelectorAll(':scope > .cx-multi-select-pill__values '
      + '.cx-multi-select-pill__value-text')).map((p) => clean(p.textContent)).filter(Boolean);
  }
  const stood = el.tagName === 'INPUT' && el.getAttribute('select') ? document.getElementById(el.getAttribute('select')) : null;
  if (stood && stood.tagName === 'SELECT') return stood.multiple ? searchTicked(el, stood) : null;
  const twin = el.id && el.id.endsWith('-button') ? document.getElementById(el.id.slice(0, -'-button'.length)) : null;
  if (!twin || twin.tagName !== 'SELECT' || !twin.multiple) return null;
  return multiTicked(el, twin);
}
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


# Is a dropdown menu open (one with options showing)? Never a list that's part of a dialog over the page unless a
# box names it (aria-controls or aria-owns: an input, a combobox, or a button that opens a list), nor one in a notice
# OPEN_DIALOGS_JS reads: a notice's list of languages, or a cookie center's list of vendors, is no menu to close with
# an Escape (the review of the fix for issue #112, note 41)
OPEN_MENU_JS = r"""
() => {
  /*OPEN_DIALOGS*/
  const OPENS = 'input, [role="combobox"], button[aria-haspopup], [role="button"][aria-haspopup]';
  // (the dialog itself only by an input or a combobox whose pop-up it is: a cookie center's own button names it too)
  const named = (n, d) => !!n.id && [...document.querySelectorAll(
    `[aria-controls~="${CSS.escape(n.id)}"], [aria-owns~="${CSS.escape(n.id)}"]`)]
    .some((b) => !n.contains(b) && b.matches(n === d ? 'input, [role="combobox"]' : OPENS));
  let notices = null;
  const owned = (m) => {
    const d = m.closest('[aria-modal="true"], [role="dialog"], [role="alertdialog"], dialog');
    if (!d) return true;
    if (notices === null) notices = openDialogs().map((o) => o.el);
    if (notices.some((o) => o.contains(m))) return false;
    for (let n = m; n && n !== d.parentElement; n = n.parentElement) if (named(n, d)) return true;
    return false;
  };
  return [...document.querySelectorAll('[role="listbox"], [role="menu"], [role="grid"]')].some((m) =>
    m.tagName !== 'SELECT' && m.getClientRects().length > 0 && getComputedStyle(m).visibility !== 'hidden'
    && (m.getAttribute('role') === 'grid'
      // a grid is a menu only as a field's list (Oracle's lookups), not as a table on the page
      ? !!(m.id && document.querySelector(`[aria-controls~="${CSS.escape(m.id)}"]`)) && !!m.querySelector('[role="gridcell"]')
      : !!m.querySelector('[role="option"], [role="menuitem"]'))
    && owned(m));
}
""".replace("/*OPEN_DIALOGS*/", OPEN_DIALOGS_JS)


# A box's own pop-up drawn as a dialog over the page, the kind OPEN_DIALOGS_JS leaves out of the notices: shown, its
# calendar's days or its list's choices on show, no boxes or switches of its own, no cookie center's, and opened by a
# box outside it (one that names it or something in it, aria-controls or aria-owns; or one that says it has opened a
# dialog or a grid, aria-expanded with aria-haspopup; or, given its id, the box being filled, where its pop-up is a
# dialog or a grid: a combobox's list is no picker, the review's). Eightfold's date picker opens its
# calendar as one, modal: it keeps the focus and swallows the next box's click and keys (issue #112, note 41). None
# while a notice (or a cookie banner) is open over the page: that's answered, never closed here (the review's: a
# notice holding a hidden list of languages was closed by an Escape). How many are open; and first, with `mode`
# "blur", the focus taken off whatever has it; "inside", the focus moved into the last one, unless it's in one
# already; "opener", the focus put on the box that opened it
OPEN_PICKERS_JS = r"""
(arg) => {
  /*OPEN_DIALOGS*/
  const [mode, id] = Array.isArray(arg) ? arg : [arg, ''];
  const shown = (n) => n.getClientRects().length > 0 && getComputedStyle(n).visibility !== 'hidden';
  const BOX = 'input:not([type="hidden"]), select, textarea, [role="switch"], [role="checkbox"], [role="radio"], '
    + '[role="textbox"], [role="combobox"]';
  const COOKIE = /cookie|onetrust|cybot|truste|gdpr|consent|privacy/i;
  const drawn = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], dialog[open]')].filter((d) => shown(d)
    && [...d.querySelectorAll('[role="grid"] [role="gridcell"], [role="listbox"] [role="option"]')].some(shown)
    && ![...d.querySelectorAll(BOX)].some(shown)
    && !COOKIE.test(`${d.id} ${typeof d.className === 'string' ? d.className : ''} ${d.getAttribute('aria-label') || ''}`));
  if (!drawn.length || openDialogs().length) return 0;
  const own = id ? document.querySelector(`[data-ja-id="${CSS.escape(id)}"]`) : null;
  // (a pop-up that's a dialog or a grid: a combobox's list is a menu, _close_menus')
  const pops = (b) => /^(dialog|grid)$/.test(b.getAttribute('aria-haspopup') || '');
  const opens = (b, d) => {
    if (!b || d.contains(b) || !shown(b)) return false;
    const refs = `${b.getAttribute('aria-controls') || ''} ${b.getAttribute('aria-owns') || ''}`.split(/\s+/).filter(Boolean);
    if (refs.some((r) => { const t = document.getElementById(r); return !!t && (t === d || d.contains(t)); })) return true;
    return pops(b) && (b.getAttribute('aria-expanded') === 'true' || b === own);
  };
  const boxes = [...document.querySelectorAll('input, button, [role="combobox"]')];
  const openerOf = (d) => (opens(own, d) ? own : boxes.find((b) => opens(b, d)));
  const tied = drawn.filter((d) => !!openerOf(d));
  const open = tied.filter((d) => !tied.some((o) => o !== d && o.contains(d)));
  if (!open.length) return 0;
  const a = document.activeElement, d = open[open.length - 1];
  if (mode === 'blur' && a && a !== document.body) a.blur();
  if (mode === 'inside' && !(a && open.some((o) => o.contains(a)))) {
    const target = d.querySelector('[tabindex="0"], [aria-selected="true"] [tabindex], button:not([disabled])') || d;
    if (target === d && !d.hasAttribute('tabindex')) d.setAttribute('tabindex', '-1');
    target.focus();
  }
  if (mode === 'opener') openerOf(d).focus();
  return open.length;
}
""".replace("/*OPEN_DIALOGS*/", OPEN_DIALOGS_JS)


# A date picker's calendar, for the day `iso` ("2026-11-02"): the dialog or grid its box names (aria-controls), else
# the last calendar shown in a dialog that wasn't before the box was pressed (`mark`: the dialogs shown now marked as
# before, data-ja-cal-before, and nothing else done). Its days are read from their title or data attribute (rc-picker's,
# as Eightfold's has them: "2026-11-02") or from their words ("Monday 2 November 2026", "November 2, 2026"). The day
# shown is marked data-ja-day (its button, where it has one) and "day" returned; else, past the days shown, the
# calendar's next month's button (one its words say moves a month, never a form's "Next") is marked data-ja-day-nav and
# "next" returned ("back" for the month before); "none" without a calendar, "unread" with no day it can read,
# "stuck" with no button to move on, or only one that's disabled (the calendar's last month).
DAY_CELL_JS = r"""
([id, iso, mark]) => {
  for (const n of document.querySelectorAll('[data-ja-day], [data-ja-day-nav]')) {
    n.removeAttribute('data-ja-day'); n.removeAttribute('data-ja-day-nav');
  }
  const shown = (n) => n.getClientRects().length > 0 && getComputedStyle(n).visibility !== 'hidden';
  const DIALOGS = '[role="dialog"], [aria-modal="true"], dialog[open]';
  if (mark) {
    for (const d of document.querySelectorAll(DIALOGS)) d.toggleAttribute('data-ja-cal-before', shown(d));
    return '';
  }
  const box = document.querySelector(`[data-ja-id="${CSS.escape(id)}"]`);
  const ref = box && (box.getAttribute('aria-controls') || box.getAttribute('aria-owns'));
  let cal = ref ? document.getElementById(ref.split(/\s+/)[0]) : null;
  if (!cal || !shown(cal) || !cal.querySelector('[role="grid"], table')) {
    cal = [...document.querySelectorAll(DIALOGS)]
      .filter((d) => shown(d) && !d.hasAttribute('data-ja-cal-before') && d.querySelector('[role="grid"], table')).pop() || null;
  }
  if (!cal) return 'none';
  const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];
  const fromWords = (words) => {
    const w = (words || '').toLowerCase();
    const month = MONTHS.findIndex((m) => new RegExp(`\\b${m}[a-z]*\\b`).test(w));
    const year = /\b(\d{4})\b/.exec(w);
    const day = /(?:^|[^\d])(\d{1,2})(?![\d])/.exec(w.replace(/\b\d{4}\b/g, ' '));
    return month >= 0 && year && day ? `${year[1]}-${String(month + 1).padStart(2, '0')}-${day[1].padStart(2, '0')}` : '';
  };
  const dayOf = (c) => {
    for (const v of ['title', 'data-date', 'data-value', 'data-day', 'datetime']) {
      const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(c.getAttribute(v) || '');
      if (m) return m[0];
    }
    const named = [c, ...c.querySelectorAll('[aria-label], [title]')].map((n) => n.getAttribute('aria-label') || '');
    return named.map(fromWords).find(Boolean) || '';
  };
  const cells = [...cal.querySelectorAll('[role="gridcell"], td')].filter(shown);
  const days = cells.map((c) => [c, dayOf(c)]).filter(([, d]) => d);
  if (!days.length) return 'unread';
  const off = (c) => c.getAttribute('aria-disabled') === 'true' || /\bdisabled\b/i.test(c.className || '')
    || !!c.querySelector('[aria-disabled="true"], [disabled]');
  const hit = days.find(([c, d]) => d === iso && !off(c));
  if (hit) {
    const [c] = hit;
    (c.querySelector('[role="button"], button') || c).setAttribute('data-ja-day', iso);
    return 'day';
  }
  const later = days.every(([, d]) => d < iso), earlier = days.every(([, d]) => d > iso);
  if (!later && !earlier) return 'stuck';  // (the day shown, but not to be picked: one the calendar keeps out)
  const words = (b) => `${b.getAttribute('aria-label') || ''} ${b.getAttribute('title') || ''} ${b.getAttribute('data-testid') || ''} `
    + (typeof b.className === 'string' ? b.className.replace(/[-_]/g, ' ') : '');
  const buttons = [...cal.querySelectorAll('button, [role="button"]')].filter((b) => shown(b) && !b.closest('[role="gridcell"], td'));
  const way = later ? /\bnext\b/i : /\bprev(ious)?\b/i;
  const nav = buttons.find((b) => way.test(words(b)) && /\bmonth\b/i.test(words(b)));
  if (!nav || nav.disabled || nav.getAttribute('aria-disabled') === 'true') return 'stuck';
  nav.setAttribute('data-ja-day-nav', '1');
  return later ? 'next' : 'back';
}
"""


# A tab looked in on (BrowserSession.peek, every few seconds while a job waits on the person): a frame's form (not a
# CAPTCHA's own frame's), its words and any CAPTCHA in it, in one call, not three (the review of Oct 15)
PEEK_JS = ("([form, prefix]) => ({found: form ? (" + EXTRACT_JS.strip() + ")(prefix) : {}, text: ("
           + VISIBLE_TEXT_JS.strip() + ")(), challenge: !!(" + CHALLENGE_JS.strip() + ")()})")
