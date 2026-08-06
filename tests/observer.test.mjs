import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

import {
  createLedger,
  mergeObservation,
  validateObservation,
} from '../src/phase1/ledger.mjs';

const OBSERVER_SOURCE = fs.readFileSync(new URL('../src/phase1/observer.js', import.meta.url), 'utf8');

class TextNode {
  constructor(value, ownerDocument) {
    this.nodeType = 3;
    this.nodeValue = value;
    this.ownerDocument = ownerDocument;
  }

  get textContent() {
    return this.nodeValue;
  }
}

class Element {
  constructor(tagName, ownerDocument, text = '', attributes = {}) {
    this.nodeType = 1;
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = ownerDocument;
    this.parentElement = null;
    this.childNodes = [];
    this.children = [];
    this.shadowRoot = null;
    this._connected = false;
    this.attributes = new Map();
    this.disabled = false;
    this.readOnly = false;
    this.required = false;
    this.checked = undefined;
    this.multiple = false;
    this.options = [];
    this.files = [];
    this.validity = { valid: true, valueMissing: false, customError: false };
    this.validationMessage = '';
    for (const [name, value] of Object.entries(attributes)) this.setAttribute(name, value);
    if (text) this.append(new TextNode(text, ownerDocument));
  }

  append(...nodes) {
    for (const node of nodes) {
      node.parentElement = this;
      node.ownerDocument = this.ownerDocument;
      if (node.nodeType === 1) {
        node._connected = this._connected;
        this.children.push(node);
      }
      this.childNodes.push(node);
    }
    return nodes[nodes.length - 1] || null;
  }

  get isConnected() {
    return this._connected;
  }

  setAttribute(name, value) {
    this.attributes.set(String(name).toLowerCase(), String(value));
  }

  getAttribute(name) {
    const value = this.attributes.get(String(name).toLowerCase());
    return value === undefined ? null : value;
  }

  hasAttribute(name) {
    return this.attributes.has(String(name).toLowerCase());
  }

  get textContent() {
    return this.childNodes.map((child) => child.textContent || '').join('');
  }

  get innerText() {
    return this.textContent;
  }

  getRootNode() {
    return this.ownerDocument;
  }

  get labels() {
    if (this.tagName !== 'INPUT' || (this.getAttribute('type') !== 'checkbox' && this.getAttribute('type') !== 'radio')) {
      return undefined;
    }
    const id = this.getAttribute('id');
    const result = [];
    function collect(root) {
      if (root.nodeType !== 1) return;
      if (root.tagName === 'LABEL') {
        const labelFor = root.getAttribute('for');
        if (labelFor === id || (!labelFor && root.contains(this))) result.push(root);
      }
      for (const child of root.children) collect.call(this, child);
    }
    collect.call(this, this.ownerDocument.documentElement || this.ownerDocument);
    return result.length > 0 ? result : undefined;
  }

  contains(other) {
    let current = other;
    while (current) {
      if (current === this) return true;
      current = current.parentElement;
    }
    return false;
  }

  closest(selector) {
    let current = this.parentElement;
    while (current) {
      if (current.tagName === selector.toUpperCase()) return current;
      if (selector.startsWith('.') && current.getAttribute('class')?.split(/\s+/).includes(selector.slice(1))) return current;
      current = current.parentElement;
    }
    return null;
  }

  getBoundingClientRect() {
    return { width: 100, height: 20 };
  }

  getClientRects() {
    return [{ width: 100, height: 20 }];
  }
}

class Document {
  constructor() {
    this.nodeType = 9;
    this.title = 'Synthetic application';
    this.baseURI = 'https://example.test/app';
    this.location = { href: this.baseURI };
    this.defaultView = {
      getComputedStyle() {
        return { display: 'block', visibility: 'visible', opacity: '1' };
      },
    };
    this.documentElement = null;
  }

  createElement(tagName, _ignoredOwnerDocument = this, text = '', attributes = {}) {
    return new Element(tagName, this, text, attributes);
  }
}

