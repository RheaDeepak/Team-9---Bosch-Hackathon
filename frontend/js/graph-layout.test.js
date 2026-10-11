import test from "node:test";
import assert from "node:assert/strict";
import { layeredLayout } from "./graph-layout.js";

const nodes = ["lb", "web", "cart", "pay", "db", "orphan"].map((id) => ({ id }));
const edges = [["lb", "web"], ["web", "cart"], ["web", "pay"], ["cart", "db"], ["pay", "db"], ["ghost", "web"]];

test("callers sit left of what they call, so every edge points right", () => {
  const { positions } = layeredLayout(nodes, edges);
  for (const [from, to] of edges.filter(([a, b]) => positions.has(a) && positions.has(b))) assert.ok(positions.get(from).x < positions.get(to).x, `${from} -> ${to}`);
});

test("no two nodes share a spot and a column keeps its row spacing", () => {
  const { positions } = layeredLayout(nodes, edges, { rowGap: 54 });
  assert.equal(positions.size, nodes.length);
  const spots = [...positions.values()].map(({ x, y }) => `${x},${y}`);
  assert.equal(new Set(spots).size, spots.length);
  const byColumn = new Map();
  [...positions.values()].forEach(({ x, y }) => byColumn.set(x, [...(byColumn.get(x) || []), y]));
  for (const ys of byColumn.values()) { ys.sort((a, b) => a - b); ys.slice(1).forEach((y, i) => assert.ok(y - ys[i] >= 54)); }
});

test("a dependency cycle does not hang and still places every node; unknown edge ends are ignored", () => {
  const { positions } = layeredLayout([{ id: "a" }, { id: "b" }], [["a", "b"], ["b", "a"], ["a", "nowhere"]]);
  assert.equal(positions.size, 2);
});

test("empty and single-node graphs are fine, and the height fits the tallest column", () => {
  assert.equal(layeredLayout([], []).positions.size, 0);
  const one = layeredLayout([{ id: "x" }], []);
  assert.deepEqual(one.positions.get("x"), { x: 70, y: 44 });
  const wide = layeredLayout(["a", "b", "c", "d"].map((id) => ({ id })), []);
  assert.equal(wide.height, 44 * 2 + 3 * 54);
});
