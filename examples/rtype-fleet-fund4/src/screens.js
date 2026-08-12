let canvas;
let ctx;

export function init(c) {
  canvas = c;
  ctx = c.getContext('2d');
}

export function drawTitle() {
  ctx.fillStyle = '#0a0e17';
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  ctx.save();
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';

  ctx.fillStyle = '#00ccff';
  ctx.font = 'bold 48px monospace';
  ctx.fillText('R-TYPE', canvas.width / 2, canvas.height / 2 - 60);

  ctx.fillStyle = '#ffffff';
  ctx.font = '16px monospace';
  ctx.fillText('PROTOTYPE', canvas.width / 2, canvas.height / 2);

  ctx.fillStyle = '#888';
  ctx.font = '14px monospace';
  ctx.fillText('Press ENTER to start', canvas.width / 2, canvas.height / 2 + 50);

  ctx.restore();
}

export function drawGameOver(score) {
  ctx.fillStyle = 'rgba(0, 0, 0, 0.7)';
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  ctx.save();
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';

  ctx.fillStyle = '#ff4444';
  ctx.font = 'bold 48px monospace';
  ctx.fillText('GAME OVER', canvas.width / 2, canvas.height / 2 - 40);

  ctx.fillStyle = '#ffffff';
  ctx.font = '20px monospace';
  ctx.fillText(`SCORE: ${score}`, canvas.width / 2, canvas.height / 2 + 20);

  const highScore = parseInt(localStorage.getItem('rtype_highscore') || '0', 10);
  if (score >= highScore && score > 0) {
    ctx.fillStyle = '#ffcc00';
    ctx.font = '16px monospace';
    ctx.fillText('NEW HIGH SCORE!', canvas.width / 2, canvas.height / 2 + 50);
  } else if (highScore > 0) {
    ctx.fillStyle = '#888';
    ctx.font = '14px monospace';
    ctx.fillText(`HIGH SCORE: ${highScore}`, canvas.width / 2, canvas.height / 2 + 50);
  }

  ctx.fillStyle = '#888';
  ctx.font = '14px monospace';
  ctx.fillText('Press ENTER to restart', canvas.width / 2, canvas.height / 2 + 80);

  ctx.restore();
}

export function drawVictory(score) {
  ctx.fillStyle = '#0a0e17';
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  ctx.save();
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';

  ctx.fillStyle = '#44ff44';
  ctx.font = 'bold 48px monospace';
  ctx.fillText('VICTORY', canvas.width / 2, canvas.height / 2 - 40);

  ctx.fillStyle = '#ffffff';
  ctx.font = '20px monospace';
  ctx.fillText(`SCORE: ${score}`, canvas.width / 2, canvas.height / 2 + 20);

  const highScore = parseInt(localStorage.getItem('rtype_highscore') || '0', 10);
  if (score >= highScore && score > 0) {
    ctx.fillStyle = '#ffcc00';
    ctx.font = '16px monospace';
    ctx.fillText('NEW HIGH SCORE!', canvas.width / 2, canvas.height / 2 + 50);
  } else if (highScore > 0) {
    ctx.fillStyle = '#888';
    ctx.font = '14px monospace';
    ctx.fillText(`HIGH SCORE: ${highScore}`, canvas.width / 2, canvas.height / 2 + 50);
  }

  ctx.fillStyle = '#888';
  ctx.font = '14px monospace';
  ctx.fillText('Press ENTER to restart', canvas.width / 2, canvas.height / 2 + 80);

  ctx.restore();
}