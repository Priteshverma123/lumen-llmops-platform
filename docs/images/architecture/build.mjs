// Lumen architecture as one machine: a parcel sorting bench.
// Canisters (OTLP batches) drop down a pneumatic tube into a receiver; a carbon copy goes into the
// archive drawer (S3) before the receiver's ack lamp; parcels queue on a roller conveyor (Redis stream);
// a gantry of three stamping heads (semconv map, cost, dedup) works them; a diverter flap sends poison
// parcels down a chute into the reject tray (dead-letter stream); the rest slide into a columnar cabinet
// (ClickHouse) whose roll-up trays on top feed a gauge pedestal (Grafana dashboards, alerts).
// Behind: a card-index box of prompt versions with a gate arm before the production card holder
// (eval-gated promotion) and an A/B splitter with a flap and two cups. In front: the embedding line,
// hopper -> cache cubbies -> single-flight gate -> batching tray on a budget ruler -> TEI press.
//
// Run: node build.mjs   (writes lumen-architecture.svg next to this file)
import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import * as k from "./kit/iso-kit.mjs";
import * as G from "./kit/lathe.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));

const W = 880;
const H = 560;
const AZ = 58;

const PLATE = { x: -214, y: -114, w: 428, d: 226, r: 10 };
const PLATE_H = 12;
const FOOT_H = 5;

const P = k.fitProjection(
  [...k.boxCorners(PLATE, -PLATE_H - FOOT_H, 0), [-125, -37, 96], [-27, 0, 70], [80, 0, 66]],
  W,
  H,
  { pad: 58, azimuth: AZ },
);

const at = (p) => k.iso(p, P);
const S = (paths, style = {}) => k.solidSvg(paths, style);
const L = (d, style = {}) => k.lineSvg(d, style);
const D = (pts, style = {}) => k.dotsSvg(pts, style);
const box = (plan, z, h, style = {}, steps = 4, bevel = 0) => S(k.slabOf(plan, z, h, P, steps, bevel), style);
const cyl = (x, y, r, z, h, style = {}, steps = 32, bevel = 0) => S(k.cylinder(x, y, r, z, h, P, steps, bevel), style);
const depth = ([x, y, z = 0]) => k.depthOf([x, y, z], P);
const byDepth = (pts) => [...pts].sort((a, b) => depth(a) - depth(b));
const halo = (plan, z = 0, blur = 5) => `<path class="iso-halo" d="${k.haloOf(plan, z, 0.1, P)}" style="filter:blur(${blur}px)"/>`;
const along = (o, axis) => G.frameAlong(o, axis);
const poly = (pts) => k.pathOf(pts.map(at), true);
// a flat plate standing in a face: +y face (y fixed) or +x face (x fixed); u runs along the face, z up
const faceRect = (face, fixed, u0, u1, z0, z1, r = 1) =>
  k.pathOf(
    k.roundedPlan({ x: u0, y: z0, w: u1 - u0, d: z1 - z0, r }, 4).map(([u, z]) => at(face === "y" ? [u, fixed, z] : [fixed, u, z])),
    true,
  );
// a sloped plate given four corners, with a little thickness under its near edge
function ramp(c, thick, style = {}) {
  const top = poly(c);
  const lip = poly([c[2], c[3], [c[3][0], c[3][1], c[3][2] - thick], [c[2][0], c[2][1], c[2][2] - thick]]);
  const side = poly([c[1], c[2], [c[2][0], c[2][1], c[2][2] - thick], [c[1][0], c[1][1], c[1][2] - thick]]);
  return [
    `<path class="iso-fill" d="${top}${lip}${side}"/>`,
    `<path class="iso-top" d="${top}"/>`,
    `<path class="iso-shade" data-shade="1" d="${lip}"/>`,
    `<path class="iso-shade" data-shade="2" d="${side}"/>`,
    L(top + lip + side, { tone: style.tone ?? "mid" }),
  ].join("");
}
// an open-top box: far walls, floor, contents, near walls
function openBox(plan, z, h, t, contents = "", tone = "mid") {
  const { x, y, w, d } = plan;
  return [
    box({ x, y, w, d: t, r: 0.4 }, z, h, { tone }, 2),
    box({ x, y, w: t, d, r: 0.4 }, z, h, { tone }, 2),
    box({ x: x + t, y: y + t, w: w - 2 * t, d: d - 2 * t, r: 0.4 }, z, 1.2, { tone: "lo", crease: "none" }, 2),
    contents,
    box({ x: x + w - t, y, w: t, d, r: 0.4 }, z, h, { tone }, 2),
    box({ x, y: y + d - t, w, d: t, r: 0.4 }, z, h, { tone }, 2),
  ].join("");
}
function dial(x, y, R, z, h, count = 40) {
  const top = z + h;
  const out = [];
  out.push(cyl(x, y, R, z, h, { tone: "hi", crease: "lo" }, 56, 0.6));
  out.push(L(k.knurl(x, y, R, top - 2.4, top, 64, P, 0.3), { tone: "lo" }));
  out.push(L(k.sideArc(x, y, R, top - 2.4, P, 40), { tone: "faint" }));
  out.push(L(k.ring(x, y, R - 1.8, top, P, 56), { tone: "lo" }));
  const t = k.radialTicks(x, y, R - 2.4, count, 5, top, [1, 2.2], P, -Math.PI * 0.75, Math.PI * 1.5);
  out.push(L(t.minor, { tone: "lo" }), L(t.major, { tone: "mid" }));
  out.push(L(k.ring(x, y, 1.2, top, P, 14), { tone: "mid" }));
  return out.join("");
}
const needle = (x, y, R, top, share) => {
  const a = -Math.PI * 0.75 + share * Math.PI * 1.5;
  return `<path class="iso-line" data-tone="hi" data-free="start" style="stroke-width:1" d="${k.lineOnTop([x - Math.sin(a) * 2, y + Math.cos(a) * 2], [x + Math.sin(a) * (R - 3.4), y - Math.cos(a) * (R - 3.4)], top + 0.1, P)}"/>`;
};
const knob = (x, y, r, z, h, pointer = 0) =>
  cyl(x, y, r, z, h, { tone: "mid" }, 28) +
  L(k.knurl(x, y, r, z, z + h, 24, P), { tone: "lo" }) +
  L(k.lineOnTop([x, y], [x + Math.cos(pointer) * r * 0.8, y + Math.sin(pointer) * r * 0.8], z + h, P), { tone: "hi", free: "start" });
