let canvas;
let ctx;

const stars = [];

export function init(c) {
  canvas = c;
  ctx = c.getContext('2d');
  for (let i = 0; i < 60; i++) {
    stars.push({
      x: Math.random() * canvas.width,
      y: Math.random() * canvas.height,
      speed: 20 + Math.random() * 60,
      size: 0.5 + Math.random() * 2,
    });
  }
}

export function clear() {
  ctx.fillStyle = '#0a0e17';
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  ctx.fillStyle = '#111827';
  for (const s of stars) {
    s.x -= s.speed / 60;
    if (s.x < 0) {
      s.x = canvas.width;
      s.y = Math.random() * canvas.height;
    }
    ctx.fillRect(Math.round(s.x), Math.round(s.y), s.size, s.size);
  }
}

const PALETTE = {
  player: '#00ccff',
  enemy: '#ff4444',
  bullet: '#ffff44',
  powerup: '#44ff44',
  explosion: '#ff8800',
};

function drawPlayer(e) {
  const w = e.hitbox.w;
  const h = e.hitbox.h;

  ctx.fillStyle = '#00ccff';
  ctx.fillRect(e.pos.x + 4, e.pos.y + 2, w - 4, h - 4);

  ctx.fillStyle = '#0088cc';
  ctx.fillRect(e.pos.x + w - 2, e.pos.y + h / 2 - 4, 8, 8);

  ctx.fillStyle = '#00eeff';
  ctx.fillRect(e.pos.x + 8, e.pos.y + 4, 4, 2);
  ctx.fillRect(e.pos.x + 8, e.pos.y + h - 6, 4, 2);

  ctx.fillStyle = '#ff6600';
  const thrustLen = 6 + Math.sin(performance.now() / 50) * 3;
  ctx.fillRect(e.pos.x - thrustLen, e.pos.y + h / 2 - 2, thrustLen, 4);
}

function drawEnemy(e) {
  const w = e.hitbox.w;
  const h = e.hitbox.h;
  const ref = e.spriteRef || 'enemy_fighter';

  if (ref === 'enemy_fighter') {
    ctx.fillStyle = '#ff4444';
    ctx.fillRect(e.pos.x, e.pos.y, w, h);
    ctx.fillStyle = '#cc0000';
    ctx.fillRect(e.pos.x + w / 2 - 2, e.pos.y - 3, 4, 3);
    ctx.fillStyle = '#ffff44';
    ctx.fillRect(e.pos.x + 2, e.pos.y + 2, 4, 4);
    ctx.fillRect(e.pos.x + w - 6, e.pos.y + 2, 4, 4);
  } else if (ref === 'enemy_bomber') {
    ctx.fillStyle = '#ff8800';
    ctx.fillRect(e.pos.x, e.pos.y, w, h);
    ctx.fillStyle = '#cc6600';
    ctx.fillRect(e.pos.x + 4, e.pos.y - 4, w - 8, 6);
    ctx.fillStyle = '#ffcc00';
    ctx.fillRect(e.pos.x + w / 2 - 3, e.pos.y + h / 2 - 3, 6, 6);
  } else if (ref === 'enemy_turret') {
    ctx.fillStyle = '#8844ff';
    ctx.fillRect(e.pos.x, e.pos.y, w, h);
    ctx.fillStyle = '#6622cc';
    ctx.fillRect(e.pos.x + 2, e.pos.y + 2, w - 4, h - 4);
    ctx.fillStyle = '#ff4444';
    ctx.fillRect(e.pos.x + w / 2 - 2, e.pos.y + h / 2 - 6, 4, 12);
  } else {
    ctx.fillStyle = '#ff4444';
    ctx.fillRect(e.pos.x, e.pos.y, w, h);
  }

  if (e.hp < e.maxHp) {
    const barW = w;
    const barH = 2;
    ctx.fillStyle = '#333';
    ctx.fillRect(e.pos.x, e.pos.y - 4, barW, barH);
    ctx.fillStyle = '#ff4444';
    ctx.fillRect(e.pos.x, e.pos.y - 4, barW * (e.hp / e.maxHp), barH);
  }
}

function drawBullet(e) {
  if (e.bulletType === 'enemy') {
    ctx.fillStyle = '#ff6666';
    ctx.fillRect(e.pos.x, e.pos.y, e.hitbox.w, e.hitbox.h);
  } else {
    ctx.fillStyle = '#ffff44';
    ctx.fillRect(e.pos.x, e.pos.y, e.hitbox.w, e.hitbox.h);
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(e.pos.x + 1, e.pos.y + 1, e.hitbox.w - 2, e.hitbox.h - 2);
  }
}

function drawPowerup(e) {
  ctx.fillStyle = '#44ff44';
  ctx.fillRect(e.pos.x, e.pos.y, e.hitbox.w, e.hitbox.h);
  ctx.fillStyle = '#22aa22';
  ctx.fillRect(e.pos.x + 2, e.pos.y + 2, e.hitbox.w - 4, e.hitbox.h - 4);
  ctx.fillStyle = '#ffffff';
  ctx.font = '10px monospace';
  ctx.textAlign = 'center';
  ctx.fillText('P', e.pos.x + e.hitbox.w / 2, e.pos.y + e.hitbox.h / 2 + 3);
}

function drawExplosion(e) {
  const progress = e._explosionProgress || 0;
  const size = 10 + progress * 20;
  ctx.fillStyle = `rgba(255, 136, 0, ${1 - progress})`;
  ctx.fillRect(e.pos.x - size / 2, e.pos.y - size / 2, size, size);
  if (progress >= 1) e.alive = false;
  e._explosionProgress = (e._explosionProgress || 0) + 0.05;
}

export function drawEntity(e) {
  if (!e.alive) return;

  if (e.type === 'explosion') {
    drawExplosion(e);
    return;
  }

  ctx.save();

  if (e.facing < 0 && e.type === 'player') {
    ctx.translate(e.pos.x + e.hitbox.w / 2, 0);
    ctx.scale(-1, 1);
    ctx.translate(-(e.pos.x + e.hitbox.w / 2), 0);
  }

  switch (e.type) {
    case 'player':
      drawPlayer(e);
      break;
    case 'enemy':
      drawEnemy(e);
      break;
    case 'bullet':
      drawBullet(e);
      break;
    case 'powerup':
      drawPowerup(e);
      break;
    default:
      ctx.fillStyle = PALETTE[e.type] || '#888';
      ctx.fillRect(e.pos.x, e.pos.y, e.hitbox.w, e.hitbox.h);
  }

  ctx.restore();
}

export function drawHealthBar(e, yOffset) {
  if (!e.alive || e.maxHp <= 0) return;

  const barW = e.hitbox.w;
  const barH = 4;
  const x = e.pos.x + e.hitbox.x;
  const y = e.pos.y + e.hitbox.y - yOffset;

  ctx.fillStyle = 'rgba(0,0,0,0.7)';
  ctx.fillRect(x - 1, y - 1, barW + 2, barH + 2);
  ctx.fillStyle = '#333';
  ctx.fillRect(x, y, barW, barH);
  const ratio = Math.max(0, e.hp / e.maxHp);
  ctx.fillStyle = ratio > 0.5 ? '#44ff44' : ratio > 0.25 ? '#ffaa00' : '#ff4444';
  ctx.fillRect(x, y, barW * ratio, barH);
}