function buildDocument() {
  const document = new Document();
  const html = document.createElement('html');
  html._connected = true;
  document.documentElement = html;
  const body = document.createElement('body');
  const form = document.createElement('form');
  const question = document.createElement('div', document, '', { class: 'ashby-application-form-field-entry' });
  const prompt = document.createElement('div', document, 'Are you authorized to work in this country?');
  const yesNo = document.createElement('div', document, '', { class: '_container_1svni_28 _yesno_1e3gg_148' });
  const yes = document.createElement('button', document, 'Yes', { type: 'button', 'aria-pressed': 'true' });
  const no = document.createElement('button', document, 'No', { type: 'button', 'aria-pressed': 'false' });
  const unrelatedYes = document.createElement('button', document, 'Yes', { type: 'button' });
  const submit = document.createElement('button', document, 'Submit Application', { id: 'submit-app', type: 'submit' });
  const locate = document.createElement('button', document, 'Locate me', { type: 'button' });
  yesNo.append(yes, no);
  question.append(prompt, yesNo);
  form.append(question, unrelatedYes, locate, submit);
  body.append(form);
  html.append(body);
  const markConnected = (element) => {
    element._connected = true;
    for (const child of element.children) markConnected(child);
  };
  markConnected(html);
  return { document, body, yes, no, locate, submit, unrelatedYes };
}

function observe(document, previousObservationId = null) {
  const window = { document, location: document.location };
  const sandbox = {
    window,
    document,
    __omp_phase1_previous_observation_id_v1: previousObservationId,
    URL,
  };
  return vm.runInNewContext(OBSERVER_SOURCE, sandbox);
}
test('normalizes paired Yes/No buttons into a stable radio field group', () => {
  const fixture = buildDocument();
  const first = observe(fixture.document);
  validateObservation(first);

  const choices = first.controls.filter((control) => control.role === 'radio');
  assert.equal(choices.length, 2);
  assert.equal(new Set(choices.map((control) => control.group_id)).size, 1);
  assert.deepEqual(new Set(choices.map((control) => control.label)), new Set(['Are you authorized to work in this country?']));
  assert.deepEqual(new Set(choices.map((control) => control.value)), new Set(['yes', 'no']));
  assert.equal(choices.find((control) => control.value === 'yes').checked, true);
  assert.equal(choices.find((control) => control.value === 'no').checked, false);
  assert.ok(choices.every((control) => control.candidate.class === 'field'));
  assert.ok(choices.every((control) => control.locator.strategy === 'role'));
  assert.ok(first.controls.some((control) => control.label === 'Yes' && control.candidate.class === 'unknown'));
  assert.equal(first.controls.find((control) => control.label === 'Locate me').candidate.class, 'non_final_navigation');
  assert.equal(first.controls.find((control) => control.label === 'Submit Application').candidate.class, 'final_candidate');

  const firstByValue = new Map(choices.map((control) => [control.value, control]));
  let ledger = createLedger(first);
  assert.equal(ledger.fields.filter((field) => field.group_id === choices[0].group_id).length, 2);

  fixture.yes.setAttribute('aria-pressed', 'false');
  fixture.no.setAttribute('aria-pressed', 'true');
  const second = observe(fixture.document, first.observation_id);
  validateObservation(second);
  const secondChoices = second.controls.filter((control) => control.role === 'radio');
  const secondByValue = new Map(secondChoices.map((control) => [control.value, control]));
  assert.equal(second.previous_observation_id, first.observation_id);
  assert.equal(secondChoices[0].label, choices[0].label);
  assert.equal(secondChoices[0].group_id, choices[0].group_id);
  assert.deepEqual(
    [...secondByValue.keys()].sort(),
    [...firstByValue.keys()].sort(),
  );
  for (const value of firstByValue.keys()) {
    assert.equal(secondByValue.get(value).stable_id, firstByValue.get(value).stable_id);
    assert.notEqual(secondByValue.get(value).ref, firstByValue.get(value).ref);
  }
  assert.equal(secondByValue.get('yes').checked, false);
  assert.equal(secondByValue.get('no').checked, true);

  ledger = mergeObservation(ledger, second);
  assert.equal(ledger.fields.filter((field) => field.group_id === choices[0].group_id).length, 2);
  assert.ok(ledger.fields.every((field) => field.label !== 'Yes' || field.group_id === null));
});

