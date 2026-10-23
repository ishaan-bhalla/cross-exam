import { test } from "node:test";
import assert from "node:assert/strict";
import { findSource, roleLabel, titleCase, uploadError } from "../../src/crossexam/web/profile-ui.js";

test("all-caps names are shown in title case; mixed case is kept", () => {
  assert.equal(titleCase("STEPHEN ROBERT GRAYSTON"), "Stephen Robert Grayston");
  assert.equal(titleCase("MR JAMES BRUCE MCNIVEN"), "Mr James Bruce McNiven");
  assert.equal(titleCase("ANNE O'BRIEN-SMITH"), "Anne O'Brien-Smith");
  assert.equal(titleCase("Alan George D’Alvarez"), "Alan George D’Alvarez");
  assert.equal(titleCase("ALISDAIR CAMERON"), "Alisdair Cameron");
});

test("role label matches the server's format", () => {
  assert.equal(roleLabel([{ title: "Head of Product & Branch Accounting", organisation: "Post Office Ltd", from: "June 2006", to: null }]),
               "Head of Product & Branch Accounting, Post Office Ltd (June 2006)");
  assert.equal(roleLabel([], "Fujitsu"), "Fujitsu");
});

test("a value's source is found in the title page first, then the paragraphs", () => {
  const units = [{ para_no: 6, text: "I joined the Post Office in 1985." }, { para_no: "6.1", text: "Head of Risk – Retail" }];
  assert.deepEqual(findSource("David Smith", "Witness Name: DAVID SMITH", units), { key: "preamble", start: 14, end: 25 });
  assert.deepEqual(findSource("post  office", "", units), { key: 6, start: 13, end: 24 });
  assert.equal(findSource("Head of Risk - Retail", "", units).key, "6.1"); // dash and hyphen match
  assert.equal(findSource("Chief Executive", "", units), null);
  assert.equal(findSource("  ", "", units), null);
});

test("upload errors are in plain words", () => {
  assert.match(uploadError(413, "file is larger", 50), /larger than the 50 MB limit/);
  assert.match(uploadError(415), /not a PDF/);
  assert.equal(uploadError(429, "You already have 3 statements open."), "You already have 3 statements open.");
});

test("role labels start with a capital", () => {
  assert.equal(roleLabel([{ title: "senior non-executive director", organisation: "Royal Mail Holdings Plc" }]),
               "Senior non-executive director, Royal Mail Holdings Plc");
});
