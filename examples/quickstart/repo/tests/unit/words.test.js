const test = require("node:test");
const assert = require("node:assert");
const { capitalize, countWords } = require("../../src/words");

test("capitalize upper-cases the first letter", () => {
  assert.strictEqual(capitalize("ada"), "Ada");
});

test("countWords counts words separated by whitespace", () => {
  assert.strictEqual(countWords("  one two\tthree \n"), 3);
});