const screws = (pts, z) => D(pts.map(([x, y]) => at([x, y, z])), { size: 0.45 });
const pellet = (x, y, z, tone = "hi") => cyl(x, y, 1.7, z, 1.6, { tone, crease: "lo" }, 18);

// ─── base ──────────────────────────────────────────────────────────────────────
function base() {
  const out = [`<path class="iso-halo" d="${k.haloOf(PLATE, -PLATE_H - FOOT_H, PLATE_H, P)}"/>`];
  for (const [x, y] of byDepth(k.corners(PLATE, 24))) {
    out.push(cyl(x, y, 8, -PLATE_H - FOOT_H, 1.6, { tone: "lo" }, 28));
    out.push(cyl(x, y, 6.2, -PLATE_H - FOOT_H + 1.6, FOOT_H - 1.6, { tone: "mid" }, 28));
  }
  out.push(S(k.slabOf(PLATE, -PLATE_H, PLATE_H, P, 8, 1.6), { tone: "mid" }));
  out.push(L(k.planOutline(k.insetPlan(PLATE, 5), 0, P), { tone: "lo" }));
  const sc = k.corners(PLATE, 11);
  out.push(L(sc.map(([x, y]) => k.ring(x, y, 2.4, 0, P, 16)).join(""), { tone: "lo" }));
  out.push(D(sc.map(([x, y]) => at([x, y, 0])), { size: 0.55 }));
  // ruler strip along the near edge
  const strip = { x: -196, y: PLATE.y + PLATE.d - 15, w: 392, d: 7, r: 1 };
  out.push(S(k.slabOf(strip, 0, 0.6, P, 3), { tone: "lo", crease: "none" }));
  const t = k.topTicks(strip.x + 4, strip.x + strip.w - 4, 4, 5, strip.y + 0.01, 0.6, [2, 3.6], P);
  out.push(L(t.minor, { tone: "lo" }), L(t.major, { tone: "mid" }));
  out.push(D([at([strip.x + 2, strip.y + 3.5, 0.6]), at([strip.x + strip.w - 2, strip.y + 3.5, 0.6])], { size: 0.45 }));
  return out.join("");
}

function labelPlate() {
  const plan = { x: 138, y: 62, w: 60, d: 22, r: 1.6 };
  const out = [];
  out.push(S(k.slabOf(plan, 0, 0.9, P, 4), { tone: "lo", crease: "none" }));
  out.push(L(k.planOutline(k.insetPlan(plan, 1.6), 0.9, P), { tone: "faint" }));
  out.push(k.faceTextSvg(k.topMatrix([plan.x + 6, plan.y + 11], 0.9, P, "x"), "LUMEN", { size: 7.4, tone: "mid" }));
  out.push(L([38, 26].map((w, i) => k.lineOnTop([plan.x + 6, plan.y + 15.5 + i * 3.2], [plan.x + 6 + w, plan.y + 15.5 + i * 3.2], 0.9, P)).join(""), { tone: "lo", free: true }));
  out.push(screws([[plan.x + plan.w - 4, plan.y + 4], [plan.x + plan.w - 4, plan.y + plan.d - 4]], 0.9));
  return out.join("");
}

// ─── archive cabinet (S3): raw batch filed before the ack ───────────────────────
const CAB = { x: -206, y: -100, w: 56, d: 44, r: 2 };
const CAB_H = 64;
const CAB_FRONT = CAB.y + CAB.d; // -56
const PULL_OUT = 24;
const DRAWERS = [4, 24, 44];
const OPEN = 24; // the middle drawer is pulled out
function archive() {
  const out = [halo(CAB)];
  out.push(box(k.insetPlan(CAB, -1.5), 0, 3, { tone: "lo" }, 4, 0.4));
  out.push(S(k.slabOf(CAB, 3, CAB_H - 3, P, 4, 1), { tone: "mid", inner: L(k.sideSeam(CAB, CAB_H - 5, P, 4), { tone: "faint" }) }));
  out.push(screws(k.corners(CAB, 4), CAB_H));
  out.push(L(k.planOutline(k.insetPlan(CAB, 4), CAB_H, P, 4), { tone: "faint" }));
  const front = (y, z0) => {
    const o = [];
    o.push(box({ x: CAB.x + 3, y, w: CAB.w - 6, d: 1.6, r: 0.5 }, z0, 17, { tone: "mid" }, 2, 0.3));
    o.push(`<path class="iso-line" data-tone="faint" d="${faceRect("y", y + 1.6, CAB.x + 21, CAB.x + 35, z0 + 10.5, z0 + 14.5, 0.6)}"/>`);
    o.push(box({ x: CAB.x + 22, y: y + 1.6, w: 12, d: 2, r: 0.8 }, z0 + 4, 2, { tone: "mid" }, 2));
    o.push(D([at([CAB.x + 20.2, y + 1.6, z0 + 12.5]), at([CAB.x + 35.8, y + 1.6, z0 + 12.5])], { size: 0.4 }));
    return o.join("");
  };
  // the dark slot the open drawer came out of
  out.push(`<path class="iso-shade" data-shade="0" d="${faceRect("y", CAB_FRONT, CAB.x + 3, CAB.x + CAB.w - 3, OPEN, OPEN + 17, 0.5)}"/>`);
  for (const z0 of DRAWERS) if (z0 !== OPEN) out.push(front(CAB_FRONT, z0));
  // open drawer: a box with filed batches standing in it, the newest one lit
  const y0 = CAB_FRONT - 2;
  const y1 = CAB_FRONT + PULL_OUT;
  const x0 = CAB.x + 4;
  const x1 = CAB.x + CAB.w - 4;
  const fz = OPEN + 1.2;
  const inside = [];
  for (let i = 0; i < 4; i++) inside.push(box({ x: x0 + 3, y: y0 + 3 + i * 3.4, w: x1 - x0 - 6, d: 1, r: 0.3 }, fz + 1.2, 11 - (i % 2) * 1.5, { tone: "hi", crease: "lo" }, 2));
  inside.push(box({ x: x0 + 5, y: y0 + 17.5, w: x1 - x0 - 10, d: 3.4, r: 0.6 }, fz + 1.2, 13.5, { tone: "hi", lit: true }, 2, 0.3));
  inside.push(box({ x: x0 + 9, y: y0 + 17.1, w: 9, d: 0.4, r: 0.2 }, fz + 13.6, 2.4, { tone: "hi", lit: true }, 2));
  out.push(openBox({ x: x0, y: y0, w: x1 - x0, d: y1 - y0, r: 0 }, fz, 11, 1.2, inside.join("")));
  out.push(front(y1, OPEN));
  return out.join("");
}

