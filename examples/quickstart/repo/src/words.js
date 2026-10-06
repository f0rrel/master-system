function capitalize(word) {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

function countWords(text) {
  return text.split(/\s+/).filter(Boolean).length;
}

module.exports = { capitalize, countWords };