test('ignores oversized hidden custom-select option catalogs while retaining a hidden selection', () => {
  const fixture = buildDocument();
  const combobox = fixture.document.createElement('div', fixture.document, '', {
    role: 'combobox',
    'aria-controls': 'country-options',
    'aria-owns': 'country-options',
  });
  const listbox = fixture.document.createElement('ul', fixture.document, '', {
    id: 'country-options',
    role: 'listbox',
  });
  for (let index = 0; index < 246; index += 1) {
    const option = fixture.document.createElement('li', fixture.document, `Country ${index}`, {
      role: 'option',
      'data-value': `country-${index}`,
      'aria-selected': index === 245 ? 'true' : 'false',
    });
    option.getBoundingClientRect = () => ({ width: 0, height: 0 });
    option.getClientRects = () => [];
    listbox.append(option);
  }
  fixture.body.append(combobox, listbox);
  const markConnected = (element) => {
    element._connected = true;
    for (const child of element.children) markConnected(child);
  };
  markConnected(combobox);
  markConnected(listbox);

  const observation = observe(fixture.document);
  validateObservation(observation);
  const control = observation.controls.find((candidate) => candidate.role === 'combobox');
  assert.ok(control);
  assert.equal(control.options.length, 1);
  assert.equal(control.options[0].selected, true);
  assert.equal(control.options[0].value, 'country-245');
});

test('observes a bounded 246-option visible custom-select catalog', () => {
  const fixture = buildDocument();
  const combobox = fixture.document.createElement('div', fixture.document, '', {
    role: 'combobox',
    'aria-controls': 'country-options',
  });
  const listbox = fixture.document.createElement('ul', fixture.document, '', {
    id: 'country-options',
    role: 'listbox',
  });
  for (let index = 0; index < 246; index += 1) {
    listbox.append(fixture.document.createElement('li', fixture.document, `Country ${index}`, {
      role: 'option',
      ...(index === 0 ? {} : { 'data-value': `country-${index}` }),
      'aria-selected': index === 0 ? 'true' : 'false',
    }));
  }
  fixture.body.append(combobox, listbox);
  const markConnected = (element) => {
    element._connected = true;
    for (const child of element.children) markConnected(child);
  };
  markConnected(combobox);
  markConnected(listbox);

  const observation = observe(fixture.document);
  validateObservation(observation);
  const control = observation.controls.find((candidate) => candidate.role === 'combobox');
  assert.ok(control);
  assert.equal(control.options.length, 246);
  assert.equal(control.options[0].value, 'Country 0');
  assert.equal(control.options[0].selected, true);
});