// ─── receiver (OTLP intake) with the pneumatic tube and the ack lamp ────────────
const RX = { x: -142, y: -54, w: 34, d: 34, r: 2 };
const RX_H = 44;
const TUBE = [-125, -39];
function intake() {
  const out = [halo(RX)];
  // copy chute: from the receiver's side down over the open drawer's wall
  const cy0 = -47;
  const cy1 = -39;
  out.push(ramp([[RX.x, cy0, RX_H - 4], [RX.x, cy1, RX_H - 4], [CAB.x + CAB.w - 7, cy1, OPEN + 13.2], [CAB.x + CAB.w - 7, cy0, OPEN + 13.2]], 1.2, { tone: "mid" }));
  out.push(box(k.insetPlan(RX, -1.4), 0, 3, { tone: "lo" }, 4, 0.4));
  out.push(S(k.slabOf(RX, 3, RX_H - 3, P, 4, 1), { tone: "mid", inner: L(k.sideSeam(RX, RX_H - 5, P, 4) + k.sideSeam(RX, 8, P, 4), { tone: "faint" }) }));
  // inspection door on the +x face
  const fx = RX.x + RX.w;
  out.push(`<path class="iso-line" data-tone="lo" d="${faceRect("x", fx, RX.y + 6, RX.y + 24, 13, 32, 1.2)}"/>`);
  out.push(D([[RX.y + 8.4, 15.4], [RX.y + 8.4, 29.6], [RX.y + 21.6, 15.4], [RX.y + 21.6, 29.6]].map(([y, z]) => at([fx, y, z])), { size: 0.4 }));
  out.push(box({ x: fx, y: RX.y + 9, w: 1.6, d: 3, r: 0.5 }, 21, 4, { tone: "mid" }, 2));
  // outlet ramp onto the conveyor
  out.push(ramp([[-134, RX.y + RX.d, 30], [-120, RX.y + RX.d, 30], [-120, -11, 27.6], [-134, -11, 27.6]], 1.2, { tone: "mid" }));
  // ack lamp
  out.push(cyl(-113, -25, 3, RX_H, 2, { tone: "mid" }, 20));
  out.push(S(G.sphereOf([-113, -25, RX_H + 4], 2.4, P), { tone: "hi" }));
  // tube: base flange, body, bands, bell mouth
  const [tx, ty] = TUBE;
  const TZ = RX_H + 2.4;
  const BELL = 82;
  out.push(cyl(tx, ty, 9, RX_H, 2.4, { tone: "mid" }, 40, 0.4));
  out.push(D(Array.from({ length: 6 }, (_, i) => [tx + Math.cos(i * 1.047 + 0.5) * 7.6, ty + Math.sin(i * 1.047 + 0.5) * 7.6]).map(([x, y]) => at([x, y, TZ])), { size: 0.45 }));
  // stay rod with a clamp arm to the upper band
  out.push(cyl(tx, -51.2, 2.4, RX_H, 2, { tone: "mid" }, 16));
  out.push(cyl(tx, -51.2, 1.4, RX_H + 2, 28, { tone: "mid", crease: "none" }, 16));
  out.push(S(G.disc(0, BELL - TZ, 6, along([tx, ty, TZ], [0, 0, 1]), P), { tone: "hi" }));
  for (const z of [58, 71]) out.push(S(G.disc(0, 3, 7.2, along([tx, ty, z], [0, 0, 1]), P), { tone: "mid" }));
  out.push(box({ x: tx - 1.5, y: -52.6, w: 3, d: 6.6, r: 0.8 }, 71, 3, { tone: "mid" }, 2));
  out.push(S(G.solidOf([[0, 6.4], [3, 6.8, 1], [8, 9.5, 1], [12, 12.5]], along([tx, ty, BELL], [0, 0, 1]), P), { tone: "hi" }));
  out.push(L(k.ring(tx, ty, 10.6, BELL + 12, P, 48), { tone: "lo" }) + L(k.ring(tx, ty, 6, BELL + 12, P, 32), { tone: "mid" }));
  return out.join("");
}

