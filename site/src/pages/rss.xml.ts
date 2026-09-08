import type { APIRoute } from 'astro';

import { loadAllStories } from '../lib/news';

/**
 * RSS 2.0 feed of the latest digest stories (newest 100). Hand-rolled XML —
 * the site has no XML dependency and the feed is simple enough to keep it
 * that way. Links are absolute: context.site (astro.config "site") + base.
 */
const MAX_ITEMS = 100;

const esc = (s: string) =>
  s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

export const GET: APIRoute = (context) => {
  const site = (context.site ?? new URL('https://sakae28.github.io')).origin;
  const base = import.meta.env.BASE_URL;
  const stories = loadAllStories().slice(0, MAX_ITEMS);

  const items = stories
    .map(({ story }) => {
      const link = `${site}${base}/news/${story.id}/`;
      const pubDate = new Date(story.published_at);
      return [
        '    <item>',
        `      <title>${esc(story.title)}</title>`,
        `      <link>${esc(link)}</link>`,
        `      <guid isPermaLink="true">${esc(link)}</guid>`,
        ...(Number.isNaN(pubDate.getTime()) ? [] : [`      <pubDate>${pubDate.toUTCString()}</pubDate>`]),
        `      <description>${esc(story.summary)}</description>`,
        '    </item>',
      ].join('\n');
    })
    .join('\n');

  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>DataCenter Atlas</title>
    <link>${esc(`${site}${base}/`)}</link>
    <description>Daily data center intelligence across Asia-Pacific</description>
    <language>en</language>
${items}
  </channel>
</rss>
`;

  return new Response(xml, {
    headers: { 'Content-Type': 'application/rss+xml; charset=utf-8' },
  });
};
