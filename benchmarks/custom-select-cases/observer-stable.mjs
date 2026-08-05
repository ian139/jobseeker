import fs from 'node:fs';
import vm from 'node:vm';

const TARGET_LABEL = 'Engineering';
const TARGET_VALUE = 'engineering';
const SEARCH_TEXT = 'Eng';
const FIXED_NOW = Date.UTC(2026, 0, 2, 3, 4, 5, 678);
const READINESS_BUDGET = 2;

class TextNode {
  constructor(value, ownerDocument) {
    this.nodeType = 3;
    this.nodeValue = value;
    this.ownerDocument = ownerDocument;
    this.parentElement = null;
  }

  get textContent() {
    return this.nodeValue;
  }
}

function splitSelectors(selector) {
  return String(selector).split(',').map((part) => part.trim()).filter(Boolean);
}

function matchesSimpleSelector(element, selector) {
  let candidate = selector.trim();
  if (!candidate) return false;
  if (candidate.startsWith(':scope > ')) candidate = candidate.slice(9).trim();

  const tagMatch = candidate.match(/^([a-z][a-z0-9-]*)/i);
  if (tagMatch && element.tagName !== tagMatch[1].toUpperCase()) return false;

  const classMatches = [...candidate.matchAll(/\.([a-z0-9_-]+)/gi)];
  if (classMatches.some((match) => !String(element.getAttribute('class') || '').split(/\s+/).includes(match[1]))) {
    return false;
  }

  const attributeMatches = [...candidate.matchAll(/\[([^\]=~]+)(?:\s*=\s*(["']?)([^\]'"\s]+)\2)?\]/g)];
  for (const [, rawName, , rawValue] of attributeMatches) {
    const name = rawName.trim();
    const actual = element.getAttribute(name);
    if (actual == null) return false;
    if (rawValue !== undefined && actual !== rawValue) return false;
  }
  return true;
}

class Element {
  constructor(tagName, ownerDocument, text = '', attributes = {}) {
    this.nodeType = 1;
    this.tagName = String(tagName).toUpperCase();
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
    this.value = '';
    for (const [name, value] of Object.entries(attributes)) this.setAttribute(name, value);
    if (text) this.append(new TextNode(text, ownerDocument));
  }
  get isConnected() {
    return this._connected;
  }


  append(...nodes) {
    for (const node of nodes) {
      if (!node) continue;
      node.parentElement = this;
      node.ownerDocument = this.ownerDocument;
      if (node.nodeType === 1) {
        node._connected = this._connected;
        node.markConnected(node._connected);
        this.children.push(node);
      }
      this.childNodes.push(node);
    }
    return nodes[nodes.length - 1] || null;
  }

  markConnected(connected) {
    this._connected = connected;
    for (const child of this.children) child.markConnected(connected);
  }