// ─── canister rack: the apps' OTLP exporters, batches ready to send ─────────────
function rack() {
  const RB = { x: -204, y: -28, w: 30, d: 58, r: 2 };
  const out = [halo(RB)];
  out.push(box(RB, 0, 5, { tone: "mid" }, 3, 0.5));
  out.push(screws(k.corners(RB, 3), 5));
  // back bar on two posts
  for (const y of [-24, 26]) out.push(cyl(-200, y, 1.6, 5, 22, { tone: "mid", crease: "none" }, 14));
  out.push(box({ x: -202, y: -26, w: 4, d: 54, r: 1 }, 27, 3, { tone: "mid" }, 2, 0.3));
  // three canisters: shell, two bands, cap with a latch
  const cans = [[-189, -14], [-189, 1], [-189, 16]];
  cans.forEach(([x, y], i) => {
    out.push(L(k.ring(x, y, 5.2, 5, P, 32), { tone: "lo" }));
    out.push(S(G.disc(0, 20, 4.2, along([x, y, 5], [0, 0, 1]), P), { tone: "hi" }));
    for (const z of [9, 19]) out.push(S(G.disc(0, 1.4, 4.6, along([x, y, z], [0, 0, 1]), P), { tone: "mid" }));
    out.push(S(G.solidOf([[0, 4.4], [1.6, 4.4, 1], [2.6, 3.2, 1], [3, 1.6]], along([x, y, 25], [0, 0, 1]), P), { tone: "mid" }));
    out.push(box({ x: x + 3.6, y: y - 1, w: 1.6, d: 2, r: 0.4 }, 14, 4, { tone: "mid" }, 2));
  });
  return out.join("");
}

// ─── conveyor (Redis stream), gantry (ingest consumers), diverter (DLQ) ─────────
const CV0 = -142;
const CV1 = 34;
const RAIL_Z = 19;
const ROLL_Z = 23;
const ROLL_R = 2.4;
const DECK = ROLL_Z + ROLL_R;
const PARCEL_H = 8;
const PARCELS = [-127, -109, -91, -73, -55];
const HEADS = [-38, -27, -16];
const STAMPED = -27;
const GZ = 56; // gantry post height
function mainLine() {
  const far = [];
  const mid = [];
  const near = [];
  // gantry posts, far pair
  for (const x of [-47, -9]) far.push(box({ x, y: -27, w: 3.6, d: 3.6, r: 0.6 }, 0, GZ, { tone: "mid" }, 2, 0.3));
  for (const x of [-47, -9]) far.push(box({ x: x - 1.6, y: -28.6, w: 6.8, d: 6.8, r: 1 }, 0, 2, { tone: "lo" }, 2));
  // conveyor legs and ties
  const legs = [-136, -96, -56, -18, 26];
  for (const x of legs) far.push(box({ x, y: -16, w: 3.4, d: 3.4, r: 0.4 }, 0, RAIL_Z, { tone: "mid" }, 2));
  for (const x of legs) far.push(box({ x: x + 0.2, y: -12.6, w: 3, d: 25.2, r: 0.3 }, 5, 2.6, { tone: "lo" }, 2));
  far.push(box({ x: CV0, y: -16, w: CV1 - CV0, d: 3.5, r: 0.6 }, RAIL_Z, 8, { tone: "mid" }, 2, 0.3));
  far.push(D(legs.map((x) => at([x + 1.7, -14.25, RAIL_Z + 8])), { size: 0.4 }));
  // rollers across the deck
  for (let x = CV0 + 4; x <= CV1 - 3; x += 6.2) {
    mid.push(S(G.disc(0, 25, ROLL_R, along([x, -12.5, ROLL_Z], [0, 1, 0]), P), { tone: "mid", crease: "none" }));
  }
  // parcels queued on the stream
  const parcel = (x, lit = false, tone = "hi") => {
    const plan = { x: x - 6, y: -8, w: 12, d: 16, r: 0.8 };
    const top = DECK + PARCEL_H;
    return (
      box(plan, DECK, PARCEL_H, { tone, crease: "lo", lit }, 2, 0.4) +
      L(k.lineOnTop([x, -8], [x, 8], top, P) + k.sideSeam({ x: x - 0.9, y: -8, w: 1.8, d: 16, r: 0 }, top - 0.01, P, 1), { tone: "lo" }) +
      box({ x: x - 4.6, y: 2, w: 3.6, d: 4, r: 0.3 }, top, 0.3, { tone: "lo", crease: "none" }, 2)
    );
  };
  for (const x of PARCELS) mid.push(parcel(x));
  mid.push(parcel(STAMPED));
  mid.push(parcel(-6));
  // stamping heads hang from the gantry plate
  const HEAD_TOP = GZ + 5;
  HEADS.forEach((x, i) => {
    const pad = i === 1 ? DECK + PARCEL_H : DECK + PARCEL_H + 3;
    mid.push(cyl(x, 0, 4.6, GZ - 11, HEAD_TOP - GZ + 11, { tone: "mid" }, 32, 0.4));
    mid.push(L(k.sideArc(x, 0, 4.6, GZ - 5, P, 20), { tone: "faint" }));
    if (i === 0) mid.push(L(k.knurl(x, 0, 4.6, GZ - 3, GZ + 2, 26, P), { tone: "lo" }));
    if (i === 2) mid.push(cyl(x + 4.2, 3.4, 1.6, GZ - 3, 4, { tone: "mid" }, 14));
    mid.push(cyl(x, 0, 5.4, GZ - 13, 2, { tone: "mid" }, 32));
    mid.push(cyl(x, 0, 1.3, pad + 3, GZ - 13 - pad - 3, { tone: "hi", crease: "none" }, 14));
    mid.push(cyl(x, 0, 3.8, pad, 3, { tone: "hi", crease: "lo" }, 28, 0.3));
  });
  // diverter flap on a pivot post, swung across the deck
  const fl = [[10, -11.5], [11.6, -12.5], [25.2, 8.6], [23.6, 9.6]];
  mid.push(S(k.extrude(fl, DECK, 7, P), { tone: "mid" }));
  mid.push(cyl(10.6, -12.4, 1.8, RAIL_Z + 8, 9, { tone: "mid" }, 16));
  mid.push(cyl(10.6, -12.4, 2.6, DECK + 9, 1.6, { tone: "mid" }, 16));
  // slide plate into the cabinet
  mid.push(box({ x: CV1, y: -12, w: 13, d: 24, r: 0.4 }, DECK - 1.6, 1.6, { tone: "lo" }, 2));
  // near rail, split where the reject chute leaves
  for (const x of legs) near.push(box({ x, y: 12.6, w: 3.4, d: 3.4, r: 0.4 }, 0, RAIL_Z, { tone: "mid" }, 2));
  near.push(box({ x: CV0, y: 12.5, w: 148, d: 3.5, r: 0.6 }, RAIL_Z, 8, { tone: "mid" }, 2, 0.3));
  near.push(box({ x: 27, y: 12.5, w: CV1 - 27, d: 3.5, r: 0.6 }, RAIL_Z, 8, { tone: "mid" }, 2, 0.3));
  near.push(D(legs.map((x) => at([x + 1.7, 14.25, RAIL_Z + 8])), { size: 0.4 }));
  const chute = [[6.5, 12.5, DECK - 0.6], [27, 12.5, DECK - 0.6], [27, 30, 10], [6.5, 30, 10]];
  near.push(ramp(chute, 1.4));
  const lerp = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);
  near.push(L([0.25, 0.5, 0.75].map((t) => k.pathOf([at(lerp(chute[0], chute[3], t)), at(lerp(chute[1], chute[2], t))])).join(""), { tone: "faint" }));
  for (const [a, b] of [[chute[0], chute[3]], [chute[1], chute[2]]]) {
    const up = (q) => [q[0], q[1], q[2] + 2.2];
    near.push(ramp([a, b, up(b), up(a)], 0, { tone: "mid" }));
  }
  // gantry: near posts, cross beams, head plate, cost dial
  for (const x of [-47, -9]) near.push(box({ x: x - 1.6, y: 21.4, w: 6.8, d: 6.8, r: 1 }, 0, 2, { tone: "lo" }, 2));
  for (const x of [-47, -9]) near.push(box({ x, y: 23, w: 3.6, d: 3.6, r: 0.6 }, 0, GZ, { tone: "mid" }, 2, 0.3));
  for (const x of [-48.5, -10.5]) near.push(box({ x, y: -29, w: 6.6, d: 54.6, r: 0.8 }, GZ, 5, { tone: "mid" }, 2, 0.4));
  for (const x of [-48.5, -10.5]) near.push(screws([[x + 3.3, -25.2], [x + 3.3, 21.8]], GZ + 5));
  const PLATE_G = { x: -50, y: -11, w: 46, d: 22, r: 1.2 };
  near.push(box(PLATE_G, GZ + 5, 3, { tone: "mid" }, 3, 0.4));
  near.push(screws([[-46.8, -7.8], [-46.8, 7.8], [-7.2, -7.8], [-7.2, 7.8]], GZ + 8));
  near.push(dial(-27, 0, 8.4, GZ + 8, 4, 30));
  near.push(needle(-27, 0, 8.4, GZ + 12, 0.62));
  return far.join("") + mid.join("") + near.join("");
}

