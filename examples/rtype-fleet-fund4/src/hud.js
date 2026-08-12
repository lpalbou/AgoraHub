let canvas;
let ctx;

export function init(c) {
  canvas = c;
  ctx = c.getContext('2d');
}

export function drawHUD(state) {
  ctx.save();
  ctx.font = '16px monospace';
  ctx.textBaseline = 'top';

  ctx.fillStyle = '#ffffff';
  ctx.textAlign = 'left';
  ctx.fillText(`SCORE: ${state.score}`, 10, 10);

  ctx.textAlign = 'right';
  ctx.fillText(`LIVES: ${state.lives}`, canvas.width - 10, 10);

  ctx.textAlign = 'center';
  ctx.fillText(`WAVE ${state.wave}`, canvas.width / 2, 10);

  const labels = ['', 'I', 'II', 'III'];
  ctx.textAlign = 'left';
  ctx.fillStyle = '#ffcc00';
  ctx.font = '12px monospace';
  ctx.fillText(`WPN: ${labels[state.weaponLevel] || 'I'}`, 10, 30);

  ctx.restore();
}