  replaceChildren(...nodes) {
    for (const child of this.children) child.markConnected(false);
    this.children = [];
    this.childNodes = [];
    this.append(...nodes);
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

  contains(other) {
    let current = other;
    while (current) {
      if (current === this) return true;
      current = current.parentElement;
    }
    return false;
  }

  getBoundingClientRect() {
    return { width: 100, height: 20 };
  }

  getClientRects() {
    return [{ width: 100, height: 20 }];
  }

  querySelectorAll(selector) {
    const selectors = splitSelectors(selector);
    const directOnly = selectors.some((part) => part.startsWith(':scope > '));
    const candidates = directOnly ? this.children : this.walkDescendants();
    return candidates.filter((candidate) => selectors.some((part) => matchesSimpleSelector(candidate, part)));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  walkDescendants() {
    const result = [];
    const stack = [...this.children].reverse();
    while (stack.length) {
      const current = stack.pop();
      result.push(current);
      for (let index = current.children.length - 1; index >= 0; index -= 1) stack.push(current.children[index]);
    }
    return result;
  }
}

class Document {
  constructor() {
    this.nodeType = 9;
    this.title = 'Synthetic custom-select application';
    this.baseURI = 'https://example.test/application';
    this.location = { href: this.baseURI };
    this.defaultView = {
      getComputedStyle() {
        return { display: 'block', visibility: 'visible', opacity: '1' };
      },
    };
    this.documentElement = null;
  }

  createElement(tagName) {
    return new Element(tagName, this);
  }

  querySelectorAll(selector) {
    if (!this.documentElement) return [];
    const selectors = splitSelectors(selector);
    return [this.documentElement, ...this.documentElement.walkDescendants()]
      .filter((candidate) => selectors.some((part) => matchesSimpleSelector(candidate, part)));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

function connectTree(document) {
  document.documentElement.markConnected(true);
}

function buildFixture() {
  const document = new Document();
  const html = document.createElement('html');
  const body = document.createElement('body');
  const field = document.createElement('div');
  field.setAttribute('class', 'application-field');
  const label = document.createElement('label', document);
  label.setAttribute('for', 'department-input');
  label.append(new TextNode('Department', document));
  field.append(label);
  body.append(field);
  html.append(body);
  document.documentElement = html;

  const fixture = {
    document,
    body,
    field,
    input: null,
    shell: null,
    menu: null,
    singleValue: null,
    pendingTurns: 1,
  };

  const makeInput = (value) => {
    const input = document.createElement('input');
    input.setAttribute('id', 'department-input');
    input.setAttribute('name', 'department');
    input.setAttribute('type', 'text');
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-label', 'Department');
    input.setAttribute('aria-controls', 'department-menu');
    input.setAttribute('aria-expanded', 'false');
    input.value = value;
    return input;
  };

  const mountClosedSearch = () => {
    fixture.menu = null;
    fixture.singleValue = null;
    const shell = document.createElement('div');
    shell.setAttribute('class', 'select__control');
    const valueContainer = document.createElement('div');
    valueContainer.setAttribute('class', 'select__value-container');
    const input = makeInput(SEARCH_TEXT);
    valueContainer.append(input);
    shell.append(valueContainer);
    fixture.field.replaceChildren(label, shell);
    fixture.input = input;
    fixture.shell = shell;
  };

  const showExactOption = () => {
    if (fixture.pendingTurns > 0) fixture.pendingTurns -= 1;
    if (fixture.pendingTurns !== 0) return;
    fixture.input.setAttribute('aria-expanded', 'true');
    const menu = document.createElement('div');
    menu.setAttribute('id', 'department-menu');
    menu.setAttribute('class', 'select__menu');
    menu.setAttribute('role', 'listbox');
    const distractor = document.createElement('div');
    distractor.append(new TextNode('Engineer', document));
    distractor.setAttribute('role', 'option');
    distractor.setAttribute('data-value', 'engineer');
    distractor.setAttribute('aria-selected', 'false');
    const exact = document.createElement('div');
    exact.setAttribute('role', 'option');
    exact.setAttribute('data-value', TARGET_VALUE);
    exact.setAttribute('aria-selected', 'false');
    exact.append(new TextNode('  Engineering  ', document));
    menu.append(distractor, exact);
    fixture.body.append(menu);
    fixture.menu = menu;
  };

  const rerenderCommitted = () => {
    if (fixture.menu) {
      fixture.body.replaceChildren(fixture.field);
      fixture.menu = null;
    }
    const shell = document.createElement('div');
    shell.setAttribute('class', 'select__control');
    const valueContainer = document.createElement('div');
    valueContainer.setAttribute('class', 'select__value-container');
    const singleValue = document.createElement('div');
    singleValue.setAttribute('class', 'select__single-value');
    singleValue.append(new TextNode('  Engineering  ', document));
    const input = makeInput('');
    input.setAttribute('aria-expanded', 'false');
    valueContainer.append(singleValue, input);
    shell.append(valueContainer);
    fixture.field.replaceChildren(label, shell);
    fixture.input = input;
    fixture.shell = shell;
    fixture.singleValue = singleValue;
  };

  mountClosedSearch();
  connectTree(document);
  fixture.showExactOption = showExactOption;
  fixture.rerenderCommitted = rerenderCommitted;
  fixture.mountClosedSearch = mountClosedSearch;
  return fixture;
}

function observe(source, document, previousObservationId, sequence) {
  const RealDate = Date;
  const seeds = [0x10203040, 0x50607080, 0x90a0b0c0];
  const FixedDate = class extends RealDate {
    constructor(...args) {
      super(...(args.length ? args : [FIXED_NOW]));
    }

    static now() {
      return FIXED_NOW;
    }
  };
  const crypto = {
    getRandomValues(values) {
      if (sequence >= seeds.length) throw new Error('observer_fixture_sequence_exhausted');
      values[0] = seeds[sequence];
      values[1] = seeds[sequence];
      return values;
    },
  };
  const window = { document, location: document.location };
  return vm.runInNewContext(source, {
    window,
    document,
    __omp_phase1_previous_observation_id_v1: previousObservationId,
    URL,
    Date: FixedDate,
    crypto,
  });
}

function validObservation(observation, stage) {
  if (!observation || typeof observation !== 'object' || !Array.isArray(observation.controls)) {
    throw new Error(`observer_fixture_invalid_result_${stage}`);
  }
  return observation;
}

function combobox(observation) {
  return observation.controls.find((control) => control.role === 'combobox') || null;
}

function exactOption(control) {
  return (control?.options || []).find((option) => option.label === TARGET_LABEL && option.value === TARGET_VALUE) || null;
}

function fixtureInvariant(fixture) {
  if (!fixture.document.documentElement?.isConnected || !fixture.input?.isConnected || !fixture.shell?.isConnected) {
    throw new Error('observer_fixture_disconnected');
  }
}

export function evaluate() {
  const sourceUrl = new URL('../../src/phase1/observer.js', import.meta.url);
  const source = fs.readFileSync(sourceUrl, 'utf8');

  const fixture = buildFixture();
  fixtureInvariant(fixture);
  const diagnostics = [];
  let checks = 0;
  let failures = 0;
  const check = (condition, diagnostic) => {
    checks += 1;
    if (!condition) {
      failures += 1;
      diagnostics.push(diagnostic);
    }
  };

  const first = validObservation(observe(source, fixture.document, null, 0), 'before-open');
  const firstControl = combobox(first);

  let readinessTurns = 0;
  while (!fixture.menu && readinessTurns < READINESS_BUDGET) {
    fixture.showExactOption();
    readinessTurns += 1;
  }
  fixtureInvariant(fixture);

  const second = validObservation(observe(source, fixture.document, first.observation_id, 1), 'option-visible');
  const secondControl = combobox(second);
  const secondOption = exactOption(secondControl);
  check(Boolean(secondOption && secondOption.label === TARGET_LABEL && secondOption.value === TARGET_VALUE), 'OBSERVER_EXACT_OPTION_MISSING');

  fixture.rerenderCommitted();
  fixtureInvariant(fixture);
  const third = validObservation(observe(source, fixture.document, second.observation_id, 2), 'post-rerender');
  const thirdControl = combobox(third);
  const renderedCanonical = fixture.singleValue?.innerText.replace(/\s+/g, ' ').trim();

  check(
    second.previous_observation_id === first.observation_id
      && third.previous_observation_id === second.observation_id
      && thirdControl?.ref !== secondControl?.ref,
    'OBSERVATION_CHAIN_MISMATCH',
  );
  check(
    Boolean(firstControl?.stable_id)
      && firstControl.stable_id === secondControl?.stable_id
      && firstControl.stable_id === thirdControl?.stable_id,
    'OBSERVER_STABLE_ID_DRIFT',
  );
  check(
    secondControl?.value !== SEARCH_TEXT
      && thirdControl?.value !== SEARCH_TEXT,
    'OBSERVER_SEARCH_TEXT_COMMITTED',
  );
  check(
    thirdControl?.value === renderedCanonical
      && thirdControl?.value === TARGET_LABEL
      && !thirdControl?.options?.length,
    'OBSERVER_COMMITTED_LABEL_MISSING',
  );

  return Object.freeze({
    name: 'observer-stable',
    checks,
    failures,
    diagnostics: Object.freeze(diagnostics),
  });
}