// reject tray (dead-letter stream): poison parcels, a replay crank
const TRAY = { x: 2, y: 30, w: 44, d: 34, r: 0 };
function rejectTray() {
  const inside = [];
  const poison = (x, y, w, d, h) => {
    const top = 1.2 + h;
    return box({ x, y, w, d, r: 0.6 }, 1.2, h, { tone: "hi", crease: "lo" }, 2, 0.3) + L(k.lineOnTop([x + 1.5, y + 1.5], [x + w - 1.5, y + d - 1.5], top, P) + k.lineOnTop([x + w - 1.5, y + 1.5], [x + 1.5, y + d - 1.5], top, P), { tone: "mid" });
  };
  inside.push(poison(8, 35, 12, 14, 7));
  inside.push(poison(25, 44, 13, 13, 6));
  const out = [halo(TRAY)];
  out.push(openBox(TRAY, 0, 9, 1.6, inside.join("")));
  out.push(screws([[TRAY.x + 0.8, TRAY.y + 0.8], [TRAY.x + TRAY.w - 0.8, TRAY.y + 0.8], [TRAY.x + TRAY.w - 0.8, TRAY.y + TRAY.d - 0.8]], 9));
  // replay crank on the +x wall
  const fx = TRAY.x + TRAY.w;
  out.push(S(G.disc(0, 2, 3.4, along([fx, 52, 5], [1, 0, 0]), P), { tone: "mid" }));
  out.push(S(G.disc(2, 3.2, 1.2, along([fx, 52, 5], [1, 0, 0]), P), { tone: "mid" }));
  out.push(box({ x: fx + 3.2, y: 51, w: 1.4, d: 2, r: 0.3 }, 4, 8, { tone: "mid" }, 2));
  out.push(S(G.disc(0, 4, 1.1, along([fx + 4.6, 52, 11], [1, 0, 0]), P), { tone: "mid" }));
  return out.join("");
}

