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
  const selfUrl = `${site}${base}/rss.xml`;
  const lastBuild = new Date().toUTCString();

  const items = stories
    .map(({ story }) => {
      const link = `${site}${base}/news/${story.id}/`;
      const pubDate = new Date(story.published_at);
      const categories = story.regions.map(
        (r) => `      <category>${esc(r)}</category>`,
      );
      return [
        '    <item>',
        `      <title>${esc(story.title)}</title>`,
        `      <link>${esc(link)}</link>`,
        `      <guid isPermaLink="true">${esc(link)}</guid>`,
        ...(Number.isNaN(pubDate.getTime()) ? [] : [`      <pubDate>${pubDate.toUTCString()}</pubDate>`]),
        `      <description>${esc(story.summary)}</description>`,
        ...categories,
        '    </item>',
      ].join('\n');
    })
    .join('\n');

  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>DataCenter Atlas</title>
    <link>${esc(`${site}${base}/`)}</link>
    <atom:link href="${esc(selfUrl)}" rel="self" type="application/rss+xml" />
    <description>Daily data center intelligence across Asia-Pacific</description>
    <language>en</language>
    <lastBuildDate>${lastBuild}</lastBuildDate>
${items}
  </channel>
</rss>
`;

  return new Response(xml, {
    headers: { 'Content-Type': 'application/rss+xml; charset=utf-8' },
  });
};
