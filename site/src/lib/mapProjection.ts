// --- Mercator projection over a fixed APAC viewport ------------------------
// Shared by HeatMap.astro (country shapes), pipeline.astro (per-row hover
// pins), AND the client-side Leaflet enhancement — kept dependency-free (no
// JSON imports) so bundling it into client JS stays tiny.

export const MAP_LON0 = 95;
export const MAP_LON1 = 160;
export const MAP_LAT0 = -48;
export const MAP_LAT1 = 52;
export const MAP_W = 1000;

const merc = (lat: number) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
const y0 = merc(MAP_LAT0);
const y1 = merc(MAP_LAT1);

// Keep the projection conformal: height follows from the longitude span.
export const MAP_H = Math.round((MAP_W * (y1 - y0) * 180) / ((MAP_LON1 - MAP_LON0) * Math.PI));

export const projectX = (lon: number) => ((lon - MAP_LON0) / (MAP_LON1 - MAP_LON0)) * MAP_W;
export const projectY = (lat: number) => ((y1 - merc(lat)) / (y1 - y0)) * MAP_H;

// Inverses (the Leaflet layer needs lat/lon back from viewBox coordinates).
export const unprojectX = (x: number) => MAP_LON0 + (x / MAP_W) * (MAP_LON1 - MAP_LON0);
export const unprojectY = (y: number) => {
  const m = y1 - (y / MAP_H) * (y1 - y0);
  return ((Math.atan(Math.exp(m)) - Math.PI / 4) * 360) / Math.PI;
};
