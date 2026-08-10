import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const SOURCE = fs.readFileSync(new URL('../src/phase1/observer.js', import.meta.url), 'utf8');
class E {
  constructor(tag, doc, text = '', attrs = {}) { this.nodeType = 1; this.tagName = tag.toUpperCase(); this.ownerDocument = doc; this.parentElement = null; this.children = []; this.childNodes = []; this.attributes = new Map(); this._connected = false; this.files = []; this.options = []; this.validity = { valid: true }; this.disabled = false; this.required = false; this.readOnly = false; for (const [k, v] of Object.entries(attrs)) this.setAttribute(k, v); if (text) this.append({ nodeType: 3, nodeValue: text, ownerDocument: doc, get textContent() { return this.nodeValue; } }); }
  append(...nodes) { for (const n of nodes) { n.parentElement = this; n.ownerDocument = this.ownerDocument; if (n.nodeType === 1) { n._connected = this._connected; this.children.push(n); } this.childNodes.push(n); } }
  setAttribute(k, v) { this.attributes.set(k.toLowerCase(), String(v)); }
  getAttribute(k) { return this.attributes.get(k.toLowerCase()) ?? null; }
  hasAttribute(k) { return this.attributes.has(k.toLowerCase()); }
  get isConnected() { return this._connected; }
  get textContent() { return this.childNodes.map((n) => n.textContent || '').join(''); }
  get innerText() { return this.textContent; }
  getRootNode() { return this.ownerDocument; }
  getBoundingClientRect() { return { width: 100, height: 20 }; }
  getClientRects() { return [{}]; }
}
class D {
  constructor() { this.nodeType = 9; this.title = ''; this.baseURI = 'https://app.example.test/'; this.location = { href: this.baseURI }; this.documentElement = null; this.defaultView = { getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1' }) }; }
  createElement(tag, _d, text = '', attrs = {}) { return new E(tag, this, text, attrs); }
}
function observe({ src, marker = null, response = null }) {
  return buildTestDocument({
    elements: [
      ...(marker ? [{ tag: 'div', attrs: { id: 'captcha-widget' }, text: marker }] : []),
      ...(response !== null ? [{ tag: 'input', attrs: { type: 'hidden', name: 'g-recaptcha-response' }, value: response }] : []),
    ],
    frames: [
      { attrs: { src } },
    ],
  });
}

function buildTestDocument({ elements = [], frames = [] } = {}) {
  const document = new D(); const html = document.createElement('html'); const body = document.createElement('body'); html._connected = true; document.documentElement = html; html.append(body);
  for (const elSpec of elements) {
    const el = document.createElement(elSpec.tag || 'div', document, elSpec.text || '', elSpec.attrs || {});
    if (elSpec.value !== undefined) el.value = elSpec.value;
    body.append(el);
  }
  for (const frameSpec of frames) {
    const frame = document.createElement('iframe', document, '', frameSpec.attrs || {});
    if (frameSpec.crossOrigin !== false) {
      Object.defineProperty(frame, 'contentWindow', {
        get() { throw new Error('cross-origin'); },
      });
    }
    body.append(frame);
  }
  const connect = (el) => { el._connected = true; for (const child of el.children) connect(child); }; connect(html);
  return vm.runInNewContext(SOURCE, { window: { document, location: document.location }, document, URL });
}

test('media inventory is retained while inaccessible media is non-blocking', () => {
  const result = observe({ src: 'https://player.vimeo.com/video/42' });
  assert.equal(result.frames.length, 2);
  assert.equal(result.blockers.length, 0);
});

test('captcha response is presence-only and suppresses transient blocker', () => {
  const token = 'opaque-token';
  const result = observe({ src: 'https://captcha.example.test/frame', marker: 'Complete CAPTCHA', response: token });
  assert.equal(result.blockers.some((blocker) => blocker.code === 'captcha'), false);
  assert.equal(JSON.stringify(result).includes(token), false);
});

test('invisible solved CAPTCHA response frame/control does not block', () => {
  const tokenResult = buildTestDocument({
    elements: [
      { tag: 'div', attrs: { id: 'captcha-widget', class: 'g-recaptcha' }, text: 'Complete CAPTCHA' },
      { tag: 'input', attrs: { type: 'hidden', name: 'g-recaptcha-response' }, value: 'token-xyz-123' },
    ],
    frames: [
      { attrs: { src: 'https://www.google.com/recaptcha/api2/anchor', title: 'invisible reCAPTCHA', 'data-size': 'invisible' } },
    ],
  });
  assert.equal(tokenResult.blockers.some((b) => b.code === 'captcha'), false);
  assert.equal(tokenResult.blockers.some((b) => b.code === 'inaccessible_frame'), false);

  const stateResult = buildTestDocument({
    elements: [
      { tag: 'div', attrs: { id: 'recaptcha-solved', class: 'g-recaptcha', 'data-state': 'solved' }, text: 'Solved CAPTCHA' },
    ],
    frames: [
      { attrs: { src: 'https://www.google.com/recaptcha/api2/anchor', title: 'invisible reCAPTCHA', 'data-size': 'invisible' } },
    ],
  });
  assert.equal(stateResult.blockers.some((b) => b.code === 'captcha'), false);
  assert.equal(stateResult.blockers.some((b) => b.code === 'inaccessible_frame'), false);
});

test('unsolved visible or inaccessible CAPTCHA frame does block', () => {
  const visibleResult = buildTestDocument({
    elements: [
      { tag: 'div', attrs: { id: 'captcha-widget', class: 'g-recaptcha' }, text: 'Complete CAPTCHA' },
      { tag: 'input', attrs: { type: 'hidden', name: 'g-recaptcha-response' }, value: '' },
    ],
  });
  assert.equal(visibleResult.blockers.some((b) => b.code === 'captcha'), true);

  const frameResult = buildTestDocument({
    frames: [
      { attrs: { src: 'https://www.google.com/recaptcha/api2/bframe', title: 'recaptcha challenge' } },
    ],
  });
  assert.equal(frameResult.blockers.some((b) => b.code === 'captcha'), true);
});

test('passive YouTube and Vimeo media frames do not block', () => {
  const result = buildTestDocument({
    frames: [
      { attrs: { src: 'https://www.youtube.com/embed/dQw4w9WgXcQ', title: 'YouTube Video' } },
      { attrs: { src: 'https://player.vimeo.com/video/98765', title: 'Vimeo Video' } },
    ],
  });
  assert.equal(result.frames.length, 3);
  assert.equal(result.blockers.length, 0);
});

test('authentication and access-control frames still block', () => {
  const authResult = buildTestDocument({
    frames: [
      { attrs: { src: 'https://auth.example.test/login', title: 'Sign in to access application' } },
    ],
  });
  assert.equal(authResult.blockers.some((b) => b.code === 'authentication'), true);
  assert.equal(authResult.blockers.some((b) => b.code === 'inaccessible_frame'), true);

  const accessResult = buildTestDocument({
    frames: [
      { attrs: { src: 'https://security.example.test/permission-check', title: 'Access Control Security Check' } },
    ],
  });
  assert.equal(accessResult.blockers.some((b) => b.code === 'access_control'), true);
  assert.equal(accessResult.blockers.some((b) => b.code === 'inaccessible_frame'), true);
});

test('unrelated element with data-state="solved" cannot clear a CAPTCHA challenge', () => {
  const result = buildTestDocument({
    elements: [
      { tag: 'div', attrs: { id: 'captcha-widget', class: 'g-recaptcha' }, text: 'Complete CAPTCHA' },
      { tag: 'div', attrs: { id: 'accordion-step-1', class: 'panel', 'data-state': 'solved' }, text: 'Step 1 Completed' },
      { tag: 'button', attrs: { id: 'unrelated-btn', 'data-state': 'solved' }, text: 'Next Step' },
    ],
  });
  assert.equal(result.blockers.some((b) => b.code === 'captcha'), true);
});

test('placeholder-only controls expose an exact attribute locator', () => {
  const result = buildTestDocument({
    elements: [{
      tag: 'input',
      attrs: { role: 'combobox', type: 'text', placeholder: 'Start typing...' },
      value: '',
    }],
  });
  const control = result.controls.find((candidate) => candidate.role === 'combobox');
  assert.equal(control.locator.strategy, 'placeholder');
  assert.equal(control.locator.value, 'Start typing...');
  assert.equal(control.locator.role, 'combobox');
  assert.equal(control.locator.name, null);
});
