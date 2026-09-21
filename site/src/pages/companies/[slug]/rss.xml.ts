import type { APIRoute, GetStaticPaths } from 'astro';

import { loadCompanies } from '../../../lib/companies';
import { loadAllStories } from '../../../lib/news';

/**
 * Per-company RSS feed: stories tagged with the company, newest 50.
 * Gives followers real notifications through any RSS reader (many readers
 * bridge to email/push). Linked from each company detail page.
 */

const MAX_ITEMS = 50;

const esc = (s: string) =>
  s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

export const getStaticPaths: GetStaticPaths = () =>
  loadCompanies().map((company) => ({
    params: { slug: company.slug },
    props: { company },
  }));

export const GET: APIRoute = (context) => {
  const { company } = context.props as { company: ReturnType<typeof loadCompanies>[number] };
  const site = (context.site ?? new URL('https://sakae28.github.io')).origin;
  const base = import.meta.env.BASE_URL;
  const names = new Set([company.name.toLowerCase(), company.operator.toLowerCase()]);
  const stories = loadAllStories()
    .filter(({ story }) => (story.companies ?? []).some((c) => names.has(c.toLowerCase())))
    .slice(0, MAX_ITEMS);
  const selfUrl = `${site}${base}/companies/${company.slug}/rss.xml`;

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
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>${esc(company.name)} · DataCenter Atlas</title>
    <link>${esc(`${site}${base}/companies/${company.slug}/`)}</link>
    <atom:link href="${esc(selfUrl)}" rel="self" type="application/rss+xml" />
    <description>News mentioning ${esc(company.name)} — tracked by DataCenter Atlas</description>
    <language>en</language>
    <lastBuildDate>${new Date().toUTCString()}</lastBuildDate>
${items}
  </channel>
</rss>
`;

  return new Response(xml, {
    headers: { 'Content-Type': 'application/rss+xml; charset=utf-8' },
  });
};
