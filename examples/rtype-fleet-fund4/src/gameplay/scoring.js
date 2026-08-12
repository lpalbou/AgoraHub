export function addScore(gameState, points) {
  gameState.score = (gameState.score || 0) + points;
}

export function loadHighScore() {
  try {
    return parseInt(localStorage.getItem('rtype_highscore') || '0', 10);
  } catch {
    return 0;
  }
}

export function saveHighScore(score) {
  try {
    const current = loadHighScore();
    if (score > current) {
      localStorage.setItem('rtype_highscore', String(score));
    }
  } catch {
  }
}