// ─── columnar cabinet (ClickHouse) with roll-up trays on top ────────────────────
const CH = { x: 48, y: -34, w: 70, d: 66, r: 2 };
const CH_H = 50;
function clickhouse() {
  const out = [halo(CH)];
  out.push(box(k.insetPlan(CH, -1.6), 0, 3, { tone: "lo" }, 4, 0.4));
  out.push(S(k.slabOf(CH, 3, CH_H - 3, P, 4, 1), { tone: "mid", inner: L(k.sideSeam(CH, CH_H - 4, P, 4), { tone: "faint" }) }));
  const fy = CH.y + CH.d;
  // tall column drawers on the +y face
  for (let i = 0; i < 6; i++) {
    const x = CH.x + 4 + i * 10.5;
    out.push(box({ x, y: fy, w: 9, d: 1.4, r: 0.4 }, 7, 37, { tone: "mid" }, 2, 0.3));
    out.push(`<path class="iso-line" data-tone="faint" d="${faceRect("y", fy + 1.4, x + 1.6, x + 7.4, 36, 41, 0.5)}"/>`);
    out.push(box({ x: x + 3, y: fy + 1.4, w: 3, d: 1.6, r: 0.5 }, 28, 4, { tone: "mid" }, 2));
  }
  // louvres on the +x face
  const fx = CH.x + CH.w;
  for (let i = 0; i < 6; i++) out.push(box({ x: fx, y: CH.y + 10, w: 1.4, d: 40, r: 0.3 }, 10 + i * 5.4, 1.4, { tone: "mid" }, 2));
  out.push(D([at([fx, CH.y + 7, 8]), at([fx, CH.y + 7, 42]), at([fx, CH.y + 53, 8]), at([fx, CH.y + 53, 42])], { size: 0.45 }));
  // minute and hour roll-up trays
  const minute = { x: CH.x + 6, y: CH.y + 6, w: 58, d: 54, r: 1.4 };
  out.push(box(minute, CH_H, 6, { tone: "mid" }, 3, 0.5));
  out.push(D(k.dotGrid(minute.x + 34, minute.y + 6, 5, 9, 5, CH_H + 6, P), { size: 0.45, tone: "lo" }));
  const hour = { x: CH.x + 12, y: CH.y + 12, w: 24, d: 42, r: 1.2 };
  out.push(box(hour, CH_H + 6, 6, { tone: "mid" }, 3, 0.5));
  out.push(D(k.dotGrid(hour.x + 5, hour.y + 6, 3, 7, 5.4, CH_H + 12, P), { size: 0.5, tone: "mid" }));
  out.push(screws(k.corners(minute, 2.4), CH_H + 6));
  // cable glands for the gauge leads
  out.push(cyl(CH.x + CH.w - 8, CH.y + 14, 2.4, CH_H + 6, 2.4, { tone: "mid" }, 18));
  out.push(cyl(CH.x + CH.w - 8, CH.y + 26, 2.4, CH_H + 6, 2.4, { tone: "mid" }, 18));
  return out.join("");
}

// ─── gauge pedestal (Grafana dashboards, alerts) ─────────────────────────────────
const GP = { x: 132, y: -106, w: 62, d: 48, r: 3 };
const GP_H = 18;
function gauges() {
  const out = [halo(GP)];
  out.push(box(k.insetPlan(GP, -1.5), 0, 3, { tone: "lo" }, 4, 0.4));
  out.push(S(k.slabOf(GP, 3, GP_H - 3, P, 4, 1), { tone: "mid", inner: L(k.sideSeam(GP, GP_H - 4, P, 4), { tone: "faint" }) }));
  out.push(screws(k.corners(GP, 3.6), GP_H));
  out.push(dial(148, -86, 12, GP_H, 6, 40));
  out.push(needle(148, -86, 12, GP_H + 6, 0.38));
  out.push(dial(176, -86, 12, GP_H, 6, 40));
  out.push(needle(176, -86, 12, GP_H + 6, 0.71));
  // alert lamp and its guard
  out.push(cyl(180, -66, 4.4, GP_H, 2, { tone: "mid" }, 28));
  out.push(S(G.solidOf([[0, 3.4], [3, 3.4, 1], [5, 2.4, 1], [6, 0.8]], along([180, -66, GP_H + 2], [0, 0, 1]), P), { tone: "hi" }));
  out.push(knob(152, -66, 3.2, GP_H, 3.6, 0.9));
  out.push(knob(162, -66, 3.2, GP_H, 3.6, -0.4));
  // leads from the roll-up cabinet: wires with terminals on the gland tops and the pedestal
  // leads from the roll-up cabinet's glands to two terminal posts on the pedestal
  const posts = [[GP.x + 5, GP.y + 30], [GP.x + 5, GP.y + 40]];
  for (const [x, y] of posts) out.push(cyl(x, y, 1.8, GP_H, 2.6, { tone: "mid" }, 16));
  const glands = [[CH.x + CH.w - 8, CH.y + 14], [CH.x + CH.w - 8, CH.y + 26]];
  glands.forEach(([gx, gy], i) => {
    const [px, py] = posts[i];
    const from = at([gx, gy, CH_H + 8.4]);
    const to = at([px, py, GP_H + 2.6]);
    // a lead hangs in a smooth arc: rises out of the gland, falls onto the post
    const z0 = CH_H + 8.4;
    const z1 = GP_H + 2.6;
    const via = Array.from({ length: 15 }, (_, j) => {
      const t = (j + 1) / 16;
      return at([gx + (px - gx) * t, gy + (py - gy) * t, z0 + (z1 - z0) * t * t + (16 - i * 4) * Math.sin(Math.PI * t)]);
    });
    out.push(k.wireSvg(from, to, { via, tone: "mid", size: 0.9 }));
  });
  return out.join("");
}

// ─── prompt registry: card index, eval gate, production holder ──────────────────
const REG = { x: -118, y: -108, w: 46, d: 34, r: 0 };
function registry() {
  const inside = [];
  for (let i = 0; i < 8; i++) {
    const x = REG.x + 4 + i * 5;
    const h = 13 + (i % 3) * 2.4;
    inside.push(box({ x, y: REG.y + 3, w: 1, d: REG.d - 6, r: 0.2 }, 1.2, h, { tone: "hi", crease: "lo" }, 2));
    if (i % 2 === 0) inside.push(box({ x, y: REG.y + 6 + (i % 4) * 4, w: 1, d: 6, r: 0.4 }, 1.2 + h, 2, { tone: "hi", crease: "lo" }, 2));
  }
  const out = [halo(REG)];
  out.push(openBox(REG, 0, 11, 1.6, inside.join("")));
  out.push(box({ x: REG.x + 16, y: REG.y + REG.d, w: 14, d: 1.4, r: 0.4 }, 4, 4.6, { tone: "mid" }, 2));
  // gate: post with a barrier arm across the lane to the production holder
  out.push(cyl(-66, -78, 2.4, 0, 2, { tone: "lo" }, 18));
  out.push(cyl(-66, -78, 1.5, 2, 18, { tone: "mid", crease: "none" }, 14));
  out.push(box({ x: -67, y: -104, w: 2, d: 27, r: 0.6 }, 18, 2.2, { tone: "mid" }, 2));
  out.push(cyl(-66, -78, 2.2, 17, 4, { tone: "mid" }, 18));
  out.push(knob(-58, -72, 3, 0, 4.2, 2.2));
  // production holder with one card
  const HOLD = { x: -60, y: -106, w: 18, d: 22, r: 1 };
  out.push(box(HOLD, 0, 7, { tone: "mid" }, 3, 0.5));
  out.push(L(k.lineOnTop([HOLD.x + 3, -95.6], [HOLD.x + HOLD.w - 3, -95.6], 7, P) + k.lineOnTop([HOLD.x + 3, -93.4], [HOLD.x + HOLD.w - 3, -93.4], 7, P), { tone: "lo" }));
  out.push(box({ x: HOLD.x + 3, y: -95.2, w: 12, d: 1.4, r: 0.3 }, 7, 20, { tone: "hi", crease: "lo" }, 2));
  out.push(screws([[HOLD.x + 2, HOLD.y + 2], [HOLD.x + 2, HOLD.y + HOLD.d - 2]], 7));
  return out.join("");
}