test('observes opacity-zero native checkboxes and radios with visible labels', () => {
  const document = new Document();
  const html = document.createElement('html');
  html._connected = true;
  const body = document.createElement('body');
  html.append(body);
  document.documentElement = html;

  const form = document.createElement('form');
  body.append(form);

  // Work authorization checkboxes (opacity-0 inputs with visible labels)
  const authFieldset = document.createElement('fieldset');
  form.append(authFieldset);
  const authLegend = document.createElement('legend', document, 'Work Authorization');
  authFieldset.append(authLegend);

  function makeChoiceInput(type, name, id, labelText, checked) {
    const container = document.createElement('div', document, '', { class: '_option_test' });
    const input = document.createElement('input', document, '', { type, name, id });
    input.checked = checked;
    input._opacity = '0';
    const label = document.createElement('label', document, labelText, { for: id });
    container.append(input, label);
    return { container, input, label };
  }

  const us = makeChoiceInput('checkbox', 'United States', 'auth-us', 'United States', false);
  const sg = makeChoiceInput('checkbox', 'Singapore', 'auth-sg', 'Singapore', false);
  const remote = makeChoiceInput('checkbox', 'N.A. - this is a remote position', 'auth-remote', 'N.A. - this is a remote position', true);
  authFieldset.append(us.container, sg.container, remote.container);

  // Sponsorship radios (opacity-0, grouped by name)
  const sponsorshipFieldset = document.createElement('fieldset');
  form.append(sponsorshipFieldset);
  const sponsorshipLegend = document.createElement('legend', document, 'Sponsorship');
  sponsorshipFieldset.append(sponsorshipLegend);

  const sponsorYes = makeChoiceInput('radio', 'sponsorship', 'sponsor-yes', 'Yes, I require sponsorship', false);
  const sponsorNo = makeChoiceInput('radio', 'sponsorship', 'sponsor-no', 'No, I do not require sponsorship', true);
  sponsorshipFieldset.append(sponsorYes.container, sponsorNo.container);

  // Terms checkbox (opacity-0, required)
  const termsContainer = document.createElement('div', document, '', { class: '_option_test' });
  const termsInput = document.createElement('input', document, '', { type: 'checkbox', name: 'terms', id: 'terms-check' });
  termsInput.required = true;
  termsInput._opacity = '0';
  const termsLabel = document.createElement('label', document, 'I have read and agree to the terms and privacy notice.', { for: 'terms-check' });
  termsContainer.append(termsInput, termsLabel);
  form.append(termsContainer);

  // Submit button
  const submit = document.createElement('button', document, 'Submit Application', { id: 'submit-app', type: 'submit' });
  form.append(submit);

  // Custom getComputedStyle that returns opacity from element._opacity
  const opacityMap = new Map();
  function collectOpacity(el) {
    if (el._opacity) opacityMap.set(el, el._opacity);
    for (const child of el.children) collectOpacity(child);
  }
  collectOpacity(html);
  document.defaultView.getComputedStyle = (el) => ({
    display: 'block',
    visibility: 'visible',
    opacity: opacityMap.get(el) || '1',
  });

  const markConnected = (el) => {
    el._connected = true;
    for (const child of el.children) markConnected(child);
  };
  markConnected(html);

  const window = { document, location: document.location };
  const sandbox = { window, document, __omp_phase1_previous_observation_id_v1: null, URL };
  const observation = vm.runInNewContext(OBSERVER_SOURCE, sandbox);
  validateObservation(observation);

  const checkboxes = observation.controls.filter((c) => c.role === 'checkbox');
  const radios = observation.controls.filter((c) => c.role === 'radio');

  // Checkboxes
  assert.equal(checkboxes.length, 4, 'should observe four native checkboxes');
  const terms = checkboxes.find((c) => c.label === 'I have read and agree to the terms and privacy notice.');
  assert.ok(terms, 'terms checkbox present');
  assert.equal(terms.required, true);
  assert.equal(terms.checked, false);

  const usCheck = checkboxes.find((c) => c.label === 'United States');
  assert.ok(usCheck, 'United States checkbox present');
  assert.equal(usCheck.checked, false);

  const remoteCheck = checkboxes.find((c) => c.label === 'N.A. - this is a remote position');
  assert.ok(remoteCheck, 'remote position checkbox present');
  assert.equal(remoteCheck.checked, true);

  // Radios
  assert.equal(radios.length, 2, 'should observe two sponsorship radios');
  const sponsorYesRadio = radios.find((c) => c.value === 'Yes, I require sponsorship');
  const sponsorNoRadio = radios.find((c) => c.value === 'No, I do not require sponsorship');
  assert.ok(sponsorYesRadio, 'sponsor yes radio present');
  assert.ok(sponsorNoRadio, 'sponsor no radio present');
  assert.equal(sponsorYesRadio.checked, false);
  assert.equal(sponsorNoRadio.checked, true);
  assert.equal(sponsorYesRadio.group_id, sponsorNoRadio.group_id, 'sponsorship radios share group_id');
  assert.ok(sponsorYesRadio.group_id !== null, 'group_id is set');

  // Verify stable IDs
  assert.ok(checkboxes.every((c) => typeof c.stable_id === 'string' && c.stable_id.length > 0));
  assert.ok(radios.every((c) => typeof c.stable_id === 'string' && c.stable_id.length > 0));

  // Verify candidate classification
  assert.ok(checkboxes.every((c) => c.candidate.class === 'field'));
  assert.ok(radios.every((c) => c.candidate.class === 'field'));
});
test('preserves a file field stable ID across uploaded-container replacement', () => {
  const document = new Document();
  const html = document.createElement('html');
  html._connected = true;
  const body = document.createElement('body');
  const form = document.createElement('form');
  const submit = document.createElement('button', document, 'Submit Application', { type: 'submit' });
  const initialContainer = document.createElement('div', document, '', { class: 'file-upload' });
  const initialLabel = document.createElement('label', document, 'Resume', { for: 'resume' });
  const initialInput = document.createElement('input', document, '', { type: 'file', id: 'resume' });
  initialContainer.append(initialLabel, initialInput);
  form.append(initialContainer, submit);
  body.append(form);
  html.append(body);
  document.documentElement = html;

  const markConnected = (element) => {
    element._connected = true;
    for (const child of element.children) markConnected(child);
  };
  markConnected(html);

  const first = observe(document);
  validateObservation(first);
  const firstFile = first.controls.find((control) => control.locator.value === 'resume');
  assert.ok(firstFile);
  assert.equal(first.controls.filter((control) => control.type === 'file').length, 1);


  initialContainer._connected = false;
  for (const child of initialContainer.children) child._connected = false;
  const uploadedContainer = document.createElement('div', document, '', {
    class: 'file-upload',
    'aria-labelledby': 'upload-label-resume',
  });
  const uploadedLabel = document.createElement('label', document, 'Resume', { id: 'upload-label-resume' });
  const filename = document.createElement('span', document, 'resume.pdf', { class: 'file-upload__filename' });
  uploadedContainer.append(uploadedLabel, filename);
  form.append(uploadedContainer);
  markConnected(uploadedContainer);

  const second = observe(document, first.observation_id);
  validateObservation(second);
  const secondFile = second.controls.find((control) => control.type === 'file' && control.file.count === 1);
  assert.ok(secondFile);
  assert.equal(second.controls.filter((control) => control.type === 'file').length, 1);
  assert.equal(secondFile.file.names.length, 1);
  assert.equal(secondFile.file.names[0], 'resume.pdf');
  assert.equal(secondFile.stable_id, firstFile.stable_id);
  assert.notEqual(secondFile.ref, firstFile.ref);

  const ledger = mergeObservation(createLedger(first), second);
  assert.equal(ledger.fields.filter((field) => field.field_id === firstFile.stable_id).length, 1);
});

 
function frameObservation({ src, title = '', frameId = '', captcha = false, response = null }) {
  const document = new Document();
  const html = document.createElement('html');
  html._connected = true;
  const body = document.createElement('body');
  document.documentElement = html;
  html.append(body);
  if (captcha) body.append(document.createElement('div', document, 'Complete the CAPTCHA', { id: 'captcha-widget' }));
  if (response !== null) {
    const responseInput = document.createElement('input', document, '', { type: 'hidden', name: 'g-recaptcha-response' });
    responseInput.value = response;
    body.append(responseInput);
  }
  const frame = document.createElement('iframe', document, '', { src, title, id: frameId });
  Object.defineProperty(frame, 'contentWindow', {
    get() { throw new Error('cross-origin'); },
  });
  body.append(frame);
  const markConnected = (element) => {
    element._connected = true;
    for (const child of element.children) markConnected(child);
  };
  markConnected(html);
  return observe(document);
}

