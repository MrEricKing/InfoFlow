const BLOCKED_TAGS = new Set([
  'script',
  'style',
  'iframe',
  'object',
  'embed',
  'form',
  'input',
  'button',
  'textarea',
  'select',
  'option',
  'meta',
  'link',
  'base',
]);

const ALLOWED_TAGS = new Set([
  'a',
  'article',
  'blockquote',
  'br',
  'code',
  'div',
  'em',
  'figcaption',
  'figure',
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'hr',
  'i',
  'img',
  'li',
  'ol',
  'p',
  'pre',
  's',
  'section',
  'span',
  'strong',
  'sub',
  'sup',
  'u',
  'ul',
]);

const URL_ATTRS = new Set(['href', 'src']);
const HTML_CONTENT_PATTERN =
  /<\/?(?:a|article|blockquote|br|code|div|em|figcaption|figure|h[1-6]|hr|i|img|li|ol|p|pre|s|section|span|strong|sub|sup|table|tbody|td|th|thead|tr|u|ul)\b/i;

export function sanitizeRichHtml(raw: string) {
  if (!raw.trim()) {
    return '';
  }

  const container = createContainer(raw);
  sanitizeNode(container);
  return container.innerHTML;
}

export function renderReaderContentHtml(raw: string) {
  if (!raw.trim()) {
    return '';
  }

  if (HTML_CONTENT_PATTERN.test(raw)) {
    return sanitizeRichHtml(raw);
  }

  return renderPlainTextHtml(raw);
}

export function extractPlainText(raw: string) {
  if (!raw.trim()) {
    return '';
  }

  const container = createContainer(raw);
  const text = container.textContent ?? '';
  return text.replace(/\s+/g, ' ').trim();
}

function renderPlainTextHtml(raw: string) {
  const normalized = decodeHtmlEntities(raw).replace(/\r\n?/g, '\n').trim();
  const paragraphs = normalized
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean);

  return paragraphs.map((paragraph) => `<p>${escapeHtml(paragraph)}</p>`).join('');
}

function decodeHtmlEntities(raw: string) {
  const documentRef = document.implementation.createHTMLDocument('');
  const textarea = documentRef.createElement('textarea');
  textarea.innerHTML = raw;
  return textarea.value;
}

function escapeHtml(raw: string) {
  return raw
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function createContainer(raw: string) {
  const documentRef = document.implementation.createHTMLDocument('');
  const container = documentRef.createElement('div');
  container.innerHTML = raw;
  return container;
}

function sanitizeNode(root: ParentNode) {
  const children = [...root.childNodes];

  for (const child of children) {
    if (child.nodeType === Node.COMMENT_NODE) {
      child.parentNode?.removeChild(child);
      continue;
    }

    if (child.nodeType !== Node.ELEMENT_NODE) {
      continue;
    }

    const element = child as HTMLElement;
    const tagName = element.tagName.toLowerCase();

    if (BLOCKED_TAGS.has(tagName)) {
      element.remove();
      continue;
    }

    if (!ALLOWED_TAGS.has(tagName)) {
      element.replaceWith(...Array.from(element.childNodes));
      sanitizeNode(root);
      continue;
    }

    sanitizeAttributes(element, tagName);
    sanitizeNode(element);
  }
}

function sanitizeAttributes(element: HTMLElement, tagName: string) {
  for (const attr of [...element.attributes]) {
    const name = attr.name.toLowerCase();
    const value = attr.value.trim();

    if (name.startsWith('on') || name === 'style' || name === 'srcset') {
      element.removeAttribute(attr.name);
      continue;
    }

    if (tagName === 'a') {
      if (name === 'href') {
        if (!isSafeUrl(value)) {
          element.removeAttribute(attr.name);
        }
        continue;
      }

      if (name !== 'title') {
        element.removeAttribute(attr.name);
      }
      continue;
    }

    if (tagName === 'img') {
      if (name === 'src') {
        if (!isSafeUrl(value)) {
          element.removeAttribute(attr.name);
        }
        continue;
      }

      if (name !== 'alt' && name !== 'title') {
        element.removeAttribute(attr.name);
      }
      continue;
    }

    if (URL_ATTRS.has(name)) {
      element.removeAttribute(attr.name);
      continue;
    }

    element.removeAttribute(attr.name);
  }

  if (tagName === 'a' && element.getAttribute('href')) {
    element.setAttribute('target', '_blank');
    element.setAttribute('rel', 'noopener noreferrer');
  }

  if (tagName === 'img' && element.getAttribute('src')) {
    element.setAttribute('loading', 'lazy');
    element.setAttribute('referrerpolicy', 'no-referrer');
  }
}

function isSafeUrl(value: string) {
  if (!value) {
    return false;
  }

  if (value.startsWith('#') || value.startsWith('/')) {
    return true;
  }

  try {
    const parsed = new URL(value, window.location.origin);
    return ['http:', 'https:', 'mailto:'].includes(parsed.protocol);
  } catch {
    return false;
  }
}