// ─── A/B splitter: inlet lane, flap on a pivot, two counting cups ───────────────
function splitter() {
  const BASE = { x: -32, y: -112, w: 56, d: 44, r: 2 };
  const out = [halo(BASE)];
  out.push(box(BASE, 0, 3, { tone: "mid" }, 3, 0.4));
  out.push(screws(k.corners(BASE, 3), 3));
  for (const y of [-94, -86.6]) out.push(box({ x: -28, y, w: 20, d: 1.4, r: 0.3 }, 3, 5, { tone: "mid" }, 2));
  out.push(cyl(-4.6, -89.6, 1.8, 3, 8, { tone: "mid" }, 16));
  out.push(S(k.extrude([[-4.6, -90.4], [-3.6, -91.2], [7.6, -101], [6.6, -100.2]], 3, 5.2, P), { tone: "mid" }));
  out.push(pellet(-18, -90.4, 3));
  for (const y of [-100, -78]) {
    out.push(cyl(14, y, 7, 3, 9, { tone: "mid" }, 36, 0.4));
    out.push(L(k.ring(14, y, 5.6, 12, P, 32), { tone: "lo" }));
    out.push(L(k.sideArc(14, y, 7, 7, P, 24), { tone: "faint" }));
  }
  out.push(D([at([12.6, -100.4, 12]), at([15.4, -99, 12]), at([14, -101.8, 12])], { size: 1.2, tone: "hi" }));
  out.push(D([at([13, -78.6, 12]), at([15.2, -77.2, 12])], { size: 1.2, tone: "hi" }));
  return out.join("");
}

// ─── embedding line: hopper → cache → single-flight → batch tray → TEI press ────
const EY = 82;
function embedder() {
  const out = [];
  const TRACK = { x: -200, y: EY - 6, w: 132, d: 12, r: 1.4 };
  out.push(S(k.slabOf(TRACK, 0, 0.8, P, 3), { tone: "lo", crease: "none" }));
  out.push(L(k.lineOnTop([TRACK.x + 2, EY], [TRACK.x + TRACK.w - 2, EY], 0.8, P), { tone: "faint" }));
  // hopper on a neck
  out.push(halo({ x: -196, y: EY - 10, w: 20, d: 20, r: 10 }));
  out.push(cyl(-186, EY, 6.4, 0.8, 2, { tone: "lo" }, 32));
  out.push(cyl(-186, EY, 3.6, 2.8, 12, { tone: "mid", crease: "none" }, 24));
  out.push(S(G.solidOf([[0, 3.6], [2, 4.2, 1], [12, 11, 1], [16, 13], [18, 13]], along([-186, EY, 14.8], [0, 0, 1]), P), { tone: "mid" }));
  out.push(L(k.ring(-186, EY, 11.6, 32.8, P, 48), { tone: "lo" }));
  out.push(D([[-189, EY - 2], [-184, EY + 3], [-183, EY - 4], [-188, EY + 4.5], [-186, EY]].map(([x, y]) => at([x, y, 32.8])), { size: 1.1, tone: "hi" }));
  // cache cubbies
  const C = { x: -170, y: EY - 9, w: 18, d: 18, r: 1 };
  out.push(halo(C));
  out.push(box(C, 0.8, 15, { tone: "mid" }, 3, 0.5));
  for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) out.push(L(k.planOutline({ x: C.x + 1.6 + i * 5.1, y: C.y + 1.6 + j * 5.1, w: 4.4, d: 4.4, r: 0.6 }, 15.8, P, 2), { tone: "lo" }));
  out.push(D([[0, 0], [2, 1], [1, 2], [2, 2]].map(([i, j]) => at([C.x + 3.8 + i * 5.1, C.y + 3.8 + j * 5.1, 15.8])), { size: 1, tone: "hi" }));
  // single-flight: two cheeks and a lintel with one slot
  out.push(box({ x: -146, y: EY - 9, w: 4, d: 7.6, r: 0.4 }, 0.8, 16, { tone: "mid" }, 2, 0.3));
  out.push(pellet(-144, EY, 0.8));
  out.push(box({ x: -146, y: EY + 1.4, w: 4, d: 7.6, r: 0.4 }, 0.8, 16, { tone: "mid" }, 2, 0.3));
  out.push(box({ x: -147, y: EY - 10, w: 6, d: 20, r: 0.6 }, 16.8, 2.4, { tone: "mid" }, 2, 0.3));
  out.push(screws([[-144, EY - 7.4], [-144, EY + 7.4]], 19.2));
  // micro-batch tray on a budget ruler, with the max-wait knob
  const T = { x: -134, y: EY - 9, w: 26, d: 18, r: 0 };
  const inside = [];
  for (let i = 0; i < 4; i++) for (let j = 0; j < 2; j++) if (!(i === 3 && j === 1)) inside.push(pellet(T.x + 5 + i * 5.4, T.y + 6 + j * 6, 2));
  out.push(halo(T));
  out.push(openBox(T, 0.8, 6, 1.2, inside.join("")));
  const R = { x: T.x, y: T.y + T.d + 1, w: T.w, d: 4, r: 0.6 };
  out.push(S(k.slabOf(R, 0.8, 0.6, P, 3), { tone: "lo", crease: "none" }));
  const tk = k.topTicks(R.x + 1, R.x + R.w - 1, 2, 4, R.y + 0.01, 1.4, [1.2, 2.4], P);
  out.push(L(tk.minor, { tone: "lo" }), L(tk.major, { tone: "mid" }));
  out.push(knob(-102, EY + 9, 3, 0.8, 4.4, 2.4));
  // TEI press: base, columns, ram, crosshead
  const PB = { x: -98, y: EY - 12, w: 28, d: 24, r: 1.6 };
  out.push(halo(PB));
  out.push(box(PB, 0.8, 9, { tone: "mid" }, 3, 0.6));
  out.push(L(k.planOutline({ x: -89, y: EY - 5, w: 10, d: 10, r: 1 }, 9.8, P, 2), { tone: "lo" }));
  const cols = [[-94, EY - 8], [-74, EY - 8], [-94, EY + 8], [-74, EY + 8]];
  const back = cols.filter(([, y]) => y < EY);
  const front = cols.filter(([, y]) => y > EY);
  for (const [x, y] of byDepth(back)) out.push(cyl(x, y, 1.6, 9.8, 30, { tone: "mid", crease: "none" }, 14));
  out.push(cyl(-84, EY, 3.6, 16, 23.8, { tone: "hi", crease: "none" }, 28));
  out.push(cyl(-84, EY, 6, 13.6, 2.4, { tone: "hi", crease: "lo" }, 32, 0.3));
  for (const [x, y] of byDepth(front)) out.push(cyl(x, y, 1.6, 9.8, 30, { tone: "mid", crease: "none" }, 14));
  out.push(box({ x: -98, y: EY - 11, w: 28, d: 22, r: 1.6 }, 39.8, 5, { tone: "mid" }, 3, 0.5));
  out.push(screws(cols, 44.8));
  out.push(cyl(-84, EY, 4.4, 44.8, 3, { tone: "mid" }, 28));
  return out.join("");
}