test('inventories inaccessible passive media frames without blocking', () => {
  const observation = frameObservation({ src: 'https://www.youtube.com/embed/abc123', title: 'Video' });
  const mediaFrame = observation.frames.find((frame) => frame.url.includes('youtube.com'));
  assert.ok(mediaFrame);
  assert.equal(mediaFrame.accessible, false);
  assert.equal(observation.blockers.some((blocker) => blocker.frame_id === mediaFrame.id), false);
});

test('blocks inaccessible relevant application frames', () => {
  const observation = frameObservation({
    src: 'https://accounts.example.test/login',
    title: 'Sign in',
    frameId: 'auth-frame',
  });
  const frame = observation.frames.find((candidate) => candidate.id !== 'top');
  assert.ok(frame);
  assert.equal(observation.blockers.some((blocker) => blocker.code === 'inaccessible_frame' && blocker.frame_id === frame.id), true);
  assert.equal(observation.blockers.some((blocker) => blocker.code === 'authentication'), true);
});

test('unsolved CAPTCHA remains a transient blocker', () => {
  const observation = frameObservation({
    src: 'https://captcha.example.test/widget',
    title: 'CAPTCHA',
    captcha: true,
  });
  assert.equal(observation.blockers.some((blocker) => blocker.code === 'captcha'), true);
});

test('top-document CAPTCHA response clears blocker without exposing token bytes', () => {
  const token = 'secret-captcha-token-should-never-appear';
  const observation = frameObservation({
    src: 'https://captcha.example.test/widget',
    title: 'CAPTCHA',
    captcha: true,
    response: token,
  });
  assert.equal(observation.blockers.some((blocker) => blocker.code === 'captcha'), false);
  assert.equal(JSON.stringify(observation).includes(token), false);
});