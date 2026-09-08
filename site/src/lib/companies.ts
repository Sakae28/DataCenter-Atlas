import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

import { loadProjects, type Project } from './projects';
import curatedData from '../data/companies.json';

/**
 * Company directory aggregation. Stats are computed from projects.json at
 * build time (zero-maintenance); companies.json only carries the curated
 * layer — display name, logo domain, category, one-line profile — for the
 * operators worth a polished card. Long-tail operators (< MIN_FACILITIES)
 * stay off the directory page entirely.
 */
export interface CompanyMeta {
  name?: string;
  domain?: string;
  category?: string;
  blurb?: string;
}

export interface CompanyStats {
  facilities: number;
  operational: number;
  /** announced + under_construction */
  planned: number;
  countries: number;
  markets: number;
  /** Tracked MW, leased tenant deployments excluded. */
  mw: number;
}

export interface Company {
  name: string;
  slug: string;
  /** Raw operator name from projects.json (differs from `name` when the
      curated meta overrides the display name, e.g. "VNET Group, Inc."). */
  operator: string;
  domain: string | null;
  category: string;
  blurb: string | null;
  stats: CompanyStats;
  projects: Project[];
}

const CURATED = curatedData as Record<string, CompanyMeta>;

const MIN_FACILITIES = 10;

// Build-time memo: every project/company/news page calls this during a
// static build.
let companiesCache: Company[] | null = null;

export const CATEGORY_ORDER = [
  'Hyperscale & Cloud',
  'Colocation',
  'Telco & Carrier',
  'Developer & Investor',
  'Other operators',
];

export function slugify(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}

export function loadCompanies(): Company[] {
  if (companiesCache) return companiesCache;
  const { projects } = loadProjects();
  const byOperator = new Map<string, Project[]>();
  for (const p of projects) {
    const list = byOperator.get(p.operator) ?? [];
    list.push(p);
    byOperator.set(p.operator, list);
  }
  const companies: Company[] = [];
  for (const [operator, plist] of byOperator) {
    const meta = CURATED[operator];
    if (!meta && plist.length < MIN_FACILITIES) continue;
    const stats: CompanyStats = {
      facilities: plist.length,
      operational: plist.filter((p) => p.status === 'operational').length,
      planned: plist.filter(
        (p) => p.status === 'announced' || p.status === 'under_construction',
      ).length,
      countries: new Set(plist.map((p) => p.country)).size,
      markets: new Set(plist.map((p) => `${p.country}${p.city}`)).size,
      mw: Math.round(
        plist.reduce((sum, p) => sum + (p.leased_from ? 0 : (p.capacity_mw ?? 0)), 0),
      ),
    };
    companies.push({
      name: meta?.name ?? operator,
      slug: slugify(operator),
      operator,
      domain: meta?.domain ?? null,
      category: meta?.category ?? 'Other operators',
      blurb: meta?.blurb ?? null,
      stats,
      projects: [...plist].sort((a, b) => (b.capacity_mw ?? 0) - (a.capacity_mw ?? 0)),
    });
  }
  companies.sort(
    (a, b) => b.stats.facilities - a.stats.facilities || a.name.localeCompare(b.name),
  );
  companiesCache = companies;
  return companies;
}

/** Lowercased display name OR raw operator name → slug, for linking story
    company chips and project operators to company directory pages. */
export function companySlugMap(): Map<string, string> {
  const map = new Map<string, string>();
  for (const c of loadCompanies()) {
    map.set(c.name.toLowerCase(), c.slug);
    map.set(c.operator.toLowerCase(), c.slug);
  }
  return map;
}

const LOGO_DIR = fileURLToPath(new URL('../../public/logos', import.meta.url));

/** Self-hosted logo under public/logos/<domain>.{png,svg,jpg} — fetched once
    and committed (Clearbit's logo API shut down in Dec 2025). Resolved at
    build time; null when we have no logo — the card shows a letter avatar. */
export function logoUrl(domain: string): string | null {
  for (const ext of ['png', 'svg', 'jpg']) {
    if (existsSync(join(LOGO_DIR, `${domain}.${ext}`))) {
      return `${import.meta.env.BASE_URL}/logos/${domain}.${ext}`;
    }
  }
  return null;
}
