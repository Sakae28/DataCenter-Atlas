import { marked } from 'marked';

export interface TocEntry {
  id: string;
  text: string;
  depth: 2 | 3;
}

export interface RenderedArticle {
  html: string;
  toc: TocEntry[];
}

marked.use({ gfm: true, breaks: false });

function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

/**
 * Render story `content` (markdown from the pipeline's extraction step) to
 * HTML for the detail page. Older plain-text content still renders fine:
 * marked treats blank-line-separated text as plain paragraphs.
 *
 * Also post-processes the HTML string to give every h2/h3 a stable id and
 * collect the entries for the "Contents" rail.
 */
export function renderArticle(markdown: string): RenderedArticle {
  let html = marked.parse(markdown, { async: false });

  // Minimal sanitization: content is scraper/extraction output and therefore
  // untrusted. marked passes raw HTML through, so strip active-content tags.
  // (Deliberately a small regex strip, not a full HTML sanitizer — the output
  // is build-time static HTML under our control.)
  html = html.replace(/<\/?(script|iframe|object|embed)(\s[^>]*)?>/gi, '');

  const toc: TocEntry[] = [];
  const used = new Map<string, number>();
  html = html.replace(/<(h[23])>([\s\S]*?)<\/\1>/gi, (match, tag: string, inner: string) => {
    const text = inner.replace(/<[^>]+>/g, '').trim();
    const base = slugify(text) || 'section';
    const seen = used.get(base) ?? 0;
    used.set(base, seen + 1);
    const id = seen === 0 ? base : `${base}-${seen}`;
    toc.push({ id, text, depth: tag.toLowerCase() === 'h2' ? 2 : 3 });
    return `<${tag} id="${id}">${inner}</${tag}>`;
  });

  return { html, toc };
}
