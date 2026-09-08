import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { z } from 'zod';

import { REGIONS, type Region } from './news';

// Projects are only tracked in the five APAC regions ("global" is news-only).
export const PROJECT_REGIONS = REGIONS.filter((r) => r !== 'global') as Exclude<Region, 'global'>[];
export type ProjectRegion = (typeof PROJECT_REGIONS)[number];

export const PROJECT_STATUSES = [
  'announced',
  'under_construction',
  'operational',
  'on_hold',
  'cancelled',
] as const;

export type ProjectStatus = (typeof PROJECT_STATUSES)[number];

/** Display metadata per status. Class names map to .badge.status-* rules in global.css. */
export const STATUS_META: Record<ProjectStatus, { label: string; className: string }> = {
  announced: { label: 'Announced', className: 'status-announced' },
  under_construction: { label: 'Under construction', className: 'status-under-construction' },
  operational: { label: 'Operational', className: 'status-operational' },
  on_hold: { label: 'On hold', className: 'status-on-hold' },
  cancelled: { label: 'Cancelled', className: 'status-cancelled' },
};

/**
 * One coherent stage display derived from `status` + the source's verbatim
 * `stage_detail` + `expansion_planned`. The source label is never shown as-is:
 * it only surfaces when it adds real information on top of the normalized
 * status — as a qualifier chip ("Expanding", "Land bank") or, for a retired
 * site, as a primary-label override ("Decommissioned"). Synonyms (Planned ≈
 * Announced, Construction ≈ Under construction, Withdrawn ≈ Cancelled, In
 * Doubt ≈ On hold) and losing conflicts are swallowed, so the UI can never
 * show two contradictory stage labels side by side.
 */
export function stageDisplay(p: {
  status: ProjectStatus;
  stage_detail?: string | null;
  expansion_planned?: boolean | null;
}): { label: string; className: string; qualifier: string | null } {
  const meta = STATUS_META[p.status];
  const sd = (p.stage_detail ?? '').trim().toLowerCase();

  if (p.status === 'cancelled' && sd === 'decommissioned') {
    // Was operational, then retired — genuinely different from "never built".
    return { label: 'Decommissioned', className: meta.className, qualifier: null };
  }
  if (
    p.status === 'operational' &&
    (p.expansion_planned ||
      sd === 'prospective expansion' || sd === 'planned' ||
      sd === 'construction' || sd === 'land bank')
  ) {
    // Live site with a planned/under-way expansion — the most valuable signal
    // hidden in source stage labels.
    return { ...meta, qualifier: 'Expanding' };
  }
  if (p.status === 'announced' && sd === 'land bank') {
    // Earlier than a formal announcement: site secured, nothing public yet.
    return { ...meta, qualifier: 'Land bank' };
  }
  return { ...meta, qualifier: null };
}

const ProjectSchema = z.object({
  id: z.string(),
  name: z.string(),
  operator: z.string(),
  region: z.enum(PROJECT_REGIONS as [ProjectRegion, ...ProjectRegion[]]),
  country: z.string(),
  city: z.string(),
  capacity_mw: z.number().nonnegative().nullable(),
  capacity_note: z.string().optional(),
  status: z.enum(PROJECT_STATUSES),
  // Legacy field, superseded by announced/construction_start/rfs.
  // Still accepted so older data files parse; ignored by the UI.
  expected: z.string().nullish(),
  // --- Extended profile (all optional/nullable; see SPEC.md) ---
  developer: z.string().nullish(),
  investor: z.string().nullish(),
  investment: z.string().nullish(),
  phases: z.string().nullish(),
  anchor_tenants: z.array(z.string()).nullish(),
  // Set when this entry is a tenant's leased deployment inside someone
  // else's facility (host not tracked in the DB). Such entries are flagged
  // in the UI and excluded from capacity rollups to avoid double counting.
  leased_from: z.string().nullish(),
  power: z.string().nullish(),
  announced: z.string().nullish(),
  construction_start: z.string().nullish(),
  rfs: z.string().nullish(),
  status_history: z
    .array(
      z.object({
        status: z.enum(PROJECT_STATUSES),
        date: z.string(),
        story_id: z.string().nullish(),
        // 'directory_sync' = detected by the weekly DataCenterMap re-scan
        // (no story_id on such entries); absent = news-driven or legacy.
        source: z.string().nullish(),
      }),
    )
    .nullish(),
  // Linked digest story ids, newest last (see SPEC.md data contract).
  story_ids: z.array(z.string()),
  // --- Directory-sourced profile (baxtel/datacentermap; see SPEC.md) ---
  lat: z.number().nullish(),
  lon: z.number().nullish(),
  category: z.string().nullish(),
  company_type: z.string().nullish(),
  description: z.string().nullish(),
  year_built: z.string().nullish(),
  stage_detail: z.string().nullish(),
  expansion_planned: z.boolean().nullish(),
  // Curated spec sheet from DCM facility pages (label -> value).
  specs: z.record(z.string(), z.string()).nullish(),
  first_seen: z.string(),
  // Null = never updated by tracked news yet; the site shows "—".
  last_updated: z.string().nullable(),
  seed: z.boolean(),
  // Provenance of directory imports ("baxtel" / "datacentermap"); absent on
  // original seed and news-created entries.
  source: z.string().nullish(),
});