// ─── card frame ──────────────────────────────────────────────────────────────────
const CARD_CSS = `.lm-card{fill:var(--anatomy-card);stroke:var(--anatomy-mid);stroke-width:.6px}.lm-page{fill:var(--anatomy-page)}.lm-t{font-family:var(--anatomy-mono);font-size:11px;letter-spacing:.04em;fill:var(--anatomy-muted)}.lm-t[data-k="fig"],.lm-t[data-k="read"]{fill:var(--anatomy-text)}.lm-t[data-k="title"],.lm-t[data-k="hint"]{text-transform:uppercase;letter-spacing:.1em}`;
function card() {
  const m = 10;
  return [
    `<style>${CARD_CSS}</style>`,
    `<rect class="lm-page" x="0" y="0" width="${W}" height="${H}"/>`,
    `<rect class="lm-card" x="${m}" y="${m}" width="${W - 2 * m}" height="${H - 2 * m}"/>`,
    `<text class="lm-t" data-k="fig" x="${m + 20}" y="${m + 28}">Fig 1</text>`,
    `<text class="lm-t" data-k="title" x="${W - m - 20}" y="${m + 28}" text-anchor="end">LUMEN · TELEMETRY SORTING BENCH</text>`,
    `<text class="lm-t" data-k="hint" x="${m + 20}" y="${H - m - 20}">S3 copy before ack · stream · stamp · reject · roll up</text>`,
    `<text class="lm-t" data-k="read" x="${W - m - 20}" y="${H - m - 20}" text-anchor="end">0 batches lost · XACK after insert</text>`,
  ].join("");
}

// assemblies, painted far to near; three are built at a home spot and moved into place
const moved = (by, fn) => () => `<g transform="${k.translateAlong(P, by)}">${fn()}</g>`;
const assemblies = [
  { at: [-178, -78], svg: archive },
  { at: [-189, 1], svg: rack },
  { at: [-125, -37], svg: intake },
  { at: [-54, 0], svg: mainLine },
  { at: [-114, EY], svg: moved([20, 0, 0], embedder) },
  { at: [83, -1], svg: clickhouse },
  { at: [24, 47], svg: rejectTray },
  { at: [163, -82], svg: gauges },
  { at: [168, 73], svg: labelPlate },
  { at: [92, 73], svg: moved([176, 164, 0], registry) },
  { at: [164, -29], svg: moved([172, 62, 0], splitter) },
].sort((a, b) => depth(a.at) - depth(b.at));

const svg = k.figureSvg({
  width: W,
  height: H,
  standalone: "dark",
  label:
    "Lumen architecture drawn as a parcel sorting bench. Telemetry canisters drop down a pneumatic tube into a receiver; a copy is filed in an archive drawer (S3) before the ack lamp; parcels queue on a roller conveyor (Redis stream) under a gantry of three stamping heads (semantic mapping, cost, dedup); a diverter flap sends poison parcels into a reject tray (dead-letter stream); the rest slide into a columnar cabinet (ClickHouse) whose roll-up trays feed a gauge pedestal (dashboards and alerts). Behind sit a card index of prompt versions with a gate arm before the production holder, and an A/B splitter with two cups. In front, the embedding line: hopper, cache cubbies, single-flight gate, batching tray on a budget ruler and the TEI press.",
  body: [card(), `<g>`, base(), ...assemblies.map((a) => a.svg()), `</g>`],
});

writeFileSync(join(HERE, "lumen-architecture.svg"), svg);
console.log("wrote lumen-architecture.svg", (svg.length / 1024).toFixed(0) + " KB");
