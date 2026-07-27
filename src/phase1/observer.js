(() => {
  "use strict";

  const MAX_CONTROLS = 512;
  const MAX_FRAMES = 64;
  const MAX_NODES = 50000;
  const MAX_OPTIONS = 128;
  const MAX_BLOCKERS = 32;
  const MAX_VALUE_CHARS = 4096;
  const MAX_TEXT_CHARS = 512;
  const MAX_LOCATOR_CHARS = 160;
  const MAX_URL_CHARS = 2048;
  const MAX_FILE_NAMES = 32;
  const MAX_FILE_NAME_CHARS = 240;

  function text(value, limit = MAX_TEXT_CHARS) {
    if (value == null) return null;
    const normalized = String(value).replace(/\s+/g, " ").trim();
    if (!normalized) return null;
    return normalized.length > limit ? normalized.slice(0, limit) : normalized;
  }

  function urlText(value) {
    if (value == null) return null;
    const normalized = String(value).trim();
    if (!normalized) return null;
    return normalized.length > MAX_URL_CHARS ? normalized.slice(0, MAX_URL_CHARS) : normalized;
  }

  function exactString(value, label) {
    const result = String(value == null ? "" : value);
    if (result.length > MAX_VALUE_CHARS) {
      throw new Error("observer_oversized_" + label);
    }
    return result;
  }

  function attr(element, name) {
    try {
      const value = element.getAttribute(name);
      return value == null || value === "" ? null : String(value);
    } catch (_) {
      return null;
    }
  }

  function boolAttr(element, name) {
    const value = attr(element, name);
    return value != null && /^(true|1|yes)$/i.test(value.trim());
  }

  function firstAttr(element, names) {
    for (const name of names) {
      const value = attr(element, name);
      if (value) return value;
    }
    return null;
  }

  function utf8(value) {
    const source = String(value);
    const bytes = [];
    for (let index = 0; index < source.length; index += 1) {
      let code = source.charCodeAt(index);
      if (code >= 0xd800 && code <= 0xdbff && index + 1 < source.length) {
        const next = source.charCodeAt(index + 1);
        if (next >= 0xdc00 && next <= 0xdfff) {
          code = 0x10000 + ((code - 0xd800) << 10) + (next - 0xdc00);
          index += 1;
        }
      }
      if (code <= 0x7f) {
        bytes.push(code);
      } else if (code <= 0x7ff) {
        bytes.push(0xc0 | (code >> 6), 0x80 | (code & 0x3f));
      } else if (code <= 0xffff) {
        bytes.push(0xe0 | (code >> 12), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f));
      } else {
        bytes.push(
          0xf0 | (code >> 18),
          0x80 | ((code >> 12) & 0x3f),
          0x80 | ((code >> 6) & 0x3f),
          0x80 | (code & 0x3f),
        );
      }
    }
    return bytes;
  }

  function sha256(value) {
    const bytes = utf8(value);
    const bitLength = bytes.length * 8;
    bytes.push(0x80);
    while ((bytes.length + 8) % 64 !== 0) bytes.push(0);
    for (let shift = 56; shift >= 0; shift -= 8) bytes.push(Math.floor(bitLength / Math.pow(2, shift)) & 0xff);

    const constants = [
      0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
      0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
      0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
      0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
      0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
      0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
      0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
      0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
    ];
    let h0 = 0x6a09e667;
    let h1 = 0xbb67ae85;
    let h2 = 0x3c6ef372;
    let h3 = 0xa54ff53a;
    let h4 = 0x510e527f;
    let h5 = 0x9b05688c;
    let h6 = 0x1f83d9ab;
    let h7 = 0x5be0cd19;

    function rotateRight(value, amount) {
      return (value >>> amount) | (value << (32 - amount));
    }

    for (let offset = 0; offset < bytes.length; offset += 64) {
      const schedule = new Array(64).fill(0);
      for (let index = 0; index < 16; index += 1) {
        const at = offset + index * 4;
        schedule[index] = ((bytes[at] << 24) | (bytes[at + 1] << 16) | (bytes[at + 2] << 8) | bytes[at + 3]) >>> 0;
      }
      for (let index = 16; index < 64; index += 1) {
        const s0 = rotateRight(schedule[index - 15], 7) ^ rotateRight(schedule[index - 15], 18) ^ (schedule[index - 15] >>> 3);
        const s1 = rotateRight(schedule[index - 2], 17) ^ rotateRight(schedule[index - 2], 19) ^ (schedule[index - 2] >>> 10);
        schedule[index] = (schedule[index - 16] + s0 + schedule[index - 7] + s1) >>> 0;
      }

      let a = h0;
      let b = h1;
      let c = h2;
      let d = h3;
      let e = h4;
      let f = h5;
      let g = h6;
      let h = h7;
      for (let index = 0; index < 64; index += 1) {
        const s1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
        const choose = (e & f) ^ (~e & g);
        const temp1 = (h + s1 + choose + constants[index] + schedule[index]) >>> 0;
        const s0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
        const majority = (a & b) ^ (a & c) ^ (b & c);
        const temp2 = (s0 + majority) >>> 0;
        h = g;
        g = f;
        f = e;
        e = (d + temp1) >>> 0;
        d = c;
        c = b;
        b = a;
        a = (temp1 + temp2) >>> 0;
      }
      h0 = (h0 + a) >>> 0;
      h1 = (h1 + b) >>> 0;
      h2 = (h2 + c) >>> 0;
      h3 = (h3 + d) >>> 0;
      h4 = (h4 + e) >>> 0;
      h5 = (h5 + f) >>> 0;
      h6 = (h6 + g) >>> 0;
      h7 = (h7 + h) >>> 0;
    }

    return [h0, h1, h2, h3, h4, h5, h6, h7]
      .map((part) => part.toString(16).padStart(8, "0"))
      .join("");
  }

  function canonical(value) {
    if (value === null) return "null";
    if (typeof value === "string") return JSON.stringify(value);
    if (typeof value === "boolean") return value ? "true" : "false";
    if (typeof value === "number") return Number.isFinite(value) ? String(value) : "null";
    if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
    if (typeof value === "object") {
      return "{" + Object.keys(value).sort().map((key) => JSON.stringify(key) + ":" + canonical(value[key])).join(",") + "}";
    }
    return "null";
  }

  function elementText(element, limit = MAX_TEXT_CHARS) {
    if (!element) return null;
    try {
      const value = element.innerText != null ? element.innerText : element.textContent;
      return text(value, limit);
    } catch (_) {
      try { return text(element.textContent, limit); } catch (__) { return null; }
    }
  }

  function exactElementText(element, label) {
    if (!element) return "";
    let value = "";
    try {
      value = element.innerText != null ? element.innerText : element.textContent;
    } catch (_) {
      try { value = element.textContent; } catch (__) { value = ""; }
    }
    return exactString(String(value || "").replace(/\s+/g, " ").trim(), label);
  }

  function hasClassToken(element, token) {
    const value = attr(element, "class");
    return Boolean(value && value.split(/\s+/).includes(token));
  }

  function descendantWithClass(root, token) {
    const stack = [];
    try {
      const children = root.children || [];
      for (let index = children.length - 1; index >= 0; index -= 1) stack.push(children[index]);
    } catch (_) {
      return null;
    }
    let visited = 0;
    while (stack.length && visited < 512) {
      const current = stack.pop();
      visited += 1;
      if (!current || current.nodeType !== 1) continue;
      if (hasClassToken(current, token)) return current;
      try {
        const children = current.children || [];
        for (let index = children.length - 1; index >= 0; index -= 1) stack.push(children[index]);
      } catch (_) {
        // Ignore an unusual detached subtree.
      }
    }
    return null;
  }

  function reactSelectSingleValue(element) {
    let current = parentElement(element);
    for (let depth = 0; current && depth < 16; depth += 1) {
      const candidate = descendantWithClass(current, "select__single-value");
      if (candidate && isVisible(candidate)) return exactElementText(candidate, "combobox_value");
      if (hasClassToken(current, "select__control")) break;
      current = parentElement(current);
    }
    return "";
  }

  function parentElement(element) {
    try {
      if (element.parentElement) return element.parentElement;
      const root = element.getRootNode && element.getRootNode();
      return root && root.host ? root.host : null;
    } catch (_) {
      return null;
    }
  }

  function ancestor(element, predicate) {
    let current = element;
    for (let depth = 0; current && depth < 64; depth += 1) {
      if (predicate(current)) return current;
      current = parentElement(current);
    }
    return null;
  }
  function uploadContainerFor(element) {
    let identified = null;
    let semantic = null;
    let current = parentElement(element);
    for (let depth = 0; current && depth < 64; depth += 1) {
      if (hasClassToken(current, "file-upload")
          || hasClassToken(current, "ashby-application-form-autofill-input-root")
          || (current.querySelector && current.querySelector(':scope > input[type="file"]')
            && (current.querySelector('[title="Delete file"]')
              || current.querySelector('button')?.textContent?.trim() === "Replace"))) {
        return current;
      }
      if (identified === null &&
          (testIdFor(current) || attr(current, "id") || attr(current, "name"))) {
        identified = current;
      }
      const tag = current.tagName && current.tagName.toLowerCase();
      if (semantic === null &&
          (tag === "label" || tag === "fieldset" || rawRole(current) === "group")) {
        semantic = current;
      }
      current = parentElement(current);
    }
    return identified || semantic || parentElement(element);
  }


  function isVisible(element) {
    try {
      if (!element || element.nodeType !== 1 || !element.isConnected) return false;
      let current = element;
      for (let depth = 0; current && depth < 64; depth += 1) {
        if (current.hasAttribute("hidden") || boolAttr(current, "aria-hidden")) return false;
        const style = current.ownerDocument.defaultView.getComputedStyle(current);
        if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse" || style.opacity === "0") return false;
        current = parentElement(current);
      }
      const rect = element.getBoundingClientRect();
      return !!rect && rect.width > 0 && rect.height > 0 && element.getClientRects().length > 0;
    } catch (_) {
      return false;
    }
  }

  function isObservableControl(element, info) {
    if (isVisible(element)) return true;
    if (!info) return false;
    if (info.type === "file") {
      if (!attr(element, "id") && !attr(element, "name")) return false;
      const container = uploadContainerFor(element);
      return Boolean(container && isVisible(container));
    }
    if ((info.type === "checkbox" || info.type === "radio") && info.native) {
      const labels = element.labels;
      if (labels && labels.length > 0) {
        for (const label of labels) {
          if (isVisible(label)) return true;
        }
      }
      const wrapper = ancestor(element, (candidate) => hasClassToken(candidate, "choice") || hasClassToken(candidate, "option"));
      if (wrapper && isVisible(wrapper)) return true;
      return false;
    }
    if (info.role !== "combobox" || info.tag !== "input") return false;
    const selectControl = ancestor(element, (candidate) => hasClassToken(candidate, "select__control"));
    return Boolean(selectControl && isVisible(selectControl));
  }

  function walkDocument(document) {
    const elements = [];
    const stack = [];
    if (document && document.documentElement) stack.push(document.documentElement);
    while (stack.length) {
      const current = stack.pop();
      if (!current || (current.nodeType !== 1 && current.nodeType !== 11)) continue;
      if (current.nodeType === 1) {
        elements.push(current);
        if (elements.length > MAX_NODES) throw new Error("observer_node_limit_exceeded");
      }
      try {
        if (current.nodeType === 1 && current.shadowRoot) stack.push(current.shadowRoot);
        const children = current.children || current.childNodes || [];
        for (let index = children.length - 1; index >= 0; index -= 1) stack.push(children[index]);
      } catch (_) {
        // A detached or unusual custom element is simply not traversed further.
      }
    }
    return elements;
  }

  function idMapFor(elements) {
    const result = Object.create(null);
    for (const element of elements) {
      const id = attr(element, "id");
      if (id && result[id] == null) result[id] = element;
    }
    return result;
  }

  function referencedText(element, attribute, ids, limit = MAX_TEXT_CHARS) {
    const value = attr(element, attribute);
    if (!value) return [];
    const output = [];
    for (const id of value.split(/\s+/)) {
      if (!id || !ids[id]) continue;
      const part = elementText(ids[id], limit);
      if (part) output.push(part);
    }
    return output;
  }

  function associatedLabels(element, elements) {
    const labels = [];
    const id = attr(element, "id");
    if (id) {
      for (const candidate of elements) {
        if (candidate.tagName && candidate.tagName.toLowerCase() === "label" && attr(candidate, "for") === id) labels.push(candidate);
      }
    }
    const wrapping = ancestor(element, (candidate) => candidate.tagName && candidate.tagName.toLowerCase() === "label");
    if (wrapping && !labels.includes(wrapping)) labels.push(wrapping);
    return labels;
  }

  function accessibleName(element, elements, ids) {
    const labelled = referencedText(element, "aria-labelledby", ids);
    if (labelled.length) return text(labelled.join(" "));
    const ariaLabel = text(attr(element, "aria-label"));
    if (ariaLabel) return ariaLabel;
    const labels = associatedLabels(element, elements).map((label) => elementText(label)).filter(Boolean);
    if (labels.length) return text(labels.join(" "));
    const tag = element.tagName ? element.tagName.toLowerCase() : "";
    if (tag === "input") {
      const inputType = nativeType(element, tag);
      if (["button", "submit", "reset", "image"].includes(inputType)) {
        let value = attr(element, "value");
        if (value == null) {
          try { value = element.value; } catch (_) { value = null; }
        }
        const inputLabel = text(value);
        if (inputLabel) return inputLabel;
      }
    }
    if (tag === "button" || tag === "a") {
      const content = elementText(element);
      if (content) return content;
    }
    const questionAncestor = ancestor(element, (candidate) => {
      const tag = candidate.tagName ? candidate.tagName.toLowerCase() : "";
      if (tag === "html" || tag === "body" || tag === "form") return false;
      const role = rawRole(candidate);
      if (role === "group" || role === "radiogroup" || tag === "fieldset") return true;
      try {
        const controls = candidate.querySelectorAll("input, select, textarea, [role='combobox'], [role='textbox']");
        return controls.length === 1;
      } catch (_) {
        return false;
      }
    });
    if (questionAncestor) {
      try {
        const candidates = Array.from(questionAncestor.querySelectorAll("p, span, label, h1, h2, h3, h4, h5, h6, legend"));
        for (const candidate of candidates) {
          if (ancestor(candidate, (parent) => parent === element)) continue;
          const candidateText = elementText(candidate);
          if (candidateText && !/^(?:Select|Select\.\.\.|\*)$/i.test(candidateText.trim())) {
            return text(candidateText);
          }
        }
      } catch (_) {
        // Fall through
      }
    }
    const legend = ancestor(element, (candidate) => candidate.tagName && candidate.tagName.toLowerCase() === "fieldset");
    if (legend) {
      try {
        const firstLegend = Array.from(legend.children).find((child) => child.tagName && child.tagName.toLowerCase() === "legend");
        const legendText = elementText(firstLegend);
        if (legendText) return legendText;
      } catch (_) {
        // Fall through to the name attribute.
      }
    }
    return text(attr(element, "name"));
  }

  function descriptionFor(element, ids, label) {
    const described = referencedText(element, "aria-describedby", ids).join(" ");
    const error = referencedText(element, "aria-errormessage", ids).join(" ");
    const title = text(attr(element, "title"));
    const value = text(error || described || (title && title !== label ? title : null));
    return value;
  }

  const ALLOWED_ROLES = new Set(["textbox", "combobox", "checkbox", "radio", "button", "switch", "listbox"]);

  function rawRole(element) {
    const value = attr(element, "role");
    return value ? value.trim().split(/\s+/)[0].toLowerCase() : null;
  }

  function explicitRole(element) {
    const role = rawRole(element);
    return role && ALLOWED_ROLES.has(role) ? role : null;
  }

  function controlledPopupIds(elements) {
    const ids = new Set();
    for (const element of elements) {
      if (rawRole(element) !== "combobox") continue;
      for (const attribute of ["aria-controls", "aria-owns"]) {
        const value = attr(element, attribute);
        if (!value) continue;
        for (const id of value.trim().split(/\s+/)) {
          if (id) ids.add(id);
        }
      }
    }
    return ids;
  }

  function nativeType(element, tag) {
    const raw = attr(element, "type");
    if (raw) return raw.toLowerCase();
    if (tag === "input") return "text";
    if (tag === "button") return "submit";
    return null;
  }

  function semantics(element) {
    const tag = element.tagName ? element.tagName.toLowerCase() : "";
    const role = explicitRole(element);
    if (tag === "input") {
      const type = nativeType(element, tag);
      if (type === "hidden") return null;
      if (["button", "submit", "reset", "image"].includes(type)) return { kind: "button", tag, type, role: "button", native: true };
      const nativeRole = type === "checkbox" ? "checkbox" : type === "radio" ? "radio" : type === "file" ? "textbox" : "textbox";
      return { kind: "input", tag, type, role: role || nativeRole, native: true };
    }
    if (tag === "textarea") return { kind: "textarea", tag, type: null, role: role || "textbox", native: true };
    if (tag === "select") return { kind: "select", tag, type: null, role: role || (element.multiple ? "listbox" : "combobox"), native: true };
    if (tag === "button") return { kind: "button", tag, type: nativeType(element, tag), role: role || "button", native: true };
    if (tag === "a" && attr(element, "href")) {
      const label = elementText(element);
      if (label && /\b(apply|start|begin|continue|next|proceed|review|go to application)\b/i.test(label)) {
        return { kind: "navigation", tag, type: null, role: "link", native: true };
      }
    }
    if (role) return { kind: "aria", tag, type: null, role, native: false };
    const contenteditable = attr(element, "contenteditable");
    if (contenteditable != null && contenteditable.toLowerCase() !== "false") {
      return { kind: "contenteditable", tag, type: null, role: "textbox", native: false };
    }
    return null;
  }

  function honeypot(element) {
    const values = [attr(element, "id"), attr(element, "name"), attr(element, "class"), attr(element, "autocomplete")].filter(Boolean).join(" ").toLowerCase();
    return /(?:honeypot|honey[-_ ]?pot|bot[-_ ]?trap|spam[-_ ]?trap|hp[-_ ]?field|no[-_ ]?submit)/i.test(values);
  }

  function structuralPath(element) {
    const parts = [];
    let current = element;
    for (let depth = 0; current && depth < 32; depth += 1) {
      const parent = parentElement(current);
      let index = 0;
      if (parent) {
        try {
          for (const sibling of parent.children) {
            if (sibling === current) break;
            index += 1;
          }
        } catch (_) {
          index = 0;
        }
      }
      parts.push((current.tagName || "element").toLowerCase() + ":" + index);
      current = parent;
    }
    return parts.reverse().join("/");
  }

  function frameUrl(element, parentUrl) {
    try {
      const contentWindow = element.contentWindow;
      if (contentWindow && contentWindow.location && contentWindow.location.href) return urlText(contentWindow.location.href);
    } catch (_) {
      // Cross-origin frame URLs may only be available from the src attribute.
    }
    const source = attr(element, "src");
    if (!source) return "about:blank";
    try { return urlText(new URL(source, parentUrl || document.baseURI).href); } catch (_) { return urlText(source); }
  }

  function originFor(value) {
    if (!value) return null;
    try {
      const origin = new URL(value, document.baseURI).origin;
      return origin === "null" ? null : origin;
    } catch (_) {
      return null;
    }
  }

  function frameIdentity(parentId, element, path) {
    const identity = [parentId || "", attr(element, "id") || "", attr(element, "name") || "", attr(element, "data-testid") || "", attr(element, "src") || "", path.join(".")].join("\u0000");
    return "frame-" + sha256(identity).slice(0, 24);
  }

  function testIdFor(element) {
    return firstAttr(element, ["data-testid", "data-test-id", "data-qa", "data-cy", "data-test"]);
  }

  function locatorFor(element, role, label, name) {
    const testId = testIdFor(element);
    const id = attr(element, "id");
    let strategy = "none";
    let value = null;
    if (testId) {
      strategy = "test_id";
      value = testId;
    } else if (id) {
      strategy = "id";
      value = id;
    } else if (name) {
      strategy = "name";
      value = name;
    } else if (role && label) {
      strategy = "role";
      value = label;
    }
    return {
      strategy,
      value: value ? text(value, MAX_LOCATOR_CHARS) : null,
      role: role || null,
      name: name || null,
    };
  }

  function boolProperty(element, property, ariaName) {
    try {
      if (Boolean(element[property])) return true;
    } catch (_) {
      // Fall through to ARIA state.
    }
    return boolAttr(element, ariaName);
  }

  function checkedFor(element, info) {
    if (!["checkbox", "radio", "switch"].includes(info.role)) return null;
    if (info.native && (info.type === "checkbox" || info.type === "radio")) {
      try { return Boolean(element.checked); } catch (_) { return boolAttr(element, "aria-checked"); }
    }
    const value = attr(element, "aria-checked");
    return value == null ? false : /^(true|mixed|1)$/i.test(value);
  }

  function optionValue(option) {
    try { return exactString(option.value, "option_value"); } catch (_) { return exactString(attr(option, "value") || "", "option_value"); }
  }

  function optionRecord(option, selectedOverride = null) {
    const value = optionValue(option);
    const label = text(option.label != null ? option.label : option.textContent, MAX_TEXT_CHARS) || "";
    let disabled = false;
    try { disabled = Boolean(option.disabled); } catch (_) { disabled = boolAttr(option, "aria-disabled"); }
    const selected = selectedOverride == null ? Boolean(option.selected) : Boolean(selectedOverride);
    return { value, label, disabled, selected };
  }

  function customOptions(element, elements, ids) {
    const candidates = [];
    const seen = new Set();
    const add = (candidate) => {
      if (!candidate || seen.has(candidate)) return;
      if (candidate !== element && (attr(candidate, "role") || "").toLowerCase().split(/\s+/)[0] === "option") {
        seen.add(candidate);
        candidates.push(candidate);
      }
    };
    for (const candidate of elements) {
      if (ancestor(candidate, (parent) => parent === element)) add(candidate);
    }
    for (const attribute of ["aria-controls", "aria-owns"]) {
      const owned = attr(element, attribute);
      if (!owned) continue;
      for (const id of owned.trim().split(/\s+/)) {
        if (!ids[id]) continue;
        for (const candidate of elements) {
          if (ancestor(candidate, (parent) => parent === ids[id])) add(candidate);
        }
      }
    }
    if (candidates.length > MAX_OPTIONS) throw new Error("observer_option_limit_exceeded");
    return candidates.map((option) => {
      const selected = boolAttr(option, "aria-selected");
      const record = optionRecord({
        value: attr(option, "data-value") || attr(option, "value") || "",
        label: elementText(option) || "",
        disabled: boolAttr(option, "aria-disabled"),
        selected,
      }, selected);
      return record;
    });
  }

  function optionsFor(element, info, elements, ids) {
    if (info.native && info.tag === "select") {
      const result = [];
      try {
        for (const option of Array.from(element.options || [])) {
          result.push(optionRecord(option));
          if (result.length > MAX_OPTIONS) throw new Error("observer_option_limit_exceeded");
        }
      } catch (error) {
        if (error && String(error.message).includes("option_limit")) throw error;
      }
      if (!element.multiple) return result.filter((option) => option.value !== "");
      return result;
    }
    if (["combobox", "listbox"].includes(info.role)) return customOptions(element, elements, ids);
    return [];
  }

  function valueFor(element, info, options, checked, label) {
    const tag = info.tag;
    if (info.role === "checkbox" || info.role === "switch") return { value: checked === true, present: checked === true };
    if (info.role === "radio") {
      let value = attr(element, "value");
      if (value == null) value = label;
      return { value: value == null ? null : exactString(value, "radio_value"), present: checked === true };
    }
    if (info.kind === "button" || info.kind === "navigation") {
      let value = attr(element, "value");
      if (value == null && info.native && info.tag === "input") {
        try { value = element.value; } catch (_) { value = null; }
      }
      return { value: value == null ? null : exactString(value, "button_value"), present: value != null && String(value) !== "" };
    }
    if (info.type === "file") return { value: null, present: (() => { try { return Boolean(element.files && element.files.length); } catch (_) { return false; } })() };
    if (tag === "select" || info.role === "listbox") {
      const selectedValues = options.filter((option) => option.selected).map((option) => option.value);
      return {
        value: tag === "select" && !element.multiple
          ? (selectedValues.length ? selectedValues[0] : null)
          : selectedValues,
        present: selectedValues.length > 0,
      };
    }
    let value = null;
    const selectedValue = options.find((option) => option.selected);
    if (info.native && (tag === "input" || tag === "textarea" || tag === "select")) {
      try { value = element.value; } catch (_) { value = ""; }
      if (!value && info.role === "combobox") value = reactSelectSingleValue(element);
    } else if (info.kind === "contenteditable" || info.role === "textbox" || info.role === "combobox") {
      const renderedValue = info.role === "combobox" ? reactSelectSingleValue(element) : "";
      value = attr(element, "aria-valuetext") || attr(element, "aria-valuenow") || (selectedValue && selectedValue.value) || renderedValue || exactElementText(element, "control_value") || "";
    }
    return { value: value == null ? null : exactString(value, "control_value"), present: value != null && String(value) !== "" };
  }

  function selectedFor(info, options) {
    if (!["select", "combobox", "listbox"].includes(info.kind) && !["combobox", "listbox"].includes(info.role)) return null;
    return options.filter((option) => option.selected).map((option) => option.value);
  }

  function validityFor(element, ids) {
    let nativeValid = true;
    try {
      if (element.validity) nativeValid = Boolean(element.validity.valid);
    } catch (_) {
      nativeValid = true;
    }
    const ariaValue = attr(element, "aria-invalid");
    const normalizedAriaValue = ariaValue == null ? null : ariaValue.trim().toLowerCase();
    const ariaInvalid = normalizedAriaValue === "false"
      ? false
      : ["true", "1", "grammar", "spelling"].includes(normalizedAriaValue) ? true : null;
    let message = null;
    try { message = text(element.validationMessage); } catch (_) { message = null; }
    if (!message) message = referencedText(element, "aria-errormessage", ids).join(" ") || null;
    if (!message && (ariaInvalid === true || nativeValid === false)) message = referencedText(element, "aria-describedby", ids).join(" ") || null;
    return { valid: nativeValid && ariaInvalid !== true, aria_invalid: ariaInvalid, message: message || null };
  }

  function fileFor(element, info) {
    if (info.type !== "file") return null;
    const accept = (attr(element, "accept") || "").split(",").map((part) => text(part, MAX_LOCATOR_CHARS)).filter(Boolean);
    const names = [];
    let count = 0;
    try {
      count = Number(element.files && element.files.length) || 0;
      if (count > MAX_FILE_NAMES) throw new Error("observer_file_limit_exceeded");
      for (const file of Array.from(element.files || [])) {
        const name = text(file && file.name, MAX_FILE_NAME_CHARS);
        if (name) names.push(name.split(/[\\/]/).pop());
      }
    } catch (error) {
      if (error && String(error.message).includes("file_limit")) throw error;
      count = 0;
    }
    if (count === 0) {
      const container = uploadContainerFor(element);
      const rendered = container ? uploadedFileForContainer(container) : null;
      if (rendered && rendered.count > 0) {
        count = rendered.count;
        names.push(...rendered.names);
      }
    }
    return { accept, count, names };
  }

  const BINARY_CHOICE_LABELS = new Set(["yes", "no"]);

  function normalizedChoiceLabel(value) {
    return String(value == null ? "" : value)
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
  }

  function binaryChoiceLabel(element, elements, ids) {
    const label = accessibleName(element, elements, ids) || elementText(element);
    const normalized = normalizedChoiceLabel(label);
    if (!BINARY_CHOICE_LABELS.has(normalized)) return null;
    return { value: normalized, label: text(label) || normalized };
  }

  function binaryChoiceState(element) {
    const raw = firstAttr(element, [
      "aria-checked",
      "aria-pressed",
      "aria-selected",
      "data-state",
      "data-selected",
      "data-checked",
    ]);
    if (raw != null) {
      const normalized = raw.trim().toLowerCase();
      if (/^(true|1|yes|on|selected|checked|active)$/.test(normalized)) return true;
      if (/^(false|0|no|off|unselected|unchecked|inactive)$/.test(normalized)) return false;
    }
    try {
      if (typeof element.checked === "boolean") return element.checked;
    } catch (_) {
      // Fall through to common selected-state class names.
    }
    const classNames = (attr(element, "class") || "").split(/\s+/);
    return classNames.some((name) => /^(?:selected|active|checked|chosen|current)$/i.test(name) ||
      /(?:^|[-_])(?:selected|active|checked|chosen|current)(?:$|[-_])/i.test(name));
  }

  function textExcludingElements(element, excluded) {
    if (!element || excluded.has(element)) return "";
    if (element.nodeType === 3) {
      try { return String(element.nodeValue || ""); } catch (_) { return ""; }
    }
    if (element.nodeType !== 1) return "";
    let children = [];
    try { children = Array.from(element.childNodes || element.children || []); } catch (_) { children = []; }
    if (!children.length) return elementText(element) || "";
    return children.map((child) => textExcludingElements(child, excluded)).join(" ");
  }

  function binaryPromptFor(container, options, ids) {
    const labelled = referencedText(container, "aria-labelledby", ids);
    if (labelled.length) return text(labelled.join(" "));
    const ariaLabel = text(attr(container, "aria-label"));
    if (ariaLabel && !BINARY_CHOICE_LABELS.has(normalizedChoiceLabel(ariaLabel))) return ariaLabel;
    const raw = text(textExcludingElements(container, new Set(options)));
    if (!raw) return null;
    const stripped = text(raw.replace(/\b(?:yes|no)\b/gi, " "));
    return stripped;
  }

  function binaryQuestionContainer(parent, options, binaryButtons, ids) {
    let current = parent;
    for (let depth = 0; current && depth < 16; depth += 1) {
      const descendants = binaryButtons.filter((button) =>
        button === current || ancestor(button, (candidate) => candidate === current));
      if (descendants.length === options.length && descendants.every((button) => options.includes(button))) {
        const prompt = binaryPromptFor(current, options, ids);
        if (prompt) {
          const tag = current.tagName && current.tagName.toLowerCase();
          const role = rawRole(current);
          if (tag === "html" || tag === "body" || tag === "form") {
            current = parentElement(current);
            continue;
          }
          const semantic = tag === "fieldset" || role === "group" || role === "radiogroup";
          const marked = Boolean(
            testIdFor(current) ||
            attr(current, "id") ||
            attr(current, "name") ||
            attr(current, "aria-labelledby") ||
            attr(current, "aria-label") ||
            hasClassToken(current, "question")
          );
          let childCount = 0;
          try { childCount = (current.children || []).length; } catch (_) { childCount = 0; }
          const questionLike = /[?？]\s*$/.test(prompt) ||
            /^(?:question|are you|do you|does |did |have you|has |can you|will you|would you|is |are |was |were )\b/i.test(prompt);
          const nestedOptions = options.some((option) => parentElement(option) !== current);
          if (semantic || marked || questionLike || nestedOptions) {
            return { container: current, prompt };
          }
        }
      }
      current = parentElement(current);
    }
    return null;
  }

  function binaryChoiceMetadata(elements, ids, frameId) {
    const binaryButtons = [];
    const byParent = new Map();
    for (const element of elements) {
      const info = semantics(element);
      if (!info || info.kind !== "button" || !isVisible(element)) continue;
      const choice = binaryChoiceLabel(element, elements, ids);
      if (!choice) continue;
      const parent = parentElement(element);
      if (!parent) continue;
      const item = { element, choice };
      binaryButtons.push(element);
      const members = byParent.get(parent) || [];
      members.push(item);
      byParent.set(parent, members);
    }
    const metadata = new Map();
    for (const [parent, members] of byParent.entries()) {
      const yes = members.filter((member) => member.choice.value === "yes");
      const no = members.filter((member) => member.choice.value === "no");
      if (yes.length !== 1 || no.length !== 1) continue;
      const options = [yes[0].element, no[0].element];
      const question = binaryQuestionContainer(parent, options, binaryButtons, ids);
      if (!question) continue;
      const groupIdentity = choiceContainerIdentity(question.container);
      const groupId = "group-" + sha256([
        frameId,
        "binary-yes-no",
        groupIdentity,
        normalizedChoiceLabel(question.prompt),
      ].join("\u0000")).slice(0, 24);
      for (const member of [yes[0], no[0]]) {
        metadata.set(member.element, {
          group_id: groupId,
          prompt: question.prompt,
          value: member.choice.value,
          label: member.choice.label,
          checked: binaryChoiceState(member.element),
        });
      }
    }
    return metadata;
  }

  function choiceGroupFor(element) {
    return ancestor(element, (candidate) => {
      const tag = candidate.tagName && candidate.tagName.toLowerCase();
      const role = rawRole(candidate);
      return tag === "fieldset" || role === "group" || role === "radiogroup";
    });
  }

  function choiceContainerIdentity(container) {
    const id = attr(container, "id");
    if (id) return "id:" + id;
    return [
      testIdFor(container) || "",
      attr(container, "name") || "",
      attr(container, "aria-labelledby") || "",
      attr(container, "aria-label") || "",
      structuralPath(container),
    ].join("\u0000");
  }

  function choiceContextFor(element) {
    const explicitGroup = choiceGroupFor(element);
    const form = explicitGroup ? null : ancestor(element, (candidate) =>
      candidate.tagName && candidate.tagName.toLowerCase() === "form");
    const container = explicitGroup || form;
    return container ? choiceContainerIdentity(container) : structuralPath(element);
  }
  function groupIdFor(element, info, frameId, label, name) {
    if (!["checkbox", "radio", "switch"].includes(info.role)) return null;
    const explicitGroup = choiceGroupFor(element);
    const form = explicitGroup || !name ? null : ancestor(element, (candidate) =>
      candidate.tagName && candidate.tagName.toLowerCase() === "form");
    const container = explicitGroup || form;
    const groupIdentity = container ? choiceContainerIdentity(container) : "";
    const fallback = name || explicitGroup ? "" : (label || structuralPath(element));
    const key = [frameId, info.role, name || "", groupIdentity, fallback].join("\u0000");
    return "group-" + sha256(key).slice(0, 24);
  }


  function insideForm(element) {
    return Boolean(ancestor(element, (candidate) => candidate.tagName && candidate.tagName.toLowerCase() === "form"));
  }
  function candidateFor(element, info, label, verifiedChoice = false) {
    const normalized = (label || attr(element, "title") || "").toLowerCase().replace(/\s+/g, " ").trim();
    const finalPattern = /\b(submit|send application|complete application|finish application|finali[sz]e|confirm and apply|submit application)\b/i;
    const navigationPattern = /\b(apply|start application|begin application|continue|next|proceed|review|go to application|begin|get started)\b/i;
    const helperPattern = /^(?:toggle flyout|attach|dropbox|google drive|enter manually|add another|remove file|clear selections|remove file|upload file|delete file|replace)$/i;
    const submitType = info.type === "submit" || info.type === "image";
    if (info.type === "file"
        && ancestor(element, (candidate) => hasClassToken(candidate, "ashby-application-form-autofill-input-root"))) {
      return { class: "non_final_navigation", reason: "optional browser autofill uploader helper" };
    }
    if (finalPattern.test(normalized)) {
      return { class: "final_candidate", reason: "visible control has a final-submission label or submit type" };
    }
    const fieldLike = !["button", "navigation"].includes(info.kind) && info.role !== "button";
    if (fieldLike || verifiedChoice) {
      const reason = info.role === "checkbox" || info.role === "radio" || info.role === "switch"
        ? (verifiedChoice ? "visible paired Yes/No choice control" : "visible user-facing choice control")
        : "visible user-facing field control";
      return { class: "field", reason };
    }
    if (submitType && insideForm(element) && /\bapply\b/i.test(normalized)) {
      return { class: "final_candidate", reason: "visible control has a final-submission label or submit type" };
    }
    if (navigationPattern.test(normalized) || helperPattern.test(normalized)) {
      return { class: "non_final_navigation", reason: "visible control has an ordinary application-navigation label" };
    }
    return { class: "unknown", reason: "visible user-facing control has no recognized field or navigation label" };
  }

  function choiceIdentity(element, info, label, identityLabel = null) {
    if (!["radio", "checkbox", "switch"].includes(info.role)) return null;
    const value = text(attr(element, "value"), MAX_LOCATOR_CHARS);
    if (value) return "value:" + value;
    const ariaValue = text(
      firstAttr(element, ["aria-label", "aria-valuetext"]),
      MAX_LOCATOR_CHARS,
    );
    if (ariaValue) return "aria:" + ariaValue;
    const labelValue = text(identityLabel || label, MAX_LOCATOR_CHARS);
    return labelValue ? "label:" + labelValue : null;
  }


  function appendChoiceIdentity(parts, optionIdentity, element) {
    parts.push("choice", optionIdentity, "group", choiceContextFor(element));
  }

  function fileIdentityElement(element, identityElement) {
    const candidates = [element, identityElement];
    for (const candidate of candidates) {
      if (!candidate) continue;
      const tag = candidate.tagName && candidate.tagName.toLowerCase();
      if (tag === "input" && text(attr(candidate, "type"), MAX_LOCATOR_CHARS).toLowerCase() === "file"
          && (testIdFor(candidate) || attr(candidate, "id") || attr(candidate, "name") || attr(candidate, "aria-labelledby"))) {
        return candidate;
      }
      const children = candidate.children;
      if (!children) continue;
      for (const child of children) {
        const childTag = child && child.tagName && child.tagName.toLowerCase();
        if (childTag === "input" && text(attr(child, "type"), MAX_LOCATOR_CHARS).toLowerCase() === "file"
            && (testIdFor(child) || attr(child, "id") || attr(child, "name") || attr(child, "aria-labelledby"))) {
          return child;
        }
      }
    }
    return null;
  }

  function stableIdFor(element, info, frameId, label, name, identityElement = null, identityLabel = null) {
    const source = info.type === "file"
      ? (fileIdentityElement(element, identityElement) || identityElement || uploadContainerFor(element) || element)
      : (identityElement || element);
    const testId = testIdFor(source);
    const id = attr(source, "id");
    const sourceName = text(attr(source, "name"), MAX_LOCATOR_CHARS);
    const labelledBy = info.type === "file"
      ? text(attr(source, "aria-labelledby"), MAX_LOCATOR_CHARS)
      : null;
    const optionIdentity = info.type === "file" ? null : choiceIdentity(element, info, label, identityLabel);
    const parts = info.type === "file"
      ? [frameId, "upload", "file"]
      : [frameId, info.kind, info.tag, info.type || "", info.role || ""];
    if (testId) {
      parts.push("test_id", testId);
      if (id) {
        parts.push("id", id);
      } else if (sourceName) {
        parts.push("name", sourceName);
        if (optionIdentity) appendChoiceIdentity(parts, optionIdentity, element);
        else parts.push("path", structuralPath(source));
      } else if (labelledBy) {
        parts.push("labelledby", labelledBy);
        parts.push("path", structuralPath(source));
      } else if (optionIdentity) {
        appendChoiceIdentity(parts, optionIdentity, element);
      } else {
        parts.push("path", structuralPath(source));
      }
    } else if (id) {
      parts.push("id", id);
    } else if (sourceName) {
      parts.push("name", sourceName);
      if (optionIdentity) appendChoiceIdentity(parts, optionIdentity, element);
      else parts.push("path", structuralPath(source));
    } else if (labelledBy) {
      parts.push("labelledby", labelledBy);
      parts.push("path", structuralPath(source));
    } else if (optionIdentity) {
      appendChoiceIdentity(parts, optionIdentity, element);
    } else {
      parts.push("path", structuralPath(source));
    }
    return "control-" + sha256(parts.join("\u0000")).slice(0, 24);
  }

  function controlFor(element, info, frameId, elements, ids, index, observationId, binaryChoice = null) {
    const label = binaryChoice?.prompt || accessibleName(element, elements, ids);
    const optionLabel = binaryChoice?.label || null;
    const name = text(attr(element, "name"), MAX_LOCATOR_CHARS);
    const description = descriptionFor(element, ids, label);
    const checked = binaryChoice ? binaryChoice.checked : checkedFor(element, info);
    const options = optionsFor(element, info, elements, ids);
    const state = binaryChoice
      ? { value: binaryChoice.value, present: checked === true }
      : valueFor(element, info, options, checked, label);
    const selected = selectedFor(info, options);
    const validity = validityFor(element, ids);
    const file = fileFor(element, info);
    const disabled = boolProperty(element, "disabled", "aria-disabled");
    const readonly = boolProperty(element, "readOnly", "aria-readonly");
    const required = boolProperty(element, "required", "aria-required");
    const candidate = candidateFor(element, info, label, binaryChoice !== null);
    const stableId = stableIdFor(element, info, frameId, label, name, null, binaryChoice?.value || optionLabel);
    return {
      ref: observationId + ":control-" + index.toString(36),
      stable_id: stableId,
      group_id: binaryChoice?.group_id || groupIdFor(element, info, frameId, label, name),
      kind: info.kind,
      tag: info.tag,
      type: info.type,
      role: info.role,
      label: label || null,
      name: name || null,
      description: description || null,
      locator: locatorFor(element, info.role, optionLabel || label, name),
      frame_id: frameId,
      visible: true,
      enabled: !disabled,
      required,
      readonly,
      disabled,
      value: state.value,
      value_present: state.present,
      checked,
      selected,
      options,
      validity,
      file,
      candidate,
    };
  }
  function uploadedFileForContainer(container) {
    const filenameElement = descendantWithClass(container, "file-upload__filename")
      || (container.querySelector && container.querySelector('[title="Delete file"]')?.parentElement?.querySelector("span"));
    const filename = text(elementText(filenameElement), MAX_FILE_NAME_CHARS);
    const name = filename ? filename.split(/[\\/]/).pop() : null;
    return { accept: [], count: name ? 1 : 0, names: name ? [name] : [] };
  }

  function uploadControlFor(container, frameId, elements, ids, index, observationId) {
    const info = { kind: "input", tag: "input", type: "file", role: "textbox", native: false };
    const label = accessibleName(container, elements, ids);
    const name = text(attr(container, "name"), MAX_LOCATOR_CHARS);
    const description = descriptionFor(container, ids, label);
    const file = uploadedFileForContainer(container);
    const disabled = boolProperty(container, "disabled", "aria-disabled");
    const required = boolProperty(container, "required", "aria-required");
    const stableId = stableIdFor(container, info, frameId, label, name, container);
    return {
      ref: observationId + ":control-" + index.toString(36),
      stable_id: stableId,
      group_id: null,
      kind: info.kind,
      tag: info.tag,
      type: info.type,
      role: info.role,
      label: label || null,
      name: name || null,
      description: description || null,
      locator: locatorFor(container, info.role, label, name),
      frame_id: frameId,
      visible: true,
      enabled: !disabled,
      required,
      readonly: false,
      disabled,
      value: null,
      value_present: file.count > 0,
      checked: null,
      selected: null,
      options: [],
      validity: { valid: true, aria_invalid: null, message: null },
      file,
      candidate: candidateFor(container, info, label),
    };
  }


  function metadataFor(element) {
    return [
      attr(element, "id"), attr(element, "name"), attr(element, "class"), attr(element, "title"),
      attr(element, "aria-label"), attr(element, "src"), attr(element, "data-sitekey"),
      attr(element, "data-size"), attr(element, "data-badge"), attr(element, "aria-hidden"),
    ].filter(Boolean).join(" ");
  }

  function highSignalTexts(document, elements) {
    const output = [];
    if (document.title) output.push(text(document.title, MAX_TEXT_CHARS));
    for (const element of elements) {
      if (!isVisible(element)) continue;
      const tag = element.tagName ? element.tagName.toLowerCase() : "";
      const role = rawRole(element);
      if (/^h[1-6]$/.test(tag) || ["alert", "status", "dialog"].includes(role) || ["legend", "summary"].includes(tag)) {
        const value = elementText(element);
        if (value) output.push(value);
      }
    }
    return output.filter(Boolean);
  }

  function addBlocker(blockers, code, frameId) {
    if (blockers.some((blocker) => blocker.code === code && blocker.frame_id === frameId)) return;
    if (blockers.length >= MAX_BLOCKERS) throw new Error("observer_blocker_limit_exceeded");
    const labels = {
      authentication: "Visible authentication UI",
      captcha: "Visible CAPTCHA or anti-bot challenge",
      assessment: "Visible assessment or integrity challenge",
      access_control: "Visible access-control UI",
      inaccessible_frame: "Visible inaccessible frame",
    };
    blockers.push({ code, label: labels[code] || "Visible access-control UI", frame_id: frameId, visible: true });
  }

  function isInactiveInvisibleCaptchaFrame(element, marker) {
    const captcha = /(?:captcha|recaptcha|hcaptcha|turnstile|not a robot)/i.test(marker);
    if (!captcha) return false;
    if (/(?:size\s*=\s*invisible|badge\s*=\s*(?:bottomright|bottomleft|inline)|(?:^|[\s_-])invisible(?:[\s_-]|$)|(?:grecaptcha|recaptcha)[-_ ]?(?:badge|logo))/i.test(marker)) {
      return true;
    }
    try {
      const rect = element.getBoundingClientRect();
      return (rect.width <= 2 || rect.height <= 2) && /(?:badge|anchor|api2[\\/]anchor)/i.test(marker);
    } catch (_) {
      return false;
    }
  }

  function detectBlockers(document, elements, controls, frameId, blockers) {
    const signals = highSignalTexts(document, elements);
    const signalText = signals.join(" ");
    const password = controls.some((control) => control.tag === "input" && control.type === "password");
    const authControl = controls.some((control) => {
      if (!insideForm(control.__element)) return false;
      return /\b(sign[ -]?in|log[ -]?in|login|authenticate)\b/i.test(control.label || "");
    });
    if (password || authControl || /\b(authentication required|account required|session expired|verify (?:your )?identity)\b/i.test(signalText)) {
      addBlocker(blockers, "authentication", frameId);
    }
    const captcha = elements.some((element) => {
      if (!isVisible(element)) return false;
      const tag = element.tagName ? element.tagName.toLowerCase() : "";
      const metadata = metadataFor(element);
      if (/(?:captcha|recaptcha|hcaptcha|turnstile|i.?m not a robot|not a robot)/i.test(metadata)) {
        if (/(?:grecaptcha|recaptcha)[-_ ]?(?:badge|logo|error)|size\s*=\s*invisible|badge\s*=\s*(?:bottomright|bottomleft|inline)|(?:^|[\s_-])invisible(?:[\s_-]|$)/i.test(metadata)) return false;
        try {
          const rect = element.getBoundingClientRect();
          if (rect.width <= 2 || rect.height <= 2) return false;
        } catch (_) {
          // Keep a metadata-positive challenge when geometry is unavailable.
        }
        return true;
      }
      if (["html", "body", "main", "section", "article", "div"].includes(tag)) return false;
      return /(?:captcha|recaptcha|hcaptcha|turnstile|i.?m not a robot|not a robot)/i.test(elementText(element) || "");
    });
    if (captcha || /\b(captcha|recaptcha|hcaptcha|turnstile)\b/i.test(signalText)) addBlocker(blockers, "captcha", frameId);
    if (/\b(assessment|coding challenge|integrity check|skills? test|hackerrank|codility)\b/i.test(signalText)) addBlocker(blockers, "assessment", frameId);
    if (/\b(access denied|forbidden|unauthori[sz]ed|permission denied|not authorized|restricted access|security check)\b/i.test(signalText)) addBlocker(blockers, "access_control", frameId);
  }

  function inspectWindow(win, parentId, path, frameElement, frames, controls, blockers, seenWindows, observationId) {
    let document;
    try { document = win.document; } catch (_) { return null; }
    if (!document || seenWindows.has(win)) return null;
    seenWindows.add(win);
    const currentUrl = (() => {
      try { return urlText(win.location.href) || "about:blank"; } catch (_) { return frameUrl(frameElement, "about:blank") || "about:blank"; }
    })();
    const frameId = frameElement ? frameIdentity(parentId, frameElement, path) : "top";
    if (frames.length >= MAX_FRAMES) throw new Error("observer_frame_limit_exceeded");
    const frame = { id: frameId, parent_id: parentId || null, url: currentUrl, origin: originFor(currentUrl), accessible: true };
    frames.push(frame);
    const elements = walkDocument(document);
    const ids = idMapFor(elements);
    const popupIds = controlledPopupIds(elements);
    const binaryChoices = binaryChoiceMetadata(elements, ids, frameId);
    const start = controls.length;
    const fileWidgetIds = new Set();
    for (const element of elements) {
      const info = semantics(element);
      const choice = binaryChoices.get(element) || null;
      const effectiveInfo = choice ? { ...info, role: "radio" } : info;
      if (effectiveInfo && effectiveInfo.kind === "aria" && effectiveInfo.role === "listbox" && popupIds.has(attr(element, "id"))) continue;
      if (!effectiveInfo || honeypot(element) || !isObservableControl(element, effectiveInfo)) continue;
      if (controls.length >= MAX_CONTROLS) throw new Error("observer_control_limit_exceeded");
      const control = controlFor(element, effectiveInfo, frameId, elements, ids, controls.length, observationId, choice);
      if (info.type === "file") {
        if (fileWidgetIds.has(control.stable_id)) continue;
        fileWidgetIds.add(control.stable_id);
      }
      control.__element = element;
      controls.push(control);
    }
    for (const container of elements) {
      if (!(hasClassToken(container, "file-upload")
          || hasClassToken(container, "ashby-application-form-autofill-input-root")
          || (container.querySelector && container.querySelector(':scope > input[type="file"]')
            && (container.querySelector('[title="Delete file"]')
              || container.querySelector('button')?.textContent?.trim() === "Replace")))
          || !isVisible(container)) continue;
      const file = uploadedFileForContainer(container);
      if (file.count === 0) continue;
      const info = { kind: "input", tag: "input", type: "file", role: "textbox", native: false };
      const label = accessibleName(container, elements, ids);
      const name = text(attr(container, "name"), MAX_LOCATOR_CHARS);
      const stableId = stableIdFor(container, info, frameId, label, name, container);
      if (fileWidgetIds.has(stableId)) continue;
      if (controls.length >= MAX_CONTROLS) throw new Error("observer_control_limit_exceeded");
      const control = uploadControlFor(container, frameId, elements, ids, controls.length, observationId);
      fileWidgetIds.add(control.stable_id);
      controls.push(control);
    }
    detectBlockers(document, elements, controls.slice(start), frameId, blockers);

    const frameElements = elements.filter((element) => {
      const tag = element.tagName && element.tagName.toLowerCase();
      return tag === "iframe" || tag === "frame";
    });
    for (let index = 0; index < frameElements.length; index += 1) {
      const childElement = frameElements[index];
      if (!isVisible(childElement)) continue;
      const childPath = path.concat(index);
      const childId = frameIdentity(frameId, childElement, childPath);
      const childUrl = frameUrl(childElement, currentUrl) || "about:blank";
      let childWindow = null;
      let accessible = false;
      try {
        childWindow = childElement.contentWindow;
        if (childWindow && childWindow.document) {
          void childWindow.document.documentElement;
          accessible = true;
        }
      } catch (_) {
        accessible = false;
      }
      if (!accessible) {
        if (frames.length >= MAX_FRAMES) throw new Error("observer_frame_limit_exceeded");
        frames.push({ id: childId, parent_id: frameId, url: childUrl, origin: originFor(childUrl), accessible: false });
        const marker = metadataFor(childElement) + " " + (attr(childElement, "title") || "");
        const captchaFrame = /(?:captcha|recaptcha|hcaptcha|turnstile|not a robot)/i.test(marker);
        const invisibleCaptchaFrame = isInactiveInvisibleCaptchaFrame(childElement, marker);
        if (!invisibleCaptchaFrame) addBlocker(blockers, "inaccessible_frame", childId);
        if (captchaFrame && !invisibleCaptchaFrame) addBlocker(blockers, "captcha", frameId);
        if (/(?:login|sign[ -]?in|auth|password)/i.test(marker)) addBlocker(blockers, "authentication", frameId);
        if (/(?:assessment|hackerrank|codility|coding challenge)/i.test(marker)) addBlocker(blockers, "assessment", frameId);
        if (/(?:access.denied|forbidden|unauthori[sz]ed|permission)/i.test(marker)) addBlocker(blockers, "access_control", frameId);
        continue;
      }
      inspectWindow(childWindow, frameId, childPath, childElement, frames, controls, blockers, seenWindows, observationId);
    }
    return frame;
  }

  function normalizeChoiceGroupValidity(controls) {
    const groups = Object.create(null);
    for (const control of controls) {
      const checkboxArray = control.role === "checkbox" &&
        typeof control.name === "string" && control.name.endsWith("[]");
      if (!control.group_id || (control.role !== "radio" && !checkboxArray)) continue;
      const key = control.role + "\u0000" + control.group_id;
      if (!groups[key]) groups[key] = [];
      groups[key].push(control);
    }
    const isNativeRequiredError = (control) => {
      try {
        return Boolean(control.__element && control.__element.validity &&
          control.__element.validity.valueMissing &&
          !control.__element.validity.customError);
      } catch (_) {
        return false;
      }
    };
    for (const key of Object.keys(groups)) {
      const members = groups[key];
      if (!members.some((member) => !member.disabled && member.checked === true)) continue;
      const checkboxArray = members[0].role === "checkbox";
      if (checkboxArray && (members.length < 2 ||
          !members.every((member) => member.validity && member.validity.aria_invalid === false))) {
        continue;
      }
      const invalidUnchecked = members.filter((control) =>
        control.required && control.checked === false && control.validity && !control.validity.valid);
      if (checkboxArray && invalidUnchecked.some((control) => !isNativeRequiredError(control))) continue;
      for (const control of invalidUnchecked) {
        if (control.validity.aria_invalid === true || !isNativeRequiredError(control)) continue;
        control.validity.valid = true;
        control.validity.message = null;
      }
    }
  }

  function nonce() {
    try {
      const values = new Uint32Array(2);
      crypto.getRandomValues(values);
      return values[0].toString(36) + values[1].toString(36);
    } catch (_) {
      return String(Date.now()) + String(Math.random()).slice(2);
    }
  }

  const previousObservationId = (() => {
    try {
      const prior = typeof __omp_phase1_previous_observation_id_v1 === "string"
        ? __omp_phase1_previous_observation_id_v1
        : null;
      return prior && prior.length <= MAX_LOCATOR_CHARS ? prior : null;
    } catch (_) {
      return null;
    }
  })();
  const observationId = "observation-" + Date.now().toString(36) + "-" + nonce();
  const frames = [];
  const controls = [];
  const blockers = [];
  const seenWindows = new WeakSet();
  inspectWindow(window, null, [], null, frames, controls, blockers, seenWindows, observationId);

  normalizeChoiceGroupValidity(controls);
  for (const control of controls) delete control.__element;
  blockers.sort((left, right) => (left.frame_id + "\u0000" + left.code).localeCompare(right.frame_id + "\u0000" + right.code));
  const currentUrl = frames.length ? frames[0].url : urlText(document.location.href) || "about:blank";
  const title = text(document.title) || "";
  const normalized = { frames, controls, blockers, title, url: currentUrl };
  const snapshotSha256 = sha256(canonical(normalized));
  const observation = {
    schema: "phase1-observation-v1",
    observation_id: observationId,
    previous_observation_id: previousObservationId,
    observed_at: new Date().toISOString(),
    url: currentUrl,
    title,
    snapshot_sha256: snapshotSha256,
    frames,
    controls,
    blockers,
  };
  return observation;
})()
