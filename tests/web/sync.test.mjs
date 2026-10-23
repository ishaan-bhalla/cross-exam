// node --test tests/web
import { test } from "node:test";
import assert from "node:assert/strict";
import { activeWordIndex, mergeWords, quotePieces, splitBySpans } from "../../src/crossexam/web/sync.js";

const words = [{ start: 0, span: [0, 3] }, { start: 200, span: [4, 9] }, { start: 450, span: [12, 16] }];
const text = "The quick & lazy dog";

test("active word runs from its start to the next word's start", () => {
  assert.equal(activeWordIndex(words, -1), -1);
  assert.equal(activeWordIndex(words, 0), 0);
  assert.equal(activeWordIndex(words, 449), 1);
  assert.equal(activeWordIndex(words, 10_000), 2);
});

test("spans split the paragraph, overlapping spans keep the first", () => {
  const pieces = splitBySpans(text, [...words, { start: 500, span: [5, 8] }]);
  assert.deepEqual(pieces.map((p) => p.text), ["The", " ", "quick", " & ", "lazy", " dog"]);
  assert.deepEqual(pieces.map((p) => p.word), [0, null, 1, null, 2, null]);
});

test("gaps between quoted words join the band; text after the quote does not", () => {
  const pieces = quotePieces(text, words);
  assert.deepEqual(pieces.map((p) => p.band), [0, 1, 1, 2, 2, null]);
  const elided = quotePieces("one two three", [{ start: 0, span: [0, 3] }, { start: 1, span: [8, 13] }]);
  assert.equal(elided[1].band, null); // " two " has letters: left out by an elision
});

test("merged words stay sorted by start", () => {
  assert.deepEqual(mergeWords([{ start: 5 }], [{ start: 1 }, { start: 9 }]).map((w) => w.start), [1, 5, 9]);
});