const ProjectsFileSchema = z.object({
  updated_at: z.string(),
  projects: z.array(ProjectSchema),
});

export type Project = z.infer<typeof ProjectSchema>;
export type ProjectsFile = z.infer<typeof ProjectsFileSchema>;

const DATA_FILE = fileURLToPath(new URL('../../../data/projects.json', import.meta.url));
const FIXTURE_FILE = fileURLToPath(new URL('../../fixtures/projects.json', import.meta.url));

function readProjectsFile(path: string): ProjectsFile | null {
  if (!existsSync(path)) return null;
  try {
    const parsed = ProjectsFileSchema.safeParse(JSON.parse(readFileSync(path, 'utf-8')));
    if (parsed.success) return parsed.data;
    console.warn(`[projects] invalid file ${path}:`, parsed.error.issues[0]?.message);
  } catch (err) {
    console.warn(`[projects] failed to read ${path}:`, err);
  }
  return null;
}

/**
 * Load the project pipeline database. Reads ../data/projects.json (pipeline
 * output); falls back to site/fixtures/projects.json for development when
 * the pipeline has produced nothing yet.
 */
// Build-time memo: pages call this per-route (thousands of times during a
// static build); the file never changes mid-build.
let projectsCache: { projects: Project[]; updatedAt: string | null; source: 'data' | 'fixtures' | 'none' } | null = null;

export function loadProjects(): { projects: Project[]; updatedAt: string | null; source: 'data' | 'fixtures' | 'none' } {
  if (projectsCache) return projectsCache;
  const data = readProjectsFile(DATA_FILE);
  if (data) {
    projectsCache = { projects: data.projects, updatedAt: data.updated_at, source: 'data' };
    return projectsCache;
  }
  const fixture = readProjectsFile(FIXTURE_FILE);
  if (fixture) {
    console.warn('[projects] ../data/projects.json missing or invalid — using fixtures from site/fixtures/projects.json');
    projectsCache = { projects: fixture.projects, updatedAt: fixture.updated_at, source: 'fixtures' };
    return projectsCache;
  }
  projectsCache = { projects: [], updatedAt: null, source: 'none' };
  return projectsCache;
}

/**
 * Reverse index story_id → project. The link is stored only on the project
 * (story_ids); this builds the lookup used by story cards / detail pages.
 * When two projects reference the same story, the first project wins.
 */
export function buildStoryProjectIndex(projects: Project[]): Map<string, Project> {
  const index = new Map<string, Project>();
  for (const project of projects) {
    for (const id of project.story_ids) {
      if (!index.has(id)) index.set(id, project);
    }
  }
  return index;
}

/** "500 MW" + optional note, or "—" when capacity is unknown. */
export function formatCapacity(project: Project): { value: string; note: string | null } {
  if (project.capacity_mw === null) return { value: '—', note: null };
  // Source attributions ("per Baxtel" / "per DataCenterMap") are provenance,
  // not information — readers should never see them.
  const note = project.capacity_note
    ?.replace(/\s*per (Baxtel|DataCenterMap)\s*$/i, '') || null;
  return {
    value: `${project.capacity_mw.toLocaleString('en-US')} MW`,
    note,
  };
}
